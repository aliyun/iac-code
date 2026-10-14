"""Original response-bound RAM negatives; all identities/responses are offline."""

import copy
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate
from scripts.web.e2e.resource_link_workflow.execution_gate import initialize
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, json_write
from scripts.web.e2e.resource_link_workflow.phases import (
    assert_cleanup_reads_complete,
    known_absence_read,
    known_original_absence_read,
    readonly_continuation,
)
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store


def negative(action, envelope):
    role = action == "GetRole"
    name = "offline-role" if role else "offline-policy"
    body = {
        "RequestId": "offline-request",
        "HostId": "ram.aliyuncs.com",
        "Code": "EntityNotExist.Role" if role else "EntityNotExist.Policy",
        "Message": ("The role not exists:" if role else "The policy does not exist: ") + name,
    }
    assert envelope
    return {
        "call": json.dumps(
            {
                "product": "ram",
                "version": "2015-05-01",
                "action": action,
                "region_id": "cn-hangzhou",
                "params": ({"RoleName": name} if role else {"PolicyName": name, "PolicyType": "Custom"}),
            }
        ),
        "response": json.dumps({"status": 404, "headers": {"x-acs-request-id": body["RequestId"]}, "body": body}),
    }


@pytest.fixture(params=["GetPolicy", "GetRole"])
def bound_wire(tmp_path, request):
    action = request.param
    role = action == "GetRole"
    name = "offline-role" if role else "offline-policy"
    key = "RoleName" if role else "PolicyName"
    item = "Role" if role else "Policy"
    path = tmp_path / "wire.sqlite3"
    initialize(path)
    call = json.loads(negative(action, True)["call"])
    call["params"][key] = "$created_id"
    create = {
        **call,
        "action": "CreateRole" if role else "CreatePolicy",
        "params": {
            key: name,
            "AssumeRolePolicyDocument" if role else "PolicyDocument": json.dumps(
                {
                    "Version": "1",
                    "Statement": [{"Effect": "Deny", "Action": ["ecs:DescribeInstances"], "Resource": ["*"]}],
                }
            ),
        },
    }
    delete = {**call, "action": "DeleteRole" if role else "DeletePolicy", "params": {key: "$created_id"}}
    identity = {
        "product": "sts",
        "version": "2015-04-01",
        "action": "GetCallerIdentity",
        "region_id": "cn-hangzhou",
        "params": {},
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
            "response_path": [item, key],
            "target_param": key,
        },
        {
            "id": "cleanup",
            "kind": "read",
            "quota": 3,
            "call": call,
            "parent": "create",
            "response_path": [item, key],
            "target_param": key,
            "read_policy": {"phase": "cleanup", "required_cleanup": True, "after_delete": "create"},
        },
    ]
    bodies = [
        {"AccountId": "123456789", "RequestId": "signed-identity"},
        {item: {key: name, **({"PolicyType": "Custom"} if not role else {})}, "RequestId": "original-create"},
        {"RequestId": "original-delete"},
        json.loads(negative(action, True)["response"]),
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO active VALUES(?,?,?,?,?,?)", ("2", "offline:CREATE", "ws~full", "123456789", None, "cleanup")
        )
        db.execute(
            "INSERT INTO identities VALUES(?,?,?,?)", ("offline:CREATE", "ws~full", "fake-credential", "123456789")
        )
        for index, (grant, body) in enumerate(zip(grants, bodies)):
            db.execute(
                "INSERT INTO grants(scope,id,kind,call,quota,used,parent,response_path,target_param,"
                "authority,read_policy) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "offline:CREATE",
                    grant["id"],
                    grant["kind"],
                    json.dumps(grant["call"]),
                    grant["quota"],
                    1,
                    grant.get("parent"),
                    json.dumps(grant.get("response_path")),
                    grant.get("target_param"),
                    "original:CREATE",
                    json.dumps(grant.get("read_policy")),
                ),
            )
            db.execute("INSERT INTO authority_limits VALUES(?,?,?)", ("original:CREATE", grant["id"], 1))
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    grant["id"] + "-attempt",
                    "offline:CREATE",
                    grant["id"],
                    "ws~full",
                    "unknown" if grant["id"] == "cleanup" else "succeeded",
                    json.dumps(body),
                    "fake-credential",
                    index + 1,
                ),
            )
    return path, grants, key


def classify(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT a.*,g.kind,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
            "WHERE a.id='cleanup-attempt'",
        ).fetchone()
        assert not known_absence_read(row)  # A placeholder alone never proves a name.
        return known_original_absence_read(db, row)


