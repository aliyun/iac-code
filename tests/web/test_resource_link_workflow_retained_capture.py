"""Original ROS source/counters survive pixel recovery; QUERY is never recreated."""

import copy
import hashlib
import json

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow import retained_capture
from scripts.web.e2e.resource_link_workflow.console_sources import bind_rocketmq_notice_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import dumps
from tests.web.test_resource_link_workflow_native_rocketmq_sample import reviewed  # noqa: F401


@pytest.fixture
def retained(reviewed, monkeypatch):  # noqa: F811
    store, packet, _ = reviewed
    store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")
    runtime = Runtime(store)
    runtime.authorize_creation(store.root / "inputs/proposal.json", "inputs/operation.json", 0)
    json_write(store.root / "environment.json", {"repo": str(store.root.parent)})
    path = store.root / "execution-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    gate.install(
        path,
        "0",
        "worker:ROS",
        "ws~full",
        "1234567890123456",
        None,
        packet["execution_grants"]["ROS"],
        authority=retained_capture.gate.canonical({"fake": "unused"}),
    )
    from scripts.web.e2e.resource_link_workflow.creation import execution_authority

    authority = execution_authority(store.root, packet, "ROS")
    with gate.database(path) as wire:
        wire.execute("UPDATE grants SET authority=?", (authority,))
        wire.execute("UPDATE authority_limits SET authority=?", (authority,))
    cred = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    identifier = "MQ_INST_1234567890123456_Own"
    raw = {
        "RequestId": "offline-member",
        "StackId": "own-stack",
        "LogicalResourceId": "Instance",
        "PhysicalResourceId": identifier,
        "ResourceType": "ALIYUN::ROCKETMQ::Instance",
        "ResourceAttributes": [
            {"ResourceAttributeKey": "InstanceId", "ResourceAttributeValue": identifier},
            {"ResourceAttributeKey": "InstanceName", "ResourceAttributeValue": "offline-own"},
        ],
    }
    bodies = {
        "identity": {"AccountId": "1234567890123456", "RequestId": "offline-identity"},
        "create": {"StackId": "own-stack", "RequestId": "offline-create"},
        "status": {
            "StackId": "own-stack",
            "StackType": "ROS",
            "Status": "CREATE_COMPLETE",
            "RequestId": "offline-status",
        },
        "members": {"Resources": [], "RequestId": "offline-members"},
        "member": raw,
        "inventory": {"Data": {"InstanceVO": []}, "RequestId": "offline-inventory"},
    }
    for grant in packet["execution_grants"]["ROS"]:
        if grant["id"] not in bodies:
            continue
        call = copy.deepcopy(grant["call"])
        if grant.get("parent"):
            call["params"][grant["target_param"]] = "own-stack"
        ticket = gate.reserve(
            call, "ws~full", cred, read_only=grant["kind"] != "create", tool_use_id=grant["id"] + "-tool"
        )
        gate.finish(ticket, bodies[grant["id"]], True)
    gate.cleanup(path, "0", "worker:ROS", "ws~full")
    with gate.database(path) as wire:
        account_scope = wire.execute("SELECT reference_scope FROM attempts WHERE grant_id='member'").fetchone()[0]
    ref = {
        "identifier": identifier,
        "resourceType": "ALIYUN::ROCKETMQ::Instance",
        "regionId": "cn-hangzhou",
        "stackId": "own-stack",
        "logicalResourceId": "Instance",
        "accountScope": account_scope,
        "sources": [
            {
                "evidence": "api_response",
                "sourceKind": "tool_result",
                "product": "ros",
                "action": "GetStackResource",
                "toolUseId": "member-tool",
                "turnId": "original-turn",
                "stackId": "own-stack",
                "logicalResourceId": "Instance",
            }
        ],
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "ros",
            "action": "GetStackResource",
            "version": "2019-09-10",
            "region_id": "cn-hangzhou",
            "params": {
                "RegionId": "cn-hangzhou",
                "StackId": "own-stack",
                "LogicalResourceId": "Instance",
                "ShowResourceAttributes": True,
            },
        },
        "results": [{"isError": False, "content": json.dumps(raw)}],
    }
    output = store.root / "jobs/worker/output"
    json_write(output / "new-session-intent.json", {"previous_session": "other"})
    json_write(
        output / "self-created-session.json",
        {"job_id": "worker", "target": "own-target", "origin": "http://127.0.0.1:16100", "session_id": "ws~full"},
    )
    source = {"session_id": "ws~full", "references": {"items": [ref]}, "messages": {"tools": {"member-tool": tool}}}
    json_write(output / "ROS-product-source.json", source)
    png = output / "R0083-ROS-1-open.png"
    png.write_bytes(b"OFFLINE FAKE PIXELS")
    json_write(
        output / "R0083-ROS-1-open.json",
        {
            "actual_ui_click": True,
            "session_id": "ws~full",
            "source_identifier": identifier,
            "screenshot": str(png.relative_to(store.root)),
            "source_evidence": str((output / "ROS-product-source.json").relative_to(store.root)),
            "source_sha256": digest(output / "ROS-product-source.json"),
        },
    )
    json_write(output / "ROS-cleanup-intent.json", {"state": "submitted"})
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='blocked',slot=0,attempt=1,packet='inputs/proposal.json'")
        db.execute("UPDATE slots SET task_id=?,target='own-target' WHERE id=0", (packet["task_id"],))
        for i, role in enumerate(("glm", "sol")):
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "worker" if role == "glm" else "review",
                    packet["task_id"],
                    role,
                    "dispatch" if role == "glm" else "review",
                    "complete",
                    role + "-thread",
                    role + "-turn",
                    "fake",
                    "result_staged",
                    i,
                ),
            )
        db.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            ("own", packet["task_id"], "ROS", dumps({"id": "own-stack"}), "present", "offline-wire"),
        )
        db.execute("INSERT INTO inspected VALUES(?,?,?)", ("review", str(png.relative_to(store.root)), digest(png)))
    store.pause(True)
    monkeypatch.setattr(retained_capture, "terminal", lambda root, job: {"path": "offline", "sha256": "fake"})
    monkeypatch.setattr(
        runtime,
        "browser",
        lambda request: {"session_id": "ws~full", "busy": False, "unlocked": True, "preview_open": False},
    )
    monkeypatch.setattr(
        runtime,
        "inspect_cleanup",
        lambda *a: {
            "scope": "worker:ROS",
            "session_id": "ws~full",
            "original_phase": "cleanup",
            "successful_deletions": [],
            "remaining_calls": [
                {
                    "kind": "delete",
                    "remaining": 1,
                    "call": {
                        "product": "ros",
                        "action": "DeleteStack",
                        "version": "2019-09-10",
                        "region_id": "cn-hangzhou",
                        "params": {"RegionId": "cn-hangzhou", "StackId": "own-stack", "RetainAllResources": False},
                    },
                }
            ],
        },
    )
    return runtime, source


