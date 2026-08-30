import asyncio

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message, ToolUseBlock
from iac_code.providers.base import ToolDefinition
from iac_code.services.permission_wait import (
    PermissionWaitPolicy,
    build_permission_checkpoint,
    canonical_digest,
    recover_permission_audit_boundary,
)
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionAuditMetadata, PermissionResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    PermissionWaitOutcome,
    PermissionWaitSuspended,
    StackProgressEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

USER_DENIED_TOOL_RESULT = (
    "The user explicitly denied this tool operation. This is not a cloud API or IAM permission error. "
    "Do not retry this operation or perform the same action with another tool unless the user asks again."
)


class WriteTool(Tool):
    def __init__(self) -> None:
        self.executed = False

    @property
    def name(self) -> str:
        return "write_test"

    @property
    def description(self) -> str:
        return "Write test content."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        self.executed = True
        return ToolResult.success(f"wrote {tool_input['value']}")

    async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
        return PermissionResult(behavior="ask", message="Allow write?")


class FakeProviderManager:
    def get_model_name(self) -> str:
        return "fake"

    async def stream(
        self,
        messages,
        system,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ):
        yield MessageStartEvent(message_id="m1")
        yield TextDeltaEvent(text="I will write.")
        yield ToolUseStartEvent(tool_use_id="tool1", name="write_test")
        yield ToolUseEndEvent(tool_use_id="tool1", name="write_test", input={"value": "ok"})
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


@pytest.mark.asyncio
async def test_agent_loop_emits_permission_request_before_write_tool() -> None:
    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        provider_manager=FakeProviderManager(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )

    events = []
    async for event in loop.run_streaming("write"):
        events.append(event)
        if isinstance(event, PermissionRequestEvent):
            assert event.response_future is not None
            event.response_future.set_result(False)

    assert any(isinstance(event, PermissionRequestEvent) for event in events)
    assert any(
        isinstance(event, ToolResultEvent) and event.is_error and event.result == USER_DENIED_TOOL_RESULT
        for event in events
    )
    assert tool.executed is False


@pytest.mark.asyncio
async def test_cancelled_permission_request_resolves_future_as_denied() -> None:
    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        provider_manager=FakeProviderManager(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )
    permission_ready = asyncio.Event()
    captured_future: asyncio.Future[bool] | None = None

    async def consume() -> None:
        nonlocal captured_future
        async for event in loop.run_streaming("write"):
            if isinstance(event, PermissionRequestEvent):
                assert event.response_future is not None
                captured_future = event.response_future
                permission_ready.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(permission_ready.wait(), timeout=1)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured_future is not None
    assert captured_future.done()
    assert captured_future.cancelled() is False
    assert captured_future.result() is False
    assert tool.executed is False


@pytest.mark.asyncio
async def test_permission_suspend_creates_no_denied_tool_result() -> None:
    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        provider_manager=FakeProviderManager(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )
    events = []

    with pytest.raises(PermissionWaitSuspended):
        async for event in loop.run_streaming("write"):
            events.append(event)
            if isinstance(event, PermissionRequestEvent):
                assert event.continuation_frame is not None
                assert event.continuation_frame["orderedToolUseIds"] == ["tool1"]
                assert event.response_future is not None
                event.response_future.set_result(PermissionWaitOutcome.SUSPEND)

    assert tool.executed is False
    assert not any(isinstance(event, ToolResultEvent) for event in events)


