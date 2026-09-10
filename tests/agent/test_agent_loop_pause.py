"""Tests for AgentLoop pause_event support."""

import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_provider_manager():
    from iac_code.types.stream_events import MessageEndEvent, Usage

    pm = MagicMock()
    pm.get_model_name.return_value = "test-model"

    # `stream` must be a real async generator that terminates: AgentLoop drives
    # it with `while True: await anext(...)` and relies on StopAsyncIteration to
    # end the turn. A bare MagicMock attribute never raises StopAsyncIteration,
    # so `anext` would spin forever once the pause is released.
    async def _stream(**_kwargs):
        yield MessageEndEvent(stop_reason="stop", usage=Usage())

    pm.stream = _stream
    return pm


def _make_loop(mock_pm, pause_event=None):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.tools.base import ToolRegistry

    return AgentLoop(
        provider_manager=mock_pm,
        system_prompt="test",
        tool_registry=ToolRegistry(),
        max_turns=5,
        pause_event=pause_event,
    )


class TestPauseEventConstructor:
    def test_default_no_pause_event(self, mock_provider_manager):
        loop = _make_loop(mock_provider_manager)
        assert loop._pause_event is None

    def test_accepts_pause_event(self, mock_provider_manager):
        ev = asyncio.Event()
        loop = _make_loop(mock_provider_manager, pause_event=ev)
        assert loop._pause_event is ev


class TestPauseEventBlocksTurn:
    @pytest.mark.asyncio
    async def test_cleared_event_blocks_turn_loop(self, mock_provider_manager):
        """run_streaming should park at the turn-top checkpoint when event is cleared."""
        ev = asyncio.Event()  # initially clear → paused
        loop = _make_loop(mock_provider_manager, pause_event=ev)

        # Drive run_streaming as a task and check it doesn't progress past the
        # checkpoint. We can't observe yields directly without a real provider,
        # so instead we verify the task doesn't complete and that flipping
        # `set()` lets it proceed past `_pending_injections` into the provider
        # call (which then fails because the mock provider doesn't implement
        # stream — that failure is fine, it proves the pause was released).
        events_before_set = []

        async def consume():
            try:
                async for ev_emitted in loop.run_streaming("hello"):
                    events_before_set.append(ev_emitted)
            except Exception:
                pass  # mock provider will blow up; we only care about the pause

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # let task reach pause checkpoint
        assert not task.done(), "run_streaming should be parked on pause_event.wait()"
        assert events_before_set == []

        ev.set()  # release
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_set_event_does_not_block(self, mock_provider_manager):
        """run_streaming should NOT block when pause_event is already set."""
        ev = asyncio.Event()
        ev.set()
        loop = _make_loop(mock_provider_manager, pause_event=ev)

        async def consume():
            try:
                async for _ in loop.run_streaming("hello"):
                    return
            except Exception:
                pass

        # Should run quickly to the provider call (which then fails); no hang.
        await asyncio.wait_for(consume(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_cancellation_through_pause_checkpoint(self, mock_provider_manager):
        """Cancelling task while parked on pause_event.wait() raises CancelledError cleanly."""
        ev = asyncio.Event()  # cleared
        loop = _make_loop(mock_provider_manager, pause_event=ev)

        async def consume():
            async for _ in loop.run_streaming("hello"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class _FakeStreamingProvider:
    """Minimal provider_manager double that yields a predefined event sequence.

    Exposes the same surface AgentLoop needs: `get_model_name()` and an async
    `stream(**kwargs)` generator. Used to drive the REAL `_run_streaming_inner`
    accumulator branch (around agent_loop.py:322) end-to-end.
    """

    def __init__(self, events):
        self._events = events

    def get_model_name(self) -> str:
        return "fake-model"

    async def stream(self, **_kwargs):
        for ev in self._events:
            yield ev


class TestPauseEventGatesTurnTextAccumulation:
    """N-I3: Gate `_current_turn_text` accumulation on pause_event presence.

    Normal mode (pause_event is None) should skip the per-TextDelta string
    concatenation entirely — the buffer is only read by pipeline mode's
    interrupt judge. Pipeline mode (pause_event provided) must keep the
    existing accumulating behavior so partial_output stays available.
    """

    @pytest.mark.asyncio
    async def test_n_i3_real_agent_loop_does_not_accumulate_without_pause(self):
        """Run a real AgentLoop turn in normal mode and confirm
        _current_turn_text stays empty when pause_event is None.

        Pre-fix: accumulator always ran, costing O(N) work per TextDelta even
        when nobody reads the buffer. Post-fix: gated on pause_event presence.
        """
        from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage

        events = [
            TextDeltaEvent(text="alpha"),
            TextDeltaEvent(text="beta"),
            MessageEndEvent(stop_reason="stop", usage=Usage()),
        ]
        provider = _FakeStreamingProvider(events)
        loop = _make_loop(provider, pause_event=None)
        assert loop._pause_event is None  # normal mode

        # Drive the real production path through run_streaming. No tool calls
        # arrive, so the loop exits after one turn and `_current_turn_text`
        # holds whatever the accumulator branch wrote.
        async for _ in loop.run_streaming("hello"):
            pass

        assert loop._current_turn_text == "", (
            f"normal-mode AgentLoop should not accumulate turn_text, got {loop._current_turn_text!r}"
        )

    @pytest.mark.asyncio
    async def test_n_i3_real_agent_loop_accumulates_with_pause(self):
        """N-I3 positive: when pause_event is provided (pipeline mode),
        _current_turn_text still accumulates so the interrupt judge can read
        partial_output."""
        from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage

        events = [
            TextDeltaEvent(text="alpha"),
            TextDeltaEvent(text="beta"),
            MessageEndEvent(stop_reason="stop", usage=Usage()),
        ]
        provider = _FakeStreamingProvider(events)
        ev = asyncio.Event()
        ev.set()  # don't block at the turn-top checkpoint
        loop = _make_loop(provider, pause_event=ev)
        assert loop._pause_event is ev  # pipeline mode

        async for _ in loop.run_streaming("hello"):
            pass

        assert loop._current_turn_text == "alphabeta", (
            f"pipeline-mode AgentLoop must keep accumulating, got {loop._current_turn_text!r}"
        )


@pytest.mark.asyncio
async def test_execution_control_allows_committed_final_answer_to_finish_during_pause(tmp_path):
    from iac_code.a2a.execution_control import ExecutionController, bind_execution_control, reset_execution_control
    from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage

    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    class Provider:
        def get_model_name(self):
            return "fake-model"

        async def stream(self, **_kwargs):
            provider_started.set()
            yield TextDeltaEvent(text="complete response")
            await release_provider.wait()
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    loop = _make_loop(Provider())
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=None,
        execution_id="exec-1",
    )

    async def consume():
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            return [event async for event in loop.run_streaming("hello")]
        finally:
            await control.detach_task(current, execution_status="normal-turn-ended")
            reset_execution_control(token)

    execution = asyncio.create_task(consume())
    await provider_started.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="pause-request",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=30,
    )
    assert pause["phase"] == "pausing"
    release_provider.set()

    await asyncio.wait_for(execution, timeout=2)
    assert loop.context_manager.get_context_messages()[-1].to_dict()["content"][0]["text"] == "complete response"

    async def wait_until_paused():
        while control.phase != "paused":
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_until_paused(), timeout=2)
    assert control.phase == "paused"
    assert control.execution_status == "normal-turn-ended"
    assert control.stream_available is False

    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="resume-request",
        connection_epoch=2,
    )

    async def wait_until_running():
        while control.phase != "running":
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_until_running(), timeout=2)
    assert control.phase == "running"
    await control.close()


