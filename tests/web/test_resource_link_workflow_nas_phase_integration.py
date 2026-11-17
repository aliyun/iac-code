"""Exercise the managed NAS gate through the actual workflow phase boundaries."""

import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow import phases
from tests.web.test_resource_link_workflow_read_policy import gate, nas_perform, nas_proofs, nas_setup


def test_managed_nas_actual_stack_envelope_is_source_ready(tmp_path, monkeypatch):
    setup = nas_setup(tmp_path, monkeypatch)
    nas_proofs(setup)
    phases.assert_stack_source_ready(setup[0], "offline-nas:TF", cleanup=False)


def test_managed_nas_required_cleanup_delivery_projects_own_children(tmp_path, monkeypatch):
    setup = nas_setup(tmp_path, monkeypatch)
    nas_proofs(setup)
    gate.cleanup(setup[0], "0", "offline-nas:TF", "ws~nas-full")
    nas_perform(setup, "delete", {"RequestId": "offline-deleted"})
    with gate.database(setup[0]) as db:
        grants = db.execute("SELECT * FROM grants WHERE scope='offline-nas:TF'").fetchall()
        results = db.execute(
            "SELECT a.*,g.kind,g.call,g.parent FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope='offline-nas:TF'"
        ).fetchall()
        pending = phases.missing_cleanup_read_delivery(db, grants, results)
    children = {entry["id"]: entry["call"]["params"] for entry in pending if entry["id"].endswith("-cleanup")}
    assert children["filesystem-cleanup"]["FileSystemId"] == "offlinefs"
    assert children["mount_target-cleanup"]["FileSystemId"] == "offlinefs"
    assert children["mount_target-cleanup"]["MountTargetDomain"] == "offlinefs-target.cn-hangzhou.nas.aliyuncs.com"


def nas_cleanup(setup, *, stack_not_found=False):
    path = setup[0]
    nas_proofs(setup)
    gate.cleanup(path, "0", "offline-nas:TF", "ws~nas-full")
    nas_perform(setup, "delete", {"RequestId": "offline-deleted"})
    if stack_not_found:
        nas_perform(
            setup,
            "status-cleanup",
            {
                "RequestId": "offline-stack-gone",
                "Code": "StackNotFound",
                "Message": "The Stack (stack-own) could not be found.",
            },
            success=False,
            status=404,
        )
    else:
        nas_perform(
            setup,
            "status-cleanup",
            {
                "RequestId": "offline-stack-gone",
                "StackId": "stack-own",
                "Status": "DELETE_COMPLETE",
            },
        )
    nas_perform(setup, "absence-name", {"RequestId": "offline-name-gone", "Stacks": [], "TotalCount": 0})
    nas_perform(
        setup,
        "filesystem-cleanup",
        {
            "RequestId": "offline-fs-gone",
            "FileSystems": {"FileSystem": []},
            "TotalCount": 0,
        },
    )
    nas_perform(
        setup,
        "mount_target-cleanup",
        {
            "Code": "InvalidFileSystem.NotFound",
            "RequestId": "offline-mount-gone",
        },
        success=False,
        status=404,
    )
    return path


def test_managed_nas_exact_negative_cleanup_read_is_complete_not_a_verdict(tmp_path, monkeypatch):
    path = nas_cleanup(nas_setup(tmp_path, monkeypatch))
    phases.assert_cleanup_reads_complete(path, "offline-nas:TF")
    with gate.database(path) as db:
        assert db.execute("SELECT state FROM attempts WHERE grant_id='mount_target-cleanup'").fetchone()[0] == "unknown"


def test_managed_nas_own_exact_stack_not_found_is_ready_without_rewriting_cloud_status(tmp_path, monkeypatch):
    path = nas_cleanup(nas_setup(tmp_path, monkeypatch), stack_not_found=True)
    phases.assert_stack_source_ready(path, "offline-nas:TF", cleanup=True)
    phases.assert_cleanup_reads_complete(path, "offline-nas:TF")
    with gate.database(path) as db:
        row = db.execute("SELECT state,response FROM attempts WHERE grant_id='status-cleanup'").fetchone()
        assert row["state"] == "unknown"
        assert json.loads(row["response"])["body"]["Code"] == "StackNotFound"


@pytest.mark.parametrize("stack_not_found", [False, True])
def test_ops_tuple_connection_classifies_original_nas_absence_without_changing_factory_or_journal(
    tmp_path,
    monkeypatch,
    stack_not_found,
):
    path = nas_cleanup(nas_setup(tmp_path, monkeypatch), stack_not_found=stack_not_found)
    with sqlite3.connect(path) as db:
        assert db.row_factory is None
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        rows = cursor.execute(
            "SELECT a.*,g.kind,g.call FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.state='unknown'"
        ).fetchall()
        before = list(db.iterdump())
        assert len(rows) == (2 if stack_not_found else 1)
        assert all(phases.known_original_absence_read(db, row) for row in rows)
        assert db.row_factory is None and list(db.iterdump()) == before


