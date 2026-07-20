from dataclasses import replace

import pytest

from iac_code.services.session_backup_state import (
    BACKUP_STATE_SCHEMA_VERSION,
    NORMAL_HANDOFF_PROOF_KEY,
    BackupPublicationProof,
    SessionBackupState,
    SessionBackupStateError,
)


def test_bootstrap_round_trips_strict_schema() -> None:
    state = SessionBackupState.bootstrap("s1", writer_id="writer-1")

    restored = SessionBackupState.from_dict(state.to_dict())

    assert restored == state
    assert restored.generation == 0
    assert restored.parent_generation is None
    assert restored.commit_id is None
    assert restored.status == "succeeded"
    assert state.to_dict()["schema_version"] == BACKUP_STATE_SCHEMA_VERSION


def test_failed_state_preserves_committed_identity_and_separates_attempted_proof() -> None:
    base = SessionBackupState.bootstrap("s1", writer_id="writer-1").committed_next(
        commit_id="commit-0",
        reason="normal_turn_end",
        writer_id="writer-1",
        proofs={},
    )
    proof = BackupPublicationProof("event-1", "pipeline_handoff_ready", 42)

    failed = base.failed_attempt(
        reason="handoff_ready",
        writer_id="writer-2",
        attempt_commit_id="attempt-1",
        attempted_proofs={NORMAL_HANDOFF_PROOF_KEY: proof},
        error="unavailable",
        attempt=1,
        retry_count=0,
        exhausted=False,
    )

    assert failed.generation == 1
    assert failed.commit_id == "commit-0"
    assert failed.publication_proofs == {}
    assert failed.attempt_publication_proofs[NORMAL_HANDOFF_PROOF_KEY] == proof


def test_committed_next_advances_generation_and_clears_attempt_fields() -> None:
    base = SessionBackupState.bootstrap("s1", writer_id="writer-1")
    proof = BackupPublicationProof("event-1", "pipeline_handoff_ready", 42)
    failed = base.failed_attempt(
        reason="handoff_ready",
        writer_id="writer-1",
        attempt_commit_id="commit-1",
        attempted_proofs={NORMAL_HANDOFF_PROOF_KEY: proof},
        error="unavailable",
        attempt=1,
        retry_count=0,
        exhausted=False,
    )

    committed = failed.committed_next(
        commit_id="commit-1",
        reason="handoff_ready",
        writer_id="writer-2",
        proofs={NORMAL_HANDOFF_PROOF_KEY: proof},
    )

    assert committed.generation == 1
    assert committed.parent_generation == 0
    assert committed.commit_id == "commit-1"
    assert committed.publication_proofs[NORMAL_HANDOFF_PROOF_KEY] == proof
    assert committed.attempt_commit_id is None
    assert committed.attempt_publication_proofs == {}
    assert committed.error is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 1},
        {"generation": True},
        {"generation": -1},
        {"session_id": ""},
        {"writer_id": ""},
        {"status": "unknown"},
        {"publication_proofs": {"unknown": {"event_id": "e", "event_type": "x", "sequence": 1}}},
        {
            "publication_proofs": {
                "normal_handoff": {"event_id": "e", "event_type": "pipeline_completed", "sequence": 1}
            }
        },
    ],
)
def test_from_dict_rejects_invalid_state(mutation: dict[str, object]) -> None:
    payload = SessionBackupState.bootstrap("s1", writer_id="writer-1").to_dict()
    payload.update(mutation)

    with pytest.raises(SessionBackupStateError):
        SessionBackupState.from_dict(payload)


def test_shared_state_rejects_failed_status() -> None:
    state = SessionBackupState.bootstrap("s1", writer_id="writer-1")
    failed = state.failed_attempt(
        reason="normal_turn_end",
        writer_id="writer-1",
        attempt_commit_id="commit-1",
        attempted_proofs={},
        error="failed",
        attempt=1,
        retry_count=0,
        exhausted=False,
    )

    with pytest.raises(SessionBackupStateError, match="shared backup state"):
        SessionBackupState.from_dict(failed.to_dict(), shared=True)


@pytest.mark.parametrize("sequence", [True, -1])
def test_publication_proof_rejects_invalid_sequence(sequence: object) -> None:
    with pytest.raises(SessionBackupStateError):
        BackupPublicationProof.from_dict(
            {"event_id": "event-1", "event_type": "pipeline_handoff_ready", "sequence": sequence}
        )


def test_same_lineage_compares_committed_identity_not_attempt_metadata() -> None:
    base = SessionBackupState.bootstrap("s1", writer_id="writer-1").committed_next(
        commit_id="commit-1",
        reason="normal_turn_end",
        writer_id="writer-1",
        proofs={},
    )
    failed = base.failed_attempt(
        reason="normal_turn_end",
        writer_id="writer-2",
        attempt_commit_id="commit-2",
        attempted_proofs={},
        error="failed",
        attempt=1,
        retry_count=0,
        exhausted=False,
    )

    assert failed.same_lineage(base)
    assert not failed.same_lineage(replace(base, commit_id="other"))
