"""Cloud-free inspection of expired original capabilities; no activation or fabricated cleanup."""

import copy
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError


@pytest.fixture
def original(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    entries = [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 2,
            "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
        },
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": {
                "product": "vpc",
                "action": "CreateVpc",
                "region_id": "cn-hangzhou",
                "params": {"Name": "offline"},
            },
        },
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "response_path": ["VpcId"],
            "target_param": "VpcId",
            "call": {
                "product": "vpc",
                "action": "DeleteVpc",
                "region_id": "cn-hangzhou",
                "params": {"VpcId": "$created_id"},
            },
        },
        {
            "id": "read-owned",
            "kind": "read",
            "quota": 3,
            "parent": "create",
            "response_path": ["VpcId"],
            "target_param": "VpcId",
            "call": {
                "product": "vpc",
                "action": "DescribeVpcs",
                "region_id": "cn-hangzhou",
                "params": {"VpcId": "$created_id", "PageSize": 10},
            },
        },
    ]
    gate.install(
        path,
        "0",
        "original-job:CREATE",
        "ws~original-full",
        "123",
        time.time() + 1000,
        entries,
        authority="unchanged-authority:CREATE",
    )
    signer = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    ticket = gate.reserve(entries[0]["call"], "ws~original-full", signer, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    ticket = gate.reserve(entries[1]["call"], "ws~original-full", signer, read_only=False)
    gate.finish(ticket, {"VpcId": "vpc-original-response"}, True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE active SET expires=0")
    return (
        path,
        {
            "slot": "0",
            "scope": "original-job:CREATE",
            "session": "ws~original-full",
            "account": "123",
            "grants": entries,
            "authority": "unchanged-authority:CREATE",
        },
        signer,
    )


def rows(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("active", "grants", "attempts", "identities", "authority_limits")
        }


def test_inspection_derives_only_original_ids_and_never_activates_or_changes_counters(original):
    path, args, signer = original
    before = rows(path)
    value = inspect_candidate(path, **args)
    assert rows(path) == before
    assert value["candidate_only"] and value["original_capability_expired"]
    assert not value["activation_allowed"] and not value["cleanup_confirmed"] and not value["cloud_call_performed"]
    assert not value["current_signer_identity_verified"]
    assert value["owned_from_wire"][0]["identifier"] == "vpc-original-response"
    assert all(call["kind"] != "create" for call in value["remaining_calls"])
    delete = next(call for call in value["remaining_calls"] if call["kind"] == "delete")
    assert delete["call"]["params"]["VpcId"] == "vpc-original-response"
    assert delete["remaining"] == 1
    text = json.dumps(value)
    assert "fake-secret" not in text and "credential" not in text
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(delete["call"], args["session"], signer, read_only=False)


@pytest.mark.parametrize(
    "key,value",
    [
        ("slot", "1"),
        ("scope", "other-job:CREATE"),
        ("session", "other"),
        ("account", "456"),
        ("authority", "new-attempt:CREATE"),
    ],
)
def test_inspection_rejects_rebound_original_context_without_writes(original, key, value):
    path, args, _ = original
    before = rows(path)
    args[key] = value
    with pytest.raises(WorkflowError):
        inspect_candidate(path, **args)
    assert rows(path) == before


@pytest.mark.parametrize("mutation", ["name", "quota", "missing_grant", "added_grant"])
def test_inspection_rejects_changed_original_plan(original, mutation):
    path, args, _ = original
    entries = copy.deepcopy(args["grants"])
    if mutation == "name":
        entries[1]["call"]["params"]["Name"] = "new-name"
    elif mutation == "quota":
        entries[0]["quota"] += 1
    elif mutation == "missing_grant":
        entries.pop()
    else:
        entries.append({**entries[0], "id": "new-identity"})
    args["grants"] = entries
    before = rows(path)
    with pytest.raises(WorkflowError):
        inspect_candidate(path, **args)
    assert rows(path) == before


@pytest.mark.parametrize("mutation", ["unknown", "missing_id", "wrong_identity", "counter", "no_create", "session"])
def test_inspection_fails_closed_on_ambiguous_or_unverified_wire_provenance(original, mutation):
    path, args, _ = original
    with closing(sqlite3.connect(path)) as connection, connection:
        if mutation == "unknown":
            connection.execute("UPDATE attempts SET state='unknown' WHERE grant_id='create'")
        elif mutation == "missing_id":
            connection.execute("UPDATE attempts SET response='{}' WHERE grant_id='create'")
        elif mutation == "wrong_identity":
            connection.execute("UPDATE identities SET account='456'")
        elif mutation == "counter":
            connection.execute("UPDATE authority_limits SET used=0 WHERE id='create'")
        elif mutation == "no_create":
            connection.execute("DELETE FROM attempts WHERE grant_id='create'")
        else:
            connection.execute("UPDATE attempts SET session='other' WHERE grant_id='create'")
    before = rows(path)
    with pytest.raises(WorkflowError):
        inspect_candidate(path, **args)
    assert rows(path) == before


def test_inspection_preserves_exhausted_delete_without_replaying_or_claiming_absence(original):
    path, args, _ = original
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE grants SET used=1 WHERE id='delete'")
        connection.execute("UPDATE authority_limits SET used=1 WHERE id='delete'")
    before = rows(path)
    value = inspect_candidate(path, **args)
    assert rows(path) == before
    assert next(call for call in value["remaining_calls"] if call["kind"] == "delete")["remaining"] == 0
    assert value["cleanup_confirmed"] is False


def test_inspection_does_not_create_missing_database(tmp_path, original):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(WorkflowError):
        inspect_candidate(missing, **original[1])
    assert not missing.exists()


def rejected_oss_detail(original):
    """Add a real-shaped, signed read rejection to an isolated fake journal."""
    path, args, signer = original
    entry = {
        "id": "detail",
        "kind": "read",
        "quota": 1,
        "read_policy": {"phase": "execute"},
        "call": {
            "product": "oss",
            "action": "GetBucketInfo",
            "region_id": "cn-hangzhou",
            "version": "2019-05-17",
            "params": {"bucket": "offline-bucket"},
        },
    }
    args["grants"].append(entry)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE active SET expires=NULL")
        connection.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,authority,read_policy) VALUES(?,?,?,?,?,?,?,?)",
            (
                args["scope"],
                entry["id"],
                entry["kind"],
                json.dumps(entry["call"]),
                1,
                0,
                args["authority"],
                json.dumps(entry["read_policy"]),
            ),
        )
        connection.execute("INSERT INTO authority_limits VALUES(?,?,?)", (args["authority"], "detail", 0))
    ticket = gate.reserve(entry["call"], args["session"], signer, read_only=True)
    gate.finish(
        ticket,
        {
            "Code": "SignatureDoesNotMatch",
            "EC": "0002-00000201",
            "RequestId": "offline-rejected-read",
            "HostId": "offline-bucket.oss-cn-hangzhou.aliyuncs.com",
            "CanonicalRequest": "private diagnostic must not be exported",
        },
        False,
        status=403,
        headers={"x-oss-request-id": "offline-rejected-read"},
    )
    return path, args, signer


