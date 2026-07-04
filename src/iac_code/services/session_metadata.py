"""Session metadata primitives."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from iac_code.i18n import _
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import atomic_write_text

SESSION_JSONL_FILENAME = "session.jsonl"
SESSION_METADATA_FILENAME = "metadata.json"
SESSION_NAME_PATTERN_TEXT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"
SESSION_NAME_PATTERN = re.compile(SESSION_NAME_PATTERN_TEXT)
SESSION_METADATA_SCHEMA_VERSION = 1
SESSION_LAYOUT_VERSION_V2 = 2
SUPPORTED_SESSION_LAYOUT_VERSIONS = {SESSION_LAYOUT_VERSION_V2}


@dataclass(frozen=True)
class SessionMetadata:
    session_id: str
    name: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    schema_version: int = SESSION_METADATA_SCHEMA_VERSION
    layout_version: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMetadata | None:
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None

        name = data.get("name")
        schema_version = data.get("schema_version")
        layout_version = data.get("layout_version")
        return cls(
            session_id=session_id,
            name=name if isinstance(name, str) and name else None,
            cwd=_string_or_none(data.get("cwd")),
            git_branch=_string_or_none(data.get("git_branch")),
            created_at=_string_or_none(data.get("created_at")),
            updated_at=_string_or_none(data.get("updated_at")),
            schema_version=schema_version if type(schema_version) is int else SESSION_METADATA_SCHEMA_VERSION,
            layout_version=layout_version if type(layout_version) is int else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def validate_session_name(name: str) -> str:
    if not SESSION_NAME_PATTERN.fullmatch(name):
        raise ValueError(_("Session name must match {pattern}").format(pattern=SESSION_NAME_PATTERN_TEXT))
    return name


def normalize_session_name(name: str) -> str:
    return validate_session_name(name.strip())


def read_session_metadata(session_dir: Path) -> SessionMetadata | None:
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    if not session_metadata_entry_exists(session_dir) or not is_session_metadata_file_entry(session_dir):
        return None
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return SessionMetadata.from_dict(data)


def read_session_layout_version(session_dir: Path) -> int | None:
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    if not session_metadata_entry_exists(session_dir):
        return None
    if not is_session_metadata_file_entry(session_dir):
        raise ValueError(_("invalid session metadata"))
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(_("invalid session metadata")) from exc
    if not isinstance(data, dict):
        raise ValueError(_("invalid session metadata"))
    if "layout_version" not in data:
        return None
    layout_version = data.get("layout_version")
    if type(layout_version) is not int:
        raise ValueError(_("invalid session layout version"))
    if SessionMetadata.from_dict(data) is None:
        raise ValueError(_("invalid session metadata"))
    return layout_version


def write_session_metadata(session_dir: Path, metadata: SessionMetadata) -> None:
    ensure_private_dir(session_dir)
    path = session_dir / SESSION_METADATA_FILENAME
    atomic_write_text(path, json.dumps(metadata.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
    ensure_private_file(path)


def session_metadata_entry_exists(session_dir: Path) -> bool:
    try:
        (session_dir / SESSION_METADATA_FILENAME).stat(follow_symlinks=False)
    except OSError:
        return False
    return True


def is_session_metadata_file_entry(session_dir: Path) -> bool:
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    if metadata_path.is_symlink() or _is_reparse_point(metadata_path):
        return False
    try:
        return stat.S_ISREG(metadata_path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
