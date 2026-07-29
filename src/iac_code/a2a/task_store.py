from __future__ import annotations

import asyncio
import builtins
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.server.tasks.inmemory_task_store import DEFAULT_LIST_TASKS_PAGE_SIZE, decode_page_token, encode_page_token
from a2a.server.tasks.inmemory_task_store import resolve_user_scope as default_owner_resolver
from a2a.types import ListTasksRequest, ListTasksResponse, Message, Part, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict

from iac_code.a2a.events import with_iac_code_session_metadata
from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.types import (
    TASK_STATE_CANCELED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
    A2AContextRecord,
    A2ATaskRecord,
    validate_protocol_id,
)
from iac_code.i18n import _
from iac_code.services.session_backup import SessionBackupService
from iac_code.services.session_layout import SessionPaths, ensure_session_owned_parent
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.file_security import atomic_write_text

logger = logging.getLogger(__name__)
A2ATaskSnapshotList: TypeAlias = list[A2ATaskSnapshot]


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
        self._context_runtime_tasks: dict[str, asyncio.Task[Any]] = {}
        self._context_runtime_waiters: dict[str, int] = {}
        self._discarded_context_runtime_tasks: set[asyncio.Task[Any]] = set()
        self._discarded_context_runtime_task_waiters: dict[asyncio.Task[Any], int] = {}
        self._reconciliation_locks: dict[str, asyncio.Lock] = {}
        self._context_reconciliation_waiters: dict[str, set[str]] = {}
        self._context_execution_starts: dict[str, dict[str, asyncio.Task[Any]]] = {}
        self._owner_resolver = owner_resolver

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        owner = self._owner(context)
        task_id = validate_protocol_id(task_id)
        task = self._sdk_tasks.get(owner, {}).get(task_id)
        if task is not None:
            return _copy_task(task)
        if self._persistence is None:
            return None
        snapshot = self._load_task_snapshot(task_id)
        if snapshot is None or snapshot.owner != owner:
            return None
        return _task_from_snapshot(snapshot) if snapshot is not None else None

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        owner = self._owner(context)
        task_id = validate_protocol_id(task.id)
        async with self._mutation_lock:
            self._attach_context_metadata(task)
            owner_tasks = self._sdk_tasks.setdefault(owner, {})
            previous = owner_tasks.get(task_id)
            if previous is not None:
                self._remove_sdk_task_from_index(owner, task_id, previous.context_id)
            owner_tasks[task_id] = _copy_task(task)
            self._sdk_tasks_by_context.setdefault(owner, {}).setdefault(task.context_id, set()).add(task_id)
            record = self._tasks.get(task_id)
            if record is None:
                record = A2ATaskRecord(
                    task_id=task_id,
                    context_id=task.context_id,
                    state=_task_state_from_sdk_task(task),
                    owner=owner,
                    updated_at=_task_updated_at_from_sdk_task(task),
                )
                self._tasks[task_id] = record
                self._metrics.record_task_created()
            else:
                record.state = _task_state_from_sdk_task(task)
                record.owner = owner
                record.updated_at = _task_updated_at_from_sdk_task(task)
                record.touch()
            self._mirror_task(record)

    def _attach_context_metadata(self, task: Task) -> None:
        context = self._contexts.get(task.context_id)
        if context is None:
            return
        metadata = MessageToDict(task.metadata, preserving_proto_field_name=False) if task.metadata.fields else {}
        metadata = with_iac_code_session_metadata(metadata, context.session_id)
        if metadata is not None:
            ParseDict(metadata, task.metadata)

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
        if self._persistence is not None:
            known_task_ids = {task.id for task in tasks}
            tasks.extend(
                _task_from_snapshot(snapshot)
                for snapshot in self._list_task_snapshots(owner)
                if snapshot.task_id not in known_task_ids
                and (not params.context_id or snapshot.context_id == params.context_id)
            )

        if params.status:
            tasks = [task for task in tasks if task.status.state == params.status]
        if params.HasField("status_timestamp_after"):
            after = _timestamp_key(params.status_timestamp_after)
            tasks = [
                task
                for task in tasks
                if (timestamp_key := _task_status_timestamp_key(task)) is not None and timestamp_key >= after
            ]

        tasks.sort(
            key=lambda task: (
                (timestamp_key := _task_status_timestamp_key(task)) is not None,
                timestamp_key or (0, 0),
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

    async def get_or_create_task(
        self,
        *,
        task_id: str | None,
        context_id: str,
        owner: str | None = None,
        restore_interrupted: bool = True,
    ) -> A2ATaskRecord:
        context_id = validate_protocol_id(context_id)
        task_id = validate_protocol_id(task_id or str(uuid.uuid4()))
        async with self._mutation_lock:
            if task_id in self._expired_task_tombstones:
                raise ValueError(_("A2A task expired"))
            record = self._tasks.get(task_id)
            if record is None:
                snapshot = self._load_task_snapshot(task_id)
                if snapshot is not None:
                    if snapshot.context_id != context_id:
                        raise ValueError(_("Task belongs to a different context"))
                    if owner is not None and snapshot.owner and snapshot.owner != owner:
                        raise ValueError(_("Task belongs to a different owner"))
                    if restore_interrupted:
                        snapshot = self._restore_task_snapshot(task_id) or snapshot
                    record = _record_from_snapshot(snapshot)
                    if owner is not None and not record.owner:
                        record.owner = owner
                else:
                    record = A2ATaskRecord(task_id=task_id, context_id=context_id, owner=owner or "")
                self._tasks[task_id] = record
                self._metrics.record_task_created()
            elif record.context_id != context_id:
                raise ValueError(_("Task belongs to a different context"))
            elif owner is not None:
                if record.owner and record.owner != owner:
                    raise ValueError(_("Task belongs to a different owner"))
                record.owner = owner
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
        create_task: asyncio.Task[Any] | None = None
        async with self._mutation_lock:
            if context_id in self._contexts:
                record = self._contexts[context_id]
                if record.expired:
                    raise ValueError(_("A2A context expired"))
                if record.cwd != cwd:
                    raise ValueError(_("A2A context belongs to a different workspace"))
                if record.runtime is None:
                    create_task = self._context_runtime_tasks.get(context_id)
                    if create_task is None:
                        create_task = asyncio.create_task(asyncio.to_thread(runtime_factory, record.session_id))
                        self._context_runtime_tasks[context_id] = create_task
                else:
                    record.touch()
                    self._mirror_context(record)
                    return record
            else:
                session_id: str | None = None
                if self._persistence is not None:
                    snapshot = self._persistence.load_context(context_id)
                    if snapshot is not None:
                        if snapshot.cwd != cwd:
                            raise ValueError(_("A2A context belongs to a different workspace"))
                        session_id = snapshot.session_id

                if session_id is None:
                    session_id = str(uuid.uuid4())
                    _ensure_new_a2a_session_layout(cwd, session_id)
                record = A2AContextRecord(
                    context_id=context_id,
                    session_id=session_id,
                    cwd=cwd,
                    runtime=None,
                    lock=asyncio.Lock(),
                )
                self._contexts[context_id] = record
                create_task = self._context_runtime_tasks.get(context_id)
                if create_task is None:
                    create_task = asyncio.create_task(asyncio.to_thread(runtime_factory, session_id))
                    self._context_runtime_tasks[context_id] = create_task
            self._context_runtime_waiters[context_id] = self._context_runtime_waiters.get(context_id, 0) + 1

        if create_task is None:  # pragma: no cover - defensive guard for inconsistent state.
            raise ValueError(_("A2A context not found"))
        try:
            runtime = await asyncio.shield(create_task)
        except asyncio.CancelledError:
            discard_task: asyncio.Task[Any] | None = None
            async with self._mutation_lock:
                remaining = self._decrement_context_runtime_waiter_locked(context_id)
                if remaining == 0:
                    record = self._contexts.get(context_id)
                    if record is not None and record.runtime is None:
                        self._contexts.pop(context_id, None)
                    if self._context_runtime_tasks.get(context_id) is create_task:
                        discard_task = self._context_runtime_tasks.pop(context_id, None)
                    if discard_task is not None:
                        self._mark_discarded_context_runtime_task_locked(discard_task, waiters=0)
            if discard_task is not None:
                _close_runtime_task_when_done(
                    discard_task,
                    self._discarded_context_runtime_tasks,
                    self._discarded_context_runtime_task_waiters,
                )
            raise
        except Exception:
            async with self._mutation_lock:
                self._decrement_context_runtime_waiter_locked(context_id)
                record = self._contexts.get(context_id)
                if record is not None and record.runtime is None:
                    self._contexts.pop(context_id, None)
                if self._context_runtime_tasks.get(context_id) is create_task:
                    self._context_runtime_tasks.pop(context_id, None)
            raise

        async with self._mutation_lock:
            self._decrement_context_runtime_waiter_locked(context_id)
            record = self._contexts.get(context_id)
            if record is None:
                if self._context_runtime_tasks.get(context_id) is create_task:
                    self._context_runtime_tasks.pop(context_id, None)
                if not self._release_discarded_context_runtime_task_locked(create_task):
                    await _close_runtime(runtime)
                raise ValueError(_("A2A context not found"))
            if record.expired:
                if self._context_runtime_tasks.get(context_id) is create_task:
                    self._context_runtime_tasks.pop(context_id, None)
                if not self._release_discarded_context_runtime_task_locked(create_task):
                    await _close_runtime(runtime)
                raise ValueError(_("A2A context expired"))
            if record.cwd != cwd:
                if self._context_runtime_tasks.get(context_id) is create_task:
                    self._context_runtime_tasks.pop(context_id, None)
                if not self._release_discarded_context_runtime_task_locked(create_task):
                    await _close_runtime(runtime)
                raise ValueError(_("A2A context belongs to a different workspace"))
            if record.runtime is None:
                record.runtime = runtime
                if self._context_runtime_tasks.get(context_id) is create_task:
                    self._context_runtime_tasks.pop(context_id, None)
                record.touch()
                self._mirror_context(record)
            elif runtime is not record.runtime:
                await _close_runtime(runtime)
            record.touch()
            self._mirror_context(record)
            return record

    async def get_context_record(self, context_id: str) -> A2AContextRecord:
        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            record = self._contexts.get(context_id)
            if record is not None:
                return A2AContextRecord(
                    context_id=record.context_id,
                    session_id=record.session_id,
                    cwd=record.cwd,
                    active_task_id=record.active_task_id,
                    expired=record.expired,
                    created_at=record.created_at,
                    last_active=record.last_active,
                )

            if self._persistence is not None:
                snapshot = self._persistence.load_context(context_id)
                if snapshot is not None:
                    return A2AContextRecord(
                        context_id=snapshot.context_id,
                        session_id=snapshot.session_id,
                        cwd=snapshot.cwd,
                        active_task_id=snapshot.active_task_id,
                    )

        raise ValueError(_("A2A context not found"))

    async def get_context_runtime_path_directories(
        self,
        context_id: str,
    ) -> tuple[builtins.list[str], builtins.list[str], builtins.list[str]]:
        """Return current runtime path directories without persisting them."""

        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            record = self._contexts.get(context_id)
            runtime = record.runtime if record is not None else None
            return _runtime_path_directories(runtime)

    async def discard_context_runtime(self, context_id: str) -> None:
        """Drop a cached runtime so the next turn rebuilds the context cleanly."""
        runtime: Any | None = None
        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            record = self._contexts.get(context_id)
            if record is None or record.runtime is None:
                return
            runtime = record.runtime
            record.runtime = None
            record.touch()
            self._mirror_context(record)
        await _close_runtime(runtime)

    def reconciliation_lock(self, context_id: str) -> asyncio.Lock:
        context_id = validate_protocol_id(context_id)
        return self._reconciliation_locks.setdefault(context_id, asyncio.Lock())

    async def context_reconciliation_is_blocked(self, context_id: str) -> bool:
        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            return (
                bool(self._context_execution_starts.get(context_id))
                or context_id in self._context_runtime_tasks
                or any(
                    task.context_id == context_id and task.active_task is not None and not task.active_task.done()
                    for task in self._tasks.values()
                )
            )

    async def begin_context_execution(self, context_id: str) -> str:
        context_id = validate_protocol_id(context_id)
        owner_task = asyncio.current_task()
        if owner_task is None:  # pragma: no cover - asyncio always provides a task here.
            raise ValueError(_("A2A context not found"))
        token = str(uuid.uuid4())
        async with self.reconciliation_lock(context_id):
            async with self._mutation_lock:
                self._register_context_execution_start_locked(
                    context_id=context_id,
                    token=token,
                    owner_task=owner_task,
                )
        return token

    async def begin_context_execution_after_reconciliation(
        self,
        context_id: str,
        reconcile: Callable[[], Awaitable[Any]],
        *,
        wait_timeout: float | None = None,
    ) -> tuple[str, Any]:
        context_id = validate_protocol_id(context_id)
        owner_task = asyncio.current_task()
        if owner_task is None:  # pragma: no cover - asyncio always provides a task here.
            raise ValueError(_("A2A context not found"))
        waiter_token = str(uuid.uuid4())
        token = str(uuid.uuid4())
        deadline = None if wait_timeout is None else asyncio.get_running_loop().time() + wait_timeout
        async with self._mutation_lock:
            self._register_context_reconciliation_waiter_locked(
                context_id=context_id,
                token=waiter_token,
                owner_task=owner_task,
            )
        try:
            while True:
                await self._wait_for_context_reconciliation_safe(
                    context_id,
                    owner_task=owner_task,
                    deadline=deadline,
                )
                async with self.reconciliation_lock(context_id):
                    async with self._mutation_lock:
                        if self._context_reconciliation_blockers_locked(context_id, owner_task=owner_task):
                            continue
                    result = await reconcile()
                    async with self._mutation_lock:
                        self._discard_context_reconciliation_waiter(
                            context_id=context_id,
                            token=waiter_token,
                        )
                        self._register_context_execution_start_locked(
                            context_id=context_id,
                            token=token,
                            owner_task=owner_task,
                        )
                    return token, result
        finally:
            async with self._mutation_lock:
                self._discard_context_reconciliation_waiter(context_id=context_id, token=waiter_token)

    async def begin_context_execution_if_task_active(
        self,
        context_id: str,
        task_id: str,
    ) -> tuple[str, asyncio.Task[Any]] | None:
        context_id = validate_protocol_id(context_id)
        task_id = validate_protocol_id(task_id)
        owner_task = asyncio.current_task()
        if owner_task is None:  # pragma: no cover - asyncio always provides a task here.
            raise ValueError(_("A2A context not found"))
        token = str(uuid.uuid4())
        async with self.reconciliation_lock(context_id):
            async with self._mutation_lock:
                record = self._tasks.get(task_id)
                if (
                    record is None
                    or record.context_id != context_id
                    or record.active_task is None
                    or record.active_task.done()
                ):
                    return None
                active_owner = record.active_task
                self._register_context_execution_start_locked(
                    context_id=context_id,
                    token=token,
                    owner_task=owner_task,
                )
                return token, active_owner

    async def end_context_execution(self, context_id: str, token: str) -> None:
        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            self._discard_context_execution_start(context_id=context_id, token=token)

    def _discard_context_execution_start(self, *, context_id: str, token: str) -> None:
        starts = self._context_execution_starts.get(context_id)
        if starts is None:
            return
        starts.pop(token, None)
        if not starts:
            self._context_execution_starts.pop(context_id, None)

    def _discard_context_reconciliation_waiter(self, *, context_id: str, token: str) -> None:
        waiters = self._context_reconciliation_waiters.get(context_id)
        if waiters is None:
            return
        waiters.discard(token)
        if not waiters:
            self._context_reconciliation_waiters.pop(context_id, None)

    def _register_context_execution_start_locked(
        self,
        *,
        context_id: str,
        token: str,
        owner_task: asyncio.Task[Any],
    ) -> None:
        self._context_execution_starts.setdefault(context_id, {})[token] = owner_task
        owner_task.add_done_callback(
            lambda _task: self._discard_context_execution_start(context_id=context_id, token=token)
        )

    def _register_context_reconciliation_waiter_locked(
        self,
        *,
        context_id: str,
        token: str,
        owner_task: asyncio.Task[Any],
    ) -> None:
        self._context_reconciliation_waiters.setdefault(context_id, set()).add(token)
        owner_task.add_done_callback(
            lambda _task: self._discard_context_reconciliation_waiter(context_id=context_id, token=token)
        )

    def _context_reconciliation_blockers_locked(
        self,
        context_id: str,
        *,
        owner_task: asyncio.Task[Any],
    ) -> set[asyncio.Task[Any]]:
        blockers = {
            task
            for task in self._context_execution_starts.get(context_id, {}).values()
            if task is not owner_task and not task.done()
        }
        runtime_task = self._context_runtime_tasks.get(context_id)
        if runtime_task is not None and runtime_task is not owner_task and not runtime_task.done():
            blockers.add(runtime_task)
        blockers.update(
            task.active_task
            for task in self._tasks.values()
            if task.context_id == context_id
            and task.active_task is not None
            and task.active_task is not owner_task
            and not task.active_task.done()
        )
        return blockers

    async def _wait_for_context_reconciliation_safe(
        self,
        context_id: str,
        *,
        owner_task: asyncio.Task[Any],
        deadline: float | None,
    ) -> None:
        while True:
            async with self._mutation_lock:
                blockers = self._context_reconciliation_blockers_locked(context_id, owner_task=owner_task)
            if not blockers:
                return
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            _done, pending = await asyncio.wait(blockers, timeout=remaining)
            if pending:
                raise TimeoutError

    async def ensure_context_reconciliation_safe(self, context_id: str) -> None:
        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            if self._context_execution_starts.get(context_id):
                raise ValueError(_("A2A context not found"))
            if context_id in self._context_runtime_tasks:
                raise ValueError(_("A2A context not found"))
            if any(
                task.context_id == context_id and task.active_task is not None and not task.active_task.done()
                for task in self._tasks.values()
            ):
                raise ValueError(_("A2A context not found"))

    async def refresh_context_from_session(
        self,
        *,
        context_id: str,
        cwd: str,
        session_id: str,
        clear_active_task_for_proven_handoff: bool,
    ) -> A2AContextRecord:
        context_id = validate_protocol_id(context_id)
        session_id = validate_protocol_id(session_id)
        snapshot = _load_session_context_snapshot(cwd=cwd, session_id=session_id)
        if snapshot.context_id != context_id or snapshot.session_id != session_id or snapshot.cwd != cwd:
            raise ValueError(_("A2A context belongs to a different workspace"))

        runtime: Any | None = None
        async with self._mutation_lock:
            create_task = self._context_runtime_tasks.get(context_id)
            if create_task is not None:
                raise ValueError(_("A2A context not found"))
            record = self._contexts.get(context_id)
            active_task_ids = {task_id for task_id in (snapshot.active_task_id,) if task_id is not None}
            if record is not None and record.active_task_id is not None:
                active_task_ids.add(record.active_task_id)
            if any(self._task_is_active_locked(task_id) for task_id in active_task_ids):
                raise ValueError(_("A2A context not found"))
            if record is None:
                record = A2AContextRecord(
                    context_id=context_id,
                    session_id=session_id,
                    cwd=cwd,
                    lock=asyncio.Lock(),
                )
                self._contexts[context_id] = record
            elif record.session_id != session_id or record.cwd != cwd:
                raise ValueError(_("A2A context belongs to a different workspace"))
            runtime = record.runtime
            record.runtime = None
            record.active_task_id = None if clear_active_task_for_proven_handoff else snapshot.active_task_id
            record.touch()
            self._mirror_context(record)
            refreshed = A2AContextRecord(
                context_id=record.context_id,
                session_id=record.session_id,
                cwd=record.cwd,
                active_task_id=record.active_task_id,
                expired=record.expired,
                created_at=record.created_at,
                last_active=record.last_active,
            )
        await _close_runtime(runtime)
        return refreshed

    def _task_is_active_locked(self, task_id: str) -> bool:
        record = self._tasks.get(validate_protocol_id(task_id))
        return bool(record is not None and record.active_task is not None and not record.active_task.done())

    async def get_task_record(self, task_id: str) -> A2ATaskRecord:
        task_id = validate_protocol_id(task_id)
        async with self._mutation_lock:
            record = self._tasks.get(task_id)
            if record is not None:
                return A2ATaskRecord(
                    task_id=record.task_id,
                    context_id=record.context_id,
                    state=record.state,
                    owner=record.owner,
                    output_text=list(record.output_text),
                    expired=record.expired,
                    updated_at=record.updated_at,
                    created_at=record.created_at,
                    last_active=record.last_active,
                )

            if self._persistence is not None:
                snapshot = self._load_task_snapshot(task_id)
                if snapshot is not None:
                    return _record_from_snapshot(snapshot)

        raise ValueError(_("A2A task not found"))

    def _decrement_context_runtime_waiter_locked(self, context_id: str) -> int:
        count = self._context_runtime_waiters.get(context_id, 0)
        if count <= 1:
            self._context_runtime_waiters.pop(context_id, None)
            return 0
        self._context_runtime_waiters[context_id] = count - 1
        return count - 1

    def _mark_discarded_context_runtime_task_locked(self, task: asyncio.Task[Any], *, waiters: int) -> None:
        self._discarded_context_runtime_tasks.add(task)
        self._discarded_context_runtime_task_waiters[task] = max(waiters, 0)

    def _release_discarded_context_runtime_task_locked(self, task: asyncio.Task[Any]) -> bool:
        if task not in self._discarded_context_runtime_tasks:
            return False
        remaining = self._discarded_context_runtime_task_waiters.get(task, 0)
        if remaining <= 1:
            self._discarded_context_runtime_task_waiters.pop(task, None)
            self._discarded_context_runtime_tasks.discard(task)
        else:
            self._discarded_context_runtime_task_waiters[task] = remaining - 1
        return True

    async def ensure_task_not_expired(self, task_id: str) -> None:
        async with self._mutation_lock:
            if validate_protocol_id(task_id) in self._expired_task_tombstones:
                raise ValueError(_("A2A task expired"))

    async def cancel_task(self, task_id: str) -> bool:
        async with self._mutation_lock:
            record = self._tasks.get(validate_protocol_id(task_id))
            if record is None or record.active_task is None or record.active_task.done():
                return False
            record.active_task.cancel()
            return True

    async def cancel_task_and_wait(self, task_id: str, *, timeout: float | None = None) -> bool:
        async with self._mutation_lock:
            record = self._tasks.get(validate_protocol_id(task_id))
            if record is None or record.active_task is None or record.active_task.done():
                return False
            active_task = record.active_task
            active_task.cancel()

        if active_task is asyncio.current_task():
            return True
        try:
            if timeout is None:
                await asyncio.shield(active_task)
            else:
                await asyncio.wait_for(asyncio.shield(active_task), timeout=timeout)
        except asyncio.CancelledError:
            if not active_task.done():
                raise
        except TimeoutError:
            logger.warning("Timed out waiting for canceled A2A task %s to finish", task_id)
        return True

    async def is_task_active(self, task_id: str) -> bool:
        async with self._mutation_lock:
            return self._task_is_active_locked(task_id)

    def mirror_task(self, record: A2ATaskRecord) -> None:
        record.updated_at = time.time()
        self._mirror_task(record)

    def mirror_context(self, record: A2AContextRecord) -> None:
        self._mirror_context(record)

    async def cleanup_once(self, *, now_offset_seconds: float = 0) -> None:
        now = time.monotonic() + now_offset_seconds
        async with self._mutation_lock:
            active_context_ids = {
                task.context_id
                for task in self._tasks.values()
                if task.active_task is not None and not task.active_task.done()
            }
            reconciling_context_ids = {
                context_id for context_id, lock in self._reconciliation_locks.items() if lock.locked()
            }
            expired_context_ids = [
                context_id
                for context_id, context in self._contexts.items()
                if context.active_task_id is None
                and now - context.last_active > self._idle_timeout_seconds
                and context_id not in self._context_runtime_tasks
                and not self._context_reconciliation_waiters.get(context_id)
                and not self._context_execution_starts.get(context_id)
                and context_id not in active_context_ids
                and context_id not in reconciling_context_ids
            ]
            for context_id in expired_context_ids:
                record = self._contexts.pop(context_id, None)
                if record is not None:
                    await _close_runtime(record.runtime)
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
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        async with self._mutation_lock:
            records = list(self._contexts.values())
            runtime_tasks = [
                (task, self._context_runtime_waiters.get(context_id, 0))
                for context_id, task in self._context_runtime_tasks.items()
            ]
            self._contexts.clear()
            self._context_runtime_tasks.clear()
            self._context_runtime_waiters.clear()
            for task, waiters in runtime_tasks:
                self._mark_discarded_context_runtime_task_locked(task, waiters=waiters)
        for record in records:
            await _close_runtime(record.runtime)
        for task, _waiters in runtime_tasks:
            _close_runtime_task_when_done(
                task,
                self._discarded_context_runtime_tasks,
                self._discarded_context_runtime_task_waiters,
            )

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval_seconds)
            try:
                await self.cleanup_once()
            except Exception:
                logger.exception("A2A cleanup loop failed")

    def _mirror_task(self, record: A2ATaskRecord) -> None:
        snapshot = A2ATaskSnapshot(
            task_id=record.task_id,
            context_id=record.context_id,
            state=record.state,
            owner=record.owner,
            output_text=list(record.output_text),
            updated_at=record.updated_at,
        )
        if self._persistence is not None:
            try:
                self._persistence.save_task(snapshot)
            except Exception:
                logger.exception("Failed to persist A2A task %s", record.task_id)
        self._mirror_session_task(snapshot)

    def _mirror_context(self, record: A2AContextRecord) -> None:
        snapshot = A2AContextSnapshot(
            context_id=record.context_id,
            session_id=record.session_id,
            cwd=record.cwd,
            active_task_id=record.active_task_id,
        )
        if self._persistence is not None:
            try:
                self._persistence.save_context(snapshot)
            except Exception:
                logger.exception("Failed to persist A2A context %s", record.context_id)
        self._mirror_session_context(snapshot)

    def _mirror_session_task(self, snapshot: A2ATaskSnapshot) -> None:
        try:
            session_paths = self._session_paths_for_task_context(snapshot.context_id)
            if session_paths is None:
                return
            _write_session_snapshot(session_paths.session_dir, session_paths.a2a_task_path, asdict(snapshot))
        except Exception as exc:
            logger.warning(
                "Failed to persist A2A session task snapshot error_type=%s",
                type(exc).__name__,
            )

    def _mirror_session_context(self, snapshot: A2AContextSnapshot) -> None:
        try:
            session_paths = _session_paths_for_a2a_snapshot(snapshot)
            if session_paths is None:
                return
            _write_session_snapshot(session_paths.session_dir, session_paths.a2a_context_path, asdict(snapshot))
        except Exception as exc:
            logger.warning(
                "Failed to persist A2A session context snapshot error_type=%s",
                type(exc).__name__,
            )

    def _session_paths_for_task_context(self, context_id: str) -> SessionPaths | None:
        context = self._contexts.get(context_id)
        if context is not None:
            return _session_paths_for_a2a_context(context)
        snapshot = self._load_context_snapshot(context_id)
        if snapshot is None:
            return None
        return _session_paths_for_a2a_snapshot(snapshot)

    def _load_task_snapshot(self, task_id: str) -> A2ATaskSnapshot | None:
        if self._persistence is None:
            return None
        load_task = getattr(self._persistence, "load_task", None)
        if load_task is None:
            return None
        try:
            return load_task(task_id)
        except Exception:
            logger.exception("Failed to load persisted A2A task %s", task_id)
            return None

    def _restore_task_snapshot(self, task_id: str) -> A2ATaskSnapshot | None:
        if self._persistence is None:
            return None
        restore_task = getattr(self._persistence, "restore_task", None)
        if restore_task is None:
            return self._load_task_snapshot(task_id)
        try:
            return restore_task(task_id)
        except Exception:
            logger.exception("Failed to restore persisted A2A task %s", task_id)
            return None

    def _load_context_snapshot(self, context_id: str) -> A2AContextSnapshot | None:
        if self._persistence is None:
            return None
        load_context = getattr(self._persistence, "load_context", None)
        if load_context is None:
            return None
        try:
            snapshot = load_context(context_id)
        except Exception as exc:
            logger.warning(
                "Failed to load persisted A2A context error_type=%s",
                type(exc).__name__,
            )
            return None
        return snapshot if isinstance(snapshot, A2AContextSnapshot) else None

    def _list_task_snapshots(self, owner: str) -> A2ATaskSnapshotList:
        if self._persistence is None:
            return []
        list_tasks = getattr(self._persistence, "list_tasks", None)
        if list_tasks is None:
            return []
        try:
            snapshots = list_tasks()
        except Exception:
            logger.exception("Failed to list persisted A2A tasks")
            return []
        restored: list[A2ATaskSnapshot] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, A2ATaskSnapshot):
                continue
            if snapshot.owner == owner:
                restored.append(snapshot)
        return restored

    def owner_for_context(self, context: ServerCallContext | None) -> str:
        return self._owner(context)

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