def test_definite_signature_read_rejection_does_not_block_original_cleanup_or_imply_absence(original):
    from scripts.web.e2e.resource_link_workflow.phases import known_original_absence_read

    path, args, _ = rejected_oss_detail(original)
    before = rows(path)
    result = inspect_candidate(path, **args)
    assert rows(path) == before
    assert not result["cleanup_confirmed"] and not result["activation_allowed"]
    assert next(r for r in result["remaining_calls"] if r["kind"] == "delete")["remaining"] == 1
    assert len(result["rejected_detail_reads"]) == 1
    rejected = result["rejected_detail_reads"][0]
    assert rejected["code"] == "SignatureDoesNotMatch" and rejected["absence_verified"] is False
    assert rejected["original_state_preserved"]
    assert "private diagnostic" not in json.dumps(result)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT a.*,g.kind,g.call FROM attempts a JOIN grants g "
            "ON a.scope=g.scope AND a.grant_id=g.id WHERE g.id='detail'"
        ).fetchone()
        assert row["state"] == "unknown"
        assert not known_original_absence_read(connection, row)


@pytest.mark.parametrize("damage", [None, "missing_delete", "missing_source", "missing_cleanup", "wrong_error"])
def test_previous_dimension_can_advance_only_after_real_cleanup_despite_old_signature_rejection(original, damage):
    from scripts.web.e2e.resource_link_workflow.files import json_write
    from scripts.web.e2e.resource_link_workflow.runtime import Runtime

    path, args, signer = rejected_oss_detail(original)
    entry = next(entry for entry in args["grants"] if entry["id"] == "read-owned")
    policy = {"phase": "cleanup", "required_cleanup": True, "after_delete": "create"}
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE grants SET read_policy=? WHERE id='read-owned'", (json.dumps(policy),))
    if damage != "missing_delete":
        gate.cleanup(path, args["slot"], args["scope"], args["session"])
        deletion = copy.deepcopy(next(entry for entry in args["grants"] if entry["kind"] == "delete")["call"])
        deletion["params"]["VpcId"] = "vpc-original-response"
        gate.finish(gate.reserve(deletion, args["session"], signer, read_only=False), {"RequestId": "deleted"}, True)
        if damage != "missing_cleanup":
            call = copy.deepcopy(entry["call"])
            call["params"]["VpcId"] = "vpc-original-response"
            gate.finish(
                gate.reserve(call, args["session"], signer, read_only=True),
                {"Vpcs": [], "TotalCount": 0},
                True,
            )
    if damage == "wrong_error":
        with closing(sqlite3.connect(path)) as connection, connection:
            response = json.loads(
                connection.execute("SELECT response FROM attempts WHERE grant_id='detail'").fetchone()[0]
            )
            response["body"]["Code"] = "AccessDenied"
            connection.execute("UPDATE attempts SET response=? WHERE grant_id='detail'", (json.dumps(response),))
    root = path.parent
    path.rename(root / "execution-gate.sqlite3")
    if damage != "missing_source":
        json_write(root / "jobs/original-job/output/CREATE-cleanup-source.json", {"offline": True})
    runtime = Runtime(SimpleNamespace(root=root))
    before = rows(root / "execution-gate.sqlite3")
    if damage is None:
        runtime.assert_previous_cleanup("original-job", "ROS")
    else:
        with pytest.raises(WorkflowError):
            runtime.assert_previous_cleanup("original-job", "ROS")
    assert rows(root / "execution-gate.sqlite3") == before


