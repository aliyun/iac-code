"""Normalization helpers for text-valued step conclusion fields."""

from __future__ import annotations

import re

_FENCE_LINE = re.compile(r"^\s*(?P<ticks>`{3,}|~{3,})[ \t]*(?P<info>[^\s`~]*)[ \t]*$")
_MAX_PREAMBLE_LINES = 20


def strip_markdown_code_fence(text: str) -> str:
    """Return the bare payload of a text wrapped in a single markdown code fence.

    Models sometimes submit an IaC template as ```` ```yaml ... ``` ```` and prepend a short
    explanatory paragraph, which makes the value unparseable for downstream structured
    consumers. This helper recovers the bare payload only when the whole value is a single
    fenced block (optionally preceded by a short preamble). Anything ambiguous — no fence,
    an unterminated fence, or content after the closing fence — is returned unchanged so a
    template is never silently truncated.
    """
    if "`" not in text and "~" not in text:
        return text

    lines = text.splitlines()
    opening = _find_opening_fence(lines)
    if opening is None:
        return text

    open_index, ticks = opening
    close_index = _find_closing_fence(lines, open_index, ticks)
    if close_index is None:
        return text
    if any(line.strip() for line in lines[close_index + 1 :]):
        return text

    return "\n".join(lines[open_index + 1 : close_index]).strip("\n")


def _find_opening_fence(lines: list[str]) -> tuple[int, str] | None:
    """Locate the opening fence, tolerating a short non-fenced preamble."""
    for index, line in enumerate(lines[:_MAX_PREAMBLE_LINES]):
        match = _FENCE_LINE.match(line)
        if match is not None:
            return index, match.group("ticks")
    return None


def _find_closing_fence(lines: list[str], open_index: int, ticks: str) -> int | None:
    """Locate the fence closing the block opened at ``open_index``."""
    for index in range(open_index + 1, len(lines)):
        match = _FENCE_LINE.match(lines[index])
        if match is None or match.group("info"):
            continue
        if match.group("ticks")[0] == ticks[0] and len(match.group("ticks")) >= len(ticks):
            return index
    return None