@pytest.mark.parametrize(
    "damage",
    [
        "generic404",
        "other_error",
        "http403",
        "missing_request",
        "wrong_request_header",
        "wrong_host",
        "wrong_call_fs",
        "wrong_call_domain",
        "wrong_session",
        "wrong_signer",
        "wrong_account",
        "before_delete",
        "delete_unknown",
        "foreign_delete",
        "foreign_member",
        "foreign_stack",
        "duplicate_member",
        "quota_limit",
        "quota_used",
        "extra_attempt",
        "execute_phase",
        "missing_required",
    ],
)
def test_managed_nas_negative_reads_reject_unbound_unknowns_without_mutating_wire(tmp_path, monkeypatch, damage):
    path = nas_cleanup(nas_setup(tmp_path, monkeypatch))
    with gate.database(path) as db:
        attempt = db.execute("SELECT * FROM attempts WHERE grant_id='mount_target-cleanup'").fetchone()
        response = json.loads(attempt["response"])
        if damage == "generic404":
            response["body"]["Code"] = "NotFound"
        elif damage == "other_error":
            response["body"]["ErrorCode"] = "Unknown"
        elif damage == "http403":
            response["status"] = 403
        elif damage == "missing_request":
            response["body"].pop("RequestId")
        elif damage == "wrong_request_header":
            response["headers"] = {"x-acs-request-id": "foreign"}
        elif damage == "wrong_host":
            response["body"]["HostId"] = "foreign"
        elif damage.startswith("wrong_call_"):
            response["call"]["params"]["FileSystemId" if damage == "wrong_call_fs" else "MountTargetDomain"] = "foreign"
        elif damage in {"wrong_session", "wrong_signer"}:
            column = "session" if damage == "wrong_session" else "credential"
            db.execute(f"UPDATE attempts SET {column}='foreign' WHERE id=?", (attempt["id"],))
        elif damage == "wrong_account":
            db.execute("UPDATE identities SET account='foreign'")
        elif damage == "before_delete":
            db.execute("UPDATE attempts SET created=1001 WHERE id=?", (attempt["id"],))
        elif damage == "delete_unknown":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='delete'")
        elif damage == "foreign_delete":
            db.execute("UPDATE attempts SET credential='foreign' WHERE grant_id='delete'")
        elif damage in {"foreign_member", "foreign_stack", "duplicate_member"}:
            grant_id = "status-source" if damage == "foreign_stack" else "member-source"
            row = db.execute("SELECT * FROM attempts WHERE grant_id=?", (grant_id,)).fetchone()
            proof = json.loads(row["response"])
            if damage == "foreign_stack":
                proof["body"]["StackId"] = "foreign"
            elif damage == "foreign_member":
                proof["body"]["Resources"][0]["PhysicalResourceId"] = "foreign"
            else:
                proof["body"]["Resources"].append(proof["body"]["Resources"][0])
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(proof), row["id"]))
        elif damage == "quota_limit":
            db.execute("UPDATE authority_limits SET used=0 WHERE id='mount_target-cleanup'")
        elif damage == "quota_used":
            db.execute("UPDATE grants SET used=0 WHERE id='mount_target-cleanup'")
        elif damage == "extra_attempt":
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                "SELECT 'duplicate',scope,grant_id,session,state,response,credential,created+1 "
                "FROM attempts WHERE id=?",
                (attempt["id"],),
            )
        else:
            policy = {"phase": "execute"} if damage == "execute_phase" else {"phase": "cleanup"}
            db.execute("UPDATE grants SET read_policy=? WHERE id='mount_target-cleanup'", (json.dumps(policy),))
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(response), attempt["id"]))
        original = db.execute(
            "SELECT a.*,g.kind,g.call,g.parent FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
            (attempt["id"],),
        ).fetchone()
        before = list(db.iterdump())
        assert phases.known_original_absence_read(db, original) is False
        assert list(db.iterdump()) == before


@pytest.mark.parametrize(
    "damage",
    [
        "message",
        "http403",
        "header",
        "host",
        "call",
        "session",
        "signer",
        "account",
        "before_delete",
        "delete_unknown",
        "foreign_delete",
        "quota_used",
        "extra_attempt",
        "member",
    ],
)
def test_managed_nas_stack_404_rejects_unbound_proofs_without_wire_changes(tmp_path, monkeypatch, damage):
    path = nas_cleanup(nas_setup(tmp_path, monkeypatch), stack_not_found=True)
    with gate.database(path) as db:
        row = db.execute("SELECT * FROM attempts WHERE grant_id='status-cleanup'").fetchone()
        response = json.loads(row["response"])
        if damage == "message":
            response["body"]["Message"] = "The Stack (foreign) could not be found."
        elif damage == "http403":
            response["status"] = 403
        elif damage == "header":
            response["headers"] = {"X-Acs-Request-Id": "foreign"}
        elif damage == "host":
            response["body"]["HostId"] = "foreign"
        elif damage == "call":
            response["call"]["params"]["StackId"] = "foreign"
        elif damage in {"session", "signer"}:
            column = "session" if damage == "session" else "credential"
            db.execute(f"UPDATE attempts SET {column}='foreign' WHERE id=?", (row["id"],))
        elif damage == "account":
            db.execute("UPDATE identities SET account='foreign'")
        elif damage == "before_delete":
            db.execute("UPDATE attempts SET created=1001 WHERE id=?", (row["id"],))
        elif damage == "delete_unknown":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='delete'")
        elif damage == "foreign_delete":
            db.execute("UPDATE attempts SET credential='foreign' WHERE grant_id='delete'")
        elif damage == "quota_used":
            db.execute("UPDATE grants SET used=0 WHERE id='status-cleanup'")
        elif damage == "extra_attempt":
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                "SELECT 'duplicate',scope,grant_id,session,state,response,credential,created+1 "
                "FROM attempts WHERE id=?",
                (row["id"],),
            )
        elif damage == "member":
            member = db.execute("SELECT * FROM attempts WHERE grant_id='member-source'").fetchone()
            proof = json.loads(member["response"])
            proof["body"]["Resources"][0]["PhysicalResourceId"] = "foreign"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(proof), member["id"]))
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(response), row["id"]))
        original = db.execute(
            "SELECT a.*,g.kind,g.call,g.parent FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
            (row["id"],),
        ).fetchone()
        before = list(db.iterdump())
        assert phases.known_original_absence_read(db, original) is False
        assert list(db.iterdump()) == before