def test_retained_queue_changes_no_wire_calls_or_resource_credits(retained):
    runtime, source = retained

    def snapshot():
        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            return {
                t: [dict(r) for r in db.execute("SELECT * FROM " + t + " ORDER BY rowid")]
                for t in ("grants", "attempts", "authority_limits", "identities", "active")
            }

    before = snapshot()
    proof = retained_capture.inspect(runtime, "worker")
    assert proof["additional_identity_reads"] == proof["new_creation_quota"] == 0
    json_write(runtime.root / "inputs/capture-review.json", {**proof, "approved": True}, private=True)
    assert retained_capture.queue(runtime, "worker", "capture-review.json")["only_luna_dispatches"]
    assert snapshot() == before
    assert read_json(runtime.root / proof["capture_source"]["path"]) == source
    with pytest.raises(WorkflowError):
        retained_capture.queue(runtime, "worker", "capture-review.json")


def rejected_capture(runtime):
    output = runtime.root / "jobs/worker/output"
    click_path = output / "R0083-ROS-1-open.json"
    click = {
        "actual_ui_click": False,
        "scene": "R0083-ROS-1-open",
        "session_id": "ws~full",
        "target": "own-target",
        "account": "1234567890123456",
        "region": "cn-hangzhou",
        "final_url": "http://127.0.0.1:16100/",
        "screenshot": "jobs/worker/output/R0083-ROS-1-open.png",
    }
    json_write(click_path, click)
    common = {"jobId": "worker", "threadId": "glm-thread", "turnId": "glm-turn"}
    events = [
        {
            "method": "script/request",
            "params": {**common, "operation": "click", "arguments": {"scene": click["scene"]}},
        },
        {
            "method": "script/rejected",
            "params": {
                **common,
                "operation": "click",
                "error": (
                    "precise reference destination ambiguous or pending; "
                    "copy the exact current snapshot session_id; never guess or retry creation"
                ),
            },
        },
        {"method": "script/result", "params": {**common, "operation": "evidence", "result": click}},
    ]
    trace = runtime.root / "jobs/worker/raw-rollout.jsonl"
    trace.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return trace, events