@pytest.mark.parametrize(
    "damage",
    [
        "status",
        "boolean_status",
        "code",
        "ec",
        "host",
        "request_id",
        "header",
        "extra_param",
        "action",
        "version",
        "region",
        "kind",
        "policy",
        "required",
        "credential",
        "identity",
        "session",
        "counter",
        "count",
        "missing_response",
    ],
)
def test_rejected_detail_inspection_refuses_ambiguous_errors_writes_or_provenance_drift(original, damage):
    path, args, _ = rejected_oss_detail(original)
    with closing(sqlite3.connect(path)) as connection, connection:
        response = json.loads(connection.execute("SELECT response FROM attempts WHERE grant_id='detail'").fetchone()[0])
        if damage in {"status", "boolean_status", "code", "ec", "host", "request_id", "header", "missing_response"}:
            if damage == "status":
                response["status"] = 500
            elif damage == "boolean_status":
                response["status"] = True
            elif damage == "header":
                response["headers"]["x-oss-request-id"] = "wrong"
            elif damage == "missing_response":
                response = {}
            else:
                key = {"code": "Code", "ec": "EC", "host": "HostId", "request_id": "RequestId"}[damage]
                response["body"][key] = "wrong"
            connection.execute("UPDATE attempts SET response=? WHERE grant_id='detail'", (json.dumps(response),))
        elif damage in {"extra_param", "action", "version", "region"}:
            call = args["grants"][-1]["call"]
            if damage == "extra_param":
                call["params"]["other"] = "unreviewed"
            else:
                call[{"action": "action", "version": "version", "region": "region_id"}[damage]] = "wrong"
            connection.execute("UPDATE grants SET call=? WHERE id='detail'", (json.dumps(call),))
        elif damage in {"policy", "required"}:
            policy = {"phase": "cleanup", "required_cleanup": True, "after_delete": "create"}
            if damage == "required":
                policy["phase"] = "execute"
            args["grants"][-1]["read_policy"] = policy
            connection.execute("UPDATE grants SET read_policy=? WHERE id='detail'", (json.dumps(policy),))
        elif damage == "kind":
            args["grants"][-1]["kind"] = "create"
            connection.execute("UPDATE grants SET kind='create' WHERE id='detail'")
        elif damage == "identity":
            connection.execute("UPDATE identities SET account='wrong'")
        elif damage == "counter":
            connection.execute("UPDATE authority_limits SET used=0 WHERE id='detail'")
        elif damage == "count":
            connection.execute("UPDATE grants SET used=0 WHERE id='detail'")
        else:
            connection.execute(f"UPDATE attempts SET {damage}='wrong' WHERE grant_id='detail'")
    before = rows(path)
    with pytest.raises(WorkflowError):
        inspect_candidate(path, **args)
    assert rows(path) == before


