"""Offline phase-isolated reads, pacing and complete cleanup regression coverage."""

import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.creation import validate_execution
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.phases import assert_cleanup_reads_complete, readonly_continuation


def call(action, **params):
    return {"product": "ros", "action": action, "region_id": "cn-hangzhou", "params": params}


def entries():
    bound = {"parent": "create", "response_path": ["StackId"], "target_param": "StackId"}
    return [
        {"id": "identity", "kind": "identity", "quota": 2, "call": {**call("GetCallerIdentity"), "product": "sts"}},
        {"id": "create", "kind": "create", "quota": 1, "call": call("CreateStack", StackName="offline")},
        {"id": "delete", "kind": "delete", "quota": 1, "call": call("DeleteStack", StackId="$created_id"), **bound},
        {
            "id": "status-source",
            "kind": "read",
            "quota": 3,
            "call": call("GetStack", StackId="$created_id"),
            **bound,
            "read_policy": {"phase": "execute", "min_interval_seconds": 30},
        },
        {
            "id": "status-cleanup",
            "kind": "read",
            "quota": 3,
            "call": call("GetStack", StackId="$created_id"),
            **bound,
            "read_policy": {
                "phase": "cleanup",
                "min_interval_seconds": 30,
                "required_cleanup": True,
                "after_delete": "create",
            },
        },
        {
            "id": "absence-name",
            "kind": "read",
            "quota": 1,
            "call": call("ListStacks", StackName="offline"),
            "read_policy": {"phase": "cleanup", "required_cleanup": True, "after_delete": "create"},
        },
    ]


@pytest.fixture
def controlled(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    gate.install(path, "0", "job:TF", "ws~full", "123", None, entries(), authority="reviewed:TF")
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    ticket = gate.reserve(entries()[0]["call"], "ws~full", credential, read_only=True)
    gate.finish(ticket, {"AccountId": "123"}, True)
    clock[0] += 1
    ticket = gate.reserve(entries()[1]["call"], "ws~full", credential, read_only=False)
    gate.finish(ticket, {"StackId": "actual-offline-id"}, True)
    return path, credential, clock


def counts(path):
    with sqlite3.connect(path) as db:
        return (
            dict(db.execute("SELECT id,used FROM authority_limits")),
            db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
        )


def read_status(controlled):
    path, credential, clock = controlled
    clock[0] += 1
    ticket = gate.reserve(call("GetStack", StackId="actual-offline-id"), "ws~full", credential, read_only=True)
    gate.finish(ticket, {"StackId": "actual-offline-id", "Status": "CREATE_COMPLETE"}, True)
    return ticket


def delete(controlled):
    path, credential, clock = controlled
    gate.cleanup(path, "0", "job:TF", "ws~full")
    clock[0] += 1
    ticket = gate.reserve(call("DeleteStack", StackId="actual-offline-id"), "ws~full", credential, read_only=False)
    gate.finish(ticket, {"RequestId": "offline-delete"}, True)
    clock[0] += 1


def test_source_reads_cannot_spend_cleanup_allowance(controlled):
    path, credential, clock = controlled
    for _ in range(3):
        read_status(controlled)
        clock[0] += 30
    assert counts(path)[0]["status-cleanup"] == 0
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(call("GetStack", StackId="actual-offline-id"), "ws~full", credential, read_only=True)
    with pytest.raises(WorkflowError, match="exhausted"):
        readonly_continuation(path, "0", "job:TF", "ws~full", "source")
    assert '"phase":"cleanup"' not in gate.execution_instructions("ws~full", "")
    delete(controlled)
    assert '"phase":"execute"' not in gate.execution_instructions("ws~full", "")
    assert readonly_continuation(path, "0", "job:TF", "ws~full", "cleanup")
    gate.finish(
        gate.reserve(call("GetStack", StackId="actual-offline-id"), "ws~full", credential, read_only=True),
        {"StackId": "actual-offline-id", "Status": "DELETE_COMPLETE"},
        True,
    )
    assert counts(path)[0]["status-cleanup"] == 1


def test_cleanup_check_cannot_run_before_successful_original_delete(controlled):
    path, credential, _ = controlled
    gate.cleanup(path, "0", "job:TF", "ws~full")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(call("ListStacks", StackName="offline"), "ws~full", credential, read_only=True)
    assert counts(path)[0]["absence-name"] == 0


@pytest.mark.asyncio
async def test_read_pacing_awaits_outside_database_and_consumes_once(controlled, monkeypatch):
    path, credential, clock = controlled
    read_status(controlled)
    before = counts(path)
    waits = []

    async def advance(seconds):
        waits.append(seconds)
        # Another slot/OPS can take the SQLite writer lock during the wait.
        with gate.database(path):
            assert counts(path) == before
        clock[0] += seconds

    monkeypatch.setattr(gate.asyncio, "sleep", advance)
    ticket = await gate.reserve_when_ready(
        call("GetStack", StackId="actual-offline-id"),
        "ws~full",
        credential,
        read_only=True,
    )
    assert waits == [30]
    assert counts(path)[0]["status-source"] == 2
    assert counts(path)[1] == before[1] + 1
    gate.finish(ticket, {"Status": "CREATE_COMPLETE"}, True)


@pytest.mark.asyncio
async def test_cancelled_wait_consumes_nothing_and_rechecks_closed_scope(controlled, monkeypatch):
    path, credential, _ = controlled
    read_status(controlled)
    before = counts(path)

    async def cancel(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(gate.asyncio, "sleep", cancel)
    with pytest.raises(asyncio.CancelledError):
        await gate.reserve_when_ready(
            call("GetStack", StackId="actual-offline-id"),
            "ws~full",
            credential,
            read_only=True,
        )
    assert counts(path) == before

    async def close(_seconds):
        with gate.database(path) as db:
            db.execute("UPDATE active SET phase='closed'")

    monkeypatch.setattr(gate.asyncio, "sleep", close)
    with pytest.raises(gate.GateDeniedError):
        await gate.reserve_when_ready(
            call("GetStack", StackId="actual-offline-id"),
            "ws~full",
            credential,
            read_only=True,
        )
    assert counts(path) == before


def test_reinitialization_preserves_original_wire_and_policy(controlled):
    path, _, _ = controlled
    read_status(controlled)
    before = counts(path)
    gate.initialize(path)
    assert counts(path) == before
    with gate.database(path) as db:
        policy = db.execute("SELECT read_policy FROM grants WHERE id='status-source'").fetchone()[0]
    assert json.loads(policy) == {"phase": "execute", "min_interval_seconds": 30}


def test_every_cleanup_read_is_required_even_if_stack_status_was_read(controlled):
    path, credential, _ = controlled
    delete(controlled)
    with pytest.raises(WorkflowError, match="required post-delete read"):
        assert_cleanup_reads_complete(path, "job:TF")
    gate.finish(
        gate.reserve(call("GetStack", StackId="actual-offline-id"), "ws~full", credential, read_only=True),
        {"StackId": "actual-offline-id", "Status": "DELETE_COMPLETE"},
        True,
    )
    with pytest.raises(WorkflowError, match="absence-name"):
        assert_cleanup_reads_complete(path, "job:TF")
    gate.finish(
        gate.reserve(call("ListStacks", StackName="offline"), "ws~full", credential, read_only=True),
        {"Stacks": [], "TotalCount": 0},
        True,
    )
    before = path.read_bytes()
    assert_cleanup_reads_complete(path, "job:TF")
    assert path.read_bytes() == before


def continuation_delivery(path):
    text = readonly_continuation(path, "0", "job:TF", "ws~full", "cleanup")
    return json.loads(text.split("\n")[-1])


def test_continuation_delivers_missing_exact_name_before_repeating_observed_status(controlled):
    path, credential, _ = controlled
    delete(controlled)
    gate.finish(
        gate.reserve(call("GetStack", StackId="actual-offline-id"), "ws~full", credential, read_only=True),
        {"StackId": "actual-offline-id", "Status": "DELETE_IN_PROGRESS"},
        True,
    )
    before = path.read_bytes()
    delivery = continuation_delivery(path)
    assert delivery == {
        "kind": "original_missing_cleanup_reads",
        "calls": [
            {
                "id": "absence-name",
                "call": call("ListStacks", StackName="offline"),
                "remaining": 1,
                "read_policy": {"phase": "cleanup", "required_cleanup": True, "after_delete": "create"},
            }
        ],
        "absence_verified": False,
        "allowance_changed": False,
    }
    assert path.read_bytes() == before
    gate.finish(
        gate.reserve(call("ListStacks", StackName="offline"), "ws~full", credential, read_only=True),
        {"Stacks": [{"StackId": "actual-offline-id", "Status": "DELETE_IN_PROGRESS"}], "TotalCount": 1},
        True,
    )
    text = readonly_continuation(path, "0", "job:TF", "ws~full", "cleanup")
    assert "original_missing_cleanup_reads" not in text  # Complete reads do not prove absence.


def test_continuation_projects_mandatory_id_only_from_actual_creation_response(controlled):
    path, _, _ = controlled
    delete(controlled)
    before = path.read_bytes()
    delivery = continuation_delivery(path)
    bound = next(item for item in delivery["calls"] if item["id"] == "status-cleanup")
    assert bound["call"] == call("GetStack", StackId="actual-offline-id")
    assert bound["read_policy"]["min_interval_seconds"] == 30
    assert delivery["absence_verified"] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["predelete_read", "exhausted_missing", "parent_response"])
def test_continuation_does_not_hide_stale_missing_or_unbound_checks(controlled, damage):
    path, credential, _ = controlled
    delete(controlled)
    if damage == "predelete_read":
        gate.finish(
            gate.reserve(call("GetStack", StackId="actual-offline-id"), "ws~full", credential, read_only=True),
            {"StackId": "actual-offline-id", "Status": "DELETE_IN_PROGRESS"},
            True,
        )
    with sqlite3.connect(path) as db:
        if damage == "predelete_read":
            db.execute("UPDATE attempts SET created=0 WHERE grant_id='status-cleanup'")
        elif damage == "exhausted_missing":
            db.execute("UPDATE grants SET used=1 WHERE id='absence-name'")
            db.execute("UPDATE authority_limits SET used=1 WHERE id='absence-name'")
        else:
            db.execute("UPDATE attempts SET response='{}' WHERE grant_id='create'")
    before = path.read_bytes()
    if damage == "predelete_read":
        assert {item["id"] for item in continuation_delivery(path)["calls"]} == {"status-cleanup", "absence-name"}
    else:
        with pytest.raises(WorkflowError, match="no original allowance|response binding"):
            continuation_delivery(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["missing", "wrong_phase", "no_required", "wrong_parent", "fast", "write_policy"])
def test_new_plan_policy_is_machine_checked_not_optional_prompt(damage):
    policy_entries = copy.deepcopy(entries())
    if damage == "missing":
        del policy_entries[3]["read_policy"]
    elif damage == "wrong_phase":
        policy_entries[4]["read_policy"]["phase"] = "execute"
    elif damage == "no_required":
        for entry in policy_entries:
            if "read_policy" in entry:
                entry["read_policy"]["required_cleanup"] = False
    elif damage == "wrong_parent":
        policy_entries[4]["read_policy"]["after_delete"] = "identity"
    elif damage == "fast":
        policy_entries[3]["read_policy"]["min_interval_seconds"] = 3
    else:
        policy_entries[1]["read_policy"] = {"phase": "execute"}
    packet = {"execution_grants": {"TF": policy_entries}, "scenes": {"x": {"account": "123"}}}
    with pytest.raises(WorkflowError):
        validate_execution(packet, {"TF"}, require_read_policy=True)


def test_valid_policy_and_legacy_input_remain_separate():
    gate.validate_grants(entries(), require_read_policy=True)
    legacy = [{k: v for k, v in entry.items() if k != "read_policy"} for entry in entries()]
    gate.validate_grants(legacy)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(legacy, require_read_policy=True)


def _ram_read_ticket(tmp_path, monkeypatch, *, action="GetPolicy", product="ram", version="2015-05-01"):
    path = tmp_path / "ram-wire.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    params = (
        {"RoleName": "offline-role"}
        if action == "GetRole"
        else {
            "PolicyName": "offline-policy",
            "PolicyType": "Custom",
        }
    )
    request = {"product": product, "action": action, "version": version, "region_id": "cn-hangzhou", "params": params}
    identity = {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}}
    grants = [
        {"id": "identity", "kind": "identity", "quota": 1, "call": identity},
        {"id": "exact-read", "kind": "read", "quota": 1, "call": request, "read_policy": {"phase": "execute"}},
    ]
    gate.install(path, "0", "offline-ram:QUERY", "ws~offline-full", "123", None, grants, authority="offline-ram")
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    gate.finish(gate.reserve(identity, "ws~offline-full", credential, read_only=True), {"AccountId": "123"}, True)
    ticket = gate.reserve(request, "ws~offline-full", credential, read_only=True)
    return path, ticket, request, credential


def _ram_negative_body(action):
    role = action == "GetRole"
    return {
        "Code": "EntityNotExist.Role" if role else "EntityNotExist.Policy",
        "Message": "The role not exists:offline-role" if role else "The policy does not exist: offline-policy",
        "HostId": "ram.aliyuncs.com",
        "RequestId": "offline-negative-request",
    }


@pytest.mark.parametrize("action", ["GetPolicy", "GetRole"])
@pytest.mark.parametrize("status", [200, 404, 403, 500, None])
def test_ram_read_finish_preserves_transport_status_body_and_quota(tmp_path, monkeypatch, action, status):
    path, ticket, request, credential = _ram_read_ticket(tmp_path, monkeypatch, action=action)
    body = _ram_negative_body(action)
    original = copy.deepcopy(body)
    succeeded = status == 200
    before = counts(path)
    headers = {
        "X-Acs-Request-Id": body["RequestId"],
        "Authorization": "fake-private",
        "Set-Cookie": "fake-cookie",
        "x-oss-request-id": "unrelated",
        7: "invalid",
    }
    gate.finish(ticket, body, succeeded, status=status, headers=headers)
    assert body == original and "statusCode" not in body
    assert counts(path) == before
    with gate.database(path) as db:
        row = dict(
            db.execute(
                "SELECT a.state,a.response,g.kind,g.call FROM attempts a JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
                (ticket[1],),
            ).fetchone()
        )
    assert row["state"] == ("succeeded" if succeeded else "unknown")
    assert json.loads(row["response"]) == {
        "body": original,
        "status": status,
        "headers": {"X-Acs-Request-Id": body["RequestId"]},
    }
    from scripts.web.e2e.resource_link_workflow.phases import known_absence_read

    assert known_absence_read(row) is (status == 404)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(request, "ws~offline-full", credential, read_only=True)
    assert counts(path) == before


