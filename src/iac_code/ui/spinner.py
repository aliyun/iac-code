"""Spinner for Rich Live — single warm color.

Pure data renderer — no timers. The caller controls refresh rate by
calling render() in a loop.
"""

from __future__ import annotations

import random
import time

from rich.text import Text

# Animation frame sequences
SPINNER_DOTS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Spinner color — warm orange
SPINNER_COLOR = "rgb(215,119,87)"

# Frame interval for spinner character rotation
_FRAME_INTERVAL = 0.08

# Verbs displayed in spinner while processing (present participle)
# These are i18n keys — call _() on them before display.
SPINNER_VERBS = [
    "Thinking",
    "Processing",
    "Working",
]

# Past-tense verbs for turn completion messages (works with "for Xs")
# These are i18n keys — call _() on them before display.
COMPLETION_VERBS = [
    "Thought",
    "Processed",
    "Worked",
]


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def random_spinner_verb() -> str:
    """Pick a random translated spinner verb for in-progress display."""
    from iac_code.i18n import _

    # NOTE: explicit _() calls for pybabel extraction
    _("Thinking")
    _("Processing")
    _("Working")
    return _(random.choice(SPINNER_VERBS))


def random_completion_verb() -> str:
    """Pick a random translated past-tense verb for completion display."""
    from iac_code.i18n import _

    # NOTE: explicit _() calls for pybabel extraction
    _("Thought")
    _("Processed")
    _("Worked")
    return _(random.choice(COMPLETION_VERBS))


class ShimmerSpinner:
    """Animated spinner with warm single-color style.

    Usage::

        spinner = ShimmerSpinner()
        with Live(spinner.render(), console=console, refresh_per_second=20) as live:
            while working:
                live.update(spinner.render())
    """

    def __init__(self, status: str | None = None) -> None:
        self._status = status or (random_spinner_verb() + "...")
        self._start_time = time.monotonic()

    @property
    def elapsed(self) -> float:
        """Seconds since this spinner was created."""
        return time.monotonic() - self._start_time

    def render(self) -> Text:
        """Produce a single frame as a Rich Text object."""
        now = time.monotonic()
        elapsed = now - self._start_time

        # Spinner character (rotates based on wall clock)
        frame_idx = int(now / _FRAME_INTERVAL) % len(SPINNER_DOTS)
        frame = SPINNER_DOTS[frame_idx]

        text = Text()

        # Spinner character + status in warm orange
        style = f"bold {SPINNER_COLOR}"
        text.append(f"{frame} ", style=style)
        text.append(self._status, style=style)

        # Elapsed time in dim
        text.append(f" ({_format_elapsed(elapsed)})", style="dim")

        return text

    def update_status(self, status: str) -> None:
        self._status = status
