"""Tests for ScreenManager."""

import io

from rich.console import Console

from iac_code.ui.core.screen import ScreenManager


def make_manager() -> tuple["ScreenManager", io.StringIO]:
    """Create a ScreenManager backed by an in-memory StringIO Console."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=80, height=24)
    manager = ScreenManager(console)
    return manager, buf


class TestScreenManager:
    def test_enter_alternate_screen_writes_escape(self):
        manager, buf = make_manager()
        manager.enter_alternate_screen()
        output = buf.getvalue()
        assert "\033[?1049h" in output

    def test_enter_alternate_screen_moves_cursor_home(self):
        manager, buf = make_manager()
        manager.enter_alternate_screen()
        output = buf.getvalue()
        assert "\033[H" in output

    def test_leave_alternate_screen_writes_escape(self):
        manager, buf = make_manager()
        manager.leave_alternate_screen()
        output = buf.getvalue()
        assert "\033[?1049l" in output

    def test_clear_writes_escape(self):
        manager, buf = make_manager()
        manager.clear()
        output = buf.getvalue()
        assert "\033[H\033[2J" in output

    def test_clear_and_render_clears_first(self):
        manager, buf = make_manager()
        manager.clear_and_render("hello")
        output = buf.getvalue()
        assert "\033[H\033[2J" in output

    def test_clear_and_render_includes_content(self):
        manager, buf = make_manager()
        manager.clear_and_render("hello world")
        output = buf.getvalue()
        assert "hello world" in output

    def test_clear_and_render_uses_crlf_for_raw_mode(self):
        # Under raw mode OPOST is off, so bare \n leaves the cursor in the
        # previous column. clear_and_render must emit \r\n so each line
        # starts at column 0.
        manager, buf = make_manager()
        manager.clear_and_render("first\nsecond")
        output = buf.getvalue()
        assert "first\r\nsecond" in output
        # And no bare \n should remain (other than the \r\n we just checked).
        assert "\nsecond" not in output.replace("\r\nsecond", "")

    def test_get_size_returns_cols_rows(self):
        manager, buf = make_manager()
        cols, rows = manager.get_size()
        assert cols == 80
        assert rows == 24

    def test_get_size_type(self):
        manager, buf = make_manager()
        cols, rows = manager.get_size()
        assert isinstance(cols, int)
        assert isinstance(rows, int)
