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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from iac_code.agent.message import (
    COMPACTION_SUMMARY_METADATA_TYPE,
    RECALLED_MEMORY_MARKER,
    RECALLED_MEMORY_METADATA_TYPE,
    is_legacy_compaction_summary_storage_row,
)
from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.services.session_layout import (
    UnsupportedSessionLayoutError,
    is_supported_session_dir_for_id,
)
from iac_code.services.session_metadata import SESSION_JSONL_FILENAME, SESSION_METADATA_FILENAME, read_session_metadata
from iac_code.utils.project_paths import (
    MAX_SANITIZED_LENGTH,
    get_projects_dir,
    is_conversation_session_file,
    project_dir_candidates,
)

LITE_READ_BUF_SIZE = 64 * 1024
_LEGACY_CLEANUP_CHINESE_PREFIX = "检测到 pipeline rollback 后仍需要清理的云资源"
_LEGACY_CLEANUP_ROLLBACK_PHRASES = ("rollback cleanup required",)
_LEGACY_CLEANUP_RESOURCE_PHRASES = (
    "leftover resource",
    "stack-",
    "delete_complete",
    "仍需要清理",
    "待清理资源",
    "回滚残留资源",
)
_INTERNAL_SKILL_CONTEXT_RE = re.compile(r"^\s*<skill-name>[^<]+</skill-name>(?:\s|\Z)")


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
        decoded: list[str] = []
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch != "\\":
                decoded.append(ch)
                i += 1
                continue
            if i + 1 >= len(raw):
                decoded.append(ch)
                i += 1
                continue
            escaped = raw[i + 1]
            if escaped == "n":
                decoded.append("\n")
            elif escaped == "t":
                decoded.append("\t")
            elif escaped == '"':
                decoded.append('"')
            elif escaped == "\\":
                decoded.append("\\")
            else:
                decoded.append("\\")
                decoded.append(escaped)
            i += 2
        return "".join(decoded)


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


def _is_recalled_memory_text(text: str | None) -> bool:
    return bool(text and RECALLED_MEMORY_MARKER in text)


def _is_cleanup_prompt_text(text: str | None) -> bool:
    if not text:
        return False
    if _LEGACY_CLEANUP_CHINESE_PREFIX in text and "DELETE_COMPLETE" in text:
        return True
    lowered = text.lower()
    has_rollback_context = any(phrase in lowered for phrase in _LEGACY_CLEANUP_ROLLBACK_PHRASES)
    has_cleanup_resource_context = any(phrase in lowered for phrase in _LEGACY_CLEANUP_RESOURCE_PHRASES)
    return has_rollback_context and has_cleanup_resource_context


def _is_internal_skill_context_text(text: str | None) -> bool:
    return bool(text and _INTERNAL_SKILL_CONTEXT_RE.match(text))


def _is_pipeline_handoff_context_text(text: str | None) -> bool:
    return bool(text and text.startswith("[Pipeline Handoff Context]"))


def _is_hidden_prompt_row(obj: dict) -> bool:
    metadata = obj.get("metadata")
    if isinstance(metadata, dict) and metadata.get("type") == RECALLED_MEMORY_METADATA_TYPE:
        return True
    if isinstance(metadata, dict) and metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE:
        return True
    if isinstance(metadata, dict) and metadata.get("type") == "internal-skill-context":
        return True
    if isinstance(metadata, dict) and metadata.get("type") == COMPACTION_SUMMARY_METADATA_TYPE:
        return True
    if is_legacy_compaction_summary_storage_row(obj):
        return True
    content = obj.get("content")
    return isinstance(content, str) and (
        _is_recalled_memory_text(content)
        or _is_cleanup_prompt_text(content)
        or _is_internal_skill_context_text(content)
        or _is_pipeline_handoff_context_text(content)
    )


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
        if _is_hidden_prompt_row(obj):
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
    if (
        _is_recalled_memory_text(last_prompt)
        or _is_cleanup_prompt_text(last_prompt)
        or _is_internal_skill_context_text(last_prompt)
        or _is_pipeline_handoff_context_text(last_prompt)
    ):
        last_prompt = None
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


