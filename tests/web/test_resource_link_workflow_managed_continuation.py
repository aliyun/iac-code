"""Retain an MSE generation while completing unused reads, without quota refill."""

import json
import shutil

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow import native_continuation as continuation
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, json_write, read_json
from scripts.web.e2e.resource_link_workflow.phases import assert_managed_source_reads_complete, readonly_continuation
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from tests.web.test_resource_link_workflow import plan
from tests.web.test_resource_link_workflow_read_policy import mse_perform, mse_setup, mse_source_bodies


@pytest.fixture
def managed(tmp_path, monkeypatch):
    setup = mse_setup(tmp_path, monkeypatch)
    mse_perform(setup, "mse-status", mse_source_bodies()[0])
    gate.cleanup(setup[0], "0", "offline-mse:TF", "ws~mse-full")  # No Delete submitted.
    store = Store.initialize(tmp_path / "state")
    plan(store)
    task_id = store.status()["tasks"][0]["id"]
    packet = {
        "task_id": task_id,
        "requests": {"TF": "Offline original request"},
        "execution_grants": {"TF": setup[3]},
        "inputs": [],
        "scenes": {"offline-TF-open": {"account": "123"}},
    }
    json_write(store.root / "environment.json", {"repo": str(tmp_path)})
    json_write(store.root / "packets/original/packet.json", packet)
    output = store.root / "jobs/offline-mse/output"
    json_write(
        output / "self-created-session.json",
        {
            "job_id": "offline-mse",
            "target": "target-0",
            "origin": "http://127.0.0.1:16100",
            "session_id": "ws~mse-full",
        },
    )
    json_write(output / "new-session-intent.json", {"previous_session": "different"})
    json_write(output / "TF-product-source.json", {"session_id": "ws~mse-full", "references": {"items": []}})
    json_write(output / "TF-cleanup-intent.json", {"state": "submitted"})
    with store.transaction() as db:
        spec = json.loads(db.execute("SELECT spec FROM tasks WHERE id=?", (task_id,)).fetchone()[0])
        spec["key"] = "ALIYUN::MSE::Cluster"
        spec["dimensions"]["TF"]["support"] = "supported"
        spec["dimensions"]["TF"]["contracts"] = [
            {"id": "offline-TF", "source_type": spec["key"], "historical": {"open": "review", "iframe": "pass"}}
        ]
        spec["retest_scope"] = {"kind": "failed_navigation_managed", "scene_ids": ["offline-TF-open"]}
        db.execute(
            "UPDATE tasks SET state='blocked',attempt=1,slot=0,packet='packets/original/packet.json',spec=?",
            (dumps(spec),),
        )
        db.execute("UPDATE slots SET task_id=?,target='target-0' WHERE id=0", (task_id,))
        for index, (ident, role, purpose) in enumerate(
            (
                ("offline-mse", "glm", "dispatch"),
                ("review", "sol", "review"),
            )
        ):
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    task_id,
                    role,
                    purpose,
                    "complete",
                    role + "-thread",
                    ident + "-turn",
                    "fake",
                    "result_staged",
                    index,
                ),
            )
        db.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            ("owner", task_id, "TF", dumps({"id": "mse-stack-own"}), "present", "offline-original"),
        )
    store.pause(True)
    target = store.root / "execution-gate.sqlite3"
    shutil.copy2(setup[0], target)
    monkeypatch.setenv(gate.ENV, str(target))
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.creation.validate_authorization", lambda *a: None)
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.creation.execution_authority", lambda *a: "offline-mse-authority"
    )
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.temporary_samples.admitted", lambda *a: {"TF": "fake"})
    monkeypatch.setattr(continuation, "terminal", lambda root, job: {"path": "offline-terminal", "sha256": "fake"})
    runtime = Runtime(store)
    monkeypatch.setattr(
        runtime,
        "browser",
        lambda request: {
            "session_id": "ws~mse-full",
            "busy": False,
            "unlocked": True,
            "preview_open": False,
        },
    )
    return runtime, task_id


def wire(runtime):
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        return {
            table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("grants", "attempts", "authority_limits", "identities")
        }