@pytest.mark.parametrize("action", ["GetPolicy", "GetRole"])
@pytest.mark.parametrize("mutation", ["body_status", "header_id", "name", "code", "host", "missing_id"])
def test_ram_read_envelope_never_rewrites_conflicting_negative_evidence(tmp_path, monkeypatch, action, mutation):
    path, ticket, _, _ = _ram_read_ticket(tmp_path, monkeypatch, action=action)
    body = _ram_negative_body(action)
    headers = {}
    if mutation == "body_status":
        body["statusCode"] = 403
    elif mutation == "header_id":
        headers["x-acs-request-id"] = "different-request"
    elif mutation == "name":
        body["Message"] += "-other"
    elif mutation == "code":
        body["Code"] = "AccessDenied"
    elif mutation == "host":
        body["HostId"] = "other.aliyuncs.com"
    else:
        body.pop("RequestId")
    original = copy.deepcopy(body)
    before = counts(path)
    gate.finish(ticket, body, False, status=404, headers=headers)
    with gate.database(path) as db:
        row = dict(
            db.execute(
                "SELECT a.state,a.response,g.kind,g.call FROM attempts a JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
                (ticket[1],),
            ).fetchone()
        )
    assert json.loads(row["response"]) == {"body": original, "status": 404, "headers": headers}
    assert body == original and row["state"] == "unknown" and counts(path) == before
    from scripts.web.e2e.resource_link_workflow.phases import known_absence_read

    assert known_absence_read(row) is False


@pytest.mark.parametrize(
    "product,action,version",
    [
        ("ram", "GetPolicy", "other-version"),
        ("ram", "GetRole", "other-version"),
        ("ram", "ListPolicies", "2015-05-01"),
        ("ecs", "GetPolicy", "2015-05-01"),
    ],
)
def test_ram_read_envelope_is_limited_to_exact_product_version_and_action(
    tmp_path,
    monkeypatch,
    product,
    action,
    version,
):
    path, ticket, _, _ = _ram_read_ticket(
        tmp_path,
        monkeypatch,
        product=product,
        action=action,
        version=version,
    )
    body = _ram_negative_body(action)
    gate.finish(ticket, body, False, status=404, headers={"x-acs-request-id": body["RequestId"]})
    with gate.database(path) as db:
        row = db.execute("SELECT state,response FROM attempts WHERE id=?", (ticket[1],)).fetchone()
    assert row["state"] == "unknown" and json.loads(row["response"]) == body


def test_ram_finish_does_not_infer_status_or_rewrite_legacy_rows(tmp_path, monkeypatch):
    path, ticket, _, _ = _ram_read_ticket(tmp_path, monkeypatch)
    body = {**_ram_negative_body("GetPolicy"), "statusCode": 404}
    gate.finish(ticket, body, False)
    with gate.database(path) as db:
        row = dict(
            db.execute(
                "SELECT a.state,a.response,g.kind,g.call FROM attempts a JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
                (ticket[1],),
            ).fetchone()
        )
    assert json.loads(row["response"]) == {"body": body, "status": None, "headers": {}}
    from scripts.web.e2e.resource_link_workflow.phases import known_absence_read

    assert known_absence_read(row) is False
    with gate.database(path) as db:
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(body), ticket[1]))
    before = path.read_bytes()
    gate.initialize(path)
    assert path.read_bytes() == before
    with gate.database(path) as db:
        legacy = dict(
            db.execute(
                "SELECT a.state,a.response,g.kind,g.call FROM attempts a JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
                (ticket[1],),
            ).fetchone()
        )
    assert legacy["response"] == json.dumps(body) and legacy["state"] == "unknown"
    assert known_absence_read(legacy) is True


def group_scope(mode):
    return "offline-group:" + {"ROS": "ROS", "Terraform": "TF", "API": "CREATE"}[mode]


@pytest.mark.parametrize("mode", ["ROS", "Terraform"])
def test_group_framework_phase_reads_actual_bound_transport_body(tmp_path, monkeypatch, mode):
    from scripts.web.e2e.resource_link_workflow.phases import assert_stack_source_ready

    controlled = setup_group_gate(tmp_path, monkeypatch, mode)
    path = controlled[0]
    group_source_proofs(controlled)
    before = path.read_bytes()
    assert_stack_source_ready(path, group_scope(mode), cleanup=False)
    assert path.read_bytes() == before
    gate.cleanup(path, "0", group_scope(mode), "ws~group-full")
    group_proof(controlled, "ListResources", empty_member_body())
    gate.finish(group_delete(controlled), {"RequestId": "offline-delete"}, True, status=200)
    with pytest.raises(WorkflowError, match="still pending"):
        assert_stack_source_ready(path, group_scope(mode), cleanup=True)
    group_proof(
        controlled,
        "GetStack",
        {
            "RequestId": "offline-cleanup",
            "StackId": "stack-own",
            "Status": "DELETE_IN_PROGRESS",
        },
    )
    with pytest.raises(WorkflowError, match="still pending"):
        assert_stack_source_ready(path, group_scope(mode), cleanup=True)
    group_proof(
        controlled,
        "GetStack",
        {
            "RequestId": "offline-cleanup-done",
            "StackId": "stack-own",
            "Status": "DELETE_COMPLETE",
        },
    )
    before = path.read_bytes()
    assert_stack_source_ready(path, group_scope(mode), cleanup=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "status",
        "request",
        "header",
        "call",
        "body_id",
        "session",
        "credential",
        "account",
        "parent",
        "error",
        "raw",
    ],
)
def test_group_framework_phase_never_trusts_unbound_envelope(tmp_path, monkeypatch, damage):
    from scripts.web.e2e.resource_link_workflow.phases import assert_stack_source_ready

    controlled = setup_group_gate(tmp_path, monkeypatch, "ROS")
    group_source_proofs(controlled)
    path = controlled[0]
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT id,response FROM attempts WHERE grant_id='status-source'").fetchone()
        envelope = json.loads(row[1])
        if damage == "status":
            envelope["status"] = 403
        elif damage == "request":
            envelope["body"].pop("RequestId")
        elif damage == "header":
            envelope["headers"]["x-acs-request-id"] = "foreign"
        elif damage == "call":
            envelope["call"]["params"]["StackId"] = "foreign"
        elif damage == "body_id":
            envelope["body"]["StackId"] = "foreign"
        elif damage in {"session", "credential"}:
            db.execute(f"UPDATE attempts SET {damage}='foreign' WHERE id=?", (row[0],))
        elif damage == "account":
            db.execute("UPDATE identities SET account='foreign'")
        elif damage == "parent":
            db.execute("UPDATE grants SET parent='foreign' WHERE id='status-source'")
        elif damage == "error":
            envelope["body"]["Code"] = "Denied"
        elif damage == "raw":
            envelope = envelope["body"]
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row[0]))
    before = path.read_bytes()
    with pytest.raises(WorkflowError, match="still pending"):
        assert_stack_source_ready(path, group_scope("ROS"), cleanup=False)
    assert path.read_bytes() == before


def test_group_framework_phase_does_not_fall_back_to_stale_valid_proof(tmp_path, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.phases import assert_stack_source_ready

    controlled = setup_group_gate(tmp_path, monkeypatch, "ROS")
    group_source_proofs(controlled)
    path = controlled[0]
    group_proof(
        controlled,
        "GetStack",
        {
            "RequestId": "offline-latest",
            "StackId": "stack-own",
            "Status": "CREATE_COMPLETE",
        },
    )
    # Test corrupted proof, not an actual rejected read: actual errors must
    # remain capturable as errors, not be disguised by a pending-state guard.
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT id,response FROM attempts WHERE grant_id='status-source' ORDER BY created DESC LIMIT 1"
        ).fetchone()
        envelope = json.loads(row[1])
        envelope["status"] = 403
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row[0]))
    before = path.read_bytes()
    with pytest.raises(WorkflowError, match="still pending"):
        assert_stack_source_ready(path, group_scope("ROS"), cleanup=False)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["ROS", "Terraform", "API"])
def test_group_cleanup_inspection_projects_only_original_child_and_preserves_guard(tmp_path, monkeypatch, mode):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate

    controlled = setup_group_gate(tmp_path, monkeypatch, mode)
    group_source_proofs(controlled)
    path = controlled[0]
    before = path.read_bytes()
    candidate = inspect_candidate(
        path,
        slot="0",
        scope=group_scope(mode),
        session="ws~group-full",
        account="123",
        grants=resource_group_entries(mode),
        authority="group:" + mode,
    )
    assert path.read_bytes() == before
    assert candidate["candidate_only"] and not candidate["activation_allowed"]
    assert not candidate["cloud_call_performed"] and not candidate["cleanup_confirmed"]
    calls = {entry["id"]: entry for entry in candidate["remaining_calls"]}
    assert calls["members"]["call"]["params"]["ResourceGroupId"] == "rg-offline123"
    assert calls["group-absent"]["call"]["params"]["ResourceGroupId"] == "rg-offline123"
    assert calls["delete"]["remaining"] == 1
    assert calls["delete"]["predelete_required_read"] == "members"
    expected_key, expected_id = ("ResourceGroupId", "rg-offline123") if mode == "API" else ("StackId", "stack-own")
    assert calls["delete"]["call"]["params"][expected_key] == expected_id
    # A read-only recovery candidate cannot bypass the actual empty-group guard.
    gate.cleanup(path, "0", group_scope(mode), "ws~group-full")
    with pytest.raises(gate.GateDeniedError):
        group_delete(controlled)


def test_group_cleanup_inspection_rejects_missing_child_proof_without_mutation(tmp_path, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate

    controlled = setup_group_gate(tmp_path, monkeypatch, "ROS")
    path = controlled[0]
    before = path.read_bytes()
    with pytest.raises(WorkflowError):
        inspect_candidate(
            path,
            slot="0",
            scope=group_scope("ROS"),
            session="ws~group-full",
            account="123",
            grants=resource_group_entries("ROS"),
            authority="group:ROS",
        )
    assert path.read_bytes() == before


def resource_group_entries(mode):
    logical = "Group" if mode == "ROS" else "group"
    child = {
        "kind": "resource_group_child",
        "logical_resource_id": logical,
        "output_key": "group_id",
        "stack_status_grant": "status-source",
        "stack_resources_grant": "resource-source",
    }
    if mode == "ROS":
        template = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {
                "Group": {
                    "Type": "ALIYUN::ResourceManager::ResourceGroup",
                    "Properties": {"Name": "offlinegrp", "DisplayName": "offline-group"},
                }
            },
            "Outputs": {"group_id": {"Value": {"Fn::GetAtt": ["Group", "Id"]}}},
        }
    else:
        content = (
            'terraform { required_providers { alicloud = { source = "aliyun/alicloud" version = "= 1.140.0" } } }\n'
            'provider "alicloud" { region = "cn-hangzhou" }\n'
            'resource "alicloud_resource_manager_resource_group" "group" {\n'
            ' resource_group_name = "offlinegrp"\n display_name = "offline-group"\n}\n'
            'output "group_id" { value = alicloud_resource_manager_resource_group.group.id }\n'
        )
        template = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Transform": "Aliyun::Terraform-v1.5",
            "Workspace": {"main.tf": content},
        }
    grants = entries()
    grants[1]["call"] = group_call(
        "CreateStack",
        StackName="offline-stack",
        RegionId="cn-hangzhou",
        TemplateBody=json.dumps(template),
        TimeoutInMinutes=20,
    )
    grants[2]["call"] = group_call(
        "DeleteStack", StackId="$created_id", RegionId="cn-hangzhou", RetainAllResources=False
    )
    grants[2]["response_path"] = {"kind": "resource_group_empty", "path": ["StackId"], "read_grant": "members"}
    for grant in grants[3:5]:
        grant["call"] = group_call("GetStack", StackId="$created_id", RegionId="cn-hangzhou", OutputOption="Enabled")
    grants[5]["call"] = group_call("ListStacks", StackName=["offline-stack"], RegionId="cn-hangzhou")
    grants += [
        {
            "id": "resource-source",
            "kind": "read",
            "quota": 2,
            "call": group_call("ListStackResources", StackId="$created_id", RegionId="cn-hangzhou"),
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "read_policy": {"phase": "execute"},
        },
        {
            "id": "group-source",
            "kind": "read",
            "quota": 2,
            "call": group_call("GetResourceGroup", ResourceGroupId="$created_id"),
            "parent": "create",
            "response_path": child,
            "target_param": "ResourceGroupId",
            "read_policy": {"phase": "execute"},
        },
        {
            "id": "members",
            "kind": "read",
            "quota": 2,
            "call": group_call("ListResources", ResourceGroupId="$created_id", PageNumber=1, PageSize=10),
            "parent": "create",
            "response_path": child,
            "target_param": "ResourceGroupId",
            "read_policy": {"phase": "cleanup"},
        },
        {
            "id": "group-absent",
            "kind": "read",
            "quota": 1,
            "call": group_call("ListResourceGroups", ResourceGroupId="$created_id", PageNumber=1, PageSize=10),
            "parent": "create",
            "response_path": child,
            "target_param": "ResourceGroupId",
            "read_policy": {"phase": "cleanup", "required_cleanup": True, "after_delete": "create"},
        },
    ]
    if mode == "API":
        grants[1]["call"] = group_call("CreateResourceGroup", Name="offlinegrp", DisplayName="offline-group")
        grants[2]["call"] = group_call("DeleteResourceGroup", ResourceGroupId="$created_id")
        grants[2]["response_path"]["path"] = ["ResourceGroup", "Id"]
        grants[2]["target_param"] = "ResourceGroupId"
        grants = [
            grant
            for grant in grants
            if grant["id"]
            not in {
                "status-source",
                "status-cleanup",
                "absence-name",
                "resource-source",
            }
        ]
        for grant in grants:
            if grant.get("parent") == "create" and grant["kind"] == "read":
                grant["response_path"] = ["ResourceGroup", "Id"]
    return grants


def group_call(action, **params):
    product = (
        "resourcemanager"
        if action
        in {
            "CreateResourceGroup",
            "DeleteResourceGroup",
            "GetResourceGroup",
            "ListResourceGroups",
            "ListResources",
        }
        else "ros"
    )
    return {
        "product": product,
        "action": action,
        "version": "2020-03-31" if product == "resourcemanager" else "2019-09-10",
        "region_id": "cn-hangzhou",
        "params": params,
    }


def setup_group_gate(tmp_path, monkeypatch, mode):
    path = tmp_path / "group.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    grants = resource_group_entries(mode)
    gate.validate_grants(grants, require_read_policy=True)
    gate.install(path, "0", group_scope(mode), "ws~group-full", "123", None, grants, authority="group:" + mode)
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    gate.finish(
        gate.reserve(grants[0]["call"], "ws~group-full", credential, read_only=True), {"AccountId": "123"}, True
    )
    group = {
        "Id": "rg-offline123",
        "Name": "offlinegrp",
        "DisplayName": "offline-group",
        "AccountId": "123",
        "Status": "OK",
    }
    created = {
        "RequestId": "offline-create",
        **({"ResourceGroup": group} if mode == "API" else {"StackId": "stack-own"}),
    }
    clock[0] += 1
    gate.finish(
        gate.reserve(grants[1]["call"], "ws~group-full", credential, read_only=False), created, True, status=200
    )
    return path, credential, clock, mode


