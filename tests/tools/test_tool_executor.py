import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.permissions import InvocationBinding


class FakeReadTool(Tool):
    @property
    def name(self):
        return "read"

    @property
    def description(self):
        return "Read"

    @property
    def input_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, *, tool_input, context):
        await asyncio.sleep(0.05)
        return ToolResult.success("read result")

    def is_read_only(self, input=None):
        return True


class FakeWriteTool(Tool):
    @property
    def name(self):
        return "write"

    @property
    def description(self):
        return "Write"

    @property
    def input_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, *, tool_input, context):
        await asyncio.sleep(0.05)
        return ToolResult.success("write result")

    def is_read_only(self, input=None):
        return False


class FakeRuntimeClassTool(FakeReadTool):
    @property
    def requires_runtime_execution_class(self) -> bool:
        return True

    def is_concurrency_safe(self, tool_input):
        raise AssertionError("runtime-class tools must not use input heuristics")


@pytest.mark.asyncio
class TestToolExecutor:
    async def test_partition(self):
        read_tool, write_tool = FakeReadTool(), FakeWriteTool()
        registry = MagicMock()
        registry.get = lambda name: read_tool if name == "read" else write_tool
        executor = ToolExecutor(registry=registry)
        calls = [
            ToolCallRequest(id="1", name="read", input={}),
            ToolCallRequest(id="2", name="read", input={}),
            ToolCallRequest(id="3", name="write", input={}),
            ToolCallRequest(id="4", name="read", input={}),
        ]
        concurrent, serial = executor.partition(calls)
        assert len(concurrent) == 3
        assert len(serial) == 1

    async def test_runtime_execution_class_controls_partition_and_missing_is_serial(self):
        tool = FakeRuntimeClassTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        calls = [
            ToolCallRequest(id="approved", name="read", input={}, execution_class="concurrent"),
            ToolCallRequest(id="serial", name="read", input={}, execution_class="serial"),
            ToolCallRequest(id="missing", name="read", input={}),
        ]

        concurrent, serial = executor.partition(calls)

        assert [call.id for call in concurrent] == ["approved"]
        assert [call.id for call in serial] == ["serial", "missing"]

    async def test_concurrent_parallel(self):
        class BlockingReadTool(FakeReadTool):
            def __init__(self, expected_calls: int):
                self.expected_calls = expected_calls
                self.started = 0
                self.active = 0
                self.max_active = 0
                self.all_started = asyncio.Event()
                self.release = asyncio.Event()

            async def execute(self, *, tool_input, context):
                self.started += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.started == self.expected_calls:
                    self.all_started.set()

                try:
                    await self.release.wait()
                finally:
                    self.active -= 1
                return ToolResult.success("read result")

        read_tool = BlockingReadTool(expected_calls=5)
        registry = MagicMock()
        registry.get = lambda name: read_tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id=f"r{i}", name="read", input={}) for i in range(5)]
        context = ToolContext()
        task = asyncio.create_task(executor.execute_batch(calls, context))
        try:
            await asyncio.wait_for(read_tool.all_started.wait(), timeout=0.5)
            assert read_tool.max_active == 5

            read_tool.release.set()
            results = await asyncio.wait_for(task, timeout=0.5)
        finally:
            read_tool.release.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        assert len(results) == 5
        assert all(r.content == "read result" for r in results)

    async def test_preserves_tool_context_read_roots(self):
        class CapturingReadTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                roots = ",".join(context.trusted_read_directories)
                return ToolResult.success(roots)

        read_tool = CapturingReadTool()
        registry = MagicMock()
        registry.get = lambda name: read_tool
        executor = ToolExecutor(registry=registry)

        results = await executor.execute_batch(
            [ToolCallRequest(id="read-1", name="read", input={})],
            ToolContext(trusted_read_directories=["/tmp/skill-root"]),
        )

        assert results[0].content == "/tmp/skill-root"

    async def test_preserves_tool_context_relative_read_roots(self):
        class CapturingReadTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                roots = ",".join(context.relative_read_directories)
                return ToolResult.success(roots)

        read_tool = CapturingReadTool()
        registry = MagicMock()
        registry.get = lambda name: read_tool
        executor = ToolExecutor(registry=registry)

        results = await executor.execute_batch(
            [ToolCallRequest(id="read-1", name="read", input={})],
            ToolContext(relative_read_directories=["/tmp/skill-root"]),
        )

        assert results[0].content == "/tmp/skill-root"

    async def test_serial_order(self):
        order = []

        class OrderedWrite(FakeWriteTool):
            async def execute(self, *, tool_input, context):
                order.append(tool_input.get("id"))
                await asyncio.sleep(0.02)
                return ToolResult.success("ok")

        write_tool = OrderedWrite()
        registry = MagicMock()
        registry.get = lambda name: write_tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id=f"w{i}", name="write", input={"id": i}) for i in range(3)]
        await executor.execute_batch(calls, ToolContext())
        assert order == [0, 1, 2]

    async def test_read_after_write_runs_after_write_in_same_batch(self):
        state = {"value": "before"}

        class StateWrite(FakeWriteTool):
            async def execute(self, *, tool_input, context):
                state["value"] = tool_input["value"]
                return ToolResult.success("written")

        class StateRead(FakeReadTool):
            async def execute(self, *, tool_input, context):
                return ToolResult.success(state["value"])

        write_tool = StateWrite()
        read_tool = StateRead()
        registry = MagicMock()
        registry.get = lambda name: write_tool if name == "write" else read_tool
        executor = ToolExecutor(registry=registry)

        results = await executor.execute_batch(
            [
                ToolCallRequest(id="write-1", name="write", input={"value": "after"}),
                ToolCallRequest(id="read-1", name="read", input={}),
            ],
            ToolContext(),
        )

        assert results[0].content == "written"
        assert results[1].content == "after"

    async def test_error_no_block(self, monkeypatch):
        monkeypatch.setattr("iac_code.tools.tool_executor._", lambda message: "i18n:" + message)

        class ErrorTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                raise RuntimeError("boom")

        error_tool, read_tool = ErrorTool(), FakeReadTool()
        registry = MagicMock()
        registry.get = lambda name: error_tool if name == "error" else read_tool
        executor = ToolExecutor(registry=registry)
        calls = [
            ToolCallRequest(id="e1", name="error", input={}),
            ToolCallRequest(id="r1", name="read", input={}),
        ]
        results = await executor.execute_batch(calls, ToolContext())
        assert results[0].is_error is True
        assert results[0].content == "i18n:Tool 'error' failed: boom"
        assert results[1].content == "read result"

    async def test_timeout(self, monkeypatch):
        monkeypatch.setattr("iac_code.tools.tool_executor._", lambda message: "i18n:" + message)

        class SlowTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                await asyncio.sleep(10)
                return ToolResult.success("never")

        slow = SlowTool()
        registry = MagicMock()
        registry.get = lambda name: slow
        executor = ToolExecutor(registry=registry, tool_timeout=0.1)
        calls = [ToolCallRequest(id="s1", name="slow", input={})]
        results = await executor.execute_batch(calls, ToolContext())
        assert results[0].is_error is True
        assert results[0].content == "i18n:Tool 'slow' timed out after 0.1s"

    async def test_unknown_tool_error_uses_gettext(self, monkeypatch):
        monkeypatch.setattr("iac_code.tools.tool_executor._", lambda message: "i18n:" + message)
        registry = MagicMock()
        registry.get.return_value = None
        executor = ToolExecutor(registry=registry)

        results = await executor.execute_batch(
            [ToolCallRequest(id="missing", name="missing", input={})],
            ToolContext(),
        )

        assert results == [ToolResult.error("i18n:Unknown tool: missing")]

    async def test_tool_specific_timeout_result_overrides_generic_message(self):
        class SafeTimeoutTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                await asyncio.sleep(10)
                return ToolResult.success("never")

            def timeout_error_result(self, tool_input, timeout):
                del tool_input, timeout
                return ToolResult.error("safe tool-specific timeout")

        tool = SafeTimeoutTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry, tool_timeout=0.01)

        results = await executor.execute_batch(
            [ToolCallRequest(id="safe-timeout", name="read", input={})],
            ToolContext(),
        )

        assert results == [ToolResult.error("safe tool-specific timeout")]

    async def test_event_queue_is_passed_only_through_context(self):
        class QueueAwareTool(FakeReadTool):
            def __init__(self):
                self._event_queue = None
                self.seen_context_queues = {}

            async def execute(self, *, tool_input, context):
                self.seen_context_queues[tool_input["name"]] = context.event_queue
                return ToolResult.success(tool_input["name"])

        tool = QueueAwareTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        first_queue = asyncio.Queue()
        second_queue = asyncio.Queue()
        calls = [
            ToolCallRequest(id="a", name="read", input={"name": "first"}, event_queue=first_queue),
            ToolCallRequest(id="b", name="read", input={"name": "second"}, event_queue=second_queue),
        ]

        results = await executor.execute_batch(calls, ToolContext())

        assert [result.content for result in results] == ["first", "second"]
        assert tool.seen_context_queues == {"first": first_queue, "second": second_queue}
        assert tool._event_queue is None

    async def test_pipeline_mode_is_preserved_in_derived_tool_context(self):
        class ContextAwareTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                return ToolResult.success(str(context.pipeline_mode))

        tool = ContextAwareTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id="a", name="read", input={})]

        results = await executor.execute_batch(calls, ToolContext(pipeline_mode=True))

        assert results[0].content == "True"

    async def test_env_overrides_are_preserved_in_derived_tool_context(self):
        class ContextAwareTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                return ToolResult.success(context.env_overrides.get("PATH", ""))

        tool = ContextAwareTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id="a", name="read", input={})]

        results = await executor.execute_batch(calls, ToolContext(env_overrides={"PATH": "/tmp/bin"}))

        assert results[0].content == "/tmp/bin"

    async def test_runtime_permission_handoff_is_preserved_in_derived_tool_context(self):
        binding = InvocationBinding("nonce", "session", "call", "read", "a" * 64)

        class ContextAwareTool(FakeRuntimeClassTool):
            async def execute(self, *, tool_input, context):
                assert context.invocation_binding is binding
                assert context.snapshot_id == "snapshot"
                assert context.security_digest == "digest"
                assert context.execution_class == "concurrent"
                return ToolResult.success("preserved")

        tool = ContextAwareTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        call = ToolCallRequest(
            id="call",
            name="read",
            input={},
            invocation_binding=binding,
            snapshot_id="snapshot",
            security_digest="digest",
            execution_class="concurrent",
        )

        results = await executor.execute_batch([call], ToolContext())

        assert results[0].content == "preserved"