@pytest.mark.asyncio
async def test_resume_permission_boundary_executes_ordered_multi_tool_batch_once() -> None:
    class RecordingTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.values: list[str] = []

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.values.append(tool_input["value"])
            return ToolResult.success("wrote {}".format(tool_input["value"]))

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield TextDeltaEvent(text="Done.")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    assistant = Message(
        role="assistant",
        content=[
            ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"}),
            ToolUseBlock(id="tool-2", name="write_test", input={"value": "second"}),
        ],
    )
    digest = canonical_digest([block.model_dump(mode="json") for block in assistant.content])
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-2",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "second"}}),
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": digest,
            "orderedToolUseIds": ["tool-1", "tool-2"],
            "currentIndex": 1,
            "decisions": [
                {
                    "toolUseId": "tool-1",
                    "state": "allow",
                    "source": "user",
                    "principalRef": None,
                    "region": None,
                    "deniedResult": None,
                },
                {"toolUseId": "tool-2", "state": "pending", "source": None, "deniedResult": None},
            ],
        },
    }

    events = [event async for event in loop.resume_permission_boundary(checkpoint)]

    assert tool.values == ["first", "second"]
    assert [event.tool_use_id for event in events if isinstance(event, ToolResultEvent)] == [
        "tool-1",
        "tool-2",
    ]
    assert not any(isinstance(event, PermissionRequestEvent) for event in events)


@pytest.mark.asyncio
async def test_resume_permission_boundary_tells_model_the_user_denied_operation() -> None:
    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "blocked"})],
    )
    digest = canonical_digest([block.model_dump(mode="json") for block in assistant.content])
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-1",
        "toolName": "write_test",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "blocked"}}),
        "decision": {"status": "claimed", "value": "deny", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": digest,
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [
                {"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None},
            ],
        },
    }

    events = [event async for event in loop.resume_permission_boundary(checkpoint)]

    denied = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(denied) == 1
    assert denied[0].is_error is True
    assert denied[0].result == USER_DENIED_TOOL_RESULT
    assert tool.executed is False


@pytest.mark.asyncio
async def test_resume_permission_boundary_forwards_tool_progress_events() -> None:
    class ProgressTool(WriteTool):
        def needs_event_queue(self) -> bool:
            return True

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            assert context.event_queue is not None
            await context.event_queue.put(
                StackProgressEvent(
                    stack_id="stack-1",
                    stack_name="demo",
                    status="CREATE_COMPLETE",
                    progress_percentage=100,
                    resources=[],
                    elapsed_seconds=1,
                    region_id="cn-hangzhou",
                    tool_use_id="tool-1",
                )
            )
            return ToolResult.success("created")

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = ProgressTool()
    registry = ToolRegistry()
    registry.register(tool)
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"})],
    )
    digest = canonical_digest([block.model_dump(mode="json") for block in assistant.content])
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-1",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "first"}}),
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": digest,
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [
                {"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None},
            ],
        },
    }

    events = [event async for event in loop.resume_permission_boundary(checkpoint)]

    progress_index = next(index for index, event in enumerate(events) if isinstance(event, StackProgressEvent))
    result_index = next(index for index, event in enumerate(events) if isinstance(event, ToolResultEvent))
    assert progress_index < result_index


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resume_messages", "loop_identity", "message_ref"),
    [
        (
            [
                Message(
                    role="assistant",
                    content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "same"})],
                ),
                Message(
                    role="assistant",
                    content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "same"})],
                ),
            ],
            {},
            "session.jsonl:0",
        ),
        (
            [
                Message(
                    role="assistant",
                    content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "same"})],
                )
            ],
            {
                "session_id": "transcript_b",
                "root_session_id": "root-session",
                "transcript_id": "transcript_b",
            },
            "pipeline/transcripts/transcript_a/session.jsonl:0",
        ),
    ],
)
async def test_resume_permission_boundary_requires_exact_runtime_message_reference(
    resume_messages,
    loop_identity,
    message_ref,
) -> None:
    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            raise AssertionError("message reference mismatch must fail before continuation")
            yield

    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    assistant = resume_messages[-1]
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=resume_messages,
        **loop_identity,
    )
    checkpoint = {
        "toolUseId": "tool-1",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "same"}}),
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": message_ref,
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
        },
    }

    with pytest.raises(ValueError, match="assistant message reference changed"):
        async for _event in loop.resume_permission_boundary(checkpoint):
            pass

    assert tool.executed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trailing_behavior", "audit_succeeds", "expected_values", "expected_error"),
    [
        ("allow", True, ["first", "second", "third"], False),
        ("allow", False, ["first", "second"], True),
        ("deny", True, ["first", "second"], True),
    ],
)
async def test_resume_permission_boundary_audits_later_policy_decisions(
    monkeypatch,
    trailing_behavior,
    audit_succeeds,
    expected_values,
    expected_error,
) -> None:
    class PolicyTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.values: list[str] = []

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.values.append(tool_input["value"])
            return ToolResult.success("wrote {}".format(tool_input["value"]))

        async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
            value = input["value"]
            if value == "second":
                return PermissionResult(behavior="ask", message="Allow second?")
            behavior = trailing_behavior if value == "third" else "allow"
            return PermissionResult(behavior=behavior, message="Policy denied.")

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = PolicyTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool_uses = [
        ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"}),
        ToolUseBlock(id="tool-2", name="write_test", input={"value": "second"}),
        ToolUseBlock(id="tool-3", name="write_test", input={"value": "third"}),
    ]
    assistant = Message(role="assistant", content=tool_uses)
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-2",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "second"}}),
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": [tool_use.id for tool_use in tool_uses],
            "currentIndex": 1,
            "decisions": [
                {"toolUseId": "tool-1", "state": "allow", "source": "policy", "deniedResult": None},
                {"toolUseId": "tool-2", "state": "pending", "source": None, "deniedResult": None},
                {"toolUseId": "tool-3", "state": "not_evaluated", "source": None, "deniedResult": None},
            ],
        },
    }
    audit_calls: list[str] = []

    def audit(**kwargs) -> bool:
        audit_calls.append(kwargs["decision"])
        return audit_succeeds

    monkeypatch.setattr("iac_code.agent.agent_loop._emit_no_prompt_permission_audit", audit)

    events = [event async for event in loop.resume_permission_boundary(checkpoint)]

    assert audit_calls == [trailing_behavior]
    assert tool.values == expected_values
    trailing_results = [
        event for event in events if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-3"
    ]
    assert len(trailing_results) == 1
    assert trailing_results[0].is_error is expected_error


