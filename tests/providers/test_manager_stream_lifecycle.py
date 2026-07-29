import asyncio
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from iac_code.providers.base import Message, NonStreamingResponse
from iac_code.providers.manager import ProviderManager
from iac_code.services.telemetry.names import Events, IacCodeAttr, PipelineAttr
from iac_code.services.telemetry.scope import get_span_attributes, use_span_attributes
from iac_code.types.stream_events import MessageEndEvent, MessageStartEvent, TextDeltaEvent, Usage


class FatalProviderError(BaseException):
    pass


class FatalCloseError(BaseException):
    pass


class RecordingSpan:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.attributes: dict[str, Any] = {}
        self.exceptions: list[BaseException] = []
        self.statuses: list[Any] = []
        self.end_calls = 0

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)

    def set_status(self, status: Any) -> None:
        self.statuses.append(status)

    def end(self) -> None:
        self.end_calls += 1
        self.order.append("end")


class ControlledIterator:
    def __init__(
        self,
        items: list[Any],
        *,
        order: list[str] | None = None,
        close_exc: BaseException | None = None,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        self.items = iter(items)
        self.order = order if order is not None else []
        self.close_exc = close_exc
        self.close_gate = close_gate
        self.close_calls = 0
        self.close_completed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            item = next(self.items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.close_calls += 1
        self.order.append("close")
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_exc is not None:
            raise self.close_exc
        self.close_completed = True


class FakeProvider:
    _PROVIDER_KEY = "anthropic"
    _logical_provider_key = "anthropic"

    def __init__(
        self,
        iterator: ControlledIterator,
        *,
        completion: NonStreamingResponse | BaseException | None = None,
    ) -> None:
        self.iterator = iterator
        self.completion = completion or NonStreamingResponse(
            message_id="fallback",
            text="fallback text",
            tool_uses=[],
            stop_reason="end_turn",
            usage=Usage(),
        )
        self.complete_calls = 0

    def stream(self, messages, system, tools=None, max_tokens=8192):
        return self.iterator

    async def complete(self, messages, system, tools=None, max_tokens=8192, **kwargs):
        self.complete_calls += 1
        if isinstance(self.completion, BaseException):
            raise self.completion
        return self.completion


def _manager(monkeypatch, provider: FakeProvider, *, model: str = "claude-sonnet-4-6") -> ProviderManager:
    monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *args, **kwargs: provider)
    return ProviderManager(model=model, credentials={"anthropic": "fake"})


def _record_telemetry(monkeypatch, order: list[str] | None = None):
    events: list[tuple[str, dict[str, Any]]] = []
    metrics: list[tuple[str, int | float, dict[str, Any]]] = []

    def log_event(name: str, attrs: dict[str, Any]) -> None:
        events.append((name, attrs))
        if order is not None and name in {Events.API_REQUEST_SUCCEEDED, Events.API_REQUEST_FAILED}:
            order.append("terminal")

    monkeypatch.setattr("iac_code.providers.manager.log_event", log_event)
    monkeypatch.setattr(
        "iac_code.providers.manager.add_metric",
        lambda name, value, attrs: metrics.append((name, value, attrs)),
    )
    return events, metrics


def _install_span(monkeypatch, span: RecordingSpan) -> list[dict[str, Any]]:
    activations: list[dict[str, Any]] = []
    monkeypatch.setattr("iac_code.providers.manager.start_detached_span", lambda *args, **kwargs: span)

    def activate(raw_span, **kwargs):
        activations.append(kwargs)
        return nullcontext(raw_span)

    monkeypatch.setattr("iac_code.providers.manager.use_span", activate)
    return activations


@pytest.mark.asyncio
async def test_stream_success_commits_closes_and_ends_before_terminal_delivery(monkeypatch) -> None:
    order: list[str] = []
    iterator = ControlledIterator(
        [MessageStartEvent(message_id="m1"), MessageEndEvent(stop_reason="end_turn", usage=Usage())],
        order=order,
    )
    manager = _manager(monkeypatch, FakeProvider(iterator))
    span = RecordingSpan(order)
    activations = _install_span(monkeypatch, span)
    events, _metrics = _record_telemetry(monkeypatch, order)
    stream = manager.stream([Message.user("hi")], "system")

    first = await anext(stream)
    assert isinstance(first, MessageStartEvent)
    terminal = await anext(stream)
    order.append("delivered")

    assert isinstance(terminal, MessageEndEvent)
    assert order[-4:] == ["terminal", "close", "end", "delivered"]
    assert iterator.close_calls == 1
    assert iterator.close_completed is True
    assert span.end_calls == 1
    assert [name for name, _ in events].count(Events.API_REQUEST_SUCCEEDED) == 1
    assert [name for name, _ in events].count(Events.API_REQUEST_FAILED) == 0
    assert activations
    assert all(
        activation == {"record_exception": False, "set_status_on_exception": False, "end_on_exit": False}
        for activation in activations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [[], [MessageStartEvent(message_id="partial"), TextDeltaEvent(text="partial")]],
    ids=["zero-events", "partial-events"],
)
async def test_stream_natural_eof_is_one_failed_attempt_then_fallback(monkeypatch, items) -> None:
    iterator = ControlledIterator(items)
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    events, _metrics = _record_telemetry(monkeypatch)

    output = [event async for event in manager.stream([Message.user("hi")], "system")]

    failures = [attrs for name, attrs in events if name == Events.API_REQUEST_FAILED]
    assert len(failures) == 1
    assert failures[0]["status"] == "error"
    assert iterator.close_calls == 1
    assert span.end_calls == 1
    assert span.exceptions == []
    assert len(span.statuses) == 1
    assert provider.complete_calls == 1
    assert output[-1].type == "message_end"
    if items:
        assert any(event.type == "tombstone" for event in output)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_in_other_task", [False, True], ids=["same-task", "cross-task"])
async def test_suspended_stream_close_is_cancelled_once_and_closes_provider(monkeypatch, close_in_other_task) -> None:
    iterator = ControlledIterator([MessageStartEvent(message_id="partial")])
    manager = _manager(monkeypatch, FakeProvider(iterator))
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    events, _metrics = _record_telemetry(monkeypatch)
    stream = manager.stream([Message.user("hi")], "system")

    assert isinstance(await anext(stream), MessageStartEvent)
    if close_in_other_task:
        await asyncio.create_task(stream.aclose())
    else:
        await stream.aclose()

    failures = [attrs for name, attrs in events if name == Events.API_REQUEST_FAILED]
    assert [item["status"] for item in failures] == ["cancelled"]
    assert iterator.close_calls == 1
    assert span.end_calls == 1
    assert span.exceptions == []
    assert span.statuses == []


@pytest.mark.asyncio
async def test_stream_capture_belongs_to_calling_scope_and_fallback_restores_consumer_context(monkeypatch) -> None:
    iterator = ControlledIterator([MessageStartEvent(message_id="partial"), RuntimeError("stream failed")])
    manager = _manager(monkeypatch, FakeProvider(iterator))
    span = RecordingSpan()
    captured_parents: list[Any] = []

    def start_span(*args, **kwargs):
        captured_parents.append(kwargs["parent_context"])
        return span

    monkeypatch.setattr("iac_code.providers.manager.start_detached_span", start_span)
    monkeypatch.setattr("iac_code.providers.manager.use_span", lambda raw_span, **kwargs: nullcontext(raw_span))
    telemetry_events, _metrics = _record_telemetry(monkeypatch)
    seen_in_fallback: list[tuple[dict[str, Any], str | None]] = []

    async def completion(*args, **kwargs):
        seen_in_fallback.append((get_span_attributes(), otel_context.get_value("test.owner")))
        return SimpleNamespace(
            response=NonStreamingResponse(
                message_id="fallback",
                text="ok",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(),
            )
        )

    monkeypatch.setattr(manager, "_complete_with_retry_result", completion)
    parent_a = otel_context.set_value("test.owner", "A")
    token_a = otel_context.attach(parent_a)
    try:
        with use_span_attributes({IacCodeAttr.MODE: "pipeline", PipelineAttr.RUN_ID: "run-a"}):
            stream = manager.stream([Message.user("hi")], "system")
    finally:
        otel_context.detach(token_a)

    async def consume_in_b():
        parent_b = otel_context.set_value("test.owner", "B")
        token_b = otel_context.attach(parent_b)
        try:
            with use_span_attributes({IacCodeAttr.MODE: "normal", PipelineAttr.RUN_ID: "run-b"}):
                observed = []
                for _ in range(3):
                    observed.append(await anext(stream))
                    assert get_span_attributes() == {
                        IacCodeAttr.MODE: "normal",
                        PipelineAttr.RUN_ID: "run-b",
                    }
                    assert otel_context.get_value("test.owner") == "B"
                return observed
        finally:
            otel_context.detach(token_b)

    output = await asyncio.create_task(consume_in_b())

    assert [event.type for event in output] == ["message_start", "tombstone", "message_start"]
    assert otel_context.get_value("test.owner", context=captured_parents[0]) == "A"
    assert seen_in_fallback == [({IacCodeAttr.MODE: "pipeline", PipelineAttr.RUN_ID: "run-a"}, "A")]
    failure = next(attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED)
    assert failure[IacCodeAttr.MODE] == "pipeline"
    assert failure[PipelineAttr.RUN_ID] == "run-a"


@pytest.mark.asyncio
async def test_stream_and_fallback_use_real_otel_parent_a_and_restore_parent_b(monkeypatch) -> None:
    iterator = ControlledIterator([MessageStartEvent(message_id="partial"), RuntimeError("stream failed")])
    manager = _manager(monkeypatch, FakeProvider(iterator))
    telemetry_events, _metrics = _record_telemetry(monkeypatch)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    monkeypatch.setattr(
        "iac_code.providers.manager.start_detached_span",
        lambda name, attributes, parent_context: tracer.start_span(
            name,
            context=parent_context,
            attributes=attributes,
        ),
    )
    monkeypatch.setattr("iac_code.providers.manager.use_span", otel_trace.use_span)

    @contextmanager
    def start_current(name, attributes=None):
        with tracer.start_as_current_span(name, attributes=attributes or {}) as span:
            yield span

    monkeypatch.setattr("iac_code.providers.manager.start_span", start_current)

    with tracer.start_as_current_span("parent-a") as parent_a:
        parent_a_id = parent_a.get_span_context().span_id
        with use_span_attributes({IacCodeAttr.MODE: "pipeline", PipelineAttr.RUN_ID: "run-a"}):
            stream = manager.stream([Message.user("hi")], "system")

    parent_b_ids: list[int] = []

    async def consume_in_b() -> list[Any]:
        with tracer.start_as_current_span("parent-b") as parent_b:
            parent_b_id = parent_b.get_span_context().span_id
            parent_b_ids.append(parent_b_id)
            with use_span_attributes({IacCodeAttr.MODE: "normal", PipelineAttr.RUN_ID: "run-b"}):
                output = [event async for event in stream]
                assert otel_trace.get_current_span().get_span_context().span_id == parent_b_id
                assert get_span_attributes() == {
                    IacCodeAttr.MODE: "normal",
                    PipelineAttr.RUN_ID: "run-b",
                }
            return output

    output = await asyncio.create_task(consume_in_b())
    finished = exporter.get_finished_spans()
    request_spans = [span for span in finished if span.name not in {"parent-a", "parent-b"}]

    assert [event.type for event in output][-2:] == ["text_delta", "message_end"]
    assert len(request_spans) == 2
    assert {span.parent.span_id for span in request_spans if span.parent is not None} == {parent_a_id}
    assert all(span.parent is not None and span.parent.span_id != parent_b_ids[0] for span in request_spans)
    failure = next(attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED)
    assert failure[IacCodeAttr.MODE] == "pipeline"
    assert failure[PipelineAttr.RUN_ID] == "run-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_exc", "raised"),
    [
        (RuntimeError("close failed"), None),
        (asyncio.CancelledError(), asyncio.CancelledError),
        (FatalCloseError("fatal close"), FatalCloseError),
    ],
    ids=["ordinary-exception", "self-cancellation", "fatal-base-exception"],
)
async def test_success_keeps_terminal_when_close_fails(monkeypatch, close_exc, raised) -> None:
    iterator = ControlledIterator(
        [MessageEndEvent(stop_reason="end_turn", usage=Usage())],
        close_exc=close_exc,
    )
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    if raised is None:
        output = [event async for event in manager.stream([Message.user("hi")], "system")]
        assert [event.type for event in output] == ["message_end"]
    else:
        with pytest.raises(raised):
            await anext(manager.stream([Message.user("hi")], "system"))

    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == 1
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 0
    assert iterator.close_calls == 1
    assert span.end_calls == 1
    assert provider.complete_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_buffered_types"),
    [
        ("claude-sonnet-4-6", ["message_start", "text_delta"]),
        ("claude-fable-5", []),
    ],
    ids=["unbuffered", "buffered"],
)
async def test_refusal_commits_closes_and_discards_buffer_before_fallback(
    monkeypatch,
    model,
    expected_buffered_types,
) -> None:
    iterator = ControlledIterator(
        [
            MessageStartEvent(message_id="refused"),
            TextDeltaEvent(text="discard me"),
            MessageEndEvent(stop_reason="refusal", usage=Usage()),
        ]
    )
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider, model=model)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    output = [event async for event in manager.stream([Message.user("hi")], "system")]

    assert [event.type for event in output[: len(expected_buffered_types)]] == expected_buffered_types
    if model == "claude-fable-5":
        assert all(getattr(event, "text", None) != "discard me" for event in output)
    else:
        assert any(getattr(event, "text", None) == "discard me" for event in output)
    assert all(not isinstance(event, MessageEndEvent) or event.stop_reason != "refusal" for event in output)
    assert iterator.close_calls == 1
    assert iterator.close_completed is True
    assert span.end_calls == 1
    refusal = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED]
    assert refusal[0]["status"] == "refusal"
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 0


