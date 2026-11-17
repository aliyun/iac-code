"""Offline workflow provenance for actual OSS headers and exact negative reads."""

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.phases import (
    assert_cleanup_reads_complete,
    known_absence_read,
    known_original_absence_read,
)


def call(action, **params):
    return {"product": "oss", "version": "2019-05-17", "action": action, "region_id": "cn-hangzhou", "params": params}


def grants():
    bound = {"parent": "create", "response_path": ["headers", "location"], "target_param": "bucket"}
    return [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 1,
            "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
        },
        {
            "id": "precheck",
            "kind": "read",
            "quota": 1,
            "call": call("GetBucketInfo", bucket="offline-new-bucket"),
            "read_policy": {"phase": "execute"},
        },
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": call(
                "PutBucket",
                bucket="offline-new-bucket",
                **{
                    "x-oss-acl": "private",
                    "body": {"CreateBucketConfiguration": {"StorageClass": "Standard", "DataRedundancyType": "LRS"}},
                },
            ),
        },
        {"id": "delete", "kind": "delete", "quota": 1, "call": call("DeleteBucket", bucket="$created_id"), **bound},
        {
            "id": "absence",
            "kind": "read",
            "quota": 1,
            "call": call("GetBucketInfo", bucket="$created_id"),
            **bound,
            "read_policy": {"phase": "cleanup", "after_delete": "create", "required_cleanup": True},
        },
    ]


def negative_row():
    return {
        "kind": "read",
        "state": "unknown",
        "call": json.dumps(call("GetBucketInfo", bucket="offline-new-bucket")),
        "response": json.dumps(
            {
                "status": 404,
                "headers": {"x-oss-request-id": "offline-read"},
                "body": {"Code": "NoSuchBucket", "BucketName": "offline-new-bucket", "RequestId": "offline-read"},
            }
        ),
    }


def test_exact_negative_bucket_read_is_recognized_without_rewriting_original():
    row = negative_row()
    before = copy.deepcopy(row)
    assert known_absence_read(row) is True
    assert row == before and row["state"] == "unknown"


@pytest.mark.parametrize(
    "mutation", ["write", "action", "version", "params", "status", "code", "bucket", "id", "header"]
)
def test_other_oss_errors_or_unknown_writes_never_prove_absence(mutation):
    row = negative_row()
    request = json.loads(row["call"])
    response = json.loads(row["response"])
    if mutation == "write":
        row["kind"] = "create"
    elif mutation == "action":
        request["action"] = "DeleteBucket"
    elif mutation == "version":
        request["version"] = "other-version"
    elif mutation == "params":
        request["params"]["other"] = "unreviewed"
    elif mutation == "status":
        response["status"] = 403
    elif mutation == "code":
        response["body"]["Code"] = "AccessDenied"
    elif mutation == "bucket":
        response["body"]["BucketName"] = "other-bucket"
    elif mutation == "id":
        response["body"].pop("RequestId")
    else:
        response["headers"]["x-oss-request-id"] = "other-request"
    row.update(call=json.dumps(request), response=json.dumps(response))
    assert known_absence_read(row) is False


@pytest.fixture
def original(tmp_path, monkeypatch):
    path = tmp_path / "wire.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    entries = grants()
    gate.validate_grants(entries, require_read_policy=True)
    gate.install(path, "0", "oss-job:CREATE", "ws~full", "123", None, entries, authority="original-oss")
    signer = SimpleNamespace(mode="AK", access_key_id="offline", access_key_secret="offline-secret")
    gate.finish(
        gate.reserve(entries[0]["call"], "ws~full", signer, read_only=True),
        {"AccountId": "123", "RequestId": "offline-identity"},
        True,
    )
    gate.finish(
        gate.reserve(entries[1]["call"], "ws~full", signer, read_only=True),
        {
            "Code": "NoSuchBucket",
            "BucketName": "offline-new-bucket",
            "RequestId": "offline-read",
        },
        False,
        status=404,
        headers={"x-oss-request-id": "offline-read"},
    )
    gate.finish(
        gate.reserve(entries[2]["call"], "ws~full", signer, read_only=False),
        None,
        True,
        status=200,
        headers={"Location": "/offline-new-bucket", "x-oss-request-id": "offline-created"},
    )
    return path


