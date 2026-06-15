"""Spinner for Rich Live — single warm color.

Pure data renderer — no timers. The caller controls refresh rate by
calling render() in a loop.
"""

from __future__ import annotations

import random
import time

from rich.text import Text

from iac_code.utils.console import use_ascii_symbols

# Animation frame sequences
SPINNER_DOTS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SPINNER_ASCII = ["-", "\\", "|", "/"]

# Spinner color — warm orange
SPINNER_COLOR = "rgb(215,119,87)"

# Frame interval for spinner character rotation
_FRAME_INTERVAL = 0.08

# Verbs displayed in spinner while processing (present participle)
# These are i18n keys — call _() on them before display.
SPINNER_VERBS = [
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


def spinner_frames() -> list[str]:
    """Return the active spinner frame sequence for the current console."""
    return SPINNER_ASCII if use_ascii_symbols() else SPINNER_DOTS


def current_spinner_frame(now: float | None = None) -> str:
    """Return the spinner frame for *now* using the shared frame interval."""
    frame_time = time.monotonic() if now is None else now
    frames = spinner_frames()
    frame_idx = int(frame_time / _FRAME_INTERVAL) % len(frames)
    return frames[frame_idx]


def random_spinner_verb() -> str:
    """Pick a random translated spinner verb for in-progress display."""
    from iac_code.i18n import _

    # NOTE: explicit _() calls for pybabel extraction
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

    def frame(self, now: float | None = None) -> str:
        """Return the current spinner frame."""
        return current_spinner_frame(now)

    def render(self) -> Text:
        """Produce a single frame as a Rich Text object."""
        now = time.monotonic()
        elapsed = now - self._start_time

        frame = self.frame(now)

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
