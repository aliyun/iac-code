"""Identity generation for telemetry.

user.id    = an explicit E2E identity, a configured account/user ID, or iac_user_<uuid4>
session.id = iac_sess_<uuid4>, per Identity instance (per process)
tenant.id  = iac_tenant_<user-defined>, from IAC_CODE_TENANT_ID
"""

from __future__ import annotations

import contextvars
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from iac_code.config import _load_yaml, _save_yaml

USER_ID_PREFIX = "iac_user_"
SESSION_ID_PREFIX = "iac_sess_"
TENANT_ID_PREFIX = "iac_tenant_"

_USER_ID_KEY = "userID"
E2E_USER_ID_ENV = "IAC_CODE_TELEMETRY_E2E_USER_ID"
_TENANT_ENV_VAR = "IAC_CODE_TENANT_ID"
_ALIYUN_ACCOUNT_ID_LENGTH = 16
_E2E_USER_ID_PATTERN = re.compile(r"iac_user_e2e_[0-9a-f]{32}\Z")


def is_e2e_user_id(value: object) -> bool:
    return isinstance(value, str) and _E2E_USER_ID_PATTERN.fullmatch(value) is not None


def get_e2e_user_id() -> str | None:
    value = os.environ.get(E2E_USER_ID_ENV, "")
    return value if is_e2e_user_id(value) else None

# Per-async-context override for session id. Set via use_session_id; when
# present, Identity.get_session_id returns this instead of the process-level
# value. Enables a2a/acp servers to report per-conversation session ids in
# telemetry without rebuilding the OTel providers.
_session_id_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iac_code_telemetry_session_id_override", default=None
)

# Per-async-context override for user id. Set via use_user_id; when present,
# Identity.get_user_id returns this instead of the process-level value.
# Enables a2a servers to report per-task user ids in telemetry.
_user_id_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iac_code_telemetry_user_id_override", default=None
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


@contextmanager
def use_user_id(user_id: str) -> Iterator[None]:
    """Override the telemetry user id for the current async context."""
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    token = _user_id_override.set(user_id)
    try:
        yield
    finally:
        _user_id_override.reset(token)


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
        """Return the persistent user.id; generate + persist on first miss.

        A ROS-deployed Web instance may use its 16-digit Alibaba Cloud account
        ID directly so restarts preserve the account-scoped identity.

        An explicit E2E identity wins over ``use_user_id`` so all test traffic,
        including A2A server lifecycle events, carries the report filter tag.
        Otherwise ``use_user_id`` supports per-task identities.
        """
        e2e_user_id = get_e2e_user_id()
        if e2e_user_id is not None:
            return e2e_user_id
        override = _user_id_override.get()
        if override is not None:
            return override
        if self._user_id is not None:
            return self._user_id
        settings = _load_yaml(self._settings_path)
        existing = settings.get(_USER_ID_KEY)
        if isinstance(existing, str) and (
            existing.startswith(USER_ID_PREFIX)
            or (len(existing) == _ALIYUN_ACCOUNT_ID_LENGTH and existing.isascii() and existing.isdigit())
        ):
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