def snapshot(path):
    with sqlite3.connect(path) as db:
        return {
            name: db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for name in ("active", "grants", "attempts", "identities", "authority_limits")
        }


def inspect(path):
    return inspect_candidate(
        path,
        slot="0",
        scope="oss-job:CREATE",
        session="ws~full",
        account="123",
        grants=grants(),
        authority="original-oss",
    )


def test_oss_recovery_uses_successful_original_location_and_preserves_wire(original):
    before = snapshot(original)
    value = inspect(original)
    assert value["owned_from_wire"][0]["identifier"] == "offline-new-bucket"
    assert value["activation_allowed"] is False and value["cleanup_confirmed"] is False
    targets = [
        entry["call"]["params"]["bucket"] for entry in value["remaining_calls"] if entry["id"] in {"delete", "absence"}
    ]
    assert targets == ["offline-new-bucket", "offline-new-bucket"]
    assert snapshot(original) == before


@pytest.fixture
def deleted_original(original):
    signer = SimpleNamespace(mode="AK", access_key_id="offline", access_key_secret="offline-secret")
    gate.cleanup(original, "0", "oss-job:CREATE", "ws~full")
    gate.finish(
        gate.reserve(call("DeleteBucket", bucket="offline-new-bucket"), "ws~full", signer, read_only=False),
        None,
        True,
        status=204,
        headers={"x-oss-request-id": "offline-delete", "Authorization": "secret"},
    )
    gate.finish(
        gate.reserve(call("GetBucketInfo", bucket="offline-new-bucket"), "ws~full", signer, read_only=True),
        {
            "Code": "NoSuchBucket",
            "BucketName": "offline-new-bucket",
            "RequestId": "offline-absent",
            "HostId": "offline-new-bucket.oss-cn-hangzhou.aliyuncs.com",
        },
        False,
        status=404,
        headers={"x-oss-request-id": "offline-absent"},
    )
    return original


