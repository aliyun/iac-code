from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from iac_code.a2a.pipeline_delta_coalescing import coalesce_pipeline_delta_envelopes_by_source
from iac_code.a2a.pipeline_flow_monitor import PipelineA2AFlowItem
from iac_code.a2a.pipeline_transport_delivery import pipeline_transport_delivery_required
from iac_code.types.stream_events import (
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
)

OUTBOUND_MAX_BATCH_EVENTS = 64
OUTBOUND_MAX_BATCH_BYTES = 65_536
OUTBOUND_MAX_BATCH_DELAY_SECONDS = 0.020
OUTBOUND_HARD_MAX_BATCH_EVENTS = 1_024
OUTBOUND_HARD_MAX_BATCH_BYTES = 1_048_576
_EVENT_SIZE_ESTIMATE_NODE_BUDGET = 256
_UNKNOWN_VALUE_ESTIMATED_BYTES = 64

_DEFAULT_SOURCE: tuple[str] = ("default",)
logger = logging.getLogger(__name__)


@dataclass
class _OutboundItem:
    arrival_no: int
    source_key: Hashable
    enqueued_at: float
    enqueued_at_ns: int
    estimated_bytes: int
    event: Any
    after_delivery: Callable[[], None] | None = None


@dataclass
class _ReadyBatch:
    items: list[_OutboundItem]
    trigger_reason: str


@dataclass
class _ControlItem:
    arrival_no: int
    close: bool
    completion: asyncio.Future[None]


@dataclass
class _CallbackItem:
    arrival_no: int
    callback: Callable[[], Awaitable[Any]]
    completion: asyncio.Future[Any]


@dataclass
class _PermissionItem:
    item: _OutboundItem
    decision: asyncio.Task[bool]
    prefix_items: list[_OutboundItem]
    resolver_used: bool


_ReadyOperation = _ReadyBatch | _ControlItem | _CallbackItem
_SenderOperation = _ReadyOperation | _PermissionItem


class PipelineA2AOutboundAbortedError(RuntimeError):
    pass


class PipelineA2AOutboundOperationCancelledError(RuntimeError):
    pass


