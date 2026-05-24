"""iac-code telemetry package — zero-dependency public facade."""

from __future__ import annotations

from typing import Any

from iac_code.services.telemetry.client import TelemetryClient

__all__ = [
    "log_event",
    "add_metric",
    "start_span",
    "bootstrap_telemetry",
    "graceful_shutdown",
    "flush_telemetry",
    "get_client",
    "set_client",
    "get_session_id",
    "get_user_id",
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