def _iter_session_files(project_dir: Path, *, projects_dir: Path | None = None) -> list[tuple[Path, str]]:
    files_by_session_id = {
        jsonl.stem: jsonl for jsonl in project_dir.glob("*.jsonl") if is_conversation_session_file(jsonl)
    }
    for session_dir in project_dir.iterdir():
        if not session_dir.is_dir():
            continue
        jsonl = session_dir / SESSION_JSONL_FILENAME
        if jsonl.exists():
            if not _is_indexable_session_dir(session_dir, session_dir.name):
                continue
            files_by_session_id[session_dir.name] = jsonl
            continue
        metadata = session_dir / SESSION_METADATA_FILENAME
        if metadata.exists():
            if not _is_indexable_session_dir(session_dir, session_dir.name):
                continue
            session_metadata = read_session_metadata(session_dir)
            if session_metadata is None:
                continue
            if _metadata_only_shadowed_by_legacy_session(
                project_dir,
                projects_dir or project_dir.parent,
                session_dir.name,
                session_metadata,
            ):
                continue
            if session_dir.name in files_by_session_id:
                continue
            files_by_session_id[session_dir.name] = metadata
    return [(jsonl, session_id) for session_id, jsonl in files_by_session_id.items()]


def _is_indexable_session_dir(session_dir: Path, session_id: str) -> bool:
    try:
        return is_supported_session_dir_for_id(session_dir, session_id)
    except UnsupportedSessionLayoutError:
        return False


def _metadata_only_shadowed_by_legacy_session(
    project_dir: Path,
    projects_dir: Path,
    session_id: str,
    metadata: object,
) -> bool:
    for candidate in _metadata_shadow_project_dirs(project_dir, projects_dir, metadata):
        legacy_path = candidate / f"{session_id}.jsonl"
        if legacy_path.exists() and is_conversation_session_file(legacy_path):
            return True
    return False


def _metadata_shadow_project_dirs(project_dir: Path, projects_dir: Path, metadata: object) -> tuple[Path, ...]:
    project_dirs: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path) -> None:
        if candidate not in seen:
            project_dirs.append(candidate)
            seen.add(candidate)

    cwd = getattr(metadata, "cwd", None)
    if isinstance(cwd, str) and cwd:
        for candidate in project_dir_candidates(cwd, projects_dir):
            add(candidate)
    add(project_dir)
    if projects_dir.exists():
        all_project_dirs = [candidate for candidate in projects_dir.iterdir() if candidate.is_dir()]
        aliases = _long_project_alias_identities(all_project_dirs)
        identity = aliases.get(project_dir.name)
        if identity is not None:
            for candidate in all_project_dirs:
                if aliases.get(candidate.name) == identity:
                    add(candidate)
    return tuple(project_dirs)


def _long_project_dir_hash_suffix(project_dir: Path) -> str | None:
    name = project_dir.name
    if len(name) < 14 or name[-13] != "-":
        return None
    suffix = name[-12:]
    try:
        int(suffix, 16)
    except ValueError:
        return None
    return suffix


def _long_project_alias_identities(project_dirs: list[Path]) -> dict[str, tuple[str, str]]:
    """Identify only real bounded/legacy long-path directory pairs.

    A normal project name may legitimately end in ``-<12 hex>``.  The suffix
    alone therefore cannot prove that it is a generated long-path alias.
    """
    by_suffix: dict[str, list[Path]] = {}
    for project_dir in project_dirs:
        suffix = _long_project_dir_hash_suffix(project_dir)
        if suffix is not None:
            by_suffix.setdefault(suffix, []).append(project_dir)

    identities: dict[str, tuple[str, str]] = {}
    legacy_length = MAX_SANITIZED_LENGTH + 13
    for suffix, candidates in by_suffix.items():
        bounded = [candidate for candidate in candidates if len(candidate.name) == MAX_SANITIZED_LENGTH]
        legacy = [candidate for candidate in candidates if len(candidate.name) == legacy_length]
        for current in bounded:
            current_prefix = current.name[:-13]
            for old in legacy:
                if not old.name[:-13].startswith(current_prefix):
                    continue
                identity = ("long-path-alias", current.name)
                identities[current.name] = identity
                identities[old.name] = identity
    return identities


