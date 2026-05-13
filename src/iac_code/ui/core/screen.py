"""Screen management using the alternate screen buffer."""

from __future__ import annotations

from typing import Any

from rich.console import Console


class ScreenManager:
    """Wraps a Rich Console to provide alternate-screen and clear/render helpers.

    The alternate screen buffer allows the UI to restore the terminal to its
    previous state when the application exits.
    """

    def __init__(self, console: Console) -> None:
        self._console = console

    def enter_alternate_screen(self) -> None:
        """Switch to the terminal alternate screen buffer and move cursor home."""
        self._console.file.write("\033[?1049h")
        self._console.file.write("\033[H")
        self._console.file.flush()

    def leave_alternate_screen(self) -> None:
        """Switch back from the alternate screen buffer to the normal screen."""
        self._console.file.write("\033[?1049l")
        self._console.file.flush()

    def enable_mouse_tracking(self) -> None:
        """Turn on basic + SGR-encoded mouse event reporting.

        ``?1000h`` enables button (incl. wheel) press/release events;
        ``?1006h`` switches the encoding to the SGR form
        ``\\x1b[<button;col;row{M|m}`` so coordinates aren't bounded by
        a single byte and event parsing is unambiguous.  Used by the
        ``/resume`` preview to translate scroll-wheel ticks into
        ``wheel_up``/``wheel_down`` ``KeyEvent``s while the alternate
        screen is active.
        """
        self._console.file.write("\033[?1000h\033[?1006h")
        self._console.file.flush()

    def disable_mouse_tracking(self) -> None:
        """Restore the terminal's pre-tracking mouse settings."""
        self._console.file.write("\033[?1006l\033[?1000l")
        self._console.file.flush()

    def clear(self) -> None:
        """Move cursor to top-left and erase the entire screen."""
        self._console.file.write("\033[H\033[2J")
        self._console.file.flush()

    def clear_and_render(self, renderable: Any) -> None:
        """Clear the screen and then render a Rich renderable.

        Captures Rich's output and rewrites bare ``\\n`` as ``\\r\\n`` so the
        render is correct under raw mode too — :class:`RawInputCapture`
        clears OPOST, which would otherwise leave each line indented to the
        end column of the previous one.

        Args:
            renderable: Any object that Rich Console can print.
        """
        with self._console.capture() as capture:
            self._console.print(renderable)
        text = capture.get().replace("\r\n", "\n").replace("\n", "\r\n")
        self._console.file.write("\033[H\033[2J")
        self._console.file.write(text)
        self._console.file.flush()

    def get_size(self) -> tuple[int, int]:
        """Return the current terminal dimensions.

        Returns:
            A tuple of (cols, rows).
        """
        size = self._console.size
        return size.width, size.height
