"""File-reference, search, history, and transcript helpers for the Web runtime."""

from __future__ import annotations

import os
import shutil
from collections import deque
from pathlib import Path, PureWindowsPath
from typing import Any

from iac_code.agent.message import Message
from iac_code.config import get_history_path
from iac_code.i18n import _
from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.ui.core.input_history import InputHistory
from iac_code.web.events import normalize_event_payload

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_CONTEXT = 5
MAX_SEARCHABLE_FILE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_LINE_CHARS = 2000
HIDDEN_TRANSCRIPT_METADATA_TYPES = {
    CLEANUP_PROMPT_METADATA_TYPE,
    "internal-skill-context",
    "recalled_memory",
}
_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _context(value: int | None) -> int:
    if value is None:
        return 0
    return max(0, min(value, MAX_CONTEXT))


def _root(cwd: str) -> Path:
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(_("session cwd is not available"))
    return root


def _relative_to_root(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _is_safe_query(query: str) -> bool:
    if Path(query).is_absolute() or PureWindowsPath(query).is_absolute():
        return False
    parts = [part for part in query.replace("\\", "/").split("/") if part]
    return ".." not in parts


def _is_excluded_dir(name: str) -> bool:
    return name.startswith(".") or name in _EXCLUDED_DIRS


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirname for dirname in dirnames if not _is_excluded_dir(dirname))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            relative = _relative_to_root(path, root)
            if relative is None:
                continue
            yield path, relative


def _is_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" not in handle.read(4096)
    except OSError:
        return False


def _truncate_text(text: str) -> str:
    if len(text) <= MAX_SEARCH_LINE_CHARS:
        return text
    return text[:MAX_SEARCH_LINE_CHARS]


def _context_line(line_number: int, text: str) -> dict[str, object]:
    return {"lineNumber": line_number, "text": _truncate_text(text)}


def _is_hidden_transcript_message(message: Message) -> bool:
    return message.metadata.get("type") in HIDDEN_TRANSCRIPT_METADATA_TYPES


def _search_file(path: Path, relative: str, query: str, *, context: int, remaining: int) -> list[dict[str, object]]:
    try:
        if path.stat().st_size > MAX_SEARCHABLE_FILE_BYTES:
            return []
    except OSError:
        return []
    if not _is_text_file(path):
        return []

    results: list[dict[str, object]] = []
    before: deque[dict[str, object]] = deque(maxlen=context)
    pending: list[tuple[int, list[dict[str, object]]]] = []
    accepting_new_matches = True
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.rstrip("\r\n")
                if context > 0:
                    still_pending: list[tuple[int, list[dict[str, object]]]] = []
                    for match_line_number, after_lines in pending:
                        if line_number <= match_line_number + context:
                            after_lines.append(_context_line(line_number, text))
                            if line_number < match_line_number + context:
                                still_pending.append((match_line_number, after_lines))
                        else:
                            still_pending.append((match_line_number, after_lines))
                    pending = still_pending

                column_index = text.find(query) if accepting_new_matches else -1
                if column_index != -1:
                    after_lines: list[dict[str, object]] = []
                    result: dict[str, object] = {
                        "path": relative,
                        "lineNumber": line_number,
                        "column": column_index + 1,
                        "text": _truncate_text(text),
                        "before": list(before),
                        "after": after_lines,
                    }
                    results.append(result)
                    if context > 0:
                        pending.append((line_number, after_lines))
                    if len(results) >= remaining:
                        accepting_new_matches = False
                        if context <= 0:
                            break
                if context > 0:
                    before.append(_context_line(line_number, text))
                if not accepting_new_matches and not pending:
                    break
    except OSError:
        return []
    return results


def search_files(
    cwd: str,
    query: str,
    *,
    limit: int | None = None,
    context: int | None = None,
) -> list[dict[str, object]]:
    """Search text files below *cwd* and return browser-safe match records."""
    if not query:
        return []
    root = _root(cwd)
    max_results = _limit(limit)
    context_lines = _context(context)
    results: list[dict[str, object]] = []

    # Keep tests independent from system ripgrep while leaving one capability probe in the service boundary.
    shutil.which("rg")

    for path, relative in _iter_files(root):
        file_results = _search_file(path, relative, query, context=context_lines, remaining=max_results - len(results))
        results.extend(file_results)
        if len(results) >= max_results:
            return results
    return results


