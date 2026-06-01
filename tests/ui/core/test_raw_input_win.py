# tests/ui/core/test_raw_input_win.py
from __future__ import annotations

import sys
from itertools import chain, repeat
from unittest.mock import MagicMock, patch

import pytest

from iac_code.ui.core.key_event import KeyEvent


# We can test the module even on non-Windows by mocking msvcrt
@pytest.fixture
def mock_msvcrt():
    mock = MagicMock()
    with patch.dict(sys.modules, {"msvcrt": mock}):
        yield mock


class TestRawInputCaptureWin:
    def test_context_manager_is_noop(self, mock_msvcrt):
        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        result = cap.__enter__()
        assert result is cap
        cap.__exit__(None, None, None)  # no error

    def test_read_key_printable_char(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "a"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="a", char="a")

    def test_read_key_enter(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "\r"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="enter", char="\r")

    def test_read_key_ctrl_c(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "\x03"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="c", char="\x03", ctrl=True)

    def test_read_key_arrow_up(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.side_effect = ["\xe0", "H"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="up", char="")

    def test_read_key_arrow_down(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.side_effect = ["\xe0", "P"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="down", char="")

    def test_read_key_escape_standalone(self, mock_msvcrt):
        """Standalone ESC (no following chars) returns escape event."""
        mock_msvcrt.kbhit.side_effect = chain([True], repeat(False))
        mock_msvcrt.getwch.return_value = "\x1b"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="escape", char="\x1b")

    def test_read_key_timeout_returns_none(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = False

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=0.05)
        assert event is None

    def test_read_key_blocking_path_does_not_poll_kbhit(self, mock_msvcrt):
        """Regression: timeout=None must NOT poll kbhit().

        100Hz PeekConsoleInput polling (via kbhit) interferes with
        Microsoft Pinyin's Shift→English mode and causes typed characters
        to stay buffered inside the IME. The blocking path must rely on
        getwch() alone.
        """
        mock_msvcrt.getwch.return_value = "a"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key()  # timeout=None
        assert event == KeyEvent(key="a", char="a")
        mock_msvcrt.kbhit.assert_not_called()

    def test_read_key_backspace(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "\x08"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="backspace", char="\x08")

    def test_read_key_backspace_del(self, mock_msvcrt):
        """0x7f (DEL) sent by some RDP/terminal combos should also map to backspace."""
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "\x7f"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="backspace", char="\x7f")

    def test_read_key_tab(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "\t"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="tab", char="\t")

    def test_read_key_delete(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.side_effect = ["\xe0", "S"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="delete", char="")

    def test_read_key_non_ascii_chinese(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "你"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="你", char="你")

    def test_read_key_non_ascii_japanese(self, mock_msvcrt):
        mock_msvcrt.kbhit.return_value = True
        mock_msvcrt.getwch.return_value = "あ"

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="あ", char="あ")