@pytest.mark.asyncio
async def test_resume_permission_boundary_does_not_reuse_prior_policy_allow_when_now_ask() -> None:
    class RecordingTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.values: list[str] = []

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.values.append(tool_input["value"])
            return ToolResult.success("ok")

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool_uses = [
        ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"}),
        ToolUseBlock(id="tool-2", name="write_test", input={"value": "second"}),
    ]
    assistant = Message(role="assistant", content=tool_uses)
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-2",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "second"}}),
        "principalRef": None,
        "region": None,
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-2"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": [tool_use.id for tool_use in tool_uses],
            "currentIndex": 1,
            "decisions": [
                {"toolUseId": "tool-1", "state": "allow", "source": "policy", "deniedResult": None},
                {"toolUseId": "tool-2", "state": "pending", "source": None, "deniedResult": None},
            ],
        },
    }

    events = [event async for event in loop.resume_permission_boundary(checkpoint)]

    assert tool.values == ["second"]
    first_result = next(
        event for event in events if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-1"
    )
    assert first_result.is_error is True


@pytest.mark.asyncio
async def test_resume_permission_boundary_revalidates_each_prior_user_approval_identity(monkeypatch) -> None:
    class RecordingTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.values: list[str] = []

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.values.append(tool_input["value"])
            return ToolResult.success("ok")

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool_uses = [
        ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"}),
        ToolUseBlock(id="tool-2", name="write_test", input={"value": "second"}),
    ]
    assistant = Message(role="assistant", content=tool_uses)
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-2",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "second"}}),
        "principalRef": "principal-current",
        "region": "cn-current",
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-2"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": [tool_use.id for tool_use in tool_uses],
            "currentIndex": 1,
            "decisions": [
                {
                    "toolUseId": "tool-1",
                    "state": "allow",
                    "source": "user",
                    "principalRef": "principal-old",
                    "region": "cn-old",
                    "deniedResult": None,
                },
                {"toolUseId": "tool-2", "state": "pending", "source": None, "deniedResult": None},
            ],
        },
    }
    monkeypatch.setattr(
        "iac_code.agent.agent_loop.permission_execution_identity",
        lambda **kwargs: (
            ("principal-new", "cn-new")
            if kwargs["tool_input"]["value"] == "first"
            else ("principal-current", "cn-current")
        ),
    )

    events = [event async for event in loop.resume_permission_boundary(checkpoint)]

    assert tool.values == ["second"]
    first_result = next(
        event for event in events if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-1"
    )
    assert first_result.is_error is True