def classify_bound(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        row = cursor.execute(
            "SELECT a.*,g.kind,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
            "WHERE g.id='absence'",
        ).fetchone()
        assert not known_absence_read(row)
        return known_original_absence_read(db, row)


def test_response_bound_oss_negative_keeps_journal_and_satisfies_original_read(deleted_original):
    before = snapshot(deleted_original)
    assert classify_bound(deleted_original)
    assert_cleanup_reads_complete(deleted_original, "oss-job:CREATE")
    assert snapshot(deleted_original) == before
    with sqlite3.connect(deleted_original) as db:
        body = json.loads(db.execute("SELECT response FROM attempts WHERE grant_id='delete'").fetchone()[0])
    assert body == {"body": None, "status": 204, "headers": {"x-oss-request-id": "offline-delete"}}


@pytest.mark.parametrize(
    "damage",
    [
        "parent_missing",
        "parent_unknown",
        "parent_duplicate",
        "parent_session",
        "parent_credential",
        "parent_authority",
        "parent_limit",
        "parent_response",
        "parent_late",
        "read_session",
        "read_credential",
        "read_limit",
        "read_counter",
        "path",
        "target",
        "encoding",
        "missing_identity",
        "identity_account",
        "identity_request",
        "identity_action",
        "identity_late",
        "host",
        "request",
        "bucket",
        "status",
    ],
)
def test_bound_oss_negative_provenance_damage_fails_closed(deleted_original, damage):
    path = deleted_original
    with sqlite3.connect(path) as db:
        if damage in {
            "parent_missing",
            "parent_unknown",
            "parent_session",
            "parent_credential",
            "parent_late",
            "read_session",
            "read_credential",
            "identity_late",
        }:
            field, value, grant_id = {
                "parent_missing": ("grant_id", "missing", "create"),
                "parent_unknown": ("state", "unknown", "create"),
                "parent_session": ("session", "other", "create"),
                "parent_credential": ("credential", "other", "create"),
                "parent_late": ("created", 9999999999, "create"),
                "read_session": ("session", "other", "absence"),
                "read_credential": ("credential", "other", "absence"),
                "identity_late": ("created", 9999999999, "identity"),
            }[damage]
            db.execute(f"UPDATE attempts SET {field}=? WHERE grant_id=?", (value, grant_id))
        elif damage == "parent_duplicate":
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                "SELECT 'duplicate',scope,grant_id,session,state,response,credential,created "
                "FROM attempts WHERE grant_id='create'"
            )
        elif damage in {"parent_limit", "read_limit"}:
            db.execute(
                "UPDATE authority_limits SET used=0 WHERE id=?", ("create" if damage == "parent_limit" else "absence",)
            )
        elif damage in {"parent_authority", "read_counter", "path", "target", "encoding"}:
            field, value = {
                "parent_authority": ("authority", "other"),
                "read_counter": ("used", 2),
                "path": ("response_path", '["bad"]'),
                "target": ("target_param", "bad"),
                "encoding": ("target_encoding", "json-singleton"),
            }[damage]
            db.execute(
                f"UPDATE grants SET {field}=? WHERE id=?",
                (value, "create" if damage == "parent_authority" else "absence"),
            )
        elif damage in {"missing_identity", "identity_account"}:
            db.execute(
                "DELETE FROM identities" if damage == "missing_identity" else "UPDATE identities SET account='other'"
            )
        elif damage == "identity_action":
            value = json.loads(db.execute("SELECT call FROM grants WHERE id='identity'").fetchone()[0])
            value["action"] = "other"
            db.execute("UPDATE grants SET call=? WHERE id='identity'", (json.dumps(value),))
        else:
            grant_id = {"parent_response": "create", "identity_request": "identity"}.get(damage, "absence")
            value = json.loads(db.execute("SELECT response FROM attempts WHERE grant_id=?", (grant_id,)).fetchone()[0])
            if damage == "parent_response":
                value["headers"]["Location"] = "/other-bucket"
            elif damage == "identity_request":
                value.pop("RequestId")
            elif damage == "status":
                value["status"] = 403
            else:
                value["body"][{"host": "HostId", "request": "RequestId", "bucket": "BucketName"}[damage]] = "other"
            db.execute("UPDATE attempts SET response=? WHERE grant_id=?", (json.dumps(value), grant_id))
    before = snapshot(path)
    assert not classify_bound(path)
    assert snapshot(path) == before


def test_negative_before_delete_never_satisfies_cleanup_completeness(deleted_original):
    with sqlite3.connect(deleted_original) as db:
        db.execute(
            "UPDATE attempts SET created=(SELECT created FROM attempts WHERE grant_id='create')+0.001 "
            "WHERE grant_id='absence'"
        )
    with pytest.raises(WorkflowError, match="required post-delete"):
        assert_cleanup_reads_complete(deleted_original, "oss-job:CREATE")


@pytest.mark.parametrize("review_damage", [None, "absent", "changed", "account", "authority"])
def test_rotated_signer_needs_real_same_account_reviewed_original_supplement(deleted_original, review_damage):
    path = deleted_original
    review_path = path.parent / "rotation.json"
    identity_call = {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}}
    review = {
        "kind": "ops_cleanup_identity_review",
        "approved": True,
        "scope": "oss-job:CREATE",
        "session": "ws~full",
        "account": "123",
        "authority": "original-oss",
        "additional_identity_reads": 1,
        "identity_call": identity_call,
    }
    json_write(review_path, review, private=True)
    with sqlite3.connect(path) as db:
        created = db.execute("SELECT created FROM attempts WHERE grant_id='create'").fetchone()[0]
        db.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,?,?,1,1,?)",
            ("oss-job:CREATE", "ops-cleanup-identity-offline", "identity", json.dumps(identity_call), "original-oss"),
        )
        db.execute(
            "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) VALUES(?,?,?,?,?,?,?,?)",
            (
                "rotated-identity",
                "oss-job:CREATE",
                "ops-cleanup-identity-offline",
                "ws~full",
                "succeeded",
                json.dumps({"AccountId": "123", "RequestId": "rotated-signed"}),
                "rotated",
                created + 0.00001,
            ),
        )
        db.execute("INSERT INTO identities VALUES(?,?,?,?)", ("oss-job:CREATE", "ws~full", "rotated", "123"))
        db.execute("UPDATE attempts SET credential='rotated' WHERE grant_id='absence'")
        if review_damage != "absent":
            db.execute(
                "INSERT INTO cleanup_identity_reviews VALUES(?,?,?,?,?,?,?)",
                (
                    "oss-job:CREATE",
                    "ops-cleanup-identity-offline",
                    "ws~full",
                    "123",
                    "original-oss",
                    str(review_path),
                    digest(review_path),
                ),
            )
        if review_damage == "account":
            db.execute("UPDATE identities SET account='other' WHERE credential='rotated'")
        elif review_damage == "authority":
            db.execute("UPDATE grants SET authority='other' WHERE id='ops-cleanup-identity-offline'")
    if review_damage == "changed":
        json_write(review_path, {**review, "approved": False}, private=True)
    before = snapshot(path)
    assert classify_bound(path) is (review_damage is None)
    assert snapshot(path) == before


