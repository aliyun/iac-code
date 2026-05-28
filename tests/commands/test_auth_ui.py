"""Tests for auth.py UI primitives (_render_*, _read_input_events)."""

import pytest

from iac_code.commands.auth import _read_input_events, _render_options, _render_title


class TestRenderHelpers:
    def test_render_title_no_error(self, capsys):
        _render_title("Hello")
        captured = capsys.readouterr()
        assert "Hello" in captured.out

    def test_render_options_no_error(self, capsys):
        _render_options(["A", "B", "C"], selected=1, hints="Navigate")
        captured = capsys.readouterr()
        assert "A" in captured.out
        assert "B" in captured.out
        assert "Navigate" in captured.out


class TestReadInputEvents:
    """`_read_input_events(fd)` reads from the given fd and returns a list of tuples."""

    @pytest.fixture(autouse=True)
    def _force_unix_input_path(self, monkeypatch):
        import iac_code.commands.auth as auth_mod

        monkeypatch.setattr(auth_mod, "_IS_WIN32", False)

    def test_empty_returns_empty_list(self, monkeypatch):
        import iac_code.commands.auth as auth_mod

        monkeypatch.setattr(auth_mod.os, "read", lambda fd, n: b"")
        events = _read_input_events(fd=0)
        assert events == []

    def test_enter_key_returns_enter_event(self, monkeypatch):
        import iac_code.commands.auth as auth_mod

        monkeypatch.setattr(auth_mod.os, "read", lambda fd, n: b"\r")
        events = _read_input_events(fd=0)
        assert events == [("enter",)]

    def test_ctrl_c_returns_cancel_event(self, monkeypatch):
        import iac_code.commands.auth as auth_mod

        monkeypatch.setattr(auth_mod.os, "read", lambda fd, n: b"\x03")
        events = _read_input_events(fd=0)
        assert events == [("cancel",)]

    def test_printable_chars_return_char_events(self, monkeypatch):
        import iac_code.commands.auth as auth_mod

        monkeypatch.setattr(auth_mod.os, "read", lambda fd, n: b"hi")
        events = _read_input_events(fd=0)
        assert ("char", "h") in events
        assert ("char", "i") in events

    def test_backspace_returns_backspace_event(self, monkeypatch):
        import iac_code.commands.auth as auth_mod

        monkeypatch.setattr(auth_mod.os, "read", lambda fd, n: b"\x7f")
        events = _read_input_events(fd=0)
        assert events == [("backspace",)]