def test_inspect_and_queue_preserve_every_original_call_and_artifact(managed):
    runtime, task_id = managed
    before = wire(runtime)
    old = read_json(runtime.root / "jobs/offline-mse/output/TF-product-source.json")
    proof = continuation.inspect_managed(runtime, "offline-mse")
    assert proof["additional_identity_reads"] == proof["new_creation_quota"] == 0
    assert proof["original_phase"] == "cleanup"
    assert wire(runtime) == before
    json_write(runtime.root / "inputs/review.json", {**proof, "approved": True}, private=True)
    result = continuation.queue_managed(runtime, "offline-mse", "review.json")
    assert result["only_luna_dispatches"] and result["continuation_queued"] == task_id
    assert wire(runtime) == before
    assert read_json(runtime.root / "jobs/offline-mse/output/TF-product-source.json") == old
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        assert db.execute("SELECT phase FROM active").fetchone()[0] == "execute"
    with pytest.raises(WorkflowError):
        continuation.queue_managed(runtime, "offline-mse", "review.json")


@pytest.mark.parametrize("account", ["123", "foreign"])
def test_reviewed_identity_renewal_binds_old_members_and_deletion_without_refill(managed, monkeypatch, account):
    from iac_code.services.providers.aliyun import AliyunCredential
    from scripts.web.e2e.resource_link_workflow.phases import (
        assert_cleanup_reads_complete,
        assert_stack_source_ready,
    )
    from tests.web.test_resource_link_workflow_mse_phases import absent
    from tests.web.test_resource_link_workflow_read_policy import mse_actual

    runtime, _ = managed
    proof = continuation.inspect_managed(runtime, "offline-mse", renew_identity=True)
    before = wire(runtime)
    json_write(runtime.root / "inputs/renewal.json", {**proof, "approved": True}, private=True)
    queued = continuation.queue_managed(runtime, "offline-mse", "renewal.json")
    assert queued["additional_identity_reads"] == 1 and queued["new_creation_quota"] == 0
    after = wire(runtime)
    assert after["attempts"] == before["attempts"] and after["identities"] == before["identities"]
    for table in ("grants", "authority_limits"):
        assert all(row in after[table] for row in before[table])
        assert len(after[table]) == len(before[table]) + 1
    with runtime.store.transaction() as db:
        recovery = continuation.load(runtime.root, db, queued["continuation_queued"])
    assert "只执行" in continuation.delivery(runtime.root, recovery, "identity")
    instructions = json.loads(gate.execution_instructions("ws~mse-full", "").rsplit("\n", 1)[-1])
    assert any(g["kind"] == "identity" and g["remaining"] == 1 for g in instructions)
    packet = read_json(runtime.root / "packets/original/packet.json")
    grants = [
        *packet["execution_grants"]["TF"],
        {"id": proof["identity_grant_id"], "kind": "identity", "quota": 1, "call": proof["identity_call"]},
    ]
    path = runtime.root / "execution-gate.sqlite3"
    credential = AliyunCredential(mode="AK", access_key_id="fake-rotated", access_key_secret="fake-new-secret")
    clock = [10000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    setup = path, credential, clock, grants
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(
            mse_actual(grants, "mse-members"),
            "ws~mse-full",
            credential,
            read_only=True,
            tool_use_id="offline-before-renewal",
        )
    mse_perform(setup, proof["identity_grant_id"], {"AccountId": account, "RequestId": "offline-renewed"})
    if account != "123":
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(
                mse_actual(grants, "mse-members"),
                "ws~mse-full",
                credential,
                read_only=True,
                tool_use_id="offline-wrong-account",
            )
        return
    with pytest.raises(WorkflowError):
        continuation.delivery(runtime.root, recovery, "identity")
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(
            mse_actual(grants, "mse-create"),
            "ws~mse-full",
            credential,
            read_only=False,
            tool_use_id="offline-no-new-creation",
        )
    _, members = mse_source_bodies()
    mse_perform(setup, "mse-members", members)
    with gate.database(path) as db:
        signed_member = db.execute("SELECT * FROM attempts WHERE grant_id='mse-members'").fetchone()
    reference = {
        "identifier": "mse-cn-own",
        "resourceType": "alicloud_mse_cluster",
        "stackId": "mse-stack-own",
        "regionId": "cn-hangzhou",
        "accountScope": signed_member["reference_scope"],
        "sources": [{"evidence": "api_response", "toolUseId": signed_member["tool_use_id"], "regionId": "cn-hangzhou"}],
    }
    assert runtime.created_references("offline-mse", "TF", "ws~mse-full", [reference]) == [reference]
    assert not runtime.created_references(
        "offline-mse", "TF", "ws~mse-full", [{**reference, "stackId": "unrelated-stack"}]
    )
    assert not runtime.created_references(
        "offline-mse", "TF", "ws~mse-full", [{**reference, "accountScope": "unreviewed-signer"}]
    )
    with pytest.raises(WorkflowError, match="original wire creation"):
        runtime.created_references("new-continuation-job", "TF", "ws~mse-full", [reference])
    for child in ("cluster", "vpc", "vswitch"):
        mse_perform(setup, child + "-resource", {"RequestId": "offline-own-resource"})
    mse_perform(
        setup,
        "cluster-execute",
        {
            "Success": True,
            "ErrorCode": "Success",
            "RequestId": "offline-cluster",
            "Data": {"InstanceId": "mse-cn-own", "RegionId": "cn-hangzhou"},
        },
    )
    mse_perform(setup, "vpc-execute", {"RequestId": "offline-vpc", "VpcId": "vpc-own"})
    mse_perform(setup, "vswitch-execute", {"RequestId": "offline-vswitch", "VSwitchId": "vsw-own"})
    assert_managed_source_reads_complete(path, "offline-mse:TF")
    assert_stack_source_ready(path, "offline-mse:TF", cleanup=False)
    gate.cleanup(path, "0", "offline-mse:TF", "ws~mse-full")
    mse_perform(setup, "mse-delete", {"RequestId": "offline-original-delete"})
    mse_perform(
        setup,
        "mse-status-cleanup",
        {"RequestId": "offline-stack-absent", "StackId": "mse-stack-own", "Status": "DELETE_COMPLETE"},
    )
    for child in ("cluster", "vpc", "vswitch"):
        absent(setup, child)
    mse_perform(
        setup,
        "mse-name-cleanup",
        {"RequestId": "offline-name-empty", "Success": True, "ErrorCode": "Success", "Data": [], "TotalSize": 0},
    )
    assert_cleanup_reads_complete(path, "offline-mse:TF")
    assert_stack_source_ready(path, "offline-mse:TF", cleanup=True)
    with gate.database(path) as db:
        assert db.execute("SELECT used FROM grants WHERE id='mse-create'").fetchone()[0] == 1
        assert db.execute("SELECT used FROM grants WHERE id='mse-identity'").fetchone()[0] == 1
        assert db.execute("SELECT used FROM grants WHERE id='mse-delete'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "unpaused",
        "busy",
        "session",
        "deleted",
        "unknown",
        "counter",
        "input",
        "actual-click",
        "different-template",
        "signer",
    ],
)
def test_reject_drift_unknowns_writes_and_replayed_clicks(managed, damage, monkeypatch):
    runtime, task_id = managed
    if damage == "unpaused":
        runtime.store.pause(False)
    elif damage in {"busy", "session"}:
        monkeypatch.setattr(
            runtime,
            "browser",
            lambda request: {
                "session_id": "foreign" if damage == "session" else "ws~mse-full",
                "busy": damage == "busy",
                "unlocked": True,
            },
        )
    elif damage == "actual-click":
        with runtime.store.transaction() as db:
            db.execute(
                "INSERT INTO observations(task_id,attempt,scene,data) VALUES(?,?,?,?)",
                (task_id, 1, "offline-TF-open", dumps({"actual_ui_click": True})),
            )
    elif damage == "input":
        path = runtime.root / "packets/original/packet.json"
        packet = read_json(path)
        packet["inputs"] = [{"path": "missing", "sha256": "bad"}]
        json_write(path, packet)
    else:
        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            if damage == "deleted":
                db.execute("UPDATE grants SET used=1 WHERE kind='delete'")
            elif damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='mse-status'")
            elif damage == "counter":
                db.execute("UPDATE authority_limits SET used=0 WHERE id='mse-create'")
            elif damage == "different-template":
                db.execute("UPDATE grants SET call='{}' WHERE kind='create'")
            elif damage == "signer":
                db.execute("UPDATE attempts SET credential='foreign' WHERE grant_id='mse-status'")
    with pytest.raises((WorkflowError, FileNotFoundError)):
        continuation.inspect_managed(runtime, "offline-mse")


