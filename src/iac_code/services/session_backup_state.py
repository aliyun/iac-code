"""Versioned state for fencing incremental session backups."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

BACKUP_STATE_SCHEMA_VERSION = 2
NORMAL_HANDOFF_PROOF_KEY = "normal_handoff"
_ALLOWED_PROOF_KEYS = frozenset({NORMAL_HANDOFF_PROOF_KEY})
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "generation",
        "parent_generation",
        "commit_id",
        "status",
        "reason",
        "updated_at",
        "writer_id",
        "publication_proofs",
        "attempt_commit_id",
        "attempt_publication_proofs",
        "error",
        "attempt",
        "retry_count",
        "exhausted",
    }
)


class SessionBackupStateError(ValueError):
    """Raised when backup state is missing required protocol invariants."""


@dataclass(frozen=True)
class BackupPublicationProof:
    event_id: str
    event_type: str
    sequence: int

    def __post_init__(self) -> None:
        _require_nonempty_string(self.event_id, "event_id")
        _require_nonempty_string(self.event_type, "event_type")
        _require_nonnegative_int(self.sequence, "sequence")

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, Any]) -> BackupPublicationProof:
        if not isinstance(envelope, Mapping):
            raise SessionBackupStateError("publication envelope must be an object")
        return cls(
            event_id=_require_nonempty_string(envelope.get("eventId"), "eventId"),
            event_type=_require_nonempty_string(envelope.get("eventType"), "eventType"),
            sequence=_require_nonnegative_int(envelope.get("sequence"), "sequence"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BackupPublicationProof:
        if not isinstance(payload, Mapping) or set(payload) != {"event_id", "event_type", "sequence"}:
            raise SessionBackupStateError("publication proof has invalid fields")
        return cls(
            event_id=_require_nonempty_string(payload.get("event_id"), "event_id"),
            event_type=_require_nonempty_string(payload.get("event_type"), "event_type"),
            sequence=_require_nonnegative_int(payload.get("sequence"), "sequence"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "event_type": self.event_type, "sequence": self.sequence}


@dataclass(frozen=True)
class SessionBackupState:
    session_id: str
    generation: int
    parent_generation: int | None
    commit_id: str | None
    status: Literal["succeeded", "failed"]
    reason: str
    updated_at: str
    writer_id: str
    publication_proofs: Mapping[str, BackupPublicationProof]
    attempt_commit_id: str | None = None
    attempt_publication_proofs: Mapping[str, BackupPublicationProof] = field(default_factory=dict)
    error: str | None = None
    attempt: int | None = None
    retry_count: int | None = None
    exhausted: bool | None = None

    def __post_init__(self) -> None:
        _require_nonempty_string(self.session_id, "session_id")
        _require_nonnegative_int(self.generation, "generation")
        _require_nonempty_string(self.reason, "reason")
        _require_nonempty_string(self.updated_at, "updated_at")
        _require_nonempty_string(self.writer_id, "writer_id")
        if self.status not in {"succeeded", "failed"}:
            raise SessionBackupStateError("status must be succeeded or failed")
        if self.generation == 0:
            if self.parent_generation is not None or self.commit_id is not None:
                raise SessionBackupStateError("generation 0 cannot have a parent or commit")
        else:
            if self.parent_generation != self.generation - 1:
                raise SessionBackupStateError("parent_generation must precede generation")
            _require_nonempty_string(self.commit_id, "commit_id")
        _validate_proofs(self.publication_proofs, "publication_proofs")
        _validate_proofs(self.attempt_publication_proofs, "attempt_publication_proofs")
        if self.status == "failed":
            _require_nonempty_string(self.attempt_commit_id, "attempt_commit_id")
            _require_nonempty_string(self.error, "error")
            _require_positive_int(self.attempt, "attempt")
            _require_nonnegative_int(self.retry_count, "retry_count")
            if not isinstance(self.exhausted, bool):
                raise SessionBackupStateError("exhausted must be a boolean")
        elif (
            any(
                value is not None
                for value in (self.attempt_commit_id, self.error, self.attempt, self.retry_count, self.exhausted)
            )
            or self.attempt_publication_proofs
        ):
            raise SessionBackupStateError("succeeded state cannot contain failed-attempt fields")

    @classmethod
    def bootstrap(cls, session_id: str, *, writer_id: str) -> SessionBackupState:
        return cls(
            session_id=session_id,
            generation=0,
            parent_generation=None,
            commit_id=None,
            status="succeeded",
            reason="initialized",
            updated_at=_utc_now(),
            writer_id=writer_id,
            publication_proofs={},
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, shared: bool = False) -> SessionBackupState:
        if not isinstance(payload, Mapping):
            raise SessionBackupStateError("backup state must be an object")
        unknown = set(payload) - _STATE_KEYS
        if unknown:
            raise SessionBackupStateError("backup state has unknown fields: {}".format(", ".join(sorted(unknown))))
        if payload.get("schema_version") != BACKUP_STATE_SCHEMA_VERSION or isinstance(
            payload.get("schema_version"), bool
        ):
            raise SessionBackupStateError("unsupported backup state schema version")
        status = payload.get("status")
        if status not in {"succeeded", "failed"}:
            raise SessionBackupStateError("status must be succeeded or failed")
        state = cls(
            session_id=_require_nonempty_string(payload.get("session_id"), "session_id"),
            generation=_require_nonnegative_int(payload.get("generation"), "generation"),
            parent_generation=_optional_nonnegative_int(payload.get("parent_generation"), "parent_generation"),
            commit_id=_optional_nonempty_string(payload.get("commit_id"), "commit_id"),
            status=status,
            reason=_require_nonempty_string(payload.get("reason"), "reason"),
            updated_at=_require_nonempty_string(payload.get("updated_at"), "updated_at"),
            writer_id=_require_nonempty_string(payload.get("writer_id"), "writer_id"),
            publication_proofs=_parse_proofs(payload.get("publication_proofs"), "publication_proofs"),
            attempt_commit_id=_optional_nonempty_string(payload.get("attempt_commit_id"), "attempt_commit_id"),
            attempt_publication_proofs=_parse_proofs(
                payload.get("attempt_publication_proofs", {}), "attempt_publication_proofs"
            ),
            error=_optional_nonempty_string(payload.get("error"), "error"),
            attempt=_optional_positive_int(payload.get("attempt"), "attempt"),
            retry_count=_optional_nonnegative_int(payload.get("retry_count"), "retry_count"),
            exhausted=payload.get("exhausted"),
        )
        if shared and state.status != "succeeded":
            raise SessionBackupStateError("shared backup state must be succeeded")
        return state

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": BACKUP_STATE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "generation": self.generation,
            "parent_generation": self.parent_generation,
            "commit_id": self.commit_id,
            "status": self.status,
            "reason": self.reason,
            "updated_at": self.updated_at,
            "writer_id": self.writer_id,
            "publication_proofs": _proofs_to_dict(self.publication_proofs),
        }
        if self.status == "failed":
            payload.update(
                {
                    "attempt_commit_id": self.attempt_commit_id,
                    "attempt_publication_proofs": _proofs_to_dict(self.attempt_publication_proofs),
                    "error": self.error,
                    "attempt": self.attempt,
                    "retry_count": self.retry_count,
                    "exhausted": self.exhausted,
                }
            )
        return payload

    def failed_attempt(
        self,
        *,
        reason: str,
        writer_id: str,
        attempt_commit_id: str,
        attempted_proofs: Mapping[str, BackupPublicationProof],
        error: str,
        attempt: int,
        retry_count: int,
        exhausted: bool,
    ) -> SessionBackupState:
        return SessionBackupState(
            session_id=self.session_id,
            generation=self.generation,
            parent_generation=self.parent_generation,
            commit_id=self.commit_id,
            status="failed",
            reason=reason,
            updated_at=_utc_now(),
            writer_id=writer_id,
            publication_proofs=dict(self.publication_proofs),
            attempt_commit_id=attempt_commit_id,
            attempt_publication_proofs=dict(attempted_proofs),
            error=error,
            attempt=attempt,
            retry_count=retry_count,
            exhausted=exhausted,
        )

    def committed_next(
        self,
        *,
        commit_id: str,
        reason: str,
        writer_id: str,
        proofs: Mapping[str, BackupPublicationProof],
    ) -> SessionBackupState:
        return SessionBackupState(
            session_id=self.session_id,
            generation=self.generation + 1,
            parent_generation=self.generation,
            commit_id=commit_id,
            status="succeeded",
            reason=reason,
            updated_at=_utc_now(),
            writer_id=writer_id,
            publication_proofs=dict(proofs),
        )

    def same_lineage(self, other: SessionBackupState) -> bool:
        return (
            self.session_id == other.session_id
            and self.generation == other.generation
            and self.parent_generation == other.parent_generation
            and self.commit_id == other.commit_id
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionBackupStateError("{} must be a non-empty string".format(field_name))
    return value


def _optional_nonempty_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_nonempty_string(value, field_name)


def _require_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SessionBackupStateError("{} must be a non-negative integer".format(field_name))
    return value


def _optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_nonnegative_int(value, field_name)


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SessionBackupStateError("{} must be a positive integer".format(field_name))
    return value


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_positive_int(value, field_name)


def _parse_proofs(value: Any, field_name: str) -> dict[str, BackupPublicationProof]:
    if not isinstance(value, Mapping):
        raise SessionBackupStateError("{} must be an object".format(field_name))
    proofs: dict[str, BackupPublicationProof] = {}
    for key, proof_payload in value.items():
        if not isinstance(key, str) or key not in _ALLOWED_PROOF_KEYS:
            raise SessionBackupStateError("{} contains an invalid proof key".format(field_name))
        proofs[key] = BackupPublicationProof.from_dict(proof_payload)
    return proofs


def _validate_proofs(proofs: Mapping[str, BackupPublicationProof], field_name: str) -> None:
    if not isinstance(proofs, Mapping):
        raise SessionBackupStateError("{} must be an object".format(field_name))
    for key, proof in proofs.items():
        if key not in _ALLOWED_PROOF_KEYS or not isinstance(proof, BackupPublicationProof):
            raise SessionBackupStateError("{} contains an invalid proof".format(field_name))
        if key == NORMAL_HANDOFF_PROOF_KEY and proof.event_type != "pipeline_handoff_ready":
            raise SessionBackupStateError("normal handoff proof has an invalid event type")


def _proofs_to_dict(proofs: Mapping[str, BackupPublicationProof]) -> dict[str, dict[str, Any]]:
    return {key: proof.to_dict() for key, proof in proofs.items()}