def test_preclick_diagnostic_continuation_reuses_current_source_and_calls(retained, monkeypatch):
    runtime, _ = retained
    trace, _ = rejected_capture(runtime)
    monkeypatch.setattr(
        retained_capture,
        "terminal",
        lambda root, job: {
            "path": "offline",
            "sha256": "fake",
            "transport_path": str(trace.relative_to(root)),
            "transport_sha256": digest(trace),
        },
    )
    before = trace.read_bytes()
    proof = retained_capture.inspect(runtime, "worker")
    assert proof["original_capture_rejection"]["actual_ui_click"] is False
    assert proof["identifier"] == "MQ_INST_1234567890123456_Own"
    assert proof["new_creation_quota"] == proof["additional_identity_reads"] == 0
    json_write(runtime.root / "inputs/rejected-review.json", {**proof, "approved": True}, private=True)
    retained_capture.queue(runtime, "worker", "rejected-review.json")
    assert trace.read_bytes() == before
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        assert db.execute("SELECT used FROM grants WHERE kind='create'").fetchone()[0] == 1
        assert db.execute("SELECT used FROM grants WHERE kind='delete'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM grants WHERE scope='worker:QUERY'").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["unknown", "successful", "error", "turn", "scene", "result", "occurrence"])
def test_preclick_checkpoint_rejects_unknown_or_different_capture(retained, monkeypatch, damage):
    runtime, _ = retained
    trace, events = rejected_capture(runtime)
    if damage == "unknown":
        events.pop(1)
    elif damage == "successful":
        events[1] = {"method": "script/result", "params": {**events[1]["params"], "result": {"actual_ui_click": True}}}
    elif damage == "error":
        events[1]["params"]["error"] = "some unknown transport error"
    elif damage == "turn":
        events[0]["params"]["turnId"] = "foreign-turn"
    elif damage == "scene":
        events[0]["params"]["arguments"]["scene"] = "R0083-QUERY-1-open"
    elif damage == "result":
        events[2]["params"]["result"]["account"] = "9999999999999999"
    else:
        json_write(runtime.root / "jobs/worker/output/occurrence-offline.json", {"offline": True})
    trace.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    monkeypatch.setattr(
        retained_capture,
        "terminal",
        lambda root, job: {
            "path": "offline",
            "sha256": "fake",
            "transport_path": str(trace.relative_to(root)),
            "transport_sha256": digest(trace),
        },
    )
    with pytest.raises(WorkflowError):
        retained_capture.inspect(runtime, "worker")


