"""Helpers for terminal architecture diagram rendering."""

from __future__ import annotations

from typing import Any

from rich.text import Text

ATTACHMENT_LINE_STYLE = "dim cyan"


def style_attachment_lines(renderable: Any) -> Any:
    """Dim attachment rows such as '+ EIP' in termaid terminal output."""
    if not isinstance(renderable, Text):
        return renderable

    styled = renderable.copy()
    active_attachment_segments: list[tuple[int, int]] = []
    offset = 0
    for line in styled.plain.splitlines(keepends=True):
        next_active_segments: list[tuple[int, int]] = []
        for left, right in _vertical_segments(line):
            content = line[left + 1 : right]
            stripped = content.strip()
            if not stripped:
                continue
            if stripped.startswith("+ "):
                content_start = offset + left + 1 + content.index("+")
                content_end = offset + left + 1 + len(content.rstrip())
                styled.stylize(ATTACHMENT_LINE_STYLE, content_start, content_end)
                next_active_segments.append((left, right))
                continue
            if _continues_attachment_segment((left, right), active_attachment_segments):
                content_start, content_end = _segment_content_bounds(line, left, right)
                if content_start < content_end:
                    styled.stylize(ATTACHMENT_LINE_STYLE, offset + content_start, offset + content_end)
                    next_active_segments.append((left, right))
        active_attachment_segments = next_active_segments
        offset += len(line)
    return styled


def _vertical_segments(line: str) -> list[tuple[int, int]]:
    columns = [index for index, char in enumerate(line.rstrip("\n")) if char == "│"]
    return [(columns[index], columns[index + 1]) for index in range(len(columns) - 1)]


def _continues_attachment_segment(segment: tuple[int, int], active_segments: list[tuple[int, int]]) -> bool:
    left, right = segment
    return any(max(left, active_left) < min(right, active_right) for active_left, active_right in active_segments)


def _segment_content_bounds(line: str, left: int, right: int) -> tuple[int, int]:
    content = line[left + 1 : right]
    start = len(content) - len(content.lstrip())
    end = len(content.rstrip())
    return left + 1 + start, left + 1 + end
