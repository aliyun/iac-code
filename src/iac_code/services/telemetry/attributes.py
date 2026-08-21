"""AttributeBuilder — build resource and per-event attribute dicts."""

from __future__ import annotations

import contextvars
import itertools
import os
import platform
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock

from iac_code.services.telemetry.identity import Identity
from iac_code.services.telemetry.names import (
    ARMS_FEATURE_GENAI_APP,
    FRAMEWORK_IAC_CODE,
    ArmsResourceAttr,
    IacCodeAttr,
)

_CHANNEL_ENV = "IAC_CODE_CHANNEL"
_DEFAULT_CHANNEL = "unknown"
_MAX_CHANNEL_LENGTH = 128
_telemetry_channel_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iac_code_telemetry_channel_override",
    default=None,
)


def normalize_telemetry_channel(value: object) -> str | None:
    """Return a bounded non-empty channel, or None for invalid input."""
    if not isinstance(value, str):
        return None
    return value.strip()[:_MAX_CHANNEL_LENGTH] or None


@contextmanager
def use_telemetry_channel(channel: str) -> Iterator[None]:
    """Override the telemetry channel for the current async context."""
    normalized = normalize_telemetry_channel(channel)
    if normalized is None:
        raise ValueError("telemetry channel must be a non-empty string")
    token = _telemetry_channel_override.set(normalized)
    try:
        yield
    finally:
        _telemetry_channel_override.reset(token)


def _detect_service_version() -> str:
    """Look up installed version; fallback to 0.0.0 for dev."""
    try:
        from importlib.metadata import version

        return version("iac-code")
    except Exception:
        return "0.0.0"


def _detect_host_name() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


class AttributeBuilder:
    """Assembles the attribute dicts attached to every signal.

    Resource attributes are identity + app + device fields. Event attributes
    wrap event.name with a timestamp and a monotonic sequence.
    """

    def __init__(
        self,
        identity: Identity,
        service_name: str,
        service_version: str | None = None,
    ) -> None:
        self._identity = identity
        self._service_name = service_name
        self._service_version = service_version or _detect_service_version()
        self._channel = normalize_telemetry_channel(os.environ.get(_CHANNEL_ENV)) or _DEFAULT_CHANNEL
        self._sequence = itertools.count(1)
        self._sequence_lock = Lock()

    def build_signal_attributes(self) -> dict[str, str]:
        """Attributes attached directly to every exported signal."""
        return {IacCodeAttr.CHANNEL: _telemetry_channel_override.get() or self._channel}

    def build_resource(self) -> dict[str, str]:
        """Identity + app + device attributes. Called once at startup, usually."""
        attrs: dict[str, str] = {
            "service.name": self._service_name,
            "service.version": self._service_version,
            "os.type": sys.platform,
            "host.arch": platform.machine() or "unknown",
            "host.name": _detect_host_name(),
            "deployment.environment": os.environ.get("IAC_CODE_ENV", "production"),
            ArmsResourceAttr.CMS_WORKSPACE: FRAMEWORK_IAC_CODE,
            ArmsResourceAttr.SERVICE_FEATURE: ARMS_FEATURE_GENAI_APP,
            "user.id": self._identity.get_user_id(),
            "session.id": self._identity.get_session_id(),
        }
        attrs.update(self.build_signal_attributes())
        tenant = self._identity.get_tenant_id()
        if tenant is not None:
            attrs["tenant.id"] = tenant
        return attrs

    def build_event(self, event_name: str) -> dict[str, str | int]:
        """Per-event envelope."""
        with self._sequence_lock:
            seq = next(self._sequence)
        return {
            "event.name": event_name,
            "event.timestamp": datetime.now(timezone.utc).isoformat(),
            "event.sequence": seq,
        }
