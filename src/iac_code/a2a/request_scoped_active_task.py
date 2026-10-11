"""Request-scoped event subscriptions for the A2A SDK ActiveTask runtime."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from a2a.server.agent_execution.active_task import (
    TERMINAL_TASK_STATES,
    ActiveTask,
    EventConsumer,
    _RequestCompleted,
    _RequestStarted,
)
from a2a.server.agent_execution.active_task_registry import ActiveTaskRegistry
from a2a.server.events.event_queue_v2 import QueueShutDown
from a2a.server.tasks.task_manager import TaskManager
from a2a.types import Task, TaskState, TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError

from iac_code.a2a.backup import await_fenced
from iac_code.a2a.execution_control import RecoverableInputAdmissionCarrier
from iac_code.utils.public_errors import sanitize_strict_text

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from a2a.server.agent_execution import RequestContext
    from a2a.server.context import ServerCallContext
    from a2a.server.events import Event


logger = logging.getLogger(__name__)


class _AcceptedRequestEventConsumer(EventConsumer):
    """Record completion only after the SDK projection and fan-out succeed."""

    def __init__(self, active_task: RequestScopedActiveTask) -> None:
        super().__init__(active_task)
        self._scoped = active_task

    async def run(self) -> None:
        try:
            await super().run()
        except BaseException:
            self._scoped._accepted_consumer_failed = True
            raise

    async def _process_event(self, event: Event) -> None:
        try:
            await super()._process_event(event)
        except BaseException:
            self._scoped._accepted_consumer_failed = True
            raise
        if isinstance(event, _RequestCompleted):
            self._scoped._pending_accepted_requests.discard(event.request_id)

    async def _enqueue_to_subscribers(self, event: Event, updated_task: Task | None) -> None:
        # SDK run() catches failures (including dequeue errors) and fans out the
        # exception. A normally returning run() therefore is not health proof.
        if isinstance(event, BaseException):
            self._scoped._accepted_consumer_failed = True
        try:
            await super()._enqueue_to_subscribers(event, updated_task)
        except BaseException:
            self._scoped._accepted_consumer_failed = True
            raise


class RequestScopedActiveTask(ActiveTask):
    """Hide events from earlier requests until this request actually starts."""

    def __init__(
        self,
        *args: Any,
        recovery_admission: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._direct_message_lock = asyncio.Lock()
        self._recovery_admission = recovery_admission
        self._recovery_replacement_pending = False
        self._pending_accepted_requests: set[uuid.UUID] = set()
        self._accepted_consumer_failed = False

    @property
    def direct_message_lock(self) -> asyncio.Lock:
        """Serialize direct messages injected into the running executor."""

        return self._direct_message_lock

    async def retire_for_recovery(self) -> None:
        """Stop an idle SDK lifecycle before reopening its durable task."""

        async with self._lock:
            self._is_finished.set()
            self._request_queue.shutdown(immediate=True)
            lifecycle_tasks = self._retirement_tasks()

        await self._finish_retirement(lifecycle_tasks)

    def _retirement_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        current = asyncio.current_task()
        return tuple(
            task for task in (self._producer_task, self._consumer_task) if task is not None and task is not current
        )

    async def _finish_retirement(self, lifecycle_tasks: tuple[asyncio.Task[Any], ...]) -> None:
        await await_fenced(self._finish_retirement_owned(lifecycle_tasks))

    async def _finish_retirement_owned(self, lifecycle_tasks: tuple[asyncio.Task[Any], ...]) -> None:
        if lifecycle_tasks:
            logger.warning(
                "A2A SDK lifecycle retirement canceling tasks task_id=%s task_names=%s",
                sanitize_strict_text(self._task_id),
                ",".join(sanitize_strict_text(task.get_name()) for task in lifecycle_tasks),
            )
        for task in lifecycle_tasks:
            if not task.done():
                task.cancel()
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        await self._event_queue_agent.close(immediate=True)
        await self._event_queue_subscribers.close(immediate=True)

    async def enqueue_request(self, request_context: RequestContext):
        admission = RecoverableInputAdmissionCarrier.read(request_context)
        async with self._lock:
            if self._recovery_replacement_pending:
                raise InvalidParamsError(f"Task {self._task_id} recovery replacement is pending.")
            if self._recovery_admission is not None and admission != self._recovery_admission:
                raise InvalidParamsError(f"Task {self._task_id} recovery continuation is reserved.")
            request_id = uuid.uuid4()
            self._pending_accepted_requests.add(request_id)
            try:
                await self._request_queue.put((request_context, request_id))
            except BaseException:
                self._pending_accepted_requests.discard(request_id)
                raise
            if self._recovery_admission is not None:
                self._recovery_admission = None
        RecoverableInputAdmissionCarrier.acknowledge_enqueued(request_context, self._producer_task)
        return request_id

    def has_unfinished_requests(self) -> bool:
        """Report SDK requests accepted by this lifecycle but not yet completed."""

        return self._queue_has_unfinished_tasks(self._request_queue)

    def has_unsettled_requests(self) -> bool:
        """Report accepted requests whose SDK projection or fan-out is not settled."""

        return bool(
            self._pending_accepted_requests
            or self._accepted_consumer_failed
            or self.has_unfinished_requests()
            or self._request_lock.locked()
            or self._queue_has_unfinished_tasks(getattr(self._event_queue_agent, "_incoming_queue", None))
            or self._queue_has_unfinished_tasks(getattr(self._event_queue_agent, "queue", None))
        )

    @staticmethod
    def _queue_has_unfinished_tasks(queue: Any) -> bool:
        if queue is None:
            return False
        unfinished = getattr(queue, "unfinished_tasks", None)
        if isinstance(unfinished, int):
            return unfinished > 0
        return bool(getattr(queue, "_unfinished_tasks", 0))

    async def _run_consumer(self) -> None:
        # Preserve the SDK lifecycle cleanup while selecting the receipt-aware
        # consumer. Producer and request completion events remain SDK-owned.
        try:
            try:
                await _AcceptedRequestEventConsumer(self).run()
            finally:
                self._is_finished.set()
                self._request_queue.shutdown(immediate=True)
                await self._event_queue_agent.close(immediate=True)
                async with self._lock:
                    self._reference_count -= 1
                await self._maybe_cleanup()
        except BaseException:
            self._accepted_consumer_failed = True
            raise

    def _require_accepted_requests_settled(self) -> None:
        if self.has_unsettled_requests():
            raise self._accepted_lifecycle_error()

    def accepted_settlement_diagnostic(self) -> dict[str, Any]:
        """Project rejection state without request IDs, exception text or payloads."""

        consumer_state, consumer_error_type = self._accepted_settlement_task_state(self._consumer_task)
        producer_state, producer_error_type = self._accepted_settlement_task_state(self._producer_task)
        return {
            "task_id": self._accepted_settlement_protocol_id(self._task_id),
            "pending_request_count": len(self._pending_accepted_requests),
            "consumer_failed": self._accepted_consumer_failed is True,
            "request_lock_locked": self._request_lock.locked(),
            "request_queue_unfinished": self._accepted_settlement_queue_count(self._request_queue),
            "incoming_unfinished": self._accepted_settlement_queue_count(
                getattr(self._event_queue_agent, "_incoming_queue", None)
            ),
            "default_sink_unfinished": self._accepted_settlement_queue_count(
                getattr(self._event_queue_agent, "queue", None)
            ),
            "consumer_state": consumer_state,
            "consumer_error_type": consumer_error_type,
            "producer_state": producer_state,
            "producer_error_type": producer_error_type,
            "is_finished": self._is_finished.is_set(),
            "recovery_pending": self._recovery_replacement_pending is True,
        }

    @staticmethod
    def _accepted_settlement_protocol_id(value: Any) -> str:
        if type(value) is str and re.fullmatch(
            r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|task-[0-9a-f]{12,32})",
            value,
        ):
            return value
        return "UNKNOWN"

    @staticmethod
    def _accepted_settlement_queue_count(queue: Any) -> int | str:
        count = getattr(queue, "unfinished_tasks", None)
        if count is None:
            count = getattr(queue, "_unfinished_tasks", None)
        return count if type(count) is int and 0 <= count <= 2147483647 else "UNKNOWN"

    @staticmethod
    def _accepted_settlement_task_state(task: asyncio.Task[Any] | None) -> tuple[str, str]:
        if task is None:
            return "ABSENT", "NONE"
        if not task.done():
            return "RUNNING", "NONE"
        if task.cancelled():
            return "CANCELLED", "CancelledError"
        error = task.exception()
        if error is None:
            return "FINISHED", "NONE"
        error_types: dict[type[BaseException], str] = {
            RuntimeError: "RuntimeError",
            ValueError: "ValueError",
            TypeError: "TypeError",
            OSError: "OSError",
            InvalidParamsError: "InvalidParamsError",
            QueueShutDown: "QueueShutDown",
        }
        return "FAILED", error_types.get(type(error), "UNKNOWN")

    def _accepted_lifecycle_error(self) -> InvalidParamsError:
        try:
            diagnostic = self.accepted_settlement_diagnostic()
            logger.warning(
                "A2AAcceptedRequestSettlementRejected task_id=%s pending_request_count=%s consumer_failed=%s "
                "request_lock_locked=%s request_queue_unfinished=%s incoming_unfinished=%s "
                "default_sink_unfinished=%s consumer_state=%s consumer_error_type=%s "
                "producer_state=%s producer_error_type=%s is_finished=%s recovery_pending=%s",
                diagnostic["task_id"],
                diagnostic["pending_request_count"],
                diagnostic["consumer_failed"],
                diagnostic["request_lock_locked"],
                diagnostic["request_queue_unfinished"],
                diagnostic["incoming_unfinished"],
                diagnostic["default_sink_unfinished"],
                diagnostic["consumer_state"],
                diagnostic["consumer_error_type"],
                diagnostic["producer_state"],
                diagnostic["producer_error_type"],
                diagnostic["is_finished"],
                diagnostic["recovery_pending"],
            )
        except Exception:
            # Observation must never replace the original admission failure.
            pass
        return InvalidParamsError(f"Task {self._task_id} lifecycle ended before the accepted request settled.")

    async def wait_for_accepted_requests(self) -> None:
        """Require exact SDK completions and wait for their real projection."""

        projection = asyncio.create_task(
            self._wait_for_accepted_request_settlement(),
            name=f"accepted-request-projection:{self._task_id}",
        )
        consumer = self._consumer_task
        try:
            if consumer is None:
                raise self._accepted_lifecycle_error()
            done, _ = await asyncio.wait((projection, consumer), return_when=asyncio.FIRST_COMPLETED)
            if consumer in done:
                if consumer.cancelled() or consumer.exception() is not None:
                    raise self._accepted_lifecycle_error()
                self._require_accepted_requests_settled()
            # Normal consumer completion can precede the projection's remaining
            # join awaits. Exact receipts allow waiting, never queue emptiness.
            await projection
            self._require_accepted_requests_settled()
        finally:
            if not projection.done():
                projection.cancel()
            await asyncio.gather(projection, return_exceptions=True)

    async def _wait_for_accepted_request_settlement(self) -> None:
        await self._request_queue.join()
        await self._wait_for_accepted_request_projection()

    async def _wait_for_accepted_request_projection(self) -> None:
        join_incoming = getattr(self._event_queue_agent, "test_only_join_incoming_queue", None)
        if callable(join_incoming):
            await join_incoming()
        agent_queue = getattr(self._event_queue_agent, "queue", None)
        join_agent_queue = getattr(agent_queue, "join", None)
        if callable(join_agent_queue):
            await join_agent_queue()

    async def subscribe(
        self,
        *,
        request: RequestContext | None = None,
        include_initial_task: bool = False,
        replace_status_update_with_task: bool = False,
    ) -> AsyncGenerator[Event, None]:
        async with self._lock:
            if self._is_finished.is_set():
                raise InvalidParamsError(f"Task {self._task_id} is already completed.")
            self._reference_count += 1

        tapped_queue = None
        request_id = None
        request_started = request is None
        pre_start_error: BaseException | None = None

        try:
            tapped_queue = await self._event_queue_subscribers.tap()
            if request is not None:
                request_id = await self.enqueue_request(request)

            if include_initial_task:
                yield await self.get_task()

            while True:
                try:
                    event, updated_task = cast("Any", await tapped_queue.dequeue_event())
                except QueueShutDown:
                    if request is not None and not request_started:
                        if pre_start_error is not None:
                            raise pre_start_error
                        raise InvalidParamsError(f"Task {self._task_id} ended before the request started.")
                    break
                except asyncio.CancelledError:
                    break

                try:
                    if isinstance(event, DirectMessageRequestStarted):
                        continue
                    if isinstance(event, _RequestStarted):
                        if request_id is not None and event.request_id == request_id:
                            request_started = True
                        continue
                    if not request_started:
                        if isinstance(event, BaseException):
                            pre_start_error = event
                        continue
                    if isinstance(event, BaseException):
                        raise event
                    if isinstance(event, _RequestCompleted):
                        if request_id is not None and event.request_id == request_id:
                            return
                        continue
                    if self.is_stale_terminal_projection(event, updated_task):
                        continue
                    if replace_status_update_with_task and isinstance(event, TaskStatusUpdateEvent):
                        event = updated_task
                    yield cast("Event", event)
                finally:
                    tapped_queue.task_done()
        finally:
            if tapped_queue is not None:
                await tapped_queue.close(immediate=True)
            async with self._lock:
                self._reference_count -= 1
            await self._maybe_cleanup()

    @staticmethod
    def is_stale_terminal_projection(event: Any, updated_task: Any) -> bool:
        """Drop a terminal event that the canonical task store rejected as stale."""

        return bool(
            isinstance(event, (Task, TaskStatusUpdateEvent))
            and event.status.state in TERMINAL_TASK_STATES
            and isinstance(updated_task, Task)
            and updated_task.status.state not in TERMINAL_TASK_STATES
        )


class DirectMessageRequestStarted:
    """Internal fan-out fence separating a direct request from older events."""


class DirectPipelineRouteOutcome(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    RECOVERY_REQUIRED = "recovery_required"


class DirectPipelineRouteGate:
    """Bind a direct stream boundary to the running pipeline accepting its input."""

    def __init__(self) -> None:
        self.marker = DirectMessageRequestStarted()
        self.outcome = DirectPipelineRouteOutcome.PENDING

    async def activate(self, event_queue: Any) -> None:
        if self.outcome is not DirectPipelineRouteOutcome.PENDING:
            return
        await event_queue.enqueue_event(self.marker)
        self.outcome = DirectPipelineRouteOutcome.ACTIVE

    def require_recovery(self) -> None:
        if self.outcome is DirectPipelineRouteOutcome.PENDING:
            self.outcome = DirectPipelineRouteOutcome.RECOVERY_REQUIRED


class DirectPipelineRouteGateCarrier:
    """Attach the transport's route gate to one direct RequestContext."""

    _ATTRIBUTE = "_iac_code_direct_pipeline_route_gate"

    @classmethod
    def attach(cls, request_context: Any, gate: DirectPipelineRouteGate) -> None:
        setattr(request_context, cls._ATTRIBUTE, gate)

    @classmethod
    def read(cls, request_context: Any) -> DirectPipelineRouteGate | None:
        gate = getattr(request_context, cls._ATTRIBUTE, None)
        return gate if isinstance(gate, DirectPipelineRouteGate) else None