def test_ordinary_native_ros_click_uses_exact_resource_card_not_pagewide_mentions(retained, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.plans import expected_scenes

    runtime, source = retained
    with runtime.store.transaction() as db:
        spec = json.loads(db.execute("SELECT spec FROM tasks").fetchone()[0])
        slot = dict(db.execute("SELECT * FROM slots WHERE id=0").fetchone())
    packet = read_json(runtime.root / "inputs/proposal.json")
    packet["ui"] = {}
    packet["inputs"] = []
    packet["scenes"]["R0083-ROS-1-open"].update(
        identifier="{identifier}",
        selector='a.resource-link[data-resource-link][aria-label="{identifier}"]',
        reference_types=[spec["key"]],
        expected_type=spec["key"],
        render_text=["offline-own"],
        source_evidence="inputs/official.json",
        source_sha256=digest(runtime.root / "inputs/official.json"),
    )
    fresh_scene = "OFFLINE-ROS-first-open"
    packet["scenes"][fresh_scene] = packet["scenes"].pop("R0083-ROS-1-open")
    json_write(runtime.root / "inputs/proposal.json", packet)
    output = runtime.root / "jobs/worker/output"
    (output / "ROS-cleanup-intent.json").unlink()
    # Both native member operations and a service query mention this same ID.
    # Only its exact GetStackResource card owns this ROS capture.
    ref = source["references"]["items"][0]
    ref["sources"].append({**ref["sources"][0], "action": "ListStackResources", "toolUseId": "members-tool"})
    json_write(output / "ROS-product-source.json", source)
    scenes = [{**s, "id": fresh_scene} if s["dimension"] == "ROS" else s for s in expected_scenes(spec)]
    context = {
        "job_id": "worker",
        "task_id": packet["task_id"],
        "role": "glm",
        "purpose": "dispatch",
        "slot": slot,
        "resource": spec,
        "expected_scenes": scenes,
        "readonly_retest_dimensions": {"QUERY"},
        "packet_path": "inputs/proposal.json",
        "output_dir": str(output),
    }
    monkeypatch.setattr(runtime.store, "context", lambda *a: context)
    monkeypatch.setattr(runtime.store, "record_observation", lambda *a: None)
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.creation.validate_authorization", lambda *a: None)
    calls = []

    def browser(request):
        calls.append(request)
        png = output / (fresh_scene + ".png")
        png.write_bytes(b"OFFLINE FRESH PIXELS")
        return {"session_id": "ws~full", "actual_ui_click": True, "screenshot": str(png)}

    monkeypatch.setattr(runtime, "browser", browser)
    runtime.tool("worker", "fake", "click", {"scene": fresh_scene, "session_id": "ws~full"})
    assert calls[-1]["selector"].startswith('details[data-tool-use-id="member-tool"] ')
    assert calls[-1]["source_card_turn_id"] == "original-turn"
    assert calls[-1]["console_inspection"]["source_tool_use_id"] == "member-tool"


@pytest.fixture
def closed_checkpoint(retained, monkeypatch):
    runtime, _ = retained
    # This unit isolates pointer lifecycle; full admission/receipt checks have
    # separate integrated follow-up tests and are not replaced in production.
    from scripts.web.e2e.resource_link_workflow.creation import execution_authority

    packet = read_json(runtime.root / "inputs/proposal.json")
    authority = execution_authority(runtime.root, packet, "ROS")
    packet["additional_source_requests"] = [{"dimension": "ROS", "prior_scope": "older:ROS"}]
    json_write(runtime.root / "inputs/proposal.json", packet)
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.creation.validate_authorization", lambda *a: None)
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.creation.execution_authority", lambda *a: authority)
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.temporary_samples.admitted", lambda *a: {"ROS": authority}
    )
    json_write(runtime.root / "inputs/older-review.json", {"approved": True}, private=True)
    previous = {
        "kind": "retained_ros_capture",
        "original_job_id": "older",
        "scope": "older:ROS",
        "evidence": "inputs/older-review.json",
        "evidence_sha256": digest(runtime.root / "inputs/older-review.json"),
    }
    with runtime.store.transaction() as db:
        db.execute("INSERT INTO meta VALUES(?,?)", ("native_continuation:" + packet["task_id"], dumps(previous)))
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "older",
                packet["task_id"],
                "glm",
                "dispatch",
                "complete",
                "glm-thread",
                "older-turn",
                "offline",
                "result_staged",
                0,
            ),
        )
        db.execute("UPDATE jobs SET rowid=-1 WHERE id='older'")
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        grant = dict(db.execute("SELECT * FROM grants WHERE kind='delete'").fetchone())
        grant.update(scope="older:ROS", used=1, authority="offline-older")
        attempt = dict(db.execute("SELECT * FROM attempts WHERE grant_id='create'").fetchone())
        attempt.update(
            id="offline-old-delete",
            scope="older:ROS",
            grant_id=grant["id"],
            response=gate.canonical({"RequestId": "OFFLINE-DELETED"}),
        )
        for table, row in (("grants", grant), ("attempts", attempt)):
            db.execute(
                "INSERT INTO " + table + "(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")",
                tuple(row.values()),
            )
    return runtime, previous