def test_source_continuation_delivers_member_then_resource_then_service_groups(tmp_path, monkeypatch):
    setup = mse_setup(tmp_path, monkeypatch)
    path = setup[0]
    status, members = mse_source_bodies()
    mse_perform(setup, "mse-status", status)
    first = readonly_continuation(path, "0", "offline-mse:TF", "ws~mse-full", "source")
    assert "ros ListStackResources" in first
    assert "ros GetStackResource" not in first and "mse QueryClusterInfo" not in first
    mse_perform(setup, "mse-members", members)
    second = readonly_continuation(path, "0", "offline-mse:TF", "ws~mse-full", "source")
    assert second.count("ros GetStackResource") == 3
    assert "ros ListStackResources" not in second and "mse QueryClusterInfo" not in second
    with pytest.raises(WorkflowError, match="incomplete"):
        assert_managed_source_reads_complete(path, "offline-mse:TF")
    for child in ("cluster", "vpc", "vswitch"):
        mse_perform(setup, child + "-resource", {"RequestId": "offline-resource"})
    third = readonly_continuation(path, "0", "offline-mse:TF", "ws~mse-full", "source")
    assert "mse QueryClusterInfo" in third and "vpc DescribeVpcAttribute" in third
    assert "vpc DescribeVSwitchAttributes" in third and "ros GetStackResource" not in third


