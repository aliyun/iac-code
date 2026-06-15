"""StatusIcon component with coloured status symbols."""

from __future__ import annotations

from enum import Enum

from rich.text import Text

from iac_code.utils.console import use_ascii_symbols


class Status(Enum):
    """Enumeration of supported status values."""

    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    PENDING = "pending"
    RUNNING = "running"


_UNICODE_ICONS: dict[Status, tuple[str, str]] = {
    Status.SUCCESS: ("✓", "green"),
    Status.ERROR: ("✗", "red"),
    Status.WARNING: ("⚠", "yellow"),
    Status.INFO: ("●", "blue"),
    Status.PENDING: ("○", "dim"),
    Status.RUNNING: ("◐", "blue"),
}

_ASCII_ICONS: dict[Status, tuple[str, str]] = {
    Status.SUCCESS: ("OK", "green"),
    Status.ERROR: ("X", "red"),
    Status.WARNING: ("!", "yellow"),
    Status.INFO: ("i", "blue"),
    Status.PENDING: (".", "dim"),
    Status.RUNNING: ("*", "blue"),
}


def status_symbol(status: Status) -> tuple[str, str]:
    """Return the display symbol and style for *status*."""
    icons = _ASCII_ICONS if use_ascii_symbols() else _UNICODE_ICONS
    return icons[status]


class StatusIcon:
    """Renders a coloured status icon as Rich Text."""

    def __init__(self, status: Status) -> None:
        self.status = status

    def render(self) -> Text:
        """Return the status icon as Rich Text."""
        icon, style = status_symbol(self.status)
        text = Text()
        text.append(icon, style=style)
        return text
