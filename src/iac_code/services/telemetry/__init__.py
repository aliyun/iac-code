"""iac-code telemetry package — zero-dependency public facade."""

from __future__ import annotations

from typing import Any

from iac_code.services.telemetry.attributes import use_telemetry_channel
from iac_code.services.telemetry.client import TelemetryClient
from iac_code.services.telemetry.identity import use_session_id, use_user_id
from iac_code.services.telemetry.tracing import (
    attach_context,
    detach_context,
    get_current_context,
)

__all__ = [
    "log_event",
    "add_metric",
    "start_span",
    "start_detached_span",
    "use_span",
    "get_current_context",
    "attach_context",
    "detach_context",
    "bootstrap_telemetry",
    "graceful_shutdown",
    "flush_telemetry",
    "get_client",
    "set_client",
    "get_session_id",
    "get_user_id",
    "use_session_id",
    "use_telemetry_channel",
    "use_user_id",
]

_client: TelemetryClient | None = None


def get_client() -> TelemetryClient:
    """Return the singleton client, creating it on first call."""
    global _client
    if _client is None:
        _client = TelemetryClient()
    return _client


def set_client(client: TelemetryClient | None) -> None:
    """Replace (or clear) the singleton. Useful for tests."""
    global _client
    _client = client


def log_event(event_name: str, metadata: dict[str, Any] | None = None) -> None:
    get_client().log_event(event_name, metadata)


def add_metric(name: str, value: int | float, attributes: dict[str, Any] | None = None) -> None:
    get_client().add_metric(name, value, attributes)


def start_span(name: str, attributes: dict[str, Any] | None = None):
    return get_client().start_span(name, attributes)


def start_detached_span(name: str, attributes: dict[str, Any] | None = None, *, parent_context=None):
    return get_client().start_detached_span(name, attributes, parent_context=parent_context)


def use_span(
    span,
    *,
    record_exception: bool = False,
    set_status_on_exception: bool = False,
    end_on_exit: bool = False,
):
    return get_client().use_span(
        span,
        record_exception=record_exception,
        set_status_on_exception=set_status_on_exception,
        end_on_exit=end_on_exit,
    )


def bootstrap_telemetry(session_id: str | None = None) -> None:
    global _client
    if _client is None:
        _client = TelemetryClient(session_id=session_id)
    get_client().bootstrap()


def graceful_shutdown() -> None:
    get_client().shutdown()


def flush_telemetry() -> None:
    """Force-flush pending telemetry without closing providers.

    Safe to call repeatedly between units of work (e.g. per-task in a2a/acp
    servers). Synchronous and bounded by the client's flush timeout — async
    callers should wrap with ``asyncio.to_thread`` to avoid blocking the loop.
    """
    get_client().flush()


def get_session_id() -> str:
    return get_client().get_session_id()


def get_user_id() -> str:
    return get_client().get_user_id()
