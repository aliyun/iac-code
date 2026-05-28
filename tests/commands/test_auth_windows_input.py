"""Tests for Windows-specific input helpers in commands/auth.py.

These verify the byte-level parsing logic added for Phase 1 Windows support:
- ``_read_input_events_win``: drains kbhit, batches paste content, parses
  CR / LF / Esc / Ctrl+C / extended keys / UTF-8 multi-byte.
- ``_select_read_key_win``: maps msvcrt key codes to selector actions.

The functions go through ``_get_msvcrt()`` which lazily imports msvcrt; the
tests inject a fake module so the suite runs on macOS / Linux.
"""

from __future__ import annotations

from typing import Iterable
from unittest.mock import patch


class _FakeMsvcrt:
    """Minimal msvcrt stand-in that delivers a scripted byte stream."""

    def __init__(self, byte_stream: Iterable[int]):
        self._buffer = list(byte_stream)
        self._pos = 0

    def kbhit(self) -> bool:
        return self._pos < len(self._buffer)

    def getch(self) -> bytes:
        b = self._buffer[self._pos]
        self._pos += 1
        return bytes([b])


def _patched(byte_stream: Iterable[int]):
    fake = _FakeMsvcrt(byte_stream)
    return fake, patch("iac_code.commands.auth._get_msvcrt", return_value=fake)


class TestReadInputEventsWin:
    def test_single_printable_char(self):
        from iac_code.commands.auth import _read_input_events_win

        _, p = _patched([ord("a")])
        with p:
            assert _read_input_events_win() == [("char", "a")]

    def test_paste_batch_drains_kbhit(self):
        """A pasted multi-character key arrives byte-by-byte; the function
        must drain kbhit to assemble the full batch in one call."""
        from iac_code.commands.auth import _read_input_events_win

        # "abc" + Enter — Enter terminates and remaining bytes (none here) discarded.
        _, p = _patched([ord("a"), ord("b"), ord("c"), 13])
        with p:
            evs = _read_input_events_win()
        assert evs == [("char", "a"), ("char", "b"), ("char", "c"), ("enter",)]

    def test_crlf_terminates_on_cr(self):
        """\\r\\n: CR triggers Enter; the LF after it is left in the buffer
        and consumed by the next call (there is no second call here, so it
        stays — caller's loop has already finished by then)."""
        from iac_code.commands.auth import _read_input_events_win

        fake = _FakeMsvcrt([ord("x"), 13, 10])
        with patch("iac_code.commands.auth._get_msvcrt", return_value=fake):
            evs = _read_input_events_win()
        assert evs == [("char", "x"), ("enter",)]

    def test_utf8_multibyte_char(self):
        """Three-byte UTF-8 (e.g. CJK) must arrive as a single ('char', X) event,
        not three separate bytes — proving batched read enables UTF-8 decoding."""
        from iac_code.commands.auth import _read_input_events_win

        # '中' = U+4E2D, UTF-8: e4 b8 ad
        _, p = _patched([0xE4, 0xB8, 0xAD])
        with p:
            evs = _read_input_events_win()
        assert evs == [("char", "中")]

    def test_escape_yields_back(self):
        from iac_code.commands.auth import _read_input_events_win

        _, p = _patched([27])
        with p:
            assert _read_input_events_win() == [("back",)]

    def test_byte_three_yields_cancel_defensively(self):
        """Defensive: msvcrt rarely delivers byte 3 (CRT raises
        KeyboardInterrupt instead), but we keep the mapping for unusual
        Console hosts that pass it through."""
        from iac_code.commands.auth import _read_input_events_win

        _, p = _patched([3])
        with p:
            assert _read_input_events_win() == [("cancel",)]

    def test_backspace_byte_8(self):
        from iac_code.commands.auth import _read_input_events_win

        _, p = _patched([8])
        with p:
            assert _read_input_events_win() == [("backspace",)]

    def test_extended_up_arrow(self):
        from iac_code.commands.auth import _read_input_events_win

        # 0xE0 prefix + 0x48 (up)
        _, p = _patched([0xE0, 0x48])
        with p:
            assert _read_input_events_win() == [("up",)]

    def test_extended_down_arrow(self):
        from iac_code.commands.auth import _read_input_events_win

        _, p = _patched([0xE0, 0x50])
        with p:
            assert _read_input_events_win() == [("down",)]

    def test_extended_delete_treated_as_backspace(self):
        from iac_code.commands.auth import _read_input_events_win

        _, p = _patched([0xE0, 0x53])
        with p:
            assert _read_input_events_win() == [("backspace",)]

    def test_unmapped_extended_key_silently_consumed(self):
        """Left/Right/Home/End/PageUp/PageDown have no event in Phase 1 —
        they are consumed without producing an event (matching Unix's CSI
        skip behavior for keys it doesn't care about)."""
        from iac_code.commands.auth import _read_input_events_win

        for ext in (0x4B, 0x4D, 0x47, 0x4F, 0x49, 0x51):  # left/right/home/end/pgup/pgdn
            _, p = _patched([0xE0, ext])
            with p:
                evs = _read_input_events_win()
            assert evs == [], f"extended key 0x{ext:02X} should be consumed silently"

    def test_extended_followed_by_char_is_processed(self):
        """An extended key (consumed silently) followed by a printable char
        in the same drain still yields the char event."""
        from iac_code.commands.auth import _read_input_events_win

        # Left arrow (silently consumed) then 'q'
        _, p = _patched([0xE0, 0x4B, ord("q")])
        with p:
            evs = _read_input_events_win()
        assert evs == [("char", "q")]