@pytest.mark.asyncio
async def test_buffered_success_is_terminal_before_consumer_closes_after_first_delivered_event(monkeypatch) -> None:
    iterator = ControlledIterator(
        [
            MessageStartEvent(message_id="accepted"),
            TextDeltaEvent(text="accepted text"),
            MessageEndEvent(stop_reason="end_turn", usage=Usage()),
        ]
    )
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider, model="claude-fable-5")
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)
    stream = manager.stream([Message.user("hi")], "system")

    assert isinstance(await anext(stream), MessageStartEvent)
    await stream.aclose()

    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == 1
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 0
    assert iterator.close_calls == 1
    assert span.end_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_exc", "raised"),
    [
        (asyncio.CancelledError(), asyncio.CancelledError),
        (FatalCloseError("fatal close"), FatalCloseError),
    ],
    ids=["self-cancellation", "fatal-base-exception"],
)
async def test_natural_eof_does_not_fallback_when_close_control_flow_propagates(
    monkeypatch,
    close_exc,
    raised,
) -> None:
    iterator = ControlledIterator([], close_exc=close_exc)
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    with pytest.raises(raised):
        await anext(manager.stream([Message.user("hi")], "system"))

    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 1
    assert iterator.close_calls == 1
    assert span.end_calls == 1
    assert provider.complete_calls == 0