def quick_open_files(cwd: str, query: str, *, limit: int | None = None) -> list[dict[str, object]]:
    """Return quick-open file candidates scoped to *cwd*."""
    if not _is_safe_query(query):
        raise ValueError(_("query is invalid"))
    root = _root(cwd)
    needle = query.lower()
    results: list[dict[str, object]] = []
    for path, relative in _iter_files(root):
        if needle and needle not in relative.lower() and needle not in path.name.lower():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        results.append(
            {
                "path": relative,
                "name": path.name,
                "kind": "file",
                "size": size,
            }
        )
        if len(results) >= _limit(limit):
            break
    return results


def search_input_history(query: str, *, limit: int | None = None) -> list[dict[str, object]]:
    """Search persisted input history without returning nonmatching entries."""
    if not query:
        return []
    history_path = get_history_path()
    if not history_path.exists():
        return []
    entries: list[tuple[int, str]] = []
    with history_path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            raw = line.rstrip("\n")
            if not raw:
                continue
            entry = InputHistory._decode_line(raw)
            if query.lower() in entry.lower():
                entries.append((index, entry))
    return [{"index": index, "text": text} for index, text in reversed(entries[-_limit(limit) :])]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
        return " ".join(text_parts)
    return ""


def search_visible_user_history(
    visible_messages: list[dict[str, Any]],
    query: str,
    *,
    session_id: str,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Search visible session user turns without exposing assistant or hidden rows."""
    if not query:
        return []
    needle = query.lower()
    entries: list[dict[str, object]] = []
    user_index = 0
    for message in visible_messages:
        if message.get("role") != "user":
            continue
        user_index += 1
        text = _content_text(message.get("content"))
        if needle not in text.lower():
            continue
        entries.append({"index": user_index, "text": text, "source": "session", "sessionId": session_id})
    return entries[-_limit(limit) :]


def _visible_payload_for_message(message: Message) -> dict[str, Any]:
    message_dict = message.to_dict()
    return normalize_event_payload({"role": message_dict.get("role"), "content": message_dict.get("content")})


def _visible_message_matches_raw(visible: dict[str, Any], raw: Message) -> bool:
    raw_payload = _visible_payload_for_message(raw)
    return raw_payload.get("role") == visible.get("role") and raw_payload.get("content") == visible.get("content")


def _transcript_row_payload(visible: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": visible.get("role"),
        "content": visible.get("content"),
    }


def _metadata_string(message: Message, *keys: str) -> str | None:
    for key in keys:
        value = message.metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _enriched_visible_messages(
    visible_messages: list[dict[str, Any]],
    resume_messages: list[Message],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    raw_index = 0
    for visible in visible_messages:
        while raw_index < len(resume_messages):
            raw = resume_messages[raw_index]
            raw_index += 1
            if _is_hidden_transcript_message(raw):
                continue
            if not _visible_message_matches_raw(visible, raw):
                continue
            row = _transcript_row_payload(visible)
            turn_id = _metadata_string(raw, "turnId", "turn_id")
            message_id = _metadata_string(raw, "messageId", "message_id")
            if turn_id is not None:
                row["turnId"] = turn_id
            if message_id is not None:
                row["messageId"] = message_id
            enriched.append(row)
            break
    return enriched


def transcript_for_identifier(
    identifier: str,
    *,
    visible_messages: list[dict[str, Any]],
    resume_messages: list[Message],
) -> dict[str, object] | None:
    """Find a visible transcript by turn id or by a message id within that turn."""
    rows = _enriched_visible_messages(visible_messages, resume_messages)
    if not rows:
        return None

    turn_rows = [row for row in rows if row.get("turnId") == identifier]
    if turn_rows:
        return {"turnId": identifier, "messages": turn_rows}

    message_rows = [row for row in rows if row.get("messageId") == identifier]
    if not message_rows:
        return None
    turn_id = message_rows[0].get("turnId")
    if isinstance(turn_id, str) and turn_id:
        related_rows = [row for row in rows if row.get("turnId") == turn_id]
        return {"turnId": turn_id, "messages": related_rows}
    return {"turnId": identifier, "messages": message_rows}


def safe_file_references(file_refs: list[str], *, cwd: str, must_exist: bool = False) -> list[str]:
    """Return safe relative file references for inclusion in agent text."""
    root = Path(cwd).expanduser().resolve()
    safe_refs: list[str] = []
    for file_ref in file_refs:
        if not file_ref or Path(file_ref).is_absolute() or PureWindowsPath(file_ref).is_absolute():
            raise ValueError(_("file reference is invalid"))
        candidate = (root / file_ref).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(_("file reference escapes the workspace")) from exc
        if must_exist and not candidate.exists():
            raise ValueError("file reference is not available: {}".format(relative.as_posix()))
        safe_refs.append(relative.as_posix())
    return safe_refs
