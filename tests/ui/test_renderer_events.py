import asyncio
from unittest.mock import MagicMock

import pytest

from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TaskNotificationEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.ui.renderer import Renderer


@pytest.fixture
def renderer():
    console = MagicMock()
    registry = ToolRegistry()
    return Renderer(console, registry, status_callback=lambda: "test")


@pytest.mark.asyncio
class TestRendererStreamEvents:
    async def test_text_delta(self, renderer):
        async def events():
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello ")
            yield TextDeltaEvent(text="world!")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        await renderer.run_streaming_output(events(), permission_handler=None)

    async def test_tool_flow(self, renderer):
        async def events():
            yield MessageStartEvent(message_id="m1")
            yield ToolUseStartEvent(tool_use_id="t1", name="read_file")
            yield ToolInputDeltaEvent(tool_use_id="t1", partial_json='{"path": "foo.py"}')
            yield ToolUseEndEvent(tool_use_id="t1", name="read_file", input={"path": "foo.py"})
            yield ToolResultEvent(tool_use_id="t1", tool_name="read_file", result="content")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        await renderer.run_streaming_output(events(), permission_handler=None)

    async def test_tombstone(self, renderer):
        async def events():
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="partial")
            yield TombstoneEvent(message_id="m1")
            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="real")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        await renderer.run_streaming_output(events(), permission_handler=None)

    async def test_task_notification(self, renderer):
        async def events():
            yield TaskNotificationEvent(task_id="t1", description="Explore", status="completed", result="Found 5 files")
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        await renderer.run_streaming_output(events(), permission_handler=None)

    async def test_thinking_delta(self, renderer):
        async def events():
            yield MessageStartEvent(message_id="m1")
            yield ThinkingDeltaEvent(text="Let me think...")
            yield TextDeltaEvent(text="Answer")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        await renderer.run_streaming_output(events(), permission_handler=None)


def _make_renderer_for_thinking_test():
    from io import StringIO

    from rich.console import Console

    from iac_code.tools.base import ToolRegistry
    from iac_code.ui.renderer import Renderer

    console = Console(file=StringIO(), force_terminal=True, width=120, color_system=None, _environ={})
    registry = ToolRegistry()
    return Renderer(console, registry)


class TestThinkingRendering:
    @pytest.mark.asyncio
    async def test_thinking_then_text_leaves_summary_segment(self):
        from iac_code.types.stream_events import (
            MessageEndEvent,
            MessageStartEvent,
            TextDeltaEvent,
            ThinkingDeltaEvent,
            Usage,
        )

        async def events():
            yield MessageStartEvent(message_id="m1")
            yield ThinkingDeltaEvent(text="weighing options...")
            yield ThinkingDeltaEvent(text="\nreached conclusion.")
            yield TextDeltaEvent(text="Hello")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        renderer = _make_renderer_for_thinking_test()

        async def _no_perm(_):
            return False

        await renderer.run_streaming_output(events(), permission_handler=_no_perm)

        history = renderer.message_history
        last = history[-1]
        kinds = [s.kind for s in last.segments]
        assert "thinking_summary" in kinds
        idx = kinds.index("thinking_summary")
        assert "text" in kinds[idx + 1 :]
        summary = last.segments[idx]
        assert summary.elapsed_seconds >= 0.0

    @pytest.mark.asyncio
    async def test_no_thinking_no_summary(self):
        from iac_code.types.stream_events import (
            MessageEndEvent,
            MessageStartEvent,
            TextDeltaEvent,
            Usage,
        )

        async def events():
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hi")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        renderer = _make_renderer_for_thinking_test()

        async def _no_perm(_):
            return False

        await renderer.run_streaming_output(events(), permission_handler=_no_perm)
        kinds = [s.kind for turn in renderer.message_history for s in turn.segments]
        assert "thinking_summary" not in kinds

    @pytest.mark.asyncio
    async def test_empty_thinking_buffer_no_summary(self):
        from iac_code.types.stream_events import (
            MessageEndEvent,
            MessageStartEvent,
            TextDeltaEvent,
            ThinkingDeltaEvent,
            Usage,
        )

        async def events():
            yield MessageStartEvent(message_id="m1")
            yield ThinkingDeltaEvent(text="   \n\t")
            yield TextDeltaEvent(text="Hi")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        renderer = _make_renderer_for_thinking_test()

        async def _no_perm(_):
            return False

        await renderer.run_streaming_output(events(), permission_handler=_no_perm)
        kinds = [s.kind for turn in renderer.message_history for s in turn.segments]
        assert "thinking_summary" not in kinds

    @pytest.mark.asyncio
    async def test_thinking_only_leaves_summary(self):
        from iac_code.types.stream_events import (
            MessageEndEvent,
            MessageStartEvent,
            ThinkingDeltaEvent,
            Usage,
        )

        async def events():
            yield MessageStartEvent(message_id="m1")
            yield ThinkingDeltaEvent(text="all alone in my head")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        renderer = _make_renderer_for_thinking_test()

        async def _no_perm(_):
            return False

        await renderer.run_streaming_output(events(), permission_handler=_no_perm)
        kinds = [s.kind for turn in renderer.message_history for s in turn.segments]
        assert "thinking_summary" in kinds


