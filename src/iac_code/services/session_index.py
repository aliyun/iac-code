"""Lite session metadata index for the /resume picker.

Reads only the first and last 64 KiB of each session JSONL file and
extracts metadata via string-search — never parses the whole file.
This keeps the picker fast even when individual sessions grow into the
megabytes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from iac_code.services.session_metadata import SESSION_JSONL_FILENAME, read_session_metadata
from iac_code.utils.project_paths import (
    get_project_dir,
    get_projects_dir,
    is_conversation_session_file,
    sanitize_path,
)

LITE_READ_BUF_SIZE = 64 * 1024


@dataclass
class LiteMetadata:
    cwd: str | None = None
    git_branch: str | None = None
    last_prompt: str | None = None
    first_prompt: str | None = None


@dataclass
class SessionEntry:
    session_id: str
    cwd: str
    project_name: str
    git_branch: str | None
    title: str
    mtime: float
    size_bytes: int
    name: str | None = None
    auto_title: str | None = None
    is_legacy: bool = True


# ---------------------------------------------------------------------------
# Field extraction helpers (string-search; tolerant of truncated chunks)
# ---------------------------------------------------------------------------


def _decode_json_string(raw: str) -> str:
    """Decode a JSON string body, tolerating partial input.

    ``raw`` is the substring between the opening and closing ``"`` of a
    JSON string. We round-trip via :func:`json.loads` to honour escapes,
    falling back to a manual unescape if the string was truncated.
    """
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\"", '"').replace(r"\\", "\\")


def _scan_string_field(chunk: str, field: str, *, last: bool) -> str | None:
    """Locate a JSON string field by name and return its decoded value."""
    needle = f'"{field}":'
    pos = chunk.rfind(needle) if last else chunk.find(needle)
    if pos < 0:
        return None
    i = pos + len(needle)
    n = len(chunk)
    while i < n and chunk[i] in " \t":
        i += 1
    if i >= n or chunk[i] != '"':
        return None
    i += 1
    start = i
    while i < n:
        ch = chunk[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return _decode_json_string(chunk[start:i])
        i += 1
    # Unterminated (chunk truncated) — return what we have.
    return _decode_json_string(chunk[start:])


def extract_first_json_string_field(chunk: str, field: str) -> str | None:
    return _scan_string_field(chunk, field, last=False)


def extract_last_json_string_field(chunk: str, field: str) -> str | None:
    return _scan_string_field(chunk, field, last=True)


# ---------------------------------------------------------------------------
# Head + tail file reader
# ---------------------------------------------------------------------------


def read_head_and_tail(path: Path, size: int | None = None) -> tuple[str, str]:
    """Read the first and last :data:`LITE_READ_BUF_SIZE` bytes.

    For files smaller than the buffer, ``head == tail`` and the whole
    content is returned twice. Decoding is best-effort UTF-8 — partial
    multibyte sequences at chunk edges are replaced.
    """
    actual_size = path.stat().st_size if size is None else size
    with open(path, "rb") as f:
        head_bytes = f.read(LITE_READ_BUF_SIZE)
        if actual_size <= LITE_READ_BUF_SIZE:
            tail_bytes = head_bytes
        else:
            f.seek(max(0, actual_size - LITE_READ_BUF_SIZE))
            tail_bytes = f.read(LITE_READ_BUF_SIZE)
    head = head_bytes.decode("utf-8", errors="replace")
    tail = tail_bytes.decode("utf-8", errors="replace")
    return head, tail


# ---------------------------------------------------------------------------
# First-user-message scanner (for fallback title)
# ---------------------------------------------------------------------------

_USER_ROLE_PATTERNS = (re.compile(r'"role"\s*:\s*"user"'),)


def _extract_first_user_text(head: str) -> str | None:
    """Find the first user message's text in a head chunk.

    Skips lite-meta rows (no ``role``), tool_result-only messages, and
    rows whose content can't be parsed.
    """
    for line in head.split("\n"):
        line = line.strip()
        if not line:
            continue
        if not any(p.search(line) for p in _USER_ROLE_PATTERNS):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("role") != "user":
            continue
        content = obj.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            texts: list[str] = []
            has_user_text = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_result":
                    continue
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        texts.append(text)
                        has_user_text = True
            if has_user_text:
                return " ".join(texts)
    return None


# ---------------------------------------------------------------------------
# Public LiteMetadata extraction
# ---------------------------------------------------------------------------


def read_lite_metadata(path: Path) -> LiteMetadata:
    """Extract LiteMetadata from a session file via head + tail scan."""
    try:
        head, tail = read_head_and_tail(path)
    except OSError:
        return LiteMetadata()
    cwd = extract_first_json_string_field(head, "cwd")
    git_branch = extract_last_json_string_field(tail, "git_branch") or extract_first_json_string_field(
        head, "git_branch"
    )
    last_prompt = extract_last_json_string_field(tail, "last_prompt")
    first_prompt = _extract_first_user_text(head)
    return LiteMetadata(
        cwd=cwd,
        git_branch=git_branch,
        last_prompt=last_prompt,
        first_prompt=first_prompt,
    )


# ---------------------------------------------------------------------------
# SessionIndex — list / search session entries across projects
# ---------------------------------------------------------------------------


def _trim_title(text: str, max_len: int = 200) -> str:
    flat = text.replace("\n", " ").strip()
    if len(flat) <= max_len:
        return flat
    return flat[:max_len].rstrip() + "…"


def _iter_session_files(project_dir: Path) -> list[tuple[Path, str]]:
    files_by_session_id = {
        jsonl.stem: jsonl for jsonl in project_dir.glob("*.jsonl") if is_conversation_session_file(jsonl)
    }
    for session_dir in project_dir.iterdir():
        if not session_dir.is_dir():
            continue
        jsonl = session_dir / SESSION_JSONL_FILENAME
        if jsonl.exists():
            files_by_session_id[session_dir.name] = jsonl
    return [(jsonl, session_id) for session_id, jsonl in files_by_session_id.items()]


def _build_entry(path: Path, fallback_cwd: str, session_id: str | None = None) -> SessionEntry | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    lite_meta = read_lite_metadata(path)
    path_session_id = session_id or path.stem
    directory_metadata = read_session_metadata(path.parent) if path.name == SESSION_JSONL_FILENAME else None
    if directory_metadata and directory_metadata.session_id != path_session_id:
        directory_metadata = None
    name = directory_metadata.name if directory_metadata else None
    auto_title_raw = lite_meta.last_prompt or lite_meta.first_prompt
    auto_title = _trim_title(auto_title_raw) if auto_title_raw else None
    cwd = (directory_metadata.cwd if directory_metadata else None) or lite_meta.cwd or fallback_cwd
    title = name or auto_title or "(empty)"
    return SessionEntry(
        session_id=path_session_id,
        cwd=cwd,
        project_name=os.path.basename(cwd) if cwd else "?",
        git_branch=(directory_metadata.git_branch if directory_metadata else None) or lite_meta.git_branch,
        title=title,
        mtime=stat.st_mtime,
        size_bytes=stat.st_size,
        name=name,
        auto_title=auto_title,
        is_legacy=path.name != SESSION_JSONL_FILENAME,
    )


class SessionIndex:
    """List/search session entries across all known project directories."""

    def __init__(self, projects_dir: Path | None = None) -> None:
        self._projects_dir = projects_dir if projects_dir is not None else get_projects_dir()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_for_cwd(self, cwd: str) -> list[SessionEntry]:
        """List entries that belong to ``cwd``, mtime-descending."""
        if self._projects_dir == get_projects_dir():
            project_dir = get_project_dir(cwd)
        else:
            project_dir = self._projects_dir / sanitize_path(cwd)
        if not project_dir.exists():
            return []
        entries: list[SessionEntry] = []
        for jsonl, session_id in _iter_session_files(project_dir):
            entry = _build_entry(jsonl, fallback_cwd=cwd, session_id=session_id)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: e.mtime, reverse=True)
        return entries

    def list_all_projects(self) -> list[SessionEntry]:
        """List entries across every known project, mtime-descending."""
        if not self._projects_dir.exists():
            return []
        entries: list[SessionEntry] = []
        for proj_dir in self._projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl, session_id in _iter_session_files(proj_dir):
                entry = _build_entry(jsonl, fallback_cwd="", session_id=session_id)
                if entry is not None:
                    entries.append(entry)
        entries.sort(key=lambda e: e.mtime, reverse=True)
        return entries

    def find_by_id_or_prefix(self, arg: str) -> SessionEntry | None:
        """Locate a single entry by exact session id or unique id prefix."""
        if not self._projects_dir.exists() or not arg:
            return None
        entries = self.list_all_projects()
        for entry in entries:
            if entry.session_id == arg:
                return entry
        matches = [entry for entry in entries if entry.session_id.startswith(arg)]
        if len(matches) == 1:
            return matches[0]
        return None