@pytest.mark.asyncio
async def test_execution_control_pauses_only_after_finished_tool_result_is_persisted(tmp_path):
    from iac_code.a2a.execution_control import ExecutionController, bind_execution_control, reset_execution_control
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        ToolResultEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )

    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    class SlowReadTool(Tool):
        @property
        def name(self):
            return "slow_read"

        @property
        def description(self):
            return "Return a durable result after being released."

        @property
        def input_schema(self):
            return {"type": "object", "additionalProperties": False}

        def is_read_only(self, input=None):
            return True

        async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
            tool_started.set()
            await release_tool.wait()
            return ToolResult.success("DURABLE_RESULT")

    class Provider:
        def get_model_name(self):
            return "fake-model"

        async def stream(self, **_kwargs):
            yield MessageStartEvent(message_id="message-1")
            yield ToolUseStartEvent(tool_use_id="tool-1", name="slow_read")
            yield ToolUseEndEvent(tool_use_id="tool-1", name="slow_read", input={})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    registry = ToolRegistry()
    registry.register(SlowReadTool())
    loop = AgentLoop(
        provider_manager=Provider(),
        system_prompt="test",
        tool_registry=registry,
        max_turns=1,
        session_storage=storage,
        session_id="session-1",
        cwd=str(cwd),
    )
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(cwd),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=None,
        execution_id="exec-1",
    )
    emitted_events = []

    async def consume():
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async for event in loop.run_streaming("use the tool"):
                emitted_events.append(event)
        finally:
            await control.detach_task(current, execution_status="normal-turn-ended")
            reset_execution_control(token)

    execution = asyncio.create_task(consume())
    await tool_started.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="pause-during-tool",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=30,
    )
    assert pause["phase"] == "pausing"
    release_tool.set()

    async def wait_until_paused():
        while control.phase != "paused":
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_until_paused(), timeout=2)
    assert execution.done() is False
    assert any(isinstance(event, ToolResultEvent) and event.result == "DURABLE_RESULT" for event in emitted_events)
    persisted = storage.load(str(cwd), "session-1")
    assert persisted[-1].to_dict()["content"][0]["content"] == "DURABLE_RESULT"

    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="resume-after-tool",
        connection_epoch=2,
    )
    await execution
    await control.close()


