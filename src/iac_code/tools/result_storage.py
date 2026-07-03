"""Externalize large tool results to disk to preserve context window."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path

from iac_code.services.session_layout import ensure_session_owned_dir
from iac_code.services.session_metadata import session_metadata_entry_exists
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import write_text_no_follow

DEFAULT_MAX_INLINE_CHARS = 50_000
DEFAULT_PREVIEW_CHARS = 2_000
EXTERNALIZED_RESULT_PATH_METADATA_KEY = "_iac_code_externalized_result_path"
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
        session_root = _session_root_for_storage_path(storage_path)
        if session_root is not None:
            storage_dir = ensure_session_owned_dir(session_root, storage_path)
        elif storage_path.parent.name == "tool-results":
            ensure_private_dir(storage_path.parent)
            storage_dir = ensure_private_dir(storage_path)
        else:
            storage_dir = ensure_private_dir(storage_path)
        file_path = storage_dir / _result_filename(tool_use_id)
        write_text_no_follow(file_path, content, encoding="utf-8")
        ensure_private_file(file_path)
        preview = content[: self._preview_chars]
        preview += f"\n\n... [truncated — full output ({len(content)} chars) saved to {file_path}]"
        return ProcessedResult(content=preview, is_externalized=True, file_path=str(file_path))


def _session_root_for_storage_path(storage_path: Path) -> Path | None:
    for candidate in (storage_path, *storage_path.parents):
        if session_metadata_entry_exists(candidate):
            return candidate
    return None
