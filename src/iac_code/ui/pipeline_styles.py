"""Shared Rich styles for pipeline terminal UI."""

from __future__ import annotations

from rich.text import Text

PIPELINE_TITLE_STYLE = "bold #e8f7ff on #12344a"
PIPELINE_STEP_HEADER_STYLE = "bold #eaf6ff on #172434"
PIPELINE_ACTIVE_PROGRESS_STYLE = "bold #e8f7ff on #12344a"
PIPELINE_PANEL_BORDER_STYLE = "#315d7d"


def pipeline_title(text: str) -> Text:
    """Build a pipeline title label using the Slate + Sky palette."""
    title = Text()
    title.append(f" {text} ", style=PIPELINE_TITLE_STYLE)
    return title


def pipeline_step_header(text: str) -> Text:
    """Build a one-line pipeline step label using the Slate + Sky palette."""
    header = Text()
    header.append(f"{text} ", style=PIPELINE_STEP_HEADER_STYLE)
    return header