class TestSelectReadKeyWin:
    def test_enter_via_cr(self):
        from iac_code.commands.auth import _select_read_key_win

        _, p = _patched([13])
        with p:
            assert _select_read_key_win() == ("enter", None)

    def test_enter_via_lf(self):
        from iac_code.commands.auth import _select_read_key_win

        _, p = _patched([10])
        with p:
            assert _select_read_key_win() == ("enter", None)

    def test_escape_cancels(self):
        from iac_code.commands.auth import _select_read_key_win

        _, p = _patched([27])
        with p:
            assert _select_read_key_win() == ("cancel", None)

    def test_byte_three_cancels_defensively(self):
        from iac_code.commands.auth import _select_read_key_win

        _, p = _patched([3])
        with p:
            assert _select_read_key_win() == ("cancel", None)

    def test_up_arrow(self):
        from iac_code.commands.auth import _select_read_key_win

        _, p = _patched([0xE0, 0x48])
        with p:
            assert _select_read_key_win() == ("up", None)

    def test_down_arrow(self):
        from iac_code.commands.auth import _select_read_key_win

        _, p = _patched([0xE0, 0x50])
        with p:
            assert _select_read_key_win() == ("down", None)

    def test_unmapped_extended_returns_none_pair(self):
        """Other extended keys (left/right/etc.) are ignored in selectors
        that only need up/down navigation — caller's loop skips (None, None)."""
        from iac_code.commands.auth import _select_read_key_win

        for ext in (0x4B, 0x4D, 0x47, 0x4F, 0x49, 0x51, 0x53):
            _, p = _patched([0xE0, ext])
            with p:
                assert _select_read_key_win() == (None, None)


class TestKeyboardInterruptHandling:
    """Verify the auth flow's Windows branches return None when msvcrt
    raises KeyboardInterrupt (the documented Ctrl+C path)."""

    def test_select_returns_none_on_keyboard_interrupt(self):
        from iac_code.commands import auth

        class _RaisingMsvcrt:
            def kbhit(self):  # pragma: no cover — never called
                return True

            def getch(self):
                raise KeyboardInterrupt

        # _select renders before reading; stub the rendering helpers so the
        # test doesn't write to stdout.
        with (
            patch.object(auth, "_IS_WIN32", True),
            patch.object(auth, "_get_msvcrt", return_value=_RaisingMsvcrt()),
            patch.object(auth, "_clear_screen"),
            patch.object(auth, "_render_title"),
            patch.object(auth, "_render_options"),
        ):
            result = auth._select("title", ["a", "b"], default_index=0)
        assert result is None

    def test_input_text_returns_none_on_keyboard_interrupt(self):
        from iac_code.commands import auth

        class _RaisingMsvcrt:
            def kbhit(self):  # pragma: no cover
                return True

            def getch(self):
                raise KeyboardInterrupt

        with (
            patch.object(auth, "_IS_WIN32", True),
            patch.object(auth, "_get_msvcrt", return_value=_RaisingMsvcrt()),
            patch.object(auth, "_clear_screen"),
            patch.object(auth, "_render_title"),
            patch.object(auth, "_write"),
            patch.object(auth, "_flush"),
        ):
            result = auth._input_text("title", "prompt: ")
        assert result is None
