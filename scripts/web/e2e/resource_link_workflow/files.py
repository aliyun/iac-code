"""Crash-safe files and explicit containment; never treat a path as authorization."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class WorkflowError(ValueError):
    pass


def contained(root: Path, name: str) -> Path:
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise WorkflowError("path escapes authorized directory") from exc
    return candidate


def atomic_write(path: Path, content: str | bytes, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_write(path: Path, value: Any, *, private: bool = False) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", private=private)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
