"""A session's prompt time, renewed only when a new user turn starts."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from iac_code.services.session_metadata import read_session_metadata, write_session_metadata
from iac_code.services.session_mutation_guard import session_mutation_guard
from iac_code.services.session_storage import SessionStorage

_VALIDITY = timedelta(minutes=30)
_TIME_LINE = re.compile(r"(?m)^(# Runtime Context\n- Current time: )[^\n]*")


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


class SessionPromptTime:
    def __init__(self, storage: Any, cwd: str, session_id: str) -> None:
        self._storage = storage
        self._cwd = cwd
        self._session_id = session_id
        metadata = storage.read_metadata(cwd, session_id) if isinstance(storage, SessionStorage) else None
        saved = _parse(metadata.prompt_time) if metadata is not None else None
        self._anchor = saved or datetime.now().astimezone()

    @property
    def value(self) -> str:
        return self._anchor.strftime("%Y-%m-%d %H:%M:%S")

    def begin_turn(self) -> None:
        """Renew from the last published time, never from the last request time."""
        now = datetime.now().astimezone()
        if not isinstance(self._storage, SessionStorage):
            self._renew(now)
            return
        directory = self._storage.ensure_v2_session_dir_for_new_session(self._cwd, self._session_id)
        if directory is None:  # Preserve legacy layouts without adding sidecars.
            self._renew(now)
            return
        with session_mutation_guard(directory):
            metadata = read_session_metadata(directory)
            if metadata is None or metadata.session_id != self._session_id:
                raise ValueError("Invalid session metadata for prompt time")
            saved = _parse(metadata.prompt_time)
            self._anchor = saved or now
            self._renew(now)
            if saved != self._anchor:
                write_session_metadata(directory, replace(metadata, prompt_time=self._anchor.isoformat()))

    def _renew(self, now: datetime) -> None:
        age = now - self._anchor
        if age < timedelta(0) or age > _VALIDITY:
            self._anchor = now

    def apply(self, prompt: str) -> str:
        """Refresh only the runtime time line; do not re-render step context."""
        return _TIME_LINE.sub(lambda match: match[1] + self.value, prompt, count=1)