def group_proof(controlled, action, body, *, status=200, succeeded=True, headers=None):
    path, credential, clock, mode = controlled
    clock[0] += 31
    params = (
        {"ResourceGroupId": "rg-offline123"}
        if action
        in {
            "GetResourceGroup",
            "ListResources",
            "ListResourceGroups",
        }
        else {"StackId": "stack-own", "RegionId": "cn-hangzhou"}
    )
    if action == "GetStack":
        params["OutputOption"] = "Enabled"
    if action in {"ListResources", "ListResourceGroups"}:
        params.update(PageNumber=1, PageSize=10)
    ticket = gate.reserve(group_call(action, **params), "ws~group-full", credential, read_only=True)
    gate.finish(ticket, body, succeeded, status=status, headers=headers)
    return ticket


def group_source_proofs(controlled):
    if controlled[3] != "API":
        group_proof(
            controlled,
            "GetStack",
            {
                "RequestId": "offline-status",
                "StackId": "stack-own",
                "StackName": "offline-stack",
                "RegionId": "cn-hangzhou",
                "Status": "CREATE_COMPLETE",
                "StackType": controlled[3],
                "Outputs": [{"OutputKey": "group_id", "OutputValue": "rg-offline123"}],
            },
        )
        group_proof(
            controlled,
            "ListStackResources",
            {
                "RequestId": "offline-resources",
                "Resources": [
                    {
                        "StackId": "stack-own",
                        "StackName": "offline-stack",
                        "LogicalResourceId": "Group" if controlled[3] == "ROS" else "group",
                        "ResourceType": "ALIYUN::ResourceManager::ResourceGroup"
                        if controlled[3] == "ROS"
                        else "alicloud_resource_manager_resource_group",
                        "PhysicalResourceId": "rg-offline123",
                        "Status": "CREATE_COMPLETE",
                    }
                ],
            },
        )
    group_proof(
        controlled,
        "GetResourceGroup",
        {
            "RequestId": "offline-group",
            "ResourceGroup": {
                "Id": "rg-offline123",
                "Name": "offlinegrp",
                "DisplayName": "offline-group",
                "AccountId": "123",
                "Status": "OK",
            },
        },
    )


def group_delete(controlled):
    path, credential, clock, mode = controlled
    clock[0] += 1
    return gate.reserve(
        group_call("DeleteResourceGroup", ResourceGroupId="rg-offline123")
        if mode == "API"
        else group_call("DeleteStack", StackId="stack-own", RegionId="cn-hangzhou", RetainAllResources=False),
        "ws~group-full",
        credential,
        read_only=False,
    )


def empty_member_body():
    return {
        "RequestId": "offline-members",
        "TotalCount": 0,
        "PageNumber": 1,
        "PageSize": 10,
        "Resources": {"Resource": []},
    }


@pytest.mark.parametrize("mode", ["ROS", "Terraform", "API"])
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "missing_review",
        "foreign_id",
        "delete_extra",
        "session",
        "nonempty_attempt",
        "unknown_attempt",
        "authority",
        "owned",
    ],
)
def test_cleanup_recovery_delivers_pending_member_read_without_unlocking_delete(tmp_path, monkeypatch, mode, damage):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import exact_delete_request, inspect_candidate
    from scripts.web.e2e.resource_link_workflow.files import digest, json_write

    controlled = setup_group_gate(tmp_path, monkeypatch, mode)
    group_source_proofs(controlled)
    path, credential, _, _ = controlled
    gate.cleanup(path, "0", group_scope(mode), "ws~group-full")
    path = path.rename(tmp_path / "execution-gate.sqlite3")
    monkeypatch.setenv(gate.ENV, str(path))
    candidate = inspect_candidate(
        path,
        slot="0",
        scope=group_scope(mode),
        session="ws~group-full",
        account="123",
        grants=resource_group_entries(mode),
        authority="group:" + mode,
    )
    delete = next(item for item in candidate["remaining_calls"] if item["kind"] == "delete")
    preread = next(item for item in candidate["remaining_calls"] if item["id"] == "members")
    if damage in {"nonempty_attempt", "unknown_attempt"}:
        gate.finish(
            gate.reserve(preread["call"], "ws~group-full", credential, read_only=True),
            {
                "RequestId": "offline-nonempty",
                "TotalCount": 1,
                "PageNumber": 1,
                "PageSize": 10,
                "Resources": {"Resource": [{"ResourceId": "business"}]},
            },
            damage != "unknown_attempt",
            status=200,
        )
    proof = {
        "kind": "ops_cleanup_only_review",
        "approved": True,
        "original_job_id": "offline-group",
        "dimension": "TF" if mode == "Terraform" else ("CREATE" if mode == "API" else "ROS"),
        **{key: candidate[key] for key in ("scope", "authority", "account", "owned_from_wire")},
        "delete_call": delete["call"],
        "predelete_read": preread,
    }
    recovery = {
        "original_job_id": "offline-group",
        "dimension": proof["dimension"],
        "authority": candidate["authority"],
        "evidence": "inputs/preread-review.json",
    }
    if damage == "missing_review":
        proof.pop("predelete_read")
    elif damage == "foreign_id":
        proof["predelete_read"]["call"]["params"]["ResourceGroupId"] = "rg-other"
    elif damage == "delete_extra":
        proof["delete_call"]["params"]["DisableRecovery"] = False
    elif damage == "authority":
        recovery["authority"] = "other-authority"
    elif damage == "owned":
        proof["owned_from_wire"] = []
    json_write(tmp_path / recovery["evidence"], proof, private=True)
    recovery["evidence_sha256"] = digest(tmp_path / recovery["evidence"])
    tables = ("grants", "attempts", "identities", "authority_limits", "active")
    with sqlite3.connect(path) as db:
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    if damage:
        with pytest.raises(WorkflowError):
            exact_delete_request(tmp_path, recovery, "0", "other-session" if damage == "session" else "ws~group-full")
    else:
        text = exact_delete_request(tmp_path, recovery, "0", "ws~group-full")
        assert '"ListResources"' in text and text.index('"ListResources"') < text.index('"Delete')
        assert "不得先调用 GetStack" in text
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(delete["call"], "ws~group-full", credential, read_only=False)
    with sqlite3.connect(path) as db:
        assert {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before


@pytest.mark.parametrize("mode", ["ROS", "Terraform", "API"])
def test_group_delete_requires_actual_empty_read_and_preserves_original_target(tmp_path, monkeypatch, mode):
    controlled = setup_group_gate(tmp_path, monkeypatch, mode)
    path, credential, _, _ = controlled
    group_source_proofs(controlled)
    gate.cleanup(path, "0", group_scope(mode), "ws~group-full")
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        group_delete(controlled)
    assert counts(path) == before
    group_proof(controlled, "ListResources", empty_member_body())
    instructions = gate.execution_instructions("ws~group-full", "")
    assert "resource_group_empty" in instructions and "Resources.Resource=[]" in instructions
    assert "same original session" in instructions
    ticket = group_delete(controlled)
    gate.finish(ticket, {"RequestId": "offline-delete"}, True, status=200)
    assert counts(path)[0]["delete"] == 1
    with pytest.raises(gate.GateDeniedError):
        group_delete(controlled)
    group_proof(
        controlled,
        "ListResourceGroups",
        {
            "RequestId": "offline-absent",
            "TotalCount": 0,
            "PageNumber": 1,
            "PageSize": 10,
            "ResourceGroups": {"ResourceGroup": []},
        },
    )
    original = gate.creations(path, ["offline-group"])
    assert original[0]["resource"]["id"] == ("rg-offline123" if mode == "API" else "stack-own")


@pytest.mark.parametrize("mode", ["ROS", "Terraform", "API"])
@pytest.mark.parametrize(
    "damage",
    [
        "member",
        "missing_resources",
        "null_resources",
        "wrong_resources",
        "bool_count",
        "count_conflict",
        "missing_request",
        "wrong_page",
        "wrong_size",
        "next_token",
        "body_error",
        "foreign_id",
        "transport_403",
        "transport_missing",
        "header_conflict",
        "unknown",
    ],
)
def test_group_empty_guard_rejects_incomplete_or_nonempty_original_reads(tmp_path, monkeypatch, mode, damage):
    controlled = setup_group_gate(tmp_path, monkeypatch, mode)
    group_source_proofs(controlled)
    path, _, _, _ = controlled
    gate.cleanup(path, "0", group_scope(mode), "ws~group-full")
    body, status, succeeded, headers = empty_member_body(), 200, True, None
    if damage == "member":
        body.update(TotalCount=1, Resources={"Resource": [{"ResourceId": "i-foreign"}]})
    elif damage == "missing_resources":
        body.pop("Resources")
    elif damage == "null_resources":
        body["Resources"] = None
    elif damage == "wrong_resources":
        body["Resources"] = []
    elif damage == "bool_count":
        body["TotalCount"] = False
    elif damage == "count_conflict":
        body["TotalCount"] = 1
    elif damage == "missing_request":
        body.pop("RequestId")
    elif damage == "wrong_page":
        body["PageNumber"] = 2
    elif damage == "wrong_size":
        body["PageSize"] = 100
    elif damage == "next_token":
        body["NextToken"] = "unread"
    elif damage == "body_error":
        body["Code"] = "NoPermission"
    elif damage == "foreign_id":
        body["ResourceGroupId"] = "rg-foreign123"
    elif damage == "transport_403":
        status = 403
    elif damage == "transport_missing":
        status = None
    elif damage == "header_conflict":
        headers = {"x-acs-request-id": "different"}
    else:
        succeeded = False
    group_proof(controlled, "ListResources", body, status=status, succeeded=succeeded, headers=headers)
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        group_delete(controlled)
    assert counts(path) == before


@pytest.mark.parametrize("mode", ["ROS", "Terraform"])
@pytest.mark.parametrize(
    "damage",
    [
        "stack",
        "name",
        "region",
        "status",
        "type",
        "account",
        "output",
        "duplicate",
        "scalar",
        "resource_id",
        "resource_stack",
        "resource_type",
        "resource_logical",
        "resource_status",
        "resource_duplicate",
    ],
)
def test_group_child_binding_rejects_foreign_ambiguous_or_unfinished_sources(tmp_path, monkeypatch, mode, damage):
    controlled = setup_group_gate(tmp_path, monkeypatch, mode)
    group_source_proofs(controlled)
    path, credential, _, _ = controlled
    grant_id = "resource-source" if damage.startswith("resource_") else "status-source"
    with gate.database(path) as db:
        row = db.execute("SELECT id,response FROM attempts WHERE grant_id=?", (grant_id,)).fetchone()
        envelope = json.loads(row["response"])
        body = envelope["body"]
        if damage in {"stack", "name", "region", "status", "type", "account"}:
            field = {
                "stack": "StackId",
                "name": "StackName",
                "region": "RegionId",
                "status": "Status",
                "type": "StackType",
                "account": "AccountId",
            }[damage]
            body[field] = "CREATE_IN_PROGRESS" if damage == "status" else "foreign"
        elif damage == "output":
            body["Outputs"][0]["OutputValue"] = "rg-foreign123"
        elif damage == "duplicate":
            body["Outputs"].append(copy.deepcopy(body["Outputs"][0]))
        elif damage == "scalar":
            body["Outputs"][0]["OutputValue"] = True
        elif damage == "resource_duplicate":
            body["Resources"].append(copy.deepcopy(body["Resources"][0]))
        else:
            field = {
                "resource_id": "PhysicalResourceId",
                "resource_stack": "StackId",
                "resource_type": "ResourceType",
                "resource_logical": "LogicalResourceId",
                "resource_status": "Status",
            }[damage]
            body["Resources"][0][field] = "foreign"
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(
            group_call("GetResourceGroup", ResourceGroupId="rg-offline123"), "ws~group-full", credential, read_only=True
        )
    assert counts(path) == before


@pytest.mark.parametrize(
    "damage", ["product", "action", "region", "path", "filter", "empty_phase", "retain", "template"]
)
def test_group_binding_validation_never_opens_generic_cross_product_or_write(damage):
    grants = copy.deepcopy(resource_group_entries("ROS"))
    member = next(grant for grant in grants if grant["id"] == "members")
    if damage == "product":
        member["call"]["product"] = "ecs"
    elif damage == "action":
        member["call"]["action"] = "DeleteResourceGroup"
    elif damage == "region":
        member["call"]["region_id"] = "cn-beijing"
    elif damage == "path":
        member["response_path"]["kind"] = "arbitrary_read_parent"
    elif damage == "filter":
        member["call"]["params"]["Service"] = "ecs"
    elif damage == "empty_phase":
        member["read_policy"]["phase"] = "execute"
    elif damage == "retain":
        grants[2]["call"]["params"]["RetainAllResources"] = True
    else:
        template = json.loads(grants[1]["call"]["params"]["TemplateBody"])
        template["Resources"]["Other"] = template["Resources"]["Group"]
        grants[1]["call"]["params"]["TemplateBody"] = json.dumps(template)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)


@pytest.mark.parametrize("damage", ["session", "signer", "account", "actual_call", "details"])
def test_group_empty_guard_requires_original_session_signer_account_and_wire_target(tmp_path, monkeypatch, damage):
    controlled = setup_group_gate(tmp_path, monkeypatch, "ROS")
    group_source_proofs(controlled)
    path, credential, _, _ = controlled
    gate.cleanup(path, "0", "offline-group:ROS", "ws~group-full")
    group_proof(controlled, "ListResources", empty_member_body())
    with gate.database(path) as db:
        if damage in {"session", "account"}:
            db.execute("UPDATE active SET " + damage + "=?", ("foreign",))
        elif damage == "actual_call":
            row = db.execute("SELECT id,response FROM attempts WHERE grant_id='members'").fetchone()
            envelope = json.loads(row["response"])
            envelope["call"]["params"]["ResourceGroupId"] = "rg-foreign123"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
        elif damage == "details":
            row = db.execute("SELECT id,response FROM attempts WHERE grant_id='group-source'").fetchone()
            envelope = json.loads(row["response"])
            envelope["body"]["ResourceGroup"]["AccountId"] = "foreign"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
    if damage == "signer":
        credential = SimpleNamespace(mode="AK", access_key_id="rotated", access_key_secret="fake-secret")
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(
            group_call("DeleteStack", StackId="stack-own", RegionId="cn-hangzhou", RetainAllResources=False),
            "ws~group-full",
            credential,
            read_only=False,
        )
    assert counts(path) == before


