"""Durable permission handoff proof and exact ownership CAS contracts."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from pydantic import ValidationError

from iac_code.a2a.input_handoff import (
    HandoffBackupReceipt,
    HandoffCommitResult,
    InputHandoffStore,
    RecoverableInputHandoffProof,
)


@pytest.fixture
def proof():
    return RecoverableInputHandoffProof(
        context_id="ctx",
        task_id="task",
        session_id="session",
        boundary_id="boundary",
        boundary_generation=3,
        execution_id="execution",
        control_revision=7,
        backup_commit_id="c62a3752-ec3c-4442-9be8-2f3f31397c25",
        phase="COMMITTED",
        backup_receipt=HandoffBackupReceipt(
            generation=4,
            commit_id="c62a3752-ec3c-4442-9be8-2f3f31397c25",
            shared_committed=True,
            staged_committed=True,
        ),
    )


def committed_snapshot(proof):
    return {
        "contextId": "ctx",
        "sessionId": "session",
        "taskId": "task",
        "executionId": "execution",
        "revision": 9,
        "persistedRevision": 9,
        "phase": "running",
        "executionStatus": "input-required",
        "streamAvailable": False,
        "activeSubprocessTools": 0,
        "subprocessToolTrackingVersion": 1,
        "blockers": [],
        "inputHandoffReady": True,
        "inputHandoff": proof.as_document(),
    }


def test_committed_proof_roundtrips_and_is_immutable(proof):
    assert RecoverableInputHandoffProof.from_document(proof.as_document()) == proof
    assert RecoverableInputHandoffProof.validates_committed(committed_snapshot(proof), "ctx", "task")
    with pytest.raises(ValidationError):
        proof.phase = "PREPARING"


@pytest.mark.parametrize(
    "path,value",
    [
        (("inputHandoffReady",), False),
        (("revision",), 10),
        (("persistedRevision",), 8),
        (("activeSubprocessTools",), 1),
        (("blockers",), [{"kind": "permission_cleanup", "count": 1}]),
        (("streamAvailable",), True),
        (("phase",), "paused"),
        (("executionStatus",), "working"),
        (("inputHandoff", "phase"), "PREPARING"),
        (("inputHandoff", "backupReceipt", "sharedCommitted"), False),
        (("inputHandoff", "backupReceipt", "generation"), 0),
        (("inputHandoff", "backupReceipt", "generation"), True),
        (("inputHandoff", "backupReceipt", "commitId"), "invalid"),
        (("inputHandoff", "executionId"), "other"),
    ],
)
def test_committed_proof_rejects_incomplete_or_unrelated_state(proof, path, value):
    snapshot = committed_snapshot(proof)
    target = snapshot
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert not RecoverableInputHandoffProof.validates_committed(snapshot, "ctx", "task")


def test_committed_proof_rejects_missing_work_tracking_and_wrong_context(proof):
    snapshot = committed_snapshot(proof)
    del snapshot["activeSubprocessTools"]
    assert not RecoverableInputHandoffProof.validates_committed(snapshot, "ctx")
    assert not RecoverableInputHandoffProof.validates_committed(committed_snapshot(proof), "other")


def test_snapshot_cas_applies_once_and_rejects_unrelated_new_revision(tmp_path, proof):
    path = tmp_path / "ctx.json"
    path.write_text(
        json.dumps(
            {
                "executionId": "execution",
                "revision": 8,
                "persistedRevision": 8,
                "inputHandoff": proof.model_copy(update={"phase": "PREPARING", "backup_receipt": None}).as_document(),
            }
        ),
        encoding="utf-8",
    )
    snapshot = committed_snapshot(proof)
    prior_proof = json.loads(path.read_text(encoding="utf-8"))["inputHandoff"]
    assert InputHandoffStore.write_snapshot(path, snapshot, "execution", 8, prior_proof) == HandoffCommitResult.APPLIED
    assert (
        InputHandoffStore.write_snapshot(path, snapshot, "execution", 8, prior_proof)
        == HandoffCommitResult.ALREADY_APPLIED
    )
    changed = deepcopy(snapshot)
    changed["backup"] = {"status": "unrelated"}
    assert (
        InputHandoffStore.write_snapshot(path, changed, "execution", 8, prior_proof) == HandoffCommitResult.FENCED_OUT
    )
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot


@pytest.mark.parametrize(
    "current",
    [
        {"executionId": "new", "revision": 8, "persistedRevision": 8},
        {"executionId": "execution", "revision": 8, "persistedRevision": 9},
        {"executionId": "execution", "revision": 9, "persistedRevision": 9},
    ],
)
def test_snapshot_cas_never_overwrites_stale_or_different_owner(tmp_path, proof, current):
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps(current), encoding="utf-8")
    assert (
        InputHandoffStore.write_snapshot(path, committed_snapshot(proof), "execution", 8)
        == HandoffCommitResult.FENCED_OUT
    )
    assert json.loads(path.read_text(encoding="utf-8")) == current


def test_snapshot_cas_requires_existing_owner_and_expected_proof(tmp_path, proof):
    path = tmp_path / "ctx.json"
    snapshot = committed_snapshot(proof)
    assert InputHandoffStore.write_snapshot(path, snapshot, "execution", 8) == HandoffCommitResult.FENCED_OUT
    path.write_text(json.dumps({"executionId": "execution", "revision": 8, "persistedRevision": 8}), encoding="utf-8")
    assert InputHandoffStore.write_snapshot(path, snapshot, "execution", 8, proof) == HandoffCommitResult.FENCED_OUT


def test_concurrent_cas_allows_only_one_distinct_snapshot(tmp_path, proof):
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps({"executionId": "execution", "revision": 8, "persistedRevision": 8}), encoding="utf-8")
    first = committed_snapshot(proof)
    second = deepcopy(first)
    second["marker"] = "second"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda snapshot: InputHandoffStore.write_snapshot(path, snapshot, "execution", 8), [first, second])
        )
    assert sorted(result.value for result in results) == ["APPLIED", "FENCED_OUT"]


@pytest.mark.parametrize("key,value", [("sessionId", "wrong"), ("sessionId", None)])
def test_committed_proof_binds_restored_session(proof, key, value):
    snapshot = committed_snapshot(proof)
    snapshot[key] = value
    assert not RecoverableInputHandoffProof.validates_committed(snapshot, "ctx")


def test_snapshot_cas_normalizes_only_target_persisted_revision(tmp_path, proof):
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps({"executionId": "execution", "revision": 8, "persistedRevision": 8}), encoding="utf-8")
    snapshot = committed_snapshot(proof)
    snapshot["persistedRevision"] = 8
    assert InputHandoffStore.write_snapshot(path, snapshot, "execution", 8) == HandoffCommitResult.APPLIED
    assert json.loads(path.read_text(encoding="utf-8"))["persistedRevision"] == 9
    assert InputHandoffStore.write_snapshot(path, snapshot, "execution", 8) == HandoffCommitResult.ALREADY_APPLIED


def test_snapshot_cas_requires_exact_preparing_proof(tmp_path, proof):
    path = tmp_path / "ctx.json"
    current = {
        "executionId": "execution",
        "revision": 8,
        "persistedRevision": 8,
        "inputHandoff": {"boundaryGeneration": 999},
    }
    path.write_text(json.dumps(current), encoding="utf-8")
    assert (
        InputHandoffStore.write_snapshot(path, committed_snapshot(proof), "execution", 8, proof)
        == HandoffCommitResult.FENCED_OUT
    )
    assert json.loads(path.read_text(encoding="utf-8")) == current


def test_direct_shared_backup_receipt_does_not_require_local_staging(proof):
    snapshot = committed_snapshot(proof)
    snapshot["inputHandoff"]["backupReceipt"]["stagedCommitted"] = False
    assert RecoverableInputHandoffProof.validates_committed(snapshot, "ctx", "task")
