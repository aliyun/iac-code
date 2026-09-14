"""Transport-independent control for a live A2A execution.

The public A2A task state remains unchanged.  This module owns the connection
pause gate, its durable control snapshot, and the local termination workflow.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal, TypeVar, cast

from iac_code.a2a.backup import await_fenced, run_sync_fenced, run_sync_fenced_with_cancel_completion
from iac_code.services.session_backup import BackupReason, BackupResult
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock

ExecutionPhase = Literal[
    "running",
    "pausing",
    "pause_committing",
    "paused",
    "resuming",
    "terminating",
    "terminated",
]

_PAUSE_PHASES = frozenset({"pausing", "pause_committing", "paused"})
_TERMINAL_TASK_STATES = frozenset({"completed", "failed", "canceled", "input-required", "normal-turn-ended"})
_RECOVERABLE_INPUT_ADMISSION_TTL_SECONDS = 60.0
_CURRENT_CONTROL: ContextVar[Any] = ContextVar("a2a_execution_control", default=None)
_CURRENT_ACTIVITY_IDS: ContextVar[tuple[str, ...]] = ContextVar("a2a_execution_activity_ids", default=())
_CURRENT_PARTICIPANT_IDS: ContextVar[tuple[str, ...]] = ContextVar("a2a_execution_participant_ids", default=())
_T = TypeVar("_T")


class ExecutionControlError(RuntimeError):
    """Base error returned by execution-control endpoints."""


class ExecutionControlNotFoundError(ExecutionControlError):
    pass


class ExecutionControlConflictError(ExecutionControlError):
    pass


class ExecutionTerminatedError(asyncio.CancelledError):
    """Raised at a safe point after termination has been claimed."""


def _utc_timestamp(epoch_seconds: float | None) -> str | None:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(action: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps({"action": action, **payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class _Activity:
    activity_id: str
    kind: str
    task: asyncio.Task[Any] | None
    ancestor_activity_ids: tuple[str, ...]
    handoff_participant_ids: tuple[str, ...]
    blocked: bool = False
    budget_changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _RecoverableInputAdmission:
    token: str
    context_id: str
    task_id: str
    owner: str
    expires_at: float

    def as_document(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "contextId": self.context_id,
            "taskId": self.task_id,
            "owner": self.owner,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True)
class _RecoverableInputActivation:
    previous_control: dict[str, Any] | None
    execution_id: str


class _RecoverableInputAdmissionStore:
    """One-request reservation store with a cross-process file fence when persistence is enabled."""

    def __init__(self, persistence_root: Path | None) -> None:
        self._root = persistence_root / "execution-control" if persistence_root is not None else None
        self._by_token: dict[str, _RecoverableInputAdmission] = {}
        self._token_by_context: dict[str, str] = {}

    def reserve(self, admission: _RecoverableInputAdmission) -> bool:
        if self._root is None:
            if admission.context_id in self._token_by_context:
                return False
            self._remember(admission)
            return True
        path = self._admission_path(admission.context_id)
        with cross_process_file_lock(self._lock_path(admission.context_id)):
            existing = self._load_document(path)
            if existing is not None and not self._expired(existing):
                return False
            if not self._persisted_control_allows_recovery(admission):
                return False
            if existing is not None:
                path.unlink(missing_ok=True)
            atomic_write_json(path, admission.as_document())
        self._remember(admission)
        return True

    def activate(
        self,
        admission: _RecoverableInputAdmission,
        control_snapshot: dict[str, Any],
    ) -> _RecoverableInputActivation | None:
        """Fence one admission while publishing its running control."""
        if self._root is None:
            if self._by_token.get(admission.token) != admission or self._expired(admission.as_document()):
                return None
            return _RecoverableInputActivation(
                previous_control=None,
                execution_id=str(control_snapshot["executionId"]),
            )
        admission_path = self._admission_path(admission.context_id)
        with cross_process_file_lock(self._lock_path(admission.context_id)):
            document = self._load_document(admission_path)
            if document is None or self._expired(document) or not self._matches(document, admission):
                return None
            # A different worker may have advanced the shared control after this
            # process inspected its stale local controller. Re-check while holding
            # the same fence used by every recovery reservation and activation.
            if not self._persisted_control_allows_recovery(admission):
                return None
            revision = int(control_snapshot["revision"])
            persisted_snapshot = dict(control_snapshot)
            persisted_snapshot["persistedRevision"] = revision
            control_path = self._root / f"{admission.context_id}.json"
            previous_control = self._load_document(control_path)
            atomic_write_json(control_path, persisted_snapshot)
        return _RecoverableInputActivation(
            previous_control=previous_control,
            execution_id=str(control_snapshot["executionId"]),
        )

    def finish_activation(self, admission: _RecoverableInputAdmission) -> bool:
        if self._root is not None:
            path = self._admission_path(admission.context_id)
            with cross_process_file_lock(self._lock_path(admission.context_id)):
                document = self._load_document(path)
                if document is not None and self._matches(document, admission):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        return False
        self._forget(admission)
        return True

    def rollback_activation(
        self,
        admission: _RecoverableInputAdmission,
        activation: _RecoverableInputActivation,
    ) -> bool:
        if self._root is None:
            return True
        with cross_process_file_lock(self._lock_path(admission.context_id)):
            control_path = self._root / f"{admission.context_id}.json"
            current = self._load_document(control_path)
            if current is None or current.get("executionId") != activation.execution_id:
                return False
            if activation.previous_control is None:
                control_path.unlink(missing_ok=True)
            else:
                atomic_write_json(control_path, activation.previous_control)
        return True

    def can_begin_without_admission(
        self,
        context_id: str,
        local_execution_id: str | None,
        local_server_instance_id: str,
        local_input_handoff_ready: bool,
    ) -> bool:
        """Reject a new local controller when another process owns shared execution state."""
        if local_input_handoff_ready:
            return False
        if self._root is None:
            return True
        with cross_process_file_lock(self._lock_path(context_id)):
            ticket = self._load_document(self._admission_path(context_id))
            if ticket is not None and not self._expired(ticket):
                return False
            control = self._load_document(self._root / f"{context_id}.json")
            if control is None:
                return not local_input_handoff_ready
            if control.get("inputHandoffReady") is True:
                return False
            if local_execution_id is not None and (
                control.get("executionId") == local_execution_id
                or control.get("serverInstanceId") == local_server_instance_id
            ):
                return True
            return bool(control.get("phase") == "terminated" and control.get("releaseReady", False))

    def has_active(self, context_id: str) -> bool:
        if self._root is None:
            return context_id in self._token_by_context
        path = self._admission_path(context_id)
        with cross_process_file_lock(self._lock_path(context_id)):
            document = self._load_document(path)
            if document is None:
                return False
            if self._expired(document):
                path.unlink(missing_ok=True)
                return False
            return True

    def release(self, token: str) -> None:
        admission = self._by_token.get(token)
        if admission is None:
            return
        if self._root is not None:
            path = self._admission_path(admission.context_id)
            with cross_process_file_lock(self._lock_path(admission.context_id)):
                document = self._load_document(path)
                if document is not None and self._matches(document, admission):
                    path.unlink(missing_ok=True)
        self._forget(admission)

    def get(self, token: str | None) -> _RecoverableInputAdmission | None:
        return self._by_token.get(token or "")

    def close(self) -> None:
        for token in tuple(self._by_token):
            self.release(token)

    def _persisted_control_allows_recovery(self, admission: _RecoverableInputAdmission) -> bool:
        assert self._root is not None
        path = self._root / f"{admission.context_id}.json"
        if not path.exists():
            return True
        document = self._load_document(path)
        if document is None or document.get("taskId") != admission.task_id:
            return False
        if document.get("inputHandoffReady") is True:
            return True
        if document.get("phase") != "terminated":
            return False
        backup = document.get("backup")
        backup_status = backup.get("status") if isinstance(backup, dict) else None
        return bool(document.get("releaseReady", False) or backup_status == "blocked")

    def _remember(self, admission: _RecoverableInputAdmission) -> None:
        self._by_token[admission.token] = admission
        self._token_by_context[admission.context_id] = admission.token

    def _forget(self, admission: _RecoverableInputAdmission) -> None:
        self._by_token.pop(admission.token, None)
        if self._token_by_context.get(admission.context_id) == admission.token:
            self._token_by_context.pop(admission.context_id, None)

    def _admission_path(self, context_id: str) -> Path:
        assert self._root is not None
        return self._root / f".{context_id}.recoverable-input.json"

    def _lock_path(self, context_id: str) -> Path:
        assert self._root is not None
        return self._root / f".{context_id}.recoverable-input.lock"

    @staticmethod
    def _load_document(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"expiresAt": math.inf}
        return value if isinstance(value, dict) else {"expiresAt": math.inf}

    @staticmethod
    def _expired(document: dict[str, Any]) -> bool:
        expires_at = document.get("expiresAt")
        return isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and expires_at <= time.time()

    @staticmethod
    def _matches(document: dict[str, Any], admission: _RecoverableInputAdmission) -> bool:
        return bool(
            document.get("token") == admission.token
            and document.get("contextId") == admission.context_id
            and document.get("taskId") == admission.task_id
            and document.get("owner") == admission.owner
        )


class RecoverableInputAdmissionCarrier:
    """Attach a server-issued admission to one queued SDK RequestContext."""

    _ATTRIBUTE = "_iac_code_recoverable_input_admission"

    @classmethod
    def attach(cls, request_context: Any, admission: str | None) -> None:
        setattr(request_context, cls._ATTRIBUTE, admission)

    @classmethod
    def read(cls, request_context: Any) -> str | None:
        admission = getattr(request_context, cls._ATTRIBUTE, None)
        return admission if isinstance(admission, str) and admission else None


@dataclass
class _Participant:
    participant_id: str
    kind: str
    task: asyncio.Task[Any]
    safe: bool = False


@dataclass(frozen=True)
class ActivityHandle:
    controller: ExecutionController
    activity_id: str

    @property
    def blocked(self) -> bool:
        return self.controller._activity_budget_blocked(self.activity_id)

    @property
    def budget_changed(self) -> asyncio.Event:
        activity = self.controller._activities.get(self.activity_id)
        if activity is None:
            event = asyncio.Event()
            event.set()
            return event
        return activity.budget_changed


class ExecutionController:
    """Single-event-loop state machine for one context execution instance."""

    def __init__(
        self,
        *,
        context_id: str,
        task_id: str,
        owner: str,
        cwd: str,
        server_instance_id: str,
        persistence_path: Path | None,
        backup_service: Any | None,
        termination_cleanup: Callable[[str, str, str], Awaitable[str | None]] | None = None,
        on_resume: Callable[[str], None] | None = None,
        input_handoff_commit: Callable[[ExecutionController, dict[str, Any]], Awaitable[None]] | None = None,
        execution_id: str | None = None,
    ) -> None:
        self.context_id = context_id
        self.task_id = task_id
        self.owner = owner
        self.cwd = cwd
        self.session_id: str | None = None
        self.execution_id = execution_id or "exec-" + uuid.uuid4().hex
        self.server_instance_id = server_instance_id
        self.phase: ExecutionPhase = "running"
        self.execution_status = "working"
        self.stream_available = True
        self.pause_id: str | None = None
        self.pause_reason: str | None = None
        self.expires_at: float | None = None
        self._expires_monotonic: float | None = None
        self.connection_epoch = -1
        self.revision = 0
        self.persisted_revision = 0
        self.release_ready = False
        self.termination_reason: str | None = None
        self._termination_pause_id: str | None = None
        self.backup: dict[str, Any] = {"status": "not_requested"}
        self.external_operations: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._commit_lock = asyncio.Lock()
        self._operation_commit_lock = asyncio.Lock()
        self._activities: dict[str, _Activity] = {}
        self._participants: dict[str, _Participant] = {}
        self._participant_ids_by_task: dict[asyncio.Task[Any], str] = {}
        self._passive_waiters: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._execution_tasks: set[asyncio.Task[Any]] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._request_ids: dict[str, tuple[str, str, str | None]] = {}
        self._connection_hold = False
        self._resume_barrier_revision: int | None = None
        self._pause_generation = 0
        self._commit_error: str | None = None
        self._release_commit_inflight = False
        self._backup_state_committed = False
        self._pending_staged_backup: BackupResult | None = None
        self._persistence_path = persistence_path
        self._backup_service = backup_service
        self._termination_cleanup = termination_cleanup
        self._on_resume = on_resume
        self._input_handoff_commit = input_handoff_commit
        self._durable_input_handoff_enabled = False
        self._termination_cleanup_complete = termination_cleanup is None
        self._termination_cleanup_inflight = False

    def bind_session(self, session_id: str) -> None:
        self.session_id = session_id

    def enable_durable_input_handoff(self) -> None:
        self._durable_input_handoff_enabled = True

    def disable_durable_input_handoff(self) -> None:
        self._durable_input_handoff_enabled = False

    def input_handoff_ready(self) -> bool:
        return bool(
            self._durable_input_handoff_enabled
            and self.phase == "running"
            and self.execution_status == "input-required"
            and not self.stream_available
            and not self.has_managed_work()
            and not any(not task.done() for task in self._background_tasks)
        )

    def permission_suspension_allowed(self) -> bool:
        return self.phase == "running" and not self._connection_hold and self._resume_barrier_revision is None

    async def run_permission_suspension(self, operation: Callable[[], Awaitable[bool]]) -> bool | None:
        """Reserve automatic permission cleanup atomically with the connection hold."""
        async with self._condition:
            await self._condition.wait_for(
                lambda: self.permission_suspension_allowed() or self.phase in {"terminating", "terminated"}
            )
            if self.phase in {"terminating", "terminated"}:
                return None
            activity_id = uuid.uuid4().hex
            self._activities[activity_id] = _Activity(
                activity_id=activity_id,
                kind="permission_cleanup",
                task=asyncio.current_task(),
                ancestor_activity_ids=(),
                handoff_participant_ids=(),
            )
            self._invalidate_pause_commit_locked()
        try:
            # A decision or termination can cancel the timer while its worker
            # is writing the checkpoint or closing the runtime. Drain that
            # cleanup before allowing pause/termination to declare completion.
            return await await_fenced(operation())
        finally:
            await self.end_activity(activity_id)

    async def attach_task(self, task: asyncio.Task[Any], *, mark_working: bool = True) -> None:
        async with self._condition:
            if self.phase in {"terminating", "terminated"}:
                raise ExecutionControlConflictError("Execution is terminating")
            self._execution_tasks.add(task)
            participant_id = self._ensure_participant_locked(task, "execution")
            participant_ids = _CURRENT_PARTICIPANT_IDS.get()
            if participant_id not in participant_ids:
                _CURRENT_PARTICIPANT_IDS.set((*participant_ids, participant_id))
            self.stream_available = True
            if mark_working:
                self.execution_status = "working"
            self._invalidate_pause_commit_locked()
            self._condition.notify_all()

    def register_spawned_task(self, task: asyncio.Task[Any], *, kind: str) -> None:
        """Synchronously reserve a newly-created Task before it can be scheduled."""

        if self.phase in {"terminating", "terminated"}:
            task.cancel()
            raise ExecutionTerminatedError()
        self._ensure_participant_locked(task, kind)
        self._invalidate_pause_commit_locked()

    async def mark_execution_started(self) -> None:
        async with self._condition:
            self.execution_status = "working"
            self.stream_available = True

    async def mark_execution_status(self, execution_status: str) -> None:
        """Keep control-plane status aligned when a detached producer finishes."""

        async with self._condition:
            self.execution_status = execution_status
            self._condition.notify_all()

    async def rollover_normal_execution(self, *, task_id: str, cwd: str) -> None:
        """Give a new normal turn a fresh identity while retaining live background participants."""

        async with self._condition:
            if self.phase != "running" or self.stream_available or not self.has_managed_work():
                raise ExecutionControlConflictError("Execution cannot roll over while its foreground is active")
            self.task_id = task_id
            self.cwd = cwd
            self.execution_id = "exec-" + uuid.uuid4().hex
            self.execution_status = "working"
            self.stream_available = False
            self.pause_id = None
            self.pause_reason = None
            self.expires_at = None
            self._expires_monotonic = None
            self.connection_epoch = -1
            self.release_ready = False
            self.termination_reason = None
            self._termination_pause_id = None
            self.backup = {"status": "not_requested"}
            self._request_ids.clear()
            self._commit_error = None
            self._backup_state_committed = False
            self._pending_staged_backup = None
            self._termination_cleanup_complete = self._termination_cleanup is None
            self._termination_cleanup_inflight = False
            self.revision += 1
            snapshot = self.snapshot()
        await self._persist_snapshot(snapshot)

    async def detach_task(self, task: asyncio.Task[Any], *, execution_status: str) -> None:
        handoff_snapshot: dict[str, Any] | None = None
        handoff_commit = self._input_handoff_commit
        async with self._condition:
            self._execution_tasks.discard(task)
            self._remove_participant_locked(task)
            if not self._execution_tasks:
                self.stream_available = False
                if execution_status != "working" or self.execution_status == "working":
                    self.execution_status = execution_status
            self._condition.notify_all()
            self._schedule_pause_commit_locked()
            self._maybe_mark_release_ready_locked()
            if self.input_handoff_ready() and handoff_commit is not None:
                self.revision += 1
                handoff_snapshot = self.snapshot()
        if handoff_snapshot is not None and handoff_commit is not None:
            await handoff_commit(self, handoff_snapshot)

    async def pause(
        self,
        *,
        task_id: str,
        expected_execution_id: str,
        request_id: str,
        connection_epoch: int,
        reason: str,
        reconnect_timeout_seconds: float,
    ) -> dict[str, Any]:
        if not math.isfinite(reconnect_timeout_seconds):
            raise ValueError("reconnectTimeoutSeconds must be finite")
        payload = {
            "contextId": self.context_id,
            "taskId": task_id,
            "executionId": expected_execution_id,
            "connectionEpoch": connection_epoch,
            "reason": reason,
            "reconnectTimeoutSeconds": reconnect_timeout_seconds,
        }
        fingerprint = _fingerprint("pause", payload)
        async with self._condition:
            self._validate_target_locked(task_id=task_id, execution_id=expected_execution_id)
            duplicate = self._idempotent_locked(request_id, "pause", fingerprint)
            if duplicate:
                if self.phase == "pause_committing" and self._commit_error is not None:
                    self._commit_error = None
                    self._spawn(
                        self._finish_pause(
                            self._pause_generation,
                            self.pause_id,
                            self.revision,
                            self._paused_commit_target_locked(),
                        ),
                        "retry-pause-commit",
                    )
                return self.snapshot()
            self._validate_epoch_locked(connection_epoch)
            if self.phase == "resuming":
                raise ExecutionControlConflictError("Execution is resuming; retry pause after it reaches running")
            if self.phase in {"terminating", "terminated"}:
                raise ExecutionControlConflictError("Execution is terminating or terminated")
            if self.phase in _PAUSE_PHASES:
                raise ExecutionControlConflictError("A different pause request is already active")
            if reconnect_timeout_seconds < 1 or reconnect_timeout_seconds > 3600:
                raise ExecutionControlConflictError("reconnectTimeoutSeconds must be between 1 and 3600")

            loop = asyncio.get_running_loop()
            self._pause_generation += 1
            generation = self._pause_generation
            self.pause_id = "pause-" + uuid.uuid4().hex
            self.pause_reason = reason
            self._connection_hold = True
            self._resume_barrier_revision = None
            self.phase = "pausing"
            self.revision += 1
            self.connection_epoch = connection_epoch
            self._expires_monotonic = loop.time() + reconnect_timeout_seconds
            self.expires_at = time.time() + reconnect_timeout_seconds
            self._request_ids[request_id] = ("pause", fingerprint, self.pause_id)
            self._refresh_passive_waiters_locked()
            snapshot = self.snapshot()
            self._spawn(self._persist_snapshot(snapshot), "persist-pausing")
            self._spawn(self._deadline(generation, self.pause_id, reconnect_timeout_seconds), "pause-deadline")
            self._schedule_pause_commit_locked()
            self._condition.notify_all()
            return snapshot

    async def resume(
        self,
        *,
        execution_id: str,
        pause_id: str,
        request_id: str,
        connection_epoch: int,
    ) -> dict[str, Any]:
        payload = {
            "contextId": self.context_id,
            "executionId": execution_id,
            "pauseId": pause_id,
            "connectionEpoch": connection_epoch,
        }
        fingerprint = _fingerprint("resume", payload)
        should_terminate = False
        async with self._condition:
            self._validate_target_locked(task_id=self.task_id, execution_id=execution_id)
            duplicate = self._idempotent_locked(request_id, "resume", fingerprint)
            if duplicate:
                if self.phase == "resuming" and self._commit_error is not None:
                    self._commit_error = None
                    if self.external_operations:
                        self._spawn(self._retry_external_operations_and_resume(), "retry-resume-external-operations")
                    else:
                        self._spawn(
                            self._finish_resume(self.revision, self._running_commit_target_locked()),
                            "retry-resume-commit",
                        )
                return self.snapshot()
            self._validate_epoch_locked(connection_epoch)
            if self.phase in {"terminating", "terminated"}:
                raise ExecutionControlConflictError("Execution is terminating or terminated")
            if self.phase == "running":
                raise ExecutionControlConflictError("Execution is not paused")
            if self.phase == "resuming":
                raise ExecutionControlConflictError("A different resume request is already active")
            if self.pause_id != pause_id:
                raise ExecutionControlConflictError("pauseId does not identify the active pause")
            loop = asyncio.get_running_loop()
            if self._expires_monotonic is not None and loop.time() >= self._expires_monotonic:
                self._claim_termination_locked("disconnect_timeout")
                should_terminate = True
                snapshot = self.snapshot()
            else:
                self._connection_hold = False
                self._pause_generation += 1
                self.phase = "resuming"
                self.revision += 1
                self.connection_epoch = connection_epoch
                self._resume_barrier_revision = self.revision
                self._refresh_passive_waiters_locked()
                self._request_ids[request_id] = ("resume", fingerprint, pause_id)
                snapshot = self.snapshot()
                self._spawn(
                    self._finish_resume(self.revision, self._running_commit_target_locked()),
                    "resume-commit",
                )
                self._condition.notify_all()
        if should_terminate:
            self._spawn(self._finish_termination(), "deadline-termination")
            raise ExecutionControlConflictError("Reconnect deadline has expired; termination was claimed")
        return snapshot

    async def terminate(
        self,
        *,
        execution_id: str,
        request_id: str,
        connection_epoch: int,
        reason: str,
        pause_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "contextId": self.context_id,
            "executionId": execution_id,
            "connectionEpoch": connection_epoch,
            "reason": reason,
            "pauseId": pause_id,
        }
        fingerprint = _fingerprint("terminate", payload)
        async with self._condition:
            self._validate_target_locked(task_id=self.task_id, execution_id=execution_id)
            duplicate = self._idempotent_locked(request_id, "terminate", fingerprint)
            if duplicate:
                self._retry_termination_if_needed_locked()
                return self.snapshot()
            self._validate_epoch_locked(connection_epoch)
            if reason == "disconnect_timeout":
                expected_pause_id = (
                    self._termination_pause_id if self.phase in {"terminating", "terminated"} else self.pause_id
                )
                if (
                    not pause_id
                    or pause_id != expected_pause_id
                    or (
                        self.phase not in {"terminating", "terminated"}
                        and (self.phase not in _PAUSE_PHASES or not self._connection_hold)
                    )
                ):
                    raise ExecutionControlConflictError("disconnect_timeout no longer identifies the active pause")
            self._request_ids[request_id] = ("terminate", fingerprint, pause_id)
            self.connection_epoch = connection_epoch
            if self.phase == "terminated":
                self._retry_termination_if_needed_locked()
                return self.snapshot()
            if self.phase == "terminating":
                return self.snapshot()
            self._claim_termination_locked(reason)
            snapshot = self.snapshot()
            self._spawn(self._finish_termination(), "explicit-termination")
            return snapshot

    async def checkpoint(self) -> None:
        async with self._condition:
            task = asyncio.current_task()
            participant_ids = _CURRENT_PARTICIPANT_IDS.get()
            if task is not None:
                own_id = self._ensure_participant_locked(task, "agent_loop")
                if own_id not in participant_ids:
                    participant_ids = (*participant_ids, own_id)
                    _CURRENT_PARTICIPANT_IDS.set(participant_ids)
                participant_ids = (own_id,)
            activity_ids = _CURRENT_ACTIVITY_IDS.get()
            while True:
                if self.phase in {"terminating", "terminated"}:
                    raise ExecutionTerminatedError()
                held = self._connection_hold or self._resume_barrier_revision is not None
                if not held:
                    self._set_activities_blocked_locked(activity_ids, False)
                    self._set_participants_safe_locked(participant_ids, False)
                    return
                self._set_activities_blocked_locked(activity_ids, True)
                self._set_participants_safe_locked(participant_ids, True)
                self._schedule_pause_commit_locked()
                await self._condition.wait()

    async def begin_activity(
        self,
        kind: str,
        *,
        check_gate: bool = True,
        handoff_to_parent: bool = False,
    ) -> ActivityHandle:
        inherited_participant_ids = _CURRENT_PARTICIPANT_IDS.get()
        handoff_participant_ids = inherited_participant_ids[-1:] if handoff_to_parent else ()
        if check_gate:
            await self.checkpoint()
        async with self._condition:
            if self.phase in {"terminating", "terminated"}:
                raise ExecutionTerminatedError()
            activity_id = uuid.uuid4().hex
            self._activities[activity_id] = _Activity(
                activity_id=activity_id,
                kind=kind,
                task=asyncio.current_task(),
                ancestor_activity_ids=_CURRENT_ACTIVITY_IDS.get(),
                handoff_participant_ids=handoff_participant_ids,
            )
            self._invalidate_pause_commit_locked()
            self._notify_activity_budgets_locked()
            self._condition.notify_all()
            return ActivityHandle(self, activity_id)

    async def begin_non_advancing_wait(self) -> str:
        async with self._condition:
            task = asyncio.current_task()
            participant_ids = _CURRENT_PARTICIPANT_IDS.get()
            if task is not None:
                own_id = self._ensure_participant_locked(task, "agent_loop")
                if own_id not in participant_ids:
                    participant_ids = (*participant_ids, own_id)
                    _CURRENT_PARTICIPANT_IDS.set(participant_ids)
                participant_ids = (own_id,)
            waiter_id = uuid.uuid4().hex
            activity_ids = _CURRENT_ACTIVITY_IDS.get()
            self._passive_waiters[waiter_id] = (participant_ids, activity_ids)
            self._set_participants_safe_locked(participant_ids, True)
            if self._connection_hold or self._resume_barrier_revision is not None:
                self._set_activities_blocked_locked(activity_ids, True)
            self._schedule_pause_commit_locked()
            self._condition.notify_all()
            return waiter_id

    async def end_non_advancing_wait(self, waiter_id: str) -> None:
        async with self._condition:
            waiter = self._passive_waiters.pop(waiter_id, None)
            if waiter is None:
                return
            participant_ids, activity_ids = waiter
            self._set_participants_safe_locked(participant_ids, False)
            if not self._connection_hold and self._resume_barrier_revision is None:
                self._set_activities_blocked_locked(activity_ids, False)
            self._refresh_passive_waiters_locked()
            self._invalidate_pause_commit_locked()
            self._condition.notify_all()

    async def end_activity(self, activity_id: str) -> None:
        async with self._condition:
            activity = self._activities.pop(activity_id, None)
            if activity is not None:
                # A child activity can finish while its parent is parked in a
                # non-advancing wait.  Keep the parent unsafe until it consumes
                # and durably records the child's result at its next checkpoint.
                self._revoke_passive_waiters_locked(activity.handoff_participant_ids)
                self._set_participants_safe_locked(activity.handoff_participant_ids, False)
                activity.budget_changed.set()
                self._notify_activity_budgets_locked()
                self._invalidate_pause_commit_locked()
            self._condition.notify_all()
            self._schedule_pause_commit_locked()
            self._maybe_mark_release_ready_locked()

    async def record_external_operation(
        self,
        *,
        product: str,
        action: str,
        outcome: Literal["accepted", "unknown"],
        resource_type: str | None = None,
        resource_id: str | None = None,
        region_id: str | None = None,
        tool_use_id: str | None = None,
    ) -> None:
        """Persist the result of a write request that completed while its caller was being cancelled."""
        operation = {
            "product": product,
            "action": action,
            "outcome": outcome,
            "resourceType": resource_type,
            "resourceId": resource_id,
            "regionId": region_id,
            "toolUseId": tool_use_id,
        }
        operation = {key: value for key, value in operation.items() if value is not None}
        async with self._condition:
            if operation not in self.external_operations:
                self.external_operations.append(operation)
                self.revision += 1
            resume_revision = self.revision if self.phase == "resuming" else None
            if resume_revision is not None:
                self._resume_barrier_revision = resume_revision
            snapshot = self.snapshot()
        try:
            await self._persist_external_operations()
            await self._persist_snapshot(snapshot)
        except Exception:
            async with self._condition:
                self._commit_error = "external_operation_commit_failed"
            raise
        if resume_revision is not None:
            async with self._condition:
                if (
                    self.phase == "resuming"
                    and self.revision == resume_revision
                    and self._resume_barrier_revision == resume_revision
                ):
                    self.revision += 1
                    self._resume_barrier_revision = self.revision
                    self._spawn(
                        self._finish_resume(self.revision, self._running_commit_target_locked()),
                        "resume-after-external-operation",
                    )

    def snapshot(self) -> dict[str, Any]:
        blockers: dict[str, int] = {}
        for participant in self._participants.values():
            if not participant.safe:
                blockers[participant.kind] = blockers.get(participant.kind, 0) + 1
        for activity in self._activities.values():
            if not activity.blocked:
                blockers[activity.kind] = blockers.get(activity.kind, 0) + 1
        return {
            "contextId": self.context_id,
            "taskId": self.task_id,
            "executionId": self.execution_id,
            "serverInstanceId": self.server_instance_id,
            "pauseId": self.pause_id,
            "pauseReason": self.pause_reason,
            "connectionEpoch": self.connection_epoch,
            "revision": self.revision,
            "persistedRevision": self.persisted_revision,
            "phase": self.phase,
            "pauseComplete": self.phase == "paused",
            "executionStatus": self.execution_status,
            "streamAvailable": self.stream_available,
            "blockers": [{"kind": kind, "count": count} for kind, count in sorted(blockers.items())],
            "expiresAt": _utc_timestamp(self.expires_at),
            "terminationReason": self.termination_reason,
            "commitError": self._commit_error,
            "backup": dict(self.backup),
            "externalOperations": [dict(operation) for operation in self.external_operations],
            "releaseReady": self.release_ready,
            "inputHandoffReady": self.input_handoff_ready(),
        }

    @staticmethod
    def protocol_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return the stable execution-control wire shape without coordination-only fields."""

        public_snapshot = dict(snapshot)
        public_snapshot.pop("inputHandoffReady", None)
        return public_snapshot

    def has_managed_work(self) -> bool:
        return bool(
            any(not task.done() for task in self._execution_tasks)
            or self._activities
            or any(not participant.task.done() for participant in self._participants.values())
        )

    def can_admit_recoverable_input_continuation(self, task_id: str) -> bool:
        """Return whether a sidecar-proven continuation can safely claim this control."""
        return bool(
            self.task_id == task_id
            and not self.has_managed_work()
            and not any(not task.done() for task in self._background_tasks)
            and (
                (
                    self.phase == "terminated"
                    and (self.release_ready or self.backup.get("status") == "blocked")
                )
                or self.input_handoff_ready()
            )
        )

    def can_replace_with_recoverable_input_continuation(self, task_id: str) -> bool:
        """Allow an admitted continuation to supersede a blocked terminal backup."""
        return bool(
            self.can_admit_recoverable_input_continuation(task_id)
            and (
                self.input_handoff_ready()
                or (not self.release_ready and self.backup.get("status") == "blocked")
            )
        )

    async def close(self) -> None:
        current = asyncio.current_task()
        managed_tasks = tuple(
            dict.fromkeys(
                task
                for task in (
                    *self._execution_tasks,
                    *(activity.task for activity in self._activities.values() if activity.task is not None),
                    *(participant.task for participant in self._participants.values()),
                )
                if task is not current and not task.done()
            )
        )
        for task in managed_tasks:
            task.cancel()
        if managed_tasks:
            await asyncio.gather(*managed_tasks, return_exceptions=True)
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _validate_target_locked(self, *, task_id: str, execution_id: str) -> None:
        if task_id != self.task_id or execution_id != self.execution_id:
            raise ExecutionControlConflictError("Execution identity does not match the current execution")

    def _validate_epoch_locked(self, connection_epoch: int) -> None:
        if connection_epoch < 0 or connection_epoch < self.connection_epoch:
            raise ExecutionControlConflictError("connectionEpoch is stale")

    def _idempotent_locked(self, request_id: str, action: str, fingerprint: str) -> bool:
        previous = self._request_ids.get(request_id)
        if previous is None:
            return False
        if previous[0] != action or previous[1] != fingerprint:
            raise ExecutionControlConflictError("requestId was already used with different content")
        return True

    def _set_activities_blocked_locked(self, activity_ids: tuple[str, ...], blocked: bool) -> None:
        changed = False
        for activity_id in activity_ids:
            activity = self._activities.get(activity_id)
            if activity is None or activity.blocked == blocked:
                continue
            activity.blocked = blocked
            activity.budget_changed.set()
            changed = True
        if changed:
            self._notify_activity_budgets_locked()

    def _notify_activity_budgets_locked(self) -> None:
        for activity in self._activities.values():
            activity.budget_changed.set()

    def _activity_budget_blocked(self, activity_id: str) -> bool:
        activity = self._activities.get(activity_id)
        if activity is None or not activity.blocked:
            return False
        return not any(
            activity_id in descendant.ancestor_activity_ids and not descendant.blocked
            for descendant in self._activities.values()
        )

    def _set_participants_safe_locked(self, participant_ids: tuple[str, ...], safe: bool) -> None:
        for participant_id in participant_ids:
            participant = self._participants.get(participant_id)
            if participant is not None:
                participant.safe = safe

    def _refresh_passive_waiters_locked(self) -> None:
        blocked = self._connection_hold or self._resume_barrier_revision is not None
        for participant_ids, activity_ids in self._passive_waiters.values():
            self._set_participants_safe_locked(participant_ids, True)
            self._set_activities_blocked_locked(activity_ids, blocked)

    def _revoke_passive_waiters_locked(self, participant_ids: tuple[str, ...]) -> None:
        if not participant_ids:
            return
        participant_id_set = set(participant_ids)
        for waiter_id, (waiter_participant_ids, _activity_ids) in tuple(self._passive_waiters.items()):
            if participant_id_set.intersection(waiter_participant_ids):
                self._passive_waiters.pop(waiter_id, None)

    def _ensure_participant_locked(self, task: asyncio.Task[Any], kind: str) -> str:
        participant_id = self._participant_ids_by_task.get(task)
        if participant_id is not None:
            return participant_id
        participant_id = uuid.uuid4().hex
        self._participant_ids_by_task[task] = participant_id
        self._participants[participant_id] = _Participant(participant_id, kind, task)

        def remove_finished(finished: asyncio.Task[Any]) -> None:
            try:
                self._spawn(self._remove_finished_participant(finished), "participant-finished")
            except RuntimeError:
                pass

        task.add_done_callback(remove_finished)
        return participant_id

    def _remove_participant_locked(self, task: asyncio.Task[Any]) -> None:
        participant_id = self._participant_ids_by_task.pop(task, None)
        if participant_id is not None:
            self._participants.pop(participant_id, None)

    async def _remove_finished_participant(self, task: asyncio.Task[Any]) -> None:
        async with self._condition:
            self._remove_participant_locked(task)
            self._schedule_pause_commit_locked()
            self._maybe_mark_release_ready_locked()
            self._condition.notify_all()

    def _maybe_mark_release_ready_locked(self) -> None:
        if (
            self.phase == "terminated"
            and self.backup.get("status") in {"disabled", "shared_committed"}
            and not self._execution_tasks
            and not self._activities
            and not self._participants
            and not self.release_ready
            and not self._release_commit_inflight
            and self._backup_state_committed
        ):
            self._release_commit_inflight = True
            self._spawn(self._commit_release_ready(), "release-ready")

    async def _commit_release_ready(self) -> None:
        async with self._condition:
            if (
                self.phase != "terminated"
                or self.backup.get("status") not in {"disabled", "shared_committed"}
                or self._execution_tasks
                or self._activities
                or self._participants
            ):
                self._release_commit_inflight = False
                return
            self.revision += 1
            revision = self.revision
            target = self.snapshot()
            target["releaseReady"] = True
            target["commitError"] = None
        try:
            await self._persist_snapshot(target)
        except Exception:
            async with self._condition:
                if self.revision == revision:
                    self._commit_error = "state_commit_failed"
                self._release_commit_inflight = False
            return
        async with self._condition:
            if self.phase == "terminated" and self.revision == revision:
                self.release_ready = True
                self._commit_error = None
            self._release_commit_inflight = False

    def _schedule_pause_commit_locked(self) -> None:
        if self.phase != "pausing" or not self._connection_hold:
            return
        if any(not activity.blocked for activity in self._activities.values()) or any(
            not participant.safe for participant in self._participants.values()
        ):
            return
        pause_id = self.pause_id
        generation = self._pause_generation
        self.phase = "pause_committing"
        self.revision += 1
        self._spawn(
            self._finish_pause(generation, pause_id, self.revision, self._paused_commit_target_locked()),
            "pause-commit",
        )

    def _invalidate_pause_commit_locked(self) -> None:
        if self.phase != "pause_committing" or not self._connection_hold:
            return
        if not any(not activity.blocked for activity in self._activities.values()) and not any(
            not participant.safe for participant in self._participants.values()
        ):
            return
        self.phase = "pausing"
        self.revision += 1
        self._spawn(self._persist_snapshot(self.snapshot()), "persist-pause-invalidated")

    def _paused_commit_target_locked(self) -> dict[str, Any]:
        target = self.snapshot()
        target["phase"] = "paused"
        target["pauseComplete"] = True
        return target

    def _running_commit_target_locked(self) -> dict[str, Any]:
        target = self.snapshot()
        target["phase"] = "running"
        target["pauseComplete"] = False
        target["expiresAt"] = None
        return target

    async def _finish_pause(
        self,
        generation: int,
        pause_id: str | None,
        revision: int,
        target: dict[str, Any],
    ) -> None:
        try:
            await self._persist_snapshot(target)
        except Exception:
            async with self._condition:
                if self.phase == "pause_committing" and self.revision == revision:
                    self._commit_error = "state_commit_failed"
            return
        async with self._condition:
            if (
                self.phase == "pause_committing"
                and self._connection_hold
                and self.pause_id == pause_id
                and self._pause_generation == generation
                and self.revision == revision
            ):
                self._commit_error = None
                self.phase = "paused"
                self._condition.notify_all()

    async def _finish_resume(self, revision: int, target: dict[str, Any]) -> None:
        try:
            await self._persist_snapshot(target)
        except Exception:
            async with self._condition:
                if self.phase == "resuming" and self.revision == revision:
                    self._commit_error = "state_commit_failed"
            return
        async with self._condition:
            if self.phase == "resuming" and self._resume_barrier_revision == revision and self.revision == revision:
                if self._on_resume is not None:
                    self._on_resume(self.context_id)
                self._commit_error = None
                self._resume_barrier_revision = None
                self.phase = "running"
                self.pause_id = None
                self.pause_reason = None
                self.expires_at = None
                self._expires_monotonic = None
                self._refresh_passive_waiters_locked()
                self._condition.notify_all()

    async def _deadline(self, generation: int, pause_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._condition:
            if (
                self._pause_generation != generation
                or self.pause_id != pause_id
                or self.phase not in _PAUSE_PHASES
                or not self._connection_hold
            ):
                return
            self._claim_termination_locked("disconnect_timeout")
        await self._finish_termination()

    def _claim_termination_locked(self, reason: str) -> None:
        self._termination_pause_id = self.pause_id if reason == "disconnect_timeout" else None
        self._pause_generation += 1
        self._connection_hold = False
        self._resume_barrier_revision = None
        self.phase = "terminating"
        self.revision += 1
        self.termination_reason = reason
        self.backup = {"status": "pending"}
        self._backup_state_committed = False
        self._termination_cleanup_complete = self._termination_cleanup is None
        self._termination_cleanup_inflight = False
        self.release_ready = False
        self._condition.notify_all()

    def _retry_termination_if_needed_locked(self) -> None:
        if self.phase != "terminated" or self.release_ready:
            return
        if not self._termination_cleanup_complete:
            if not self._termination_cleanup_inflight:
                self._termination_cleanup_inflight = True
                self.backup = {"status": "pending"}
                self._backup_state_committed = False
                self._spawn(self._retry_termination_cleanup(), "retry-termination-cleanup")
            return
        if self.backup.get("status") == "blocked":
            self.backup = {"status": "pending"}
            self._backup_state_committed = False
            self._spawn(self._retry_backup(), "retry-termination-backup")
        elif self._commit_error is not None:
            self._commit_error = None
            if self._backup_state_committed:
                self._maybe_mark_release_ready_locked()
            else:
                self._spawn(self._retry_backup_state_commit(), "retry-backup-state-commit")

    async def _finish_termination(self) -> None:
        current = asyncio.current_task()
        async with self._condition:
            cleanup = None
            if not self._termination_cleanup_complete and not self._termination_cleanup_inflight:
                self._termination_cleanup_inflight = True
                cleanup = self._termination_cleanup
            execution_tasks = tuple(task for task in self._execution_tasks if task is not current and not task.done())
            activity_tasks = tuple(
                activity.task
                for activity in self._activities.values()
                if activity.task is not None and activity.task is not current and not activity.task.done()
            )
            participant_tasks = tuple(
                participant.task
                for participant in self._participants.values()
                if participant.task is not current and not participant.task.done()
            )
        cleanup_error = await self._run_termination_cleanup(cleanup, mark_complete=False)
        tasks = tuple(dict.fromkeys((*execution_tasks, *activity_tasks, *participant_tasks)))
        for task in tasks:
            task.cancel()
        # A transport may reuse its producer Task after execute() returns (the
        # SDK does this for normal input-required turns). Its execution scope
        # ends at detach_task(), after executor cleanup, not at producer exit.
        async with self._condition:
            await self._condition.wait_for(
                lambda: all(task not in self._execution_tasks or task.done() for task in execution_tasks)
            )
        owned_tasks = tuple(task for task in tasks if task not in execution_tasks)
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)
        if cleanup_error is None:
            cleanup_error = await self._run_termination_cleanup(cleanup, mark_complete=True)
        async with self._condition:
            for task in tuple(self._participant_ids_by_task):
                if task.done():
                    self._remove_participant_locked(task)
            self.stream_available = False
            if self.execution_status not in _TERMINAL_TASK_STATES:
                self.execution_status = "canceled"
            self.phase = "terminated"
            self.revision += 1
            terminated = self.snapshot()
        try:
            await self._persist_snapshot(terminated)
        except Exception:
            async with self._condition:
                self._commit_error = "state_commit_failed"
        if cleanup_error is not None:
            await self._commit_blocked_backup_state(
                "execution termination cleanup failed",
                cleanup_error,
                clear_cleanup_inflight=True,
            )
            return
        await self._perform_backup()

    async def _run_termination_cleanup(
        self,
        cleanup: Callable[[str, str, str], Awaitable[str | None]] | None,
        *,
        mark_complete: bool = True,
    ) -> BaseException | None:
        if cleanup is None:
            return None
        try:
            execution_status = await cleanup(self.context_id, self.task_id, self.termination_reason or "terminated")
        except Exception as exc:
            return exc
        async with self._condition:
            if execution_status is not None:
                self.execution_status = execution_status
            if mark_complete:
                self._termination_cleanup_complete = True
                self._termination_cleanup_inflight = False
        return None

    async def _retry_termination_cleanup(self) -> None:
        error = await self._run_termination_cleanup(self._termination_cleanup)
        if error is not None:
            await self._commit_blocked_backup_state(
                "execution termination cleanup failed",
                error,
                clear_cleanup_inflight=True,
            )
            return
        await self._perform_backup()

    async def _retry_backup(self) -> None:
        await self._perform_backup()

    async def _perform_backup(self) -> None:
        try:
            await self._persist_external_operations()
        except Exception as exc:
            await self._commit_blocked_backup_state("external operation state could not be persisted", exc)
            return
        if self._backup_service is None or self.session_id is None:
            async with self._condition:
                self.backup = {"status": "disabled"}
                self._backup_state_committed = False
                self.release_ready = False
                self.revision += 1
                snapshot = self.snapshot()
                snapshot["commitError"] = None
            try:
                await self._persist_snapshot(snapshot)
            except Exception:
                async with self._condition:
                    self._commit_error = "state_commit_failed"
                return
            async with self._condition:
                self._commit_error = None
                self._backup_state_committed = True
                self._maybe_mark_release_ready_locked()
            return
        try:
            result = self._pending_staged_backup
            if result is None:
                result = await run_sync_fenced(
                    self._backup_service.backup_session,
                    self.cwd,
                    self.session_id,
                    reason=(
                        BackupReason.DISCONNECT_TIMEOUT
                        if self.termination_reason == "disconnect_timeout"
                        else BackupReason.TERMINAL
                    ),
                    critical=True,
                )
            if result.enabled and result.staged_committed and not result.shared_committed:
                wait_for_shared = getattr(self._backup_service, "wait_until_shared_committed", None)
                if not callable(wait_for_shared) or result.generation is None or result.commit_id is None:
                    raise RuntimeError("Staged backup cannot prove shared publication")
                result = cast(
                    BackupResult,
                    await run_sync_fenced(
                        wait_for_shared,
                        self.cwd,
                        self.session_id,
                        generation=result.generation,
                        commit_id=result.commit_id,
                    ),
                )
            succeeded = (not result.enabled) or (result.succeeded and result.shared_committed)
            self._pending_staged_backup = None if succeeded else result if result.staged_committed else None
            backup = {
                "status": "disabled" if not result.enabled else "shared_committed" if succeeded else "blocked",
                "generation": result.generation,
                "commitId": result.commit_id,
                "error": result.error,
            }
        except Exception as exc:
            succeeded = False
            backup = {"status": "blocked", "error": str(exc)}
        async with self._condition:
            self.backup = backup
            self._backup_state_committed = False
            self.release_ready = False
            self.revision += 1
            snapshot = self.snapshot()
            snapshot["commitError"] = None
        try:
            await self._persist_snapshot(snapshot)
        except Exception:
            async with self._condition:
                self._commit_error = "state_commit_failed"
            return
        if succeeded:
            async with self._condition:
                self._commit_error = None
                self._backup_state_committed = True
                self._maybe_mark_release_ready_locked()
        else:
            async with self._condition:
                self._commit_error = None
                self._backup_state_committed = True

    async def _commit_blocked_backup_state(
        self,
        message: str,
        exc: BaseException,
        *,
        clear_cleanup_inflight: bool = False,
    ) -> None:
        async with self._condition:
            self.backup = {"status": "blocked", "error": f"{message}: {type(exc).__name__}"}
            if clear_cleanup_inflight:
                # A retry that observes the blocked state must also be able to
                # claim cleanup. Publishing these two changes separately can
                # lose a retry in the gap between them.
                self._termination_cleanup_inflight = False
            self._backup_state_committed = False
            self.release_ready = False
            self.revision += 1
            revision = self.revision
            snapshot = self.snapshot()
            snapshot["commitError"] = None
        try:
            await self._persist_snapshot(snapshot)
        except Exception:
            async with self._condition:
                if self.revision == revision and self.backup.get("status") == "blocked":
                    self._commit_error = "state_commit_failed"
            return
        async with self._condition:
            if self.revision == revision and self.backup.get("status") == "blocked":
                self._commit_error = None
                self._backup_state_committed = True

    async def _persist_external_operations(self) -> None:
        if not self.external_operations or self.session_id is None:
            return
        async with self._operation_commit_lock:
            document = {
                "version": 1,
                "contextId": self.context_id,
                "taskId": self.task_id,
                "executionId": self.execution_id,
                "operations": [dict(operation) for operation in self.external_operations],
            }
            path = SessionStorage().session_dir(self.cwd, self.session_id) / "a2a" / "external-operations.json"
            await run_sync_fenced(atomic_write_json, path, document)

    async def _retry_backup_state_commit(self) -> None:
        async with self._condition:
            snapshot = self.snapshot()
            snapshot["commitError"] = None
            backup_complete = self.backup.get("status") in {"disabled", "shared_committed"}
        try:
            await self._persist_snapshot(snapshot)
        except Exception:
            async with self._condition:
                self._commit_error = "state_commit_failed"
            return
        async with self._condition:
            self._commit_error = None
            self._backup_state_committed = True
            if backup_complete:
                self._maybe_mark_release_ready_locked()

    async def _retry_external_operations_and_resume(self) -> None:
        try:
            await self._persist_external_operations()
        except Exception:
            async with self._condition:
                if self.phase == "resuming":
                    self._commit_error = "external_operation_commit_failed"
            return
        async with self._condition:
            if self.phase != "resuming":
                return
            self.revision += 1
            self._resume_barrier_revision = self.revision
            revision = self.revision
            target = self._running_commit_target_locked()
        await self._finish_resume(revision, target)

    async def _persist_snapshot(self, snapshot: dict[str, Any]) -> None:
        revision = int(snapshot["revision"])
        if self._persistence_path is None:
            self.persisted_revision = max(self.persisted_revision, revision)
            return
        async with self._commit_lock:
            if revision <= self.persisted_revision:
                return
            value = dict(snapshot)
            value["persistedRevision"] = revision
            await run_sync_fenced(atomic_write_json, self._persistence_path, value)
            self.persisted_revision = revision

    def _spawn(self, awaitable: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(awaitable)
        task.set_name(f"a2a-execution-{name}-{self.context_id}")
        self._background_tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled():
                with suppress(BaseException):
                    done.exception()

        task.add_done_callback(completed)
        return task


class ExecutionControlService:
    def __init__(self, *, persistence_root: Path | None, backup_service: Any | None) -> None:
        self.server_instance_id = "instance-" + uuid.uuid4().hex
        self._persistence_root = persistence_root
        self._backup_service = backup_service
        self._controls: dict[str, ExecutionController] = {}
        self._context_start_locks: dict[str, asyncio.Lock] = {}
        self._recoverable_input_admissions = _RecoverableInputAdmissionStore(persistence_root)
        self._termination_cleanup: Callable[[str, str, str], Awaitable[str | None]] | None = None
        self._on_resume: Callable[[str], None] | None = None

    def set_resume_callback(self, callback: Callable[[str], None]) -> None:
        self._on_resume = callback
        for control in self._controls.values():
            control._on_resume = callback

    def set_termination_cleanup(
        self,
        cleanup: Callable[[str, str, str], Awaitable[str | None]],
    ) -> None:
        self._termination_cleanup = cleanup
        for control in self._controls.values():
            control._termination_cleanup = cleanup

    async def _commit_input_handoff(
        self,
        control: ExecutionController,
        snapshot: dict[str, Any],
    ) -> None:
        """Publish a drained recovered input wait before another request can start locally."""
        async with self._context_start_locks.setdefault(control.context_id, asyncio.Lock()):
            if (
                self._controls.get(control.context_id) is not control
                or control.revision != snapshot.get("revision")
                or not control.input_handoff_ready()
            ):
                return
            await control._persist_snapshot(snapshot)

    async def begin_execution(
        self,
        *,
        context_id: str,
        task_id: str,
        owner: str,
        cwd: str,
        continue_input_required: bool = False,
        recoverable_input_admission: str | None = None,
    ) -> ExecutionController:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("A2A execution requires an asyncio Task")
        retired_control: ExecutionController | None = None
        async with self._context_start_locks.setdefault(context_id, asyncio.Lock()):
            control = self._controls.get(context_id)
            if control is not None and control.owner != owner:
                raise ExecutionControlNotFoundError("Execution was not found")
            admission = self._recoverable_input_admissions.get(recoverable_input_admission)
            admitted_recovery = bool(
                admission is not None
                and admission.context_id == context_id
                and admission.task_id == task_id
                and admission.owner == owner
            )
            if recoverable_input_admission is not None and not admitted_recovery:
                raise ExecutionControlConflictError("Recoverable input continuation admission is stale")
            replace_blocked_input_wait = bool(
                control is not None
                and admitted_recovery
                and control.can_replace_with_recoverable_input_continuation(task_id)
            )
            if control is not None and (
                (
                    control.task_id != task_id
                    and control.phase in {"pausing", "pause_committing", "paused", "resuming", "terminating"}
                )
                or (
                    control.phase in {"terminating", "terminated"}
                    and not control.release_ready
                    and not replace_blocked_input_wait
                )
            ):
                raise ExecutionControlConflictError("Current execution must finish recovery before a new task starts")
            reuse = bool(
                control is not None
                and control.task_id == task_id
                and control.phase not in {"terminating", "terminated"}
                and not control.input_handoff_ready()
                and (
                    control.stream_available
                    or (continue_input_required and control.execution_status == "input-required")
                    or control.phase != "running"
                )
            )
            if (
                not reuse
                and control is not None
                and control.phase == "running"
                and not control.stream_available
                and control.has_managed_work()
            ):
                await control.rollover_normal_execution(task_id=task_id, cwd=cwd)
                reuse = True
            if not reuse and admission is None:
                shared_start_allowed = await run_sync_fenced(
                    self._recoverable_input_admissions.can_begin_without_admission,
                    context_id,
                    control.execution_id if control is not None else None,
                    self.server_instance_id,
                    control.input_handoff_ready() if control is not None else False,
                )
                if not shared_start_allowed:
                    raise ExecutionControlConflictError("Current execution is active in another process")
            if not reuse:
                retired_control = control
                path = None
                if self._persistence_root is not None:
                    path = self._persistence_root / "execution-control" / f"{context_id}.json"
                control = ExecutionController(
                    context_id=context_id,
                    task_id=task_id,
                    owner=owner,
                    cwd=cwd,
                    server_instance_id=self.server_instance_id,
                    persistence_path=path,
                    backup_service=self._backup_service,
                    termination_cleanup=self._termination_cleanup,
                    on_resume=self._on_resume,
                    input_handoff_commit=self._commit_input_handoff,
                )
            assert control is not None
            if admission is not None and not reuse:
                control.enable_durable_input_handoff()
                control.revision += 1
                activation: _RecoverableInputActivation | None = None

                async def cleanup_cancelled_activation(
                    result: _RecoverableInputActivation | None,
                    error: BaseException | None,
                ) -> None:
                    if result is not None and error is None:
                        await run_sync_fenced(
                            self._recoverable_input_admissions.rollback_activation,
                            admission,
                            result,
                        )
                    control.disable_durable_input_handoff()
                    await control.detach_task(task, execution_status="input-required")
                    await control.close()

                await control.attach_task(task, mark_working=False)
                try:
                    activation = await run_sync_fenced_with_cancel_completion(
                        self._recoverable_input_admissions.activate,
                        cleanup_cancelled_activation,
                        admission,
                        control.snapshot(),
                    )
                    if activation is None:
                        raise ExecutionControlConflictError("Recoverable input continuation admission is stale")
                    control.persisted_revision = control.revision
                    self._controls[context_id] = control
                    await run_sync_fenced(self._recoverable_input_admissions.finish_activation, admission)
                except asyncio.CancelledError:
                    if activation is not None:
                        await run_sync_fenced(
                            self._recoverable_input_admissions.rollback_activation,
                            admission,
                            activation,
                        )
                        if retired_control is None:
                            self._controls.pop(context_id, None)
                        else:
                            self._controls[context_id] = retired_control
                        control.disable_durable_input_handoff()
                        await control.detach_task(task, execution_status="input-required")
                        await control.close()
                    raise
                except BaseException:
                    if activation is not None:
                        await run_sync_fenced(
                            self._recoverable_input_admissions.rollback_activation,
                            admission,
                            activation,
                        )
                        if retired_control is None:
                            self._controls.pop(context_id, None)
                        else:
                            self._controls[context_id] = retired_control
                    control.disable_durable_input_handoff()
                    await control.detach_task(task, execution_status="input-required")
                    await control.close()
                    raise
            self._controls[context_id] = control
            if retired_control is not None:
                await retired_control.close()
            if admission is None or reuse:
                await control.attach_task(task, mark_working=False)
        return control

    async def reserve_recoverable_input_continuation(
        self,
        *,
        context_id: str,
        task_id: str,
        owner: str,
    ) -> str | None:
        """Reserve one request-scoped continuation after the caller proves the sidecar wait."""
        async with self._context_start_locks.setdefault(context_id, asyncio.Lock()):
            control = self._controls.get(context_id)
            if control is not None and (
                control.owner != owner or not control.can_admit_recoverable_input_continuation(task_id)
            ):
                return None
            if control is not None and control.input_handoff_ready():
                # A previous detach may have completed locally while its durable
                # handoff write failed. Re-publish the same revision before a
                # request can reserve and replace this controller.
                await control._persist_snapshot(control.snapshot())
            token = "recovery-" + uuid.uuid4().hex
            admission = _RecoverableInputAdmission(
                token=token,
                context_id=context_id,
                task_id=task_id,
                owner=owner,
                expires_at=time.time() + _RECOVERABLE_INPUT_ADMISSION_TTL_SECONDS,
            )
            reserved = await run_sync_fenced(
                self._recoverable_input_admissions.reserve,
                admission,
            )
            return token if reserved else None

    async def release_recoverable_input_continuation(self, token: str) -> None:
        """Release an unused recovery reservation; consumed reservations are a no-op."""
        admission = self._recoverable_input_admissions.get(token)
        if admission is None:
            return
        async with self._context_start_locks.setdefault(admission.context_id, asyncio.Lock()):
            await run_sync_fenced(self._recoverable_input_admissions.release, token)

    def get_for_context(self, context_id: str) -> ExecutionController | None:
        return self._controls.get(context_id)

    async def require(self, *, context_id: str, owner: str) -> ExecutionController:
        control = self._controls.get(context_id)
        if control is None or control.owner != owner:
            raise ExecutionControlNotFoundError("Execution was not found")
        return control

    def snapshot_for_context(self, context_id: str) -> dict[str, Any] | None:
        control = self._controls.get(context_id)
        return control.snapshot() if control is not None else None

    def has_active_work(self) -> bool:
        active_phases = {"pausing", "pause_committing", "paused", "resuming", "terminating"}
        return any(
            control.has_managed_work()
            or control.phase in active_phases
            or (control.phase == "terminated" and not control.release_ready)
            for control in self._controls.values()
        )

    async def close(self) -> None:
        await asyncio.gather(*(control.close() for control in tuple(self._controls.values())), return_exceptions=True)
        await run_sync_fenced(self._recoverable_input_admissions.close)


def bind_execution_control(control: ExecutionController | None) -> Token[ExecutionController | None]:
    return _CURRENT_CONTROL.set(control)


def reset_execution_control(token: Token[ExecutionController | None]) -> None:
    _CURRENT_CONTROL.reset(token)


def clear_execution_participants() -> Token[tuple[str, ...]]:
    return _CURRENT_PARTICIPANT_IDS.set(())


def reset_execution_participants(token: Token[tuple[str, ...]]) -> None:
    _CURRENT_PARTICIPANT_IDS.reset(token)


def current_execution_control() -> ExecutionController | None:
    return _CURRENT_CONTROL.get()


def current_execution_termination_reason() -> str | None:
    control = current_execution_control()
    if control is None or control.phase not in {"terminating", "terminated"}:
        return None
    return control.termination_reason


def register_execution_task(task: asyncio.Task[Any], *, kind: str) -> None:
    control = current_execution_control()
    if control is not None:
        control.register_spawned_task(task, kind=kind)


async def execution_checkpoint() -> None:
    control = current_execution_control()
    if control is not None:
        await control.checkpoint()


async def record_execution_external_operation(
    *,
    product: str,
    action: str,
    outcome: Literal["accepted", "unknown"],
    resource_type: str | None = None,
    resource_id: str | None = None,
    region_id: str | None = None,
    tool_use_id: str | None = None,
) -> None:
    control = current_execution_control()
    if control is not None:
        await control.record_external_operation(
            product=product,
            action=action,
            outcome=outcome,
            resource_type=resource_type,
            resource_id=resource_id,
            region_id=region_id,
            tool_use_id=tool_use_id,
        )


@asynccontextmanager
async def execution_activity(
    kind: str,
    *,
    check_gate: bool = True,
    handoff_to_parent: bool = False,
) -> AsyncIterator[ActivityHandle | None]:
    control = current_execution_control()
    if control is None:
        yield None
        return
    handle = await control.begin_activity(
        kind,
        check_gate=check_gate,
        handoff_to_parent=handoff_to_parent,
    )
    stack_token = _CURRENT_ACTIVITY_IDS.set((*_CURRENT_ACTIVITY_IDS.get(), handle.activity_id))
    try:
        yield handle
    finally:
        try:
            _CURRENT_ACTIVITY_IDS.reset(stack_token)
        except ValueError:
            # Async-generator finalization may run in a different Context. Its
            # token cannot reset that Context, but the activity must still end.
            pass
        finally:
            await control.end_activity(handle.activity_id)


@asynccontextmanager
async def execution_non_advancing_wait() -> AsyncIterator[None]:
    control = current_execution_control()
    if control is None:
        yield
        return
    waiter_id = await control.begin_non_advancing_wait()
    try:
        yield
    finally:
        await control.end_non_advancing_wait(waiter_id)


async def run_with_execution_budget(awaitable: Awaitable[_T], timeout: float | None) -> _T:
    """Wait for one existing Task while excluding descendant connection holds."""

    if timeout is None:
        return await awaitable
    control = current_execution_control()
    activity_ids = _CURRENT_ACTIVITY_IDS.get()
    if control is None or not activity_ids:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    activity_id = activity_ids[-1]
    activity = control._activities.get(activity_id)
    if activity is None:
        return await asyncio.wait_for(awaitable, timeout=timeout)

    task = asyncio.ensure_future(awaitable)
    handle = ActivityHandle(control, activity_id)
    remaining = float(timeout)
    loop = asyncio.get_running_loop()
    changed: asyncio.Task[bool] | None = None

    async def cancel_and_drain_tool() -> None:
        if not task.done():
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            with suppress(BaseException):
                task.result()

    try:
        while True:
            if task.done():
                return await task
            if handle.blocked:
                activity.budget_changed.clear()
                if not handle.blocked:
                    continue
                changed = asyncio.create_task(activity.budget_changed.wait())
                async with execution_non_advancing_wait():
                    done, _ = await asyncio.wait({task, changed}, return_when=asyncio.FIRST_COMPLETED)
                if changed not in done:
                    changed.cancel()
                    with suppress(asyncio.CancelledError):
                        await changed
                continue
            started = loop.time()
            activity.budget_changed.clear()
            if handle.blocked:
                continue
            changed = asyncio.create_task(activity.budget_changed.wait())
            done, _ = await asyncio.wait({task, changed}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            elapsed = loop.time() - started
            remaining -= elapsed
            if task in done:
                changed.cancel()
                with suppress(asyncio.CancelledError):
                    await changed
                return await task
            if changed in done:
                continue
                changed.cancel()
                with suppress(asyncio.CancelledError):
                    await changed
            await cancel_and_drain_tool()
            raise asyncio.TimeoutError
    except BaseException:
        await cancel_and_drain_tool()
        raise
    finally:
        if changed is not None and not changed.done():
            changed.cancel()
            with suppress(asyncio.CancelledError):
                await changed