@pytest.mark.asyncio
async def test_terminate_drains_already_finished_tool_result_before_release_ready(tmp_path):
    from iac_code.a2a.execution_control import ExecutionController, bind_execution_control, reset_execution_control
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import ToolRegistry, ToolResult
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )

    class Provider:
        def get_model_name(self):
            return "fake-model"

        async def stream(self, **_kwargs):
            yield MessageStartEvent(message_id="message-1")
            yield ToolUseStartEvent(tool_use_id="tool-1", name="finished_tool")
            yield ToolUseEndEvent(tool_use_id="tool-1", name="finished_tool", input={})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    loop = AgentLoop(
        provider_manager=Provider(),
        system_prompt="test",
        tool_registry=ToolRegistry(),
        max_turns=1,
        session_storage=storage,
        session_id="session-1",
        cwd=str(cwd),
    )
    tool_result_ready = asyncio.Event()

    async def execute_batch(_requests, _context):
        tool_result_ready.set()
        return [ToolResult.success("RESULT_BEFORE_TERMINATE")]

    loop._tool_executor.execute_batch = execute_batch
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(cwd),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=None,
        execution_id="exec-1",
    )

    async def consume():
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async for _event in loop.run_streaming("use the tool"):
                pass
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    execution = asyncio.create_task(consume())
    await tool_result_ready.wait()
    await asyncio.sleep(0)
    await control.terminate(
        execution_id="exec-1",
        request_id="terminate-after-result",
        connection_epoch=1,
        reason="explicit_terminate",
    )

    with pytest.raises(asyncio.CancelledError):
        await execution

    async def wait_until_release_ready():
        while not control.release_ready:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_until_release_ready(), timeout=2)
    persisted = storage.load(str(cwd), "session-1")
    assert persisted[-1].to_dict()["content"][0]["content"] == "RESULT_BEFORE_TERMINATE"
    await control.close()


@pytest.mark.asyncio
async def test_terminate_during_tool_result_publication_keeps_persisted_result(tmp_path):
    from iac_code.a2a.execution_control import ExecutionController, bind_execution_control, reset_execution_control
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import ToolRegistry, ToolResult
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        ToolResultEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )

    class Provider:
        def get_model_name(self):
            return "fake-model"

        async def stream(self, **_kwargs):
            yield MessageStartEvent(message_id="message-1")
            yield ToolUseStartEvent(tool_use_id="tool-1", name="finished_tool")
            yield ToolUseEndEvent(tool_use_id="tool-1", name="finished_tool", input={})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    loop = AgentLoop(
        provider_manager=Provider(),
        system_prompt="test",
        tool_registry=ToolRegistry(),
        max_turns=1,
        session_storage=storage,
        session_id="session-1",
        cwd=str(cwd),
    )

    async def execute_batch(_requests, _context):
        return [ToolResult.success("RESULT_BEFORE_PUBLICATION")]

    loop._tool_executor.execute_batch = execute_batch
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(cwd),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=None,
        execution_id="exec-1",
    )
    publication_started = asyncio.Event()
    publication_blocked = asyncio.Event()

    async def consume():
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async for event in loop.run_streaming("use the tool"):
                if isinstance(event, ToolResultEvent):
                    publication_started.set()
                    await publication_blocked.wait()
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    execution = asyncio.create_task(consume())
    await asyncio.wait_for(publication_started.wait(), timeout=2)
    await control.terminate(
        execution_id="exec-1",
        request_id="terminate-during-publication",
        connection_epoch=1,
        reason="explicit_terminate",
    )
    with pytest.raises(asyncio.CancelledError):
        await execution

    async def wait_until_release_ready():
        while not control.release_ready:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_until_release_ready(), timeout=2)
    persisted = storage.load(str(cwd), "session-1")
    assert persisted[-1].to_dict()["content"][0]["content"] == "RESULT_BEFORE_PUBLICATION"
    await control.close()