def test_group_transport_preserves_body_and_never_rewrites_legacy_or_replenishes_quota(tmp_path, monkeypatch):
    controlled = setup_group_gate(tmp_path, monkeypatch, "API")
    group_source_proofs(controlled)
    path, _, _, _ = controlled
    gate.cleanup(path, "0", group_scope("API"), "ws~group-full")
    body = empty_member_body()
    original = copy.deepcopy(body)
    ticket = group_proof(
        controlled,
        "ListResources",
        body,
        status=403,
        headers={"x-acs-request-id": "offline-members", "Authorization": "fake-private"},
    )
    with gate.database(path) as db:
        row = db.execute("SELECT state,response FROM attempts WHERE id=?", (ticket[1],)).fetchone()
        envelope = json.loads(row["response"])
    assert row["state"] == "unknown" and envelope["body"] == original and body == original
    assert envelope["status"] == 403 and envelope["headers"] == {"x-acs-request-id": "offline-members"}
    before = path.read_bytes()
    gate.initialize(path)
    assert path.read_bytes() == before
    with pytest.raises(gate.GateDeniedError):
        gate.install(path, "0", group_scope("API"), "ws~group-full", "123", None, resource_group_entries("API"))
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["ROS", "Terraform", "API"])
@pytest.mark.parametrize("damage", ["missing_details", "duplicate_details", "unguarded_delete", "duplicate_delete"])
def test_group_review_rejects_missing_ambiguous_proofs_and_delete_bypasses(mode, damage):
    grants = copy.deepcopy(resource_group_entries(mode))
    details = next(grant for grant in grants if grant["call"]["action"] == "GetResourceGroup")
    deletion = next(grant for grant in grants if grant["kind"] == "delete")
    if damage == "missing_details":
        grants.remove(details)
    elif damage == "duplicate_details":
        duplicate = copy.deepcopy(details)
        duplicate["id"] = "duplicate-details"
        grants.append(duplicate)
    else:
        duplicate = copy.deepcopy(deletion)
        duplicate["id"] = "duplicate-delete"
        if damage == "unguarded_delete":
            duplicate["response_path"] = duplicate["response_path"]["path"]
        grants.append(duplicate)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)


@pytest.mark.parametrize("mode", ["ROS", "Terraform", "API"])
def test_group_latest_unknown_member_read_blocks_previous_empty_success(tmp_path, monkeypatch, mode):
    controlled = setup_group_gate(tmp_path, monkeypatch, mode)
    group_source_proofs(controlled)
    gate.cleanup(controlled[0], "0", group_scope(mode), "ws~group-full")
    group_proof(controlled, "ListResources", empty_member_body())
    group_proof(controlled, "ListResources", empty_member_body(), succeeded=False)
    before = counts(controlled[0])
    with pytest.raises(gate.GateDeniedError):
        group_delete(controlled)
    assert counts(controlled[0]) == before


@pytest.mark.parametrize("damage", ["constant", "other_output", "extra_resource", "extra_module", "provider", "region"])
def test_group_hosted_template_requires_real_sole_group_output(damage):
    grants = copy.deepcopy(resource_group_entries("Terraform"))
    template = json.loads(grants[1]["call"]["params"]["TemplateBody"])
    content = template["Workspace"]["main.tf"]
    if damage == "constant":
        content = content.replace("alicloud_resource_manager_resource_group.group.id", '"rg-forged123"')
    elif damage == "other_output":
        content = content.replace("alicloud_resource_manager_resource_group.group.id", "other.group.id")
    elif damage == "extra_resource":
        content += '\nresource "alicloud_resource_manager_resource_group" "other" {}'
    elif damage == "extra_module":
        content += '\nmodule "remote" { source = "foreign" }'
    elif damage == "provider":
        content = content.replace("1.140.0", "1.141.0")
    else:
        content = content.replace("cn-hangzhou", "cn-beijing")
    template["Workspace"]["main.tf"] = content
    grants[1]["call"]["params"]["TemplateBody"] = json.dumps(template)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)


def test_group_hosted_real_output_can_bind_without_native_resource_rows(tmp_path, monkeypatch):
    controlled = setup_group_gate(tmp_path, monkeypatch, "Terraform")
    group_proof(
        controlled,
        "GetStack",
        {
            "RequestId": "offline-status",
            "StackId": "stack-own",
            "StackName": "offline-stack",
            "RegionId": "cn-hangzhou",
            "Status": "CREATE_COMPLETE",
            "StackType": "Terraform",
            "Outputs": [{"OutputKey": "group_id", "OutputValue": "rg-offline123"}],
        },
    )
    group_proof(
        controlled,
        "GetResourceGroup",
        {
            "RequestId": "offline-details",
            "ResourceGroup": {
                "Id": "rg-offline123",
                "Name": "offlinegrp",
                "DisplayName": "offline-group",
                "AccountId": "123",
                "Status": "OK",
            },
        },
    )
    gate.cleanup(controlled[0], "0", group_scope("Terraform"), "ws~group-full")
    group_proof(controlled, "ListResources", empty_member_body())
    gate.finish(group_delete(controlled), {"RequestId": "offline-delete"}, True, status=200)
    assert counts(controlled[0])[0]["delete"] == 1


@pytest.mark.parametrize(
    "damage", [None, "stack_id", "status", "header", "call", "identity", "signer", "missing_read", "newer_invalid"]
)
def test_previous_dimension_uses_original_group_transport_envelope_not_outer_status(tmp_path, monkeypatch, damage):
    from scripts.web.e2e.resource_link_workflow.files import json_write
    from scripts.web.e2e.resource_link_workflow.runtime import Runtime
    from scripts.web.e2e.resource_link_workflow.store import Store

    controlled = setup_group_gate(tmp_path, monkeypatch, "ROS")
    path, credential, clock, _ = controlled
    group_source_proofs(controlled)
    gate.cleanup(path, "0", group_scope("ROS"), "ws~group-full")
    group_proof(controlled, "ListResources", empty_member_body())
    gate.finish(group_delete(controlled), {"RequestId": "offline-delete"}, True, status=200)
    group_proof(
        controlled, "GetStack", {"RequestId": "offline-final", "StackId": "stack-own", "Status": "DELETE_COMPLETE"}
    )
    clock[0] += 31
    gate.finish(
        gate.reserve(
            group_call("ListStacks", StackName=["offline-stack"], RegionId="cn-hangzhou"),
            "ws~group-full",
            credential,
            read_only=True,
        ),
        {"RequestId": "offline-no-stack", "TotalCount": 0, "Stacks": []},
        True,
        status=200,
    )
    group_proof(
        controlled,
        "ListResourceGroups",
        {
            "RequestId": "offline-pending",
            "TotalCount": 1,
            "PageNumber": 1,
            "PageSize": 10,
            "ResourceGroups": {
                "ResourceGroup": [{"Id": "rg-offline123", "AccountId": "123", "Status": "PendingDelete"}]
            },
        },
    )
    with sqlite3.connect(path) as db:
        # The actual original STS response is needed after the active slot moves
        # on to another dimension. Never rely on that next scope's identity.
        db.execute(
            "UPDATE attempts SET response=? WHERE grant_id='identity'",
            (json.dumps({"AccountId": "123", "RequestId": "offline-signed"}),),
        )
        db.execute("UPDATE active SET scope='next-dimension',account='another-account'")
        record = db.execute("SELECT id,response FROM attempts WHERE grant_id='status-cleanup'").fetchone()
        envelope = json.loads(record[1])
        if damage == "stack_id":
            envelope["body"]["StackId"] = "other-stack"
        elif damage == "status":
            envelope["body"]["Status"] = "DELETE_IN_PROGRESS"
        elif damage == "header":
            envelope["headers"]["x-acs-request-id"] = "conflict"
        elif damage == "call":
            envelope["call"]["params"]["StackId"] = "other-stack"
        elif damage == "identity":
            db.execute("UPDATE identities SET account='foreign'")
        elif damage == "signer":
            db.execute("UPDATE attempts SET credential='foreign' WHERE grant_id='status-cleanup'")
        elif damage == "missing_read":
            db.execute("DELETE FROM attempts WHERE grant_id='group-absent'")
        elif damage == "newer_invalid":
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,credential,state,response,created) "
                "SELECT 'new-invalid',scope,grant_id,session,credential,state,'{}',created+1 "
                "FROM attempts WHERE id=?",
                (record[0],),
            )
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), record[0]))
    root = tmp_path / "workflow"
    store = Store.initialize(root)
    (root / "execution-gate.sqlite3").write_bytes(path.read_bytes())
    json_write(root / "jobs/offline-group/output/ROS-cleanup-source.json", {"original": True})
    before = (root / "execution-gate.sqlite3").read_bytes()
    result = Runtime(store).dimension_transition_admission("offline-group", {"requests": {"CREATE": "approved"}})
    assert result["remaining_dimensions"]["CREATE"]["previous_cleanup_execution_complete"] is (damage is None)
    assert result["physical_absence_verified"] is False
    assert result["cleanup_accepted"] is False
    assert (root / "execution-gate.sqlite3").read_bytes() == before


# NAS managed-child capability tests use only fake wire responses and isolated SQLite.
def nas_entries():
    hcl = """terraform { required_providers { alicloud = { source = "aliyun/alicloud" version = "= 1.260.0" } } }
provider "alicloud" { region = "cn-hangzhou" }
resource "alicloud_nas_file_system" "filesystem" {
 file_system_type = "standard" protocol_type = "NFS" storage_type = "Performance"
 zone_id = "cn-hangzhou-f" description = "offline-empty"
}
resource "alicloud_nas_mount_target" "mount_target" {
 file_system_id = alicloud_nas_file_system.filesystem.id access_group_name = "offline-restricted"
 network_type = "Vpc" vpc_id = "vpc-offline" vswitch_id = "vsw-offline"
}
output "file_system_id" { value = alicloud_nas_file_system.filesystem.id }
output "mount_target_domain" { value = alicloud_nas_mount_target.mount_target.mount_target_domain }"""
    template = json.dumps(
        {"ROSTemplateFormatVersion": "2015-09-01", "Transform": "Aliyun::Terraform-v1.5", "Workspace": {"main.tf": hcl}}
    )
    grants = entries()
    grants[0]["quota"] = 1
    grants[0]["call"]["version"] = "2015-04-01"
    grants[1]["call"]["params"] = {
        "RegionId": "cn-hangzhou",
        "StackName": "offline-nas",
        "TemplateBody": template,
        "TimeoutInMinutes": 10,
        "DisableRollback": False,
    }
    for grant in grants[1:]:
        grant["call"]["version"] = "2019-09-10"
        grant["call"]["params"]["RegionId"] = "cn-hangzhou"
    grants[2]["call"]["params"]["RetainAllResources"] = False
    for grant in grants[3:5]:
        grant["call"]["params"]["OutputOption"] = "Enabled"
    grants[5]["call"]["params"].update(StackName=["offline-nas"], PageNumber=1, PageSize=10)
    grants.append(
        {
            "id": "precheck",
            "kind": "read",
            "quota": 1,
            "call": copy.deepcopy(grants[5]["call"]),
            "read_policy": {"phase": "execute"},
        }
    )
    grants.append(
        {
            "id": "member-source",
            "kind": "read",
            "quota": 2,
            "call": {
                "product": "ros",
                "action": "ListStackResources",
                "version": "2019-09-10",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "$created_id", "RegionId": "cn-hangzhou"},
            },
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "read_policy": {"phase": "execute"},
        }
    )
    for child in ("filesystem", "mount_target"):
        for phase in ("execute", "cleanup"):
            grants.append(
                {
                    "id": child + "-" + phase,
                    "kind": "read",
                    "quota": 1,
                    "parent": "create",
                    "target_param": "FileSystemId",
                    "response_path": {
                        "kind": "nas_managed_child",
                        "child": child,
                        "filesystem_logical_id": "filesystem",
                        "mount_target_logical_id": "mount_target",
                        "stack_status_grant": "status-source",
                        "stack_resources_grant": "member-source",
                    },
                    "call": {
                        "product": "nas",
                        "action": "DescribeFileSystems" if child == "filesystem" else "DescribeMountTargets",
                        "version": "2017-06-26",
                        "region_id": "cn-hangzhou",
                        "params": {
                            "FileSystemId": "$created_id",
                            "PageNumber": 1,
                            "PageSize": 10,
                            **(
                                {"MountTargetDomain": "$created_mount_target_domain"} if child == "mount_target" else {}
                            ),
                        },
                    },
                    "read_policy": {
                        "phase": phase,
                        **({"required_cleanup": True, "after_delete": "create"} if phase == "cleanup" else {}),
                    },
                }
            )
    return grants


def nas_setup(tmp_path, monkeypatch):
    path = tmp_path / "nas.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    grants = nas_entries()
    gate.validate_grants(grants, require_read_policy=True)
    gate.install(path, "0", "offline-nas:TF", "ws~nas-full", "123", None, grants)
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    setup = path, credential, clock, grants
    nas_perform(setup, "identity", {"AccountId": "123", "RequestId": "offline-identity"})
    nas_perform(
        setup,
        "precheck",
        {"RequestId": "offline-precheck", "Stacks": [], "TotalCount": 0, "PageNumber": 1, "PageSize": 10},
    )
    nas_perform(setup, "create", {"RequestId": "offline-created", "StackId": "stack-own"})
    return setup


def nas_actual(grants, grant_id):
    grant = next(g for g in grants if g["id"] == grant_id)
    value = copy.deepcopy(grant["call"])
    for key, replacement in (
        ("StackId", "stack-own"),
        ("FileSystemId", "offlinefs"),
        ("MountTargetDomain", "offlinefs-target.cn-hangzhou.nas.aliyuncs.com"),
    ):
        if key in value["params"]:
            value["params"][key] = replacement
    return value


def nas_perform(setup, grant_id, body, *, success=True, status=200):
    _, credential, clock, grants = setup
    clock[0] += 31
    grant = next(g for g in grants if g["id"] == grant_id)
    ticket = gate.reserve(
        nas_actual(grants, grant_id),
        "ws~nas-full",
        credential,
        read_only=grant["kind"] in {"read", "identity"},
        tool_use_id=grant_id + "-tool",
    )
    gate.finish(ticket, body, success, status=status)
    return ticket


def nas_proofs(setup, status_field="ResourceStatus"):
    nas_perform(
        setup,
        "status-source",
        {
            "RequestId": "offline-status",
            "StackId": "stack-own",
            "StackName": "offline-nas",
            "RegionId": "cn-hangzhou",
            "AccountId": "123",
            "StackType": "Terraform",
            "Status": "CREATE_COMPLETE",
        },
    )
    nas_perform(
        setup,
        "member-source",
        {
            "RequestId": "offline-members",
            "Resources": [
                {
                    "StackId": "stack-own",
                    "LogicalResourceId": "filesystem",
                    "ResourceType": "alicloud_nas_file_system",
                    "PhysicalResourceId": "offlinefs",
                    status_field: "CREATE_COMPLETE",
                },
                {
                    "StackId": "stack-own",
                    "LogicalResourceId": "mount_target",
                    "ResourceType": "alicloud_nas_mount_target",
                    "PhysicalResourceId": "offlinefs:offlinefs-target.cn-hangzhou.nas.aliyuncs.com",
                    status_field: "CREATE_COMPLETE",
                },
            ],
        },
    )


