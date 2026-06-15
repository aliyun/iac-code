"""Main REPL input component replacing prompt-toolkit's PromptSession."""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from loguru import logger

from iac_code.ui.core.key_event import KeyEvent


def _display_width(s: str) -> int:
    """Return the terminal display width of a string.

    East Asian wide/fullwidth characters occupy 2 columns.
    """
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


if TYPE_CHECKING:
    from typing import Callable

    from iac_code.ui.core.input_history import InputHistory
    from iac_code.ui.keybindings.manager import KeybindingManager
    from iac_code.ui.suggestions.aggregator import SuggestionAggregator
    from iac_code.utils.image.pasted_content import PastedContent
    from iac_code.utils.image.store import ImageStore


@dataclass
class PromptInputResult:
    text: str
    pasted_contents: dict[int, "PastedContent"] = field(default_factory=dict)


# ANSI escape helpers
_COLOR_SELECTED = "\033[96m"  # bright_cyan — matches logo accent color
_COLOR_DIM = "\033[38;2;128;128;128m"  # gray (#808080)
_COLOR_GHOST = "\033[2m"  # dim
_COLOR_RESET = "\033[0m"
_COLOR_BOLD = "\033[1m"
_COLOR_CYAN = "\033[36m"


class PromptInput:
    """Interactive line-editor with inline rendering, ghost text, and suggestions.

    The public entry-point is :meth:`get_input`, which runs a blocking
    input loop in a thread executor so it does not block the asyncio event
    loop.  Individual key handling is exposed via :meth:`_handle_key` so
    that tests can drive the component without a real terminal.
    """

    def __init__(
        self,
        keybinding_manager: "KeybindingManager",
        suggestion_aggregator: "SuggestionAggregator | None" = None,
        history: "InputHistory | None" = None,
        console=None,
        paste_handler: "Callable[[str], bool] | None" = None,
        image_store: "ImageStore | None" = None,
    ) -> None:
        self._km = keybinding_manager
        self._aggregator = suggestion_aggregator
        self._history = history
        self._console = console
        # Optional bracketed-paste hook: called with the pasted text. Returning
        # True signals the handler attached the content out-of-band (e.g. as an
        # image placeholder) and the buffer must NOT also receive the text.
        # Returning False keeps default behaviour: insert the text verbatim.
        self._paste_handler = paste_handler
        self._image_store = image_store

        # Buffer and cursor
        self._buffer: list[str] = []
        self._cursor: int = 0

        # Pasted contents (e.g. images) tracked alongside the text buffer
        self._pasted_contents: dict[int, "PastedContent"] = {}
        self._next_paste_id: int = 1

        # Control flags
        self._submitted: bool = False
        self._cancelled: bool = False
        self._esc_pressed: bool = False
        self._text_changed: bool = False  # set when buffer content changes
        self._pending_action: "Callable[[], None] | None" = None
        self._clipboard_has_image: bool = False  # True when clipboard contains an image

        # Rendering state
        self._prompt: str = ""
        self._prev_suggestion_lines: int = 0  # how many suggestion lines were rendered last frame
        self._prev_content_extra_lines: int = 0  # physical extra lines beyond first (incl. wrapping)
        self._prev_cursor_physical_row: int = 0  # physical row the cursor was placed on (from row 0)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def schedule_action(self, action: "Callable[[], None]") -> None:
        """Schedule an action to run outside of raw mode, then resume input."""
        self._pending_action = action

    def _get_text(self) -> str:
        """Return current buffer contents as a string."""
        return "".join(self._buffer)

    def insert_text(self, text: str) -> None:
        """Insert text at the current cursor position."""
        if not text:
            return
        self._insert(text)

    def attach_image(self, pc: "PastedContent") -> None:
        """Insert ``[Image #N]`` at the cursor and track the paste.

        Idempotent: re-attaching the same id is a no-op.
        """
        if pc.id in self._pasted_contents:
            return
        self._pasted_contents[pc.id] = pc
        placeholder = f"[Image #{pc.id}]"
        self._insert(placeholder)
        if pc.id >= self._next_paste_id:
            self._next_paste_id = pc.id + 1

    def next_paste_id(self) -> int:
        """Return a fresh, monotonically increasing paste id."""
        pid = self._next_paste_id
        self._next_paste_id += 1
        return pid

    def make_result(self) -> "PromptInputResult":
        """Build a snapshot of the current text + tracked pasted contents."""
        return PromptInputResult(
            text=self._get_text(),
            pasted_contents=dict(self._pasted_contents),
        )

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def _handle_key(self, key_event: KeyEvent) -> None:
        """Process a single key event and update internal state."""
        key = key_event.key
        ctrl = key_event.ctrl

        # Diagnostics for the image-paste path. Logged at INFO so the user
        # can correlate keystrokes with downstream pipeline events.
        if key == "paste":
            logger.info("prompt_input: bracketed paste event ({} chars)", len(key_event.char))
        elif ctrl and key == "v":
            logger.info("prompt_input: Ctrl+V keystroke received")

        # 0. Bracket paste → optionally route through paste_handler first, then
        # insert. The handler can attach images discovered in the system
        # clipboard and signal "consumed" to suppress the text insert.
        if key == "paste":
            consumed = False
            if self._paste_handler is not None:
                try:
                    consumed = self._paste_handler(key_event.char)
                except Exception:
                    # Handler errors must never deadlock the input loop —
                    # fall through to plain-text insert.
                    consumed = False
            if not consumed:
                self._insert(key_event.char)
            return

        # 0b. Focus events — probe clipboard for image on focus-in
        if key == "focus_in":
            self._check_clipboard_for_image()
            return

        if key == "focus_out":
            if self._clipboard_has_image:
                self._clipboard_has_image = False
                self._render()
            return

        # 1. Esc+Enter → insert newline
        if self._esc_pressed:
            self._esc_pressed = False
            if key == "enter":
                self._insert("\n")
                return

        # 2. Escape alone → set flag; resolve through KeybindingManager
        if key == "escape":
            self._esc_pressed = True
            if self._aggregator and self._aggregator.suggestions:
                self._aggregator.dismiss()
            else:
                self._km.resolve(key_event)
            return

        # 3. Ctrl+C → clear buffer if non-empty, otherwise cancel
        if ctrl and key == "c":
            if self._buffer:
                self._buffer.clear()
                self._cursor = 0
                self._text_changed = True
            else:
                self._cancelled = True
            return

        # 4. Shift+Enter → insert newline when the terminal reports Shift.
        if key == "enter" and key_event.shift:
            self._insert("\n")
            return

        # 5. Enter — accept suggestion and submit immediately
        if key == "enter":
            if self._aggregator and self._aggregator.suggestions:
                result = self._aggregator.accept_selected()
                if result is not None:
                    completion, start, end = result
                    self._apply_completion(completion, start, end)
            self._submitted = True
            return

        # 6. Tab → accept ghost text
        if key == "tab":
            if self._aggregator:
                result = self._aggregator.accept_ghost_text()
                if result is not None:
                    completion, start, end = result
                    self._apply_completion(completion, start, end)
                    return
            return

        # 7. KeybindingManager resolution (Ctrl+R, Ctrl+P, etc.)
        if self._km.resolve(key_event):
            return

        # 8. Up/Down with active suggestions → move selection
        #    But if the user is actively navigating history, prioritize history.
        _in_history_nav = self._history and self._history.is_navigating
        if self._aggregator and self._aggregator.suggestions and not _in_history_nav:
            if key == "up" or (ctrl and key == "p"):
                self._aggregator.move_selection(-1)
                return
            if key == "down" or (ctrl and key == "n"):
                self._aggregator.move_selection(1)
                return

        # 9. Up/Down with history
        if self._history:
            if key == "up":
                entry = self._history.navigate(-1, self._get_text())
                if entry is not None:
                    self._set_text(entry)
                return
            if key == "down":
                if not self._history.is_navigating:
                    return
                entry = self._history.navigate(1)
                if entry is None:
                    self._set_text(self._history.saved_input)
                else:
                    self._set_text(entry)
                return

        # 10. Line editing
        if (ctrl and key == "a") or key == "home":
            self._cursor = 0
            return
        if (ctrl and key == "e") or key == "end":
            self._cursor = len(self._buffer)
            return
        if ctrl and key == "k":
            del self._buffer[self._cursor :]
            self._text_changed = True
            return
        if ctrl and key == "u":
            del self._buffer[: self._cursor]
            self._cursor = 0
            self._text_changed = True
            return
        if ctrl and key == "w":
            pos = self._cursor
            while pos > 0 and self._buffer[pos - 1] == " ":
                pos -= 1
            while pos > 0 and self._buffer[pos - 1] != " ":
                pos -= 1
            del self._buffer[pos : self._cursor]
            self._cursor = pos
            self._text_changed = True
            return
        if key == "left":
            if self._cursor > 0:
                self._cursor -= 1
            return
        if key == "right":
            if self._cursor < len(self._buffer):
                self._cursor += 1
            return
        if key == "backspace":
            if self._cursor > 0:
                del self._buffer[self._cursor - 1]
                self._cursor -= 1
                self._text_changed = True
            return
        if key == "delete":
            if self._cursor < len(self._buffer):
                del self._buffer[self._cursor]
                self._text_changed = True
            return

        # 11. Printable character insertion
        char = key_event.char
        if char and char.isprintable():
            # Clear clipboard indicator on visible character input
            if self._clipboard_has_image:
                self._clipboard_has_image = False
            self._insert(char)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_clipboard_for_image(self) -> None:
        """Synchronously probe the system clipboard for an image on focus-in."""
        from iac_code.utils.image.clipboard import has_image_in_clipboard, invalidate_clipboard_cache

        invalidate_clipboard_cache()
        result = has_image_in_clipboard()
        if result != self._clipboard_has_image:
            self._clipboard_has_image = result
            self._render()

    @staticmethod
    def _clipboard_hint_text() -> str:
        """Return the clipboard image hint text."""
        from iac_code.i18n import _

        return _("Image in clipboard \u00b7 ctrl+v to paste")

    def _insert(self, text: str) -> None:
        """Insert *text* at the current cursor position."""
        chars = list(text)
        self._buffer[self._cursor : self._cursor] = chars
        self._cursor += len(chars)
        self._text_changed = True

    def _set_text(self, text: str) -> None:
        """Replace the entire buffer with *text*, cursor at end."""
        self._buffer = list(text)
        self._cursor = len(self._buffer)
        self._text_changed = True

    def _apply_completion(self, completion: str, start: int, end: int) -> None:
        """Replace the token range [start, end) with *completion*."""
        del self._buffer[start:end]
        insert_pos = start
        for ch in completion:
            self._buffer.insert(insert_pos, ch)
            insert_pos += 1
        self._cursor = insert_pos
        self._text_changed = True

    # ------------------------------------------------------------------
    # Suggestion update (sync wrapper for async aggregator)
    # ------------------------------------------------------------------

    def _update_suggestions_sync(self) -> None:
        """Update suggestions based on current buffer content."""
        if not self._aggregator:
            return
        self._aggregator.update(self._get_text(), self._cursor)

    # ------------------------------------------------------------------
    # Inline rendering
    # ------------------------------------------------------------------

    _IMAGE_REF_RE = re.compile(r"\[Image #(\d+)\]")

    def _highlight_image_refs(self, line: str) -> str:
        """Return the line with [Image #N] tracked refs wrapped in cyan and OSC 8 hyperlink."""

        def repl(m: "re.Match[str]") -> str:
            ref_id = int(m.group(1))
            if ref_id in self._pasted_contents:
                # Try to build OSC 8 hyperlink if image_store available
                if self._image_store is not None:
                    image_path = self._image_store.get_path(ref_id)
                    if image_path:
                        file_url = f"file://{image_path}"
                        # OSC 8 format: \033]8;;URL\033\\text\033]8;;\033\\
                        return f"\033]8;;{file_url}\033\\{_COLOR_CYAN}{m.group(0)}{_COLOR_RESET}\033]8;;\033\\"
                # Fallback: color only, no hyperlink
                return f"{_COLOR_CYAN}{m.group(0)}{_COLOR_RESET}"
            return m.group(0)

        return self._IMAGE_REF_RE.sub(repl, line)

    def _render(self) -> None:
        """Re-render the input line, ghost text, and suggestion overlay."""
        out = sys.stdout
        text = self._get_text()
        lines = text.split("\n")
        cols = shutil.get_terminal_size().columns

        # Calculate physical extra lines accounting for terminal wrapping.
        # After writing content of display width w starting from col 0,
        # the cursor ends on physical row (w-1)//cols (0-indexed) for w>0.
        content_extra_physical = 0
        for i, line in enumerate(lines):
            if i > 0:
                content_extra_physical += 1  # \n moves to next row
            line_width = _display_width(line)
            if i == 0:
                line_width += _display_width(self._prompt)
            if line_width > 0:
                content_extra_physical += (line_width - 1) // cols

        # Move cursor up to the prompt line (first content line).
        # The cursor is at _prev_cursor_physical_row from last render.
        if self._prev_cursor_physical_row > 0:
            out.write(f"\033[{self._prev_cursor_physical_row}A")

        # Clear all previous content + suggestion lines from the prompt line down
        total_prev = self._prev_content_extra_lines + self._prev_suggestion_lines
        out.write("\r\033[K")  # clear prompt line
        if total_prev > 0:
            out.write("\033[s")  # save
            for _ in range(total_prev):
                out.write("\033[B\033[2K")
            out.write("\033[u")  # restore

        # Render prompt + first line
        out.write(f"{_COLOR_BOLD}{_COLOR_CYAN}{self._prompt}{_COLOR_RESET}")
        out.write(self._highlight_image_refs(lines[0]))

        # Right-aligned clipboard image indicator (first line only, no wrapping)
        if self._clipboard_has_image and content_extra_physical == 0:
            hint_text = self._clipboard_hint_text()
            hint_width = _display_width(hint_text)
            first_line_width = _display_width(self._prompt) + _display_width(lines[0])
            gap = 2
            available = cols - first_line_width - gap
            if available >= hint_width:
                hint_col = cols - hint_width
                out.write(f"\033[s\033[{hint_col + 1}G\033[2m{hint_text}\033[0m\033[u")

        # Render continuation lines
        for i in range(1, len(lines)):
            out.write(f"\n\r{self._highlight_image_refs(lines[i])}")

        # Ghost text (only for single-line input without wrapping)
        ghost = ""
        if content_extra_physical == 0 and self._aggregator:
            ghost = self._aggregator.ghost_text
        if ghost:
            out.write(f"{_COLOR_GHOST}{ghost}{_COLOR_RESET}")

        # Position cursor: find which logical line and column the cursor maps to
        cursor_line = 0
        cursor_col = 0
        pos = 0
        for i, line in enumerate(lines):
            line_end = pos + len(line)
            if self._cursor <= line_end:
                cursor_line = i
                cursor_col = self._cursor - pos
                break
            pos = line_end + 1  # +1 for the \n
        else:
            cursor_line = len(lines) - 1
            cursor_col = len(lines[-1])

        # Calculate target display column within the logical line
        target_col = _display_width(lines[cursor_line][:cursor_col])
        if cursor_line == 0:
            target_col += _display_width(self._prompt)

        # Calculate cursor's absolute physical row from prompt start
        cursor_physical_row = 0
        for i in range(cursor_line):
            if i > 0:
                cursor_physical_row += 1  # \n
            lw = _display_width(lines[i]) + (_display_width(self._prompt) if i == 0 else 0)
            if lw > 0:
                cursor_physical_row += (lw - 1) // cols
        if cursor_line > 0:
            cursor_physical_row += 1  # \n before cursor's line
        cursor_physical_row += target_col // cols
        cursor_physical_col = target_col % cols

        # Clamp cursor row to content extent (handles pending-wrap edge case:
        # when width is exactly N*cols, terminal is in pending-wrap state on the
        # last row, so position cursor at rightmost column instead of col 0).
        if cursor_physical_row > content_extra_physical:
            cursor_physical_row = content_extra_physical
            cursor_physical_col = cols  # CUF will clamp to right margin

        # Terminal cursor is at end of rendered content (row content_extra_physical).
        # Move up to cursor's physical row.
        lines_up = content_extra_physical - cursor_physical_row
        if lines_up > 0:
            out.write(f"\033[{lines_up}A")

        # Move to correct column
        out.write("\r")
        if cursor_physical_col > 0:
            out.write(f"\033[{cursor_physical_col}C")

        # Render suggestion overlay below all content lines
        suggestion_lines = 0
        if self._aggregator and self._aggregator.suggestions:
            visible = self._aggregator.visible_suggestions
            selected = self._aggregator.visible_selected_index

            max_name_w = max(len(s.display_text) for s in visible)
            name_col_w = min(max_name_w + 3, int(cols * 0.4))

            total_new_suggestions = len(visible) + 1  # items + hint bar

            # Move from cursor position to after last content line
            lines_to_bottom = content_extra_physical - cursor_physical_row
            if lines_to_bottom > 0:
                out.write(f"\033[{lines_to_bottom}B")

            # Pre-allocate space to prevent terminal scroll from corrupting
            # cursor positions. Writing \n at the bottom of the terminal causes
            # scrolling which invalidates save/restore cursor positions.
            for _ in range(total_new_suggestions):
                out.write("\n")
            out.write(f"\033[{total_new_suggestions}A")

            for i, item in enumerate(visible):
                out.write("\n\r\033[K")
                is_sel = i == selected
                padded = item.display_text + " " * max(0, name_col_w - len(item.display_text))
                desc = item.description
                desc_max = cols - name_col_w - 4
                if len(desc) > desc_max:
                    desc = desc[: max(0, desc_max - 1)] + "…"
                color = _COLOR_SELECTED if is_sel else _COLOR_DIM
                out.write(f"  {color}{padded}{desc}{_COLOR_RESET}")
                suggestion_lines += 1

            from iac_code.i18n import _

            out.write("\n\r\033[K")
            nav, confirm, fill, dismiss = _("Navigate"), _("Confirm"), _("Fill"), _("Dismiss")
            scroll_hint = ""
            if self._aggregator.has_more_above:
                scroll_hint += "↑"
            if self._aggregator.has_more_below:
                scroll_hint += "↓"
            if scroll_hint:
                scroll_hint = f" {scroll_hint}"
            out.write(f"  {_COLOR_DIM}↑↓ {nav}{scroll_hint}  Enter {confirm}  Tab {fill}  Esc {dismiss}{_COLOR_RESET}")
            suggestion_lines += 1

            # Move cursor back to its correct position using explicit movement
            # instead of save/restore, which breaks when terminal scrolls
            total_up = lines_to_bottom + suggestion_lines
            if total_up > 0:
                out.write(f"\033[{total_up}A")
            out.write("\r")
            if cursor_physical_col > 0:
                out.write(f"\033[{cursor_physical_col}C")

        self._prev_content_extra_lines = content_extra_physical
        self._prev_cursor_physical_row = cursor_physical_row
        self._prev_suggestion_lines = suggestion_lines
        out.flush()

    def _clear_suggestions(self) -> None:
        """Clear any rendered suggestion lines below input."""
        if self._prev_suggestion_lines > 0:
            out = sys.stdout
            out.write("\033[s")  # save cursor
            # Move from cursor's physical row to end of content
            rows_to_bottom = self._prev_content_extra_lines - self._prev_cursor_physical_row
            if rows_to_bottom > 0:
                out.write(f"\033[{rows_to_bottom}B")
            for _ in range(self._prev_suggestion_lines):
                out.write("\n\033[2K")
            out.write("\033[u")  # restore cursor
            out.flush()
            self._prev_suggestion_lines = 0

    def _render_transient_cleanup(self) -> None:
        """Clear the prompt frame in-place so the caller can render feedback.

        Cursor at entry is at ``_prev_cursor_physical_row`` (the cursor's
        physical row from the last render), NOT at the bottom of content.
        Using ``_prev_content_extra_lines`` here would overshoot when the
        cursor was mid-buffer at submission, getting clamped at row 0 and
        causing ``\\033[J`` to erase content above the prompt frame (U-C1).
        """
        if self._prev_cursor_physical_row > 0:
            sys.stdout.write(f"\033[{self._prev_cursor_physical_row}A")
        sys.stdout.write("\r\033[J")
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Public async entry-point
    # ------------------------------------------------------------------

    async def get_input(
        self,
        prompt: str = "❯ ",
        *,
        initial_text: str = "",
        transient: bool = False,
    ) -> Optional[str]:
        """Prompt the user for input and return it.

        Runs the blocking input loop directly in the main thread because
        termios operations on stdin require the main thread on macOS.
        This blocks the event loop while waiting for input, which is
        acceptable for a REPL — we must wait for user input before proceeding.

        When ``transient`` is true, the prompt + entered text are cleared
        from the screen before returning, so the caller can render its own
        feedback in that space (used by pipeline interrupt input).

        Returns the entered string, or None if the user pressed Ctrl+C or
        Ctrl+D.
        """
        return self._input_loop(prompt, initial_text=initial_text, transient=transient)

    def _input_loop(self, prompt: str, *, initial_text: str = "", transient: bool = False) -> Optional[str]:
        """Blocking input loop with inline rendering."""
        from iac_code.ui.core.raw_input import RawInputCapture

        # Reset state
        self._buffer = list(initial_text)
        self._cursor = len(self._buffer)
        self._pasted_contents = {}
        self._next_paste_id = 1
        self._submitted = False
        self._cancelled = False
        self._esc_pressed = False
        self._text_changed = bool(initial_text)
        self._pending_action = None
        self._clipboard_has_image = False
        self._prompt = prompt
        self._prev_suggestion_lines = 0
        self._prev_content_extra_lines = 0
        self._prev_cursor_physical_row = 0

        # Initial render (just prompt)
        if self._text_changed:
            self._update_suggestions_sync()
            self._text_changed = False
        self._render()

        while not self._submitted and not self._cancelled:
            with RawInputCapture() as cap:
                while not self._submitted and not self._cancelled and self._pending_action is None:
                    try:
                        event = cap.read_key()
                    except KeyboardInterrupt:
                        # On Windows, msvcrt raises KeyboardInterrupt for
                        # Ctrl+C instead of returning a key event. Preserve the
                        # normal Ctrl+C behavior: clear non-empty input first,
                        # otherwise cancel this prompt.
                        if self._buffer:
                            self._buffer.clear()
                            self._cursor = 0
                            self._text_changed = True
                            self._render()
                            continue
                        self._cancelled = True
                        break
                    if event is None:
                        continue
                    self._handle_key(event)
                    if not self._submitted and not self._cancelled and self._pending_action is None:
                        if self._text_changed:
                            self._update_suggestions_sync()
                            self._text_changed = False
                        self._render()

            # Execute pending action outside raw mode (so console.print works)
            if self._pending_action is not None:
                action = self._pending_action
                self._pending_action = None
                # Move to prompt line and clear before action output
                if self._prev_cursor_physical_row > 0:
                    sys.stdout.write(f"\033[{self._prev_cursor_physical_row}A")
                sys.stdout.write("\r\x1b[K")
                sys.stdout.flush()
                action()
                # Re-render prompt after action output
                sys.stdout.write(f"{_COLOR_BOLD}{_COLOR_CYAN}{self._prompt}{_COLOR_RESET}")
                sys.stdout.write(self._get_text())
                sys.stdout.flush()
                self._prev_content_extra_lines = 0
                self._prev_cursor_physical_row = 0
                self._prev_suggestion_lines = 0

        # Clear suggestion overlay before returning
        self._clear_suggestions()

        if transient:
            self._render_transient_cleanup()
        else:
            # Re-render submitted content with background highlight
            if self._submitted:
                text = self._get_text()
                lines = text.split("\n")
                term_width = shutil.get_terminal_size().columns
                _bg = "\033[48;5;236m"

                # Move cursor up to prompt line (from cursor's physical row)
                if self._prev_cursor_physical_row > 0:
                    sys.stdout.write(f"\033[{self._prev_cursor_physical_row}A")

                # Render first line with prompt
                first_content = f"{prompt}{lines[0]}"
                pad = max(0, term_width - _display_width(first_content))
                sys.stdout.write(
                    f"\r{_bg}{_COLOR_BOLD}{_COLOR_CYAN}{prompt}{_COLOR_RESET}"
                    f"{_bg}{self._highlight_image_refs(lines[0])}{' ' * pad}{_COLOR_RESET}"
                )

                # Render continuation lines
                for i in range(1, len(lines)):
                    pad = max(0, term_width - _display_width(lines[i]))
                    sys.stdout.write(f"\n\r{_bg}{self._highlight_image_refs(lines[i])}{' ' * pad}{_COLOR_RESET}")

            sys.stdout.write("\n")
            sys.stdout.flush()

        if self._cancelled:
            return None
        return self._get_text()