class PipelineLifecycleEventQueueCarrier:
    """Mark a RequestContext whose SDK lifecycle owns its event queue."""

    _ATTRIBUTE = "_iac_code_pipeline_lifecycle_event_queue"
    _BOUND_ATTRIBUTE = "_iac_code_pipeline_lifecycle_event_queue_bound"

    @classmethod
    def attach(cls, request_context: Any) -> None:
        setattr(request_context, cls._ATTRIBUTE, True)

    @classmethod
    def read(cls, request_context: Any) -> bool:
        return getattr(request_context, cls._ATTRIBUTE, False) is True

    @classmethod
    def mark_bound(cls, request_context: Any) -> None:
        setattr(request_context, cls._BOUND_ATTRIBUTE, True)

    @classmethod
    def is_bound(cls, request_context: Any) -> bool:
        return getattr(request_context, cls._BOUND_ATTRIBUTE, False) is True


class DirectPipelineRecoveryRequiredError(RuntimeError):
    """The old owner won terminal publication, so this request must recover."""


class RequestScopedActiveTaskRegistry(ActiveTaskRegistry):
    """Create request-scoped ActiveTask instances while retaining SDK lifecycle rules."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._recoveries_in_progress: set[str] = set()

    async def reconcile_and_replace_for_recovery(
        self,
        task_id: str,
        *,
        call_context: ServerCallContext,
        context_id: str,
        acquire_admission: Callable[[], Awaitable[str | None]],
        release_admission: Callable[[str], Awaitable[None]] | None = None,
    ) -> str | None:
        """Claim durable recovery and replace the old lifecycle in one local critical section."""

        scoped = await self._reserve_recovery_replacement(task_id)
        admission = None
        replacement = None
        retirement_started = False
        published = False
        try:
            if scoped is not None:
                await self._drain_accepted_recovery_predecessors(scoped, task_id, call_context)
            admission = await acquire_admission()
            if admission is None:
                return None

            lifecycle_tasks = await self._begin_recovery_retirement(task_id, scoped)
            retirement_started = scoped is not None
            if scoped is not None:
                await scoped._finish_retirement(lifecycle_tasks)
            replacement = self._create_active_task(
                task_id=task_id,
                call_context=call_context,
                context_id=context_id,
                recovery_admission=admission,
            )
            await replacement.start(call_context=call_context, create_task_if_missing=True)
            await self._publish_recovery_replacement(task_id, scoped, replacement)
            published = True
            return admission
        except BaseException:
            await await_fenced(
                self._abort_recovery_replacement(
                    replacement=replacement,
                    admission=admission,
                    release_admission=release_admission,
                )
            )
            raise
        finally:
            await await_fenced(
                self._clear_recovery_replacement(
                    task_id,
                    scoped,
                    remove_finished=retirement_started and not published,
                )
            )

    async def _reserve_recovery_replacement(self, task_id: str) -> RequestScopedActiveTask | None:
        async with self._lock:
            if task_id in self._recoveries_in_progress:
                raise InvalidParamsError(f"Task {task_id} recovery replacement is pending.")
            self._recoveries_in_progress.add(task_id)
            existing = self._active_tasks.get(task_id)
            scoped = cast("RequestScopedActiveTask | None", existing)
            if scoped is not None and scoped._recovery_replacement_pending:
                self._recoveries_in_progress.discard(task_id)
                raise InvalidParamsError(f"Task {task_id} recovery replacement is pending.")
            if scoped is not None:
                scoped._recovery_replacement_pending = True

        try:
            if scoped is not None:
                # An enqueue that crossed the fence already owns predecessor
                # status. Wait for its short critical section without holding
                # the registry-wide mapping lock.
                async with scoped._lock:
                    pass
            return scoped
        except BaseException:
            await await_fenced(self._clear_recovery_replacement(task_id, scoped, remove_finished=False))
            raise

    async def _begin_recovery_retirement(
        self,
        task_id: str,
        scoped: RequestScopedActiveTask | None,
    ) -> tuple[asyncio.Task[Any], ...]:
        async with self._lock:
            if self._active_tasks.get(task_id) is not scoped:
                raise InvalidParamsError(f"Task {task_id} lifecycle changed during recovery.")
            if scoped is None:
                return ()
            scoped._is_finished.set()
            scoped._request_queue.shutdown(immediate=True)
            return scoped._retirement_tasks()

    async def _publish_recovery_replacement(
        self,
        task_id: str,
        scoped: RequestScopedActiveTask | None,
        replacement: RequestScopedActiveTask,
    ) -> None:
        async with self._lock:
            if self._active_tasks.get(task_id) is not scoped:
                raise InvalidParamsError(f"Task {task_id} lifecycle changed during recovery.")
            self._active_tasks[task_id] = replacement

    @staticmethod
    async def _abort_recovery_replacement(
        *,
        replacement: RequestScopedActiveTask | None,
        admission: str | None,
        release_admission: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        if replacement is not None:
            await replacement.retire_for_recovery()
        if admission is not None and release_admission is not None:
            await release_admission(admission)

    async def _clear_recovery_replacement(
        self,
        task_id: str,
        scoped: RequestScopedActiveTask | None,
        *,
        remove_finished: bool,
    ) -> None:
        async with self._lock:
            if scoped is not None:
                scoped._recovery_replacement_pending = False
                if self._active_tasks.get(task_id) is scoped and (
                    remove_finished or (scoped._is_finished.is_set() and not scoped.has_unsettled_requests())
                ):
                    self._active_tasks.pop(task_id, None)
            self._recoveries_in_progress.discard(task_id)

    async def _drain_accepted_recovery_predecessors(
        self,
        scoped: RequestScopedActiveTask,
        task_id: str,
        call_context: ServerCallContext,
    ) -> None:
        """Preserve accepted old-lifecycle requests before a terminal recovery decision."""

        if not scoped.has_unsettled_requests():
            return
        if scoped._accepted_consumer_failed:
            raise scoped._accepted_lifecycle_error()
        consumer = scoped._consumer_task
        if consumer is None or consumer.done():
            await scoped.wait_for_accepted_requests()
            return
        task = await self._task_store.get(task_id, call_context)
        if scoped._accepted_consumer_failed:
            raise scoped._accepted_lifecycle_error()
        if task is None or (
            task.status.state not in TERMINAL_TASK_STATES and task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED
        ):
            consumer = scoped._consumer_task
            if consumer is None or consumer.done():
                await scoped.wait_for_accepted_requests()
            return
        await scoped.wait_for_accepted_requests()

    async def cancel_recovery_reservation(self, task_id: str, admission: str) -> None:
        """Remove an unconsumed reservation and its unpublished replacement lifecycle."""

        active_task = None
        async with self._lock:
            candidate = self._active_tasks.get(task_id)
            if isinstance(candidate, RequestScopedActiveTask) and candidate._recovery_admission == admission:
                active_task = self._active_tasks.pop(task_id)
        if active_task is not None:
            await cast("RequestScopedActiveTask", active_task).retire_for_recovery()

    async def retire_for_recovery(self, task_id: str) -> None:
        """Remove and stop a lifecycle during tests or shutdown."""

        async with self._lock:
            active_task = self._active_tasks.pop(task_id, None)
        if active_task is not None:
            await cast("RequestScopedActiveTask", active_task).retire_for_recovery()

    async def get_or_create(
        self,
        task_id: str,
        call_context: ServerCallContext,
        context_id: str | None = None,
        create_task_if_missing: bool = False,
    ) -> RequestScopedActiveTask:
        async with self._lock:
            if task_id in self._recoveries_in_progress:
                raise InvalidParamsError(f"Task {task_id} recovery replacement is pending.")
            existing = self._active_tasks.get(task_id)
            if existing is not None and not existing._is_finished.is_set():
                return cast("RequestScopedActiveTask", existing)
            if existing is not None:
                scoped = cast("RequestScopedActiveTask", existing)
                if scoped.has_unsettled_requests():
                    raise scoped._accepted_lifecycle_error()
                self._active_tasks.pop(task_id, None)

            active_task = self._create_active_task(
                task_id=task_id,
                call_context=call_context,
                context_id=context_id,
            )
            self._active_tasks[task_id] = active_task

        await active_task.start(
            call_context=call_context,
            create_task_if_missing=create_task_if_missing,
        )
        return active_task

    def _create_active_task(
        self,
        *,
        task_id: str,
        call_context: ServerCallContext,
        context_id: str | None,
        recovery_admission: str | None = None,
    ) -> RequestScopedActiveTask:
        task_manager = TaskManager(
            task_id=task_id,
            context_id=context_id,
            task_store=self._task_store,
            initial_message=None,
            context=call_context,
        )
        return RequestScopedActiveTask(
            agent_executor=self._agent_executor,
            task_id=task_id,
            task_manager=task_manager,
            push_sender=self._push_sender,
            on_cleanup=self._on_active_task_cleanup,
            recovery_admission=recovery_admission,
        )

    def _on_active_task_cleanup(self, active_task: ActiveTask) -> None:
        cleanup = asyncio.create_task(
            self._remove_task_if_same(active_task),
            name=f"remove-finished-active-task:{active_task.task_id}",
        )
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def _remove_task_if_same(self, active_task: ActiveTask) -> None:
        async with self._lock:
            if active_task.task_id in self._recoveries_in_progress:
                return
            if self._active_tasks.get(active_task.task_id) is active_task:
                if cast("RequestScopedActiveTask", active_task).has_unsettled_requests():
                    return
                self._active_tasks.pop(active_task.task_id, None)