def _session_paths_for_a2a_context(record: A2AContextRecord) -> SessionPaths | None:
    session_dir = SessionStorage().v2_session_dir(record.cwd, record.session_id)
    if session_dir is None:
        return None
    return SessionPaths.require_supported(session_dir)


def _session_paths_for_a2a_snapshot(snapshot: A2AContextSnapshot) -> SessionPaths | None:
    session_dir = SessionStorage().v2_session_dir(snapshot.cwd, snapshot.session_id)
    if session_dir is None:
        return None
    return SessionPaths.require_supported(session_dir)


def _ensure_new_a2a_session_layout(cwd: str, session_id: str) -> None:
    try:
        SessionStorage().ensure_v2_session_dir_for_new_session(cwd, session_id)
    except Exception as exc:
        logger.warning(
            "Failed to prepare A2A session layout error_type=%s",
            type(exc).__name__,
        )
        return
    try:
        SessionBackupService().initialize_session(cwd, session_id)
    except Exception as exc:
        logger.error(
            "Failed to initialize A2A session backup state error_type=%s",
            type(exc).__name__,
        )
        raise


def _load_session_context_snapshot(*, cwd: str, session_id: str) -> A2AContextSnapshot:
    session_dir = SessionStorage().v2_session_dir(cwd, session_id)
    if session_dir is None:
        raise ValueError(_("A2A context not found"))
    session_paths = SessionPaths.require_supported(session_dir)
    try:
        payload = json.loads(session_paths.a2a_context_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(_("A2A context not found")) from exc
    if not isinstance(payload, dict):
        raise ValueError(_("A2A context not found"))
    context_id = validate_protocol_id(payload.get("context_id"))
    snapshot_session_id = validate_protocol_id(payload.get("session_id"))
    snapshot_cwd = payload.get("cwd")
    active_task_id = payload.get("active_task_id")
    if not isinstance(snapshot_cwd, str) or (active_task_id is not None and not isinstance(active_task_id, str)):
        raise ValueError(_("A2A context not found"))
    if active_task_id is not None:
        active_task_id = validate_protocol_id(active_task_id)
    updated_at = payload.get("updated_at")
    return A2AContextSnapshot(
        context_id=context_id,
        session_id=snapshot_session_id,
        cwd=snapshot_cwd,
        active_task_id=active_task_id,
        updated_at=float(updated_at) if isinstance(updated_at, (int, float)) else time.time(),
    )


def _write_session_snapshot(session_dir: Path, path: Path, data: dict[str, Any]) -> None:
    ensure_session_owned_parent(session_dir, path)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, sort_keys=True))