def test_signature_rejection_allows_only_cleanup_reads_after_original_successful_delete(original):
    from scripts.web.e2e.resource_link_workflow.phases import readonly_continuation

    path, args, signer = rejected_oss_detail(original)
    before = rows(path)
    with pytest.raises(WorkflowError, match="unresolved original"):
        readonly_continuation(path, args["slot"], args["scope"], args["session"], "source")
    assert rows(path) == before
    gate.cleanup(path, args["slot"], args["scope"], args["session"])
    with pytest.raises(WorkflowError, match="writes must already"):
        readonly_continuation(path, args["slot"], args["scope"], args["session"], "cleanup")
    delete_call = {**args["grants"][2]["call"], "params": {"VpcId": "vpc-original-response"}}
    ticket = gate.reserve(delete_call, args["session"], signer, read_only=False)
    gate.finish(ticket, {"RequestId": "offline-cleanup"}, True)
    before = rows(path)
    request = readonly_continuation(path, args["slot"], args["scope"], args["session"], "cleanup")
    assert "禁止再次创建、删除" in request
    assert rows(path) == before


def deleted_original(original):
    path, args, signer = original
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE active SET expires=NULL")  # Isolated offline fixture only.
    gate.cleanup(path, args["slot"], args["scope"], args["session"])
    call = {**args["grants"][2]["call"], "params": {"VpcId": "vpc-original-response"}}
    ticket = gate.reserve(call, args["session"], signer, read_only=False)
    gate.finish(ticket, {"RequestId": "offline-delete-request"}, True)
    return path, args


def test_readonly_review_and_request_keep_original_scope_counters_and_successful_delete(original):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import exact_readonly_request, readonly_review_fields
    from scripts.web.e2e.resource_link_workflow.files import digest, json_write

    path, args = deleted_original(original)
    before = rows(path)
    candidate = inspect_candidate(path, **args)
    fields = readonly_review_fields(candidate)
    assert len(fields["successful_deletions"]) == 1
    assert fields["successful_deletions"][0]["parent"] == "create"
    assert fields["cleanup_read_calls"][0]["call"]["params"]["VpcId"] == "vpc-original-response"
    assert fields["cleanup_read_calls"][0]["remaining"] == 3
    root = path.parent
    # Same original private database, not a new scope or copied allowance.
    path.rename(root / "execution-gate.sqlite3")
    review = {
        "kind": "ops_cleanup_readonly_review",
        "approved": True,
        "original_job_id": "original-job",
        "dimension": "CREATE",
        **{
            k: candidate[k]
            for k in (
                "scope",
                "authority",
                "account",
                "session_id",
                "owned_from_wire",
            )
        },
        **fields,
    }
    json_write(root / "inputs/readonly.json", review)
    recovery = {
        "original_job_id": "original-job",
        "dimension": "CREATE",
        "read_only": True,
        "authority": args["authority"],
        "evidence": "inputs/readonly.json",
        "evidence_sha256": digest(root / "inputs/readonly.json"),
    }
    text = exact_readonly_request(root, recovery, args["slot"], args["session"], args["grants"])
    assert "DescribeVpcs" in text and "DeleteVpc" not in text and "CreateVpc" not in text
    assert rows(root / "execution-gate.sqlite3") == before


