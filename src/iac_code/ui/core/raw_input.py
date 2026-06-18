"""Raw terminal input capture using terminal raw mode."""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Optional

from loguru import logger

from iac_code.ui.core.key_event import KeyEvent

if sys.platform == "win32":
    from iac_code.ui.core.raw_input_win import RawInputCapture  # noqa: F401

    def query_cursor_row(fd: int, timeout: float = 0.1) -> int | None:
        """On Windows, cursor position query is not supported in Phase 1."""
        return None

else:
    import termios
    import tty

    _CURSOR_REPORT_RE = re.compile(rb"\x1b\[(\d+);(\d+)R")

    # SGR-encoded mouse event: ``\x1b[<button;col;row{M|m}``.  Only the
    # leading ``[<button;col;row`` portion is matched against the bytes
    # *after* the ESC byte, since we strip the ESC before parsing escape
    # sequences.
    _MOUSE_SGR_RE = re.compile(r"\[<(\d+);(\d+);(\d+)([Mm])")
    _CSI_U_RE = re.compile(r"\[(\d+);(\d+)u")
    _XTERM_MODIFY_OTHER_KEYS_RE = re.compile(r"\[27;(\d+);(\d+)~")
    _MODIFIED_SPECIAL_KEY_RE = re.compile(r"\[(\d+);(\d+)~")

    _ENTER_CODEPOINTS = {10, 13}
    _ESC_INITIAL_TIMEOUT = 0.15
    _ESC_CONTINUATION_TIMEOUT = 0.15
    _ESC_MAX_SEQUENCE_BYTES = 64

    def query_cursor_row(fd: int, timeout: float = 0.1) -> int | None:
        """Send Device Status Report 6 and parse the cursor's 1-indexed row.

        The terminal must already be in raw mode — under cooked mode the
        response (``\\x1b[<row>;<col>R``) wouldn't be readable until a
        newline. Returns ``None`` if the terminal doesn't reply within
        ``timeout``.
        """
        import select

        try:
            os.write(fd, b"\x1b[6n")
        except OSError:
            return None
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            try:
                chunk = os.read(fd, 32)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if b"R" in buf:
                break
        m = _CURSOR_REPORT_RE.search(buf)
        if m is None:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    # Mapping from escape sequence (after initial ESC byte) to key name
    _ESCAPE_SEQUENCES: dict[str, str] = {
        "[A": "up",
        "[B": "down",
        "[C": "right",
        "[D": "left",
        "[H": "home",
        "[F": "end",
        "[3~": "delete",
        "[5~": "pageup",
        "[6~": "pagedown",
        "[I": "focus_in",
        "[O": "focus_out",
        "OP": "f1",
        "OQ": "f2",
        "OR": "f3",
        "OS": "f4",
    }

    class RawInputCapture:
        """Context manager that puts the terminal into raw mode for key-by-key input.

        Usage:
            with RawInputCapture() as cap:
                event = cap.read_key(timeout=1.0)
        """

        def __init__(self, fd: int | None = None, use_cbreak: bool = False) -> None:
            self._fd = fd if fd is not None else sys.stdin.fileno()
            self._old_settings: Optional[list] = None
            self._use_cbreak = use_cbreak

        def __enter__(self) -> "RawInputCapture":
            try:
                self._old_settings = termios.tcgetattr(self._fd)
                if self._use_cbreak:
                    tty.setcbreak(self._fd)
                else:
                    tty.setraw(self._fd)
                # Enable bracket paste mode so we can distinguish pasted text from typed input
                os.write(self._fd, b"\033[?2004h")
                # Enable focus reporting so we can detect terminal focus changes
                os.write(self._fd, b"\033[?1004h")
                # Ask supporting terminals to report Shift+Enter distinctly.
                os.write(self._fd, b"\033[>1u")
                os.write(self._fd, b"\033[>4;2m")
            except OSError:
                # File descriptor may be invalid after interruption (e.g. double Ctrl+C)
                self._old_settings = None
                raise
            return self

        def __exit__(self, exc_type, exc_val, exc_tb) -> None:
            try:
                # Restore terminal modified-key reporting before leaving raw mode.
                os.write(self._fd, b"\033[>4;0m")
                os.write(self._fd, b"\033[<u")
                # Disable focus reporting
                os.write(self._fd, b"\033[?1004l")
                # Disable bracket paste mode
                os.write(self._fd, b"\033[?2004l")
            except OSError:
                pass
            if self._old_settings is not None:
                try:
                    termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
                except OSError:
                    pass

        def read_key(self, timeout: Optional[float] = None) -> Optional[KeyEvent]:
            """Read a single key press and return the corresponding KeyEvent.

            Args:
                timeout: Maximum seconds to wait. None means block indefinitely.
                         Returns None if no key is available within the timeout.

            Returns:
                A KeyEvent, or None on timeout.
            """
            import select

            if timeout is not None:
                ready, _, _ = select.select([self._fd], [], [], timeout)
                if not ready:
                    return None

            first = os.read(self._fd, 1)
            if not first:
                return None

            b = first[0]

            # Escape — may begin a multi-byte sequence
            if b == 27:
                rest = self._read_escape_sequence_bytes()
                if rest is None:
                    # Standalone ESC
                    return self._byte_to_key_event(27)

                # Bracket paste start: ESC [200~ — check raw bytes before decoding
                # to avoid splitting multi-byte UTF-8 characters
                if rest.startswith(b"[200~"):
                    logger.info(
                        "raw_input: PASTE_START detected; tail bytes after marker: {!r}",
                        rest[5:][:64],
                    )
                    pasted = self._read_bracketed_paste(rest[5:])
                    logger.info(
                        "raw_input: bracketed paste complete — {} chars, repr={!r}",
                        len(pasted),
                        pasted[:80],
                    )
                    return KeyEvent(key="paste", char=pasted)

                seq = rest.decode("utf-8", errors="replace")
                return self._parse_escape_sequence(seq)

            # Multi-byte UTF-8 character (Chinese, etc.)
            if b >= 0x80:
                return self._read_utf8_char(first)

            return self._byte_to_key_event(b)

        def _read_byte_with_timeout(self, timeout: float) -> bytes:
            """Read available bytes after waiting up to timeout seconds."""
            import select as _select

            ready, _, _ = _select.select([self._fd], [], [], timeout)
            if not ready:
                return b""
            try:
                return os.read(self._fd, 1)
            except OSError:
                return b""

        @staticmethod
        def _is_csi_final_byte(value: int) -> bool:
            """Return whether value is a CSI final byte."""
            return 0x40 <= value <= 0x7E

        def _read_escape_sequence_bytes(self) -> bytes | None:
            """Read bytes following ESC without mistaking slow sequences for ESC."""
            first = self._read_byte_with_timeout(_ESC_INITIAL_TIMEOUT)
            if not first:
                return None

            # ESC [ and ESC O are terminal sequence initiators; if they time
            # out incomplete, parse them as unknown rather than Alt+[ or Alt+O.
            if first.startswith(b"["):
                buf = first
                while len(buf) < _ESC_MAX_SEQUENCE_BYTES and not any(
                    self._is_csi_final_byte(value) for value in buf[1:]
                ):
                    chunk = self._read_byte_with_timeout(_ESC_CONTINUATION_TIMEOUT)
                    if not chunk:
                        break
                    buf += chunk
                return buf[:_ESC_MAX_SEQUENCE_BYTES]

            if first.startswith(b"O"):
                if len(first) > 1:
                    return first
                second = self._read_byte_with_timeout(_ESC_CONTINUATION_TIMEOUT)
                if second:
                    return first + second
                return first

            return first

        def _read_bracketed_paste(self, initial: bytes) -> str:
            """Read pasted content until the bracket paste end sequence ESC [201~.

            Works entirely with raw bytes to avoid splitting multi-byte UTF-8
            characters during intermediate reads, and only decodes once all
            content has been collected.

            Args:
                initial: Any leftover bytes already read after the start marker.

            Returns:
                The pasted text with the end marker stripped.
            """
            import select as _select

            buf = initial
            end_marker = b"\033[201~"

            while end_marker not in buf:
                ready, _, _ = _select.select([self._fd], [], [], 1.0)
                if not ready:
                    break
                chunk = os.read(self._fd, 4096)
                if not chunk:
                    break
                buf += chunk

            idx = buf.find(end_marker)
            if idx >= 0:
                buf = buf[:idx]

            text = buf.decode("utf-8", errors="replace")
            # Normalize \r\n and \r to \n
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            return text

        def _read_utf8_char(self, first_byte: bytes) -> KeyEvent:
            """Read remaining bytes of a multi-byte UTF-8 character."""
            b = first_byte[0]
            # Determine expected byte count from leading byte
            if b < 0xC0:
                # Continuation byte alone — shouldn't happen
                return KeyEvent(key="unknown", char="", ctrl=False, alt=False, shift=False)
            elif b < 0xE0:
                remaining = 1
            elif b < 0xF0:
                remaining = 2
            else:
                remaining = 3

            data = first_byte
            for _ in range(remaining):
                extra = os.read(self._fd, 1)
                if not extra:
                    break
                data += extra

            try:
                char = data.decode("utf-8")
            except UnicodeDecodeError:
                return KeyEvent(key="unknown", char="", ctrl=False, alt=False, shift=False)

            return KeyEvent(key=char, char=char, ctrl=False, alt=False, shift=False)

        @staticmethod
        def _byte_to_key_event(b: int) -> KeyEvent:
            """Convert a single byte value to a KeyEvent.

            Args:
                b: Integer byte value (0-255).

            Returns:
                The corresponding KeyEvent.
            """
            if b in (13, 10):
                return KeyEvent(key="enter", char=chr(b))

            if b == 9:
                return KeyEvent(key="tab", char="\t")

            if b == 127:
                return KeyEvent(key="backspace", char=chr(127))

            if b == 27:
                return KeyEvent(key="escape", char="\x1b")

            # Ctrl+a through ctrl+z (bytes 1-26, excluding 9, 10, 13)
            if 1 <= b <= 26:
                letter = chr(ord("a") + b - 1)
                return KeyEvent(key=letter, char=chr(b), ctrl=True)

            # Printable ASCII: 32-126
            if 32 <= b <= 126:
                char = chr(b)
                shift = char.isupper()
                return KeyEvent(key=char, char=char, shift=shift)

            # Fallback
            return KeyEvent(key="unknown", char=chr(b) if b < 256 else "")

        @staticmethod
        def _parse_escape_sequence(seq: str) -> KeyEvent:
            """Parse the bytes following an ESC byte into a KeyEvent.

            Args:
                seq: String of characters that came after the ESC byte.

            Returns:
                The corresponding KeyEvent.
            """
            if seq in _ESCAPE_SEQUENCES:
                return KeyEvent(key=_ESCAPE_SEQUENCES[seq], char="")

            modified_key = RawInputCapture._parse_modified_key_sequence(seq)
            if modified_key is not None:
                return modified_key

            # SGR mouse event — only wheel up/down are useful here.  The
            # ``rest`` buffer may contain multiple back-to-back wheel events
            # when the user spins the wheel quickly; ``re.match`` picks up
            # the first one and the trailing bytes are dropped (each tick
            # is small, losing a few during a fast spin is fine).
            m = _MOUSE_SGR_RE.match(seq)
            if m is not None:
                button = int(m.group(1))
                if button == 64:
                    return KeyEvent(key="wheel_up", char="")
                if button == 65:
                    return KeyEvent(key="wheel_down", char="")
                # Other mouse events (clicks, motion) — pass through as a
                # generic ``mouse`` event so callers can ignore them.
                return KeyEvent(key="mouse", char="")

            if seq.startswith("["):
                return KeyEvent(key="unknown", char="")

            # Single printable char → alt+char
            if len(seq) == 1 and 32 <= ord(seq) <= 126:
                return KeyEvent(key=seq, char=seq, alt=True)

            return KeyEvent(key="unknown", char="")

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