class TestAnsiEscapeSequences:
    """ANSI/VT escape sequences sent by ConPTY, Windows Terminal, and RDP."""

    @pytest.fixture(autouse=True)
    def _mock_msvcrt(self):
        self.mock = MagicMock()
        with patch.dict(sys.modules, {"msvcrt": self.mock}):
            yield

    @pytest.mark.parametrize(
        "seq_chars, expected_key",
        [
            (["A"], "up"),
            (["B"], "down"),
            (["C"], "right"),
            (["D"], "left"),
            (["H"], "home"),
            (["F"], "end"),
            (["3", "~"], "delete"),
            (["5", "~"], "pageup"),
            (["6", "~"], "pagedown"),
        ],
    )
    def test_ansi_csi_sequences(self, seq_chars, expected_key):
        """ESC [ <seq> should map to the correct key name."""
        # kbhit: read_key loop=True, ESC follow-up=True, then one True per CSI body char
        self.mock.kbhit.side_effect = [True, True] + [True] * len(seq_chars)
        self.mock.getwch.side_effect = ["\x1b", "[", *seq_chars]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key=expected_key, char="")

    def test_esc_followed_by_non_bracket_preserves_char(self):
        """ESC + non-'[' char: return escape, next read_key returns the char."""
        self.mock.kbhit.side_effect = [True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "O"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event1 = cap.read_key(timeout=1.0)
        assert event1 == KeyEvent(key="escape", char="\x1b")

        event2 = cap.read_key(timeout=1.0)
        assert event2 == KeyEvent(key="O", char="O")

    def test_unknown_csi_sequence(self):
        """Unrecognized CSI sequence returns 'unknown'."""
        self.mock.kbhit.side_effect = [True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "[", "Z"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="unknown", char="")

    def test_shift_enter_csi_u(self):
        self.mock.kbhit.side_effect = [True, True, True, True, True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "[", "1", "3", ";", "2", "u"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)

        assert event == KeyEvent(key="enter", char="", shift=True)

    def test_shift_enter_xterm_modify_other_keys(self):
        self.mock.kbhit.side_effect = [True, True, True, True, True, True, True, True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "[", "2", "7", ";", "2", ";", "1", "3", "~"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)

        assert event == KeyEvent(key="enter", char="", shift=True)

    def test_shift_enter_modified_special_key(self):
        self.mock.kbhit.side_effect = [True, True, True, True, True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "[", "1", "3", ";", "2", "~"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)

        assert event == KeyEvent(key="enter", char="", shift=True)

    def test_unknown_modified_enter_csi_u(self):
        self.mock.kbhit.side_effect = [True, True, True, True, True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "[", "1", "3", ";", "5", "u"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)

        assert event == KeyEvent(key="unknown", char="")

    def test_ctrl_c_xterm_modify_other_keys(self):
        self.mock.kbhit.side_effect = [True, True, True, True, True, True, True, True, True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "[", "2", "7", ";", "5", ";", "9", "9", "~"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)

        assert event == KeyEvent(key="c", char="", ctrl=True)

    def test_ctrl_r_csi_u(self):
        self.mock.kbhit.side_effect = [True, True, True, True, True, True, True, True]
        self.mock.getwch.side_effect = ["\x1b", "[", "1", "1", "4", ";", "5", "u"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)

        assert event == KeyEvent(key="r", char="", ctrl=True)

    def test_esc_followup_delayed_arrival(self):
        """ESC followed by '[' after a brief delay should still parse as CSI."""
        call_count = 0

        def kbhit_delay():
            nonlocal call_count
            call_count += 1
            # read_key loop: immediately ready
            if call_count == 1:
                return True
            # ESC follow-up: first poll returns False (simulating RDP delay),
            # second poll returns True — bracket has arrived
            if call_count == 2:
                return False
            return True

        self.mock.kbhit.side_effect = kbhit_delay
        self.mock.getwch.side_effect = ["\x1b", "[", "A"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="up", char="")

    def test_csi_multi_char_body_delayed(self):
        """Delete sequence ESC [ 3 ~ should parse correctly even when
        '~' arrives after a brief delay (each body char gets its own timeout)."""
        call_count = 0

        def kbhit_delay():
            nonlocal call_count
            call_count += 1
            # 1: read_key loop, 2: ESC follow-up → '[' ready
            if call_count <= 2:
                return True
            # 3: '3' ready immediately
            if call_count == 3:
                return True
            # 4: '~' not ready yet (RDP delay), 5: '~' arrives
            if call_count == 4:
                return False
            return True

        self.mock.kbhit.side_effect = kbhit_delay
        self.mock.getwch.side_effect = ["\x1b", "[", "3", "~"]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()
        event = cap.read_key(timeout=1.0)
        assert event == KeyEvent(key="delete", char="")

    def test_csi_body_timeout_does_not_block(self):
        """If CSI body never arrives (ESC [ with no following char),
        read_key must return within the timeout, not hang."""
        self.mock.kbhit.side_effect = chain([True, True], repeat(False))
        self.mock.getwch.side_effect = ["\x1b", "["]

        from iac_code.ui.core.raw_input_win import RawInputCapture

        cap = RawInputCapture()
        cap.__enter__()

        import time

        start = time.monotonic()
        event = cap.read_key(timeout=1.0)
        elapsed = time.monotonic() - start

        assert event is not None
        assert event.key in ("unknown", "escape")
        assert elapsed < 0.5
