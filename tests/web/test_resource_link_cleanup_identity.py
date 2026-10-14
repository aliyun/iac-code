"""Offline recovery of rotated credentials without resetting cloud counters."""

import copy
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.web.e2e.resource_link_workflow import cleanup_recovery
from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    path = tmp_path / "execution-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    identity = {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}}
    create = {
        "product": "vpc",
        "action": "CreateVSwitch",
        "region_id": "cn-hangzhou",
        "params": {"VSwitchName": "offline-own"},
    }
    delete = {
        "product": "vpc",
        "action": "DeleteVSwitch",
        "region_id": "cn-hangzhou",
        "params": {"VSwitchId": "$created_id"},
    }
    grants = [
        {"id": "identity", "kind": "identity", "quota": 1, "call": identity},
        {"id": "create", "kind": "create", "quota": 1, "call": create},
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "call": delete,
            "parent": "create",
            "response_path": ["VSwitchId"],
            "target_param": "VSwitchId",
        },
    ]
    gate.install(path, "0", "original:CREATE", "ws~original", "123", None, grants, authority="original:CREATE")
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    gate.finish(gate.reserve(identity, "ws~original", credential, read_only=True), {"AccountId": "123"}, True)
    gate.finish(gate.reserve(create, "ws~original", credential, read_only=False), {"VSwitchId": "vsw-own"}, True)

    def inspect():
        return cleanup_recovery.inspect_candidate(
            path,
            slot="0",
            scope="original:CREATE",
            session="ws~original",
            account="123",
            grants=grants,
            authority="original:CREATE",
        )

    candidate = inspect()
    evidence = "inputs/identity-review.json"
    review = {
        "kind": "ops_cleanup_identity_review",
        "approved": True,
        **{key: candidate[key] for key in ("scope", "authority", "account", "owned_from_wire")},
        "session": candidate["session_id"],
        "additional_identity_reads": 1,
        "identity_call": identity,
        "reason": "Actual original credentials expired; original owned deletion remains unused.",
    }
    json_write(tmp_path / evidence, review, private=True)
    return tmp_path, candidate, evidence, review, inspect, credential


def original_rows(root):
    with sqlite3.connect(root / "execution-gate.sqlite3") as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("active", "grants", "authority_limits", "attempts", "identities")
        }


def test_cleanup_identity_is_append_only_and_one_shot(recovery):
    root, candidate, evidence, _, inspect, _ = recovery
    before = original_rows(root)
    result = cleanup_recovery.authorize_identity(root, candidate, evidence)
    assert result["additional_create_delete"] == 0 and result["cloud_call_performed"] is False
    after = original_rows(root)
    for table in before:
        assert all(row in after[table] for row in before[table])
    assert len(after["grants"]) == len(before["grants"]) + 1
    assert len(after["authority_limits"]) == len(before["authority_limits"]) + 1
    assert after["attempts"] == before["attempts"] and after["active"] == before["active"]
    assert next(x for x in inspect()["remaining_calls"] if x["id"] == result["grant_id"])["remaining"] == 1
    with pytest.raises(WorkflowError):
        cleanup_recovery.authorize_identity(root, candidate, evidence)
    assert original_rows(root) == after


def test_cleanup_identity_is_not_advertised_to_creation_phase(recovery):
    root, candidate, evidence, _, _, _ = recovery
    cleanup_recovery.authorize_identity(root, candidate, evidence)
    assert gate.execution_instructions(candidate["session_id"], "").count('"GetCallerIdentity"') == 1
    gate.cleanup(root / "execution-gate.sqlite3", "0", candidate["scope"], candidate["session_id"])
    assert gate.execution_instructions(candidate["session_id"], "").count('"GetCallerIdentity"') == 2


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "Actual same-account OAuth recovery independently verified; prior failure was prewire.",
    ],
)
def test_original_cleanup_delivers_frozen_recovery_context_without_consuming_calls(recovery, reason):
    root, candidate, evidence, _, inspect, _ = recovery
    cleanup_recovery.authorize_identity(root, candidate, evidence)
    gate.cleanup(root / "execution-gate.sqlite3", "0", candidate["scope"], candidate["session_id"])
    deletion = next(x["call"] for x in inspect()["remaining_calls"] if x["kind"] == "delete")
    proof = {
        "kind": "ops_cleanup_only_review",
        "approved": True,
        "original_job_id": "original",
        "dimension": "CREATE",
        "scope": candidate["scope"],
        "authority": candidate["authority"],
        "account": candidate["account"],
        "delete_call": deletion,
    }
    if reason is not None:
        proof["reason"] = reason
    json_write(root / "inputs/cleanup-review.json", proof, private=True)
    recovery_state = {
        "original_job_id": "original",
        "dimension": "CREATE",
        "authority": candidate["authority"],
        "evidence": "inputs/cleanup-review.json",
        "evidence_sha256": digest(root / "inputs/cleanup-review.json"),
    }
    before = original_rows(root)
    text = cleanup_recovery.exact_delete_request(root, recovery_state, "0", candidate["session_id"])
    assert ("OPS 本次独立恢复审查说明" in text) is (reason is not None)
    if reason:
        assert reason in text
    assert text.count('"GetCallerIdentity"') == 1
    assert '"VSwitchId":"vsw-own"' in text
    assert original_rows(root) == before
    json_write(root / "inputs/cleanup-review.json", {**proof, "reason": "changed"}, private=True)
    with pytest.raises(WorkflowError, match="review changed"):
        cleanup_recovery.exact_delete_request(root, recovery_state, "0", candidate["session_id"])