def test_original_ram_response_projection_preserves_all_bytes_counters_and_errors(bound_wire):
    path, grants, key = bound_wire
    before = path.read_bytes()
    assert classify(path)
    assert_cleanup_reads_complete(path, "offline:CREATE")
    assert "禁止再次创建、删除" in readonly_continuation(path, "2", "offline:CREATE", "ws~full", "cleanup")
    candidate = inspect_candidate(
        path,
        slot="2",
        scope="offline:CREATE",
        session="ws~full",
        account="123456789",
        grants=grants,
        authority="original:CREATE",
    )
    assert candidate["cleanup_confirmed"] is False
    assert candidate["cloud_call_performed"] is False
    assert candidate["remaining_calls"][-1]["call"]["params"][key].startswith("offline-")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "parent_missing",
        "parent_unknown",
        "parent_duplicate",
        "parent_session",
        "parent_credential",
        "parent_late",
        "parent_scope",
        "parent_kind",
        "parent_authority",
        "parent_unused",
        "parent_limit",
        "name_drift",
        "response_name",
        "response_path",
        "target_param",
        "encoding",
        "region",
        "version",
        "missing_identity",
        "identity_account",
        "identity_response",
        "identity_request",
        "identity_action",
        "identity_late",
        "read_scope",
        "read_authority",
        "read_counter",
        "read_limit",
        "read_session",
        "read_credential",
        "header",
        "wrong_message",
        "missing_create_request",
        "system_policy",
    ],
)
def test_response_binding_provenance_or_negative_damage_fails_closed(bound_wire, damage):
    path, _, key = bound_wire
    with sqlite3.connect(path) as db:
        if damage.startswith("parent_") and damage not in {"parent_duplicate", "parent_limit"}:
            field, value = {
                "parent_missing": ("grant_id", "missing"),
                "parent_unknown": ("state", "unknown"),
                "parent_session": ("session", "other"),
                "parent_credential": ("credential", "other"),
                "parent_late": ("created", 10),
                "parent_scope": ("scope", "other"),
                "parent_kind": ("kind", "read"),
                "parent_authority": ("authority", "other"),
                "parent_unused": ("used", 0),
            }[damage]
            table = "grants" if field in {"kind", "authority", "used"} else "attempts"
            db.execute(
                f"UPDATE {table} SET {field}=? WHERE "
                + ("id='create'" if table == "grants" else "id='create-attempt'"),
                (value,),
            )
        elif damage == "parent_duplicate":
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                "SELECT 'duplicate',scope,grant_id,session,state,response,credential,created FROM attempts "
                "WHERE id='create-attempt'",
            )
        elif damage in {"parent_limit", "read_limit"}:
            db.execute(
                "UPDATE authority_limits SET used=0 WHERE id=?", ("create" if damage == "parent_limit" else "cleanup",)
            )
        elif damage in {"name_drift", "region", "version", "identity_action"}:
            grant_id = "identity" if damage == "identity_action" else "create"
            call = json.loads(db.execute("SELECT call FROM grants WHERE id=?", (grant_id,)).fetchone()[0])
            if damage == "name_drift":
                call["params"][key] = "other-name"
            else:
                call[{"region": "region_id", "version": "version", "identity_action": "action"}[damage]] = "other"
            db.execute("UPDATE grants SET call=? WHERE id=?", (json.dumps(call), grant_id))
        elif damage in {"response_path", "target_param", "encoding", "read_authority", "read_counter"}:
            field, value = {
                "response_path": ("response_path", '["other"]'),
                "target_param": ("target_param", "other"),
                "encoding": ("target_encoding", "json-singleton"),
                "read_authority": ("authority", "other"),
                "read_counter": ("used", 2),
            }[damage]
            db.execute(f"UPDATE grants SET {field}=? WHERE id='cleanup'", (value,))
        elif damage in {"missing_identity", "identity_account"}:
            db.execute(
                "DELETE FROM identities" if damage == "missing_identity" else "UPDATE identities SET account='other'"
            )
        elif damage in {"identity_late", "read_scope", "read_session", "read_credential"}:
            field, value = {
                "identity_late": ("created", 10),
                "read_scope": ("scope", "other"),
                "read_session": ("session", "other"),
                "read_credential": ("credential", "other"),
            }[damage]
            if damage == "read_scope":
                # Keep a joined row but no parent/identity in this different scope.
                db.execute("UPDATE grants SET scope='other' WHERE id='cleanup'")
            db.execute(
                f"UPDATE attempts SET {field}=? WHERE id=?",
                (value, "identity-attempt" if damage == "identity_late" else "cleanup-attempt"),
            )
        else:
            ident = (
                "identity"
                if damage.startswith("identity_")
                else "create"
                if damage in {"response_name", "missing_create_request", "system_policy"}
                else "cleanup"
            ) + "-attempt"
            body = json.loads(db.execute("SELECT response FROM attempts WHERE id=?", (ident,)).fetchone()[0])
            if damage == "identity_response":
                body["AccountId"] = "other"
            elif damage in {"identity_request", "missing_create_request"}:
                body.pop("RequestId")
            elif damage == "response_name":
                body["Role" if key == "RoleName" else "Policy"][key] = "other"
            elif damage == "system_policy":
                if key == "RoleName":
                    body["Role"][key] = "other"  # Role is not a policy; still malformed proof.
                else:
                    body["Policy"]["PolicyType"] = "System"
            elif damage == "header":
                body["headers"]["x-acs-request-id"] = "different"
            else:
                body["body"]["Message"] += "other"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(body), ident))
    before = path.read_bytes()
    assert not classify(path)
    assert path.read_bytes() == before