class TestRenderDiagramFallback:
    """Regression: termaid render failures must log a warning (U-C3)."""

    def test_render_diagram_logs_warning_on_termaid_failure(self, renderer, monkeypatch):
        # Force termaid.render_rich to raise a non-ImportError exception.
        # Use a fake module so the test runs even when the optional `termaid`
        # extra is not installed (it is gated on python_version >= '3.11').
        import sys
        import types

        from loguru import logger

        from iac_code.types.stream_events import DiagramEvent

        def broken_render(_):
            raise ValueError("invalid mermaid syntax")

        fake_termaid = types.SimpleNamespace(render_rich=broken_render)
        monkeypatch.setitem(sys.modules, "termaid", fake_termaid)
        event = DiagramEvent(
            candidate_name="方案1",
            template_content="ROSTemplate...",
            mermaid_source="graph TD\n A-->B",
        )
        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
        try:
            result = renderer._render_diagram(event)
        finally:
            logger.remove(handler_id)
        # Still returns a Group with the fallback code block
        assert result is not None
        # Warning is logged with enough info to debug
        assert any("termaid render failed" in r for r in records), (
            f"Expected 'termaid render failed' warning, got: {records}"
        )

    def test_render_diagram_silent_when_termaid_missing(self, renderer, monkeypatch):
        # Make `from termaid import render_rich` fail with ImportError
        import sys

        from loguru import logger

        from iac_code.types.stream_events import DiagramEvent

        monkeypatch.setitem(sys.modules, "termaid", None)
        event = DiagramEvent(
            candidate_name="方案1",
            template_content="ROSTemplate...",
            mermaid_source="graph TD",
        )
        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
        try:
            result = renderer._render_diagram(event)
        finally:
            logger.remove(handler_id)
        # Falls back silently — no warning when the module is just missing
        assert result is not None
        assert not any("termaid render failed" in r for r in records)


class TestCandidateDetailWithToolUseId:
    """U-I14: CandidateDetailEvent must accept tool_use_id (so multiple
    tool invocations don't collide on accumulator key)."""

    def test_dataclass_accepts_tool_use_id(self):
        from iac_code.types.stream_events import CandidateDetailEvent

        e = CandidateDetailEvent(
            tool_use_id="tool_use_aaa",
            candidate_name="方案A",
            summary="cheap",
            cost_items=[{"item": "ECS", "monthly": "100"}],
            total_monthly_cost="100",
        )
        assert e.tool_use_id == "tool_use_aaa"

    def test_two_events_different_tool_use_ids_distinct(self):
        from iac_code.types.stream_events import CandidateDetailEvent

        e1 = CandidateDetailEvent(
            tool_use_id="tu_a",
            candidate_name="方案A",
            summary="cheap",
            cost_items=[],
            total_monthly_cost="100",
        )
        e2 = CandidateDetailEvent(
            tool_use_id="tu_b",
            candidate_name="方案A",
            summary="cheap v2",
            cost_items=[],
            total_monthly_cost="150",
        )
        assert e1.tool_use_id != e2.tool_use_id
        assert e1.summary != e2.summary


async def _tool_result_events(*events):
    for event in events:
        yield event


async def _allow_permission(_event):
    return True


def _make_renderer_for_tool_result_fallback_test() -> Renderer:
    from io import StringIO

    from rich.console import Console

    registry = MagicMock()
    registry.get.return_value = None
    console = Console(file=StringIO(), force_terminal=True, width=120, color_system=None, _environ={})
    return Renderer(console, registry)


async def _idle_key_listener(*_args, **_kwargs):
    await asyncio.Event().wait()


class TestRendererToolResultFallback:
    @pytest.mark.asyncio
    async def test_does_not_fallback_stale_tool_result_id_to_same_named_tool(self, monkeypatch):
        monkeypatch.setattr(Renderer, "_key_listener", _idle_key_listener)
        renderer = _make_renderer_for_tool_result_fallback_test()

        await renderer.run_streaming_output(
            _tool_result_events(
                ToolUseStartEvent(tool_use_id="tool-a", name="read_file"),
                ToolUseStartEvent(tool_use_id="tool-b", name="read_file"),
                ToolResultEvent(tool_use_id="stale-id", tool_name="read_file", result="wrong"),
            ),
            _allow_permission,
        )

        tools = [segment.tool for segment in renderer.message_history[-1].segments if segment.kind == "tool"]
        assert len(tools) == 2
        assert [tool.done for tool in tools if tool is not None] == [False, False]
        assert [tool.result for tool in tools if tool is not None] == [None, None]

    @pytest.mark.asyncio
    async def test_does_not_fallback_ambiguous_empty_tool_result_id(self, monkeypatch):
        monkeypatch.setattr(Renderer, "_key_listener", _idle_key_listener)
        renderer = _make_renderer_for_tool_result_fallback_test()

        await renderer.run_streaming_output(
            _tool_result_events(
                ToolUseStartEvent(tool_use_id="tool-a", name="read_file"),
                ToolUseStartEvent(tool_use_id="tool-b", name="read_file"),
                ToolResultEvent(tool_use_id="", tool_name="read_file", result="ambiguous"),
            ),
            _allow_permission,
        )

        tools = [segment.tool for segment in renderer.message_history[-1].segments if segment.kind == "tool"]
        assert len(tools) == 2
        assert [tool.done for tool in tools if tool is not None] == [False, False]
        assert [tool.result for tool in tools if tool is not None] == [None, None]