def test_terminal_pre_wire_retry_keeps_original_review_and_all_counters(managed):
    runtime, task_id = managed
    first = continuation.inspect_managed(runtime, "offline-mse")
    json_write(runtime.root / "inputs/first.json", {**first, "approved": True}, private=True)
    continuation.queue_managed(runtime, "offline-mse", "first.json")
    with runtime.store.transaction() as db:
        db.execute("UPDATE tasks SET state='blocked',attempt=2 WHERE id=?", (task_id,))
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "failed",
                task_id,
                "glm",
                "continuation",
                "complete",
                "glm-thread",
                "failed-turn",
                "fake",
                "result_staged",
                2,
            ),
        )
    json_write(runtime.root / "jobs/failed/output/source.json", {"gate_denied_before_wire": True})
    before = wire(runtime)
    proof = continuation.inspect_managed(runtime, "offline-mse")
    assert proof["previous_continuation_count"] == 1 and proof["failed_job_id"] == "failed"
    assert proof["wire_sha256"] == first["wire_sha256"]
    json_write(runtime.root / "inputs/second.json", {**proof, "approved": True}, private=True)
    continuation.queue_managed(runtime, "offline-mse", "second.json")
    assert wire(runtime) == before
    with runtime.store.transaction() as db:
        assert db.execute("SELECT value FROM meta WHERE key='managed_continuation_history:failed'").fetchone()
    runtime.store.pause(False)
    action = runtime.store.next_action()["actions"][0]
    job = runtime.store.execute(action["id"])
    with runtime.store.transaction() as db:
        saved = db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
        assert runtime.store.reviewed_continuation(db, saved)
    assert wire(runtime) == before


@pytest.mark.parametrize("operation", ["read_artifact", "accept", "approve"])
def test_capacity_failure_only_retains_prior_review_after_readonly_inspection(managed, monkeypatch, operation):
    runtime, task_id = managed
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "capacity",
                task_id,
                "sol",
                "review",
                "complete",
                "sol-thread",
                "capacity-turn",
                "fake",
                "confirmed",
                3,
            ),
        )
    raw_path = runtime.root / "jobs/capacity/raw.json"
    trace_path = runtime.root / "jobs/capacity/trace.jsonl"
    json_write(
        raw_path,
        {
            "thread": {
                "turns": [
                    {
                        "id": "capacity-turn",
                        "status": "failed",
                        "error": {"codexErrorInfo": "serverOverloaded"},
                        "items": [
                            {
                                "type": "commandExecution",
                                "command": "python3 workflow-agent.py read_artifact",
                                "exitCode": 0,
                            }
                        ],
                    }
                ]
            }
        },
    )
    trace_path.write_text(
        json.dumps({"method": "script/request", "params": {"operation": operation}}) + "\n", encoding="utf-8"
    )
    evidence = {
        "path": raw_path.relative_to(runtime.root).as_posix(),
        "transport_path": trace_path.relative_to(runtime.root).as_posix(),
    }
    monkeypatch.setattr(
        continuation,
        "terminal",
        lambda root, job, **kwargs: evidence if job["id"] == "capacity" else {"path": "old-review", "sha256": "old"},
    )
    before = wire(runtime)
    if operation == "read_artifact":
        proof = continuation.inspect_managed(runtime, "offline-mse", renew_identity=True)
        assert proof["review_terminal"]["path"] == "old-review"
        assert proof["additional_identity_reads"] == 1
    else:
        with pytest.raises(WorkflowError, match="unreviewed model failure"):
            continuation.inspect_managed(runtime, "offline-mse", renew_identity=True)
    assert wire(runtime) == before