@pytest.mark.parametrize("status_field", ["Status", "ResourceStatus"])
def test_nas_actual_member_shape_binds_original_children_even_without_survival_reads(
    tmp_path,
    monkeypatch,
    status_field,
):
    setup = nas_setup(tmp_path, monkeypatch)
    path, _, _, _ = setup
    nas_proofs(setup, status_field)
    original_records = gate.creations(path, ["offline-nas"])
    assert len(original_records) == 3 and all(row["state"] == "succeeded" for row in original_records)
    # An earlier pre-transport denied survival read creates no prerequisite or
    # new quota. Cleanup reuses the same signed pre-delete ROS member facts.
    gate.cleanup(path, "0", "offline-nas:TF", "ws~nas-full")
    nas_perform(setup, "delete", {"RequestId": "offline-delete"})
    nas_perform(
        setup,
        "filesystem-cleanup",
        {
            "RequestId": "offline-empty",
            "FileSystems": {"FileSystem": []},
            "TotalCount": 0,
        },
    )
    nas_perform(
        setup,
        "mount_target-cleanup",
        {
            "RequestId": "offline-empty-mount",
            "MountTargets": {"MountTarget": []},
            "TotalCount": 0,
        },
    )
    assert gate.creations(path, ["offline-nas"]) == original_records
    assert counts(path)[0]["create"] == counts(path)[0]["delete"] == 1


@pytest.mark.parametrize("status_field", ["Status", "ResourceStatus"])
@pytest.mark.parametrize("damage", [None, "member_status", "member_stack", "delete_body", "delete_credential"])
def test_nas_readonly_recovery_projects_only_original_children_without_writes(
    tmp_path,
    monkeypatch,
    status_field,
    damage,
):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import inspect_candidate, readonly_review_fields

    setup = nas_setup(tmp_path, monkeypatch)
    path, _, _, grants = setup
    nas_proofs(setup, status_field)
    gate.cleanup(path, "0", "offline-nas:TF", "ws~nas-full")
    nas_perform(setup, "delete", {"RequestId": "offline-delete"})
    with gate.database(path) as db:
        if damage in {"member_status", "member_stack"}:
            row = db.execute("SELECT id,response FROM attempts WHERE grant_id='member-source'").fetchone()
            envelope = json.loads(row["response"])
            child = envelope["body"]["Resources"][1]
            child[status_field if damage == "member_status" else "StackId"] = "foreign"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
        elif damage == "delete_body":
            db.execute("UPDATE attempts SET response='{}' WHERE grant_id='delete'")
        elif damage == "delete_credential":
            db.execute("UPDATE attempts SET credential='foreign' WHERE grant_id='delete'")
        authority = db.execute("SELECT authority FROM grants WHERE id='create'").fetchone()[0]
    before = path.read_bytes()
    args = {
        "slot": "0",
        "scope": "offline-nas:TF",
        "session": "ws~nas-full",
        "account": "123",
        "grants": grants,
        "authority": authority,
    }
    if damage:
        with pytest.raises(WorkflowError):
            inspect_candidate(path, **args)
    else:
        candidate = inspect_candidate(path, **args)
        fields = readonly_review_fields(candidate)
        selected = {call["id"]: call for call in fields["cleanup_read_calls"]}
        assert selected["filesystem-cleanup"]["call"]["params"]["FileSystemId"] == "offlinefs"
        assert selected["mount_target-cleanup"]["call"]["params"]["MountTargetDomain"] == (
            "offlinefs-target.cn-hangzhou.nas.aliyuncs.com"
        )
        assert "filesystem-execute" not in selected and "mount_target-execute" not in selected
        assert len(candidate["owned_from_wire"]) == 1  # Root is the sole write parent; children share its delete.
        assert not candidate["cloud_call_performed"] and not candidate["cleanup_confirmed"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("child", [0, 1])
@pytest.mark.parametrize("damage", ["conflicting_alias", "missing_status", "failed_status"])
def test_nas_actual_status_never_overrides_conflicting_or_missing_original_status(
    tmp_path,
    monkeypatch,
    child,
    damage,
):
    setup = nas_setup(tmp_path, monkeypatch)
    path, credential, _, grants = setup
    nas_proofs(setup, "Status")
    with gate.database(path) as db:
        row = db.execute("SELECT * FROM attempts WHERE grant_id='member-source'").fetchone()
        envelope = json.loads(row["response"])
        member = envelope["body"]["Resources"][child]
        if damage == "conflicting_alias":
            member["ResourceStatus"] = "DELETE_COMPLETE"
        elif damage == "missing_status":
            del member["Status"]
        else:
            member["Status"] = "CREATE_FAILED"
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
    original = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(nas_actual(grants, "filesystem-execute"), "ws~nas-full", credential, read_only=True)
    assert counts(path) == original


def test_nas_exact_children_survive_and_keep_three_owners_after_stack_deletion(tmp_path, monkeypatch):
    setup = nas_setup(tmp_path, monkeypatch)
    path, _, _, _ = setup
    unresolved = gate.creations(path, ["offline-nas"])
    assert unresolved[0]["resource"]["id"] == "stack-own"
    assert unresolved[1]["managed_child"] is True and unresolved[1]["state"] == "unknown"
    nas_proofs(setup)
    nas_perform(
        setup,
        "filesystem-execute",
        {"RequestId": "offline-fs", "FileSystems": {"FileSystem": [{"FileSystemId": "offlinefs"}]}, "TotalCount": 1},
    )
    nas_perform(
        setup,
        "mount_target-execute",
        {
            "RequestId": "offline-mount",
            "MountTargets": {"MountTarget": [{"MountTargetDomain": "offlinefs-target.cn-hangzhou.nas.aliyuncs.com"}]},
            "TotalCount": 1,
        },
    )
    records = gate.creations(path, ["offline-nas"])
    assert len(records) == 3 and [r.get("managed_child", False) for r in records] == [False, True, True]
    assert set(records[0]) == {
        "attempt_id",
        "job_id",
        "dimension",
        "session_id",
        "reference_scope",
        "tool_use_id",
        "state",
        "response",
        "resource",
    }
    assert records[0]["response"] == {"RequestId": "offline-created", "StackId": "stack-own"}
    assert records[1]["resource"] == {
        "id": "offlinefs",
        "product": "nas",
        "resource_type": "alicloud_nas_file_system",
        "region": "cn-hangzhou",
        "account": "123",
    }
    assert records[2]["resource"]["id"] == "offlinefs:offlinefs-target.cn-hangzhou.nas.aliyuncs.com"
    assert records[2]["resource"]["filesystem_id"] == "offlinefs"
    assert records[1]["parent_attempt_id"] == records[2]["parent_attempt_id"] == records[0]["attempt_id"]
    assert records[1]["member_attempt_id"] == records[2]["member_attempt_id"]
    assert records[2]["tool_use_id"] == "member-source-tool"
    assert records[2]["response"]["body"]["RequestId"] == "offline-members"
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        nas_perform(setup, "filesystem-cleanup", {})
    assert counts(path) == before
    gate.cleanup(path, "0", "offline-nas:TF", "ws~nas-full")
    with pytest.raises(gate.GateDeniedError):
        nas_perform(setup, "mount_target-cleanup", {})
    nas_perform(setup, "delete", {"RequestId": "offline-deleted"})
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
            "PageNumber": 1,
            "PageSize": 10,
        },
    )
    nas_perform(
        setup,
        "mount_target-cleanup",
        {"Code": "InvalidFileSystem.NotFound", "RequestId": "offline-mount-gone"},
        success=False,
        status=404,
    )
    # Honest transport stays unknown; neither generic 404 nor export is an absence verdict.
    with gate.database(path) as db:
        read = db.execute("SELECT state,response FROM attempts WHERE grant_id='mount_target-cleanup'").fetchone()
        assert read["state"] == "unknown"
        assert json.loads(read["response"])["status"] == 404
        assert json.loads(read["response"])["call"]["params"]["FileSystemId"] == "offlinefs"
        assert json.loads(read["response"])["body"]["Code"] == "InvalidFileSystem.NotFound"
    assert gate.creations(path, ["offline-nas"]) == records
    assert counts(path)[0]["create"] == counts(path)[0]["delete"] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "missing_status",
        "missing_member",
        "unknown_member",
        "newer_unknown",
        "duplicate",
        "foreign_fs",
        "foreign_domain",
        "foreign_stack",
        "foreign_region",
        "foreign_account",
        "foreign_logical",
        "foreign_type",
        "failed_member",
        "wrong_stack_type",
        "wrong_stack_status",
        "wrong_stack_name",
        "wrong_status_region",
        "wrong_status_account",
        "wire_call",
        "header",
        "transport",
        "missing_request",
        "pagination",
        "session",
        "signer",
        "identity",
        "identity_body",
        "identity_request",
        "tool_id",
        "reference_scope",
    ],
)
def test_nas_children_reject_foreign_incomplete_or_stale_wire_without_consuming_quota(tmp_path, monkeypatch, damage):
    setup = nas_setup(tmp_path, monkeypatch)
    path, credential, clock, grants = setup
    nas_proofs(setup)
    status_damage = damage.startswith("wrong_") or damage == "missing_status"
    grant_id = "status-source" if status_damage else "member-source"
    with gate.database(path) as db:
        row = db.execute("SELECT * FROM attempts WHERE grant_id=?", (grant_id,)).fetchone()
        envelope = json.loads(row["response"])
        body = envelope["body"]
        if damage in {"missing_status", "missing_member"}:
            db.execute("DELETE FROM attempts WHERE id=?", (row["id"],))
        elif damage == "unknown_member":
            db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (row["id"],))
        elif damage == "newer_unknown":
            clock[0] += 1
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                "SELECT 'newer',scope,grant_id,session,'unknown',response,credential,? FROM attempts WHERE id=?",
                (clock[0], row["id"]),
            )
        elif status_damage:
            field = {
                "wrong_stack_type": "StackType",
                "wrong_stack_status": "Status",
                "wrong_stack_name": "StackName",
                "wrong_status_region": "RegionId",
                "wrong_status_account": "AccountId",
            }[damage]
            body[field] = "foreign"
        elif damage == "duplicate":
            body["Resources"].append(copy.deepcopy(body["Resources"][0]))
        elif damage.startswith("foreign_") or damage == "failed_member":
            field = {
                "foreign_fs": "PhysicalResourceId",
                "foreign_domain": "PhysicalResourceId",
                "foreign_stack": "StackId",
                "foreign_region": "RegionId",
                "foreign_account": "AccountId",
                "foreign_logical": "LogicalResourceId",
                "foreign_type": "ResourceType",
                "failed_member": "ResourceStatus",
            }[damage]
            body["Resources"][1 if damage == "foreign_domain" else 0][field] = "foreign"
        elif damage == "wire_call":
            envelope["call"]["params"]["StackId"] = "foreign"
        elif damage == "header":
            envelope["headers"]["x-acs-request-id"] = "foreign"
        elif damage == "transport":
            envelope["status"] = 403
        elif damage == "missing_request":
            body.pop("RequestId")
        elif damage == "pagination":
            body["NextToken"] = "unread"
        elif damage in {"identity_body", "identity_request"}:
            identity = db.execute("SELECT id,response FROM attempts WHERE grant_id='identity'").fetchone()
            identity_body = json.loads(identity["response"])
            if damage == "identity_body":
                identity_body["AccountId"] = "foreign"
            else:
                identity_body.pop("RequestId")
            db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(identity_body), identity["id"]))
        elif damage in {"tool_id", "reference_scope"}:
            field = "tool_use_id" if damage == "tool_id" else "reference_scope"
            db.execute(
                "UPDATE attempts SET " + field + "=? WHERE id=?", ("" if damage == "tool_id" else "foreign", row["id"])
            )
        elif damage == "session":
            db.execute("UPDATE attempts SET session='foreign' WHERE id=?", (row["id"],))
        elif damage == "signer":
            db.execute("UPDATE attempts SET credential='foreign' WHERE id=?", (row["id"],))
        else:
            db.execute("UPDATE identities SET account='foreign'")
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(nas_actual(grants, "mount_target-execute"), "ws~nas-full", credential, read_only=True)
    assert counts(path) == before
    records = gate.creations(path, ["offline-nas"])
    assert not any(record.get("managed_child") and record["state"] == "succeeded" for record in records)


@pytest.mark.parametrize(
    "damage",
    [
        "kind",
        "parent",
        "target",
        "path",
        "logical",
        "product",
        "action",
        "region",
        "version",
        "quota",
        "literal_fs",
        "literal_domain",
        "arbitrary_marker",
        "extra_filter",
        "read_phase",
        "cleanup_required",
        "cleanup_delete",
        "missing_child",
        "duplicate_child",
        "missing_stack_cleanup",
        "unbound_status",
        "retain",
        "duplicate_delete",
        "missing_precheck",
        "template_extra",
        "template_constant",
        "template_provider",
    ],
)
def test_nas_binding_schema_never_admits_arbitrary_ids_paths_templates_or_writes(damage):
    grants = nas_entries()
    child = next(g for g in grants if g["id"] == "mount_target-cleanup")
    if damage in {"kind", "parent", "target"}:
        child[{"kind": "kind", "parent": "parent", "target": "target_param"}[damage]] = "foreign"
    elif damage == "path":
        child["response_path"]["kind"] = "arbitrary"
    elif damage == "logical":
        child["response_path"]["filesystem_logical_id"] = "other"
    elif damage in {"product", "action", "region", "version"}:
        child["call"][{"region": "region_id"}.get(damage, damage)] = "foreign"
    elif damage == "quota":
        child["quota"] = 2
    elif damage in {"literal_fs", "literal_domain", "arbitrary_marker", "extra_filter"}:
        key = {
            "literal_fs": "FileSystemId",
            "literal_domain": "MountTargetDomain",
            "arbitrary_marker": "MountTargetDomain",
            "extra_filter": "VpcId",
        }[damage]
        child["call"]["params"][key] = "$arbitrary" if damage == "arbitrary_marker" else "foreign"
    elif damage in {"read_phase", "cleanup_required", "cleanup_delete"}:
        child["read_policy"][
            {"read_phase": "phase", "cleanup_required": "required_cleanup", "cleanup_delete": "after_delete"}[damage]
        ] = False
    elif damage in {"missing_child", "duplicate_child"}:
        if damage == "missing_child":
            grants.remove(child)
        else:
            duplicate = copy.deepcopy(child)
            duplicate["id"] = "other-child"
            grants.append(duplicate)
    elif damage == "missing_stack_cleanup":
        grants[:] = [g for g in grants if g["id"] != "status-cleanup"]
    elif damage == "unbound_status":
        grants[3]["response_path"] = ["Other"]
    elif damage == "retain":
        grants[2]["call"]["params"]["RetainAllResources"] = True
    elif damage == "duplicate_delete":
        grants.append({**copy.deepcopy(grants[2]), "id": "other-delete"})
    elif damage == "missing_precheck":
        grants[:] = [g for g in grants if g["id"] != "precheck"]
    else:
        template = json.loads(grants[1]["call"]["params"]["TemplateBody"])
        hcl = template["Workspace"]["main.tf"]
        if damage == "template_extra":
            hcl += '\nresource "alicloud_nas_file_system" "other" {}'
        elif damage == "template_constant":
            hcl = hcl.replace("alicloud_nas_file_system.filesystem.id", '"foreignfs"')
        else:
            hcl = hcl.replace("1.260.0", "1.261.0")
        template["Workspace"]["main.tf"] = hcl
        grants[1]["call"]["params"]["TemplateBody"] = json.dumps(template)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)


