import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor


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

    async def test_error_no_block(self):
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
        assert "boom" in results[0].content
        assert results[1].content == "read result"

    async def test_timeout(self):
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
        assert "timed out" in results[0].content

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

    async def test_invalid_input_returns_error(self):
        tool = FakeStrictTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        calls = [ToolCallRequest(id="v2", name="strict", input={})]
        results = await executor.execute_batch(calls, ToolContext())
        assert results[0].is_error is True
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