def test_closed_predecessor_pointer_is_archived_not_erased_or_reused(closed_checkpoint):
    runtime, previous = closed_checkpoint
    proof = retained_capture.inspect(runtime, "worker")
    assert proof["previous_completed_capture"] == previous
    json_write(runtime.root / "inputs/next-review.json", {**proof, "approved": True}, private=True)
    retained_capture.queue(runtime, "worker", "next-review.json")
    with runtime.store.transaction() as db:
        current = json.loads(
            db.execute("SELECT value FROM meta WHERE key=?", ("native_continuation:" + proof["task_id"],)).fetchone()[0]
        )
        history = json.loads(
            db.execute(
                "SELECT value FROM meta WHERE key=?",
                ("native_continuation_history:" + proof["task_id"] + ":older:ROS",),
            ).fetchone()[0]
        )
    assert current["original_job_id"] == "worker"
    assert history == previous
    assert read_json(runtime.root / "inputs/older-review.json") == {"approved": True}


@pytest.mark.parametrize("damage", ["unknown", "not-deleted", "unreviewed-scope", "same-generation"])
def test_previous_pointer_cannot_skip_unresolved_or_unreviewed_generation(closed_checkpoint, damage):
    runtime, previous = closed_checkpoint
    if damage in {"unknown", "not-deleted"}:
        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            if damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown' WHERE scope='older:ROS'")
            else:
                db.execute("DELETE FROM attempts WHERE scope='older:ROS'")
    else:
        if damage == "same-generation":
            previous.update(original_job_id="worker", scope="worker:ROS")
        else:
            previous["scope"] = "unreviewed:ROS"
        with runtime.store.transaction() as db:
            task_id = read_json(runtime.root / "inputs/proposal.json")["task_id"]
            db.execute("UPDATE meta SET value=? WHERE key=?", (dumps(previous), "native_continuation:" + task_id))
    with pytest.raises(WorkflowError):
        retained_capture.inspect(runtime, "worker")


@pytest.mark.parametrize("damage", ["unknown", "query-installed", "counter", "deleted", "session", "unpaused"])
def test_retained_capture_rejects_unknowns_counter_drift_and_query_replay(retained, monkeypatch, damage):
    runtime, _ = retained
    if damage == "unpaused":
        runtime.store.pause(False)
    elif damage == "session":
        monkeypatch.setattr(runtime, "browser", lambda *a: {"session_id": "foreign"})
    else:
        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            if damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='member'")
            elif damage == "query-installed":
                db.execute(
                    "INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,?,?,?,?,?)",
                    ("worker:QUERY", "identity", "identity", "{}", 1, 0, "fake"),
                )
            elif damage == "counter":
                db.execute("UPDATE authority_limits SET used=0 WHERE id='create'")
            else:
                db.execute("UPDATE grants SET used=1 WHERE kind='delete'")
    with pytest.raises(WorkflowError):
        retained_capture.inspect(runtime, "worker")