@pytest.mark.parametrize("damage", ["unknown", "http404", "session", "signer", "call", "account"])
def test_nas_cleanup_requires_original_successful_delete_generation(tmp_path, monkeypatch, damage):
    setup = nas_setup(tmp_path, monkeypatch)
    path, credential, _, grants = setup
    nas_proofs(setup)
    gate.cleanup(path, "0", "offline-nas:TF", "ws~nas-full")
    nas_perform(setup, "delete", {"RequestId": "offline-deleted"})
    with gate.database(path) as db:
        row = db.execute("SELECT * FROM attempts WHERE grant_id='delete'").fetchone()
        envelope = json.loads(row["response"])
        if damage == "unknown":
            db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (row["id"],))
        elif damage == "http404":
            envelope["status"] = 404
        elif damage in {"session", "signer"}:
            db.execute(
                "UPDATE attempts SET " + ("credential" if damage == "signer" else damage) + "='foreign' WHERE id=?",
                (row["id"],),
            )
        elif damage == "call":
            envelope["call"]["params"]["StackId"] = "foreign"
        else:
            db.execute("UPDATE active SET account='foreign'")
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(nas_actual(grants, "filesystem-cleanup"), "ws~nas-full", credential, read_only=True)
    assert counts(path) == before


def test_nas_export_uses_creator_proof_after_slot_moves_and_root_selection_excludes_children(tmp_path, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.runtime import Runtime
    from scripts.web.e2e.resource_link_workflow.store import Store

    setup = nas_setup(tmp_path, monkeypatch)
    path, _, _, _ = setup
    nas_proofs(setup)
    records = gate.creations(path, ["offline-nas"])
    with gate.database(path) as db:
        db.execute("UPDATE active SET scope='other',session='other',account='other'")
    assert gate.creations(path, ["offline-nas"]) == records
    root = tmp_path / "workflow"
    store = Store.initialize(root)
    (root / "execution-gate.sqlite3").write_bytes(path.read_bytes())
    (root / "execution-gate.sqlite3").chmod(0o600)
    ref = {
        "stackId": "stack-own",
        "regionId": "cn-hangzhou",
        "scopeKind": "regional",
        "accountScope": records[0]["reference_scope"] or records[0]["resource"]["account"],
    }
    assert Runtime(store).created_references("offline-nas", "TF", "ws~nas-full", [ref]) == [ref]
    # Query-only samples cannot export an owner, and the existing non-NAS schema is unchanged.
    assert gate.creations(path, ["query-only"]) == []


@pytest.mark.parametrize("damage", ["missing", "unknown", "http403", "nonempty", "request", "next_page", "bool_count"])
def test_nas_create_requires_actual_complete_same_signer_name_precheck(tmp_path, monkeypatch, damage):
    setup = nas_setup(tmp_path, monkeypatch)
    path, credential, _, grants = setup
    # Rewind only this offline fixture's create to exercise the pre-wire guard.
    with gate.database(path) as db:
        db.execute("DELETE FROM attempts WHERE grant_id='create'")
        db.execute("UPDATE grants SET used=0 WHERE id='create'")
        db.execute("UPDATE authority_limits SET used=0 WHERE id='create'")
        row = db.execute("SELECT * FROM attempts WHERE grant_id='precheck'").fetchone()
        envelope = json.loads(row["response"])
        if damage == "missing":
            db.execute("DELETE FROM attempts WHERE id=?", (row["id"],))
        elif damage == "unknown":
            db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (row["id"],))
        elif damage == "http403":
            envelope["status"] = 403
        elif damage == "nonempty":
            envelope["body"].update(TotalCount=1, Stacks=[{"StackId": "foreign"}])
        elif damage == "request":
            envelope["call"]["params"]["StackName"] = ["foreign"]
        elif damage == "next_page":
            envelope["body"]["NextToken"] = "unread"
        else:
            envelope["body"]["TotalCount"] = False
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), row["id"]))
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(nas_actual(grants, "create"), "ws~nas-full", credential, read_only=False)
    assert counts(path) == before


def test_nas_cleanup_freezes_original_members_and_never_spends_execute_reads(tmp_path, monkeypatch):
    setup = nas_setup(tmp_path, monkeypatch)
    path, _, clock, _ = setup
    nas_proofs(setup)
    records = gate.creations(path, ["offline-nas"])
    gate.cleanup(path, "0", "offline-nas:TF", "ws~nas-full")
    nas_perform(setup, "delete", {"RequestId": "offline-deleted"})
    with gate.database(path) as db:
        # Later observations cannot replace the already frozen original member generation.
        db.execute(
            "INSERT INTO attempts(id,scope,grant_id,session,credential,state,response,created) "
            "SELECT 'post-delete',scope,grant_id,session,credential,'unknown','{}',? "
            "FROM attempts WHERE grant_id='member-source'",
            (clock[0] + 1,),
        )
    nas_perform(
        setup,
        "filesystem-cleanup",
        {"RequestId": "offline-absence", "FileSystems": {"FileSystem": []}, "TotalCount": 0},
    )
    assert gate.creations(path, ["offline-nas"]) == records
    assert counts(path)[0]["filesystem-execute"] == counts(path)[0]["mount_target-execute"] == 0


def test_nas_new_signed_credential_cannot_borrow_original_child_generation(tmp_path, monkeypatch):
    setup = nas_setup(tmp_path, monkeypatch)
    path, _, _, grants = setup
    nas_proofs(setup)
    rotated = SimpleNamespace(mode="AK", access_key_id="rotated", access_key_secret="fake-secret")
    import hashlib

    fingerprint = hashlib.sha256(gate.canonical(["AK", "rotated", "fake-secret", None]).encode()).hexdigest()
    with gate.database(path) as db:
        db.execute("INSERT INTO identities VALUES(?,?,?,?)", ("offline-nas:TF", "ws~nas-full", fingerprint, "123"))
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(nas_actual(grants, "filesystem-execute"), "ws~nas-full", rotated, read_only=True)
    assert counts(path) == before


# Independent offline wire fixtures for the reviewed MSE managed template.
MSE_TEST_HCL = """terraform {
  required_providers {
    alicloud = {
      source = "aliyun/alicloud"
      version = "= 1.260.0"
    }
  }
}
provider "alicloud" {
  region = "cn-hangzhou"
}
resource "alicloud_vpc" "sample_vpc" {
  vpc_name = "{name}"
  cidr_block = "192.168.244.0/24"
  enable_ipv6 = false
  force_delete = false
}
resource "alicloud_vswitch" "sample_vswitch" {
  vswitch_name = "{name}"
  vpc_id = alicloud_vpc.sample_vpc.id
  zone_id = "cn-hangzhou-b"
  cidr_block = "192.168.244.0/26"
  enable_ipv6 = false
}
resource "alicloud_mse_cluster" "sample" {
  cluster_alias_name = "{name}"
  cluster_specification = "MSE_SC_1_2_60_c"
  cluster_type = "Nacos-Ans"
  cluster_version = "NACOS_2_0_0"
  instance_count = 1
  mse_version = "mse_dev"
  payment_type = "PayAsYouGo"
  net_type = "privatenet"
  connection_type = "slb"
  pub_network_flow = "0"
  vpc_id = alicloud_vpc.sample_vpc.id
  vswitch_id = alicloud_vswitch.sample_vswitch.id
}
output "sample_instance_id" { value = alicloud_mse_cluster.sample.id }
output "sample_cluster_id" { value = alicloud_mse_cluster.sample.cluster_id }
output "sample_vpc_id" { value = alicloud_vpc.sample_vpc.id }
output "sample_vswitch_id" { value = alicloud_vswitch.sample_vswitch.id }
"""


def mse_call(product, action, version, **params):
    return {"product": product, "action": action, "version": version, "region_id": "cn-hangzhou", "params": params}


def mse_entries(*, professional=False):
    name = "offline-mse"
    hcl = MSE_TEST_HCL
    if professional:
        hcl = hcl.replace("instance_count = 1", "instance_count = 3").replace("mse_dev", "mse_pro")
    bound = {"parent": "mse-create", "response_path": ["StackId"], "target_param": "StackId"}
    grants = [
        {
            "id": "mse-identity",
            "kind": "identity",
            "quota": 1,
            "call": mse_call("sts", "GetCallerIdentity", "2015-04-01"),
        },
        {
            "id": "mse-create",
            "kind": "create",
            "quota": 1,
            "call": mse_call(
                "ros",
                "CreateStack",
                "2019-09-10",
                RegionId="cn-hangzhou",
                StackName=name,
                TimeoutInMinutes=30,
                DisableRollback=False,
                TemplateBody=json.dumps(
                    {
                        "ROSTemplateFormatVersion": "2015-09-01",
                        "Transform": "Aliyun::Terraform-v1.5",
                        "Workspace": {"main.tf": hcl.replace("{name}", name)},
                    }
                ),
            ),
        },
        {
            "id": "mse-delete",
            "kind": "delete",
            "quota": 1,
            **bound,
            "call": mse_call(
                "ros",
                "DeleteStack",
                "2019-09-10",
                RegionId="cn-hangzhou",
                StackId="$created_id",
                RetainAllResources=False,
            ),
        },
        {
            "id": "mse-status",
            "kind": "read",
            "quota": 3,
            **bound,
            "call": mse_call(
                "ros", "GetStack", "2019-09-10", RegionId="cn-hangzhou", StackId="$created_id", OutputOption="Enabled"
            ),
            "read_policy": {"phase": "execute", "min_interval_seconds": 30},
        },
        {
            "id": "mse-members",
            "kind": "read",
            "quota": 3,
            **bound,
            "call": mse_call("ros", "ListStackResources", "2019-09-10", RegionId="cn-hangzhou", StackId="$created_id"),
            "read_policy": {"phase": "execute"},
        },
        {
            "id": "mse-status-cleanup",
            "kind": "read",
            "quota": 3,
            **bound,
            "call": mse_call(
                "ros", "GetStack", "2019-09-10", RegionId="cn-hangzhou", StackId="$created_id", OutputOption="Enabled"
            ),
            "read_policy": {
                "phase": "cleanup",
                "min_interval_seconds": 30,
                "required_cleanup": True,
                "after_delete": "mse-create",
            },
        },
        {
            "id": "mse-name-cleanup",
            "kind": "read",
            "quota": 1,
            "call": mse_call(
                "mse",
                "ListClusters",
                "2019-05-31",
                RegionId="cn-hangzhou",
                ClusterAliasName=name,
                PageNum=1,
                PageSize=10,
            ),
            "read_policy": {"phase": "cleanup", "required_cleanup": True, "after_delete": "mse-create"},
        },
    ]
    children = {
        "cluster": ("mse", "QueryClusterInfo", "2019-05-31", "InstanceId", "sample"),
        "vpc": ("vpc", "DescribeVpcAttribute", "2016-04-28", "VpcId", "sample_vpc"),
        "vswitch": ("vpc", "DescribeVSwitchAttributes", "2016-04-28", "VSwitchId", "sample_vswitch"),
    }
    for child, (product, action, version, parameter, _logical) in children.items():
        binding = {
            "kind": "mse_managed_child",
            "child": child,
            "logical_ids": {c: s[4] for c, s in children.items()},
            "stack_status_grant": "mse-status",
            "stack_resources_grant": "mse-members",
        }
        for phase in ("execute", "cleanup"):
            grants.append(
                {
                    "id": child + "-" + phase,
                    "kind": "read",
                    "quota": 1 if phase == "execute" else 2,
                    "parent": "mse-create",
                    "response_path": binding,
                    "target_param": parameter,
                    "call": mse_call(product, action, version, RegionId="cn-hangzhou", **{parameter: "$created_id"}),
                    "read_policy": {
                        "phase": phase,
                        **(
                            {"required_cleanup": True, "after_delete": "mse-create", "min_interval_seconds": 30}
                            if phase == "cleanup"
                            else {}
                        ),
                    },
                }
            )
        grants.append(
            {
                "id": child + "-resource",
                "kind": "read",
                "quota": 1,
                "parent": "mse-create",
                "response_path": binding,
                "target_param": "LogicalResourceId",
                "call": mse_call(
                    "ros",
                    "GetStackResource",
                    "2019-09-10",
                    RegionId="cn-hangzhou",
                    StackId="$created_stack_id",
                    LogicalResourceId="$created_id",
                    ShowResourceAttributes=True,
                ),
                "read_policy": {"phase": "execute"},
            }
        )
    return grants


def mse_actual(grants, grant_id):
    grant = next(g for g in grants if g["id"] == grant_id)
    value = copy.deepcopy(grant["call"])
    for key, replacement in (
        ("StackId", "mse-stack-own"),
        ("InstanceId", "mse-cn-own"),
        ("VpcId", "vpc-own"),
        ("VSwitchId", "vsw-own"),
    ):
        if key in value["params"]:
            value["params"][key] = replacement
    binding = grant.get("response_path")
    if isinstance(binding, dict) and "LogicalResourceId" in value["params"]:
        value["params"]["LogicalResourceId"] = binding["logical_ids"][binding["child"]]
    return value


def mse_perform(setup, grant_id, body, *, status=200, success=True):
    _, credential, clock, grants = setup
    clock[0] += 31
    grant = next(g for g in grants if g["id"] == grant_id)
    ticket = gate.reserve(
        mse_actual(grants, grant_id),
        "ws~mse-full",
        credential,
        read_only=grant["kind"] in {"identity", "read"},
        tool_use_id=grant_id + "-tool",
    )
    gate.finish(ticket, body, success, status=status)
    return ticket