@pytest.mark.asyncio
async def test_consumed_provider_error_does_not_hide_iterator_close_cancellation(monkeypatch) -> None:
    iterator = ControlledIterator(
        [RuntimeError("provider failed")],
        close_exc=asyncio.CancelledError(),
    )
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await anext(manager.stream([Message.user("hi")], "system"))

    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 1
    assert iterator.close_calls == 1
    assert span.end_calls == 1
    assert provider.complete_calls == 0


@pytest.mark.asyncio
async def test_fatal_provider_error_wins_over_secondary_fatal_close(monkeypatch) -> None:
    primary = FatalProviderError("primary")
    iterator = ControlledIterator([primary], close_exc=FatalCloseError("secondary"))
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    with pytest.raises(FatalProviderError) as exc_info:
        await anext(manager.stream([Message.user("hi")], "system"))

    assert exc_info.value is primary
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 1
    assert span.exceptions == [primary]
    assert len(span.statuses) == 1
    assert iterator.close_calls == 1
    assert span.end_calls == 1
    assert provider.complete_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_kind", ["success", "failed"])
async def test_external_cancellation_during_close_preserves_committed_terminal(monkeypatch, terminal_kind) -> None:
    close_gate = asyncio.Event()
    item: Any = (
        MessageEndEvent(stop_reason="end_turn", usage=Usage())
        if terminal_kind == "success"
        else RuntimeError("provider failed")
    )
    iterator = ControlledIterator([item], close_gate=close_gate)
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)
    task = asyncio.create_task(anext(manager.stream([Message.user("hi")], "system")))

    while iterator.close_calls == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert iterator.close_calls == 1
    assert iterator.close_completed is False
    assert span.end_calls == 1
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == (
        1 if terminal_kind == "success" else 0
    )
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == (
        0 if terminal_kind == "success" else 1
    )
    assert provider.complete_calls == 0