class PipelineA2AOutboundQueue:
    def __init__(
        self,
        publisher: Any,
        *,
        max_batch_events: int = OUTBOUND_MAX_BATCH_EVENTS,
        max_batch_bytes: int = OUTBOUND_MAX_BATCH_BYTES,
        max_batch_delay_seconds: float = OUTBOUND_MAX_BATCH_DELAY_SECONDS,
    ) -> None:
        self._publisher = publisher
        self._flow_monitor = getattr(publisher, "flow_monitor", None)
        self._max_batch_events = max_batch_events
        self._max_batch_bytes = max_batch_bytes
        self._max_batch_delay_seconds = max(0.0, max_batch_delay_seconds)
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._source_queues: dict[Hashable, deque[_OutboundItem]] = {}
        self._ready: deque[_ReadyOperation] = deque()
        self._permissions: deque[_PermissionItem] = deque()
        self._blocked_source_counts: dict[Hashable, int] = {}
        self._arrival_no = 0
        self._sender_busy = False
        self._worker: asyncio.Task[None] | None = None
        self._delay_task: asyncio.Task[None] | None = None
        self._close_future: asyncio.Future[None] | None = None
        self._fatal_exception: BaseException | None = None
        self._abort_exception: PipelineA2AOutboundAbortedError | None = None
        self._closing = False

    async def start(self) -> None:
        if self._worker is not None:
            return
        if self._closing:
            raise RuntimeError("Outbound queue is closed")
        self._worker = asyncio.create_task(self._run(), name="pipeline-a2a-outbound")
        if self._flow_monitor is not None:
            try:
                await self._flow_monitor.start()
            except Exception:
                logger.warning("Failed to start A2A pipeline flow monitor", exc_info=True)
                self._flow_monitor = None

    async def submit(
        self,
        event: Any,
        *,
        permission_resolver: Any = None,
        auto_approve_permissions: bool = False,
        after_delivery: Callable[[], None] | None = None,
    ) -> str | None:
        self._ensure_started()
        enqueued_at_ns = time.monotonic_ns()
        estimated_bytes = _estimated_event_size(event, limit=max(1, self._max_batch_bytes))
        async with self._lock:
            self._raise_if_failed()
            if self._closing:
                raise RuntimeError("Outbound queue is closed")

            item = _OutboundItem(
                arrival_no=self._next_arrival_no(),
                source_key=_source_key(event),
                enqueued_at=enqueued_at_ns / 1_000_000_000,
                enqueued_at_ns=enqueued_at_ns,
                estimated_bytes=estimated_bytes,
                event=event,
                after_delivery=after_delivery,
            )
            if _permission_request_from(event) is not None:
                self._enqueue_permission_locked(
                    item,
                    permission_resolver=permission_resolver,
                    auto_approve_permissions=auto_approve_permissions,
                )
            else:
                self._source_queues.setdefault(item.source_key, deque()).append(item)
                self._freeze_if_triggered_locked()
            if self._flow_monitor is not None:
                self._flow_monitor.event_enqueued(_flow_item(item))
            self._wakeup.set()

        return _text_delta_value(event)

    async def run_serialized(self, callback: Callable[[], Awaitable[Any]]) -> Any:
        self._ensure_started()
        completion = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._raise_if_failed()
            if self._closing:
                raise RuntimeError("Outbound queue is closed")
            self._freeze_active_locked(trigger_reason="callback")
            self._ready.append(
                _CallbackItem(
                    arrival_no=self._next_arrival_no(),
                    callback=callback,
                    completion=completion,
                )
            )
            self._wakeup.set()
        return await self._await_completion(completion)

    async def flush(self) -> None:
        self._ensure_started()
        completion = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._raise_if_failed()
            if self._closing:
                raise RuntimeError("Outbound queue is closed")
            self._freeze_active_locked(trigger_reason="flush")
            self._ready.append(
                _ControlItem(
                    arrival_no=self._next_arrival_no(),
                    close=False,
                    completion=completion,
                )
            )
            self._wakeup.set()
        await self._await_completion(completion)

    async def close(self) -> None:
        if self._worker is None:
            return

        aborted = True
        try:
            async with self._lock:
                self._raise_if_failed()
                if self._close_future is None:
                    self._closing = True
                    self._close_future = asyncio.get_running_loop().create_future()
                    self._freeze_active_locked(trigger_reason="close")
                    self._ready.append(
                        _ControlItem(
                            arrival_no=self._next_arrival_no(),
                            close=True,
                            completion=self._close_future,
                        )
                    )
                    self._wakeup.set()
                close_future = self._close_future

            try:
                await asyncio.shield(close_future)
            except asyncio.CancelledError:
                close_future.add_done_callback(_consume_future_exception)
                raise
            await asyncio.shield(self._worker)
            await self._cancel_delay_task()
            aborted = False
        finally:
            await self._close_flow_monitor(aborted=aborted)

    async def abort(self) -> None:
        if self._worker is None:
            return

        error = PipelineA2AOutboundAbortedError("Outbound queue aborted")
        async with self._lock:
            self._closing = True
            self._abort_exception = error
            if self._fatal_exception is None:
                self._fatal_exception = error
            self._cancel_permission_decisions_locked()
            self._wakeup.set()

        await self._cancel_delay_task()
        if not self._worker.done():
            self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass

        async with self._lock:
            self._fail_pending_locked(self._fatal_exception or error)
        await self._close_flow_monitor(aborted=True)

    async def _close_flow_monitor(self, *, aborted: bool) -> None:
        if self._flow_monitor is None:
            return
        try:
            await self._flow_monitor.close(aborted=aborted)
        except Exception:
            logger.warning("Failed to close A2A pipeline flow monitor", exc_info=True)

    async def _run(self) -> None:
        current: _SenderOperation | None = None
        try:
            while True:
                current = await self._next_operation()
                should_close = isinstance(current, _ControlItem) and current.close
                await self._execute_operation(current)
                async with self._lock:
                    self._sender_busy = False
                    self._wakeup.set()
                current = None
                if should_close:
                    return
        except asyncio.CancelledError as exc:
            failure = self._operation_failure(exc)
            async with self._lock:
                self._sender_busy = False
                if current is not None:
                    self._fail_operation_locked(current, failure)
                self._fail_pending_locked(failure)
            if self._abort_exception is not None:
                raise
        except BaseException as exc:
            self._fatal_exception = exc
            async with self._lock:
                self._sender_busy = False
                if current is not None:
                    self._fail_operation_locked(current, exc)
                self._fail_pending_locked(exc)

    async def _next_operation(self) -> _SenderOperation:
        while True:
            async with self._lock:
                operation = self._select_operation_locked()
                if operation is not None:
                    self._sender_busy = True
                    return operation
                self._wakeup.clear()
            await self._wakeup.wait()

    def _select_operation_locked(self) -> _SenderOperation | None:
        boundary_arrival = self._first_boundary_arrival_locked()
        permission = self._first_deliverable_permission_locked(boundary_arrival)
        if permission is not None:
            if permission.prefix_items:
                prefix = permission.prefix_items
                permission.prefix_items = []
                return _ReadyBatch(prefix, trigger_reason="permission_prefix")
            if permission.decision.done():
                self._permissions.remove(permission)
                return permission

        if self._ready:
            ready = self._ready[0]
            if isinstance(ready, _ReadyBatch):
                return self._take_ready_batch_locked()
            if isinstance(ready, (_ControlItem, _CallbackItem)):
                prior_batch = self._snapshot_active_before_locked(ready.arrival_no)
                if prior_batch is not None:
                    return prior_batch
                if self._has_permission_before_locked(ready.arrival_no):
                    return None
            return self._ready.popleft()

        batch = self._snapshot_active_locked(trigger_reason="idle")
        if batch is not None:
            return batch
        return None

    def _take_ready_batch_locked(self) -> _ReadyBatch:
        first = self._ready.popleft()
        assert isinstance(first, _ReadyBatch)

        items = list(first.items)
        merged = False
        while self._ready and isinstance(self._ready[0], _ReadyBatch):
            ready = self._ready.popleft()
            assert isinstance(ready, _ReadyBatch)
            items.extend(ready.items)
            merged = True

        boundary_arrival = self._first_boundary_arrival_locked()
        if boundary_arrival is None:
            active = self._snapshot_active_locked(trigger_reason="backlog")
        else:
            active = self._snapshot_active_before_locked(boundary_arrival)
        if active is not None:
            items.extend(active.items)
            merged = True

        if merged:
            items.sort(key=lambda item: item.arrival_no)
        return _ReadyBatch(items, trigger_reason="backlog" if merged else first.trigger_reason)

    def _first_boundary_arrival_locked(self) -> int | None:
        for operation in self._ready:
            if isinstance(operation, (_ControlItem, _CallbackItem)):
                return operation.arrival_no
        return None

    def _first_deliverable_permission_locked(self, boundary_arrival: int | None) -> _PermissionItem | None:
        pending_sources: set[Hashable] = set()
        for permission in self._permissions:
            if boundary_arrival is not None and permission.item.arrival_no > boundary_arrival:
                return None
            if permission.item.source_key in pending_sources:
                continue
            if permission.prefix_items or permission.decision.done():
                return permission
            pending_sources.add(permission.item.source_key)
        return None

    def _has_permission_before_locked(self, arrival_no: int) -> bool:
        return any(permission.item.arrival_no < arrival_no for permission in self._permissions)

    async def _execute_operation(self, operation: _SenderOperation) -> None:
        if isinstance(operation, _ReadyBatch):
            await self._publish_batch(operation)
            return
        if isinstance(operation, _PermissionItem):
            await self._publish_permission(operation)
            return
        if isinstance(operation, _CallbackItem):
            await self._run_callback(operation)
            return
        if not operation.completion.done():
            operation.completion.set_result(None)

    async def _publish_batch(self, batch: _ReadyBatch) -> None:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        monitor = self._flow_monitor
        if monitor is not None:
            monitor.batch_started([_flow_item(item) for item in batch.items], trigger_reason=batch.trigger_reason)
        try:
            persisted = await self._publisher.persist_batch_events([item.event for item in batch.items])
            if monitor is not None:
                monitor.phase_started("coalesce")
            network_envelopes = coalesce_pipeline_delta_envelopes_by_source(persisted)
            if monitor is not None:
                monitor.phase_started("a2a_internal_queue")
            frame_count_result = await self._publisher.enqueue_persisted_batch(
                network_envelopes,
                wait_for_transport=True,
            )
            network_frames = (
                int(frame_count_result) if isinstance(frame_count_result, int) else int(bool(network_envelopes))
            )
        except BaseException:
            if monitor is not None:
                monitor.batch_failed()
            raise
        if monitor is not None:
            monitor.batch_completed(
                persisted_envelopes=len(persisted),
                wire_envelopes=len(network_envelopes),
                network_frames=network_frames,
            )
        for item in batch.items:
            self._run_after_delivery(item)
        oldest_age_ms = max(0.0, (loop.time() - min(item.enqueued_at for item in batch.items)) * 1_000)
        logger.debug(
            "A2A pipeline outbound batch trigger=%s raw_events=%d persisted_envelopes=%d wire_envelopes=%d "
            "network_frames=%d estimated_bytes=%d oldest_age_ms=%.2f send_duration_ms=%.2f",
            batch.trigger_reason,
            len(batch.items),
            len(persisted),
            len(network_envelopes),
            network_frames,
            sum(item.estimated_bytes for item in batch.items),
            oldest_age_ms,
            (loop.time() - started_at) * 1_000,
        )

    async def _publish_permission(self, permission: _PermissionItem) -> None:
        prepared = None
        delivered = False
        monitor = self._flow_monitor
        if monitor is not None:
            monitor.batch_started([_flow_item(permission.item)], trigger_reason="permission")
        try:
            approved = permission.decision.result()
            prepared = await self._publisher.prepare_permission_event(
                permission.item.event,
                approved=approved,
                resolver_used=permission.resolver_used,
            )
            if monitor is not None:
                monitor.phase_started("a2a_internal_queue")
            delivered = await self._publisher.enqueue_prepared_permission(prepared)
            self._publisher.complete_prepared_permission(prepared, delivered=delivered)
            if monitor is not None:
                envelope_count = len(prepared.envelopes)
                monitor.batch_completed(
                    persisted_envelopes=envelope_count,
                    wire_envelopes=envelope_count,
                    network_frames=envelope_count if delivered else 0,
                )
            self._run_after_delivery(permission.item)
            logger.debug(
                "A2A pipeline permission delivered approved=%s queued_ms=%.2f",
                approved,
                max(0.0, (asyncio.get_running_loop().time() - permission.item.enqueued_at) * 1_000),
            )
        except BaseException:
            if monitor is not None:
                monitor.batch_failed()
            if prepared is None:
                self._publisher.fail_permission_event(permission.item.event)
            else:
                self._publisher.complete_prepared_permission(prepared, delivered=False)
            raise
        finally:
            async with self._lock:
                self._unblock_source_locked(permission.item.source_key)
                self._freeze_if_triggered_locked()

    async def _run_callback(self, item: _CallbackItem) -> None:
        try:
            with pipeline_transport_delivery_required():
                result = await item.callback()
            if not item.completion.done():
                item.completion.set_result(result)
        except BaseException as exc:
            if not item.completion.done():
                item.completion.set_exception(self._operation_failure(exc))
            raise

    def _enqueue_permission_locked(
        self,
        item: _OutboundItem,
        *,
        permission_resolver: Any,
        auto_approve_permissions: bool,
    ) -> None:
        prefix_items = self._extract_source_items_locked(item.source_key)
        decision = asyncio.create_task(
            self._publisher.resolve_permission_event(
                item.event,
                permission_resolver=permission_resolver,
                auto_approve_permissions=auto_approve_permissions,
            ),
            name=f"pipeline-a2a-permission-{item.arrival_no}",
        )
        decision.add_done_callback(self._permission_decision_done)
        self._permissions.append(
            _PermissionItem(
                item=item,
                decision=decision,
                prefix_items=prefix_items,
                resolver_used=permission_resolver is not None,
            )
        )
        self._blocked_source_counts[item.source_key] = self._blocked_source_counts.get(item.source_key, 0) + 1
        self._reschedule_delay_locked()

    def _extract_source_items_locked(self, source_key: Hashable) -> list[_OutboundItem]:
        extracted: list[_OutboundItem] = []
        source_queue = self._source_queues.pop(source_key, None)
        if source_queue:
            extracted.extend(source_queue)

        last_boundary_index = max(
            (
                index
                for index, operation in enumerate(self._ready)
                if isinstance(operation, (_ControlItem, _CallbackItem))
            ),
            default=-1,
        )
        retained_operations: deque[_ReadyOperation] = deque()
        for index, operation in enumerate(self._ready):
            if not isinstance(operation, _ReadyBatch) or index <= last_boundary_index:
                retained_operations.append(operation)
                continue
            retained_items: list[_OutboundItem] = []
            for item in operation.items:
                if item.source_key == source_key:
                    extracted.append(item)
                else:
                    retained_items.append(item)
            if retained_items:
                retained_operations.append(_ReadyBatch(retained_items, trigger_reason=operation.trigger_reason))
        self._ready = retained_operations
        extracted.sort(key=lambda item: item.arrival_no)
        return extracted

    def _freeze_if_triggered_locked(self) -> None:
        count, estimated_bytes, oldest = self._active_totals_locked()
        if count == 0:
            self._reschedule_delay_locked()
            return
        if not self._sender_busy and not self._ready:
            self._freeze_active_locked(trigger_reason="idle")
            return
        if count >= self._max_batch_events or estimated_bytes >= self._max_batch_bytes:
            trigger_reason = "count" if count >= self._max_batch_events else "bytes"
            self._freeze_active_locked(trigger_reason=trigger_reason)
            return
        if oldest is not None and asyncio.get_running_loop().time() - oldest >= self._max_batch_delay_seconds:
            self._freeze_active_locked(trigger_reason="time")
            return
        self._reschedule_delay_locked()

    def _freeze_active_locked(self, *, trigger_reason: str) -> None:
        batch = self._snapshot_active_locked(trigger_reason=trigger_reason)
        if batch is not None:
            self._ready.append(batch)

    def _snapshot_active_locked(self, *, trigger_reason: str) -> _ReadyBatch | None:
        items: list[_OutboundItem] = []
        empty_sources: list[Hashable] = []
        for source_key, source_queue in self._source_queues.items():
            if self._blocked_source_counts.get(source_key, 0) > 0:
                continue
            items.extend(source_queue)
            empty_sources.append(source_key)
        for source_key in empty_sources:
            self._source_queues.pop(source_key, None)
        if not items:
            self._reschedule_delay_locked()
            return None
        items.sort(key=lambda item: item.arrival_no)
        self._reschedule_delay_locked()
        return _ReadyBatch(items, trigger_reason=trigger_reason)

    def _snapshot_active_before_locked(self, arrival_no: int) -> _ReadyBatch | None:
        items: list[_OutboundItem] = []
        empty_sources: list[Hashable] = []
        for source_key, source_queue in self._source_queues.items():
            if self._blocked_source_counts.get(source_key, 0) > 0:
                continue
            retained: deque[_OutboundItem] = deque()
            for item in source_queue:
                if item.arrival_no < arrival_no:
                    items.append(item)
                else:
                    retained.append(item)
            if retained:
                self._source_queues[source_key] = retained
            else:
                empty_sources.append(source_key)
        for source_key in empty_sources:
            self._source_queues.pop(source_key, None)
        if not items:
            return None
        items.sort(key=lambda item: item.arrival_no)
        self._reschedule_delay_locked()
        return _ReadyBatch(items, trigger_reason="boundary")

    def _active_totals_locked(self) -> tuple[int, int, float | None]:
        count = 0
        estimated_bytes = 0
        oldest: float | None = None
        for source_key, source_queue in self._source_queues.items():
            if self._blocked_source_counts.get(source_key, 0) > 0:
                continue
            count += len(source_queue)
            estimated_bytes += sum(item.estimated_bytes for item in source_queue)
            if source_queue:
                source_oldest = source_queue[0].enqueued_at
                oldest = source_oldest if oldest is None else min(oldest, source_oldest)
        return count, estimated_bytes, oldest

    def _reschedule_delay_locked(self) -> None:
        if self._delay_task is not None and not self._delay_task.done():
            self._delay_task.cancel()
        self._delay_task = None
        count, _estimated_bytes, oldest = self._active_totals_locked()
        if count == 0 or oldest is None:
            return
        delay = max(0.0, oldest + self._max_batch_delay_seconds - asyncio.get_running_loop().time())
        self._delay_task = asyncio.create_task(self._freeze_after_delay(delay), name="pipeline-a2a-batch-delay")

    async def _freeze_after_delay(self, delay: float) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._delay_task is current:
                    self._delay_task = None
                self._freeze_active_locked(trigger_reason="time")
                self._wakeup.set()
        except asyncio.CancelledError:
            return

    async def _cancel_delay_task(self) -> None:
        task = self._delay_task
        self._delay_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _unblock_source_locked(self, source_key: Hashable) -> None:
        count = self._blocked_source_counts.get(source_key, 0)
        if count <= 1:
            self._blocked_source_counts.pop(source_key, None)
        else:
            self._blocked_source_counts[source_key] = count - 1

    def _cancel_permission_decisions_locked(self) -> None:
        for permission in self._permissions:
            if not permission.decision.done():
                permission.decision.cancel()

    def _permission_decision_done(self, task: asyncio.Task[bool]) -> None:
        self._wakeup.set()
        if not task.cancelled():
            task.exception()

    def _fail_pending_locked(self, exc: BaseException) -> None:
        if self._delay_task is not None and not self._delay_task.done():
            self._delay_task.cancel()
        self._delay_task = None
        self._cancel_permission_decisions_locked()
        for operation in self._ready:
            self._fail_operation_locked(operation, exc)
        self._ready.clear()
        self._source_queues.clear()
        for permission in self._permissions:
            self._fail_operation_locked(permission, exc)
        self._permissions.clear()
        if self._close_future is not None and not self._close_future.done():
            self._close_future.set_exception(exc)

    def _fail_operation_locked(self, operation: _SenderOperation, exc: BaseException) -> None:
        if isinstance(operation, (_ControlItem, _CallbackItem)):
            if not operation.completion.done():
                operation.completion.set_exception(exc)
            return
        if isinstance(operation, _PermissionItem):
            self._publisher.fail_permission_event(operation.item.event)

    def _run_after_delivery(self, item: _OutboundItem) -> None:
        if item.after_delivery is not None:
            item.after_delivery()

    async def _await_completion(self, completion: asyncio.Future[Any]) -> Any:
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(completion)
                break
            except asyncio.CancelledError as exc:
                if completion.cancelled():
                    raise
                cancellation = exc
        if cancellation is not None:
            raise cancellation
        return result

    def _operation_failure(self, exc: BaseException) -> BaseException:
        if self._abort_exception is not None:
            return self._abort_exception
        if not isinstance(exc, asyncio.CancelledError):
            return exc
        if self._fatal_exception is None:
            self._fatal_exception = PipelineA2AOutboundOperationCancelledError(
                "Outbound publisher operation was cancelled"
            )
        return self._fatal_exception

    def _next_arrival_no(self) -> int:
        self._arrival_no += 1
        return self._arrival_no

    def _ensure_started(self) -> None:
        if self._worker is None:
            raise RuntimeError("Outbound queue has not been started")

    def _raise_if_failed(self) -> None:
        if self._fatal_exception is not None:
            raise self._fatal_exception


