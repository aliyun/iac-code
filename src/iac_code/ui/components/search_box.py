"""Single-line text input component with editing operations."""

from __future__ import annotations

from typing import Callable

from rich.text import Text

from iac_code.ui.core.key_event import KeyEvent


class SearchBox:
    """A single-line text input component with cursor and editing operations.

    Editing operations supported:
        - Printable character insertion
        - Backspace (delete before cursor), Delete (delete after cursor)
        - Left / Right cursor movement
        - Home / Ctrl+A (move to start), End / Ctrl+E (move to end)
        - Ctrl+K (delete to end of line)
        - Ctrl+U (delete to start of line)
        - Ctrl+W (delete previous word)
    """

    def __init__(
        self,
        placeholder: str = "",
        initial_value: str = "",
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self._placeholder = placeholder
        self._text: list[str] = list(initial_value)
        self._cursor: int = len(self._text)
        self._on_change = on_change

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def value(self) -> str:
        return "".join(self._text)

    @property
    def cursor(self) -> int:
        return self._cursor

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def handle_key(self, key_event: KeyEvent) -> bool:
        """Handle a key event.  Returns True if consumed, False otherwise."""
        key = key_event.key
        ctrl = key_event.ctrl

        old_value = self.value

        # --- Navigation (no text change) ---
        if key == "left":
            if self._cursor > 0:
                self._cursor -= 1
            return True

        if key == "right":
            if self._cursor < len(self._text):
                self._cursor += 1
            return True

        if key == "home" or (ctrl and key == "a"):
            self._cursor = 0
            return True

        if key == "end" or (ctrl and key == "e"):
            self._cursor = len(self._text)
            return True

        # --- Deletion ---
        if key == "backspace":
            if self._cursor > 0:
                self._cursor -= 1
                self._text.pop(self._cursor)
            self._notify(old_value)
            return True

        if key == "delete":
            if self._cursor < len(self._text):
                self._text.pop(self._cursor)
            self._notify(old_value)
            return True

        if ctrl and key == "k":
            del self._text[self._cursor :]
            self._notify(old_value)
            return True

        if ctrl and key == "u":
            del self._text[: self._cursor]
            self._cursor = 0
            self._notify(old_value)
            return True

        if ctrl and key == "w":
            # Delete backwards one "token": either a run of spaces or a run of
            # non-space characters immediately before the cursor.
            pos = self._cursor
            if pos > 0 and self._text[pos - 1] == " ":
                # preceding char is a space: delete the run of spaces
                while pos > 0 and self._text[pos - 1] == " ":
                    pos -= 1
            else:
                # preceding char is a non-space: delete the word
                while pos > 0 and self._text[pos - 1] != " ":
                    pos -= 1
            del self._text[pos : self._cursor]
            self._cursor = pos
            self._notify(old_value)
            return True

        # --- Character insertion ---
        # Only handle printable characters (single char, no ctrl modifier)
        char = key_event.char
        if not ctrl and len(char) == 1 and char.isprintable():
            self._text.insert(self._cursor, char)
            self._cursor += 1
            self._notify(old_value)
            return True

        return False

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        """Render the search box as a Rich Text object.

        Format: "> text_" where _ represents the cursor position.
        """
        text = Text()
        text.append("> ", style="bold cyan")

        value = self.value
        if not value and self._placeholder:
            text.append(self._placeholder, style="dim")
        else:
            before = value[: self._cursor]
            at = value[self._cursor] if self._cursor < len(value) else " "
            after = value[self._cursor + 1 :] if self._cursor < len(value) else ""
            text.append(before)
            text.append(at, style="reverse")
            text.append(after)

        return text

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _notify(self, old_value: str) -> None:
        """Call on_change callback if value changed."""
        if self._on_change is not None:
            new_value = self.value
            if new_value != old_value:
                self._on_change(new_value)