@pytest.mark.asyncio
async def test_resume_permission_boundary_persists_hard_denies_across_successor(monkeypatch) -> None:
    class RecordingTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.values: list[str] = []
            self.first_behavior = "deny"
            self.second_behavior = "deny"

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.values.append(tool_input["value"])
            return ToolResult.success("ok")

        async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
            if input["value"] == "first":
                return PermissionResult(behavior=self.first_behavior, message="Policy denied.")
            if input["value"] == "second":
                return PermissionResult(behavior=self.second_behavior, message="Policy denied.")
            return PermissionResult(behavior="ask", message="Allow write?")

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool_uses = [
        ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"}),
        ToolUseBlock(id="tool-2", name="write_test", input={"value": "second"}),
        ToolUseBlock(id="tool-3", name="write_test", input={"value": "third"}),
    ]
    assistant = Message(role="assistant", content=tool_uses)
    message_digest = canonical_digest([block.model_dump(mode="json") for block in assistant.content])
    first_loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    audit_calls: list[tuple[str, str]] = []

    def audit(**kwargs) -> bool:
        audit_calls.append((kwargs["request"].id, kwargs["decision"]))
        return True

    monkeypatch.setattr("iac_code.agent.agent_loop._emit_no_prompt_permission_audit", audit)
    first_checkpoint = {
        "boundaryId": "pwb_firstboundary",
        "toolUseId": "tool-2",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "second"}}),
        "principalRef": None,
        "region": None,
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-2"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": message_digest,
            "orderedToolUseIds": [tool_use.id for tool_use in tool_uses],
            "currentIndex": 1,
            "decisions": [
                {"toolUseId": "tool-1", "state": "allow", "source": "policy", "deniedResult": None},
                {"toolUseId": "tool-2", "state": "pending", "source": None, "deniedResult": None},
                {"toolUseId": "tool-3", "state": "not_evaluated", "source": None, "deniedResult": None},
            ],
        },
    }

    first_stream = first_loop.resume_permission_boundary(first_checkpoint)
    successor = await anext(first_stream)
    assert isinstance(successor, PermissionRequestEvent)
    assert successor.tool_use_id == "tool-3"
    assert successor.continuation_frame is not None
    assert successor.continuation_frame["decisions"][0]["state"] == "deny"
    assert successor.continuation_frame["decisions"][0]["source"] == "policy"
    assert successor.continuation_frame["decisions"][1]["state"] == "deny"
    assert successor.continuation_frame["decisions"][1]["source"] == "policy"
    assert audit_calls == [("tool-1", "deny"), ("tool-2", "deny")]
    assert successor.response_future is not None
    successor.response_future.set_result(PermissionWaitOutcome.SUSPEND)
    with pytest.raises(PermissionWaitSuspended):
        await anext(first_stream)

    tool.first_behavior = "allow"
    tool.second_behavior = "allow"
    second_loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    second_checkpoint = {
        "toolUseId": "tool-3",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "third"}}),
        "principalRef": None,
        "region": None,
        "decision": {"status": "claimed", "value": "deny", "claimId": "claim-3"},
        "continuationFrame": successor.continuation_frame,
    }

    events = [event async for event in second_loop.resume_permission_boundary(second_checkpoint)]

    assert tool.values == []
    first_result = next(
        event for event in events if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-1"
    )
    second_result = next(
        event for event in events if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-2"
    )
    assert first_result.is_error is True
    assert second_result.is_error is True