class FakeStrictTool(Tool):
    @property
    def name(self):
        return "strict"

    @property
    def description(self):
        return "Strict"

    @property
    def input_schema(self):
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        }

    async def execute(self, *, tool_input, context):
        return ToolResult.success(f"got {tool_input['path']}")

    def is_read_only(self, input=None):
        return True


@pytest.mark.asyncio
class TestToolExecutorValidation:
    async def test_valid_input_executes(self):
        tool = FakeStrictTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id="v1", name="strict", input={"path": "/tmp/f"})]
        results = await executor.execute_batch(calls, ToolContext())
        assert results[0].is_error is False
        assert "got /tmp/f" in results[0].content

    async def test_invalid_input_returns_error(self, monkeypatch):
        monkeypatch.setattr("iac_code.tools.tool_executor._", lambda message: "i18n:" + message)
        tool = FakeStrictTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id="v2", name="strict", input={})]
        results = await executor.execute_batch(calls, ToolContext())
        assert results[0].is_error is True
        assert results[0].content.startswith("i18n:Invalid input for tool 'strict':")
        assert "path" in results[0].content

    async def test_invalid_input_does_not_execute(self):
        executed = []

        class TrackingTool(FakeStrictTool):
            async def execute(self, *, tool_input, context):
                executed.append(True)
                return ToolResult.success("ran")

        tool = TrackingTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id="v3", name="strict", input={})]
        await executor.execute_batch(calls, ToolContext())
        assert len(executed) == 0

    async def test_repeated_identical_invalid_input_escalates_error(self):
        tool = FakeStrictTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)

        first = await executor.execute_batch([ToolCallRequest(id="r1", name="strict", input={})], ToolContext())
        second = await executor.execute_batch([ToolCallRequest(id="r2", name="strict", input={})], ToolContext())

        assert first[0].is_error is True
        assert "already rejected" not in first[0].content
        assert second[0].is_error is True
        assert "already rejected" in second[0].content
        # The original actionable detail must survive the escalation.
        assert "path" in second[0].content

    async def test_different_invalid_input_does_not_escalate(self):
        tool = FakeStrictTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)

        first = await executor.execute_batch([ToolCallRequest(id="d1", name="strict", input={})], ToolContext())
        second = await executor.execute_batch(
            [ToolCallRequest(id="d2", name="strict", input={"path": 123})], ToolContext()
        )

        assert "already rejected" not in first[0].content
        assert second[0].is_error is True
        assert "already rejected" not in second[0].content

    async def test_repeated_invalid_input_escalates_tool_specific_error(self):
        class ToolErrorTool(FakeStrictTool):
            def validation_error_result(self, tool_input):
                return ToolResult.error("tool specific detail")

        tool = ToolErrorTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)

        first = await executor.execute_batch([ToolCallRequest(id="t1", name="strict", input={})], ToolContext())
        second = await executor.execute_batch([ToolCallRequest(id="t2", name="strict", input={})], ToolContext())

        assert first[0].content == "tool specific detail"
        assert "already rejected" in second[0].content
        assert "tool specific detail" in second[0].content

    async def test_unserializable_invalid_input_does_not_break_validation(self):
        tool = FakeStrictTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)

        unserializable = {"path": {object()}}
        results = await executor.execute_batch(
            [ToolCallRequest(id="u1", name="strict", input=unserializable)], ToolContext()
        )

        assert results[0].is_error is True

    async def test_repeated_rejection_tracking_is_bounded(self):
        from iac_code.tools.tool_executor import _MAX_REJECTED_INPUT_DIGESTS

        tool = FakeStrictTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)

        for index in range(_MAX_REJECTED_INPUT_DIGESTS + 10):
            await executor.execute_batch(
                [ToolCallRequest(id=f"b{index}", name="strict", input={"path": index})],
                ToolContext(),
            )

        assert len(executor._rejected_input_digests) <= _MAX_REJECTED_INPUT_DIGESTS


