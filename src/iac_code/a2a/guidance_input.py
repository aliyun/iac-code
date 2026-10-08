"""Durable, per-context guidance acceptance; ambiguous claims never replay."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from iac_code.config import get_config_dir
from iac_code.services.handoff_fence import SessionWriterFence
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock


class GuidanceInputConflictError(RuntimeError):
    pass


class GuidanceInputJournal:
    _current: ContextVar[dict[str, str] | None] = ContextVar("guidance_input_identity", default=None)

    def __init__(self, root: Path | None = None):
        self.root = root or get_config_dir() / "guidance-input"

    def context_dir(self, context_id: str) -> Path:
        return self.root / hashlib.sha256(context_id.encode()).hexdigest()

    def path(self, context_id: str, guidance_id: str) -> Path:
        return self.context_dir(context_id) / (hashlib.sha256(guidance_id.encode()).hexdigest() + ".json")

    def read(self, *, context_id: str, guidance_id: str) -> dict[str, Any] | None:
        path = self.path(context_id, guidance_id)
        if not path.exists():
            return None
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get("status") not in {"STARTED", "APPLIED"}:
            raise GuidanceInputConflictError("Guidance acceptance record is unverifiable")
        return value

    def claim(self, *, context_id: str, task_id: str, guidance_id: str, input_digest: str) -> dict[str, Any]:
        identity = dict(context_id=context_id, task_id=task_id, guidance_id=guidance_id, input_digest=input_digest)
        if not all(isinstance(value, str) and value for value in identity.values()):
            raise GuidanceInputConflictError("Guidance identity is incomplete")
        path = self.path(context_id, guidance_id)
        with (
            SessionWriterFence().operation("context:" + context_id, kind="input"),
            cross_process_file_lock(path.with_suffix(".lock")),
        ):
            existing = self.read(context_id=context_id, guidance_id=guidance_id)
            if existing is not None:
                if any(existing.get(key) != value for key, value in identity.items()):
                    raise GuidanceInputConflictError("Guidance identity changed")
                if existing["status"] != "APPLIED":
                    raise GuidanceInputConflictError("Prior guidance acceptance is unknown")
                return existing
            record = identity | {"status": "STARTED"}
            atomic_write_json(path, record)
            return record

    def mark_applied(self, *, context_id: str, task_id: str, guidance_id: str, input_digest: str) -> None:
        identity = dict(context_id=context_id, task_id=task_id, guidance_id=guidance_id, input_digest=input_digest)
        path = self.path(context_id, guidance_id)
        with (
            SessionWriterFence().operation("context:" + context_id),
            cross_process_file_lock(path.with_suffix(".lock")),
        ):
            existing = self.read(context_id=context_id, guidance_id=guidance_id)
            if existing is None or any(existing.get(key) != value for key, value in identity.items()):
                raise GuidanceInputConflictError("Guidance application has no matching claim")
            atomic_write_json(path, identity | {"status": "APPLIED"})

    @classmethod
    @contextmanager
    def bind(cls, identity: dict[str, str] | None) -> Iterator[None]:
        token = cls._current.set(identity)
        try:
            yield
        finally:
            cls._current.reset(token)

    @classmethod
    def mark_current_applied(cls) -> None:
        identity = cls._current.get()
        if identity is not None:
            cls().mark_applied(**identity)
