"""StatusIcon component with coloured status symbols."""

from __future__ import annotations

from enum import Enum

from rich.text import Text


class Status(Enum):
    """Enumeration of supported status values."""

    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    PENDING = "pending"
    RUNNING = "running"


_ICONS: dict[Status, tuple[str, str]] = {
    Status.SUCCESS: ("✓", "green"),
    Status.ERROR: ("✗", "red"),
    Status.WARNING: ("⚠", "yellow"),
    Status.INFO: ("●", "blue"),
    Status.PENDING: ("○", "dim"),
    Status.RUNNING: ("◐", "blue"),
}


class StatusIcon:
    """Renders a coloured status icon as Rich Text."""

    def __init__(self, status: Status) -> None:
        self.status = status

    def render(self) -> Text:
        """Return the status icon as Rich Text."""
        icon, style = _ICONS[self.status]
        text = Text()
        text.append(icon, style=style)
        return text