def test_capture_only_uses_completed_original_source_without_new_reads_or_identity(managed, monkeypatch):
    from iac_code.services.providers.aliyun import AliyunCredential

    runtime, task_id = managed
    first = continuation.inspect_managed(runtime, "offline-mse", renew_identity=True)
    json_write(runtime.root / "inputs/renew.json", {**first, "approved": True}, private=True)
    continuation.queue_managed(runtime, "offline-mse", "renew.json")
    packet = read_json(runtime.root / "packets/original/packet.json")
    grants = [
        *packet["execution_grants"]["TF"],
        {"id": first["identity_grant_id"], "kind": "identity", "quota": 1, "call": first["identity_call"]},
    ]
    clock = [10000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    setup = (
        runtime.root / "execution-gate.sqlite3",
        AliyunCredential(mode="AK", access_key_id="fake-rotated", access_key_secret="fake-new-secret"),
        clock,
        grants,
    )
    mse_perform(setup, first["identity_grant_id"], {"AccountId": "123", "RequestId": "offline-new"})
    mse_perform(setup, "mse-members", mse_source_bodies()[1])
    for child in ("cluster", "vpc", "vswitch"):
        mse_perform(setup, child + "-resource", {"RequestId": "offline-resource"})
    mse_perform(
        setup,
        "cluster-execute",
        {
            "Success": True,
            "ErrorCode": "Success",
            "RequestId": "offline-cluster",
            "Data": {"InstanceId": "mse-cn-own", "RegionId": "cn-hangzhou"},
        },
    )
    mse_perform(setup, "vpc-execute", {"RequestId": "offline-vpc", "VpcId": "vpc-own"})
    mse_perform(setup, "vswitch-execute", {"RequestId": "offline-vswitch", "VSwitchId": "vsw-own"})
    with gate.database(setup[0]) as db:
        member = db.execute("SELECT * FROM attempts WHERE grant_id='mse-members'").fetchone()
    source = {
        "session_id": "ws~mse-full",
        "references": {
            "items": [
                {
                    "identifier": "mse-cn-own",
                    "resourceType": "ALIYUN::MSE::Cluster",
                    "stackId": "mse-stack-own",
                    "regionId": "cn-hangzhou",
                    "accountScope": member["reference_scope"],
                    "sources": [
                        {"evidence": "api_response", "toolUseId": member["tool_use_id"], "regionId": "cn-hangzhou"}
                    ],
                }
            ]
        },
    }
    json_write(runtime.root / "jobs/capture-needed/output/TF-product-source.json", source)
    with runtime.store.transaction() as db:
        db.execute("UPDATE tasks SET state='reported',attempt=2 WHERE id=?", (task_id,))
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "capture-needed",
                task_id,
                "glm",
                "continuation",
                "complete",
                "glm-thread",
                "capture-needed-turn",
                "fake",
                "result_staged",
                3,
            ),
        )
    before = wire(runtime)
    proof = continuation.inspect_managed(runtime, "offline-mse", capture_only=True)
    assert proof["capture_only"] and proof["additional_identity_reads"] == proof["new_creation_quota"] == 0
    assert proof["capture_source"]["path"] == "jobs/capture-needed/output/TF-product-source.json"
    json_write(runtime.root / "inputs/capture.json", {**proof, "approved": True}, private=True)
    continuation.queue_managed(runtime, "offline-mse", "capture.json")
    assert wire(runtime) == before
    with runtime.store.transaction() as db:
        recovery = continuation.load(runtime.root, db, task_id)
    assert recovery["capture_source"] == proof["capture_source"]
    assert read_json(runtime.root / proof["capture_source"]["path"]) == source
    with pytest.raises(WorkflowError):
        continuation.inspect_managed(runtime, "offline-mse", renew_identity=True, capture_only=True)