def _warning_outcome(label: str):
    from iac_code.tools.cloud.aliyun.ros_validation.model import (
        Category,
        Severity,
        ValidationReport,
        make_diagnostic,
    )
    from iac_code.tools.cloud.aliyun.ros_validation.outcome import outcome_from_report

    report = ValidationReport.build(
        [
            make_diagnostic(
                code="ROS5999",
                severity=Severity.WARNING,
                category=Category.QUALITY,
                summary="warning-{}".format(label),
                detail="detail-{}".format(label),
                stable_args=(label,),
            )
        ]
    )
    return outcome_from_report(report)


def test_attach_ros_validation_merges_by_diagnostic_id_and_preserves_existing_payload():
    from iac_code.tools.cloud.aliyun.ros_validation.model import (
        Category,
        Severity,
        ValidationReport,
        make_diagnostic,
    )
    from iac_code.tools.cloud.aliyun.ros_validation.outcome import attach_ros_validation, outcome_from_report

    existing_diagnostic = make_diagnostic(
        code="ROS-EXISTING",
        severity=Severity.WARNING,
        category=Category.QUALITY,
        summary="existing",
        detail="existing",
    )
    new_diagnostic = make_diagnostic(
        code="ROS-NEW",
        severity=Severity.LIMITATION,
        category=Category.LIMITATION,
        summary="new",
        detail="new",
    )
    existing_report = ValidationReport.build([existing_diagnostic]).to_dict()
    existing_report["server_field"] = "keep"
    result = ToolResult(
        content="api-payload",
        is_error=False,
        metadata={"ros_validation": existing_report, "api_metadata": "keep"},
    )
    outcome = outcome_from_report(ValidationReport.build([new_diagnostic]))

    merged = attach_ros_validation(result, outcome)
    merged_twice = attach_ros_validation(merged, outcome)

    diagnostics = merged_twice.metadata["ros_validation"]["diagnostics"]
    assert [item["diagnostic_id"] for item in diagnostics] == [
        existing_diagnostic.diagnostic_id,
        new_diagnostic.diagnostic_id,
    ]
    assert merged_twice.metadata["ros_validation"]["server_field"] == "keep"
    assert merged_twice.metadata["api_metadata"] == "keep"
    assert merged_twice.content.startswith("api-payload")


