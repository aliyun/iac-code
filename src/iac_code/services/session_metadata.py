"""Session metadata primitives."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from iac_code.i18n import _
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

SESSION_JSONL_FILENAME = "session.jsonl"
SESSION_METADATA_FILENAME = "metadata.json"
SESSION_NAME_PATTERN_TEXT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"
SESSION_NAME_PATTERN = re.compile(SESSION_NAME_PATTERN_TEXT)
SESSION_METADATA_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SessionMetadata:
    session_id: str
    name: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    schema_version: int = SESSION_METADATA_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMetadata | None:
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None

        name = data.get("name")
        schema_version = data.get("schema_version")
        return cls(
            session_id=session_id,
            name=name if isinstance(name, str) and name else None,
            cwd=_string_or_none(data.get("cwd")),
            git_branch=_string_or_none(data.get("git_branch")),
            created_at=_string_or_none(data.get("created_at")),
            updated_at=_string_or_none(data.get("updated_at")),
            schema_version=schema_version if type(schema_version) is int else SESSION_METADATA_SCHEMA_VERSION,
        )


def validate_session_name(name: str) -> str:
    if not SESSION_NAME_PATTERN.fullmatch(name):
        raise ValueError(_("Session name must match {pattern}").format(pattern=SESSION_NAME_PATTERN_TEXT))
    return name


def normalize_session_name(name: str) -> str:
    return validate_session_name(name.strip())


def read_session_metadata(session_dir: Path) -> SessionMetadata | None:
    try:
        data = json.loads((session_dir / SESSION_METADATA_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return SessionMetadata.from_dict(data)


def write_session_metadata(session_dir: Path, metadata: SessionMetadata) -> None:
    ensure_private_dir(session_dir)
    path = session_dir / SESSION_METADATA_FILENAME
    path.write_text(json.dumps(asdict(metadata), ensure_ascii=False) + "\n", encoding="utf-8")
    ensure_private_file(path)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