def _project_storage_identity(
    project_dir: Path,
    aliases: dict[str, tuple[str, str]],
) -> tuple[str, str]:
    """Return a stable identity shared by bounded and legacy long-path aliases."""
    return aliases.get(project_dir.name, ("directory", project_dir.name))


def _project_alias_priority(project_dir: Path) -> int:
    """Prefer the bounded current alias over the pre-fix overlong alias."""
    return int(len(project_dir.name) <= MAX_SANITIZED_LENGTH)


def _session_file_priority(path: Path) -> int:
    if path.name == SESSION_JSONL_FILENAME:
        return 3
    if path.name == SESSION_METADATA_FILENAME:
        return 1
    return 2


def _project_dirs_for_cwd(cwd: str, projects_dir: Path) -> tuple[Path, ...]:
    candidates = project_dir_candidates(cwd, projects_dir)
    return tuple(candidate for candidate in candidates if candidate.exists())


def _build_entry(path: Path, fallback_cwd: str, session_id: str | None = None) -> SessionEntry | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    metadata_only = path.name == SESSION_METADATA_FILENAME
    lite_meta = LiteMetadata() if metadata_only else read_lite_metadata(path)
    path_session_id = session_id or path.stem
    directory_metadata = (
        read_session_metadata(path.parent) if path.name in {SESSION_JSONL_FILENAME, SESSION_METADATA_FILENAME} else None
    )
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
        size_bytes=0 if metadata_only else stat.st_size,
        name=name,
        auto_title=auto_title,
        is_legacy=path.name not in {SESSION_JSONL_FILENAME, SESSION_METADATA_FILENAME},
    )