def _source_key(event: Any) -> Hashable:
    path: list[tuple[str, int]] = []
    while isinstance(event, SubPipelineStreamEvent):
        path.append((event.sub_pipeline_id, event.candidate_index))
        event = event.inner
    return ("candidate", *path) if path else _DEFAULT_SOURCE


def _permission_request_from(event: Any) -> PermissionRequestEvent | None:
    event = _unwrap_stream_event(event)
    return event if isinstance(event, PermissionRequestEvent) else None


def _text_delta_value(event: Any) -> str | None:
    event = _unwrap_stream_event(event)
    return event.text if isinstance(event, TextDeltaEvent) else None


def _estimated_event_size(event: Any, *, limit: int) -> int:
    limit = max(1, limit)
    try:
        estimated = _estimate_value_size(
            event,
            remaining=limit,
            node_budget=[_EVENT_SIZE_ESTIMATE_NODE_BUDGET],
            active_ids=set(),
        )
    except Exception:
        return limit
    return max(1, min(limit, estimated))


def _estimate_value_size(
    value: Any,
    *,
    remaining: int,
    node_budget: list[int],
    active_ids: set[int],
) -> int:
    if remaining <= 0:
        return 0
    node_budget[0] -= 1
    if node_budget[0] < 0:
        return remaining
    if value is None:
        return min(remaining, 4)
    if isinstance(value, bool):
        return min(remaining, 5)
    if isinstance(value, str):
        if len(value) >= remaining:
            return remaining
        return min(remaining, len(value.encode("utf-8")))
    if isinstance(value, bytes | bytearray | memoryview):
        return min(remaining, len(value))
    if isinstance(value, int | float):
        return min(remaining, 32)

    value_id = id(value)
    if value_id in active_ids:
        return min(remaining, _UNKNOWN_VALUE_ESTIMATED_BYTES)
    active_ids.add(value_id)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            total = min(remaining, 2)
            for field in fields(value):
                total += min(remaining - total, len(field.name) + 3)
                if total >= remaining:
                    break
                total += _estimate_value_size(
                    getattr(value, field.name),
                    remaining=remaining - total,
                    node_budget=node_budget,
                    active_ids=active_ids,
                )
                if total >= remaining:
                    break
            return total
        if isinstance(value, dict):
            total = min(remaining, 2)
            for key, item in value.items():
                total += _estimate_value_size(
                    key,
                    remaining=remaining - total,
                    node_budget=node_budget,
                    active_ids=active_ids,
                )
                if total >= remaining:
                    break
                total += _estimate_value_size(
                    item,
                    remaining=remaining - total,
                    node_budget=node_budget,
                    active_ids=active_ids,
                )
                if total >= remaining:
                    break
                total += min(remaining - total, 2)
            return total
        if isinstance(value, list | tuple | set | frozenset):
            total = min(remaining, 2)
            for item in value:
                total += _estimate_value_size(
                    item,
                    remaining=remaining - total,
                    node_budget=node_budget,
                    active_ids=active_ids,
                )
                if total >= remaining:
                    break
                total += min(remaining - total, 1)
            return total
        return min(remaining, _UNKNOWN_VALUE_ESTIMATED_BYTES)
    finally:
        active_ids.remove(value_id)


def _flow_item(item: _OutboundItem) -> PipelineA2AFlowItem:
    return PipelineA2AFlowItem(
        arrival_no=item.arrival_no,
        enqueued_at_ns=item.enqueued_at_ns,
        estimated_bytes=item.estimated_bytes,
    )


def _unwrap_stream_event(event: Any) -> Any:
    while isinstance(event, SubPipelineStreamEvent):
        event = event.inner
    return event


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()