class FakeRosPreflightTool(FakeReadTool):
    async def execute(self, *, tool_input, context):
        outcome = _warning_outcome(tool_input["label"])
        context.ros_preflight_outcome = outcome
        mode = tool_input.get("mode", "success")
        if mode == "server-error":
            result = ToolResult.error("service failed")
            from iac_code.tools.cloud.aliyun.ros_validation.outcome import attach_ros_validation

            return attach_ros_validation(result, outcome)
        if mode == "exception":
            raise RuntimeError("after preflight")
        if mode == "timeout":
            await asyncio.sleep(10)
        result = ToolResult.success("payload-{}".format(tool_input["label"]))
        from iac_code.tools.cloud.aliyun.ros_validation.outcome import attach_ros_validation

        return attach_ros_validation(result, outcome)


class FakeUnattachedRosPreflightTool(FakeReadTool):
    async def execute(self, *, tool_input, context):
        context.ros_preflight_outcome = _warning_outcome(tool_input["label"])
        return ToolResult.success("payload-{}".format(tool_input["label"]))


@pytest.mark.asyncio
class TestRosPreflightOutcomePropagation:
    async def test_normal_return_is_the_adapter_attachment_boundary(self):
        registry = MagicMock()
        registry.get = lambda name: FakeUnattachedRosPreflightTool()

        results = await ToolExecutor(registry=registry).execute_batch(
            [ToolCallRequest(id="normal", name="read", input={"label": "normal"})],
            ToolContext(),
        )

        assert results[0].content == "payload-normal"
        assert not results[0].metadata

    async def test_trusted_ros_account_context_survives_per_invocation_context_copy(self):
        from iac_code.tools.cloud.aliyun.ros_validation.model import TrustedRosAccountContext

        trusted = TrustedRosAccountContext("tenant", "owner", "production", "host-test")

        class ContextProbeTool(FakeReadTool):
            async def execute(self, *, tool_input, context):
                del tool_input
                assert context.trusted_ros_account_context is trusted
                return ToolResult.success("ok")

        registry = MagicMock()
        registry.get = lambda name: ContextProbeTool()
        results = await ToolExecutor(registry=registry).execute_batch(
            [ToolCallRequest(id="trusted", name="read", input={})],
            ToolContext(trusted_ros_account_context=trusted),
        )
        assert results[0].content == "ok"

    async def test_warning_attaches_to_success_and_server_error_without_overwriting_payload(self):
        tool = FakeRosPreflightTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        results = await ToolExecutor(registry=registry).execute_batch(
            [
                ToolCallRequest(id="success", name="read", input={"label": "success"}),
                ToolCallRequest(id="server-error", name="read", input={"label": "server", "mode": "server-error"}),
            ],
            ToolContext(),
        )

        assert results[0].content.startswith("payload-success")
        assert not results[0].is_error
        assert results[1].content.startswith("service failed")
        assert results[1].is_error
        assert results[0].metadata["ros_validation"]["diagnostics"][0]["summary"] == "warning-success"
        assert results[1].metadata["ros_validation"]["diagnostics"][0]["summary"] == "warning-server"

    async def test_warning_attaches_after_exception_and_timeout(self):
        tool = FakeRosPreflightTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        exception_result = await ToolExecutor(registry=registry).execute_batch(
            [ToolCallRequest(id="exception", name="read", input={"label": "exception", "mode": "exception"})],
            ToolContext(),
        )
        timeout_result = await ToolExecutor(registry=registry, tool_timeout=0.01).execute_batch(
            [ToolCallRequest(id="timeout", name="read", input={"label": "timeout", "mode": "timeout"})],
            ToolContext(),
        )

        assert exception_result[0].is_error
        assert "after preflight" in exception_result[0].content
        assert exception_result[0].metadata["ros_validation"]["diagnostics"][0]["summary"] == "warning-exception"
        assert timeout_result[0].is_error
        assert "timed out" in timeout_result[0].content
        assert timeout_result[0].metadata["ros_validation"]["diagnostics"][0]["summary"] == "warning-timeout"

    async def test_concurrent_invocations_do_not_share_outcomes(self):
        tool = FakeRosPreflightTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        results = await ToolExecutor(registry=registry).execute_batch(
            [
                ToolCallRequest(id="a", name="read", input={"label": "a"}),
                ToolCallRequest(id="b", name="read", input={"label": "b"}),
            ],
            ToolContext(),
        )

        summaries = [item.metadata["ros_validation"]["diagnostics"][0]["summary"] for item in results]
        assert summaries == ["warning-a", "warning-b"]
