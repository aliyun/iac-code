"""Versioned permission handoff proofs and a file-backed ownership CAS.

The file fence requires a filesystem that implements shared advisory locks.
A staged backup ACK alone never authorizes a cold execution handoff.
"""

import json
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock


class HandoffCommitResult(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    FENCED_OUT = "FENCED_OUT"


class HandoffBackupReceipt(BaseModel):
    """Identity of the exact safe snapshot committed to the shared backup."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, strict=True, extra="forbid")

    generation: int = Field(gt=0)
    commit_id: str = Field(alias="commitId", min_length=1)
    shared_committed: bool = Field(alias="sharedCommitted")
    staged_committed: bool = Field(alias="stagedCommitted")

    @field_validator("commit_id")
    @classmethod
    def validate_commit_id(cls, value: str) -> str:
        return str(UUID(value))

    def as_document(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class RecoverableInputHandoffProof(BaseModel):
    """A PREPARING/COMMITTED transition bound to one execution and boundary."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, strict=True, extra="forbid")

    version: Literal[1] = 1
    context_id: str = Field(alias="contextId", min_length=1)
    task_id: str = Field(alias="taskId", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)
    boundary_id: str = Field(alias="boundaryId", min_length=1)
    boundary_generation: int = Field(alias="boundaryGeneration", gt=0)
    execution_id: str = Field(alias="executionId", min_length=1)
    control_revision: int = Field(alias="controlRevision", ge=0)
    backup_commit_id: str = Field(alias="backupCommitId", min_length=1)
    phase: Literal["PREPARING", "COMMITTED"]
    backup_receipt: HandoffBackupReceipt | None = Field(default=None, alias="backupReceipt")
    boundaries: dict[str, int] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_boundaries(self) -> "RecoverableInputHandoffProof":
        if self.boundaries.get(self.boundary_id) != self.boundary_generation or any(
            not key or type(generation) is not int or generation < 1 for key, generation in self.boundaries.items()
        ):
            raise ValueError("invalid permission handoff boundary group")
        return self

    def matches_boundary(self, boundary_id: str, generation: int) -> bool:
        return self.boundaries.get(boundary_id) == generation

    @field_validator("backup_commit_id")
    @classmethod
    def validate_backup_commit_id(cls, value: str) -> str:
        return str(UUID(value))

    def as_document(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    @classmethod
    def from_document(cls, document: Any) -> "RecoverableInputHandoffProof":
        return cls.model_validate(document)

    @classmethod
    def from_checkpoint(cls, record: dict[str, Any]) -> "RecoverableInputHandoffProof":
        proof = cls.from_document(record.get("executionHandoff"))
        if (
            any(record.get(key) != proof.as_document()[key] for key in ("contextId", "taskId", "sessionId"))
            or type(record.get("generation")) is not int
            or not any(
                boundary_id == record.get("boundaryId") and generation <= record["generation"]
                for boundary_id, generation in proof.boundaries.items()
            )
        ):
            raise ValueError("permission recovery checkpoint does not match its handoff")
        return proof

    @classmethod
    def validates_committed(cls, document: dict[str, Any], context_id: str, task_id: str | None = None) -> bool:
        """Reject partial, unbacked, active, and unrelated committed records."""

        try:
            proof = cls.from_document(document.get("inputHandoff"))
        except (ValidationError, ValueError, TypeError):
            return False
        receipt = proof.backup_receipt
        return bool(
            proof.phase == "COMMITTED"
            and receipt is not None
            and receipt.shared_committed
            and receipt.commit_id == proof.backup_commit_id
            and proof.context_id == context_id == document.get("contextId")
            and proof.task_id == document.get("taskId")
            and (task_id is None or proof.task_id == task_id)
            and proof.execution_id == document.get("executionId")
            and proof.session_id == document.get("sessionId")
            and document.get("inputHandoffReady") is True
            and document.get("phase") == "running"
            and document.get("executionStatus") == "input-required"
            and document.get("streamAvailable") is False
            and document.get("blockers") == []
            and type(document.get("subprocessToolTrackingVersion")) is int
            and document["subprocessToolTrackingVersion"] == 1
            and type(document.get("activeSubprocessTools")) is int
            and document["activeSubprocessTools"] == 0
            and type(document.get("revision")) is int
            and type(document.get("persistedRevision")) is int
            and document["revision"] == document["persistedRevision"] == proof.control_revision + 2
        )


class InputHandoffStore:
    """Publish exact ownership transitions under the existing admission lock."""

    @staticmethod
    def write_snapshot(
        control_path: Path,
        snapshot: dict[str, Any],
        expected_execution_id: str,
        expected_revision: int,
        expected_proof: RecoverableInputHandoffProof | dict[str, Any] | None = None,
    ) -> HandoffCommitResult:
        target = dict(snapshot)
        revision = target.get("revision")
        if (
            type(expected_revision) is not int
            or type(revision) is not int
            or revision != expected_revision + 1
            or target.get("executionId") != expected_execution_id
        ):
            return HandoffCommitResult.FENCED_OUT
        target["persistedRevision"] = revision
        proof_document = (
            expected_proof.as_document() if isinstance(expected_proof, RecoverableInputHandoffProof) else expected_proof
        )
        lock_path = control_path.with_name(f".{control_path.stem}.recoverable-input.lock")
        with cross_process_file_lock(lock_path):
            try:
                current = json.loads(control_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return HandoffCommitResult.FENCED_OUT
            if not isinstance(current, dict):
                return HandoffCommitResult.FENCED_OUT
            if current == target and isinstance(target.get("inputHandoff"), dict):
                return HandoffCommitResult.ALREADY_APPLIED
            if (
                current.get("executionId") != expected_execution_id
                or type(current.get("revision")) is not int
                or current["revision"] != expected_revision
                or type(current.get("persistedRevision")) is not int
                or current["persistedRevision"] != expected_revision
                or (proof_document is not None and current.get("inputHandoff") != proof_document)
            ):
                return HandoffCommitResult.FENCED_OUT
            atomic_write_json(control_path, target, durable=True)
            return HandoffCommitResult.APPLIED
