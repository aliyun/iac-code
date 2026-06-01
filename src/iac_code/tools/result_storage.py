"""Externalize large tool results to disk to preserve context window."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path

from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

DEFAULT_MAX_INLINE_CHARS = 50_000
DEFAULT_PREVIEW_CHARS = 2_000
_SAFE_TOOL_USE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _result_filename(tool_use_id: str) -> str:
    cleaned = tool_use_id.strip()
    if (
        cleaned
        and cleaned not in {".", ".."}
        and "/" not in cleaned
        and "\\" not in cleaned
        and ".." not in cleaned
        and not Path(cleaned).is_absolute()
        and _SAFE_TOOL_USE_ID_RE.fullmatch(cleaned)
    ):
        return f"{cleaned}.txt"
    digest = blake2b(tool_use_id.encode("utf-8"), digest_size=12).hexdigest()
    return f"tool_result_{digest}.txt"


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
        storage_path = Path(self._storage_dir)
        if storage_path.parent.name == "tool-results":
            ensure_private_dir(storage_path.parent)
        storage_dir = ensure_private_dir(storage_path)
        file_path = storage_dir / _result_filename(tool_use_id)
        with file_path.open("w", encoding="utf-8") as f:
            f.write(content)
        ensure_private_file(file_path)
        preview = content[: self._preview_chars]
        preview += f"\n\n... [truncated — full output ({len(content)} chars) saved to {file_path}]"
        return ProcessedResult(content=preview, is_externalized=True, file_path=str(file_path))
