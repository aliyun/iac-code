"""Request-scoped event subscriptions for the A2A SDK ActiveTask runtime."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from a2a.server.agent_execution.active_task import (
    TERMINAL_TASK_STATES,
    ActiveTask,
    _RequestCompleted,
    _RequestStarted,
)
from a2a.server.agent_execution.active_task_registry import ActiveTaskRegistry
from a2a.server.events.event_queue_v2 import QueueShutDown
from a2a.server.tasks.task_manager import TaskManager
from a2a.types import Task, TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError

from iac_code.a2a.execution_control import RecoverableInputAdmissionCarrier

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from a2a.server.agent_execution import RequestContext
    from a2a.server.context import ServerCallContext
    from a2a.server.events import Event


class RequestScopedActiveTask(ActiveTask):
    """Hide events from earlier requests until this request actually starts."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._direct_message_lock = asyncio.Lock()

    @property
    def direct_message_lock(self) -> asyncio.Lock:
        """Serialize direct messages injected into the running executor."""

        return self._direct_message_lock

    async def enqueue_request(self, request_context: RequestContext):
        request_id = await super().enqueue_request(request_context)
        RecoverableInputAdmissionCarrier.acknowledge_enqueued(request_context, self._producer_task)
        return request_id

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


class RequestScopedActiveTaskRegistry(ActiveTaskRegistry):
    """Create request-scoped ActiveTask instances while retaining SDK lifecycle rules."""

    async def get_or_create(
        self,
        task_id: str,
        call_context: ServerCallContext,
        context_id: str | None = None,
        create_task_if_missing: bool = False,
    ) -> RequestScopedActiveTask:
        async with self._lock:
            existing = self._active_tasks.get(task_id)
            if existing is not None and not existing._is_finished.is_set():
                return cast("RequestScopedActiveTask", existing)
            if existing is not None:
                self._active_tasks.pop(task_id, None)

            task_manager = TaskManager(
                task_id=task_id,
                context_id=context_id,
                task_store=self._task_store,
                initial_message=None,
                context=call_context,
            )
            active_task = RequestScopedActiveTask(
                agent_executor=self._agent_executor,
                task_id=task_id,
                task_manager=task_manager,
                push_sender=self._push_sender,
                on_cleanup=self._on_active_task_cleanup,
            )
            self._active_tasks[task_id] = active_task

        await active_task.start(
            call_context=call_context,
            create_task_if_missing=create_task_if_missing,
        )
        return active_task

    def _on_active_task_cleanup(self, active_task: ActiveTask) -> None:
        cleanup = asyncio.create_task(
            self._remove_task_if_same(active_task),
            name=f"remove-finished-active-task:{active_task.task_id}",
        )
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def _remove_task_if_same(self, active_task: ActiveTask) -> None:
        async with self._lock:
            if self._active_tasks.get(active_task.task_id) is active_task:
                self._active_tasks.pop(active_task.task_id, None)
