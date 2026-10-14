"""Offline OPS projection never guesses managed children or renews authority."""

import json

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate, readonly_review_fields
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from tests.web.test_resource_link_workflow_read_policy import (
    counts,
    mse_perform,
    mse_proofs,
    mse_setup,
    mse_source_bodies,
)


def inspect(setup):
    path, _, _, grants = setup
    return inspect_candidate(
        path,
        slot="0",
        scope="offline-mse:TF",
        session="ws~mse-full",
        account="123",
        grants=grants,
        authority="offline-mse-authority",
    )


def test_historical_success_code_does_not_block_original_readonly_cleanup(tmp_path, monkeypatch):
    setup = mse_setup(tmp_path, monkeypatch)
    mse_proofs(setup)
    mse_perform(
        setup,
        "cluster-execute",
        {
            "RequestId": "own-detail",
            "Success": True,
            "ErrorCode": "Success",
            "Data": {"InstanceId": "mse-cn-own", "RegionId": "cn-hangzhou"},
        },
    )
    with gate.database(setup[0]) as db:
        db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='cluster-execute'")
    gate.cleanup(setup[0], "0", "offline-mse:TF", "ws~mse-full")
    mse_perform(setup, "mse-delete", {"RequestId": "original-delete"})
    before = counts(setup[0])
    fields = readonly_review_fields(inspect(setup))
    assert {r["id"] for r in fields["cleanup_read_calls"]} == {
        "mse-status-cleanup",
        "mse-name-cleanup",
        "cluster-cleanup",
        "vpc-cleanup",
        "vswitch-cleanup",
    }
    assert counts(setup[0]) == before


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_complete_mse_inspection_uses_original_members_and_keeps_gate(tmp_path, monkeypatch, deleted, nested):
    setup = mse_setup(tmp_path, monkeypatch)
    path, _, _, _ = setup
    mse_proofs(setup, nested=nested)
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    if deleted:
        mse_perform(setup, "mse-delete", {"RequestId": "original-delete"})
    original_owners, before = gate.creations(path, ["offline-mse"]), counts(path)
    candidate = inspect(setup)
    calls = {row["id"]: row for row in candidate["remaining_calls"]}
    assert candidate["owned_from_wire"][0]["identifier"] == "mse-stack-own"
    assert calls["cluster-cleanup"]["call"]["params"]["InstanceId"] == "mse-cn-own"
    assert calls["vpc-cleanup"]["call"]["params"]["VpcId"] == "vpc-own"
    assert calls["vswitch-cleanup"]["call"]["params"]["VSwitchId"] == "vsw-own"
    assert calls["cluster-resource"]["call"]["params"] == {
        "RegionId": "cn-hangzhou",
        "StackId": "mse-stack-own",
        "LogicalResourceId": "sample",
        "ShowResourceAttributes": True,
    }
    assert not candidate["unresolved_managed_children"]
    if deleted:
        fields = readonly_review_fields(candidate)
        assert {row["id"] for row in fields["cleanup_read_calls"]} == {
            "mse-status-cleanup",
            "mse-name-cleanup",
            "cluster-cleanup",
            "vpc-cleanup",
            "vswitch-cleanup",
        }
    else:
        with pytest.raises(WorkflowError):
            readonly_review_fields(candidate)
    assert counts(path) == before and gate.creations(path, ["offline-mse"]) == original_owners
    assert not candidate["cleanup_confirmed"] and not candidate["activation_allowed"]


@pytest.mark.parametrize("members", ["missing", "empty", "networks"])
@pytest.mark.parametrize("deleted", [False, True])
def test_rollback_inspection_keeps_unresolved_obligations_and_root_cleanup_only(
    tmp_path,
    monkeypatch,
    members,
    deleted,
):
    setup = mse_setup(tmp_path, monkeypatch)
    path, _, _, _ = setup
    status, body = mse_source_bodies()
    status["Status"] = "CREATE_ROLLBACK_COMPLETE"
    status.pop("Outputs")
    mse_perform(setup, "mse-status", status)
    if members != "missing":
        body["Resources"] = body["Resources"][1:] if members == "networks" else []
        mse_perform(setup, "mse-members", body)
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    if deleted:
        mse_perform(setup, "mse-delete", {"RequestId": "original-rollback-delete"})
    before, owners = counts(path), gate.creations(path, ["offline-mse"])
    candidate = inspect(setup)
    calls = {row["id"]: row for row in candidate["remaining_calls"]}
    assert calls["mse-delete"]["call"]["params"]["StackId"] == "mse-stack-own"
    assert calls["mse-delete"]["remaining"] == (0 if deleted else 1)
    assert len(candidate["unresolved_managed_children"]) == 9
    assert not any(child + "-cleanup" in calls for child in ("cluster", "vpc", "vswitch"))
    assert all(row["call_deliverable"] is False for row in candidate["unresolved_managed_children"])
    with pytest.raises(WorkflowError, match="independent reconciliation"):
        readonly_review_fields(candidate)
    assert counts(path) == before and gate.creations(path, ["offline-mse"]) == owners
    assert all(row["state"] == "unknown" for row in owners[1:])


@pytest.mark.parametrize("damage", ["parent_unknown", "session", "signer", "wire", "status", "identity"])
def test_mse_inspection_fails_closed_on_original_provenance_damage(tmp_path, monkeypatch, damage):
    setup = mse_setup(tmp_path, monkeypatch)
    path, _, _, _ = setup
    mse_proofs(setup)
    with gate.database(path) as db:
        if damage == "parent_unknown":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='mse-create'")
        elif damage == "identity":
            db.execute("DELETE FROM identities")
        elif damage in {"session", "signer"}:
            field = "session" if damage == "session" else "credential"
            db.execute(f"UPDATE attempts SET {field}='foreign' WHERE grant_id='mse-status'")
        else:
            row = db.execute("SELECT id,response FROM attempts WHERE grant_id='mse-status'").fetchone()
            response = json.loads(row["response"])
            if damage == "wire":
                response["call"]["params"]["StackId"] = "foreign"
            else:
                response["body"]["Status"] = "CREATE_FAILED"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(response), row["id"]))
    before = counts(path)
    with pytest.raises(WorkflowError):
        inspect(setup)
    assert counts(path) == before
