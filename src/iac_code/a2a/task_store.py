from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.server.tasks.inmemory_task_store import DEFAULT_LIST_TASKS_PAGE_SIZE, decode_page_token, encode_page_token
from a2a.server.tasks.inmemory_task_store import resolve_user_scope as default_owner_resolver
from a2a.types import ListTasksRequest, ListTasksResponse, Task
from a2a.utils.errors import InvalidParamsError

from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.types import A2AContextRecord, A2ATaskRecord, validate_protocol_id

logger = logging.getLogger(__name__)


class A2ATaskStore(TaskStore):
    def __init__(
        self,
        *,
        metrics: A2AMetrics | None = None,
        idle_timeout_seconds: float = 3600,
        cleanup_interval_seconds: float = 300,
        persistence: A2APersistenceStore | None = None,
        owner_resolver: Callable[[ServerCallContext], str] = default_owner_resolver,
    ) -> None:
        self._sdk_tasks: dict[str, dict[str, Task]] = {}
        self._sdk_tasks_by_context: dict[str, dict[str, set[str]]] = {}
        self._tasks: dict[str, A2ATaskRecord] = {}
        self._contexts: dict[str, A2AContextRecord] = {}
        self._expired_task_tombstones: dict[str, float] = {}
        self._metrics = metrics or NoOpA2AMetrics()
        self._persistence = persistence
        self._idle_timeout_seconds = idle_timeout_seconds
        self._cleanup_interval_seconds = cleanup_interval_seconds
        self._cleanup_task: asyncio.Task[None] | None = None
        self._mutation_lock = asyncio.Lock()
        self._owner_resolver = owner_resolver

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        task = self._owner_tasks(context).get(validate_protocol_id(task_id))
        return _copy_task(task) if task is not None else None

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        owner = self._owner(context)
        task_id = validate_protocol_id(task.id)
        owner_tasks = self._sdk_tasks.setdefault(owner, {})
        previous = owner_tasks.get(task_id)
        if previous is not None:
            self._remove_sdk_task_from_index(owner, task_id, previous.context_id)
        owner_tasks[task_id] = _copy_task(task)
        self._sdk_tasks_by_context.setdefault(owner, {}).setdefault(task.context_id, set()).add(task_id)

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        owner = self._owner(context)
        task_id = validate_protocol_id(task_id)
        async with self._mutation_lock:
            owner_tasks = self._owner_tasks(context)
            existing = owner_tasks.get(task_id)
            if existing is not None:
                self._remove_sdk_task_from_index(owner, task_id, existing.context_id)
            owner_tasks.pop(task_id, None)
            self._tasks.pop(task_id, None)
            self._expired_task_tombstones.pop(task_id, None)

    async def list(self, params: ListTasksRequest, context: ServerCallContext | None = None) -> ListTasksResponse:
        owner = self._owner(context)
        owner_tasks = self._sdk_tasks.get(owner, {})
        if params.context_id:
            task_ids = self._sdk_tasks_by_context.get(owner, {}).get(params.context_id, set())
            tasks = [owner_tasks[task_id] for task_id in task_ids if task_id in owner_tasks]
        else:
            tasks = list(owner_tasks.values())

        if params.status:
            tasks = [task for task in tasks if task.status.state == params.status]
        if params.HasField("status_timestamp_after"):
            after = params.status_timestamp_after.ToJsonString()
            tasks = [
                task
                for task in tasks
                if task.HasField("status")
                and task.status.HasField("timestamp")
                and task.status.timestamp.ToJsonString() >= after
            ]

        tasks.sort(
            key=lambda task: (
                task.status.HasField("timestamp") if task.HasField("status") else False,
                task.status.timestamp.ToJsonString()
                if task.HasField("status") and task.status.HasField("timestamp")
                else "",
                task.id,
            ),
            reverse=True,
        )

        total_size = len(tasks)
        start_idx = 0
        if params.page_token:
            start_task_id = decode_page_token(params.page_token)
            for idx, task in enumerate(tasks):
                if task.id == start_task_id:
                    start_idx = idx
                    break
            else:
                raise InvalidParamsError(f"Invalid page token: {params.page_token}")

        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        end_idx = start_idx + page_size
        next_page_token = encode_page_token(tasks[end_idx].id) if end_idx < total_size else None
        page = [_project_task(task, include_artifacts=params.include_artifacts) for task in tasks[start_idx:end_idx]]
        return ListTasksResponse(
            tasks=page,
            next_page_token=next_page_token,
            page_size=page_size,
            total_size=total_size,
        )

    async def get_or_create_task(self, *, task_id: str | None, context_id: str) -> A2ATaskRecord:
        context_id = validate_protocol_id(context_id)
        task_id = validate_protocol_id(task_id or str(uuid.uuid4()))
        async with self._mutation_lock:
            if task_id in self._expired_task_tombstones:
                raise ValueError("A2A task expired")
            record = self._tasks.get(task_id)
            if record is None:
                record = A2ATaskRecord(task_id=task_id, context_id=context_id)
                self._tasks[task_id] = record
                self._metrics.record_task_created()
            elif record.context_id != context_id:
                raise ValueError("Task belongs to a different context")
            record.touch()
            self._mirror_task(record)
            return record

    async def get_or_create_context(
        self,
        *,
        context_id: str,
        cwd: str,
        runtime_factory: Callable[[str], Any],
    ) -> A2AContextRecord:
        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            if context_id in self._contexts:
                record = self._contexts[context_id]
                if record.expired:
                    raise ValueError("A2A context expired")
                if record.cwd != cwd:
                    raise ValueError("A2A context belongs to a different workspace")
                record.touch()
                self._mirror_context(record)
                return record

            session_id: str | None = None
            if self._persistence is not None:
                snapshot = self._persistence.load_context(context_id)
                if snapshot is not None:
                    if snapshot.cwd != cwd:
                        raise ValueError("A2A context belongs to a different workspace")
                    session_id = snapshot.session_id

            if session_id is None:
                session_id = str(uuid.uuid4())
            record = A2AContextRecord(
                context_id=context_id,
                session_id=session_id,
                cwd=cwd,
                runtime=runtime_factory(session_id),
                lock=asyncio.Lock(),
            )
            self._contexts[context_id] = record
            self._mirror_context(record)
            return record

    async def ensure_task_not_expired(self, task_id: str) -> None:
        async with self._mutation_lock:
            if validate_protocol_id(task_id) in self._expired_task_tombstones:
                raise ValueError("A2A task expired")

    async def cancel_task(self, task_id: str) -> bool:
        async with self._mutation_lock:
            record = self._tasks.get(validate_protocol_id(task_id))
            if record is None or record.active_task is None or record.active_task.done():
                return False
            record.active_task.cancel()
            return True

    async def is_task_active(self, task_id: str) -> bool:
        async with self._mutation_lock:
            record = self._tasks.get(validate_protocol_id(task_id))
            return bool(record is not None and record.active_task is not None and not record.active_task.done())

    def mirror_task(self, record: A2ATaskRecord) -> None:
        self._mirror_task(record)

    def mirror_context(self, record: A2AContextRecord) -> None:
        self._mirror_context(record)

    async def cleanup_once(self, *, now_offset_seconds: float = 0) -> None:
        now = time.monotonic() + now_offset_seconds
        async with self._mutation_lock:
            expired_context_ids = [
                context_id
                for context_id, context in self._contexts.items()
                if context.active_task_id is None and now - context.last_active > self._idle_timeout_seconds
            ]
            for context_id in expired_context_ids:
                self._contexts.pop(context_id, None)
                for task_id, task in list(self._tasks.items()):
                    if task.context_id == context_id:
                        task.expired = True
                        self._expired_task_tombstones[task_id] = now
                self._metrics.record_context_evicted()

            for task_id, expired_at in list(self._expired_task_tombstones.items()):
                if now - expired_at > self._cleanup_interval_seconds:
                    self._expired_task_tombstones.pop(task_id, None)
                    self._tasks.pop(task_id, None)
                    for owner, owner_tasks in list(self._sdk_tasks.items()):
                        existing = owner_tasks.pop(task_id, None)
                        if existing is not None:
                            self._remove_sdk_task_from_index(owner, task_id, existing.context_id)
                        if not owner_tasks:
                            self._sdk_tasks.pop(owner, None)

    async def start_cleanup_loop(self) -> None:
        if self._cleanup_task is not None:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup_loop(self) -> None:
        if self._cleanup_task is None:
            return
        self._cleanup_task.cancel()
        try:
            await self._cleanup_task
        except asyncio.CancelledError:
            pass
        self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval_seconds)
            try:
                await self.cleanup_once()
            except Exception:
                logger.exception("A2A cleanup loop failed")

    def _mirror_task(self, record: A2ATaskRecord) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.save_task(
                A2ATaskSnapshot(
                    task_id=record.task_id,
                    context_id=record.context_id,
                    state=record.state,
                    output_text=list(record.output_text),
                )
            )
        except Exception:
            logger.exception("Failed to persist A2A task %s", record.task_id)

    def _mirror_context(self, record: A2AContextRecord) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.save_context(
                A2AContextSnapshot(
                    context_id=record.context_id,
                    session_id=record.session_id,
                    cwd=record.cwd,
                    active_task_id=record.active_task_id,
                )
            )
        except Exception:
            logger.exception("Failed to persist A2A context %s", record.context_id)

    def _owner(self, context: ServerCallContext | None) -> str:
        if context is None:
            return ""
        return self._owner_resolver(context)

    def _owner_tasks(self, context: ServerCallContext | None) -> dict[str, Task]:
        return self._sdk_tasks.get(self._owner(context), {})

    def _remove_sdk_task_from_index(self, owner: str, task_id: str, context_id: str) -> None:
        task_ids = self._sdk_tasks_by_context.get(owner, {}).get(context_id)
        if task_ids is None:
            return
        task_ids.discard(task_id)
        if not task_ids:
            owner_contexts = self._sdk_tasks_by_context.get(owner)
            if owner_contexts is not None:
                owner_contexts.pop(context_id, None)
                if not owner_contexts:
                    self._sdk_tasks_by_context.pop(owner, None)


def _copy_task(task: Task) -> Task:
    copied = Task()
    copied.CopyFrom(task)
    return copied


def _project_task(task: Task, *, include_artifacts: bool) -> Task:
    projected = _copy_task(task)
    if not include_artifacts:
        projected.ClearField("artifacts")
    return projected
