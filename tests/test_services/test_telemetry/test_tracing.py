"""Tests for SpanFactory."""

from unittest.mock import MagicMock

from iac_code.services.telemetry.tracing import SpanFactory


def test_start_delegates_to_attached_tracer():
    tracer = MagicMock()
    factory = SpanFactory()
    factory.attach(tracer)
    with factory.start("iac.test", {"k": 1}):
        pass
    tracer.start_as_current_span.assert_called_once_with("iac.test", attributes={"k": 1})


def test_start_is_safe_when_not_attached():
    factory = SpanFactory()
    # Must not raise even without attach.
    with factory.start("iac.test") as span:
        assert span is not None  # gets the OTel NonRecordingSpan


def test_start_default_attributes_empty_dict():
    tracer = MagicMock()
    factory = SpanFactory()
    factory.attach(tracer)
    with factory.start("iac.test"):
        pass
    tracer.start_as_current_span.assert_called_once_with("iac.test", attributes={})