@pytest.mark.parametrize("reason", [False, {}, "", "x" * 5001])
def test_original_cleanup_rejects_invalid_recovery_context_without_cloud_calls(recovery, reason):
    root, candidate, _, _, inspect, _ = recovery
    gate.cleanup(root / "execution-gate.sqlite3", "0", candidate["scope"], candidate["session_id"])
    deletion = next(x["call"] for x in inspect()["remaining_calls"] if x["kind"] == "delete")
    proof = {
        "kind": "ops_cleanup_only_review",
        "approved": True,
        "original_job_id": "original",
        "dimension": "CREATE",
        "scope": candidate["scope"],
        "authority": candidate["authority"],
        "account": candidate["account"],
        "delete_call": deletion,
        "reason": reason,
    }
    json_write(root / "inputs/cleanup-review.json", proof, private=True)
    recovery_state = {
        "original_job_id": "original",
        "dimension": "CREATE",
        "authority": candidate["authority"],
        "evidence": "inputs/cleanup-review.json",
        "evidence_sha256": digest(root / "inputs/cleanup-review.json"),
    }
    before = original_rows(root)
    with pytest.raises(WorkflowError, match="explanation invalid"):
        cleanup_recovery.exact_delete_request(root, recovery_state, "0", candidate["session_id"])
    assert original_rows(root) == before


@pytest.mark.parametrize("account", ["123", "999"])
def test_real_rotated_identity_required_only_in_cleanup(recovery, account):
    root, candidate, evidence, review, inspect, credential = recovery
    result = cleanup_recovery.authorize_identity(root, candidate, evidence)
    rotated = SimpleNamespace(mode="AK", access_key_id="rotated-fake", access_key_secret="rotated-secret")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(review["identity_call"], "ws~original", rotated, read_only=True)
    gate.cleanup(root / "execution-gate.sqlite3", "0", candidate["scope"], candidate["session_id"])
    delete = next(x["call"] for x in inspect()["remaining_calls"] if x["kind"] == "delete")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~original", rotated, read_only=False)
    ticket = gate.reserve(review["identity_call"], "ws~original", rotated, read_only=True)
    assert ticket[1]
    gate.finish(ticket, {"AccountId": account}, True)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(review["identity_call"], "ws~original", rotated, read_only=True)
    if account != "123":
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(delete, "ws~original", rotated, read_only=False)
        return
    gate.finish(gate.reserve(delete, "ws~original", rotated, read_only=False), {}, True)
    calls = inspect()["remaining_calls"]
    assert next(x for x in calls if x["id"] == result["grant_id"])["remaining"] == 0
    assert next(x for x in calls if x["id"] == "identity")["remaining"] == 0
    assert next(x for x in calls if x["id"] == "delete")["remaining"] == 0
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete, "ws~original", credential, read_only=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", False),
        ("scope", "other:CREATE"),
        ("authority", "other"),
        ("session", "ws~other"),
        ("account", "999"),
        ("owned_from_wire", []),
        ("additional_identity_reads", 2),
        ("additional_identity_reads", True),
        ("identity_call", {"product": "sts", "action": "Other"}),
    ],
)
def test_cleanup_identity_rejects_wrong_or_expanded_review(recovery, field, value):
    root, candidate, evidence, review, _, _ = recovery
    review = {**review, field: value}
    json_write(root / evidence, review, private=True)
    before = original_rows(root)
    with pytest.raises(WorkflowError):
        cleanup_recovery.authorize_identity(root, candidate, evidence)
    assert original_rows(root) == before


def test_cleanup_identity_review_tamper_revokes_supplement_not_original_records(recovery):
    root, candidate, evidence, review, inspect, _ = recovery
    cleanup_recovery.authorize_identity(root, candidate, evidence)
    before = original_rows(root)
    json_write(root / evidence, {**review, "reason": "changed"}, private=True)
    with pytest.raises(WorkflowError):
        inspect()
    gate.cleanup(root / "execution-gate.sqlite3", "0", candidate["scope"], candidate["session_id"])
    credential = SimpleNamespace(mode="AK", access_key_id="rotated", access_key_secret="rotated")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(review["identity_call"], candidate["session_id"], credential, read_only=True)
    after = original_rows(root)
    assert before["attempts"] == after["attempts"] and before["grants"] == after["grants"]


@pytest.mark.parametrize("state", ["unknown", "used_delete", "remaining_identity"])
def test_cleanup_identity_does_not_recover_unknown_or_refill_used_deletes(recovery, state):
    root, candidate, evidence, _, _, _ = recovery
    candidate = copy.deepcopy(candidate)
    if state == "unknown":
        with sqlite3.connect(root / "execution-gate.sqlite3") as connection:
            connection.execute("UPDATE attempts SET state='unknown' WHERE grant_id='create'")
    elif state == "used_delete":
        with sqlite3.connect(root / "execution-gate.sqlite3") as connection:
            connection.execute("UPDATE authority_limits SET used=1 WHERE id='delete'")
    else:
        candidate["remaining_calls"][0]["remaining"] = 1
    before = original_rows(root)
    with pytest.raises(WorkflowError):
        cleanup_recovery.authorize_identity(root, candidate, evidence)
    assert original_rows(root) == before
