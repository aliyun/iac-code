"""Externalize large tool results to disk to preserve context window."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from hashlib import blake2b
from pathlib import Path

from iac_code.services.session_layout import ensure_session_owned_dir
from iac_code.services.session_metadata import read_session_metadata, session_metadata_entry_exists
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import open_text_no_follow

DEFAULT_MAX_INLINE_CHARS = 20_000
RESULT_URI_PREFIX = "tool-result://"
EXTERNALIZED_RESULT_PATH_METADATA_KEY = "_iac_code_externalized_result_path"
_SAFE_TOOL_USE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _result_filename(tool_use_id: str) -> str:
    cleaned = tool_use_id.strip()
    if (
        cleaned
        and len(cleaned) <= 120
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
    ):
        self._storage_dir = storage_dir
        self._max_inline_chars = max_inline_chars

    @property
    def storage_dir(self) -> str:
        return self._storage_dir

    def prepare_directory(self) -> Path:
        storage_path = Path(self._storage_dir)
        session_root = _session_root_for_storage_path(storage_path)
        if session_root is not None:
            storage_dir = ensure_session_owned_dir(session_root, storage_path)
        else:
            for parent in (storage_path, *storage_path.parents):
                if parent.is_symlink():
                    raise OSError("Unsafe tool result directory.")
            if storage_path.parent.name == "tool-results":
                ensure_private_dir(storage_path.parent)
            storage_dir = ensure_private_dir(storage_path)
        return storage_dir

    def process(
        self,
        tool_use_id: str,
        content: str,
        *,
        tool_name: str = "",
        is_error: bool = False,
        metadata: dict | None = None,
    ) -> ProcessedResult:
        if len(content) <= self._max_inline_chars:
            return ProcessedResult(content=content)
        storage_dir = self.prepare_directory()
        file_path = storage_dir / _result_filename(tool_use_id)
        file_path = store_result_text(file_path, content)
        reference = result_reference(file_path, storage_dir)
        description = describe_result(reference, content, tool_name=tool_name, is_error=is_error)
        if isinstance(metadata, dict):
            if type(metadata.get("exitCode")) is int:
                description += f"\nExit code: {metadata['exitCode']}"
            fetch = metadata.get("web_fetch")
            if isinstance(fetch, dict) and fetch.get("source_truncated"):
                description += "\nThe fetched source was incomplete; this file preserves all retained fetch output."
        return ProcessedResult(content=description, is_externalized=True, file_path=str(file_path))


def result_reference(path: Path, storage_dir: Path) -> str:
    digest = result_digest(str(path))
    session_root = _session_root_for_storage_path(storage_dir)
    if session_root is not None:
        metadata = read_session_metadata(session_root)
        if metadata is None:
            raise ValueError("Invalid tool result session metadata.")
        relative = path.relative_to(session_root).as_posix()
        return RESULT_URI_PREFIX + f"session/{metadata.session_id}/{digest}/{relative}"
    return RESULT_URI_PREFIX + f"local/{digest}/" + path.relative_to(storage_dir).as_posix()


def describe_result(reference: str, content: str, *, tool_name: str = "", is_error: bool = False) -> str:
    lines = content.count("\n") + int(bool(content) and not content.endswith("\n"))
    return (
        f"Tool: {tool_name or 'tool'}\nStatus: {'error' if is_error else 'success'}\n"
        f"Complete output saved as {reference}\nCharacters: {len(content)}; lines: {lines}\n"
        "Use grep(path=the reference) to locate content or read_file(path=the reference) "
        "to read pages. No output content has been discarded. "
        "Do not repeatedly cat the whole file or grep an entire long JSON line; "
        "use grep output_mode=positions, read_file cursors, or a focused Bash query."
    )


def resolve_result_reference(reference: str, storage_dir: str | None, *, must_exist: bool = True) -> str:
    """Resolve only this session's files; references survive moving/restoring the session."""
    if not storage_dir:
        raise ValueError("Tool result references require a current session.")
    relative = reference.removeprefix(RESULT_URI_PREFIX)
    parts = relative.split("/")
    if not reference.startswith(RESULT_URI_PREFIX) or any(
        not part or part in {".", ".."} or "\\" in part or ":" in part for part in parts
    ):
        raise ValueError("Invalid tool result reference.")
    if any(character in relative for character in "*?[]{}"):
        raise ValueError("Tool result references identify one file; wildcard patterns are not supported.")
    root = Path(storage_dir)
    digest = None
    if parts[0] == "session":
        if len(parts) < 5:
            raise ValueError("Invalid session tool result reference.")
        session_root = _session_root_for_storage_path(root)
        if session_root is None:
            raise ValueError("Tool result reference requires its original session.")
        metadata = read_session_metadata(session_root) if session_root is not None else None
        if metadata is None or metadata.session_id != parts[1]:
            raise ValueError("Tool result reference belongs to another session.")
        root, digest, parts = session_root, parts[2], parts[3:]
        valid_scope = parts[0] == "tool-results" or (
            len(parts) >= 5 and parts[:2] == ["pipeline", "transcripts"] and parts[3] == "tool-results"
        )
        if not valid_scope:
            raise ValueError("Tool result reference is outside result storage.")
    elif parts[0] == "local":
        if len(parts) < 3:
            raise ValueError("Invalid local tool result reference.")
        digest, parts = parts[1], parts[2:]
    if ".bash-inputs" in parts or (digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("Invalid tool result reference.")
    # Do not follow symlinks in the root or descendants.
    for component in (root, *root.parents):
        if component.is_symlink():
            raise ValueError("Unsafe tool result directory.")
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("Unsafe tool result reference.")
    target.resolve().relative_to(root.resolve())
    if not target.is_file():
        if not must_exist and not target.exists():
            return str(target.resolve())
        raise FileNotFoundError(f"Tool result not found: {reference}")
    if digest is not None and result_digest(str(target)) != digest:
        raise ValueError("Tool result content has changed; the original reference is no longer valid.")
    return str(target.resolve())


def result_digest(path: str) -> str:
    info = Path(path).stat()
    return _cached_digest(path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@lru_cache(maxsize=128)
def _cached_digest(path: str, *identity: int) -> str:
    digest = hashlib.sha256()
    with open_text_no_follow(path, "r", encoding="utf-8", newline="") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), ""):
            digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def store_result_text(path: Path, content: str, *, read_only: bool = False) -> Path:
    """Publish complete, private, immutable text without replacing an existing result."""
    return _store_result_chunks(path, [content], read_only=read_only)


