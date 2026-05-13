from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

from rich.console import Console
from rich.text import Text

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.renderer import RenderedTurn, _Segment, _ToolCallRecord
from iac_code.ui.transcript_view import TranscriptView


def make_console(width: int = 80, height: int = 8) -> Console:
    return Console(
        file=StringIO(),
        width=width,
        height=height,
        force_terminal=True,
        color_system=None,
        legacy_windows=False,
        _environ={},
    )


def make_renderer(height: int = 8):
    console = make_console(height=height)
    return SimpleNamespace(
        console=console,
        _message_history=[],
        _verbose=False,
        _render_text_block=lambda text, continuation=False: [Text(f"TXT:{text}")],
        _render_tool_header=lambda rec: Text("HEAD\n  CHILD"),
        _render_tool_result=lambda rec: Text("RESULT"),
    )


class TestTranscriptView:
    def test_run_returns_immediately_without_lines(self, monkeypatch):
        view = TranscriptView(make_renderer())
        enter = MagicMock()
        leave = MagicMock()
        monkeypatch.setattr(view, "_render_lines", lambda: [])
        monkeypatch.setattr(view._screen, "enter_alternate_screen", enter)
        monkeypatch.setattr(view._screen, "leave_alternate_screen", leave)

        view.run()

        enter.assert_not_called()
        leave.assert_not_called()

    def test_run_enters_and_leaves_alternate_screen(self, monkeypatch):
        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read_key(self, timeout=None):
                if timeout is None:
                    return KeyEvent(key="o", char="o", ctrl=True)
                return None

        view = TranscriptView(make_renderer())
        draw = MagicMock()
        enter = MagicMock()
        leave = MagicMock()
        monkeypatch.setattr("iac_code.ui.transcript_view.RawInputCapture", FakeCapture)
        monkeypatch.setattr(view, "_render_lines", lambda: ["line 1"])
        monkeypatch.setattr(view, "_draw", draw)
        monkeypatch.setattr(view._screen, "enter_alternate_screen", enter)
        monkeypatch.setattr(view._screen, "leave_alternate_screen", leave)

        view.run()

        enter.assert_called_once()
        leave.assert_called_once()
        draw.assert_called_once_with(["line 1"])

    def test_render_lines_includes_history_and_current_segments(self):
        renderer = make_renderer()
        renderer._message_history = [
            RenderedTurn(role="user", text="hello"),
            RenderedTurn(
                role="assistant",
                segments=[
                    _Segment(kind="text", text="answer"),
                    _Segment(
                        kind="tool", tool=_ToolCallRecord(tool_name="demo", tool_input={"prompt": "do work"}, done=True)
                    ),
                ],
            ),
        ]
        view = TranscriptView(renderer, current_segments=[_Segment(kind="text", text="streaming")])

        lines = view._render_lines()

        joined = "\n".join(lines)
        assert "❯ hello" in joined
        assert "TXT:answer" in joined
        assert "HEAD" in joined
        assert "RESULT" in joined
        assert "TXT:streaming" in joined

    def test_render_tool_inserts_prompt_between_header_and_children(self):
        renderer = make_renderer()
        view = TranscriptView(renderer)
        rec = _ToolCallRecord(tool_name="demo", tool_input={"prompt": "line 1\nline 2"}, done=True)

        view._render_tool(rec)

        output = renderer.console.file.getvalue()
        head_pos = output.index("HEAD")
        prompt_pos = output.index("Prompt:")
        child_pos = output.index("CHILD")
        result_pos = output.index("RESULT")
        assert head_pos < prompt_pos < child_pos < result_pos

    def test_draw_crops_oldest_lines_and_renders_footer(self):
        renderer = make_renderer(height=6)
        view = TranscriptView(renderer)
        view._draw(["one", "two", "three", "four", "five", "six"])

        output = renderer.console.file.getvalue()
        assert "one" not in output
        assert "two" not in output
        assert "three" in output
        assert "six" in output
        assert "Showing transcript" in output

    def test_should_exit_handles_shortcuts(self):
        view = TranscriptView(make_renderer())
        assert view._should_exit(KeyEvent(key="o", char="o", ctrl=True)) is True
        assert view._should_exit(KeyEvent(key="c", char="c", ctrl=True)) is True
        assert view._should_exit(KeyEvent(key="escape", char="", ctrl=False)) is True
        assert view._should_exit(KeyEvent(key="enter", char="", ctrl=False)) is False
