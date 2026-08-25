"""Layout-aware paths for session-owned runtime data."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

from iac_code.i18n import _
from iac_code.services.session_metadata import (
    SESSION_LAYOUT_VERSION_V2,
    SUPPORTED_SESSION_LAYOUT_VERSIONS,
    is_session_metadata_file_entry,
    read_session_layout_version,
    read_session_metadata,
    session_metadata_entry_exists,
)
from iac_code.utils.path_components import is_unsafe_windows_path_component

_SAFE_TRANSCRIPT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

__all__ = [
    "SESSION_LAYOUT_VERSION_V2",
    "SessionPaths",
    "UnsupportedSessionLayoutError",
    "ensure_session_owned_dir",
    "ensure_session_owned_parent",
    "is_supported_session_dir_for_id",
    "require_supported_session_layout",
    "session_layout_version",
]


class UnsupportedSessionLayoutError(RuntimeError):
    """Raised when a session metadata layout version is unknown to this build."""


def session_layout_version(session_dir: Path) -> int | None:
    return read_session_layout_version(session_dir)


def require_supported_session_layout(session_dir: Path) -> int | None:
    try:
        version = session_layout_version(session_dir)
    except ValueError as exc:
        raise UnsupportedSessionLayoutError(_("Unsupported session metadata: {path}").format(path=session_dir)) from exc
    if version is None or version in SUPPORTED_SESSION_LAYOUT_VERSIONS:
        return version
    raise UnsupportedSessionLayoutError(_("Unsupported session layout version: {version}").format(version=version))


def is_supported_session_dir_for_id(session_dir: Path | str, session_id: str) -> bool:
    """Return whether a supported session directory belongs to ``session_id``.

    A metadata ``session_id`` mismatch is treated as a non-match so callers can
    ignore shadow directories. Invalid or unsupported metadata for the same
    directory is still surfaced as an unsupported layout.
    """

    path = Path(session_dir)
    if not session_metadata_entry_exists(path):
        require_supported_session_layout(path)
        return True
    if not is_session_metadata_file_entry(path):
        raise UnsupportedSessionLayoutError(_("Unsupported session metadata: {path}").format(path=path))
    metadata = read_session_metadata(path)
    if metadata is not None and metadata.session_id != session_id:
        return False
    require_supported_session_layout(path)
    if metadata is None:
        raise UnsupportedSessionLayoutError(_("Unsupported session metadata: {path}").format(path=path))
    return True


def ensure_session_owned_dir(session_dir: Path | str, path: Path | str) -> Path:
    session_root = Path(session_dir)
    target = Path(path)
    require_supported_session_layout(session_root)
    relative = _session_owned_relative_path(session_root, target)
    _ensure_directory_entry_is_safe(session_root)
    current = session_root
    for part in relative.parts:
        current = current / part
        _ensure_directory_entry_is_safe(current)
    return target


def ensure_session_owned_parent(session_dir: Path | str, path: Path | str) -> Path:
    return ensure_session_owned_dir(session_dir, Path(path).parent)


def _session_owned_relative_path(session_dir: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(session_dir)
    except ValueError as exc:
        raise UnsupportedSessionLayoutError(_("Path is outside session directory: {path}").format(path=path)) from exc
    if not relative.parts:
        return Path()
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise UnsupportedSessionLayoutError(_("Unsafe session-owned path: {path}").format(path=path))
    return relative


def _ensure_directory_entry_is_safe(path: Path) -> None:
    if path.is_symlink() or _is_reparse_point(path):
        raise UnsupportedSessionLayoutError(_("Unsafe session-owned path: {path}").format(path=path))
    if not path.exists():
        from iac_code.utils.file_security import ensure_private_dir

        ensure_private_dir(path)
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise UnsupportedSessionLayoutError(_("Unsafe session-owned path: {path}").format(path=path)) from exc
    if not stat.S_ISDIR(mode):
        raise UnsupportedSessionLayoutError(_("Unsafe session-owned path: {path}").format(path=path))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validate_transcript_id(transcript_id: str) -> str:
    if not transcript_id or transcript_id in {".", ".."}:
        raise ValueError(_("unsafe transcript id"))
    if "/" in transcript_id or "\\" in transcript_id or ".." in transcript_id:
        raise ValueError(_("unsafe transcript id"))
    if not _SAFE_TRANSCRIPT_ID.fullmatch(transcript_id):
        raise ValueError(_("unsafe transcript id"))
    if is_unsafe_windows_path_component(transcript_id):
        raise ValueError(_("unsafe transcript id"))
    return transcript_id


@dataclass(frozen=True)
class SessionPaths:
    session_dir: Path

    @classmethod
    def from_session_dir(cls, session_dir: Path | str) -> SessionPaths:
        return cls(Path(session_dir))

    @classmethod
    def require_supported(cls, session_dir: Path | str) -> SessionPaths:
        path = Path(session_dir)
        require_supported_session_layout(path)
        return cls(path)

    @property
    def session_jsonl_path(self) -> Path:
        return self.session_dir / "session.jsonl"

    @property
    def usage_path(self) -> Path:
        return self.session_dir / "usage.jsonl"

    @property
    def permission_audit_path(self) -> Path:
        return self.session_dir / "permission-audit.jsonl"

    @property
    def image_cache_dir(self) -> Path:
        return self.session_dir / "image-cache"

    @property
    def tool_results_dir(self) -> Path:
        return self.session_dir / "tool-results"

    @property
    def permission_waits_dir(self) -> Path:
        return self.session_dir / "permission-waits"

    @property
    def permission_waits_lock_path(self) -> Path:
        return self.permission_waits_dir / ".lock"

    @property
    def a2a_dir(self) -> Path:
        return self.session_dir / "a2a"

    @property
    def a2a_task_path(self) -> Path:
        return self.a2a_dir / "task.json"

    @property
    def a2a_context_path(self) -> Path:
        return self.a2a_dir / "context.json"

    @property
    def a2a_artifacts_dir(self) -> Path:
        return self.a2a_dir / "artifacts"

    @property
    def logs_dir(self) -> Path:
        return self.session_dir / "logs"

    @property
    def a2a_pipeline_flow_log_path(self) -> Path:
        return self.logs_dir / "a2a-pipeline-flow.jsonl"

    def transcript_dir(self, transcript_id: str) -> Path:
        return self.session_dir / "pipeline" / "transcripts" / _validate_transcript_id(transcript_id)

    def transcript_usage_path(self, transcript_id: str) -> Path:
        return self.transcript_dir(transcript_id) / "usage.jsonl"

    def transcript_permission_audit_path(self, transcript_id: str) -> Path:
        return self.transcript_dir(transcript_id) / "permission-audit.jsonl"

    def transcript_tool_results_dir(self, transcript_id: str) -> Path:
        return self.transcript_dir(transcript_id) / "tool-results"

    def transcript_image_cache_dir(self, transcript_id: str) -> Path:
        return self.transcript_dir(transcript_id) / "image-cache"
