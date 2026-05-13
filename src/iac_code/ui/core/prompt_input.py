"""Main REPL input component replacing prompt-toolkit's PromptSession."""

from __future__ import annotations

import shutil
import sys
import unicodedata
from typing import TYPE_CHECKING, Optional

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
    ) -> None:
        self._km = keybinding_manager
        self._aggregator = suggestion_aggregator
        self._history = history
        self._console = console

        # Buffer and cursor
        self._buffer: list[str] = []
        self._cursor: int = 0

        # Control flags
        self._submitted: bool = False
        self._cancelled: bool = False
        self._esc_pressed: bool = False
        self._text_changed: bool = False  # set when buffer content changes
        self._pending_action: "Callable[[], None] | None" = None

        # Rendering state
        self._prompt: str = ""
        self._prev_suggestion_lines: int = 0  # how many suggestion lines were rendered last frame
        self._prev_content_extra_lines: int = 0  # extra lines beyond the first (for multi-line text)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def schedule_action(self, action: "Callable[[], None]") -> None:
        """Schedule an action to run outside of raw mode, then resume input."""
        self._pending_action = action

    def _get_text(self) -> str:
        """Return current buffer contents as a string."""
        return "".join(self._buffer)

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def _handle_key(self, key_event: KeyEvent) -> None:
        """Process a single key event and update internal state."""
        key = key_event.key
        ctrl = key_event.ctrl

        # 0. Bracket paste → insert all content (including newlines) into buffer
        if key == "paste":
            self._insert(key_event.char)
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

        # 4. Enter — accept suggestion and submit immediately
        if key == "enter":
            if self._aggregator and self._aggregator.suggestions:
                result = self._aggregator.accept_selected()
                if result is not None:
                    completion, start, end = result
                    self._apply_completion(completion, start, end)
            self._submitted = True
            return

        # 5. Tab → accept ghost text
        if key == "tab":
            if self._aggregator:
                result = self._aggregator.accept_ghost_text()
                if result is not None:
                    completion, start, end = result
                    self._apply_completion(completion, start, end)
                    return
            return

        # 6. KeybindingManager resolution (Ctrl+R, Ctrl+P, etc.)
        if self._km.resolve(key_event):
            return

        # 7. Up/Down with active suggestions → move selection
        if self._aggregator and self._aggregator.suggestions:
            if key == "up" or (ctrl and key == "p"):
                self._aggregator.move_selection(-1)
                return
            if key == "down" or (ctrl and key == "n"):
                self._aggregator.move_selection(1)
                return

        # 8. Up/Down with history (no active suggestions)
        if self._history:
            if key == "up":
                entry = self._history.navigate(-1, self._get_text())
                if entry is not None:
                    self._set_text(entry)
                return
            if key == "down":
                entry = self._history.navigate(1)
                if entry is None:
                    self._set_text("")
                else:
                    self._set_text(entry)
                return

        # 9. Line editing
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

        # 10. Printable character insertion
        char = key_event.char
        if char and char.isprintable():
            self._insert(char)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _insert(self, text: str) -> None:
        """Insert *text* at the current cursor position."""
        for ch in text:
            self._buffer.insert(self._cursor, ch)
            self._cursor += 1
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

    def _render(self) -> None:
        """Re-render the input line, ghost text, and suggestion overlay."""
        out = sys.stdout
        text = self._get_text()
        lines = text.split("\n")
        content_extra_lines = len(lines) - 1
        cols = shutil.get_terminal_size().columns

        # Move cursor up to the prompt line (first content line)
        if self._prev_content_extra_lines > 0:
            out.write(f"\033[{self._prev_content_extra_lines}A")

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
        out.write(lines[0])

        # Render continuation lines
        for i in range(1, len(lines)):
            out.write(f"\n\r{lines[i]}")

        # Ghost text (only for single-line input)
        ghost = ""
        if not content_extra_lines and self._aggregator:
            ghost = self._aggregator.ghost_text
        if ghost:
            out.write(f"{_COLOR_GHOST}{ghost}{_COLOR_RESET}")

        # Position cursor: find which line and column the cursor maps to
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

        # Terminal cursor is currently at end of the last content line (+ ghost).
        # Move up to cursor_line.
        lines_up = content_extra_lines - cursor_line
        if lines_up > 0:
            out.write(f"\033[{lines_up}A")

        # Move to correct column
        target_col = _display_width(lines[cursor_line][:cursor_col])
        if cursor_line == 0:
            target_col += _display_width(self._prompt)
        out.write("\r")
        if target_col > 0:
            out.write(f"\033[{target_col}C")

        # Render suggestion overlay below all content lines
        suggestion_lines = 0
        if self._aggregator and self._aggregator.suggestions:
            visible = self._aggregator.visible_suggestions
            selected = self._aggregator.visible_selected_index

            max_name_w = max(len(s.display_text) for s in visible)
            name_col_w = min(max_name_w + 3, int(cols * 0.4))

            total_new_suggestions = len(visible) + 1  # items + hint bar

            # Move from cursor position to after last content line
            lines_to_bottom = content_extra_lines - cursor_line
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
            if target_col > 0:
                out.write(f"\033[{target_col}C")

        self._prev_content_extra_lines = content_extra_lines
        self._prev_suggestion_lines = suggestion_lines
        out.flush()

    def _clear_suggestions(self) -> None:
        """Clear any rendered suggestion lines below input."""
        if self._prev_suggestion_lines > 0:
            out = sys.stdout
            out.write("\033[s")  # save cursor
            # Move to last content line first
            if self._prev_content_extra_lines > 0:
                out.write(f"\033[{self._prev_content_extra_lines}B")
            for _ in range(self._prev_suggestion_lines):
                out.write("\n\033[2K")
            out.write("\033[u")  # restore cursor
            out.flush()
            self._prev_suggestion_lines = 0

    # ------------------------------------------------------------------
    # Public async entry-point
    # ------------------------------------------------------------------

    async def get_input(self, prompt: str = "❯ ") -> Optional[str]:
        """Prompt the user for input and return it.

        Runs the blocking input loop directly in the main thread because
        termios operations on stdin require the main thread on macOS.
        This blocks the event loop while waiting for input, which is
        acceptable for a REPL — we must wait for user input before proceeding.

        Returns the entered string, or None if the user pressed Ctrl+C or
        Ctrl+D.
        """
        return self._input_loop(prompt)

    def _input_loop(self, prompt: str) -> Optional[str]:
        """Blocking input loop with inline rendering."""
        from iac_code.ui.core.raw_input import RawInputCapture

        # Reset state
        self._buffer = []
        self._cursor = 0
        self._submitted = False
        self._cancelled = False
        self._esc_pressed = False
        self._text_changed = False
        self._pending_action = None
        self._prompt = prompt
        self._prev_suggestion_lines = 0
        self._prev_content_extra_lines = 0

        # Initial render (just prompt)
        sys.stdout.write(f"{_COLOR_BOLD}{_COLOR_CYAN}{prompt}{_COLOR_RESET}")
        sys.stdout.flush()

        while not self._submitted and not self._cancelled:
            with RawInputCapture() as cap:
                while not self._submitted and not self._cancelled and self._pending_action is None:
                    event = cap.read_key()
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
                # Clear prompt line so action output starts on a clean line
                sys.stdout.write("\r\x1b[K")
                sys.stdout.flush()
                action()
                # Re-render prompt after action output
                sys.stdout.write(f"{_COLOR_BOLD}{_COLOR_CYAN}{self._prompt}{_COLOR_RESET}")
                sys.stdout.write(self._get_text())
                sys.stdout.flush()
                self._prev_content_extra_lines = 0
                self._prev_suggestion_lines = 0

        # Clear suggestion overlay before returning
        self._clear_suggestions()

        # Re-render submitted content with background highlight
        if self._submitted:
            text = self._get_text()
            lines = text.split("\n")
            term_width = shutil.get_terminal_size().columns
            _bg = "\033[48;5;236m"

            # Move cursor up to prompt line if multi-line
            if self._prev_content_extra_lines > 0:
                sys.stdout.write(f"\033[{self._prev_content_extra_lines}A")

            # Render first line with prompt
            first_content = f"{prompt}{lines[0]}"
            pad = max(0, term_width - _display_width(first_content))
            sys.stdout.write(
                f"\r{_bg}{_COLOR_BOLD}{_COLOR_CYAN}{prompt}{_COLOR_RESET}{_bg}{lines[0]}{' ' * pad}{_COLOR_RESET}"
            )

            # Render continuation lines
            for i in range(1, len(lines)):
                pad = max(0, term_width - _display_width(lines[i]))
                sys.stdout.write(f"\n\r{_bg}{lines[i]}{' ' * pad}{_COLOR_RESET}")

        sys.stdout.write("\n")
        sys.stdout.flush()

        if self._cancelled:
            return None
        return self._get_text()
