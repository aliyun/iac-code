"""Identity generation for telemetry.

user.id    = iac_user_<uuid4>, persisted to a settings.yml path
session.id = iac_sess_<uuid4>, per Identity instance (per process)
tenant.id  = iac_tenant_<user-defined>, from IAC_CODE_TENANT_ID
"""

from __future__ import annotations

import contextvars
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from iac_code.config import _load_yaml, _save_yaml

USER_ID_PREFIX = "iac_user_"
SESSION_ID_PREFIX = "iac_sess_"
TENANT_ID_PREFIX = "iac_tenant_"

_USER_ID_KEY = "userID"
_TENANT_ENV_VAR = "IAC_CODE_TENANT_ID"

# Per-async-context override for session id. Set via use_session_id; when
# present, Identity.get_session_id returns this instead of the process-level
# value. Enables a2a/acp servers to report per-conversation session ids in
# telemetry without rebuilding the OTel providers.
_session_id_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iac_code_telemetry_session_id_override", default=None
)


@contextmanager
def use_session_id(session_id: str) -> Iterator[None]:
    """Override the telemetry session id for the current async context."""
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    value = session_id if session_id.startswith(SESSION_ID_PREFIX) else f"{SESSION_ID_PREFIX}{session_id}"
    token = _session_id_override.set(value)
    try:
        yield
    finally:
        _session_id_override.reset(token)


class Identity:
    """Owns user.id / session.id / tenant.id.

    `settings_path` is injected so tests can pass a tmp path instead of the
    real ~/.iac-code/settings.yml.
    """

    def __init__(self, settings_path: Path, session_id: str | None = None) -> None:
        self._settings_path = settings_path
        self._user_id: str | None = None
        self._session_id: str | None = f"{SESSION_ID_PREFIX}{session_id}" if session_id else None
        self._was_first_run = False

    def get_user_id(self) -> str:
        """Return the persistent user.id; generate + persist on first miss."""
        if self._user_id is not None:
            return self._user_id
        settings = _load_yaml(self._settings_path)
        existing = settings.get(_USER_ID_KEY)
        if isinstance(existing, str) and existing.startswith(USER_ID_PREFIX):
            self._user_id = existing
            return existing
        new_id = f"{USER_ID_PREFIX}{uuid.uuid4()}"
        settings[_USER_ID_KEY] = new_id
        _save_yaml(self._settings_path, settings)
        self._user_id = new_id
        self._was_first_run = True
        return new_id

    def get_session_id(self) -> str:
        """Return per-instance session.id; generate on first call.

        Honors an active ``use_session_id`` override so a2a/acp servers can
        report per-context session ids without mutating process state.
        """
        override = _session_id_override.get()
        if override is not None:
            return override
        if self._session_id is None:
            self._session_id = f"{SESSION_ID_PREFIX}{uuid.uuid4()}"
        return self._session_id

    def get_tenant_id(self) -> str | None:
        """Return tenant.id if IAC_CODE_TENANT_ID is set, else None.

        Read fresh each call so monkeypatching in tests works reliably.
        """
        raw = os.environ.get(_TENANT_ENV_VAR, "").strip()
        if not raw:
            return None
        if raw.startswith(TENANT_ID_PREFIX):
            return raw
        return f"{TENANT_ID_PREFIX}{raw}"

    def was_first_run(self) -> bool:
        """True iff get_user_id() minted a new id on this instance."""
        return self._was_first_run