@pytest.mark.parametrize(
    "body",
    [
        {"buckets": [], "owner": {"id": "123"}},
        {"buckets": [], "owner": {"id": "123"}, "is_truncated": False},
        {"buckets": [{"name": "offline-new-bucket"}], "is_truncated": True, "next_marker": "cursor"},
    ],
)
def test_list_wire_persists_actual_transport_without_defaulting_pagination(tmp_path, body):
    path = tmp_path / "list.sqlite3"
    gate.initialize(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO grants(scope,id,kind,call,quota) VALUES(?,?,?,?,1)",
            ("offline:QUERY", "list", "read", json.dumps(call("ListBuckets", prefix="offline-new", **{"max-keys": 1}))),
        )
        db.execute(
            "INSERT INTO attempts(id,scope,grant_id,state) VALUES(?,?,?,'unknown')",
            ("offline-list", "offline:QUERY", "list"),
        )
    gate.finish(
        (path, "offline-list"),
        body,
        True,
        status=200,
        headers={"x-oss-request-id": "offline-list-id", "Set-Cookie": "secret"},
    )
    with sqlite3.connect(path) as db:
        value = json.loads(db.execute("SELECT response FROM attempts").fetchone()[0])
    assert value == {"status": 200, "headers": {"x-oss-request-id": "offline-list-id"}, "body": body}


@pytest.mark.parametrize("mutation", ["status", "missing_location", "other_bucket", "request_id", "invented_body"])
def test_oss_recovery_rejects_invalid_original_creation_response(original, mutation):
    with sqlite3.connect(original) as db:
        ident, text = db.execute("SELECT id,response FROM attempts WHERE grant_id='create'").fetchone()
        response = json.loads(text)
        if mutation == "status":
            response["status"] = 403
        elif mutation == "missing_location":
            response["headers"].pop("Location")
        elif mutation == "other_bucket":
            response["headers"]["Location"] = "/other-bucket"
        elif mutation == "request_id":
            response["headers"].pop("x-oss-request-id")
        else:
            response["body"] = {"BucketName": "offline-new-bucket"}
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(response), ident))
    before = snapshot(original)
    with pytest.raises(WorkflowError):
        inspect(original)
    assert snapshot(original) == before


@pytest.mark.parametrize("mutation", ["cross_session", "unsigned_read", "unknown_write", "orphan"])
def test_oss_negative_read_never_bypasses_original_provenance(original, mutation):
    with sqlite3.connect(original) as db:
        if mutation == "cross_session":
            db.execute("UPDATE attempts SET session='other' WHERE grant_id='precheck'")
        elif mutation == "unsigned_read":
            db.execute("UPDATE attempts SET credential='other-signer' WHERE grant_id='precheck'")
        elif mutation == "unknown_write":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='create'")
        else:
            db.execute("UPDATE attempts SET grant_id='orphan' WHERE grant_id='precheck'")
    before = snapshot(original)
    with pytest.raises(WorkflowError):
        inspect(original)
    assert snapshot(original) == before
