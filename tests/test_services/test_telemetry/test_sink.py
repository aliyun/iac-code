"""Tests for AnalyticsSink."""

from unittest.mock import MagicMock

import pytest

from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.sink import AnalyticsSink


@pytest.fixture
def mock_emitter():
    return MagicMock(spec=EventEmitter)


def test_log_event_before_attach_queues(mock_emitter, monkeypatch):
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    sink = AnalyticsSink(mock_emitter)
    sink.log_event("iac.test.early", {"k": 1})
    mock_emitter.emit.assert_not_called()  # queued, not yet drained
    sink.activate()
    sink.drain_sync()
    mock_emitter.emit.assert_called_once_with("iac.test.early", {"k": 1})


def test_log_event_after_activate_goes_direct(mock_emitter, monkeypatch):
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    sink = AnalyticsSink(mock_emitter)
    sink.activate()
    sink.log_event("iac.test.direct", {"k": 2})
    mock_emitter.emit.assert_called_once_with("iac.test.direct", {"k": 2})


def test_privacy_gate_blocks_when_no_telemetry(mock_emitter, monkeypatch):
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    sink = AnalyticsSink(mock_emitter)
    sink.activate()
    sink.log_event("iac.test", {"k": 1})
    mock_emitter.emit.assert_not_called()


def test_privacy_gate_blocks_under_essential_traffic(mock_emitter, monkeypatch):
    monkeypatch.setenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    sink = AnalyticsSink(mock_emitter)
    sink.activate()
    sink.log_event("iac.test", {"k": 1})
    mock_emitter.emit.assert_not_called()


def test_activate_is_idempotent(mock_emitter, monkeypatch):
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    sink = AnalyticsSink(mock_emitter)
    sink.activate()
    sink.activate()  # second call no-op
    sink.log_event("iac.test", {})
    assert mock_emitter.emit.call_count == 1
