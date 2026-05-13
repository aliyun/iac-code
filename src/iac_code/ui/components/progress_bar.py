"""ProgressBar component using Rich Text."""

from __future__ import annotations

from rich.text import Text


class ProgressBar:
    """A simple terminal progress bar rendered as Rich Text.

    Format: "████░░░░ 65%"
    """

    def __init__(
        self,
        total: int = 100,
        completed: int = 0,
        width: int = 40,
        filled_char: str = "█",
        empty_char: str = "░",
        style: str = "blue",
    ) -> None:
        self.total = total
        self._completed = completed
        self.width = width
        self.filled_char = filled_char
        self.empty_char = empty_char
        self.style = style

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self, completed: int) -> None:
        """Set the completed count."""
        self._completed = completed

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        """Render the progress bar as Rich Text."""
        ratio = self._completed / self.total if self.total > 0 else 0.0
        ratio = max(0.0, min(1.0, ratio))
        filled = round(self.width * ratio)
        empty = self.width - filled
        pct = int(ratio * 100)

        text = Text()
        text.append(self.filled_char * filled, style=self.style)
        text.append(self.empty_char * empty, style="dim")
        text.append(f" {pct}%")
        return text
