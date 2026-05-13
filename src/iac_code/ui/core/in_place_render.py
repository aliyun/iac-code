"""In-place renderer for full-screen pickers and dialogs.

Renders a Rich renderable at the cursor's current position in the main
buffer (not the alternate screen), erasing the previous frame before
drawing each new one. Same teardown pattern as
``Renderer._quiet_stop_live`` — emits zero newlines past the bottom of
the render so nothing leaks into scrollback.

Why not the alternate screen / Rich ``Live(transient=True)``: both leak
the rendered frames into the main buffer's scrollback on some terminals,
which makes ``↑`` after a picker show every frame instead of pre-picker
history.

Why not bare ``console.print`` in a loop: that appends each frame below
the previous one (no erase), and under ``RawInputCapture`` (raw mode,
OPOST off) the kernel TTY no longer maps ``\\n`` → ``\\r\\n``, so each
line stair-steps right of where the previous one ended.
"""

from __future__ import annotations

from rich.console import Console, RenderableType


class InPlaceRenderer:
    """Erase-and-redraw renderer for picker / dialog event loops.

    Each :meth:`render` call rewinds over the previous frame using
    ``CR + erase-line + (UP + erase-line) × (h-1)``, then writes the new
    content. :meth:`clear` runs the same erase to wipe the last frame on
    exit. Safe under raw mode: bare ``\\n`` in Rich's output is
    translated to ``\\r\\n`` so each line returns to column 0.

    Optional ``cursor_to`` lets callers park the hardware cursor inside
    the frame (e.g. inside a search box) after drawing — the renderer
    walks the cursor back to the last row before the next erase, so the
    erase math stays correct.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._last_height = 0
        # Where the cursor currently sits within the last rendered frame
        # (0-indexed from the top). After a plain :meth:`render` the
        # cursor is on the bottom row; if ``cursor_to`` was passed, it
        # may be parked higher up.
        self._cursor_row = 0

    @property
    def last_height(self) -> int:
        return self._last_height

    def render(
        self,
        renderable: RenderableType,
        cursor_to: tuple[int, int] | None = None,
    ) -> None:
        """Erase the previous frame (if any) and draw the new one.

        Args:
            renderable: Rich renderable to draw.
            cursor_to: Optional ``(row, col)`` offset (both 0-indexed,
                relative to the top-left of the rendered frame) where the
                terminal cursor should land after drawing. Useful for
                pickers that want the hardware cursor to sit inside their
                search box rather than at the bottom of the frame.
        """
        with self._console.capture() as capture:
            self._console.print(renderable)
        text = capture.get().replace("\r\n", "\n").replace("\n", "\r\n")
        lines = text.split("\r\n")
        if lines and lines[-1] == "":
            lines.pop()

        out = self._console.file
        if self._last_height > 0:
            self._erase_previous(out)
        if lines:
            out.write("\r\n".join(lines))
        new_height = len(lines)

        # Cursor is now on the last drawn row; place it elsewhere only if
        # the caller asked for it.
        self._cursor_row = max(0, new_height - 1)
        if cursor_to is not None and new_height > 0:
            target_row, target_col = cursor_to
            target_row = max(0, min(target_row, new_height - 1))
            target_col = max(0, target_col)
            out.write("\r")
            rows_up = (new_height - 1) - target_row
            if rows_up > 0:
                out.write(f"\x1b[{rows_up}A")
            if target_col > 0:
                out.write(f"\x1b[{target_col}C")
            self._cursor_row = target_row

        out.flush()
        self._last_height = new_height

    def clear(self) -> None:
        """Erase the last rendered frame.

        Idempotent — calling :meth:`clear` twice is a no-op.
        """
        if self._last_height <= 0:
            return
        out = self._console.file
        try:
            self._erase_previous(out)
            out.flush()
        except OSError:
            pass
        self._last_height = 0
        self._cursor_row = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _erase_previous(self, out) -> None:
        """Walk the cursor back to the last row of the previous frame
        (if it was parked higher by ``cursor_to``) and erase every row.
        """
        rows_down = (self._last_height - 1) - self._cursor_row
        if rows_down > 0:
            out.write(f"\x1b[{rows_down}B")
        out.write("\r\x1b[2K")
        for _ in range(self._last_height - 1):
            out.write("\x1b[A\x1b[2K")
