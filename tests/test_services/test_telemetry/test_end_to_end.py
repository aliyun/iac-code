"""End-to-end smoke test for TelemetryClient.

Validates that log_event() calls made before bootstrap() are queued and
drained after activate + bootstrap. Uses a fresh TelemetryClient per test
for isolation (no shared singleton).
"""

from unittest.mock import MagicMock

import pytest

from iac_code.services.telemetry import (
    add_metric,
    bootstrap_telemetry,
    graceful_shutdown,
    log_event,
    set_client,
)
from iac_code.services.telemetry.client import TelemetryClient
from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.names import Events, Metrics
from iac_code.services.telemetry.sink import AnalyticsSink


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Default: block the hardcoded ARMS default backend so tests don't hit
    # real network. Individual tests that need the privacy gate open can
    # override with monkeypatch.delenv("DISABLE_TELEMETRY").
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.delenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    monkeypatch.delenv("IAC_CODE_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.delenv("IAC_CODE_TELEMETRY_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("IAC_CODE_TELEMETRY_METRICS_ENDPOINT", raising=False)
    monkeypatch.delenv("IAC_CODE_TELEMETRY_LOGS_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    set_client(None)  # reset facade singleton
    yield
    set_client(None)


def test_events_emitted_before_bootstrap_do_not_raise():
    log_event(Events.SESSION_STARTED, {"k": 1})
    add_metric(Metrics.SESSION_COUNT, 1, {})


def test_bootstrap_with_no_default_endpoint_does_not_crash():
    bootstrap_telemetry()


def test_prequeue_drained_after_bootstrap(tmp_path, monkeypatch):
    # This test needs the privacy gate open so the sink actually forwards
    # drained events to the mock emitter.
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    # Build a custom client with a mock emitter so we can observe.
    mock_emitter = MagicMock(spec=EventEmitter)
    sink = AnalyticsSink(mock_emitter)
    client = TelemetryClient(sink=sink)
    set_client(client)

    # Call before bootstrap — must queue.
    log_event(Events.SESSION_STARTED, {"headless": False})
    assert mock_emitter.emit.call_count == 0

    # Bootstrap activates sink + drains.
    bootstrap_telemetry()
    # drain_soon schedules via asyncio; force sync drain for test.
    sink.drain_sync()
    assert mock_emitter.emit.call_count >= 1


def test_shutdown_is_safe_after_bootstrap():
    bootstrap_telemetry()
    graceful_shutdown()  # must not raise