@pytest.mark.parametrize(
    "damage", ["unused", "unknown", "missing", "counter_only", "identity", "early", "phase", "reads_exhausted"]
)
def test_readonly_cleanup_fails_closed_without_original_successful_delete_and_remaining_reads(original, damage):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import readonly_review_fields

    path, args, _ = original
    if damage != "unused":
        path, args = deleted_original(original)
    with closing(sqlite3.connect(path)) as connection, connection:
        if damage == "unknown":
            connection.execute("UPDATE attempts SET state='unknown' WHERE grant_id='delete'")
        elif damage in {"missing", "counter_only"}:
            connection.execute("DELETE FROM attempts WHERE grant_id='delete'")
        elif damage == "identity":
            connection.execute("UPDATE attempts SET credential='unverified' WHERE grant_id='delete'")
        elif damage == "early":
            connection.execute("UPDATE attempts SET created=0 WHERE grant_id='delete'")
        elif damage == "phase":
            connection.execute("UPDATE active SET phase='execute'")
        elif damage == "reads_exhausted":
            connection.execute("UPDATE grants SET used=quota WHERE id='read-owned'")
            connection.execute("UPDATE authority_limits SET used=3 WHERE id='read-owned'")
    before = rows(path)
    with pytest.raises(WorkflowError):
        readonly_review_fields(inspect_candidate(path, **args))
    assert rows(path) == before


@pytest.mark.parametrize("damage", ["review_sha", "session", "read_target", "delete_proof", "account", "quota"])
def test_readonly_request_revalidates_frozen_review_before_submission(original, damage):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import exact_readonly_request, readonly_review_fields
    from scripts.web.e2e.resource_link_workflow.files import digest, json_write

    path, args = deleted_original(original)
    candidate = inspect_candidate(path, **args)
    review = {
        "kind": "ops_cleanup_readonly_review",
        "approved": True,
        "original_job_id": "original-job",
        "dimension": "CREATE",
        **{
            k: candidate[k]
            for k in (
                "scope",
                "authority",
                "account",
                "session_id",
                "owned_from_wire",
            )
        },
        **readonly_review_fields(candidate),
    }
    root = path.parent
    path.rename(root / "execution-gate.sqlite3")
    if damage == "session":
        review["session_id"] = "another-session"
    elif damage == "read_target":
        review["cleanup_read_calls"][0]["call"]["params"]["VpcId"] = "existing-vpc"
    elif damage == "delete_proof":
        review["successful_deletions"][0]["response_sha256"] = "changed"
    elif damage == "account":
        review["account"] = "456"
    elif damage == "quota":
        review["cleanup_read_calls"][0]["remaining"] += 1
    json_write(root / "inputs/readonly.json", review)
    recovery = {
        "original_job_id": "original-job",
        "dimension": "CREATE",
        "read_only": True,
        "authority": args["authority"],
        "evidence": "inputs/readonly.json",
        "evidence_sha256": digest(root / "inputs/readonly.json"),
    }
    if damage == "review_sha":
        json_write(root / "inputs/readonly.json", {**review, "changed": True})
    before = rows(root / "execution-gate.sqlite3")
    with pytest.raises(WorkflowError):
        exact_readonly_request(root, recovery, args["slot"], args["session"], args["grants"])
    assert rows(root / "execution-gate.sqlite3") == before
