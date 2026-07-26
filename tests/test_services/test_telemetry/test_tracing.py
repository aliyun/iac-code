"""Tests for SpanFactory."""

from unittest.mock import MagicMock, patch

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


def test_start_detached_uses_explicit_parent_without_activating_span():
    tracer = MagicMock()
    parent = object()
    span = object()
    tracer.start_span.return_value = span
    factory = SpanFactory()
    factory.attach(tracer)

    assert factory.start_detached("iac.detached", {"k": 1}, parent_context=parent) is span

    tracer.start_span.assert_called_once_with(
        "iac.detached",
        context=parent,
        attributes={"k": 1},
    )
    tracer.start_as_current_span.assert_not_called()


def test_use_span_disables_automatic_exception_status_and_lifetime():
    span = object()
    context_manager = MagicMock()

    with patch("iac_code.services.telemetry.tracing.trace.use_span", return_value=context_manager) as use_span:
        assert SpanFactory.use(span) is context_manager

    use_span.assert_called_once_with(
        span,
        record_exception=False,
        set_status_on_exception=False,
        end_on_exit=False,
    )
