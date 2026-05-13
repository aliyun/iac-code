"""Externalize large tool results to disk to preserve context window."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MAX_INLINE_CHARS = 50_000
DEFAULT_PREVIEW_CHARS = 2_000


@dataclass
class ProcessedResult:
    content: str
    is_externalized: bool = False
    file_path: str | None = None


class ResultStorage:
    def __init__(
        self,
        storage_dir: str,
        max_inline_chars: int = DEFAULT_MAX_INLINE_CHARS,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ):
        self._storage_dir = storage_dir
        self._max_inline_chars = max_inline_chars
        self._preview_chars = preview_chars

    def process(self, tool_use_id: str, content: str) -> ProcessedResult:
        if len(content) <= self._max_inline_chars:
            return ProcessedResult(content=content)
        os.makedirs(self._storage_dir, exist_ok=True)
        file_path = os.path.join(self._storage_dir, f"{tool_use_id}.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        preview = content[: self._preview_chars]
        preview += f"\n\n... [truncated — full output ({len(content)} chars) saved to {file_path}]"
        return ProcessedResult(content=preview, is_externalized=True, file_path=file_path)
