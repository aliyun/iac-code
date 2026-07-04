from __future__ import annotations

import stat
from pathlib import Path

from iac_code.i18n import _
from iac_code.services.session_layout import (
    SESSION_LAYOUT_VERSION_V2,
    UnsupportedSessionLayoutError,
    require_supported_session_layout,
)
from iac_code.services.session_storage import SessionStorage


def a2a_pipeline_dir_for_sidecar_dir(sidecar_dir: str | Path) -> Path:
    path = Path(sidecar_dir)
    if path.name == "pipeline":
        return path.parent / "a2a" / "pipeline"
    return path


def a2a_pipeline_dir_for_session(*, cwd: str, session_id: str) -> Path:
    storage = SessionStorage()
    session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, session_id)
    if session_dir is None:
        session_dir = storage.session_dir(cwd, session_id)
        version = require_supported_session_layout(session_dir)
    else:
        version = SESSION_LAYOUT_VERSION_V2
    preferred = session_dir / "a2a" / "pipeline"
    legacy = session_dir / "pipeline"
    if version != SESSION_LAYOUT_VERSION_V2 and (_has_a2a_metadata(preferred) or _has_a2a_metadata(legacy)):
        return preferred if _has_a2a_metadata(preferred) or not _has_a2a_metadata(legacy) else legacy
    if version != SESSION_LAYOUT_VERSION_V2:
        raise UnsupportedSessionLayoutError(_("Unsupported session layout version: {version}").format(version="legacy"))
    return preferred


def existing_a2a_pipeline_dir_for_session(*, cwd: str, session_id: str) -> Path:
    session_dir = SessionStorage().session_dir(cwd, session_id)
    preferred = session_dir / "a2a" / "pipeline"
    legacy = session_dir / "pipeline"
    version = require_supported_session_layout(session_dir)
    if version != SESSION_LAYOUT_VERSION_V2 and (_has_a2a_metadata(preferred) or _has_a2a_metadata(legacy)):
        return preferred if _has_a2a_metadata(preferred) or not _has_a2a_metadata(legacy) else legacy
    if _has_a2a_metadata(preferred) or not _has_a2a_metadata(legacy):
        return preferred
    return legacy


def _has_a2a_metadata(path: Path) -> bool:
    return _is_regular_file_entry(path / "a2a-events.jsonl") or _is_regular_file_entry(path / "a2a-snapshot.json")


def _is_regular_file_entry(path: Path) -> bool:
    if path.is_symlink() or _is_reparse_point(path):
        return False
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