@pytest.mark.asyncio
async def test_inflight_cancel_records_one_cancelled_terminal_and_runs_provider_finally(monkeypatch) -> None:
    finalized = asyncio.Event()

    class WaitingProvider(FakeProvider):
        def stream(self, messages, system, tools=None, max_tokens=8192):
            async def wait_forever():
                try:
                    await asyncio.Event().wait()
                    yield MessageEndEvent(stop_reason="never", usage=Usage())
                finally:
                    finalized.set()

            return wait_forever()

    provider = WaitingProvider(ControlledIterator([]))
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)
    stream = manager.stream([Message.user("hi")], "system")
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()

    assert finalized.is_set()
    failures = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
    assert [item["status"] for item in failures] == ["cancelled"]
    assert span.end_calls == 1
    assert provider.complete_calls == 0


@pytest.mark.asyncio
async def test_concurrent_direct_aclose_runtime_error_is_not_a_manager_terminal(monkeypatch) -> None:
    entered = asyncio.Event()
    finalized = asyncio.Event()

    class WaitingProvider(FakeProvider):
        def stream(self, messages, system, tools=None, max_tokens=8192):
            async def wait_forever():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield MessageEndEvent(stop_reason="never", usage=Usage())
                finally:
                    finalized.set()

            return wait_forever()

    provider = WaitingProvider(ControlledIterator([]))
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)
    stream = manager.stream([Message.user("hi")], "system")
    owner = asyncio.create_task(anext(stream))
    await entered.wait()

    with pytest.raises(RuntimeError, match="already running"):
        await stream.aclose()

    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == 0
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 0

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert finalized.is_set()
    failures = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
    assert [attrs["status"] for attrs in failures] == ["cancelled"]
    assert span.end_calls == 1