async def _close_runtime(runtime: Any | None) -> None:
    if runtime is None:
        return
    close = getattr(runtime, "aclose", None)
    if callable(close):
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
            return
        except Exception:
            logger.exception("Failed to close A2A runtime")
            return
    manager = getattr(runtime, "mcp_manager", None)
    if manager is not None:
        try:
            await manager.disconnect_all()
        except Exception:
            logger.exception("Failed to disconnect A2A MCP manager")
    agent_runtime = getattr(runtime, "agent_runtime", None)
    if agent_runtime is not None and agent_runtime is not runtime:
        await _close_runtime(agent_runtime)


def _runtime_path_directories(runtime: Any | None) -> tuple[list[str], list[str], list[str]]:
    """Copy path roots exposed by the current agent runtime."""

    agent_runtime = getattr(runtime, "agent_runtime", runtime)
    agent_loop = getattr(agent_runtime, "agent_loop", None)
    if agent_loop is None:
        return [], [], []

    permission_context = None
    permission_context_getter = getattr(agent_loop, "_permission_context_getter", None)
    if callable(permission_context_getter):
        try:
            permission_context = permission_context_getter()
        except Exception:
            permission_context = None
    if permission_context is None:
        permission_context = getattr(agent_loop, "_permission_context", None)

    additional = list(getattr(permission_context, "additional_directories", []) or [])
    trusted = list(getattr(permission_context, "trusted_read_directories", []) or [])
    relative = list(getattr(permission_context, "relative_read_directories", []) or [])
    _extend_unique_strings(trusted, getattr(permission_context, "strict_read_directories", []) or [])
    _extend_unique_strings(trusted, getattr(agent_loop, "_tool_context_trusted_read_directories", []) or [])
    _extend_unique_strings(relative, getattr(agent_loop, "_tool_context_relative_read_directories", []) or [])
    return additional, trusted, relative