def test_exact_absence_is_not_automatically_post_delete_absence(bound_wire):
    path, _, _ = bound_wire
    with sqlite3.connect(path) as db:
        db.execute("UPDATE attempts SET created=2.5 WHERE id='cleanup-attempt'")
    assert classify(path)  # Valid exact negative read, but before the successful delete.
    with pytest.raises(WorkflowError, match="post-delete"):
        assert_cleanup_reads_complete(path, "offline:CREATE")


def test_projected_ram_read_requires_persisted_original_not_a_forged_row(bound_wire):
    path, _, _ = bound_wire
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(
            db.execute(
                "SELECT a.*,g.kind,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.id='cleanup-attempt'",
            ).fetchone()
        )
        original = copy.deepcopy(row)
        row["response"] = row["response"].replace("offline-request", "different-request")
        assert not known_original_absence_read(db, row)
        assert known_original_absence_read(db, original)


def previous_dimension_runtime(bound_wire):
    """The production transition must use the same audited binding as exports."""
    path, _, _ = bound_wire
    root = path.parent
    path = path.rename(root / "execution-gate.sqlite3")
    runtime = Runtime(Store.initialize(root))
    json_write(root / "jobs/offline/output/CREATE-cleanup-source.json", {"offline": True})
    return path, runtime


def test_previous_dimension_accepts_bound_ram_negative_without_mutating_journal(bound_wire):
    path, runtime = previous_dimension_runtime(bound_wire)
    before = path.read_bytes()
    runtime.assert_previous_cleanup("offline", "QUERY")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing_source",
        "before_delete",
        "no_delete",
        "unknown_delete",
        "wrong_message",
        "credential_drift",
        "orphan_attempt",
    ],
)
def test_previous_dimension_still_rejects_unproven_ram_cleanup(bound_wire, damage):
    path, runtime = previous_dimension_runtime(bound_wire)
    if damage == "missing_source":
        (path.parent / "jobs/offline/output/CREATE-cleanup-source.json").unlink()
    else:
        with sqlite3.connect(path) as db:
            if damage == "before_delete":
                db.execute("UPDATE attempts SET created=2.5 WHERE id='cleanup-attempt'")
            elif damage == "no_delete":
                db.execute("DELETE FROM attempts WHERE id='delete-attempt'")
            elif damage == "unknown_delete":
                db.execute("UPDATE attempts SET state='unknown' WHERE id='delete-attempt'")
            elif damage == "credential_drift":
                db.execute("UPDATE attempts SET credential='other' WHERE id='cleanup-attempt'")
            elif damage == "orphan_attempt":
                db.execute(
                    "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                    "VALUES('orphan','offline:CREATE','unreviewed','ws~full','unknown','{}','fake-credential',5)",
                )
            else:
                body = json.loads(db.execute("SELECT response FROM attempts WHERE id='cleanup-attempt'").fetchone()[0])
                body["body"]["Message"] += "other"
                db.execute("UPDATE attempts SET response=? WHERE id='cleanup-attempt'", (json.dumps(body),))
    before = path.read_bytes()
    with pytest.raises(WorkflowError):
        runtime.assert_previous_cleanup("offline", "QUERY")
    assert path.read_bytes() == before