@pytest.mark.asyncio
async def test_cross_task_close_uses_creation_scope_and_restores_closing_scope(monkeypatch) -> None:
    iterator = ControlledIterator([MessageStartEvent(message_id="partial")])
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    captured_parents: list[Any] = []

    def start_span(*args, **kwargs):
        captured_parents.append(kwargs["parent_context"])
        return span

    monkeypatch.setattr("iac_code.providers.manager.start_detached_span", start_span)
    monkeypatch.setattr("iac_code.providers.manager.use_span", lambda raw_span, **kwargs: nullcontext(raw_span))
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    parent_a = otel_context.set_value("test.owner", "A")
    token_a = otel_context.attach(parent_a)
    try:
        with use_span_attributes({IacCodeAttr.MODE: "pipeline", PipelineAttr.RUN_ID: "run-a"}):
            stream = manager.stream([Message.user("hi")], "system")
    finally:
        otel_context.detach(token_a)

    async def consume_and_close_in_b() -> None:
        parent_b = otel_context.set_value("test.owner", "B")
        token_b = otel_context.attach(parent_b)
        try:
            with use_span_attributes({IacCodeAttr.MODE: "normal", PipelineAttr.RUN_ID: "run-b"}):
                assert isinstance(await anext(stream), MessageStartEvent)
                await stream.aclose()
                assert get_span_attributes() == {
                    IacCodeAttr.MODE: "normal",
                    PipelineAttr.RUN_ID: "run-b",
                }
                assert otel_context.get_value("test.owner") == "B"
        finally:
            otel_context.detach(token_b)

    await asyncio.create_task(consume_and_close_in_b())

    failure = next(attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED)
    assert failure["status"] == "cancelled"
    assert failure[IacCodeAttr.MODE] == "pipeline"
    assert failure[PipelineAttr.RUN_ID] == "run-a"
    assert otel_context.get_value("test.owner", context=captured_parents[0]) == "A"
    assert iterator.close_calls == 1
    assert span.end_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_result", ["raise", "none"])
async def test_stream_iterator_creation_failure_and_none_guard_fall_back_once(monkeypatch, creation_result) -> None:
    class InvalidStreamProvider(FakeProvider):
        def stream(self, messages, system, tools=None, max_tokens=8192):
            if creation_result == "raise":
                raise RuntimeError("stream creation failed")
            return None

    provider = InvalidStreamProvider(ControlledIterator([]))
    manager = _manager(monkeypatch, provider)
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    output = [event async for event in manager.stream([Message.user("hi")], "system")]

    assert [event.type for event in output][-3:] == ["message_start", "text_delta", "message_end"]
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_STARTED) == 2
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 1
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == 1
    assert provider.complete_calls == 1
    assert span.end_calls == 1


@pytest.mark.asyncio
async def test_span_end_exception_does_not_change_success_outcome(monkeypatch) -> None:
    class FailingEndSpan(RecordingSpan):
        def end(self) -> None:
            super().end()
            raise RuntimeError("span end failed")

    iterator = ControlledIterator([MessageEndEvent(stop_reason="end_turn", usage=Usage())])
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    span = FailingEndSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    output = [event async for event in manager.stream([Message.user("hi")], "system")]

    assert [event.type for event in output] == ["message_end"]
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == 1
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_FAILED) == 0
    assert span.end_calls == 1


