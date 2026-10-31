"""Commit a drained permission boundary without releasing its generation fence."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from iac_code.a2a.input_handoff import (
    HandoffBackupReceipt,
    HandoffCommitResult,
    InputHandoffStore,
    RecoverableInputHandoffProof,
)
from iac_code.services.permission_wait import PermissionWaitCheckpointStore


class SuspendedPermissionHandoff:
    """Own one safe suspension's preparation, backup receipt and final CAS.

    The caller holds its controller condition and has drained all business
    work. This synchronous operation holds the permission generation fence
    until the final ownership CAS, using the established session/permission/
    backup lock order. COMMITTED lives in execution-control; the backup only
    contains PREPARING and cannot authorize recovery by itself.
    """

    def __init__(
        self, store: PermissionWaitCheckpointStore, boundary_id: str, *, boundary_ids: tuple[str, ...] | None = None
    ) -> None:
        self.store = store
        self.boundary_id = boundary_id
        self.boundary_ids = boundary_ids or (boundary_id,)
        self.proof: RecoverableInputHandoffProof | None = None
        self.published_revision: int | None = None
        self.result = HandoffCommitResult.FENCED_OUT
        self._preparing_snapshot: dict[str, Any] | None = None
        self._pending_committed_snapshot: dict[str, Any] | None = None
        self.suspension_started = False
        self.runtime_drained = False
        self.runtime_close_confirmation: Callable[[], Awaitable[None]] | None = None
        self.local_only = False

    def commit(self, snapshot: dict[str, Any], control_path: Path, backup_service: Any) -> HandoffCommitResult:
        records = {boundary_id: self.store.load(boundary_id) for boundary_id in self.boundary_ids}
        records = {key: value for key, value in records.items() if value is None or value.get("phase") != "RESOLVED"}
        if not records or any(not self._matches_boundary(record, snapshot) for record in records.values()):
            return HandoffCommitResult.FENCED_OUT
        self.boundary_id = next(iter(records))
        record = records[self.boundary_id]
        assert record is not None
        if not backup_service._backup_enabled():
            # No shared backup can authorize migration. A local service may
            # instead reserve its own drained owner using a local-only ticket.
            self.local_only = True
            return HandoffCommitResult.FENCED_OUT
        generation = int(record["generation"])
        generations = {key: int(value["generation"]) for key, value in records.items() if value is not None}
        proof_changed = self.proof is not None and self.proof.boundaries != generations
        if proof_changed:
            # A failed backup owns its operation id until its mirror settles.
            # Finish that attempt without granting its obsolete generation any
            # recovery authority, then capture the new checkpoint separately.
            settled = self.store.run_generation_fenced(
                self.boundary_id,
                expected_generation=generation,
                operation=partial(
                    backup_service.backup_recoverable_input_handoff,
                    str(self.store.cwd),
                    self.proof.session_id,
                    operation_commit_id=self.proof.backup_commit_id,
                ),
            )
            if not self._shared_backup_matches(settled, self.proof.backup_commit_id):
                return HandoffCommitResult.FENCED_OUT
        # An answer may have advanced the safe checkpoint while a failed
        # preparation remained. Bind a fresh proof to that exact generation.
        if self.proof is None or proof_changed:
            self._preparing_snapshot = None
            self._pending_committed_snapshot = None
            self.proof = RecoverableInputHandoffProof(
                contextId=str(snapshot["contextId"]),
                taskId=str(snapshot["taskId"]),
                sessionId=str(record["sessionId"]),
                boundaryId=self.boundary_id,
                boundaryGeneration=generation,
                executionId=str(snapshot["executionId"]),
                controlRevision=int(snapshot["revision"]),
                backupCommitId=str(uuid.uuid4()),
                phase="PREPARING",
                boundaries=generations,
            )
        # This annotation grants no authority. The final generation fence
        # rejects an answer/cancel that wins before the ownership preparation.
        for boundary_id in generations:
            self.store.transaction(boundary_id, self._save_preparing)
        if len(generations) > 1:
            return self.store.run_generations_fenced(
                generations, partial(self._commit_fenced, snapshot, control_path, backup_service)
            )
        return self.store.run_generation_fenced(
            self.boundary_id,
            expected_generation=generation,
            operation=lambda: self._commit_fenced(snapshot, control_path, backup_service),
        )

    def _commit_fenced(self, snapshot: dict[str, Any], control_path: Path, backup_service: Any) -> HandoffCommitResult:
        proof = self.proof
        assert proof is not None
        if proof.phase == "COMMITTED":
            return self.result
        if self._pending_committed_snapshot is not None:
            return self._publish_committed(control_path, self._pending_committed_snapshot)
        if self._preparing_snapshot is None:
            self._preparing_snapshot = {
                **snapshot,
                "revision": proof.control_revision + 1,
                "inputHandoff": proof.as_document(),
                "inputHandoffReady": False,
                "localInputContinuationReady": False,
                "commitError": None,
            }
        preparing = self._preparing_snapshot
        self.result = InputHandoffStore.write_snapshot(
            control_path, preparing, proof.execution_id, proof.control_revision
        )
        if self.result == HandoffCommitResult.FENCED_OUT:
            return self.result
        self.published_revision = proof.control_revision + 1
        backup = backup_service.backup_recoverable_input_handoff(
            str(self.store.cwd), proof.session_id, operation_commit_id=proof.backup_commit_id
        )
        if not self._shared_backup_matches(backup, proof.backup_commit_id):
            # In particular, a local staged snapshot is not a shared receipt.
            return HandoffCommitResult.FENCED_OUT
        receipt = HandoffBackupReceipt(
            generation=backup.generation,
            commitId=backup.commit_id,
            sharedCommitted=True,
            stagedCommitted=backup.staged_committed,
        )
        committed_proof = proof.model_copy(update={"phase": "COMMITTED", "backup_receipt": receipt})
        committed = {
            **preparing,
            "revision": proof.control_revision + 2,
            "inputHandoff": committed_proof.as_document(),
            "inputHandoffReady": True,
        }
        self._pending_committed_snapshot = committed
        return self._publish_committed(control_path, committed)

    def _publish_committed(self, control_path: Path, committed: dict[str, Any]) -> HandoffCommitResult:
        proof = self.proof
        assert proof is not None
        self.result = InputHandoffStore.write_snapshot(
            control_path, committed, proof.execution_id, proof.control_revision + 1, expected_proof=proof
        )
        if self.result != HandoffCommitResult.FENCED_OUT:
            self.proof = RecoverableInputHandoffProof.from_document(committed["inputHandoff"])
            self.published_revision = proof.control_revision + 2
        return self.result

    @staticmethod
    def _shared_backup_matches(backup: Any, commit_id: str) -> bool:
        return bool(
            backup.enabled
            and backup.succeeded
            and backup.shared_committed
            and backup.commit_id == commit_id
            and type(backup.generation) is int
            and backup.generation > 0
        )

    def _save_preparing(self, record: dict[str, Any]) -> dict[str, Any]:
        assert self.proof is not None
        generation = record.get("generation")
        if (
            type(generation) is not int
            or not self.proof.matches_boundary(str(record.get("boundaryId")), generation)
            or record.get("phase") not in {"SUSPENDED", "CANCELED"}
        ):
            raise ValueError("permission generation changed before handoff")
        record["executionHandoff"] = self.proof.as_document()
        return record

    def _matches_boundary(self, record: dict[str, Any] | None, snapshot: dict[str, Any]) -> bool:
        return bool(
            record is not None
            and record.get("boundaryId") in self.boundary_ids
            and record.get("contextId") == snapshot.get("contextId")
            and record.get("taskId") == snapshot.get("taskId")
            and record.get("sessionId") == snapshot.get("sessionId")
            and record.get("permissionClass") in {"normal", "pipeline"}
            and record.get("phase") in {"SUSPENDED", "CANCELED"}
            and isinstance(record.get("continuationFrame"), dict)
        )
