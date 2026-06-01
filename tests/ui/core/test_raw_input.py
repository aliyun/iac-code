"""Tests for RawInputCapture static conversion methods (Unix only)."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip("Unix-only tests (termios)", allow_module_level=True)

import termios  # noqa: E402

from iac_code.ui.core.raw_input import RawInputCapture  # noqa: E402


class TestByteToKeyEvent:
    """Tests for _byte_to_key_event static method."""

    def test_enter_cr(self):
        event = RawInputCapture._byte_to_key_event(13)
        assert event.key == "enter"
        assert event.key_id == "enter"

    def test_enter_lf(self):
        event = RawInputCapture._byte_to_key_event(10)
        assert event.key == "enter"

    def test_tab(self):
        event = RawInputCapture._byte_to_key_event(9)
        assert event.key == "tab"

    def test_backspace(self):
        event = RawInputCapture._byte_to_key_event(127)
        assert event.key == "backspace"

    def test_ctrl_a(self):
        event = RawInputCapture._byte_to_key_event(1)
        assert event.key == "a"
        assert event.ctrl is True
        assert event.key_id == "ctrl+a"

    def test_ctrl_z(self):
        event = RawInputCapture._byte_to_key_event(26)
        assert event.key == "z"
        assert event.ctrl is True
        assert event.key_id == "ctrl+z"

    def test_ctrl_c(self):
        event = RawInputCapture._byte_to_key_event(3)
        assert event.key == "c"
        assert event.ctrl is True

    def test_ctrl_range_excludes_tab(self):
        # byte 9 = tab, not ctrl+i
        event = RawInputCapture._byte_to_key_event(9)
        assert event.key == "tab"
        assert event.ctrl is False

    def test_ctrl_range_excludes_lf(self):
        # byte 10 = enter
        event = RawInputCapture._byte_to_key_event(10)
        assert event.key == "enter"
        assert event.ctrl is False

    def test_ctrl_range_excludes_cr(self):
        # byte 13 = enter
        event = RawInputCapture._byte_to_key_event(13)
        assert event.key == "enter"
        assert event.ctrl is False

    def test_printable_lowercase(self):
        event = RawInputCapture._byte_to_key_event(ord("a"))
        assert event.key == "a"
        assert event.char == "a"
        assert event.ctrl is False
        assert event.shift is False

    def test_printable_uppercase_shift(self):
        event = RawInputCapture._byte_to_key_event(ord("A"))
        assert event.key == "A"
        assert event.shift is True

    def test_printable_space(self):
        event = RawInputCapture._byte_to_key_event(32)
        assert event.key == " "
        assert event.char == " "

    def test_printable_tilde(self):
        event = RawInputCapture._byte_to_key_event(126)
        assert event.key == "~"

    def test_escape_standalone(self):
        event = RawInputCapture._byte_to_key_event(27)
        assert event.key == "escape"


class TestParseEscapeSequence:
    """Tests for _parse_escape_sequence static method."""

    def test_arrow_up(self):
        event = RawInputCapture._parse_escape_sequence("[A")
        assert event.key == "up"

    def test_arrow_down(self):
        event = RawInputCapture._parse_escape_sequence("[B")
        assert event.key == "down"

    def test_arrow_right(self):
        event = RawInputCapture._parse_escape_sequence("[C")
        assert event.key == "right"

    def test_arrow_left(self):
        event = RawInputCapture._parse_escape_sequence("[D")
        assert event.key == "left"

    def test_home(self):
        event = RawInputCapture._parse_escape_sequence("[H")
        assert event.key == "home"

    def test_end(self):
        event = RawInputCapture._parse_escape_sequence("[F")
        assert event.key == "end"

    def test_delete(self):
        event = RawInputCapture._parse_escape_sequence("[3~")
        assert event.key == "delete"

    def test_pageup(self):
        event = RawInputCapture._parse_escape_sequence("[5~")
        assert event.key == "pageup"

    def test_pagedown(self):
        event = RawInputCapture._parse_escape_sequence("[6~")
        assert event.key == "pagedown"

    def test_f1(self):
        event = RawInputCapture._parse_escape_sequence("OP")
        assert event.key == "f1"

    def test_f2(self):
        event = RawInputCapture._parse_escape_sequence("OQ")
        assert event.key == "f2"

    def test_f3(self):
        event = RawInputCapture._parse_escape_sequence("OR")
        assert event.key == "f3"

    def test_f4(self):
        event = RawInputCapture._parse_escape_sequence("OS")
        assert event.key == "f4"

    def test_alt_char(self):
        # single printable char after ESC = alt+char
        event = RawInputCapture._parse_escape_sequence("p")
        assert event.key == "p"
        assert event.alt is True
        assert event.key_id == "alt+p"

    def test_alt_char_digit(self):
        event = RawInputCapture._parse_escape_sequence("1")
        assert event.key == "1"
        assert event.alt is True

    def test_unknown_sequence(self):
        event = RawInputCapture._parse_escape_sequence("[999~")
        assert event.key == "unknown"

    def test_shift_enter_csi_u(self):
        event = RawInputCapture._parse_escape_sequence("[13;2u")
        assert event.key == "enter"
        assert event.shift is True
        assert event.key_id == "enter"

    def test_shift_enter_xterm_modify_other_keys(self):
        event = RawInputCapture._parse_escape_sequence("[27;2;13~")
        assert event.key == "enter"
        assert event.shift is True

    def test_shift_enter_modified_special_key(self):
        event = RawInputCapture._parse_escape_sequence("[13;2~")
        assert event.key == "enter"
        assert event.shift is True

    def test_unknown_modified_enter_csi_u(self):
        event = RawInputCapture._parse_escape_sequence("[13;5u")
        assert event.key == "unknown"

    def test_ctrl_c_xterm_modify_other_keys(self):
        event = RawInputCapture._parse_escape_sequence("[27;5;99~")
        assert event.key == "c"
        assert event.ctrl is True

    def test_ctrl_r_csi_u(self):
        event = RawInputCapture._parse_escape_sequence("[114;5u")
        assert event.key == "r"
        assert event.ctrl is True

    def test_mouse_wheel_up(self):
        # SGR mouse encoding: button 64 = wheel up.
        event = RawInputCapture._parse_escape_sequence("[<64;10;5M")
        assert event.key == "wheel_up"

    def test_mouse_wheel_down(self):
        # button 65 = wheel down.
        event = RawInputCapture._parse_escape_sequence("[<65;10;5M")
        assert event.key == "wheel_down"

    def test_mouse_other_button_passes_through(self):
        # Click events (button 0) aren't actionable for the picker —
        # parsed as a generic ``mouse`` event so they don't match the
        # ``unknown`` fallback (which would log noise).
        event = RawInputCapture._parse_escape_sequence("[<0;3;7M")
        assert event.key == "mouse"

    def test_mouse_with_trailing_bytes(self):
        # Rapid wheel ticks pack multiple events into one read; the
        # parser should pick up the first event and ignore the rest.
        event = RawInputCapture._parse_escape_sequence("[<64;10;5M[<64;10;5M")
        assert event.key == "wheel_up"


class TestRawInputCaptureRuntime:
    def test_enter_and_exit_toggle_terminal_modes(self, monkeypatch):
        writes = []
        tcsetattr_calls = []

        monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["old"])
        monkeypatch.setattr("iac_code.ui.core.raw_input.tty.setraw", lambda fd: writes.append(("setraw", fd)))
        monkeypatch.setattr("iac_code.ui.core.raw_input.os.write", lambda fd, data: writes.append((fd, data)))
        monkeypatch.setattr(
            termios,
            "tcsetattr",
            lambda fd, when, settings: tcsetattr_calls.append((fd, when, settings)),
        )

        with RawInputCapture(fd=7) as capture:
            assert capture._old_settings == ["old"]

        assert writes == [
            ("setraw", 7),
            (7, b"\033[?2004h"),
            (7, b"\033[?1004h"),
            (7, b"\033[>1u"),
            (7, b"\033[>4;2m"),
            (7, b"\033[>4;0m"),
            (7, b"\033[<u"),
            (7, b"\033[?1004l"),
            (7, b"\033[?2004l"),
        ]
        assert tcsetattr_calls == [(7, termios.TCSADRAIN, ["old"])]

    def test_exit_ignores_disable_bracket_paste_error(self, monkeypatch):
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["old"])
        monkeypatch.setattr("iac_code.ui.core.raw_input.tty.setraw", lambda fd: None)
        monkeypatch.setattr("iac_code.ui.core.raw_input.os.write", lambda fd, data: None)
        capture = RawInputCapture(fd=7)
        capture.__enter__()

        def fail_write(fd, data):
            raise OSError("broken")

        monkeypatch.setattr("iac_code.ui.core.raw_input.os.write", fail_write)
        restored = []
        monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, settings: restored.append((fd, settings)))

        capture.__exit__(None, None, None)

        assert restored == [(7, ["old"])]

    def test_enter_propagates_oserror_and_clears_settings(self, monkeypatch):
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["old"])
        monkeypatch.setattr("iac_code.ui.core.raw_input.tty.setraw", lambda fd: None)

        def fail_write(fd, data):
            raise OSError("bad fd")

        monkeypatch.setattr("iac_code.ui.core.raw_input.os.write", fail_write)
        capture = RawInputCapture(fd=7)

        with pytest.raises(OSError):
            capture.__enter__()

        assert capture._old_settings is None

    def test_read_key_timeout_returns_none(self, monkeypatch):
        monkeypatch.setattr("select.select", lambda fds, _w, _x, timeout: ([], [], []))
        capture = RawInputCapture(fd=7)

        assert capture.read_key(timeout=0.01) is None

    def test_read_key_returns_none_on_eof(self, monkeypatch):
        monkeypatch.setattr("iac_code.ui.core.raw_input.os.read", lambda fd, n: b"")
        capture = RawInputCapture(fd=7)

        assert capture.read_key() is None

    def test_read_key_parses_standalone_escape(self, monkeypatch):
        monkeypatch.setattr("iac_code.ui.core.raw_input.os.read", lambda fd, n: b"\x1b")
        monkeypatch.setattr("select.select", lambda fds, _w, _x, timeout: ([], [], []))
        capture = RawInputCapture(fd=7)

        event = capture.read_key()

        assert event is not None
        assert event.key == "escape"

    def test_read_key_parses_bracketed_paste(self, monkeypatch):
        reads = iter([b"\x1b", b"[200~hello", b"\r\nworld\x1b[201~"])

        monkeypatch.setattr("iac_code.ui.core.raw_input.os.read", lambda fd, n: next(reads))
        monkeypatch.setattr("select.select", lambda fds, _w, _x, timeout: ([7], [], []))
        capture = RawInputCapture(fd=7)

        event = capture.read_key()

        assert event is not None
        assert event.key == "paste"
        assert event.char == "hello\nworld"

    def test_read_key_parses_utf8_character(self, monkeypatch):
        reads = iter([b"\xe4", b"\xbd", b"\xa0"])
        monkeypatch.setattr("iac_code.ui.core.raw_input.os.read", lambda fd, n: next(reads))
        capture = RawInputCapture(fd=7)

        event = capture.read_key()

        assert event is not None
        assert event.key == "你"
        assert event.char == "你"

    def test_read_bracketed_paste_stops_on_timeout(self, monkeypatch):
        monkeypatch.setattr("select.select", lambda fds, _w, _x, timeout: ([], [], []))
        capture = RawInputCapture(fd=7)

        assert capture._read_bracketed_paste(b"hello\rworld") == "hello\nworld"

    def test_read_utf8_char_handles_invalid_sequence(self, monkeypatch):
        monkeypatch.setattr("iac_code.ui.core.raw_input.os.read", lambda fd, n: b"\xff")
        capture = RawInputCapture(fd=7)

        event = capture._read_utf8_char(b"\xe4")

        assert event.key == "unknown"

    def test_read_key_parses_alt_sequence(self, monkeypatch):
        reads = iter([b"\x1b", b"x"])

        monkeypatch.setattr("iac_code.ui.core.raw_input.os.read", lambda fd, n: next(reads))
        monkeypatch.setattr("select.select", lambda fds, _w, _x, timeout: ([7], [], []))
        capture = RawInputCapture(fd=7)

        event = capture.read_key()

        assert event is not None
        assert event.key == "x"
        assert event.alt is True
