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

from iac_code.a2a.backup import await_fenced, run_sync_fenced
from iac_code.a2a.events import with_iac_code_session_metadata
from iac_code.a2a.execution_control import current_execution_control, current_execution_termination_reason
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
from iac_code.services.telemetry.attributes import normalize_telemetry_channel
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
        backup_service: Any | None = None,
    ) -> None:
        self._sdk_tasks: dict[str, dict[str, Task]] = {}
        self._sdk_tasks_by_context: dict[str, dict[str, set[str]]] = {}
        self._pending_permissions: dict[str, list[dict[str, Any]]] = {}
        self._tasks: dict[str, A2ATaskRecord] = {}
        self._task_persistence_dirty: set[str] = set()
        self._contexts: dict[str, A2AContextRecord] = {}
        self._pending_context_telemetry_channels: dict[str, str] = {}
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
        self._termination_commit_locks: dict[str, asyncio.Lock] = {}
        self._context_reconciliation_waiters: dict[str, set[str]] = {}
        self._context_execution_starts: dict[str, dict[str, asyncio.Task[Any]]] = {}
        self._owner_resolver = owner_resolver
        self._backup_service = backup_service or SessionBackupService()
        self._permission_wait_active_probe: Callable[[], bool] | None = None
        self._execution_control_snapshot_provider: Callable[[str], dict[str, Any] | None] | None = None
        self._execution_control_active_probe: Callable[[], bool] | None = None

    def set_permission_wait_active_probe(self, probe: Callable[[], bool] | None) -> None:
        self._permission_wait_active_probe = probe

    def set_execution_control_provider(
        self,
        snapshot_provider: Callable[[str], dict[str, Any] | None] | None,
        active_probe: Callable[[], bool] | None,
    ) -> None:
        self._execution_control_snapshot_provider = snapshot_provider
        self._execution_control_active_probe = active_probe

    def touch_context(self, context_id: str) -> None:
        """Restart the idle interval after a connection hold, without storage I/O."""
        record = self._contexts.get(context_id)
        if record is not None:
            record.touch()

    def _execution_snapshot(self, context_id: str) -> dict[str, Any] | None:
        if self._execution_control_snapshot_provider is not None:
            return self._execution_control_snapshot_provider(context_id)
        control = current_execution_control()
        return control.snapshot() if control is not None and control.context_id == context_id else None

    def _execution_is_terminating(self, context_id: str) -> bool:
        control = self._execution_snapshot(context_id)
        return bool(control and control["phase"] in {"terminating", "terminated"})

    def _execution_retains_context(self, context_id: str) -> bool:
        control = self._execution_snapshot(context_id)
        return bool(
            control
            and (
                control["phase"] in {"pausing", "pause_committing", "paused", "resuming", "terminating"}
                or (control["phase"] == "terminated" and not control["releaseReady"])
            )
        )

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
            record = self._tasks.get(task_id)
            next_state = _task_state_from_sdk_task(task)
            incoming_updated_at = _task_updated_at_from_sdk_task(task)
            preserve_terminal = bool(
                record is not None
                and record.state
                in {TASK_STATE_CANCELED, TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_INPUT_REQUIRED}
                and self._execution_is_terminating(task.context_id)
            )
            if preserve_terminal:
                # Queued SDK working events must not undo the executor's final
                # state while its immutable termination snapshot is committed.
                assert record is not None
                task = _copy_task(task)
                task.status.state = TaskState.Value("TASK_STATE_" + record.state.upper().replace("-", "_"))
            active_finalization = bool(
                record is not None
                and record.state
                in {TASK_STATE_CANCELED, TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_INPUT_REQUIRED}
                and next_state in {TASK_STATE_SUBMITTED, TASK_STATE_WORKING}
                and record.active_task is not None
                and not record.active_task.done()
            )
            stale_state_projection = bool(
                record is not None
                and record.state
                in {TASK_STATE_CANCELED, TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_INPUT_REQUIRED}
                and next_state in {TASK_STATE_SUBMITTED, TASK_STATE_WORKING}
                and not active_finalization
                and incoming_updated_at < record.updated_at
            )
            if stale_state_projection:
                # The SDK consumes executor events asynchronously. An event
                # older than a detached final state must not roll either task
                # projection back.
                return
            self._attach_context_metadata(task)
            self._attach_pending_permissions(task)
            owner_tasks = self._sdk_tasks.setdefault(owner, {})
            previous = owner_tasks.get(task_id)
            if previous is not None:
                self._remove_sdk_task_from_index(owner, task_id, previous.context_id)
            owner_tasks[task_id] = _copy_task(task)
            self._sdk_tasks_by_context.setdefault(owner, {}).setdefault(task.context_id, set()).add(task_id)
            if preserve_terminal or active_finalization:
                # During active finalization the SDK still needs the delayed
                # nonterminal frame for stream ordering, but the durable record
                # already describes the backup boundary and must stay final.
                return
            # The SDK saves the full Task before yielding every streaming frame. Executors already
            # mirror task records explicitly at output/state durability boundaries, so repeated SDK
            # saves with the same projected state only need the session-local mirror below.
            persist_shared_snapshot = (
                record is None
                or task_id in self._task_persistence_dirty
                or record.state != next_state
                or record.owner != owner
            )
            if record is None:
                record = A2ATaskRecord(
                    task_id=task_id,
                    context_id=task.context_id,
                    state=next_state,
                    owner=owner,
                    updated_at=incoming_updated_at,
                )
                self._tasks[task_id] = record
                self._metrics.record_task_created()
            else:
                record.state = next_state
                record.owner = owner
                record.updated_at = incoming_updated_at
                record.touch()
            self._mirror_task(record, persist_shared_snapshot=persist_shared_snapshot)

    def _attach_context_metadata(self, task: Task) -> None:
        context = self._contexts.get(task.context_id)
        if context is None:
            return
        metadata = MessageToDict(task.metadata, preserving_proto_field_name=False) if task.metadata.fields else {}
        metadata = with_iac_code_session_metadata(metadata, context.session_id)
        if metadata is None:
            metadata = {}
        if self._execution_control_snapshot_provider is not None:
            control = self._execution_control_snapshot_provider(task.context_id)
            if control is not None:
                iac_code = metadata.setdefault("iac_code", {})
                if isinstance(iac_code, dict):
                    iac_code["executionControl"] = control
        ParseDict(metadata, task.metadata)

    def _attach_pending_permissions(self, task: Task) -> None:
        metadata = MessageToDict(task.metadata, preserving_proto_field_name=False) if task.metadata.fields else {}
        iac_code = metadata.get("iac_code")
        if not isinstance(iac_code, dict):
            iac_code = {}
            metadata["iac_code"] = iac_code
        # The A2A SDK merges status metadata into the stored Task.  Once an answer moves the
        # task out of INPUT_REQUIRED, keeping the old unified input would make GetTask replay
        # an already-consumed permission/question and invite a duplicate response.
        if task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED:
            iac_code.pop("input", None)
        pending = self._pending_permissions.get(task.id)
        if pending:
            iac_code["pendingPermissions"] = [dict(envelope) for envelope in pending]
        else:
            iac_code.pop("pendingPermissions", None)
            if not iac_code:
                metadata.pop("iac_code", None)
        task.metadata.Clear()
        if metadata:
            ParseDict(metadata, task.metadata)

    async def set_pending_permissions(self, task_id: str, permissions: builtins.list[dict[str, Any]]) -> None:
        task_id = validate_protocol_id(task_id)
        async with self._mutation_lock:
            if permissions:
                self._pending_permissions[task_id] = [dict(envelope) for envelope in permissions]
            else:
                self._pending_permissions.pop(task_id, None)
            for owner_tasks in self._sdk_tasks.values():
                task = owner_tasks.get(task_id)
                if task is not None:
                    self._attach_pending_permissions(task)
            self._task_persistence_dirty.add(task_id)

    async def record_expected_permission_backup_generation(self, task_id: str, generation: int) -> None:
        """Raise the internal restore floor for the next persisted permission Resume."""

        task_id = validate_protocol_id(task_id)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("Invalid permission backup generation")
        async with self._mutation_lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise ValueError(_("A2A task not found"))
            current = record.expected_permission_backup_generation
            if current is None or generation > current:
                record.expected_permission_backup_generation = generation
                self._task_persistence_dirty.add(task_id)

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        owner = self._owner(context)
        task_id = validate_protocol_id(task_id)
        async with self._mutation_lock:
            owner_tasks = self._owner_tasks(context)
            existing = owner_tasks.get(task_id)
            if existing is not None:
                self._remove_sdk_task_from_index(owner, task_id, existing.context_id)
            owner_tasks.pop(task_id, None)
            self._pending_permissions.pop(task_id, None)
            self._tasks.pop(task_id, None)
            self._task_persistence_dirty.discard(task_id)
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
                pending_channel = self._pending_context_telemetry_channels.get(context_id)
                if pending_channel is not None:
                    record.telemetry_channel = pending_channel
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
                    self._pending_context_telemetry_channels.pop(context_id, None)
                    return record
            else:
                session_id: str | None = None
                telemetry_channel = self._pending_context_telemetry_channels.get(context_id)
                snapshot = self._load_context_snapshot(context_id)
                if snapshot is not None:
                    if snapshot.cwd != cwd:
                        raise ValueError(_("A2A context belongs to a different workspace"))
                    session_id = snapshot.session_id
                    if telemetry_channel is None:
                        telemetry_channel = snapshot.telemetry_channel

                if session_id is None:
                    session_id = str(uuid.uuid4())
                    _ensure_new_a2a_session_layout(cwd, session_id, self._backup_service)
                record = A2AContextRecord(
                    context_id=context_id,
                    session_id=session_id,
                    cwd=cwd,
                    telemetry_channel=telemetry_channel,
                    runtime=None,
                    lock=asyncio.Lock(),
                )
                self._contexts[context_id] = record
                create_task = self._context_runtime_tasks.get(context_id)
                if create_task is None:
                    create_task = asyncio.create_task(asyncio.to_thread(runtime_factory, session_id))
                    self._context_runtime_tasks[context_id] = create_task
            control = current_execution_control()
            if control is not None and control.context_id == context_id:
                control.bind_session(record.session_id)
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
                    if record is not None and record.runtime is None and current_execution_termination_reason() is None:
                        self._contexts.pop(context_id, None)
                    if self._context_runtime_tasks.get(context_id) is create_task:
                        discard_task = self._context_runtime_tasks.pop(context_id, None)
                    if discard_task is not None:
                        self._mark_discarded_context_runtime_task_locked(discard_task, waiters=0)
            if discard_task is not None:
                if current_execution_termination_reason() is not None:
                    # Keep the executor alive until bootstrap and runtime cleanup
                    # finish. A canceled to_thread waiter does not stop its thread.
                    async def drain_runtime() -> None:
                        try:
                            runtime = await asyncio.shield(discard_task)
                            await _close_runtime(runtime)
                        finally:
                            self._discarded_context_runtime_tasks.discard(discard_task)
                            self._discarded_context_runtime_task_waiters.pop(discard_task, None)

                    await await_fenced(drain_runtime())
                else:
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
            self._pending_context_telemetry_channels.pop(context_id, None)
            return record

    async def resolve_context_telemetry_channel(
        self,
        context_id: str,
        requested_channel: str | None,
    ) -> str | None:
        """Resolve and, when explicitly supplied, bind telemetry channel to an A2A context."""

        context_id = validate_protocol_id(context_id)
        requested_channel = normalize_telemetry_channel(requested_channel)
        async with self._mutation_lock:
            record = self._contexts.get(context_id)
            if record is not None:
                if requested_channel is not None and requested_channel != record.telemetry_channel:
                    record.telemetry_channel = requested_channel
                    record.touch()
                    self._mirror_context(record)
                return record.telemetry_channel

            snapshot = self._load_context_snapshot(context_id)
            stored_channel = self._pending_context_telemetry_channels.get(context_id)
            if stored_channel is None and snapshot is not None:
                stored_channel = snapshot.telemetry_channel
            effective_channel = requested_channel or stored_channel
            if effective_channel is None:
                return None

            self._pending_context_telemetry_channels[context_id] = effective_channel
            if snapshot is not None and snapshot.telemetry_channel != effective_channel:
                snapshot = A2AContextSnapshot(
                    context_id=snapshot.context_id,
                    session_id=snapshot.session_id,
                    cwd=snapshot.cwd,
                    telemetry_channel=effective_channel,
                    active_task_id=snapshot.active_task_id,
                    updated_at=time.time(),
                )
                self._persist_context_snapshot(snapshot)
            return effective_channel

    async def get_context_record(self, context_id: str) -> A2AContextRecord:
        context_id = validate_protocol_id(context_id)
        async with self._mutation_lock:
            record = self._contexts.get(context_id)
            if record is not None:
                return A2AContextRecord(
                    context_id=record.context_id,
                    session_id=record.session_id,
                    cwd=record.cwd,
                    telemetry_channel=record.telemetry_channel,
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
                        telemetry_channel=snapshot.telemetry_channel,
                        active_task_id=snapshot.active_task_id,
                    )

        raise ValueError(_("A2A context not found"))

    async def activate_restored_task(self, task: A2ATaskRecord, context: A2AContextRecord) -> A2AContextRecord:
        """Attach a permission recovery to live records without creating a cached runtime."""
        async with self._mutation_lock:
            record = self._contexts.setdefault(context.context_id, context)
            if record.lock is None:
                record.lock = asyncio.Lock()
            record.active_task_id = task.task_id
            record.touch()
            task.state = TASK_STATE_WORKING
            task.active_task = asyncio.current_task()
            task.touch()
            return record

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

    async def discard_context_runtime(self, context_id: str, *, persist_context: bool = True) -> None:
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
            # Runtime is memory-only. Termination commits the durable task and
            # context together later, outside the global lock and event loop.
            # Permission cancellation can also reach here through its suspend
            # callback before the termination cleanup calls us directly.
            control = (
                self._execution_control_snapshot_provider(context_id)
                if self._execution_control_snapshot_provider is not None
                else None
            )
            terminating = control is not None and control.get("phase") in {"terminating", "terminated"}
            if persist_context and not terminating:
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
                    telemetry_channel=snapshot.telemetry_channel,
                    lock=asyncio.Lock(),
                )
                self._contexts[context_id] = record
            elif record.session_id != session_id or record.cwd != cwd:
                raise ValueError(_("A2A context belongs to a different workspace"))
            elif record.telemetry_channel is None:
                record.telemetry_channel = snapshot.telemetry_channel
            runtime = record.runtime
            record.runtime = None
            record.active_task_id = None if clear_active_task_for_proven_handoff else snapshot.active_task_id
            record.touch()
            self._mirror_context(record)
            refreshed = A2AContextRecord(
                context_id=record.context_id,
                session_id=record.session_id,
                cwd=record.cwd,
                telemetry_channel=record.telemetry_channel,
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
                    expected_permission_backup_generation=record.expected_permission_backup_generation,
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

    async def cancel_inactive_input_required_task(self, *, task_id: str, context_id: str) -> bool:
        """Terminalize a detached input wait without overwriting a finished turn."""
        return await self.commit_inactive_execution_task(task_id=task_id, context_id=context_id, cancel_wait=True)

    async def commit_inactive_execution_task(
        self, *, task_id: str, context_id: str, cancel_wait: bool = False
    ) -> bool:
        """Strictly commit an execution's final task/context snapshots off the event loop."""

        task_id = validate_protocol_id(task_id)
        context_id = validate_protocol_id(context_id)
        allowed_states = {TASK_STATE_INPUT_REQUIRED, TASK_STATE_CANCELED}
        if not cancel_wait:
            allowed_states.update({TASK_STATE_COMPLETED, TASK_STATE_FAILED})
        async with self._termination_commit_locks.setdefault(context_id, asyncio.Lock()):
            async with self._mutation_lock:
                record = self._tasks.get(task_id)
                if (
                    record is None
                    or record.context_id != context_id
                    or (record.active_task is not None and not record.active_task.done())
                    or record.state not in allowed_states
                ):
                    return False
                if cancel_wait:
                    record.state = TASK_STATE_CANCELED
                record.active_task = None
                record.touch()
                self._pending_permissions.pop(task_id, None)
                context = self._contexts.get(context_id)
                if context is not None and context.active_task_id == task_id:
                    context.active_task_id = None
                    context.touch()
                for owner_tasks in self._sdk_tasks.values():
                    task = owner_tasks.get(task_id)
                    if task is None or task.context_id != context_id:
                        continue
                    if not cancel_wait:
                        task.status.state = TaskState.Value("TASK_STATE_" + record.state.upper().replace("-", "_"))
                        continue
                    task.status.CopyFrom(
                        TaskStatus(
                            state=TaskState.Name(TaskState.TASK_STATE_CANCELED),
                            message=Message(
                                message_id=f"{task_id}-terminated",
                                task_id=task_id,
                                context_id=context_id,
                                role=Role.ROLE_AGENT,
                                parts=[Part(text=_("Task canceled."))],
                            ),
                        )
                    )
                    task.status.timestamp.GetCurrentTime()
                    self._attach_context_metadata(task)
                    self._attach_pending_permissions(task)
                if context is None:
                    raise OSError("A2A terminated context snapshot is unavailable")
                task_snapshot = A2ATaskSnapshot(
                    task_id=record.task_id,
                    context_id=record.context_id,
                    state=record.state,
                    owner=record.owner,
                    output_text=list(record.output_text),
                    updated_at=record.updated_at,
                    expected_permission_backup_generation=record.expected_permission_backup_generation,
                )
                context_snapshot = A2AContextSnapshot(
                    context_id=context.context_id,
                    session_id=context.session_id,
                    cwd=context.cwd,
                    telemetry_channel=context.telemetry_channel,
                    active_task_id=context.active_task_id,
                )
                self._task_persistence_dirty.add(task_id)
            await run_sync_fenced(self._persist_terminated_task_snapshots_strict, task_snapshot, context_snapshot)
            async with self._mutation_lock:
                if (
                    self._tasks.get(task_id) is not record
                    or self._contexts.get(context_id) is not context
                    or record.updated_at != task_snapshot.updated_at
                    or context.session_id != context_snapshot.session_id
                    or context.cwd != context_snapshot.cwd
                    or context.telemetry_channel != context_snapshot.telemetry_channel
                    or record.state != task_snapshot.state
                    or context.active_task_id is not None
                ):
                    raise OSError("A2A termination snapshot changed during commit; retry termination")
                self._task_persistence_dirty.discard(task_id)
            return True

    def _persist_terminated_task_snapshots_strict(
        self,
        task_snapshot: A2ATaskSnapshot,
        context_snapshot: A2AContextSnapshot,
    ) -> None:
        """Write detached snapshots in a worker; never access mutable task-store records."""

        if self._persistence is not None:
            self._persistence.save_task(task_snapshot)
            persisted_task = self._persistence.load_task(task_snapshot.task_id)
            if persisted_task is None or persisted_task.state != task_snapshot.state:
                raise OSError("A2A terminated task snapshot verification failed")
        session_paths = _session_paths_for_a2a_snapshot(context_snapshot)
        if session_paths is None:
            raise OSError("A2A terminated task session path is unavailable")
        _write_session_snapshot(session_paths.session_dir, session_paths.a2a_task_path, asdict(task_snapshot))
        persisted_session_task = json.loads(session_paths.a2a_task_path.read_text(encoding="utf-8"))
        if (
            persisted_session_task.get("task_id") != task_snapshot.task_id
            or persisted_session_task.get("state") != task_snapshot.state
        ):
            raise OSError("A2A terminated session task snapshot verification failed")

        if self._persistence is not None:
            self._persistence.save_context(context_snapshot)
            persisted_context = self._persistence.load_context(context_snapshot.context_id)
            if persisted_context is None or persisted_context.active_task_id is not None:
                raise OSError("A2A terminated context snapshot verification failed")
        _write_session_snapshot(session_paths.session_dir, session_paths.a2a_context_path, asdict(context_snapshot))
        persisted_session_context = json.loads(session_paths.a2a_context_path.read_text(encoding="utf-8"))
        if (
            persisted_session_context.get("context_id") != context_snapshot.context_id
            or persisted_session_context.get("active_task_id") is not None
        ):
            raise OSError("A2A terminated session context snapshot verification failed")

    async def cancel_task_and_wait(self, task_id: str, *, timeout: float | None = None) -> bool:
        task_id = validate_protocol_id(task_id)
        async with self._mutation_lock:
            record = self._tasks.get(task_id)
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
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for canceled A2A task %s to finish", task_id)
            return False
        return await self._complete_cancel_task_wait(
            task_id=task_id,
            canceled_owner=active_task,
        )

    async def _complete_cancel_task_wait(
        self,
        *,
        task_id: str,
        canceled_owner: asyncio.Task[Any],
    ) -> bool:
        async with self._mutation_lock:
            record = self._tasks.get(task_id)
            if record is None:
                return True
            current_owner = record.active_task
            if current_owner is not None and current_owner is not canceled_owner and not current_owner.done():
                return False
            if current_owner is not None:
                record.active_task = None

            context = self._contexts.get(record.context_id)
            if context is not None and context.active_task_id == task_id:
                context.active_task_id = None
                context.touch()
                self._mirror_context(context)
            return True

    async def is_task_active(self, task_id: str) -> bool:
        async with self._mutation_lock:
            return self._task_is_active_locked(task_id)

    async def has_active_work(self) -> bool:
        """Return whether shutting down would interrupt in-process A2A work."""
        async with self._mutation_lock:
            return (
                any(record.active_task is not None and not record.active_task.done() for record in self._tasks.values())
                or any(not task.done() for task in self._context_runtime_tasks.values())
                or any(
                    not task.done() for starts in self._context_execution_starts.values() for task in starts.values()
                )
                or any(self._context_reconciliation_waiters.values())
                or any(lock.locked() for lock in self._reconciliation_locks.values())
                or any(count > 0 for count in self._context_runtime_waiters.values())
                or any(not task.done() for task in self._discarded_context_runtime_tasks)
                or bool(self._execution_control_active_probe and self._execution_control_active_probe())
                or bool(self._permission_wait_active_probe and self._permission_wait_active_probe())
            )

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
                and not self._execution_retains_context(context_id)
            ]
            for context_id in expired_context_ids:
                # Closing an earlier runtime can yield while a later context
                # accepts pause/resume. Recheck its hold and idle timestamp.
                record = self._contexts.get(context_id)
                if (
                    record is None
                    or now - record.last_active <= self._idle_timeout_seconds
                    or self._execution_retains_context(context_id)
                ):
                    continue
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
                    self._task_persistence_dirty.discard(task_id)
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
            self._pending_context_telemetry_channels.clear()
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

    def _mirror_task(self, record: A2ATaskRecord, *, persist_shared_snapshot: bool = True) -> None:
        if self._execution_is_terminating(record.context_id):
            # Final snapshots are committed after all execution participants drain.
            # This also covers SDK saves and executor finally blocks on slow OSS.
            self._task_persistence_dirty.add(record.task_id)
            return
        snapshot = A2ATaskSnapshot(
            task_id=record.task_id,
            context_id=record.context_id,
            state=record.state,
            owner=record.owner,
            output_text=list(record.output_text),
            updated_at=record.updated_at,
            expected_permission_backup_generation=record.expected_permission_backup_generation,
        )
        if self._persistence is not None and persist_shared_snapshot:
            try:
                self._persistence.save_task(snapshot)
                self._task_persistence_dirty.discard(record.task_id)
            except Exception:
                self._task_persistence_dirty.add(record.task_id)
                logger.exception("Failed to persist A2A task %s", record.task_id)
        self._mirror_session_task(snapshot)

    def _mirror_context(self, record: A2AContextRecord) -> None:
        snapshot = A2AContextSnapshot(
            context_id=record.context_id,
            session_id=record.session_id,
            cwd=record.cwd,
            telemetry_channel=record.telemetry_channel,
            active_task_id=record.active_task_id,
        )
        self._persist_context_snapshot(snapshot)

    def _persist_context_snapshot(self, snapshot: A2AContextSnapshot) -> None:
        if self._execution_is_terminating(snapshot.context_id):
            return
        if self._persistence is not None:
            try:
                self._persistence.save_context(snapshot)
            except Exception:
                logger.exception("Failed to persist A2A context %s", snapshot.context_id)
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


def _ensure_new_a2a_session_layout(cwd: str, session_id: str, backup_service: Any) -> None:
    try:
        SessionStorage().ensure_v2_session_dir_for_new_session(cwd, session_id)
    except Exception as exc:
        logger.warning(
            "Failed to prepare A2A session layout error_type=%s",
            type(exc).__name__,
        )
        return
    try:
        backup_service.initialize_session(cwd, session_id)
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
        telemetry_channel=normalize_telemetry_channel(payload.get("telemetry_channel")),
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
        expected_permission_backup_generation=snapshot.expected_permission_backup_generation,
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