@pytest.mark.asyncio
async def test_stream_works_with_noop_otel_span(monkeypatch) -> None:
    iterator = ControlledIterator([MessageEndEvent(stop_reason="end_turn", usage=Usage())])
    provider = FakeProvider(iterator)
    manager = _manager(monkeypatch, provider)
    monkeypatch.setattr(
        "iac_code.providers.manager.start_detached_span",
        lambda *args, **kwargs: otel_trace.NonRecordingSpan(otel_trace.INVALID_SPAN_CONTEXT),
    )
    monkeypatch.setattr("iac_code.providers.manager.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.providers.manager.add_metric", lambda *args, **kwargs: None)

    output = [event async for event in manager.stream([Message.user("hi")], "system")]

    assert [event.type for event in output] == ["message_end"]
    assert iterator.close_calls == 1


@pytest.mark.asyncio
async def test_complete_fatal_base_exception_has_one_failed_terminal_and_no_retry(monkeypatch) -> None:
    fatal = FatalProviderError("fatal completion")
    provider = FakeProvider(ControlledIterator([]), completion=fatal)
    manager = _manager(monkeypatch, provider)
    manager._retry_config.max_retries = 2
    span = RecordingSpan()
    monkeypatch.setattr("iac_code.providers.manager.start_span", lambda *args, **kwargs: nullcontext(span))
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    with pytest.raises(FatalProviderError) as exc_info:
        await manager.complete([Message.user("hi")], "system")

    assert exc_info.value is fatal
    assert provider.complete_calls == 1
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_STARTED) == 1
    failures = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
    assert len(failures) == 1
    assert failures[0]["status"] == "error"


@pytest.mark.asyncio
async def test_recursive_completion_fallback_fatal_base_exception_has_one_terminal_per_attempt(monkeypatch) -> None:
    class Status503Error(Exception):
        status_code = 503

    class PrimaryProvider:
        _PROVIDER_KEY = "anthropic"
        _logical_provider_key = "anthropic"

        async def complete(self, messages, system, tools=None, max_tokens=8192, **kwargs):
            raise Status503Error("primary unavailable")

    class FatalFallbackProvider:
        _PROVIDER_KEY = "anthropic"
        _logical_provider_key = "anthropic"

        async def complete(self, messages, system, tools=None, max_tokens=8192, **kwargs):
            raise FatalProviderError("fatal fallback")

    created_models: list[str] = []

    def create(model, *args, **kwargs):
        created_models.append(model)
        return PrimaryProvider() if model == "claude-sonnet-4-6" else FatalFallbackProvider()

    monkeypatch.setattr("iac_code.providers.manager.create_provider", create)
    manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "fake"})
    manager._retry_config.max_retries = 0
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    with pytest.raises(FatalProviderError, match="fatal fallback"):
        await manager.complete([Message.user("hi")], "system")

    assert created_models == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_STARTED) == 2
    failures = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
    assert len(failures) == 2
    assert [attrs["status"] for attrs in failures] == ["error", "error"]


@pytest.mark.asyncio
async def test_stream_completion_fallback_fatal_base_exception_is_not_converted_to_error_event(monkeypatch) -> None:
    fatal = FatalProviderError("fatal stream fallback")
    provider = FakeProvider(
        ControlledIterator([MessageStartEvent(message_id="partial"), RuntimeError("stream failed")]),
        completion=fatal,
    )
    manager = _manager(monkeypatch, provider)
    manager._retry_config.max_retries = 0
    span = RecordingSpan()
    _install_span(monkeypatch, span)
    telemetry_events, _metrics = _record_telemetry(monkeypatch)

    with pytest.raises(FatalProviderError) as exc_info:
        _ = [event async for event in manager.stream([Message.user("hi")], "system")]

    assert exc_info.value is fatal
    assert [name for name, _ in telemetry_events].count(Events.API_REQUEST_STARTED) == 2
    failures = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
    assert len(failures) == 2
    assert [attrs["status"] for attrs in failures] == ["error", "error"]
    assert provider.complete_calls == 1


@pytest.mark.asyncio
async def test_qwenpaw_configuration_check_still_runs_on_first_consumption(monkeypatch) -> None:
    iterator = ControlledIterator([MessageEndEvent(stop_reason="end_turn", usage=Usage())])
    manager = _manager(monkeypatch, FakeProvider(iterator))
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(manager, "_check_qwenpaw_config_change", check)
    stream = manager.stream([Message.user("hi")], "system")
    assert calls == 0

    await anext(stream)

    assert calls == 1
