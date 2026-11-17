"""Hosted MSE phase/cleanup integration, entirely fake offline wire responses."""

import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.phases import (
    assert_cleanup_reads_complete,
    assert_stack_source_ready,
    known_original_absence_read,
    known_original_resolved_read,
    missing_cleanup_read_delivery,
    readonly_continuation,
)
from tests.web.test_resource_link_workflow_read_policy import (
    counts,
    mse_perform,
    mse_proofs,
    mse_setup,
    mse_source_bodies,
)


def rows(db):
    return db.execute(
        "SELECT a.*,g.kind,g.call,g.parent FROM attempts a JOIN grants g "
        "ON a.scope=g.scope AND a.grant_id=g.id ORDER BY a.created"
    ).fetchall()


def deleted(tmp_path, monkeypatch):
    setup = mse_setup(tmp_path, monkeypatch)
    mse_proofs(setup)
    gate.cleanup(setup[0], "0", "offline-mse:TF", "ws~mse-full")
    mse_perform(setup, "mse-delete", {"RequestId": "offline-deletion"})
    return setup


@pytest.mark.parametrize("code", ["Success", "mse-100-000", "", None, "NoPermission"])
@pytest.mark.parametrize("success", [True, False, "true"])
def test_mse_success_code_transport_classification_keeps_original_body(tmp_path, monkeypatch, code, success):
    setup = mse_setup(tmp_path, monkeypatch)
    mse_proofs(setup)
    body = {
        "Success": success,
        "ErrorCode": code,
        "RequestId": "original-detail",
        "Data": {"InstanceId": "mse-cn-own", "RegionId": "cn-hangzhou"},
    }
    mse_perform(setup, "cluster-execute", body)
    with gate.database(setup[0]) as db:
        row = next(r for r in rows(db) if r["grant_id"] == "cluster-execute")
        assert (row["state"] == "succeeded") is (success is True and code != "NoPermission")
        assert json.loads(row["response"])["body"] == body


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "signer",
        "session",
        "scope",
        "tool",
        "counter",
        "account",
        "parent",
        "members",
        "header",
        "status",
        "code",
        "success",
        "id",
        "region",
        "call",
        "order",
        "write",
        "parent-counter",
        "data-account",
        "data-vpc",
        "data-vswitch",
    ],
)
def test_historical_mse_success_is_resolved_not_absent_without_journal_or_quota_edit(
    tmp_path,
    monkeypatch,
    damage,
):
    setup = mse_setup(tmp_path, monkeypatch)
    path = setup[0]
    mse_proofs(setup)
    mse_perform(
        setup,
        "cluster-execute",
        {
            "Success": True,
            "ErrorCode": "Success",
            "RequestId": "original-detail",
            "Data": {"InstanceId": "mse-cn-own", "RegionId": "cn-hangzhou"},
        },
    )
    # Fixture reproduces the old classifier's persisted state; production never updates it.
    with gate.database(path) as db:
        db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='cluster-execute'")
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    mse_perform(setup, "mse-delete", {"RequestId": "original-deletion"})
    with gate.database(path) as db:
        row = next(r for r in rows(db) if r["grant_id"] == "cluster-execute")
        envelope = json.loads(row["response"])
        if damage in {"signer", "session", "scope", "tool"}:
            column = {"signer": "credential", "session": "session", "scope": "reference_scope", "tool": "tool_use_id"}[
                damage
            ]
            db.execute(f"UPDATE attempts SET {column}='' WHERE id=?", (row["id"],))
        elif damage == "counter":
            db.execute("UPDATE authority_limits SET used=0 WHERE id='cluster-execute'")
        elif damage == "account":
            db.execute("UPDATE identities SET account='foreign'")
        elif damage == "parent":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='mse-create'")
        elif damage == "parent-counter":
            db.execute("UPDATE authority_limits SET used=0 WHERE id='mse-create'")
        elif damage == "members":
            db.execute("UPDATE attempts SET response='{}' WHERE grant_id='mse-members'")
        elif damage == "order":
            db.execute("UPDATE attempts SET created=999999 WHERE id=?", (row["id"],))
        elif damage == "write":
            db.execute("UPDATE grants SET kind='delete' WHERE id='cluster-execute'")
        else:
            if damage == "header":
                envelope["headers"] = {"x-acs-request-id": "foreign"}
            elif damage == "status":
                envelope["status"] = 500
            elif damage == "code":
                envelope["body"]["Code"] = "NoPermission"
            elif damage == "success":
                envelope["body"]["Success"] = False
            elif damage == "id":
                envelope["body"]["Data"]["InstanceId"] = "mse-foreign"
            elif damage == "region":
                envelope["body"]["Data"]["RegionId"] = "cn-shenzhen"
            elif damage == "call":
                envelope["call"]["params"]["InstanceId"] = "mse-foreign"
            elif damage in {"data-account", "data-vpc", "data-vswitch"}:
                key = {"data-account": "AccountId", "data-vpc": "VpcId", "data-vswitch": "VSwitchId"}[damage]
                envelope["body"]["Data"][key] = "foreign"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
        row = next(r for r in rows(db) if r["grant_id"] == "cluster-execute")
        before = db.total_changes
        assert known_original_resolved_read(db, row) is (damage is None)
        assert not known_original_absence_read(db, row)
        assert db.total_changes == before
        assert row["state"] == "unknown"
    if damage is None:
        before = counts(path)
        assert "mse-cn-own" in readonly_continuation(path, "0", "offline-mse:TF", "ws~mse-full", "cleanup")
        assert counts(path) == before


