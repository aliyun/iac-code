from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any


class PipelineTransportDeliveryClosedError(RuntimeError):
    pass


@dataclass
class PipelineTransportDeliveryTracker:
    active: bool = True
    pending: dict[int, _PendingDelivery] = field(default_factory=dict)


@dataclass
class _PendingDelivery:
    event: Any
    completion: asyncio.Future[None]
    tracker: PipelineTransportDeliveryTracker
    stage_observer: PipelineTransportDeliveryStageObserver | None = None


PipelineTransportDeliveryStageObserver = Callable[[str, int], None]


_PENDING_DELIVERIES: dict[int, _PendingDelivery] = {}
_ROUTED_DELIVERY_TRACKERS: dict[tuple[str, str], list[PipelineTransportDeliveryTracker]] = {}
_DELIVERY_TRACKER: contextvars.ContextVar[PipelineTransportDeliveryTracker | None] = contextvars.ContextVar(
    "pipeline_transport_delivery_tracker",
    default=None,
)
_DELIVERY_REQUIRED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pipeline_transport_delivery_required",
    default=False,
)


def create_pipeline_transport_delivery_tracker() -> PipelineTransportDeliveryTracker:
    return PipelineTransportDeliveryTracker()


@contextmanager
def bind_pipeline_transport_delivery_tracker(tracker: PipelineTransportDeliveryTracker) -> Iterator[None]:
    token = _DELIVERY_TRACKER.set(tracker)
    try:
        yield
    finally:
        _DELIVERY_TRACKER.reset(token)


@contextmanager
def bind_pipeline_transport_delivery_route(
    tracker: PipelineTransportDeliveryTracker,
    *,
    task_id: str,
    context_id: str,
) -> Iterator[None]:
    key = (task_id, context_id)
    trackers = _ROUTED_DELIVERY_TRACKERS.setdefault(key, [])
    trackers.append(tracker)
    try:
        yield
    finally:
        current = _ROUTED_DELIVERY_TRACKERS.get(key)
        if current is not None:
            with suppress(ValueError):
                current.remove(tracker)
            if not current:
                _ROUTED_DELIVERY_TRACKERS.pop(key, None)


def routed_pipeline_transport_delivery_tracker(
    *,
    task_id: str,
    context_id: str,
) -> PipelineTransportDeliveryTracker | None:
    trackers = _ROUTED_DELIVERY_TRACKERS.get((task_id, context_id), ())
    return next((tracker for tracker in reversed(trackers) if tracker.active), None)


def close_pipeline_transport_delivery_tracker(tracker: PipelineTransportDeliveryTracker) -> None:
    if not tracker.active:
        return
    tracker.active = False
    for event_id, pending in list(tracker.pending.items()):
        if _PENDING_DELIVERIES.get(event_id) is pending:
            _PENDING_DELIVERIES.pop(event_id, None)
        _notify_delivery_stage(pending, "closed")
        if not pending.completion.done():
            pending.completion.add_done_callback(_consume_delivery_exception)
            pending.completion.set_exception(
                PipelineTransportDeliveryClosedError("A2A streaming transport closed before frame delivery")
            )
    tracker.pending.clear()


@contextmanager
def pipeline_transport_delivery_tracking() -> Iterator[None]:
    tracker = create_pipeline_transport_delivery_tracker()
    try:
        with bind_pipeline_transport_delivery_tracker(tracker):
            yield
    finally:
        close_pipeline_transport_delivery_tracker(tracker)


def pipeline_transport_delivery_tracking_enabled() -> bool:
    return _DELIVERY_TRACKER.get() is not None


@contextmanager
def pipeline_transport_delivery_required() -> Iterator[None]:
    token = _DELIVERY_REQUIRED.set(True)
    try:
        yield
    finally:
        _DELIVERY_REQUIRED.reset(token)


def pipeline_transport_delivery_is_required() -> bool:
    return _DELIVERY_REQUIRED.get()


def register_pipeline_transport_delivery(
    event: Any,
    *,
    fallback_tracker: PipelineTransportDeliveryTracker | None = None,
    stage_observer: PipelineTransportDeliveryStageObserver | None = None,
) -> asyncio.Future[None]:
    completion = asyncio.get_running_loop().create_future()
    tracker = _DELIVERY_TRACKER.get()
    if (tracker is None or not tracker.active) and fallback_tracker is not None and fallback_tracker.active:
        tracker = fallback_tracker
    if tracker is None:
        if stage_observer is not None:
            now_ns = time.monotonic_ns()
            _notify_stage_observer(stage_observer, "registered", now_ns)
            _notify_stage_observer(stage_observer, "acknowledged", now_ns)
        completion.set_result(None)
        return completion
    if not tracker.active:
        if stage_observer is not None:
            now_ns = time.monotonic_ns()
            _notify_stage_observer(stage_observer, "registered", now_ns)
            _notify_stage_observer(stage_observer, "closed", now_ns)
        completion.add_done_callback(_consume_delivery_exception)
        completion.set_exception(
            PipelineTransportDeliveryClosedError("A2A streaming transport closed before frame delivery")
        )
        return completion
    event_id = id(event)
    previous = _PENDING_DELIVERIES.get(event_id)
    if previous is not None and previous.event is event:
        discard_pipeline_transport_delivery(event)
    pending = _PendingDelivery(
        event=event,
        completion=completion,
        tracker=tracker,
        stage_observer=stage_observer,
    )
    _PENDING_DELIVERIES[event_id] = pending
    tracker.pending[event_id] = pending
    _notify_delivery_stage(pending, "registered")
    return completion


def mark_pipeline_transport_delivery_enqueued(event: Any) -> None:
    pending = _pending_delivery(event)
    if pending is not None:
        _notify_delivery_stage(pending, "enqueued")


def mark_pipeline_transport_delivery_dequeued(event: Any) -> None:
    pending = _pending_delivery(event)
    if pending is not None:
        _notify_delivery_stage(pending, "dequeued")


def acknowledge_pipeline_transport_delivery(event: Any) -> None:
    pending = _pending_delivery(event)
    if pending is None:
        return
    _PENDING_DELIVERIES.pop(id(event), None)
    pending.tracker.pending.pop(id(event), None)
    _notify_delivery_stage(pending, "acknowledged")
    if not pending.completion.done():
        pending.completion.set_result(None)


def discard_pipeline_transport_delivery(event: Any) -> None:
    pending = _pending_delivery(event)
    if pending is None:
        return
    _PENDING_DELIVERIES.pop(id(event), None)
    pending.tracker.pending.pop(id(event), None)
    _notify_delivery_stage(pending, "discarded")
    if not pending.completion.done():
        pending.completion.cancel()


def _consume_delivery_exception(completion: asyncio.Future[None]) -> None:
    if not completion.cancelled():
        completion.exception()


def _pending_delivery(event: Any) -> _PendingDelivery | None:
    pending = _PENDING_DELIVERIES.get(id(event))
    return pending if pending is not None and pending.event is event else None


def _notify_delivery_stage(pending: _PendingDelivery, stage: str) -> None:
    if pending.stage_observer is not None:
        _notify_stage_observer(pending.stage_observer, stage, time.monotonic_ns())


def _notify_stage_observer(observer: PipelineTransportDeliveryStageObserver, stage: str, at_ns: int) -> None:
    try:
        observer(stage, at_ns)
    except Exception:
        return
