from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from iac_code.a2a.types import validate_protocol_id
from iac_code.services.session_mutation_guard import session_mutation_guard
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock

_SCHEMA_VERSION = 1
_INTENT_FILENAME = "continuation-intent.json"
_LOCK_FILENAME = ".continuation-intent.lock"


class PipelineContinuationError(RuntimeError):
    pass


class PipelineContinuationCorruptError(PipelineContinuationError):
    pass


class PipelineContinuationConflictError(PipelineContinuationError):
    pass


class PipelineContinuationUnsafeError(PipelineContinuationError):
    code = "CONTINUATION_UNSAFE"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


@dataclass(frozen=True)
class PipelineCheckpointIdentity:
    version: int
    digest: str
    sequence: int
    event_id: str


@dataclass(frozen=True)
class PipelineContinuationIntent:
    context_id: str
    predecessor_task_id: str
    successor_task_id: str
    invocation_id: str
    checkpoint: PipelineCheckpointIdentity
    cancellation_execution_id: str
    cancellation_revision: int
    cancellation_backup_generation: int | None
    cancellation_backup_commit_id: str | None
    fence: int
    phase: str
    claim_id: str | None = None
    unsafe_reason: str | None = None


class PipelineContinuationStore:
    """Durable successor allocation and one-shot execution claim.

    Corrupt state is fail-closed. A claimed intent is never automatically
    reclaimed because the claimant may have completed an external write whose
    result is unknown.
    """

    def __init__(self, pipeline_dir: str | Path, *, session_dir: str | Path) -> None:
        self._pipeline_dir = Path(pipeline_dir)
        self._session_dir = Path(session_dir)
        if self._pipeline_dir != self._session_dir / "a2a" / "pipeline":
            raise ValueError("Continuation store requires the V2 session pipeline path")
        self._path = self._pipeline_dir / _INTENT_FILENAME
        self._lock_path = self._pipeline_dir / _LOCK_FILENAME

    def load(self, *, context_id: str | None = None) -> PipelineContinuationIntent | None:
        if context_id is not None:
            context_id = validate_protocol_id(context_id)
        ensure_private_dir(self._pipeline_dir)
        with session_mutation_guard(self._session_dir):
            with cross_process_file_lock(self._lock_path):
                intent = self._load_unlocked()
        if intent is None or (context_id is not None and intent.context_id != context_id):
            return None
        return intent

    def reserve_successor(
        self,
        *,
        context_id: str,
        predecessor_task_id: str,
        invocation_id: str,
        checkpoint: PipelineCheckpointIdentity,
        cancellation_execution_id: str,
        cancellation_revision: int,
        cancellation_backup_generation: int | None,
        cancellation_backup_commit_id: str | None,
    ) -> PipelineContinuationIntent:
        context_id = validate_protocol_id(context_id)
        predecessor_task_id = validate_protocol_id(predecessor_task_id)
        invocation_id = validate_protocol_id(invocation_id)
        cancellation_execution_id = validate_protocol_id(cancellation_execution_id)
        if cancellation_revision < 0:
            raise ValueError("Invalid cancellation revision")
        ensure_private_dir(self._pipeline_dir)
        with session_mutation_guard(self._session_dir):
            with cross_process_file_lock(self._lock_path):
                current = self._load_unlocked()
                if current is not None and current.context_id == context_id:
                    if current.phase == "settled":
                        pass
                    elif current.predecessor_task_id != predecessor_task_id:
                        raise PipelineContinuationConflictError("A different predecessor owns the continuation")
                    elif current.checkpoint != checkpoint:
                        raise PipelineContinuationConflictError("The canceled checkpoint changed")
                    elif current.invocation_id != invocation_id:
                        raise PipelineContinuationConflictError("A different invocation owns the continuation")
                    elif (
                        current.cancellation_execution_id != cancellation_execution_id
                        or current.cancellation_revision != cancellation_revision
                        or current.cancellation_backup_generation != cancellation_backup_generation
                        or current.cancellation_backup_commit_id != cancellation_backup_commit_id
                    ):
                        raise PipelineContinuationConflictError("The cancellation proof changed")
                    else:
                        return current
                intent = PipelineContinuationIntent(
                    context_id=context_id,
                    predecessor_task_id=predecessor_task_id,
                    successor_task_id="task-" + uuid.uuid4().hex[:12],
                    invocation_id=invocation_id,
                    checkpoint=checkpoint,
                    cancellation_execution_id=cancellation_execution_id,
                    cancellation_revision=cancellation_revision,
                    cancellation_backup_generation=cancellation_backup_generation,
                    cancellation_backup_commit_id=cancellation_backup_commit_id,
                    fence=(current.fence + 1 if current is not None else 1),
                    phase="reserved",
                )
                self._write_unlocked(intent)
                return intent

    def claim_execution(
        self,
        *,
        successor_task_id: str,
        invocation_id: str,
        checkpoint: PipelineCheckpointIdentity,
        expected_fence: int,
    ) -> PipelineContinuationIntent:
        successor_task_id = validate_protocol_id(successor_task_id)
        invocation_id = validate_protocol_id(invocation_id)
        ensure_private_dir(self._pipeline_dir)
        with session_mutation_guard(self._session_dir):
            with cross_process_file_lock(self._lock_path):
                current = self._load_unlocked()
                if current is None or current.successor_task_id != successor_task_id:
                    raise PipelineContinuationConflictError("Successor intent is unavailable")
                if current.checkpoint != checkpoint:
                    raise PipelineContinuationConflictError("Successor checkpoint fence changed")
                if current.fence != expected_fence:
                    raise PipelineContinuationConflictError("Successor ownership fence changed")
                if current.invocation_id != invocation_id:
                    raise PipelineContinuationConflictError("A different invocation owns the continuation")
                if current.phase != "reserved":
                    raise PipelineContinuationConflictError("Successor execution is already claimed")
                claimed = replace(current, phase="claimed", claim_id=uuid.uuid4().hex)
                self._write_unlocked(claimed)
                return claimed

    def begin_execution(
        self,
        *,
        successor_task_id: str,
        claim_id: str,
        expected_fence: int,
    ) -> PipelineContinuationIntent:
        """Durably close the no-side-effect claim window before stream iteration."""

        successor_task_id = validate_protocol_id(successor_task_id)
        claim_id = validate_protocol_id(claim_id)
        with session_mutation_guard(self._session_dir):
            with cross_process_file_lock(self._lock_path):
                current = self._load_unlocked()
                if (
                    current is None
                    or current.successor_task_id != successor_task_id
                    or current.fence != expected_fence
                    or current.claim_id != claim_id
                    or current.phase not in {"claimed", "waiting_input"}
                ):
                    raise PipelineContinuationConflictError("Successor execution cannot start")
                running = replace(current, phase="running")
                self._write_unlocked(running)
                return running

    def pause_for_input(self, *, successor_task_id: str, expected_fence: int) -> PipelineContinuationIntent:
        successor_task_id = validate_protocol_id(successor_task_id)
        with session_mutation_guard(self._session_dir):
            with cross_process_file_lock(self._lock_path):
                current = self._load_unlocked()
                if current is None or current.successor_task_id != successor_task_id or current.fence != expected_fence:
                    raise PipelineContinuationConflictError("Successor ownership fence changed")
                if current.phase == "waiting_input":
                    return current
                if current.phase != "running":
                    raise PipelineContinuationConflictError("Successor execution is not running")
                waiting = replace(current, phase="waiting_input")
                self._write_unlocked(waiting)
                return waiting

    def settle(self, *, successor_task_id: str, expected_fence: int) -> PipelineContinuationIntent:
        successor_task_id = validate_protocol_id(successor_task_id)
        with session_mutation_guard(self._session_dir):
            with cross_process_file_lock(self._lock_path):
                current = self._load_unlocked()
                if current is None or current.successor_task_id != successor_task_id or current.fence != expected_fence:
                    raise PipelineContinuationConflictError("Successor ownership fence changed")
                if current.phase == "settled":
                    return current
                if current.phase != "running":
                    raise PipelineContinuationConflictError("Successor execution is not running")
                settled = replace(current, phase="settled")
                self._write_unlocked(settled)
                return settled

    def mark_unsafe(
        self,
        *,
        successor_task_id: str,
        checkpoint: PipelineCheckpointIdentity,
        expected_fence: int,
        reason: str,
    ) -> PipelineContinuationIntent:
        successor_task_id = validate_protocol_id(successor_task_id)
        if not reason:
            raise ValueError("Unsafe continuation reason is required")
        with session_mutation_guard(self._session_dir):
            with cross_process_file_lock(self._lock_path):
                current = self._load_unlocked()
                if (
                    current is None
                    or current.successor_task_id != successor_task_id
                    or current.checkpoint != checkpoint
                    or current.fence != expected_fence
                ):
                    raise PipelineContinuationConflictError("Successor ownership fence changed")
                if current.phase == "unsafe":
                    return current
                if current.phase not in {"claimed", "running"}:
                    raise PipelineContinuationConflictError("Successor execution is not claimed")
                unsafe = replace(current, phase="unsafe", unsafe_reason=reason)
                self._write_unlocked(unsafe)
                return unsafe

    def _write_unlocked(self, intent: PipelineContinuationIntent) -> None:
        atomic_write_json(
            self._path,
            {
                "schemaVersion": _SCHEMA_VERSION,
                "contextId": intent.context_id,
                "predecessorTaskId": intent.predecessor_task_id,
                "successorTaskId": intent.successor_task_id,
                "invocationId": intent.invocation_id,
                "checkpoint": {
                    "version": intent.checkpoint.version,
                    "digest": intent.checkpoint.digest,
                    "sequence": intent.checkpoint.sequence,
                    "eventId": intent.checkpoint.event_id,
                },
                "cancellationExecutionId": intent.cancellation_execution_id,
                "cancellationRevision": intent.cancellation_revision,
                "cancellationBackupGeneration": intent.cancellation_backup_generation,
                "cancellationBackupCommitId": intent.cancellation_backup_commit_id,
                "fence": intent.fence,
                "phase": intent.phase,
                "claimId": intent.claim_id,
                "unsafeReason": intent.unsafe_reason,
            },
            durable=True,
        )

    def _load_unlocked(self) -> PipelineContinuationIntent | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise PipelineContinuationCorruptError("Continuation intent cannot be read safely") from exc
        try:
            if not isinstance(raw, dict) or raw.get("schemaVersion") != _SCHEMA_VERSION:
                raise ValueError("schema")
            checkpoint = raw["checkpoint"]
            intent = PipelineContinuationIntent(
                context_id=validate_protocol_id(raw["contextId"]),
                predecessor_task_id=validate_protocol_id(raw["predecessorTaskId"]),
                successor_task_id=validate_protocol_id(raw["successorTaskId"]),
                invocation_id=validate_protocol_id(raw["invocationId"]),
                checkpoint=PipelineCheckpointIdentity(
                    version=int(checkpoint["version"]),
                    digest=str(checkpoint["digest"]),
                    sequence=int(checkpoint["sequence"]),
                    event_id=str(checkpoint["eventId"]),
                ),
                cancellation_execution_id=validate_protocol_id(raw["cancellationExecutionId"]),
                cancellation_revision=int(raw["cancellationRevision"]),
                cancellation_backup_generation=(
                    int(raw["cancellationBackupGeneration"])
                    if raw.get("cancellationBackupGeneration") is not None
                    else None
                ),
                cancellation_backup_commit_id=(
                    str(raw["cancellationBackupCommitId"])
                    if raw.get("cancellationBackupCommitId") is not None
                    else None
                ),
                fence=int(raw["fence"]),
                phase=str(raw["phase"]),
                claim_id=str(raw["claimId"]) if raw.get("claimId") is not None else None,
                unsafe_reason=str(raw["unsafeReason"]) if raw.get("unsafeReason") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineContinuationCorruptError("Continuation intent is invalid") from exc
        if (
            intent.fence <= 0
            or intent.checkpoint.version != 1
            or intent.checkpoint.sequence < 0
            or len(intent.checkpoint.digest) != 64
            or intent.phase not in {"reserved", "claimed", "running", "waiting_input", "unsafe", "settled"}
            or (intent.phase in {"claimed", "running", "waiting_input", "unsafe", "settled"})
            != bool(intent.claim_id)
            or (intent.phase == "unsafe") != bool(intent.unsafe_reason)
        ):
            raise PipelineContinuationCorruptError("Continuation intent violates its protocol")
        return intent


def checkpoint_identity(snapshot: dict[str, Any], terminal_event: dict[str, Any]) -> PipelineCheckpointIdentity:
    sequence = terminal_event.get("sequence")
    event_id = terminal_event.get("eventId")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise PipelineContinuationError("Canceled checkpoint has no valid sequence")
    if not isinstance(event_id, str) or not event_id:
        raise PipelineContinuationError("Canceled checkpoint has no event id")
    encoded = json.dumps(
        {"snapshot": snapshot, "terminalEvent": terminal_event},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PipelineCheckpointIdentity(
        version=1,
        digest=hashlib.sha256(encoded).hexdigest(),
        sequence=sequence,
        event_id=event_id,
    )


__all__ = [
    "PipelineCheckpointIdentity",
    "PipelineContinuationConflictError",
    "PipelineContinuationCorruptError",
    "PipelineContinuationIntent",
    "PipelineContinuationStore",
    "PipelineContinuationUnsafeError",
    "checkpoint_identity",
]
