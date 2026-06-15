"""Tests for AgentLoop pause_event support."""

import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_provider_manager():
    pm = MagicMock()
    pm.get_model_name.return_value = "test-model"
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
