"""Windows implementation of raw terminal input using msvcrt."""

from __future__ import annotations

import re
import sys
import time

from iac_code.ui.core.key_event import KeyEvent
from iac_code.utils.console import stdout_supports_virtual_terminal

# Windows extended key codes (after \xe0 or \x00 prefix)
_EXTENDED_KEY_MAP: dict[int, str] = {
    0x48: "up",
    0x50: "down",
    0x4D: "right",
    0x4B: "left",
    0x47: "home",
    0x4F: "end",
    0x53: "delete",
    0x49: "pageup",
    0x51: "pagedown",
}

# ANSI CSI sequence body (after ESC [) — sent by ConPTY, Windows Terminal, RDP
_ANSI_CSI_MAP: dict[str, str] = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "3~": "delete",
    "5~": "pageup",
    "6~": "pagedown",
}
_CSI_U_RE = re.compile(r"(\d+);(\d+)u")
_XTERM_MODIFY_OTHER_KEYS_RE = re.compile(r"27;(\d+);(\d+)~")
_MODIFIED_SPECIAL_KEY_RE = re.compile(r"(\d+);(\d+)~")
_ENTER_CODEPOINTS = {10, 13}


def _get_msvcrt():
    """Lazy import of msvcrt to support testing on non-Windows platforms."""
    import msvcrt

    return msvcrt