def test_native_notice_fields_must_be_from_exact_signed_ros_member(retained):
    _, source = retained
    expected = {
        "kind": "rocketmq_instance_notice",
        "resource_type": "ALIYUN::ROCKETMQ::Instance",
        "dimension": "ROS",
        "account": "1234567890123456",
        "region": "cn-hangzhou",
        "identifier": "MQ_INST_1234567890123456_Own",
        "name": "offline-own",
    }
    assert (
        bind_rocketmq_notice_source(expected, source["references"]["items"], source["messages"]["tools"])[
            "source_tool_use_id"
        ]
        == "member-tool"
    )
    with pytest.raises(WorkflowError):
        bind_rocketmq_notice_source(
            {**expected, "name": "business"}, source["references"]["items"], source["messages"]["tools"]
        )


def test_runtime_click_cleanup_and_first_query_all_use_original_scope(retained, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.plans import expected_scenes

    runtime, _ = retained
    proof = retained_capture.inspect(runtime, "worker")
    json_write(runtime.root / "inputs/runtime-review.json", {**proof, "approved": True}, private=True)
    retained_capture.queue(runtime, "worker", "runtime-review.json")
    with runtime.store.transaction() as db:
        recovery = json.loads(
            db.execute("SELECT value FROM meta WHERE key=?", ("native_continuation:" + proof["task_id"],)).fetchone()[0]
        )
        spec = json.loads(db.execute("SELECT spec FROM tasks WHERE id=?", (proof["task_id"],)).fetchone()[0])
        slot = dict(db.execute("SELECT * FROM slots WHERE id=0").fetchone())
    packet_path = runtime.root / "inputs/proposal.json"
    packet = read_json(packet_path)
    packet["inputs"] = []
    packet["ui"] = {}
    for scene in expected_scenes(spec):
        packet["scenes"][scene["id"]].update(
            identifier="{identifier}" if scene["dimension"] == "ROS" else "MQ_INST_1234567890123456_Business",
            selector='a.resource-link[aria-label="{identifier}"]',
            expected_type=spec["key"],
            reference_types=[spec["key"]],
            render_text=["offline-own"],
            source_evidence="inputs/official.json",
            source_sha256=digest(runtime.root / "inputs/official.json"),
        )
    json_write(packet_path, packet)
    output = runtime.root / "jobs/new-continuation/output"
    context = {
        "job_id": "new-continuation",
        "task_id": proof["task_id"],
        "role": "glm",
        "purpose": "continuation",
        "slot": slot,
        "resource": spec,
        "native_continuation": recovery,
        "expected_scenes": expected_scenes(spec),
        "readonly_retest_dimensions": {"QUERY"},
        "packet_path": "inputs/proposal.json",
        "output_dir": str(output),
    }
    monkeypatch.setattr(runtime.store, "context", lambda *a: context)
    monkeypatch.setattr(runtime.store, "record_observation", lambda *a: None)
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.creation.validate_authorization", lambda *a: None)
    calls = []

    def browser(request):
        calls.append(request)
        if request["operation"] == "snapshot":
            return {"session_id": "ws~full", "busy": False, "unlocked": True, "preview_open": False}
        if request["operation"] == "click":
            image = output / (request["artifact_id"] + ".png")
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"OFFLINE NEW PIXELS")
            return {
                "actual_ui_click": True,
                "session_id": "ws~full",
                "screenshot": str(image),
                "console_inspection": {"readonly_completed": True, "identity_visible": True},
            }
        if request["operation"] == "source":
            file = output / (request["artifact_id"] + ".json")
            json_write(file, {"session_id": "ws~full", "references": {"items": []}})
            return {"source_evidence": str(file)}
        return {"submitted": True, "session_id": "ws~full"}

    monkeypatch.setattr(runtime, "browser", browser)
    clicked = runtime.tool("new-continuation", "fake", "click", {"scene": "R0083-ROS-1-open"})
    assert clicked["source_evidence"] == proof["capture_source"]["path"]
    assert calls[-1]["console_inspection"]["source_tool_use_id"] == "member-tool"
    assert calls[-1]["selector"].startswith('details[data-tool-use-id="member-tool"]')
    with pytest.raises(WorkflowError, match="unresolved creation/deletion"):
        runtime.tool("new-continuation", "fake", "request", {"dimension": "QUERY"})
    click_path = output / "R0083-ROS-1-open.json"
    valid_click = read_json(click_path)
    json_write(click_path, {**valid_click, "console_inspection": {"identity_visible": False}})
    before = len(calls)
    with pytest.raises(WorkflowError, match="announcement capture failed"):
        runtime.tool("new-continuation", "fake", "cleanup", {"dimension": "ROS"})
    assert len(calls) == before
    json_write(click_path, valid_click)
    runtime.tool("new-continuation", "fake", "cleanup", {"dimension": "ROS"})
    assert '"StackId":"own-stack"' in calls[-1]["text"]
    cred = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    for action, params, body in (
        (
            "DeleteStack",
            {"RegionId": "cn-hangzhou", "StackId": "own-stack", "RetainAllResources": False},
            {"RequestId": "offline-delete"},
        ),
        (
            "GetStack",
            {"RegionId": "cn-hangzhou", "StackId": "own-stack"},
            {"RequestId": "offline-root-absent", "StackId": "own-stack", "Status": "DELETE_COMPLETE"},
        ),
        (
            "OnsInstanceInServiceList",
            {"RegionId": "cn-hangzhou", "NeedResourceInfo": True},
            {"RequestId": "offline-instance-absent", "Data": {"InstanceVO": []}},
        ),
    ):
        ticket = gate.reserve(
            {
                "product": "ons" if action.startswith("Ons") else "ros",
                "action": action,
                "version": "2019-02-14" if action.startswith("Ons") else "2019-09-10",
                "region_id": "cn-hangzhou",
                "params": params,
            },
            "ws~full",
            cred,
            read_only=action != "DeleteStack",
            tool_use_id=action + "-tool",
        )
        gate.finish(ticket, body, True)
    runtime.tool("new-continuation", "fake", "cleanup-source", {"dimension": "ROS"})
    runtime.tool("new-continuation", "fake", "request", {"dimension": "QUERY"})
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        assert db.execute("SELECT scope FROM active").fetchone()[0] == "worker:QUERY"
        assert db.execute("SELECT used FROM grants WHERE scope='worker:ROS' AND id='create'").fetchone()[0] == 1
        assert db.execute("SELECT used FROM grants WHERE scope='worker:ROS' AND id='delete'").fetchone()[0] == 1
        assert all(r[0] == 0 for r in db.execute("SELECT used FROM grants WHERE scope='worker:QUERY'"))
    identifier = "MQ_INST_1234567890123456_Business"
    row = {"InstanceId": identifier, "InstanceName": "offline-own", "InstanceType": 1, "InstanceStatus": 5}
    query_body = {"RequestId": "offline-query", "Data": {"InstanceVO": [row]}}
    query_call = next(g["call"] for g in packet["execution_grants"]["QUERY"] if g["kind"] == "read")
    for grant in packet["execution_grants"]["QUERY"]:
        ticket = gate.reserve(
            grant["call"],
            "ws~full",
            cred,
            read_only=True,
            tool_use_id="current-query" if grant["kind"] == "read" else "query-identity",
        )
        gate.finish(
            ticket,
            query_body
            if grant["kind"] == "read"
            else {"AccountId": "1234567890123456", "RequestId": "offline-query-identity"},
            True,
        )
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        account_scope = db.execute("SELECT reference_scope FROM attempts WHERE tool_use_id='current-query'").fetchone()[
            0
        ]
    ref = {
        "identifier": identifier,
        "resourceType": spec["key"],
        "regionId": "cn-hangzhou",
        "accountScope": account_scope,
        "sources": [
            {
                "evidence": "api_response",
                "sourceKind": "tool_result",
                "product": "ons",
                "action": "OnsInstanceInServiceList",
                "toolUseId": tool,
                "turnId": tool + "-turn",
            }
            for tool in ("historical-cleanup", "current-query")
        ],
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": query_call,
        "results": [{"isError": False, "content": json.dumps(query_body)}],
    }
    json_write(
        output / "QUERY-product-source.json",
        {
            "session_id": "ws~full",
            "references": {"items": [ref]},
            "messages": {"tools": {"historical-cleanup": tool, "current-query": tool}},
        },
    )
    runtime.tool("new-continuation", "fake", "click", {"scene": "R0083-QUERY-1-open"})
    assert calls[-1]["console_inspection"]["source_tool_use_id"] == "current-query"


