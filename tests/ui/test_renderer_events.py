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
            yield ToolUseEndEvent(tool_use_id="t1", input={"path": "foo.py"})
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