class RawInputCapture:
    """Windows implementation of raw terminal input.

    On Windows, msvcrt provides direct key reading without needing
    to change terminal modes. The context manager is a no-op.

    Handles both traditional Windows extended key codes (\\xe0 + scancode)
    and ANSI/VT escape sequences (ESC [ ...) which arrive from ConPTY,
    Windows Terminal, and Remote Desktop.
    """

    def __init__(self, fd: int | None = None, use_cbreak: bool = False) -> None:
        # ``fd`` and ``use_cbreak`` are accepted for API compatibility with the
        # Unix RawInputCapture but are unused on Windows: msvcrt always reads
        # from the console (CONIN$) regardless of which fd is currently mapped
        # to stdin, and termios cooked-vs-raw modes don't apply.
        self._fd = fd
        self._use_cbreak = use_cbreak
        self._pending: str | None = None
        self._bracketed_paste_enabled = False

    def __enter__(self) -> "RawInputCapture":
        # W-I3: enable bracketed paste so multi-char pastes (CJK etc.) arrive
        # as one ESC [ 200 ~ ... ESC [ 201 ~ block instead of char-by-char.
        self._bracketed_paste_enabled = False
        if not stdout_supports_virtual_terminal():
            return self
        try:
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()
            self._bracketed_paste_enabled = True
        except (OSError, ValueError):
            pass  # closed stdout or non-tty
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._bracketed_paste_enabled:
            return
        try:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        finally:
            self._bracketed_paste_enabled = False

    def read_key(self, timeout: float | None = None) -> KeyEvent | None:
        """Read a single key press and return the corresponding KeyEvent."""
        if self._pending is not None:
            return self._read_one_key()

        msvcrt = _get_msvcrt()
        if timeout is not None:
            deadline = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)

        # No timeout → blocking getwch(). Polling kbhit() at 100Hz here
        # interferes with Microsoft Pinyin's Shift→English mode and causes
        # typed characters to stay buffered inside the IME until something
        # else (e.g. switching back to Chinese + Space) forces a commit.
        # getwch() blocks until a character is delivered and still raises
        # KeyboardInterrupt on Ctrl+C, which the caller already handles.
        return self._read_one_key()

    def _read_one_key(self) -> KeyEvent:
        """Read a single key event from msvcrt."""
        msvcrt = _get_msvcrt()

        if self._pending is not None:
            ch = self._pending
            self._pending = None
        else:
            ch = msvcrt.getwch()
        code = ord(ch)

        # Extended key prefix — traditional Windows console
        if ch in ("\x00", "\xe0"):
            ext_ch = msvcrt.getwch()
            ext = ord(ext_ch)
            key_name = _EXTENDED_KEY_MAP.get(ext, "unknown")
            return KeyEvent(key=key_name, char="")

        # ESC — may begin ANSI/VT escape sequence (ConPTY, Windows Terminal, RDP)
        if code == 27:
            # W-I2: extend timeout to 100ms; ConPTY scheduling jitter can delay
            # the follow-up byte beyond 50ms, causing arrow keys to register
            # as a lone ESC + stray characters.
            next_ch = self._wait_for_key(msvcrt, timeout=0.10)
            if next_ch is not None:
                if next_ch == "[":
                    return self._read_csi_sequence(msvcrt)
                if next_ch == "O":
                    # W-I2: SS3 sequence (ESC O X) — app-mode arrow keys + F1-F4
                    return self._read_ss3_sequence(msvcrt)
                self._pending = next_ch
            return KeyEvent(key="escape", char="\x1b")

        # Enter
        if code in (13, 10):
            return KeyEvent(key="enter", char=ch)

        # Tab
        if code == 9:
            return KeyEvent(key="tab", char="\t")

        # Backspace (0x08) or DEL (0x7f, sent by some RDP/terminal combos)
        if code in (8, 127):
            return KeyEvent(key="backspace", char=ch)

        # Ctrl+A through Ctrl+Z (1-26, excluding handled above: 9=Tab, 10=LF, 13=CR)
        if 1 <= code <= 26:
            letter = chr(ord("a") + code - 1)
            return KeyEvent(key=letter, char=ch, ctrl=True)

        # Printable characters (ASCII and beyond)
        if code >= 32:
            return KeyEvent(key=ch, char=ch)

        # Fallback
        return KeyEvent(key="unknown", char=ch)

    @staticmethod
    def _wait_for_key(msvcrt, timeout: float) -> str | None:
        """Poll kbhit() until a key arrives or *timeout* seconds elapse."""
        deadline = time.monotonic() + timeout
        while not msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)
        return msvcrt.getwch()

    @staticmethod
    def _read_csi_sequence(msvcrt) -> KeyEvent:
        """Parse CSI sequence body after ESC [ has been consumed.

        Each character is waited for independently with a short timeout
        so a truncated sequence (lost bytes, slow RDP) never blocks the UI.
        """
        buf = ""
        for _ in range(8):
            # W-I2: same 100ms budget as the initial ESC wait — once we're
            # past ESC, subsequent body bytes are subject to the same
            # ConPTY/RDP scheduling jitter.
            ch = RawInputCapture._wait_for_key(msvcrt, timeout=0.10)
            if ch is None:
                return KeyEvent(key="unknown" if buf else "escape", char="")
            buf += ch
            # W-I3: bracketed paste start marker — dispatch to paste reader.
            if buf == "200~":
                return RawInputCapture._read_bracketed_paste(msvcrt)
            if ch.isalpha() or ch == "~":
                break
        modified_key = RawInputCapture._parse_modified_key_sequence(buf)
        if modified_key is not None:
            return modified_key
        key_name = _ANSI_CSI_MAP.get(buf, "unknown")
        return KeyEvent(key=key_name, char="")

    @staticmethod
    def _event_from_codepoint(codepoint: int, modifier: int) -> KeyEvent | None:
        """Build a KeyEvent from CSI-u / modifyOtherKeys codepoint data."""
        flags = max(modifier - 1, 0)
        shift = bool(flags & 1)
        alt = bool(flags & 2)
        ctrl = bool(flags & 4)

        if codepoint in _ENTER_CODEPOINTS:
            if shift and not alt and not ctrl:
                return KeyEvent(key="enter", char="", shift=True)
            return None

        if 32 <= codepoint <= 0x10FFFF:
            key = chr(codepoint)
            if ctrl:
                key = key.lower()
            return KeyEvent(key=key, char="", ctrl=ctrl, alt=alt, shift=shift)

        return None

    @staticmethod
    def _parse_modified_key_sequence(seq: str) -> KeyEvent | None:
        """Parse common terminal encodings for modified keys."""
        m = _CSI_U_RE.fullmatch(seq)
        if m is not None:
            codepoint = int(m.group(1))
            modifier = int(m.group(2))
            return RawInputCapture._event_from_codepoint(codepoint, modifier)

        m = _XTERM_MODIFY_OTHER_KEYS_RE.fullmatch(seq)
        if m is not None:
            modifier = int(m.group(1))
            codepoint = int(m.group(2))
            return RawInputCapture._event_from_codepoint(codepoint, modifier)

        m = _MODIFIED_SPECIAL_KEY_RE.fullmatch(seq)
        if m is not None:
            key_code = int(m.group(1))
            modifier = int(m.group(2))
            return RawInputCapture._event_from_codepoint(key_code, modifier)
        return None

    @staticmethod
    def _read_ss3_sequence(msvcrt) -> KeyEvent:
        """Parse SS3 sequence body after ESC O has been consumed.

        SS3 sequences send a single trailing byte: ESC O A/B/C/D for arrows
        in application keypad mode (Windows Terminal, xterm), ESC O P/Q/R/S for F1-F4.
        """
        ch = RawInputCapture._wait_for_key(msvcrt, timeout=0.10)
        if ch is None:
            return KeyEvent(key="escape", char="")
        ss3_map = {
            "A": "up",
            "B": "down",
            "C": "right",
            "D": "left",
            "H": "home",
            "F": "end",
            "P": "f1",
            "Q": "f2",
            "R": "f3",
            "S": "f4",
        }
        key_name = ss3_map.get(ch, "unknown")
        return KeyEvent(key=key_name, char="")

    @staticmethod
    def _read_bracketed_paste(msvcrt) -> KeyEvent:
        """Accumulate paste body after ESC [ 200 ~ until ESC [ 201 ~ end marker."""
        body: list[str] = []
        # Reasonable cap to avoid runaway on malformed input (1MB is far more than any UI paste).
        for _ in range(1024 * 1024):
            ch = RawInputCapture._wait_for_key(msvcrt, timeout=1.0)
            if ch is None:
                break
            if ch == "\x1b":
                # Could be ESC [ 201~ end marker.
                seq = ""
                for _ in range(5):
                    nxt = RawInputCapture._wait_for_key(msvcrt, timeout=0.10)
                    if nxt is None:
                        break
                    seq += nxt
                    if seq == "[201~":
                        return KeyEvent(key="paste", char=_normalize_pasted_text("".join(body)))
                # Not the end marker — treat the ESC + partial seq as literal paste content.
                body.append("\x1b")
                body.append(seq)
                continue
            body.append(ch)
        return KeyEvent(key="paste", char=_normalize_pasted_text("".join(body)))


def _normalize_pasted_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
