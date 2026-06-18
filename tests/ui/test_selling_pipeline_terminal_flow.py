from __future__ import annotations

import asyncio
import json
import time
from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent, ToolInputDeltaEvent, ToolUseStartEvent
from iac_code.ui.repl import InlineREPL


class ClosableAsyncStream:
    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __aiter__(self):
        self._iter = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self):
        self.closed = True


def _make_repl_for_selection(monkeypatch, key_sequence: list[str] | None = None):
    from iac_code.ui.core.key_event import KeyEvent

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, height=30, force_terminal=True)
    repl.store = MagicMock()
    repl._pipeline_waiting_input = False
    repl._render_interrupt_feedback_inline = MagicMock()

    resumed_payloads: list[str] = []

    async def fake_render_pipeline_stream(_stream):
        return None

    repl._render_pipeline_stream = fake_render_pipeline_stream

    pipeline = MagicMock()
    pipeline.resume.side_effect = lambda payload: resumed_payloads.append(payload) or ClosableAsyncStream([])
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
    keys = list(key_sequence or ["enter"])

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            self._keys = keys

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            if not self._keys:
                return None
            if repl._pipeline_waiting_input:
                next_key = self._keys.pop(0)
                return KeyEvent(key=next_key, char="")
            time.sleep(0.01)
            return None

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    return repl, resumed_payloads


@pytest.mark.asyncio
async def test_candidate_selection_resumes_with_structured_payload(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)

    stream = ClosableAsyncStream(
        [
            DiagramEvent("Plan A", "Resources: {}", "graph TD", candidate_index=0),
            CandidateDetailEvent("tu_a", "Plan A", "summary", [], "¥0/月", candidate_index=0),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={"prompt": "请选择", "options": [{"name": "Plan A", "summary": "summary"}]},
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Plan A"
    assert stream.closed is True
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {"selected_candidate_name": "Plan A", "selected_candidate_index": 0}


@pytest.mark.asyncio
async def test_candidate_selection_seeds_options_when_display_tools_are_missing(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)

    stream = ClosableAsyncStream(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={
                    "prompt": "请选择",
                    "options": [
                        {"name": "Plan A", "summary": "missing display", "candidate_index": 0},
                        {"name": "Plan B", "summary": "missing display", "candidate_index": 1},
                    ],
                },
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Plan A"
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {"selected_candidate_name": "Plan A", "selected_candidate_index": 0}


@pytest.mark.asyncio
async def test_streaming_candidate_detail_preserves_indexed_identity(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)

    stream = ClosableAsyncStream(
        [
            ToolUseStartEvent("tu_same", "show_candidate_detail"),
            ToolInputDeltaEvent(
                "tu_same",
                '{"candidate_name":"Same","candidate_index":0,"summary":"partial summary',
            ),
            CandidateDetailEvent("tu_same", "Same", "full summary", [], "¥0/月", candidate_index=0),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={"prompt": "请选择", "options": [{"name": "Same", "summary": "full", "candidate_index": 0}]},
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Same"
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {"selected_candidate_name": "Same", "selected_candidate_index": 0}


@pytest.mark.asyncio
async def test_candidate_selection_can_choose_second_duplicate_by_index(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch, key_sequence=["right", "enter"])

    stream = ClosableAsyncStream(
        [
            DiagramEvent("Same", "Resources: {}", "graph TD\nA-->B", candidate_index=0),
            CandidateDetailEvent("tu_a", "Same", "first", [], "¥0/月", candidate_index=0),
            DiagramEvent("Same", "Resources: {}", "graph TD\nC-->D", candidate_index=1),
            CandidateDetailEvent("tu_b", "Same", "second", [], "¥0/月", candidate_index=1),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={
                    "prompt": "请选择",
                    "options": [
                        {"name": "Same", "summary": "first", "candidate_index": 0},
                        {"name": "Same", "summary": "second", "candidate_index": 1},
                    ],
                },
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Same"
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {"selected_candidate_name": "Same", "selected_candidate_index": 1}
    assert "Same #2" in repl.renderer.console.file.getvalue()