@pytest.mark.asyncio
async def test_live_successor_frame_records_each_user_approval_identity(monkeypatch) -> None:
    class TwoToolProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield ToolUseStartEvent(tool_use_id="tool-1", name="write_test")
            yield ToolUseEndEvent(tool_use_id="tool-1", name="write_test", input={"value": "first"})
            yield ToolUseStartEvent(tool_use_id="tool-2", name="write_test")
            yield ToolUseEndEvent(tool_use_id="tool-2", name="write_test", input={"value": "second"})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        provider_manager=TwoToolProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )
    monkeypatch.setattr(
        "iac_code.agent.agent_loop.permission_execution_identity",
        lambda **kwargs: (
            "principal-{}".format(kwargs["tool_input"]["value"]),
            "region-{}".format(kwargs["tool_input"]["value"]),
        ),
    )
    permission_events: list[PermissionRequestEvent] = []

    async for event in loop.run_streaming("write twice"):
        if not isinstance(event, PermissionRequestEvent):
            continue
        permission_events.append(event)
        assert event.response_future is not None
        if len(permission_events) == 1:
            event.boundary_id = "pwb_firstboundary"
            event.response_future.set_result(True)
        else:
            assert event.continuation_frame is not None
            assert event.continuation_frame["decisions"][0] == {
                "toolUseId": "tool-1",
                "state": "allow",
                "source": "user",
                "deniedResult": None,
                "principalRef": "principal-first",
                "region": "region-first",
            }
            event.response_future.set_result(False)

    assert len(permission_events) == 2


@pytest.mark.asyncio
async def test_permission_checkpoint_digest_uses_raw_transcript_input_before_prepare() -> None:
    class PreparedTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.executed_input: dict | None = None

        def prepare_invocation_input(self, tool_input: dict) -> dict:
            return {**tool_input, "region_id": "cn-prepared"}

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.executed_input = tool_input
            return ToolResult.success("ok")

    class OneToolProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield ToolUseStartEvent(tool_use_id="tool-1", name="write_test")
            yield ToolUseEndEvent(tool_use_id="tool-1", name="write_test", input={"value": "raw"})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = PreparedTool()
    registry = ToolRegistry()
    registry.register(tool)
    live_loop = AgentLoop(
        provider_manager=OneToolProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )
    permission_event = None
    with pytest.raises(PermissionWaitSuspended):
        async for event in live_loop.run_streaming("write"):
            if isinstance(event, PermissionRequestEvent):
                permission_event = event
                assert event.response_future is not None
                event.response_future.set_result(PermissionWaitOutcome.SUSPEND)
    assert permission_event is not None
    assert permission_event.tool_input == {"value": "raw", "region_id": "cn-prepared"}
    assert permission_event.continuation_frame is not None
    raw_payload_digest = canonical_digest({"name": "write_test", "input": {"value": "raw"}})
    assert permission_event.continuation_frame["currentPayloadDigest"] == raw_payload_digest

    checkpoint = build_permission_checkpoint(
        session_id="session-1",
        task_id=None,
        context_id="context-1",
        input_id="input-1",
        tool_use_id="tool-1",
        tool_name="write_test",
        tool_input=permission_event.tool_input,
        permission_class="normal",
        continuation_frame=permission_event.continuation_frame,
        policy=PermissionWaitPolicy(),
    )
    checkpoint["decision"] = {"status": "claimed", "value": "allow_once", "claimId": "claim-1"}
    assert checkpoint["payloadDigest"] == raw_payload_digest

    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "raw"})],
    )
    resumed_loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[Message(role="user", content="write"), assistant],
    )
    _events = [event async for event in resumed_loop.resume_permission_boundary(checkpoint)]

    assert tool.executed_input == {"value": "raw", "region_id": "cn-prepared"}


