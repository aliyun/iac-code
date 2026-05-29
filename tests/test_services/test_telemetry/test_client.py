"""Tests for TelemetryClient."""

from unittest.mock import MagicMock

import pytest

from iac_code.services.telemetry.client import TelemetryClient
from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.metrics import MetricsRegistry
from iac_code.services.telemetry.names import Events, Metrics, Spans
from iac_code.services.telemetry.sink import AnalyticsSink
from iac_code.services.telemetry.tracing import SpanFactory


@pytest.fixture(autouse=True)
def _disable_default_backend(monkeypatch):
    """Prevent tests from exporting to the hardcoded default backend (ARMS).

    Without this, bootstrap() wires real OTLP exporters pointed at ARMS —
    flushes would attempt real HTTP calls during test teardown.
    """
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")


def test_default_construction_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = TelemetryClient()
    assert client is not None


def test_log_event_before_bootstrap_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = TelemetryClient()
    client.log_event(Events.SESSION_STARTED, {"headless": False})


def test_log_event_delegates_to_sink_when_active(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Override the autouse default-backend disable so emission flows through.
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    mock_emitter = MagicMock(spec=EventEmitter)
    sink = AnalyticsSink(mock_emitter)
    sink.activate()
    client = TelemetryClient(sink=sink)
    client.log_event(Events.SESSION_STARTED, {"headless": True})
    mock_emitter.emit.assert_called_once_with(Events.SESSION_STARTED, {"headless": True})


def test_add_metric_delegates_to_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    counter = MagicMock()
    registry = MetricsRegistry(instruments={Metrics.SESSION_COUNT: counter})
    client = TelemetryClient(metrics=registry)
    client.add_metric(Metrics.SESSION_COUNT, 1, {"os.type": "linux"})
    counter.add.assert_called_once_with(1, {"os.type": "linux"})


def test_start_span_delegates_to_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tracer = MagicMock()
    factory = SpanFactory()
    factory.attach(tracer)
    client = TelemetryClient(tracer=factory)
    with client.start_span(Spans.ENTRY, {"k": 1}):
        pass
    tracer.start_as_current_span.assert_called_once()


def test_bootstrap_with_no_default_endpoint_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("IAC_CODE_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    client = TelemetryClient()
    client.bootstrap()


def test_bootstrap_emits_iac_init_on_first_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Need the privacy gate open so iac.init reaches the emitter.
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    mock_emitter = MagicMock(spec=EventEmitter)
    sink = AnalyticsSink(mock_emitter)
    client = TelemetryClient(sink=sink)
    client.bootstrap()
    event_names = [call.args[0] for call in mock_emitter.emit.call_args_list]
    assert Events.INIT in event_names


def test_shutdown_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = TelemetryClient()
    client.bootstrap()
    client.shutdown()  # must not raise


def test_flush_force_flushes_without_closing_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = TelemetryClient()
    client.bootstrap()
    fake_meter = MagicMock()
    fake_logger = MagicMock()
    fake_tracer = MagicMock()
    client._meter_provider = fake_meter
    client._logger_provider = fake_logger
    client._tracer_provider = fake_tracer

    client.flush()

    fake_meter.force_flush.assert_called_once()
    fake_logger.force_flush.assert_called_once()
    fake_tracer.force_flush.assert_called_once()
    # Critical: providers stay open so subsequent tasks can still emit.
    fake_meter.shutdown.assert_not_called()
    fake_logger.shutdown.assert_not_called()
    fake_tracer.shutdown.assert_not_called()

    # Repeated flush must remain safe.
    client.flush()
    assert fake_meter.force_flush.call_count == 2


def test_flush_swallows_force_flush_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = TelemetryClient()
    client.bootstrap()
    bad = MagicMock()
    bad.force_flush.side_effect = RuntimeError("boom")
    client._meter_provider = bad
    client._logger_provider = None
    client._tracer_provider = None

    client.flush()  # must not raise


def test_facade_delegates_to_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from iac_code.services.telemetry import (
        add_metric,
        flush_telemetry,
        log_event,
        set_client,
    )

    mock_client = MagicMock(spec=TelemetryClient)
    set_client(mock_client)
    try:
        log_event(Events.SESSION_STARTED, {"k": 1})
        add_metric(Metrics.SESSION_COUNT, 1, {"os.type": "linux"})
        flush_telemetry()
        mock_client.log_event.assert_called_once_with(Events.SESSION_STARTED, {"k": 1})
        mock_client.add_metric.assert_called_once_with(Metrics.SESSION_COUNT, 1, {"os.type": "linux"})
        mock_client.flush.assert_called_once_with()
    finally:
        set_client(None)


def test_use_session_id_facade_overrides_get_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from iac_code.services.telemetry import bootstrap_telemetry, get_session_id, set_client, use_session_id
    from iac_code.services.telemetry.identity import SESSION_ID_PREFIX

    set_client(None)
    try:
        bootstrap_telemetry(session_id="process-id")
        assert get_session_id() == f"{SESSION_ID_PREFIX}process-id"
        with use_session_id("per-context"):
            assert get_session_id() == f"{SESSION_ID_PREFIX}per-context"
        assert get_session_id() == f"{SESSION_ID_PREFIX}process-id"
    finally:
        set_client(None)