def _extend_unique_strings(target: list[str], values: Any) -> None:
    seen = set(target)
    for value in values:
        if isinstance(value, str) and value not in seen:
            target.append(value)
            seen.add(value)


def _close_runtime_task_when_done(
    task: asyncio.Task[Any],
    discarded_tasks: set[asyncio.Task[Any]] | None = None,
    discarded_task_waiters: dict[asyncio.Task[Any], int] | None = None,
) -> None:
    def discard_marker(done: asyncio.Task[Any]) -> None:
        if discarded_task_waiters is not None:
            discarded_task_waiters.pop(done, None)
        if discarded_tasks is not None:
            discarded_tasks.discard(done)

    def close_result(done: asyncio.Task[Any]) -> None:
        try:
            runtime = done.result()
        except asyncio.CancelledError:
            discard_marker(done)
            return
        except Exception:
            discard_marker(done)
            logger.debug("Discarded A2A runtime creation task failed", exc_info=True)
            return
        loop = done.get_loop()
        if loop.is_closed():
            discard_marker(done)
            return
        loop.create_task(_close_runtime(runtime))
        if discarded_task_waiters is None or discarded_task_waiters.get(done, 0) <= 0:
            discard_marker(done)

    if task.done():
        close_result(task)
    else:
        task.add_done_callback(close_result)