def mse_setup(tmp_path, monkeypatch, *, create_success=True, professional=False):
    from iac_code.services.providers.aliyun import AliyunCredential

    path = tmp_path / "mse.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    grants = mse_entries(professional=professional)
    gate.validate_grants(grants, require_read_policy=True)
    gate.install(path, "0", "offline-mse:TF", "ws~mse-full", "123", None, grants, authority="offline-mse-authority")
    setup = (path, AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake-secret"), clock, grants)
    mse_perform(setup, "mse-identity", {"AccountId": "123", "RequestId": "offline-identity"})
    mse_perform(
        setup, "mse-create", {"RequestId": "offline-create", "StackId": "mse-stack-own"}, success=create_success
    )
    return setup


def mse_source_bodies(*, status_field="ResourceStatus", nested=False):
    status = {
        "StackId": "mse-stack-own",
        "StackName": "offline-mse",
        "RegionId": "cn-hangzhou",
        "AccountId": "123",
        "StackType": "Terraform",
        "Status": "CREATE_COMPLETE",
        "Outputs": [
            {"OutputKey": key, "OutputValue": value}
            for key, value in (
                ("sample_instance_id", "mse-cn-own"),
                ("sample_cluster_id", "mse-logical-different"),
                ("sample_vpc_id", "vpc-own"),
                ("sample_vswitch_id", "vsw-own"),
            )
        ],
    }
    members = {
        "RequestId": "offline-members",
        "Resources": [
            {
                "StackId": "mse-stack-own",
                "LogicalResourceId": logical,
                "ResourceType": resource_type,
                "PhysicalResourceId": identifier,
                status_field: "CREATE_COMPLETE",
            }
            for logical, resource_type, identifier in (
                ("sample", "alicloud_mse_cluster", "mse-cn-own"),
                ("sample_vpc", "alicloud_vpc", "vpc-own"),
                ("sample_vswitch", "alicloud_vswitch", "vsw-own"),
            )
        ],
    }
    return (
        {"RequestId": "offline-status", "Stack": status} if nested else {"RequestId": "offline-status", **status}
    ), members


def mse_proofs(setup, **kwargs):
    status, members = mse_source_bodies(**kwargs)
    mse_perform(setup, "mse-status", status)
    mse_perform(setup, "mse-members", members)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("status_field", ["Status", "ResourceStatus"])
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_original_hosted_children_bind_exact_reads_and_survive_deletion(
    tmp_path,
    monkeypatch,
    nested,
    status_field,
    professional,
):
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, credential, clock, grants = setup
    unresolved = gate.creations(path, ["offline-mse"])
    assert len(unresolved) == 2 and unresolved[-1]["state"] == "unknown"
    mse_proofs(setup, nested=nested, status_field=status_field)
    for child in ("cluster", "vpc", "vswitch"):
        mse_perform(
            setup,
            child + "-resource",
            {
                "RequestId": "offline-resource",
                "Resource": {
                    "PhysicalResourceId": {"cluster": "mse-cn-own", "vpc": "vpc-own", "vswitch": "vsw-own"}[child],
                },
            },
        )
        mse_perform(
            setup,
            child + "-execute",
            {
                "RequestId": "offline-detail",
                "Success": True,
                "ErrorCode": "mse-100-000",
            }
            if child == "cluster"
            else {"RequestId": "offline-network"},
        )
    records = gate.creations(path, ["offline-mse"])
    assert len(records) == 4 and all(r["state"] == "succeeded" for r in records)
    assert {r["resource"]["id"] for r in records} == {"mse-stack-own", "mse-cn-own", "vpc-own", "vsw-own"}
    assert all("credential" not in r for r in records)
    assert records[1]["parent_stack_id"] == "mse-stack-own"
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(mse_actual(grants, "cluster-cleanup"), "ws~mse-full", credential, read_only=True)
    assert counts(path) == before
    mse_perform(setup, "mse-delete", {"RequestId": "offline-deleted"})
    for child in ("cluster", "vpc", "vswitch"):
        # Keep actual 404 as unknown raw evidence; the gate does not manufacture absence/PASS.
        mse_perform(
            setup, child + "-cleanup", {"RequestId": "offline-notfound", "Code": "NotFound"}, status=404, success=False
        )
    assert gate.creations(path, ["offline-mse"]) == records
    assert counts(path)[0]["mse-create"] == counts(path)[0]["mse-delete"] == 1
    assert counts(path)[0]["mse-identity"] == 1
    with gate.database(path) as db:
        wire = json.loads(db.execute("SELECT response FROM attempts WHERE grant_id='cluster-cleanup'").fetchone()[0])
        assert wire["call"]["params"]["InstanceId"] == "mse-cn-own"
        assert wire["status"] == 404 and wire["body"]["Code"] == "NotFound"
    clock[0] += 31


@pytest.mark.parametrize(
    "damage",
    [
        "extra_member",
        "duplicate_member",
        "foreign_stack",
        "foreign_account",
        "foreign_region",
        "wrong_type",
        "wrong_logical",
        "conflicting_status",
        "missing_status",
        "partial_member",
        "output_mismatch",
        "duplicate_output",
        "wrong_stack_type",
        "wrong_stack_status",
        "truncated",
        "wire_call",
        "wire_header",
        "wire_status",
        "member_session",
        "member_signer",
        "member_scope",
        "member_tool",
        "status_scope",
        "status_tool",
        "parent_session",
        "parent_signer",
        "active_account",
        "signed_account",
        "signed_tool",
        "second_create",
    ],
)
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_tampered_or_partial_lineage_never_consumes_child_or_delete_quota(
    tmp_path,
    monkeypatch,
    damage,
    professional,
):
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, credential, _, grants = setup
    mse_proofs(setup)
    with gate.database(path) as db:
        member = db.execute("SELECT * FROM attempts WHERE grant_id='mse-members'").fetchone()
        status = db.execute("SELECT * FROM attempts WHERE grant_id='mse-status'").fetchone()
        envelope = json.loads(member["response"])
        status_envelope = json.loads(status["response"])
        row = envelope["body"]["Resources"][0]
        if damage == "extra_member":
            envelope["body"]["Resources"].append({"ResourceType": "alicloud_eip_address"})
        elif damage == "duplicate_member":
            envelope["body"]["Resources"][1] = copy.deepcopy(row)
        elif damage in {"foreign_stack", "foreign_account", "foreign_region", "wrong_type", "wrong_logical"}:
            field = {
                "foreign_stack": "StackId",
                "foreign_account": "AccountId",
                "foreign_region": "RegionId",
                "wrong_type": "ResourceType",
                "wrong_logical": "LogicalResourceId",
            }[damage]
            row[field] = "foreign"
        elif damage == "conflicting_status":
            row["Status"] = "DELETE_COMPLETE"
        elif damage == "missing_status":
            del row["ResourceStatus"]
        elif damage == "partial_member":
            envelope["body"]["Resources"].pop()
        elif damage == "output_mismatch":
            status_envelope["body"]["Outputs"][0]["OutputValue"] = "mse-foreign"
        elif damage == "duplicate_output":
            status_envelope["body"]["Outputs"][1] = status_envelope["body"]["Outputs"][0]
        elif damage == "wrong_stack_type":
            status_envelope["body"]["StackType"] = "ROS"
        elif damage == "wrong_stack_status":
            status_envelope["body"]["Status"] = "CREATE_FAILED"
        elif damage == "truncated":
            envelope["body"]["NextToken"] = "more"
        elif damage == "wire_call":
            envelope["call"]["params"]["StackId"] = "foreign"
        elif damage == "wire_header":
            envelope["headers"] = {"x-acs-request-id": "wrong"}
        elif damage == "wire_status":
            envelope["status"] = 403
        elif damage.startswith(("member_", "status_", "parent_")):
            prefix, field = damage.split("_")
            column = {"session": "session", "signer": "credential", "scope": "reference_scope", "tool": "tool_use_id"}[
                field
            ]
            target = {"member": "mse-members", "status": "mse-status", "parent": "mse-create"}[prefix]
            db.execute(
                f"UPDATE attempts SET {column}=? WHERE grant_id=?", ("" if field == "tool" else "foreign", target)
            )
        elif damage == "active_account":
            db.execute("UPDATE active SET account='foreign'")
        elif damage.startswith("signed_"):
            if damage == "signed_tool":
                db.execute("UPDATE attempts SET tool_use_id='' WHERE grant_id='mse-identity'")
            else:
                db.execute(
                    "UPDATE attempts SET response=? WHERE grant_id='mse-identity'",
                    (json.dumps({"AccountId": "foreign", "RequestId": "offline-identity"}),),
                )
        elif damage == "second_create":
            db.execute(
                "INSERT INTO attempts SELECT 'second-create',scope,grant_id,session,state,response,credential,"
                "created+1,reference_scope,tool_use_id FROM attempts WHERE grant_id='mse-create'"
            )
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(envelope), member["id"]))
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(status_envelope), status["id"]))
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(mse_actual(grants, "cluster-execute"), "ws~mse-full", credential, read_only=True)
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(mse_actual(grants, "mse-delete"), "ws~mse-full", credential, read_only=False)
    assert counts(path) == before
    records = gate.creations(path, ["offline-mse"])
    # Active routing damage cannot rewrite valid original ownership facts.
    assert (
        all(r["state"] == "succeeded" for r in records)
        if damage == "active_account"
        else any(r["state"] == "unknown" for r in records)
    )


@pytest.mark.parametrize(
    "damage",
    [
        "mse_wrong_action",
        "mse_wrong_product",
        "mse_wrong_version",
        "mse_extra_param",
        "mse_missing_region",
        "mse_missing_phase",
        "mse_wrong_kind",
        "retained",
        "cleanup_missing",
        "cleanup_not_required",
        "resource_read_missing",
        "extra_resource",
        "public_network",
        "subscription",
        "unpinned",
        "foreign_network",
        "template_url",
        "binding_missing",
        "foreign_logical",
        "extra_workspace",
        "wrong_target",
    ],
)
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_closed_plan_validation_rejects_expansion(tmp_path, damage, professional):
    grants = mse_entries(professional=professional)
    query = next(g for g in grants if g["id"] == "cluster-execute")
    create = next(g for g in grants if g["kind"] == "create")
    if damage.startswith("mse_"):
        if damage == "mse_wrong_action":
            query["call"]["action"] = "QueryAnything"
        elif damage == "mse_wrong_product":
            query["call"]["product"] = "ecs"
        elif damage == "mse_wrong_version":
            query["call"]["version"] = "2099-01-01"
        elif damage == "mse_extra_param":
            query["call"]["params"]["ClusterId"] = "mse-not-instance"
        elif damage == "mse_missing_region":
            del query["call"]["params"]["RegionId"]
        elif damage == "mse_missing_phase":
            del query["read_policy"]
        else:
            query["kind"] = "create"
    elif damage == "retained":
        next(g for g in grants if g["kind"] == "delete")["call"]["params"]["RetainAllResources"] = True
    elif damage in {"cleanup_missing", "resource_read_missing"}:
        grants = [g for g in grants if g["id"] != ("vpc-cleanup" if damage == "cleanup_missing" else "vpc-resource")]
    elif damage == "cleanup_not_required":
        next(g for g in grants if g["id"] == "vpc-cleanup")["read_policy"]["required_cleanup"] = False
    elif damage == "binding_missing":
        grants = [g for g in grants if not isinstance(g.get("response_path"), dict)]
    elif damage == "foreign_logical":
        query["response_path"]["logical_ids"]["cluster"] = "foreign"
    elif damage == "wrong_target":
        query["target_param"] = "ClusterId"
    else:
        template = json.loads(create["call"]["params"]["TemplateBody"])
        content = template["Workspace"]["main.tf"]
        if damage == "template_url":
            create["call"]["params"]["TemplateURL"] = "https://example.invalid"
        elif damage == "extra_workspace":
            template["Workspace"]["other.tf"] = 'resource "alicloud_eip_address" "extra" {}'
        else:
            changes = {
                "extra_resource": (content, content + '\nresource "alicloud_eip_address" "extra" {}\n'),
                "public_network": ('pub_network_flow = "0"', 'pub_network_flow = "1"'),
                "subscription": ('payment_type = "PayAsYouGo"', 'payment_type = "Subscription"'),
                "unpinned": ('version = "= 1.260.0"', 'version = ">= 1.260.0"'),
                "foreign_network": ("vpc_id = alicloud_vpc.sample_vpc.id", 'vpc_id = "vpc-business"'),
            }
            old, new = changes[damage]
            template["Workspace"]["main.tf"] = content.replace(old, new)
        create["call"]["params"]["TemplateBody"] = json.dumps(template)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)


@pytest.mark.parametrize(
    "damage",
    [
        "session",
        "signer",
        "target",
        "read_only",
        "phase",
        "delete_failed",
        "delete_signer",
        "delete_session",
        "late_member",
        "late_status",
    ],
)
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_phase_signer_target_and_delete_ordering_are_enforced(tmp_path, monkeypatch, damage, professional):
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, credential, _, grants = setup
    mse_proofs(setup)
    session = "ws~mse-full"
    request = mse_actual(grants, "cluster-execute")
    read_only = True
    if damage == "session":
        session = "ws~foreign"
    elif damage == "signer":
        credential = SimpleNamespace(mode="AK", access_key_id="rotated", access_key_secret="fake-secret")
    elif damage == "target":
        request["params"]["InstanceId"] = "mse-logical-different"
    elif damage == "read_only":
        read_only = False
    else:
        gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
        if damage != "phase":
            mse_perform(setup, "mse-delete", {"RequestId": "offline-delete"}, success=damage != "delete_failed")
            with gate.database(path) as db:
                if damage in {"delete_signer", "delete_session"}:
                    column = "credential" if damage == "delete_signer" else "session"
                    db.execute(f"UPDATE attempts SET {column}='foreign' WHERE grant_id='mse-delete'")
                elif damage.startswith("late_"):
                    target = "mse-members" if damage == "late_member" else "mse-status"
                    db.execute(
                        "UPDATE attempts SET created="
                        "(SELECT created+1 FROM attempts WHERE grant_id='mse-delete') "
                        "WHERE grant_id=?",
                        (target,),
                    )
            request = mse_actual(grants, "cluster-cleanup")
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(request, session, credential, read_only=read_only)
    assert counts(path) == before


@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_partial_and_unknown_creates_keep_known_children_and_unresolved_owner(tmp_path, monkeypatch, professional):
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, _, _, _ = setup
    status, members = mse_source_bodies()
    status["Status"] = "CREATE_FAILED"
    members["Resources"] = members["Resources"][1:]
    mse_perform(setup, "mse-status", status)
    mse_perform(setup, "mse-members", members)
    records = gate.creations(path, ["offline-mse"])
    assert {r["resource"]["id"] for r in records} == {"mse-stack-own", "vpc-own", "vsw-own", None}
    assert all(r["state"] == "unknown" for r in records[1:])
    with gate.database(path) as db:
        db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='mse-create'")
    records = gate.creations(path, ["offline-mse"])
    assert {r["resource"]["id"] for r in records} == {"mse-stack-own", "vpc-own", "vsw-own", None}
    assert records[0]["state"] == records[-1]["state"] == "unknown"
    assert records[-1]["resource"]["resource_type"] == "unresolved_managed_children"


def test_mse_query_exception_is_exact_official_read_and_keeps_quota_bounds():
    grants = [
        {"id": "identity", "kind": "identity", "quota": 1, "call": mse_call("sts", "GetCallerIdentity", "2015-04-01")},
        {
            "id": "read",
            "kind": "read",
            "quota": 1,
            "read_policy": {"phase": "execute"},
            "call": mse_call("mse", "QueryClusterInfo", "2019-05-31", RegionId="cn-hangzhou", InstanceId="mse-cn-own"),
        },
    ]
    gate.validate_grants(grants, require_read_policy=True)
    grants[-1]["quota"] = 101
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)