def _store_result_chunks(
    path: Path,
    chunks: Iterable[str],
    *,
    read_only: bool = False,
    expected_digest: str | None = None,
) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=".result-", dir=path.parent)
    temporary_path = Path(temporary)
    published = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            hasher = hashlib.sha256()
            for chunk in chunks:
                data = chunk.encode("utf-8")
                hasher.update(data)
                stream.write(data)
        digest = hasher.hexdigest()
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("Tool result changed while preparing Bash input.")
        ensure_private_file(temporary_path)
        if read_only:
            temporary_path.chmod(stat.S_IRUSR)
        candidate = path
        for attempt in range(2):
            try:
                os.link(temporary_path, candidate)
                published = candidate
                return candidate
            except FileExistsError:
                if candidate.is_symlink() or not candidate.is_file():
                    raise OSError("Refusing to replace a symlinked or non-regular tool result.")
                if result_digest(str(candidate)) == digest:
                    return candidate
                if attempt:
                    raise ValueError("Conflicting immutable tool result.")
                candidate = path.with_name(f"{path.stem[:100]}-{digest}{path.suffix}")
        raise AssertionError("unreachable")
    finally:
        # Windows cannot unlink a read-only file, including its temporary link.
        if os.name == "nt":
            temporary_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        temporary_path.unlink(missing_ok=True)
        if os.name == "nt" and published is not None and read_only:
            published.chmod(stat.S_IRUSR)


def bash_result_snapshot(reference: str, storage_dir: str | None) -> str:
    """Use a verified private copy so replacing the source cannot race shell startup."""
    path = resolve_result_reference(reference, storage_dir)
    root = _bash_input_root(storage_dir)
    parts = reference.removeprefix(RESULT_URI_PREFIX).split("/")
    digest = parts[2] if parts[0] == "session" else parts[1] if parts[0] == "local" else result_digest(path)
    target = root / f"{digest}.txt"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or result_digest(str(target)) != digest:
            raise ValueError("Bash result input has changed.")
        return str(target.resolve())
    with open_text_no_follow(path, "r", encoding="utf-8", newline="") as stream:
        copied = _store_result_chunks(
            target,
            iter(lambda: stream.read(64 * 1024), ""),
            read_only=True,
            expected_digest=digest,
        )
    return str(copied.resolve())


def missing_bash_result_input(reference: str, storage_dir: str | None) -> str:
    """A missing result stays missing even if a later command creates its source path."""
    target = _bash_input_root(storage_dir) / (".missing-" + hashlib.sha256(reference.encode()).hexdigest())
    if target.exists() or target.is_symlink():
        raise ValueError("Unexpected file at a missing Bash result input.")
    return str(target.resolve())


def _bash_input_root(storage_dir: str | None) -> Path:
    if storage_dir is None:
        raise ValueError("Tool result references require a current session.")
    root = ResultStorage(storage_dir).prepare_directory() / ".bash-inputs"
    if root.is_symlink():
        raise ValueError("Unsafe Bash result input directory.")
    return ensure_private_dir(root)


def _session_root_for_storage_path(storage_path: Path) -> Path | None:
    for candidate in (storage_path, *storage_path.parents):
        if session_metadata_entry_exists(candidate):
            return candidate
    return None