def _copy_task(task: Task) -> Task:
    copied = Task()
    copied.CopyFrom(task)
    return copied


def _project_task(task: Task, *, include_artifacts: bool) -> Task:
    projected = _copy_task(task)
    if not include_artifacts:
        projected.ClearField("artifacts")
    return projected


def _record_from_snapshot(snapshot: A2ATaskSnapshot) -> A2ATaskRecord:
    return A2ATaskRecord(
        task_id=snapshot.task_id,
        context_id=snapshot.context_id,
        state=snapshot.state,
        owner=snapshot.owner,
        output_text=list(snapshot.output_text),
        updated_at=snapshot.updated_at,
    )


def _task_from_snapshot(snapshot: A2ATaskSnapshot) -> Task:
    status_message = None
    if snapshot.status_message:
        status_message = Message(
            message_id=f"{snapshot.task_id}-restored",
            task_id=snapshot.task_id,
            context_id=snapshot.context_id,
            role=Role.ROLE_AGENT,
            parts=[Part(text=snapshot.status_message)],
        )
    status = TaskStatus(
        state=TaskState.Name(_task_state_to_a2a_state(snapshot.state)),
        message=status_message,
        timestamp=_timestamp_from_epoch(snapshot.updated_at),
    )
    return Task(id=snapshot.task_id, context_id=snapshot.context_id, status=status)