class SessionIndex:
    """List/search session entries across all known project directories."""

    def __init__(self, projects_dir: Path | None = None) -> None:
        self._projects_dir = projects_dir if projects_dir is not None else get_projects_dir()
        # 请求级快照:>0 时 list_all_projects_page 复用同一份全量扫描结果(见 snapshot())。
        self._snapshot_depth = 0
        self._snapshot_entries: tuple[list[SessionEntry], int] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """在此代码块内让 :meth:`list_all_projects_page` 只做一次全量扫描并复用。

        首页 ``/api/sessions`` 一次请求里会经由 list_sessions_page /
        list_pinned_sessions / list_session_projects / list_pinned_projects
        触发 3~4 次 ``list_all_projects_page(limit=None)``,每次都 stat + 解析
        磁盘上的每个会话文件。进入本块时把首次扫描结果缓存,块内后续调用(全量或
        限量)都从这份按 mtime 降序排好的列表返回;退出最外层块时清空缓存,
        块外的读写仍取磁盘最新状态。可重入(按深度计数)。
        """
        self._snapshot_depth += 1
        try:
            yield
        finally:
            self._snapshot_depth -= 1
            if self._snapshot_depth == 0:
                self._snapshot_entries = None

    def _scan_all_entries(self) -> tuple[list[SessionEntry], int]:
        """Build a SessionEntry for every session file across all projects, mtime-desc."""
        if not self._projects_dir.exists():
            return [], 0
        entries_by_project_session: dict[tuple[str, str], SessionEntry] = {}
        priorities: dict[tuple[str, str], tuple[int, int, float]] = {}
        for proj_dir in self._projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl, session_id in _iter_session_files(proj_dir, projects_dir=self._projects_dir):
                entry = _build_entry(jsonl, fallback_cwd="", session_id=session_id)
                if entry is None:
                    continue
                key = (entry.cwd, session_id)
                priority = (_session_file_priority(jsonl), _project_alias_priority(proj_dir), entry.mtime)
                if priority <= priorities.get(key, (-1, -1, -1.0)):
                    continue
                entries_by_project_session[key] = entry
                priorities[key] = priority
        entries = list(entries_by_project_session.values())
        entries.sort(key=lambda e: e.mtime, reverse=True)
        return entries, len(entries)

    def list_for_cwd(self, cwd: str) -> list[SessionEntry]:
        """List entries that belong to ``cwd``, mtime-descending."""
        project_dirs = _project_dirs_for_cwd(cwd, self._projects_dir)
        if not project_dirs:
            return []
        entries_by_session_id: dict[str, SessionEntry] = {}
        priorities: dict[str, int] = {}
        for project_dir in project_dirs:
            for jsonl, session_id in _iter_session_files(project_dir, projects_dir=self._projects_dir):
                entry = _build_entry(jsonl, fallback_cwd=cwd, session_id=session_id)
                if entry is None:
                    continue
                priority = _session_file_priority(jsonl)
                if priority <= priorities.get(session_id, -1):
                    continue
                entries_by_session_id[session_id] = entry
                priorities[session_id] = priority
        entries = list(entries_by_session_id.values())
        entries.sort(key=lambda e: e.mtime, reverse=True)
        return entries

    def list_all_projects_page(self, *, limit: int | None = None) -> tuple[list[SessionEntry], int]:
        """List entries across every known project with optional metadata-read limit."""
        if self._snapshot_depth > 0:
            # 请求级快照生效:全量扫描一次后,全量/限量调用都从这份缓存返回。
            if self._snapshot_entries is None:
                self._snapshot_entries = self._scan_all_entries()
            entries, total = self._snapshot_entries
            if limit is None:
                return entries, total
            if limit <= 0:
                return [], total
            return entries[:limit], total
        if not self._projects_dir.exists():
            return [], 0
        if limit is None:
            return self._scan_all_entries()

        project_dirs = [proj_dir for proj_dir in self._projects_dir.iterdir() if proj_dir.is_dir()]
        aliases = _long_project_alias_identities(project_dirs)
        candidates_by_storage: dict[tuple[tuple[str, str], str], tuple[float, Path, str, int, int]] = {}
        for proj_dir in project_dirs:
            project_identity = _project_storage_identity(proj_dir, aliases)
            alias_priority = _project_alias_priority(proj_dir)
            for jsonl, session_id in _iter_session_files(proj_dir, projects_dir=self._projects_dir):
                try:
                    mtime = jsonl.stat().st_mtime
                except OSError:
                    continue
                key = (project_identity, session_id)
                priority = _session_file_priority(jsonl)
                current = candidates_by_storage.get(key)
                candidate_rank = (priority, alias_priority, mtime)
                current_rank = (current[3], current[4], current[0]) if current is not None else (-1, -1, -1.0)
                if candidate_rank > current_rank:
                    candidates_by_storage[key] = (mtime, jsonl, session_id, priority, alias_priority)
        candidates = list(candidates_by_storage.values())
        candidates.sort(key=lambda item: item[0], reverse=True)
        if limit <= 0:
            return [], len(candidates)

        entries_by_project_session: dict[tuple[str, str], SessionEntry] = {}
        priorities: dict[tuple[str, str], int] = {}
        for _mtime, jsonl, session_id, _priority, _alias_priority in candidates:
            entry = _build_entry(jsonl, fallback_cwd="", session_id=session_id)
            if entry is None:
                continue
            key = (entry.cwd, session_id)
            priority = _session_file_priority(jsonl)
            if priority <= priorities.get(key, -1):
                continue
            entries_by_project_session[key] = entry
            priorities[key] = priority
            if len(entries_by_project_session) >= limit:
                break
        entries = list(entries_by_project_session.values())
        entries.sort(key=lambda entry: entry.mtime, reverse=True)
        return entries, len(candidates)

    def list_all_projects(self) -> list[SessionEntry]:
        """List entries across every known project, mtime-descending."""
        entries, _total = self.list_all_projects_page()
        return entries

    def list_project_directories(self) -> list[Path]:
        """List known project storage directories by directory name."""
        if not self._projects_dir.exists():
            return []
        return sorted((path for path in self._projects_dir.iterdir() if path.is_dir()), key=lambda path: path.name)

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
