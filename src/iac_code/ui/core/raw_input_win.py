"""Windows implementation of raw terminal input using msvcrt."""

from __future__ import annotations

import time

from iac_code.ui.core.key_event import KeyEvent

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

    def __init__(self, fd: int | None = None) -> None:
        # ``fd`` is accepted for API compatibility with the Unix RawInputCapture
        # but is unused on Windows: msvcrt always reads from the console
        # (CONIN$) regardless of which fd is currently mapped to stdin.
        self._fd = fd
        self._pending: str | None = None

    def __enter__(self) -> "RawInputCapture":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

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
        else:
            while not msvcrt.kbhit():
                time.sleep(0.01)

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
            next_ch = self._wait_for_key(msvcrt, timeout=0.05)
            if next_ch is not None:
                if next_ch == "[":
                    return self._read_csi_sequence(msvcrt)
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
            ch = RawInputCapture._wait_for_key(msvcrt, timeout=0.05)
            if ch is None:
                return KeyEvent(key="unknown" if buf else "escape", char="")
            buf += ch
            if ch.isalpha() or ch == "~":
                break
        key_name = _ANSI_CSI_MAP.get(buf, "unknown")
        return KeyEvent(key=key_name, char="")