@pytest.mark.parametrize(
    "damage",
    [
        "foreign_read",
        "duplicate_quota",
        "identity_quota",
        "logical_type",
        "output_type",
        "missing_identity_wire",
        "missing_tool",
        "scope_rotation",
    ],
)
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_closed_scope_rejects_undeclared_capability_and_missing_actual_wire(
    tmp_path,
    monkeypatch,
    damage,
    professional,
):
    if damage in {"foreign_read", "duplicate_quota", "identity_quota", "logical_type"}:
        grants = mse_entries(professional=professional)
        if damage == "foreign_read":
            grants.append(
                {
                    "id": "foreign",
                    "kind": "read",
                    "quota": 1,
                    "read_policy": {"phase": "cleanup"},
                    "call": mse_call(
                        "mse", "QueryClusterInfo", "2019-05-31", RegionId="cn-hangzhou", InstanceId="mse-business"
                    ),
                }
            )
        elif damage == "duplicate_quota":
            extra = copy.deepcopy(next(g for g in grants if g["id"] == "mse-status-cleanup"))
            extra["id"] = "extra-status-quota"
            grants.append(extra)
        elif damage == "identity_quota":
            grants[0]["quota"] += 1
        else:
            next(g for g in grants if g["id"] == "cluster-execute")["response_path"]["logical_ids"]["cluster"] = []
        with pytest.raises(gate.GateDeniedError):
            gate.validate_grants(grants, require_read_policy=True)
        return
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, credential, _, grants = setup
    mse_proofs(setup)
    tool = "offline-tool"
    if damage in {"output_type", "missing_identity_wire"}:
        with gate.database(path) as db:
            if damage == "missing_identity_wire":
                db.execute("DELETE FROM attempts WHERE grant_id='mse-identity'")
            else:
                row = db.execute("SELECT * FROM attempts WHERE grant_id='mse-status'").fetchone()
                body = json.loads(row["response"])
                body["body"]["Outputs"][0]["OutputKey"] = []
                db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(body), row["id"]))
    elif damage == "missing_tool":
        tool = ""
    else:
        from iac_code.tools.cloud.aliyun import resource_reference_extractors

        monkeypatch.setattr(
            resource_reference_extractors,
            "credential_reference_context",
            lambda _credential: {"account_scope": "opaque-rotated"},
        )
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(mse_actual(grants, "cluster-execute"), "ws~mse-full", credential, read_only=True, tool_use_id=tool)
    assert counts(path) == before


@pytest.mark.parametrize("damage", [None, "failure", "error_code", "error_code_type", "header", "status", "request_id"])
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_success_envelope_preserves_official_error_code_and_honest_failures(
    tmp_path,
    monkeypatch,
    damage,
    professional,
):
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, credential, clock, grants = setup
    mse_proofs(setup)
    clock[0] += 31
    ticket = gate.reserve(
        mse_actual(grants, "cluster-execute"), "ws~mse-full", credential, read_only=True, tool_use_id="offline-mse-read"
    )
    original = {"RequestId": "offline-detail", "Success": True, "ErrorCode": "mse-100-000"}
    headers = {}
    status = 200
    if damage == "failure":
        original["Success"] = False
    elif damage == "error_code":
        original["ErrorCode"] = "NoPermission"
    elif damage == "error_code_type":
        original["ErrorCode"] = ["malformed"]
    elif damage == "header":
        headers["x-acs-request-id"] = "foreign"
    elif damage == "status":
        status = 403
    elif damage == "request_id":
        del original["RequestId"]
    gate.finish(ticket, original, True, status=status, headers=headers)
    with gate.database(path) as db:
        row = db.execute("SELECT state,response FROM attempts WHERE id=?", (ticket[1],)).fetchone()
        assert row["state"] == ("unknown" if damage else "succeeded")
        stored = json.loads(row["response"])
        assert stored["body"] == original and stored["status"] == status


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("members", ["missing", "empty", "networks"])
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_rollback_complete_root_cleanup_needs_no_successful_child_identity(
    tmp_path,
    monkeypatch,
    nested,
    members,
    professional,
):
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, credential, _, grants = setup
    status, _ = mse_source_bodies(nested=nested)
    stack = status.get("Stack", status)
    stack["Status"] = "CREATE_ROLLBACK_COMPLETE"
    stack.pop("Outputs")
    mse_perform(setup, "mse-status", status)
    if members != "missing":
        _, member_body = mse_source_bodies()
        member_body["Resources"] = member_body["Resources"][1:] if members == "networks" else []
        mse_perform(setup, "mse-members", member_body)
    original = gate.creations(path, ["offline-mse"])
    assert len(original) == (4 if members == "networks" else 2)
    assert all(row["state"] == "unknown" for row in original[1:])
    if members == "networks":
        assert {row["resource"]["id"] for row in original} == {"mse-stack-own", "vpc-own", "vsw-own", None}
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(mse_actual(grants, "mse-delete"), "ws~mse-full", credential, read_only=False)
    assert counts(path) == before  # Execute phase never acquires cleanup write capability.
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    mse_perform(setup, "mse-delete", {"RequestId": "offline-delete-rolled-back"})
    # A root cleanup does not manufacture member IDs, release unknown ownership,
    # replenish an exhausted operation, or grant a guessed child read.
    assert gate.creations(path, ["offline-mse"]) == original
    for grant_id in ("mse-delete", "cluster-cleanup", "vpc-cleanup", "vswitch-cleanup"):
        before = counts(path)
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(
                mse_actual(grants, grant_id),
                "ws~mse-full",
                credential,
                read_only=grant_id != "mse-delete",
                tool_use_id="offline-no-extra",
            )
        assert counts(path) == before
    mse_perform(
        setup,
        "mse-status-cleanup",
        {"RequestId": "offline-status-deleted", "StackId": "mse-stack-own", "Status": "DELETE_COMPLETE"},
    )
    assert counts(path)[0]["mse-delete"] == counts(path)[0]["mse-create"] == counts(path)[0]["mse-identity"] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "missing_status",
        "creating",
        "create_failed",
        "rollback_failed",
        "delete_complete",
        "foreign_stack",
        "foreign_name",
        "foreign_region",
        "foreign_account",
        "wrong_type",
        "nested_identity",
        "nested_status",
        "outer_account",
        "stack_status",
        "wire_call",
        "wire_status",
        "wire_header",
        "status_session",
        "status_signer",
        "status_scope",
        "status_tool",
        "status_unknown",
        "parent_unknown",
        "parent_response",
        "identity_missing",
        "latest_incomplete",
        "latest_unknown",
        "late_status",
        "request_target",
        "current_signer",
        "source_phase",
        "source_parent",
    ],
)
@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
def test_mse_rollback_root_cleanup_rejects_unproved_or_changed_generation(tmp_path, monkeypatch, damage, professional):
    setup = mse_setup(tmp_path, monkeypatch, professional=professional)
    path, credential, clock, grants = setup
    status, _ = mse_source_bodies(nested=True)
    stack = status["Stack"]
    stack["Status"] = "CREATE_ROLLBACK_COMPLETE"
    stack.pop("Outputs")
    changes = {
        "creating": ("Status", "CREATE_IN_PROGRESS"),
        "create_failed": ("Status", "CREATE_FAILED"),
        "rollback_failed": ("Status", "CREATE_ROLLBACK_FAILED"),
        "delete_complete": ("Status", "DELETE_COMPLETE"),
        "foreign_stack": ("StackId", "foreign-stack"),
        "foreign_name": ("StackName", "foreign-name"),
        "foreign_region": ("RegionId", "cn-beijing"),
        "foreign_account": ("AccountId", "foreign-account"),
        "wrong_type": ("StackType", "ROS"),
        "stack_status": ("StackStatus", "CREATE_COMPLETE"),
    }
    if damage in changes:
        key, value = changes[damage]
        stack[key] = value
    elif damage == "nested_identity":
        status["StackId"] = "foreign-stack"
    elif damage == "nested_status":
        status["Status"] = "CREATE_COMPLETE"
    elif damage == "outer_account":
        stack.pop("AccountId")
        status["AccountId"] = "foreign-account"
    if damage != "missing_status":
        mse_perform(setup, "mse-status", status)
    if damage == "latest_incomplete":
        stack["Status"] = "CREATE_IN_PROGRESS"
        mse_perform(setup, "mse-status", status)
    elif damage == "latest_unknown":
        mse_perform(
            setup, "mse-status", {"RequestId": "offline-error", "Code": "InternalError"}, status=500, success=False
        )
    with gate.database(path) as db:
        if damage in {"wire_call", "wire_status", "wire_header"}:
            wire = json.loads(db.execute("SELECT response FROM attempts WHERE grant_id='mse-status'").fetchone()[0])
            if damage == "wire_call":
                wire["call"]["params"]["StackId"] = "foreign-stack"
            elif damage == "wire_status":
                wire["status"] = 403
            else:
                wire["headers"]["x-acs-request-id"] = "foreign-request"
            db.execute("UPDATE attempts SET response=? WHERE grant_id='mse-status'", (json.dumps(wire),))
        elif damage.startswith("status_"):
            column = {
                "status_session": "session",
                "status_signer": "credential",
                "status_scope": "reference_scope",
                "status_tool": "tool_use_id",
                "status_unknown": "state",
            }[damage]
            value = "unknown" if damage == "status_unknown" else "" if damage == "status_tool" else "foreign"
            db.execute(f"UPDATE attempts SET {column}=? WHERE grant_id='mse-status'", (value,))
        elif damage == "parent_unknown":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='mse-create'")
        elif damage == "parent_response":
            db.execute(
                "UPDATE attempts SET response=? WHERE grant_id='mse-create'",
                (json.dumps({"StackId": "foreign-stack", "RequestId": "offline-create"}),),
            )
        elif damage == "identity_missing":
            db.execute("DELETE FROM identities")
        elif damage == "source_parent":
            db.execute("UPDATE grants SET parent='foreign-create' WHERE id='mse-status'")
        elif damage == "source_phase":
            db.execute(
                "UPDATE grants SET read_policy=? WHERE id='mse-status'",
                (json.dumps({"phase": "cleanup", "min_interval_seconds": 30}),),
            )
        elif damage == "late_status":
            # Freeze the latest failure read before the earliest delete intent,
            # even if that original deletion outcome is unknown.
            parent = db.execute("SELECT created FROM attempts WHERE grant_id='mse-create'").fetchone()[0]
            row = db.execute("SELECT * FROM attempts WHERE grant_id='mse-status'").fetchone()
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,created,credential,tool_use_id,reference_scope) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "offline-prior-delete",
                    "offline-mse:TF",
                    "mse-delete",
                    "ws~mse-full",
                    "unknown",
                    parent + 1,
                    row["credential"],
                    "delete-tool",
                    row["reference_scope"],
                ),
            )
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    request = mse_actual(grants, "mse-delete")
    if damage == "request_target":
        request["params"]["StackId"] = "foreign-stack"
    elif damage == "current_signer":
        credential = SimpleNamespace(mode="AK", access_key_id="rotated", access_key_secret="fake-secret")
    clock[0] += 31
    before = counts(path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(request, "ws~mse-full", credential, read_only=False, tool_use_id="offline-delete-tool")
    assert counts(path) == before


@pytest.mark.parametrize("professional", [False, True], ids=["developer1", "professional3"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('source = "aliyun/alicloud"', 'source = "other/alicloud"'),
        ('version = "= 1.260.0"', 'version = "= 1.261.0"'),
        ('region = "cn-hangzhou"', 'region = "cn-beijing"'),
        ('vpc_name = "offline-mse"', 'vpc_name = "business"'),
        ('cidr_block = "192.168.244.0/24"', 'cidr_block = "10.0.0.0/8"'),
        ("enable_ipv6 = false", "enable_ipv6 = true"),
        ("force_delete = false", "force_delete = true"),
        ('vswitch_name = "offline-mse"', 'vswitch_name = "business"'),
        ("vpc_id = alicloud_vpc.sample_vpc.id", 'vpc_id = "vpc-business"'),
        ('zone_id = "cn-hangzhou-b"', 'zone_id = "cn-hangzhou-h"'),
        ('cidr_block = "192.168.244.0/26"', 'cidr_block = "192.168.244.0/25"'),
        ('cluster_alias_name = "offline-mse"', 'cluster_alias_name = "business"'),
        ('cluster_specification = "MSE_SC_1_2_60_c"', 'cluster_specification = "MSE_SC_2_4_60_c"'),
        ('cluster_type = "Nacos-Ans"', 'cluster_type = "ZooKeeper"'),
        ('cluster_version = "NACOS_2_0_0"', 'cluster_version = "ZooKeeper_3_8_0"'),
        ('payment_type = "PayAsYouGo"', 'payment_type = "Subscription"'),
        ('net_type = "privatenet"', 'net_type = "pubnet"'),
        ('connection_type = "slb"', 'connection_type = "single_eni"'),
        ('pub_network_flow = "0"', 'pub_network_flow = "1"'),
        ("vswitch_id = alicloud_vswitch.sample_vswitch.id", 'vswitch_id = "vsw-business"'),
        ("value = alicloud_mse_cluster.sample.id", 'value = "mse-business"'),
        ("value = alicloud_mse_cluster.sample.cluster_id", "value = alicloud_mse_cluster.sample.id"),
        ("value = alicloud_vpc.sample_vpc.id", 'value = "vpc-business"'),
        ("value = alicloud_vswitch.sample_vswitch.id", 'value = "vsw-business"'),
        ('resource "alicloud_mse_cluster" "sample"', 'resource "alicloud_mse_cluster" "foreign"'),
        ('resource "alicloud_vpc" "sample_vpc"', 'resource "alicloud_vpc" "foreign"'),
        ('resource "alicloud_vswitch" "sample_vswitch"', 'resource "alicloud_vswitch" "foreign"'),
    ],
)
def test_mse_professional_alternative_keeps_every_other_template_field_closed(professional, old, new):
    grants = mse_entries(professional=professional)
    create = next(g for g in grants if g["kind"] == "create")
    template = json.loads(create["call"]["params"]["TemplateBody"])
    original = template["Workspace"]["main.tf"]
    assert old in original
    template["Workspace"]["main.tf"] = original.replace(old, new, 1)
    create["call"]["params"]["TemplateBody"] = json.dumps(template)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)


@pytest.mark.parametrize(
    ("edition", "count"),
    [
        ("mse_pro", 1),
        ("mse_dev", 3),
        ("mse_pro", 0),
        ("mse_pro", 2),
        ("mse_pro", 5),
        ("mse_pro", 9),
        ("mse_dev", 2),
        ("mse_serverless", 3),
    ],
)
def test_mse_professional_alternative_never_allows_crossed_or_other_node_counts(edition, count):
    grants = mse_entries()
    create = next(g for g in grants if g["kind"] == "create")
    template = json.loads(create["call"]["params"]["TemplateBody"])
    template["Workspace"]["main.tf"] = (
        template["Workspace"]["main.tf"]
        .replace(
            "instance_count = 1",
            f"instance_count = {count}",
        )
        .replace('mse_version = "mse_dev"', f'mse_version = "{edition}"')
    )
    create["call"]["params"]["TemplateBody"] = json.dumps(template)
    with pytest.raises(gate.GateDeniedError):
        gate.validate_grants(grants, require_read_policy=True)
