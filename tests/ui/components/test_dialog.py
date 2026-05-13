"""Tests for Dialog component."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console
from rich.text import Text

from iac_code.ui.components.dialog import Dialog
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.keybindings.manager import KeybindingManager


def make_dialog(title="Test Dialog", **kwargs):
    km = KeybindingManager()
    cancelled = []
    dialog = Dialog(
        title=title,
        keybinding_manager=km,
        on_cancel=lambda: cancelled.append(True),
        **kwargs,
    )
    return dialog, km, cancelled


class TestDialogShow:
    def test_show_renders_frame_with_title(self, capsys):
        dialog, _, _ = make_dialog(title="My Dialog")
        dialog.show(Text("Body content"))
        out = capsys.readouterr().out
        assert "My Dialog" in out

    def test_show_renders_body(self, capsys):
        dialog, _, _ = make_dialog()
        dialog.show(Text("Hello Body"))
        out = capsys.readouterr().out
        assert "Hello Body" in out

    def test_show_with_subtitle(self, capsys):
        dialog, _, _ = make_dialog(subtitle="Subtitle text")
        dialog.show(Text("Body"))
        out = capsys.readouterr().out
        assert "Subtitle text" in out

    def test_show_with_footer_hints(self, capsys):
        dialog, _, _ = make_dialog(footer_hints=[("Enter", "confirm"), ("Esc", "cancel")])
        dialog.show(Text("Body"))
        out = capsys.readouterr().out
        assert "Enter" in out
        assert "Esc" in out


class TestDialogClose:
    def test_close_marks_dialog_closed(self):
        dialog, _, _ = make_dialog()
        assert not dialog._closed
        dialog.close()
        assert dialog._closed


class TestDialogFooterHints:
    def test_footer_hints_keys_appear_in_output(self, capsys):
        dialog, _, _ = make_dialog(footer_hints=[("Ctrl+S", "save"), ("Ctrl+Q", "quit")])
        dialog.show(Text("Body"))
        out = capsys.readouterr().out
        assert "Ctrl+S" in out
        assert "Ctrl+Q" in out


class TestDialogRun:
    """Cover the run() event loop."""

    def test_build_frame_returns_panel_with_title(self):
        from rich.panel import Panel

        dialog, _, _ = make_dialog(title="Frame Title")
        panel = dialog._build_frame(Text("Inner body"))
        assert isinstance(panel, Panel)
        assert "Frame Title" in str(panel.title)

    def test_build_frame_renders_to_console(self):
        out = StringIO()
        rich_console = Console(
            file=out,
            width=80,
            force_terminal=True,
            color_system=None,
            legacy_windows=False,
            _environ={},
        )
        dialog, _, _ = make_dialog(title="Render Frame")
        panel = dialog._build_frame(Text("Frame content"))
        rich_console.print(panel)
        output = out.getvalue()
        assert "Render Frame" in output
        assert "Frame content" in output

    def test_run_calls_body_builder_and_exits_on_close(self):
        dialog, _, _ = make_dialog(title="Run Dialog")

        call_count = [0]

        def body_builder():
            call_count[0] += 1
            dialog.close()
            return Text("Loop body")

        mock_cap = MagicMock()
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)
        mock_cap.read_key = MagicMock(return_value=None)

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            dialog.run(body_builder=body_builder)

        assert call_count[0] >= 1

    def test_run_calls_key_handler_when_key_event_available(self):
        dialog, _, _ = make_dialog(title="Key Handler")

        key_events_seen = []

        def body_builder():
            return Text("body")

        def key_handler(event):
            key_events_seen.append(event)
            dialog.close()
            return True

        mock_cap = MagicMock()
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)

        test_event = KeyEvent(key="a", char="a")
        mock_cap.read_key = MagicMock(side_effect=[test_event, None])

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            dialog.run(body_builder=body_builder, key_handler=key_handler)

        assert len(key_events_seen) == 1
        assert key_events_seen[0] == test_event

    def test_run_falls_through_to_km_resolve_when_key_handler_not_consumed(self):
        dialog, km, _ = make_dialog(title="KM Resolve")

        resolved_events = []

        def tracking_resolve(event):
            resolved_events.append(event)
            dialog.close()
            return False

        km.resolve = tracking_resolve

        test_event = KeyEvent(key="x", char="x")
        mock_cap = MagicMock()
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)
        mock_cap.read_key = MagicMock(side_effect=[test_event, None])

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            dialog.run(body_builder=lambda: Text("body"), key_handler=lambda e: False)

        assert len(resolved_events) == 1
        assert resolved_events[0] == test_event

    def test_run_cleans_up_on_exit(self):
        dialog, km, _ = make_dialog(title="Cleanup")

        mock_cap = MagicMock()
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)
        mock_cap.read_key = MagicMock(return_value=None)

        call_count = [0]

        def body_builder():
            call_count[0] += 1
            dialog.close()
            return Text("body")

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            dialog.run(body_builder=body_builder)

        assert "dialog" not in km.active_contexts
