"""Durable, execution-free state for the AG-UI to A2A adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from iac_code.config import get_config_dir
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import atomic_write_json, open_text_no_follow

AGUI_STATE_SCHEMA_VERSION = 1
_STATE_DIR_ENV = "IAC_CODE_AGUI_STATE_DIR"
_THREADS_DIR_NAME = "threads"
_ENCODED_THREAD_ID_PREFIX = "aguiid~"
_HASHED_THREAD_ID_PREFIX = "aguihash~"
_MAX_ATOMIC_FILE_STEM_LENGTH = 200
_SAFE_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class AguiStateStoreError(RuntimeError):
    """Durable AG-UI thread state could not be read or committed."""


class AguiStateStore(Protocol):
    """Per-thread persistence boundary used by :class:`AguiA2AAdapter`."""

    def load_thread(self, thread_id: str) -> dict[str, Any] | None: ...

    def save_thread(self, thread_id: str, state: Mapping[str, Any]) -> None: ...


class FileAguiThreadStateStore:
    """Owner-private, atomically replaced JSON state split by AG-UI thread."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = resolve_agui_state_dir(state_dir)
        self.threads_dir = self.state_dir / _THREADS_DIR_NAME

    def path_for_thread(self, thread_id: str) -> Path:
        return self.threads_dir / f"{_thread_file_stem(thread_id)}.json"

    def load_thread(self, thread_id: str) -> dict[str, Any] | None:
        path = self.path_for_thread(thread_id)
        if not path.exists():
            return None
        try:
            ensure_private_file(path)
            with open_text_no_follow(path, "r") as handle:
                value = json.load(handle)
        except Exception as exc:
            raise AguiStateStoreError("Unable to read the AG-UI thread state.") from exc
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != AGUI_STATE_SCHEMA_VERSION
            or value.get("threadId") != thread_id
        ):
            raise AguiStateStoreError("Unsupported or invalid AG-UI thread state schema.")
        return value

    def save_thread(self, thread_id: str, state: Mapping[str, Any]) -> None:
        document = dict(state)
        if document.get("schemaVersion") != AGUI_STATE_SCHEMA_VERSION or document.get("threadId") != thread_id:
            raise AguiStateStoreError("Refusing to save invalid AG-UI thread state.")
        try:
            ensure_private_dir(self.state_dir)
            ensure_private_dir(self.threads_dir)
            path = self.path_for_thread(thread_id)
            atomic_write_json(path, document)
            ensure_private_file(path)
        except Exception as exc:
            raise AguiStateStoreError("Unable to commit the AG-UI thread state.") from exc


def resolve_agui_state_dir(value: str | Path | None = None) -> Path:
    """Resolve an explicit/env state root without coupling it to any request cwd."""

    raw = str(value).strip() if value is not None else os.environ.get(_STATE_DIR_ENV, "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    return get_config_dir() / "agui"


def _thread_file_stem(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not thread_id:
        raise AguiStateStoreError("AG-UI thread id must be a non-empty string.")
    basename = thread_id.rstrip(".").split(".", 1)[0].upper()
    if (
        _SAFE_THREAD_ID_PATTERN.fullmatch(thread_id) is not None
        and thread_id == thread_id.lower()
        and thread_id not in {".", ".."}
        and thread_id == thread_id.rstrip(".")
        and basename not in _WINDOWS_RESERVED_BASENAMES
    ):
        return thread_id
    # Lowercase hexadecimal is reversible and remains collision-free on the
    # case-insensitive filesystems used by default on Windows and macOS.
    encoded = thread_id.encode("utf-8").hex()
    encoded_stem = f"{_ENCODED_THREAD_ID_PREFIX}{encoded}"
    if len(encoded_stem) <= _MAX_ATOMIC_FILE_STEM_LENGTH:
        return encoded_stem
    # The original id remains in the JSON document and is validated when read.
    # A fixed-length lowercase key avoids filesystem component limits while
    # retaining case-insensitive safety for unusually long client-provided ids.
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return f"{_HASHED_THREAD_ID_PREFIX}{digest}"


__all__ = [
    "AGUI_STATE_SCHEMA_VERSION",
    "AguiStateStore",
    "AguiStateStoreError",
    "FileAguiThreadStateStore",
    "resolve_agui_state_dir",
]
