"""AttributeBuilder — build resource and per-event attribute dicts."""

from __future__ import annotations

import itertools
import os
import platform
import socket
import sys
from datetime import datetime, timezone
from threading import Lock

from iac_code.services.telemetry.identity import Identity
from iac_code.services.telemetry.names import (
    ARMS_FEATURE_GENAI_APP,
    FRAMEWORK_IAC_CODE,
    ArmsResourceAttr,
)


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
        self._sequence = itertools.count(1)
        self._sequence_lock = Lock()

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