def _task_state_to_a2a_state(state: str) -> int:
    if state == TASK_STATE_COMPLETED:
        return TaskState.TASK_STATE_COMPLETED
    if state == TASK_STATE_FAILED:
        return TaskState.TASK_STATE_FAILED
    if state == TASK_STATE_CANCELED:
        return TaskState.TASK_STATE_CANCELED
    if state == TASK_STATE_WORKING:
        return TaskState.TASK_STATE_WORKING
    if state == TASK_STATE_SUBMITTED:
        return TaskState.TASK_STATE_SUBMITTED
    if state == TASK_STATE_INPUT_REQUIRED:
        return TaskState.TASK_STATE_INPUT_REQUIRED
    return TaskState.TASK_STATE_INPUT_REQUIRED


def _task_state_from_sdk_task(task: Task) -> str:
    if not task.HasField("status"):
        return TASK_STATE_SUBMITTED
    state = task.status.state
    if state == TaskState.TASK_STATE_COMPLETED:
        return TASK_STATE_COMPLETED
    if state == TaskState.TASK_STATE_FAILED:
        return TASK_STATE_FAILED
    if state == TaskState.TASK_STATE_CANCELED:
        return TASK_STATE_CANCELED
    if state == TaskState.TASK_STATE_WORKING:
        return TASK_STATE_WORKING
    if state == TaskState.TASK_STATE_INPUT_REQUIRED:
        return TASK_STATE_INPUT_REQUIRED
    return TASK_STATE_SUBMITTED


def _task_updated_at_from_sdk_task(task: Task) -> float:
    if task.HasField("status") and task.status.HasField("timestamp"):
        return float(task.status.timestamp.seconds) + (float(task.status.timestamp.nanos) / 1_000_000_000)
    return time.time()


def _task_status_timestamp_key(task: Task) -> tuple[int, int] | None:
    if not task.HasField("status") or not task.status.HasField("timestamp"):
        return None
    return _timestamp_key(task.status.timestamp)


def _timestamp_key(timestamp: Any) -> tuple[int, int]:
    return (int(getattr(timestamp, "seconds", 0)), int(getattr(timestamp, "nanos", 0)))


def _timestamp_from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)