def test_retained_capture_consumes_separately_reviewed_identity_without_refilling_originals(retained):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import authorize_identity

    runtime, _ = retained
    path = runtime.root / "execution-gate.sqlite3"
    with gate.database(path) as db:
        original = dict(db.execute("SELECT * FROM attempts WHERE grant_id='create'").fetchone())
        identity = dict(db.execute("SELECT * FROM grants WHERE id='identity'").fetchone())
        before = [dict(g) for g in db.execute("SELECT * FROM grants ORDER BY id")]
    candidate = {
        "slot": "0",
        "scope": "worker:ROS",
        "session_id": "ws~full",
        "account": "1234567890123456",
        "original_phase": "cleanup",
        "authority": identity["authority"],
        "owned_from_wire": [
            {
                "creation_attempt_id": original["id"],
                "create_grant_id": "create",
                "identifier": "own-stack",
                "account": "1234567890123456",
                "region": "cn-hangzhou",
                "response_sha256": hashlib.sha256(
                    gate.canonical(json.loads(original["response"])).encode()
                ).hexdigest(),
            }
        ],
        "remaining_calls": [
            {"id": "identity", "kind": "identity", "remaining": 0, "call": json.loads(identity["call"])},
            {"id": "delete", "kind": "delete", "remaining": 1},
        ],
    }
    review = {
        "kind": "ops_cleanup_identity_review",
        "approved": True,
        **{k: candidate[k] for k in ("scope", "authority", "account", "owned_from_wire")},
        "session": "ws~full",
        "additional_identity_reads": 1,
        "identity_call": json.loads(identity["call"]),
        "reason": "OFFLINE explicit actual signer expiry review, no new resources",
    }
    json_write(runtime.root / "inputs/identity-renewal.json", review, private=True)
    added = authorize_identity(runtime.root, candidate, "inputs/identity-renewal.json")
    proof = retained_capture.inspect(runtime, "worker")
    assert proof["pending_cleanup_identity"]["id"] == added["grant_id"]
    assert proof["additional_identity_reads"] == 0  # This queue did not append the separate identity grant.
    json_write(runtime.root / "inputs/capture-with-identity.json", {**proof, "approved": True}, private=True)
    retained_capture.queue(runtime, "worker", "capture-with-identity.json")
    with runtime.store.transaction() as db:
        recovery = json.loads(
            db.execute("SELECT value FROM meta WHERE key=?", ("native_continuation:" + proof["task_id"],)).fetchone()[0]
        )
    assert "GetCallerIdentity" in retained_capture.identity_delivery(runtime.root, recovery)
    cred = AliyunCredential(mode="AK", access_key_id="fake-rotated", access_key_secret="fake-new-secret")
    ticket = gate.reserve(review["identity_call"], "ws~full", cred, read_only=True, tool_use_id="renewed-tool")
    gate.finish(ticket, {"AccountId": "1234567890123456", "RequestId": "offline-renewed"}, True)
    with pytest.raises(WorkflowError):
        retained_capture.identity_delivery(runtime.root, recovery)
    with gate.database(path) as db:
        assert all(dict(db.execute("SELECT * FROM grants WHERE id=?", (g["id"],)).fetchone()) == g for g in before)
        assert db.execute("SELECT used FROM grants WHERE id=?", (added["grant_id"],)).fetchone()[0] == 1