@pytest.mark.asyncio
async def test_permission_recovery_after_tool_injected_messages_uses_persisted_transcript(tmp_path) -> None:
    class InjectingTool(Tool):
        @property
        def name(self) -> str:
            return "inject_skill"

        @property
        def description(self) -> str:
            return "Inject skill instructions."

        @property
        def input_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        async def check_permissions(self, input: dict, context=None) -> PermissionResult:
            return PermissionResult(behavior="allow")

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            return ToolResult(
                content="skill loaded",
                new_messages=[{"role": "user", "content": "<skill>persisted instructions</skill>"}],
            )

    class RecordingWriteTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.execution_count = 0

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.execution_count += 1
            return ToolResult(
                content="wrote {}".format(tool_input["value"]),
                new_messages=[{"role": "user", "content": "post-approval instructions"}],
            )

    class TwoTurnProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            self.calls += 1
            if self.calls == 1:
                yield MessageStartEvent(message_id="load-skill")
                yield ToolUseStartEvent(tool_use_id="tool-skill", name="inject_skill")
                yield ToolUseEndEvent(tool_use_id="tool-skill", name="inject_skill", input={})
            else:
                yield MessageStartEvent(message_id="write")
                yield ToolUseStartEvent(tool_use_id="tool-write", name="write_test")
                yield ToolUseEndEvent(tool_use_id="tool-write", name="write_test", input={"value": "ok"})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = str(workspace)
    session_id = "session-with-injected-messages"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    write_tool = RecordingWriteTool()
    registry = ToolRegistry()
    registry.register(InjectingTool())
    registry.register(write_tool)
    live_loop = AgentLoop(
        provider_manager=TwoTurnProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=2,
        session_storage=storage,
        session_id=session_id,
        cwd=cwd,
    )

    permission_event = None
    with pytest.raises(PermissionWaitSuspended):
        async for event in live_loop.run_streaming("run"):
            if isinstance(event, PermissionRequestEvent):
                permission_event = event
                assert event.response_future is not None
                event.response_future.set_result(PermissionWaitOutcome.SUSPEND)

    assert permission_event is not None
    assert permission_event.continuation_frame is not None
    persisted = storage.load(cwd, session_id)
    assert [message.role for message in persisted] == ["user", "assistant", "user", "user", "assistant"]
    assert persisted[3].content == "<skill>persisted instructions</skill>"
    assert permission_event.continuation_frame["assistantMessageRef"] == "session.jsonl:4"
    assert len(persisted) == len(live_loop.context_manager.get_messages())

    checkpoint = build_permission_checkpoint(
        session_id=session_id,
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id=permission_event.tool_use_id,
        tool_name=permission_event.tool_name,
        tool_input=permission_event.tool_input,
        permission_class="normal",
        continuation_frame=permission_event.continuation_frame,
        policy=PermissionWaitPolicy(),
    )
    checkpoint["decision"] = {"status": "claimed", "value": "allow_once", "claimId": "claim-1"}
    resumed_loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        session_storage=storage,
        session_id=session_id,
        cwd=cwd,
        resume_messages=persisted,
    )

    _events = [event async for event in resumed_loop.resume_permission_boundary(checkpoint)]

    assert write_tool.execution_count == 1
    persisted_after_resume = storage.load(cwd, session_id)
    assert persisted_after_resume[-1].content == "post-approval instructions"
    assert len(persisted_after_resume) == len(resumed_loop.context_manager.get_messages())


