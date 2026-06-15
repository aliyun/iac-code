"""U-I7: candidate-selection tabs must close outer event_stream BEFORE
the recursive `_render_pipeline_stream` call on the resumed stream.

NOTE: The task description references `_render_parallel_tabs`, but the
actual recursive `_render_pipeline_stream` call lives in
`_render_candidate_selection_tabs` (repl.py). `_render_parallel_tabs`
returns to its caller and does not recurse. The semantics described in
the task (USER_INPUT_REQUIRED → candidate selection → resume → recurse)
match `_render_candidate_selection_tabs`, so the fix and this regression
test target that method.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_outer_event_stream_closed_before_recursive_render(monkeypatch):
    """After USER_INPUT_REQUIRED → candidate selection → resume(),
    the OUTER event_stream must have aclose() awaited BEFORE the
    recursive _render_pipeline_stream call on the resumed stream."""
    from io import StringIO

    from rich.console import Console

    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import DiagramEvent
    from iac_code.ui.core.key_event import KeyEvent
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl.store = MagicMock()
    repl._pipeline_waiting_input = False
    repl._render_interrupt_feedback_inline = MagicMock()
    repl._render_pipeline_event = MagicMock()

    aclose_order: list[str] = []

    async def fake_render_pipeline_stream(_stream):
        aclose_order.append("recursive_called")

    repl._render_pipeline_stream = fake_render_pipeline_stream

    pipeline = MagicMock()
    new_stream = MagicMock(name="new_stream_after_resume")
    pipeline.resume = MagicMock(return_value=new_stream)
    pipeline.state_machine = MagicMock(is_complete=False)
    pipeline.pause_agent_loops = MagicMock()
    pipeline.resume_agent_loops = MagicMock()
    repl._pipeline = pipeline

    # Patch Live to a no-op so we don't try to render to a real terminal.
    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr("iac_code.ui.repl.Live", FakeLive)

    # Patch RawInputCapture to deliver an "enter" key once the REPL is
    # in selection mode. We poll `_pipeline_waiting_input` (the flag set
    # by the USER_INPUT_REQUIRED handler) to avoid races between
    # key_reader starting and the main task entering selection mode.
    class FakeCapture:
        def __init__(self, *args, **kwargs):
            self._fired = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            if self._fired:
                if timeout:
                    time.sleep(min(timeout, 0.05))
                return None
            deadline = time.time() + (timeout if timeout else 1.0)
            while time.time() < deadline:
                if repl._pipeline_waiting_input:
                    self._fired = True
                    return KeyEvent(key="enter", char="")
                time.sleep(0.01)
            return None

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    # Outer stream — yield a DiagramEvent (to populate at least one tab so
    # confirm_selection returns a non-empty name) and then USER_INPUT_REQUIRED.
    # Track aclose vs recursive ordering.
    class TrackedStream:
        def __init__(self):
            self._iter = self._gen()

        async def _gen(self):
            yield DiagramEvent(
                candidate_name="c1",
                template_content="ROSTemplateFormatVersion: '2015-09-01'",
                mermaid_source="graph TD; A-->B",
            )
            yield PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id=None,
                timestamp=time.time(),
                data={"candidates": [{"name": "c1"}]},
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self._iter.__anext__()

        async def aclose(self):
            aclose_order.append("outer_aclose")
            try:
                await self._iter.aclose()
            except Exception:
                pass

    outer = TrackedStream()

    # Run with a timeout so a test bug never hangs CI.
    await asyncio.wait_for(
        repl._render_candidate_selection_tabs(outer),
        timeout=5.0,
    )

    # Verify ordering: outer aclose must come BEFORE the recursive
    # _render_pipeline_stream call on the resumed stream.
    assert "outer_aclose" in aclose_order, f"outer event_stream.aclose() was never awaited; got: {aclose_order!r}"
    assert "recursive_called" in aclose_order, f"recursive _render_pipeline_stream never fired; got: {aclose_order!r}"
    outer_idx = aclose_order.index("outer_aclose")
    recur_idx = aclose_order.index("recursive_called")
    assert outer_idx < recur_idx, f"outer_aclose must come BEFORE recursive call, got order: {aclose_order!r}"


@pytest.mark.asyncio
async def test_user_input_required_escape_empty_input_returns_to_candidate_selection(monkeypatch):
    """ESC in candidate selection opens supplement input; empty ESC cancels back to selection."""
    from io import StringIO

    from rich.console import Console

    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import DiagramEvent
    from iac_code.ui.core.key_event import KeyEvent
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl.store = MagicMock()
    repl._pipeline_waiting_input = False
    repl._handle_mid_pipeline_message = AsyncMock(return_value=(False, ""))
    repl._render_pipeline_stream = AsyncMock()

    pipeline = MagicMock()
    events: list[str] = []
    resumed_payloads: list[dict] = []
    resumed_stream = MagicMock(name="resumed_stream_after_selection")

    def resume_pipeline(selection: str):
        import json

        resumed_payloads.append(json.loads(selection))
        events.append("pipeline_resume")
        return resumed_stream

    pipeline.resume = MagicMock(side_effect=resume_pipeline)
    pipeline.pause_agent_loops = MagicMock(side_effect=lambda: events.append("pause"))
    pipeline.resume_agent_loops = MagicMock(side_effect=lambda: events.append("resume"))
    repl._pipeline = pipeline

    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr("iac_code.ui.repl.Live", FakeLive)

    key_events = deque(
        [
            ("key:escape:selection", KeyEvent(key="escape", char="\x1b")),
            ("key:escape:input_cancel", KeyEvent(key="escape", char="\x1b")),
            ("key:enter:selection", KeyEvent(key="enter", char="")),
        ]
    )

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            deadline = time.time() + (timeout if timeout else 1.0)
            while time.time() < deadline:
                if repl._pipeline_waiting_input and key_events:
                    label, key_event = key_events.popleft()
                    events.append(label)
                    return key_event
                time.sleep(0.01)
            return None

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    async def stream():
        yield DiagramEvent(
            candidate_name="c1",
            template_content="ROSTemplateFormatVersion: '2015-09-01'",
            mermaid_source="graph TD; A-->B",
        )
        yield PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id=None,
            timestamp=time.time(),
            data={"candidates": [{"name": "c1"}]},
        )

    await asyncio.wait_for(
        repl._render_candidate_selection_tabs(stream()),
        timeout=5.0,
    )

    repl._handle_mid_pipeline_message.assert_not_awaited()
    pipeline.pause_agent_loops.assert_called()
    pipeline.resume_agent_loops.assert_called()
    pipeline.resume.assert_called_once()
    assert resumed_payloads == [{"selected_candidate_name": "c1", "selected_candidate_index": None}]

    assert events.index("key:escape:selection") < events.index("pause")
    assert events.index("pause") < events.index("key:escape:input_cancel")
    assert events.index("key:escape:input_cancel") < events.index("resume")
    assert events.index("resume") < events.index("key:enter:selection")
    assert events.index("key:enter:selection") < events.index("pipeline_resume")


@pytest.mark.asyncio
async def test_user_input_required_hard_interrupt_clears_waiting_flag(monkeypatch):
    from io import StringIO

    from rich.console import Console

    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import DiagramEvent
    from iac_code.ui.core.key_event import KeyEvent
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl.store = MagicMock()
    repl._pipeline_waiting_input = False
    repl._handle_mid_pipeline_message = AsyncMock(return_value=(True, "已切换方案"))
    repl._render_interrupt_feedback_inline = MagicMock()

    pipeline = MagicMock()
    pipeline.resume = MagicMock()
    pipeline.pause_agent_loops = MagicMock()
    pipeline.resume_agent_loops = MagicMock()
    repl._pipeline = pipeline

    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr("iac_code.ui.repl.Live", FakeLive)

    key_events = deque(
        [
            KeyEvent(key="escape", char="\x1b"),
            KeyEvent(key="x", char="换"),
            KeyEvent(key="enter", char=""),
        ]
    )

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            deadline = time.time() + (timeout if timeout else 1.0)
            while time.time() < deadline:
                if repl._pipeline_waiting_input and key_events:
                    return key_events.popleft()
                time.sleep(0.01)
            return None

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    async def stream():
        yield DiagramEvent(
            candidate_name="c1",
            template_content="ROSTemplateFormatVersion: '2015-09-01'",
            mermaid_source="graph TD; A-->B",
        )
        yield PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id=None,
            timestamp=time.time(),
            data={"candidates": [{"name": "c1"}]},
        )

    result = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream()), timeout=5.0)

    assert result is True
    assert repl._pipeline_waiting_input is False
    pipeline.resume.assert_not_called()
    pipeline.pause_agent_loops.assert_called()
    pipeline.resume_agent_loops.assert_called()


@pytest.mark.asyncio
async def test_user_input_required_ctrl_c_cancels_candidate_selection(monkeypatch):
    """Ctrl+C inside the candidate-selection UI should abort the pipeline,
    not wait until a later step."""
    from io import StringIO

    from rich.console import Console

    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import DiagramEvent
    from iac_code.ui.core.key_event import KeyEvent
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl.store = MagicMock()
    repl._pipeline_waiting_input = False
    repl._handle_mid_pipeline_message = AsyncMock()
    repl._render_pipeline_stream = AsyncMock()

    pipeline = MagicMock()
    pipeline.resume = MagicMock()
    pipeline.pause_agent_loops = MagicMock()
    pipeline.resume_agent_loops = MagicMock()
    repl._pipeline = pipeline

    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr("iac_code.ui.repl.Live", FakeLive)

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            deadline = time.time() + (timeout if timeout else 1.0)
            while time.time() < deadline:
                if repl._pipeline_waiting_input and not self._sent:
                    self._sent = True
                    return KeyEvent(key="c", char="\x03", ctrl=True)
                time.sleep(0.01)
            return None

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    async def stream():
        yield DiagramEvent(
            candidate_name="c1",
            template_content="ROSTemplateFormatVersion: '2015-09-01'",
            mermaid_source="graph TD; A-->B",
        )
        yield PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id=None,
            timestamp=time.time(),
            data={"candidates": [{"name": "c1"}]},
        )
        await asyncio.Future()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(repl._render_candidate_selection_tabs(stream()), timeout=5.0)

    pipeline.resume.assert_not_called()


@pytest.mark.asyncio
async def test_user_input_required_sigint_clears_waiting_flag(monkeypatch):
    """SIGINT-style task cancellation while candidate selection is waiting
    must still let the outer pipeline cleanup mark the run as aborted."""
    from io import StringIO

    from rich.console import Console

    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import DiagramEvent
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl.store = MagicMock()
    repl._pipeline_waiting_input = False

    pipeline = MagicMock()
    pipeline.resume = MagicMock()
    pipeline.pause_agent_loops = MagicMock()
    pipeline.resume_agent_loops = MagicMock()
    repl._pipeline = pipeline

    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr("iac_code.ui.repl.Live", FakeLive)

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            if timeout:
                time.sleep(min(timeout, 0.05))
            return None

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    async def stream():
        yield DiagramEvent(
            candidate_name="c1",
            template_content="ROSTemplateFormatVersion: '2015-09-01'",
            mermaid_source="graph TD; A-->B",
        )
        yield PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id=None,
            timestamp=time.time(),
            data={"candidates": [{"name": "c1"}]},
        )
        await asyncio.Future()

    task = asyncio.create_task(repl._render_candidate_selection_tabs(stream()))
    while not repl._pipeline_waiting_input:
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert repl._pipeline_waiting_input is False
    pipeline.resume.assert_not_called()