ERRORS = {
    "cluster": (404, "NotFound", "Not found:mse-cn-own"),
    "vpc": (404, "InvalidVpcId.NotFound", "Specified VPC does not exist."),
    "vswitch": (400, "InvalidVSwitchId.NotFound", "Specified VSwitch can not found."),
}


def absent(setup, child, *, damage=None):
    status, code, message = ERRORS[child]
    body = {"RequestId": "offline-absence", "Code": code, "Message": message}
    if damage == "permission":
        status, body["Code"] = 403, "NoPermission"
    elif damage == "server":
        status = 500
    elif damage == "message":
        body["Message"] = "Unknown failure"
    elif damage == "host":
        body["HostId"] = "foreign.aliyuncs.com"
    elif damage == "request":
        body.pop("RequestId")
    mse_perform(setup, child + "-cleanup", body, status=status, success=False)


def test_mse_pending_delivery_and_readiness_keep_original_members_and_remaining_quotas(tmp_path, monkeypatch):
    setup = deleted(tmp_path, monkeypatch)
    path = setup[0]
    before = counts(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        calls = missing_cleanup_read_delivery(db, db.execute("SELECT * FROM grants").fetchall(), rows(db))
    calls = {item["id"]: item for item in calls}
    assert calls["cluster-cleanup"]["call"]["params"]["InstanceId"] == "mse-cn-own"
    assert calls["vpc-cleanup"]["call"]["params"]["VpcId"] == "vpc-own"
    assert calls["vswitch-cleanup"]["call"]["params"]["VSwitchId"] == "vsw-own"
    assert calls["cluster-cleanup"]["remaining"] == 2
    with pytest.raises(WorkflowError, match="pending"):
        assert_stack_source_ready(path, "offline-mse:TF", cleanup=True)
    assert "mse-cn-own" in readonly_continuation(path, "0", "offline-mse:TF", "ws~mse-full", "cleanup")
    assert counts(path) == before
    for child in ERRORS:
        absent(setup, child)
    # Exact original negative reads satisfy execution completeness, not a PASS.
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        pending = missing_cleanup_read_delivery(db, db.execute("SELECT * FROM grants").fetchall(), rows(db))
        assert {r["id"] for r in pending} == {"mse-status-cleanup", "mse-name-cleanup"}
        assert all(
            known_original_absence_read(db, r)
            for r in rows(db)
            if r["grant_id"] in {child + "-cleanup" for child in ERRORS}
        )
        assert db.execute("SELECT COUNT(*) FROM attempts WHERE state='unknown'").fetchone()[0] == 3
    with pytest.raises(WorkflowError, match="required post-delete"):
        assert_cleanup_reads_complete(path, "offline-mse:TF")
    mse_perform(
        setup,
        "mse-status-cleanup",
        {
            "RequestId": "offline-stack-absence",
            "Code": "StackNotFound",
            "Message": "The Stack (mse-stack-own) could not be found.",
        },
        status=404,
        success=False,
    )
    mse_perform(
        setup,
        "mse-name-cleanup",
        {"RequestId": "offline-name", "Success": True, "ErrorCode": "mse-100-000", "TotalCount": 0, "Data": []},
    )
    assert_stack_source_ready(path, "offline-mse:TF", cleanup=True)
    assert_cleanup_reads_complete(path, "offline-mse:TF")
    assert counts(path)[0]["mse-create"] == counts(path)[0]["mse-delete"] == 1


@pytest.mark.parametrize("nested", [False, True])
def test_mse_actual_success_envelope_can_finish_stack_source_capture(tmp_path, monkeypatch, nested):
    setup = mse_setup(tmp_path, monkeypatch)
    status, members = mse_source_bodies(nested=nested)
    mse_perform(setup, "mse-status", status)
    mse_perform(setup, "mse-members", members)
    assert_stack_source_ready(setup[0], "offline-mse:TF", cleanup=False)


@pytest.mark.parametrize("child", list(ERRORS))
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "permission",
        "server",
        "message",
        "host",
        "request",
        "call",
        "signer",
        "session",
        "scope",
        "tool",
        "counter",
        "delete",
    ],
)
def test_mse_original_absence_is_exact_read_only_and_tamper_resistant(tmp_path, monkeypatch, child, damage):
    setup = deleted(tmp_path, monkeypatch)
    absent(setup, child, damage=damage)
    with gate.database(setup[0]) as db:
        row = next(r for r in rows(db) if r["grant_id"] == child + "-cleanup")
        if damage == "call":
            response = json.loads(row["response"])
            response["call"]["params"]["RegionId"] = "cn-shenzhen"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(response), row["id"]))
        elif damage in {"signer", "session", "scope", "tool"}:
            column = {"signer": "credential", "session": "session", "scope": "reference_scope", "tool": "tool_use_id"}[
                damage
            ]
            db.execute(f"UPDATE attempts SET {column}='' WHERE id=?", (row["id"],))
        elif damage == "counter":
            db.execute("UPDATE authority_limits SET used=0 WHERE id=?", (child + "-cleanup",))
        elif damage == "delete":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='mse-delete'")
        row = next(r for r in rows(db) if r["grant_id"] == child + "-cleanup")
        before = db.total_changes
        assert known_original_absence_read(db, row) is (damage is None)
        assert db.total_changes == before