@pytest.mark.asyncio
async def test_permission_recovery_uses_full_session_index_when_resumed_context_is_partial(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = str(workspace)
    session_id = "session-with-partial-resume-context"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    history = [
        Message(role="user", content="first request"),
        Message(role="assistant", content="first answer"),
        Message(role="user", content="second request"),
        Message(role="assistant", content="second answer"),
    ]
    for message in history:
        storage.append(cwd, session_id, message)

    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    live_loop = AgentLoop(
        provider_manager=FakeProviderManager(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        session_storage=storage,
        session_id=session_id,
        cwd=cwd,
        resume_messages=history[-2:],
    )

    permission_event = None
    with pytest.raises(PermissionWaitSuspended):
        async for event in live_loop.run_streaming("write"):
            if isinstance(event, PermissionRequestEvent):
                permission_event = event
                assert event.response_future is not None
                event.response_future.set_result(PermissionWaitOutcome.SUSPEND)

    assert permission_event is not None
    assert permission_event.continuation_frame is not None
    persisted = storage.load(cwd, session_id)
    assert len(live_loop.context_manager.get_messages()) == 4
    assert len(persisted) == 6
    assert permission_event.continuation_frame["assistantMessageRef"] == "session.jsonl:5"

    checkpoint = build_permission_checkpoint(
        session_id=session_id,
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id=permission_event.tool_use_id,
        tool_name=permission_event.tool_name,
        tool_input=permission_event.tool_input,
        permission_class="normal",
        continuation_frame=permission_event.continuation_frame,
        policy=PermissionWaitPolicy(),
    )
    recovered = recover_permission_audit_boundary(
        checkpoint,
        cwd=cwd,
        session_id=session_id,
        storage=storage,
    )

    assert recovered is not None
    assert recovered.tool_use_id == "tool1"
    assert recovered.tool_input == {"value": "ok"}


@pytest.mark.asyncio
async def test_resume_permission_boundary_fails_closed_when_secondary_audits_fail(monkeypatch) -> None:
    secondary_audit = PermissionAuditMetadata(scope="path_constraint", source="permission_pipeline")

    class RecordingTool(WriteTool):
        def __init__(self) -> None:
            super().__init__()
            self.values: list[str] = []

        async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
            return PermissionResult(
                behavior="ask",
                message="Allow write?",
                audit_items=(secondary_audit,),
            )

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            self.values.append(tool_input["value"])
            return ToolResult.success("ok")

    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool_uses = [
        ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"}),
        ToolUseBlock(id="tool-2", name="write_test", input={"value": "second"}),
    ]
    assistant = Message(role="assistant", content=tool_uses)
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-1",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "first"}}),
        "principalRef": None,
        "region": None,
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": [tool_use.id for tool_use in tool_uses],
            "currentIndex": 0,
            "decisions": [
                {"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None},
                {"toolUseId": "tool-2", "state": "not_evaluated", "source": None, "deniedResult": None},
            ],
        },
    }
    audit_calls: list[tuple[str, str]] = []

    def fail_secondary_audit(**kwargs) -> bool:
        audit_calls.append((kwargs["request"].id, kwargs["decision"]))
        return False

    monkeypatch.setattr("iac_code.agent.agent_loop._emit_permission_audit_items", fail_secondary_audit)
    events = []
    async for event in loop.resume_permission_boundary(checkpoint):
        events.append(event)
        if isinstance(event, PermissionRequestEvent):
            assert event.tool_use_id == "tool-2"
            assert event.response_future is not None
            event.response_future.set_result(True)

    assert audit_calls == [("tool-1", "allow"), ("tool-2", "allow")]
    assert tool.values == []
    assert all(
        event.is_error
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id in {"tool-1", "tool-2"}
    )


@pytest.mark.asyncio
async def test_resume_permission_boundary_rejects_unavailable_current_tool() -> None:
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(id="tool-1", name="removed_tool", input={"value": "raw"})],
    )
    loop = AgentLoop(
        provider_manager=FakeProviderManager(),
        system_prompt="system",
        tool_registry=ToolRegistry(),
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-1",
        "payloadDigest": canonical_digest({"name": "removed_tool", "input": {"value": "raw"}}),
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
        },
    }

    with pytest.raises(ValueError, match="current tool is unavailable"):
        async for _event in loop.resume_permission_boundary(checkpoint):
            pass


@pytest.mark.asyncio
async def test_resume_permission_boundary_rejects_changed_cloud_identity(monkeypatch) -> None:
    class ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None):
            raise AssertionError("identity mismatch must fail before provider continuation")
            yield

    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "first"})],
    )
    digest = canonical_digest([block.model_dump(mode="json") for block in assistant.content])
    loop = AgentLoop(
        provider_manager=ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
    )
    checkpoint = {
        "toolUseId": "tool-1",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "first"}}),
        "principalRef": "aliyun:original",
        "region": "cn-shanghai",
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": digest,
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
        },
    }
    monkeypatch.setattr(
        "iac_code.agent.agent_loop.permission_execution_identity",
        lambda **_kwargs: ("aliyun:changed", "cn-shanghai"),
    )

    with pytest.raises(ValueError, match="cloud execution identity changed"):
        async for _event in loop.resume_permission_boundary(checkpoint):
            pass

    assert tool.executed is False
