"""Key event type definitions for terminal input handling."""

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyEvent:
    """Represents a single key press event from the terminal.

    Attributes:
        key: Normalized key name (e.g. "a", "up", "enter", "f1").
        char: Raw character string associated with the key press.
        ctrl: True if the Ctrl modifier was held.
        alt: True if the Alt/Meta modifier was held.
        shift: True if the Shift modifier was held.
    """

    key: str
    char: str
    ctrl: bool = False
    alt: bool = False
    shift: bool = False

    @property
    def key_id(self) -> str:
        """Return a normalized string identifier for this key event.

        Format: [ctrl+][alt+]<key>
        Shift is NOT included as a prefix for printable characters because
        the character itself (e.g. "A") already reflects the shift state.

        Examples:
            "a", "ctrl+r", "alt+p", "ctrl+alt+x", "up", "enter"
        """
        parts: list[str] = []
        if self.ctrl:
            parts.append("ctrl")
        if self.alt:
            parts.append("alt")
        parts.append(self.key)
        return "+".join(parts)
