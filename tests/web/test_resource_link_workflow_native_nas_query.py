"""Own NAS QUERY sample admission. All identities, prices and API responses are offline fakes."""

import copy
import hashlib
import json

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.creation import execution_digest, validate_execution
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, nas_native_plan
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.hourly_budget import register
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_execution_gate import NATIVE_IDS, NATIVE_NAME, native_complete, native_wire
from tests.web.test_resource_link_workflow import PNG, bound, reserve, resource
from tests.web.test_resource_link_workflow_hourly_budget import operation_fixture


@pytest.fixture(params=gate.NAS_NATIVE_PLAN_VARIANTS)
def reviewed(tmp_path, request):
    store = Store.initialize(tmp_path / "state")
    spec = resource(36)
    spec.update(key="ALIYUN::NAS::DataFlow", execution_policy_version=2)
    spec["dimensions"]["QUERY"]["contracts"][0].update(
        source_type="ALIYUN::NAS::DataFlow",
        historical={"iframe": "fail", "open": "pass"},
    )
    plan = {"schema_version": 1, "plan_id": "native-nas-offline", "resources": [spec]}
    store.import_plan(plan)
    spec["retest_scope"] = {"kind": "failed_navigation_managed", "scene_ids": ["R0036-QUERY-1-iframe"]}
    with store.transaction() as db:
        db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
        db.execute("UPDATE runs SET plan=?", (dumps(plan),))
        task = db.execute("SELECT * FROM tasks").fetchone()
    frozen = hashlib.sha256(dumps(plan).encode()).hexdigest()
    human = {
        "schema_version": 1,
        "kind": "human_hourly_fee_authorization",
        "approved": True,
        "plan_id": plan["plan_id"],
        "currency": "CNY",
        "resource_ids": ["R0036"],
        "scene_ids": ["R0036-QUERY-1-iframe"],
        "max_concurrent_hourly_cents": 1000,
        "total_budget_cents": None,
        "verbatim_user_message": "OFFLINE FAKE AUTHORIZATION",
    }
    json_write(store.root / "inputs/human.json", human)
    allowance = {
        "schema_version": 1,
        "kind": "ops_run_hourly_fee_review",
        "approved": True,
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": frozen,
        "currency": "CNY",
        "max_concurrent_hourly_cents": 1000,
        "resource_ids": ["R0036"],
        "scene_ids": ["R0036-QUERY-1-iframe"],
        "human_authorization": {"path": "inputs/human.json", "sha256": digest(store.root / "inputs/human.json")},
    }
    json_write(store.root / "inputs/hourly-review.json", allowance)
    register(store, plan["plan_id"], "inputs/hourly-review.json")
    packet, cost, operation = operation_fixture(store, task, 900)
    packet.update(
        execution_grants={"QUERY": nas_native_plan(NATIVE_NAME, variant=request.param)},
        requests={"QUERY": "Execute only approved own graph then query its DataFlow"},
        cleanup_requests={"QUERY": "Reverse cleanup of original response-bound objects and absence"},
        cleanup_recipe="Own DataFlow/export/protocol/fileset/FS/bucket/SG/ENI/networks only",
    )
    for scene in packet["scenes"].values():
        scene.update(
            account="1234567890123456",
            region="cn-hangzhou",
            identifier="{identifier}",
            selector='a.resource-link[aria-label="{identifier}"]',
            expected_type="ALIYUN::NAS::DataFlow",
            render_text=[NATIVE_NAME],
        )
    cost.update(
        account="1234567890123456",
        execution_grants_sha256=hashlib.sha256(canonical(packet["execution_grants"]).encode()).hexdigest(),
    )
    json_write(store.root / "inputs/R0036-cost.json", cost)
    packet["hourly_cost"]["cost_evidence"]["sha256"] = digest(store.root / "inputs/R0036-cost.json")
    json_write(store.root / "inputs/proposal.json", packet)
    operation["hourly_operation_review"].update(
        execution_sha256=execution_digest(packet), hourly_cost=packet["hourly_cost"]
    )
    operation["proposal_sha256"] = digest(store.root / "inputs/proposal.json")
    json_write(store.root / "inputs/operation.json", operation)
    source = {
        "kind": "ops_native_nas_query_source_review",
        "approved": True,
        "task_id": task["id"],
        "account": "1234567890123456",
        "execution_sha256": execution_digest(packet),
        "sample_binding": {
            "name": NATIVE_NAME,
            "region": "cn-hangzhou",
            "execution_grants_sha256": cost["execution_grants_sha256"],
        },
        "full_native_graph_reviewed": True,
        "implicit_dependencies_reviewed": True,
        "no_existing_resource_mutation": True,
        "all_new_resources_response_bound_cleanup": True,
        "no_object_io": True,
        "no_posix_client": True,
        "estimate_basis": "offline only",
        "limitations": "not real price/source/authorization",
        "official_sources": cost["components"][0]["official_sources"],
    }
    json_write(store.root / "inputs/source-review.json", source)
    proof = {
        "schema_version": 1,
        "kind": "ops_native_nas_query_sample_admission",
        "approved": True,
        "task_id": task["id"],
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": frozen,
        "execution_sha256": execution_digest(packet),
    }
    for key, path in (
        ("proposal", "inputs/proposal.json"),
        ("source_review", "inputs/source-review.json"),
        ("operation_review", "inputs/operation.json"),
    ):
        proof[key] = {"path": path, "sha256": digest(store.root / path)}
    json_write(store.root / "inputs/admission.json", proof)
    return store, packet, proof


def test_native_query_default_stays_readonly_and_packet_flags_do_not_grant_writes(reviewed):
    store, packet, _ = reviewed
    packet["native_nas_query_sample"] = True
    with pytest.raises(WorkflowError):
        validate_execution(packet, {"QUERY"}, require_read_policy=True)
    with pytest.raises(WorkflowError):
        Runtime(store).authorize_creation(store.root / "inputs/proposal.json", "inputs/operation.json", 0)
    assert not list((store.root / "creation-authorizations").glob("*.json"))


def test_native_query_admission_authorization_and_packet_preserve_original_targets(reviewed):
    store, packet, _ = reviewed
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        original_spec = task["spec"]
        assert store.readonly_dimensions(db, task, packet) == {"QUERY"}
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")["no_cloud_call"]
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")["already_recorded"]
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert task["spec"] == original_spec
        assert store.readonly_dimensions(db, task, packet) == set()
        assert admitted(store.root, db, task, packet) == {"QUERY": execution_digest(packet) + ":QUERY"}
    assert (
        Runtime(store).authorize_creation(
            store.root / "inputs/proposal.json",
            "inputs/operation.json",
            0,
        )["cloud_write_performed"]
        is False
    )
    store.pause(False)
    job = reserve(store, "prepare")
    token, thread, turn = bound(store, job)
    store.approve_packet(job["job_id"], token, packet)
    store.finish_job(job["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    worker_token, _, _ = bound(store, worker)
    assert store.context(worker["job_id"], worker_token)["native_nas_query_sample"] is True
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM owned").fetchone()[0] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "human",
        "proposal",
        "source",
        "operation",
        "cost",
        "approval",
        "frozen",
        "scene",
        "resource",
        "quota",
        "cleanup",
        "request",
        "account",
        "region",
        "active",
        "pause",
        "attempt",
        "worker",
    ],
)
def test_native_query_admission_rejects_drift_and_refills(reviewed, damage):
    store, packet, proof = reviewed
    if damage in {"human", "proposal", "source", "operation", "cost"}:
        path = {
            "human": "human.json",
            "proposal": "proposal.json",
            "source": "source-review.json",
            "operation": "operation.json",
            "cost": "R0036-cost.json",
        }[damage]
        value = read_json(store.root / "inputs" / path)
        value["tampered"] = True
        json_write(store.root / "inputs" / path, value)
    elif damage in {"approval", "frozen"}:
        proof["approved" if damage == "approval" else "frozen_plan_sha256"] = False
        json_write(store.root / "inputs/admission.json", proof)
    elif damage in {"scene", "quota", "cleanup", "request", "account", "region"}:
        if damage == "scene":
            packet["scenes"]["R0036-QUERY-1-open"] = packet["scenes"]["R0036-QUERY-1-iframe"]
        elif damage == "quota":
            next(g for g in packet["execution_grants"]["QUERY"] if g["id"] == "create-dataflow")["quota"] = 2
        elif damage == "cleanup":
            packet["cleanup_requests"] = {}
        elif damage == "request":
            packet["requests"]["QUERY"] = " "
        else:
            packet["scenes"]["R0036-QUERY-1-iframe"][damage] = "wrong"
        json_write(store.root / "inputs/proposal.json", packet)
        proof["proposal"]["sha256"] = digest(store.root / "inputs/proposal.json")
        proof["execution_sha256"] = execution_digest(packet)
        operation = read_json(store.root / "inputs/operation.json")
        operation["proposal_sha256"] = proof["proposal"]["sha256"]
        json_write(store.root / "inputs/operation.json", operation)
        proof["operation_review"]["sha256"] = digest(store.root / "inputs/operation.json")
        json_write(store.root / "inputs/admission.json", proof)
    elif damage == "pause":
        store.pause(False)
    else:
        with store.transaction() as db:
            if damage == "resource":
                spec = json.loads(db.execute("SELECT spec FROM tasks").fetchone()[0])
                spec["key"] = "ALIYUN::NAS::FileSystem"
                db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
            elif damage == "attempt":
                db.execute("UPDATE tasks SET attempt=1")
            elif damage == "worker":
                db.execute(
                    "INSERT INTO jobs VALUES('old',?,'glm','dispatch','complete','t','u','','',1)", (packet["task_id"],)
                )
            else:
                db.execute("INSERT INTO actions VALUES('pending',NULL,'prepare','issued',1)")
    with pytest.raises(WorkflowError):
        store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")


@pytest.mark.parametrize("field", ["requests", "ui", "hourly_cost", "scenes", "execution_grants"])
def test_native_admitted_full_proposal_cannot_change_or_refill_by_new_attempt(reviewed, field):
    store, packet, _ = reviewed
    store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")
    changed = copy.deepcopy(packet)
    changed[field] = {}
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        with pytest.raises(WorkflowError):
            admitted(store.root, db, task, changed)


@pytest.mark.parametrize("damage", [None, "foreign_id", "foreign_tool", "unhealthy", "cleanup"])
def test_actual_runtime_click_binds_own_native_source_not_an_existing_resource(reviewed, monkeypatch, damage):
    store, packet, _ = reviewed
    store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")
    runtime = Runtime(store)
    runtime.authorize_creation(store.root / "inputs/proposal.json", "inputs/operation.json", 0)
    store.pause(False)
    prep = reserve(store, "prepare")
    token, thread, turn = bound(store, prep)
    store.approve_packet(prep["job_id"], token, packet)
    store.finish_job(prep["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    job = worker["job_id"]
    token, _, _ = bound(store, worker)
    with store.transaction() as db:
        db.execute("UPDATE slots SET target='offline-native-target' WHERE task_id=?", (packet["task_id"],))
    context = store.context(job, token)
    assert context["native_nas_query_sample"] is True
    output = store.root / "jobs" / job / "output"
    session, account, scene = "ws~native", "1234567890123456", "R0036-QUERY-1-iframe"
    slot = context["slot"]
    json_write(
        output / "self-created-session.json",
        {"job_id": job, "session_id": session, "origin": slot["origin"], "target": slot["target"]},
    )
    json_write(output / "new-session-intent.json", {"previous_session": "previous-offline-session"})
    path = store.root / "execution-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, str(slot["id"]))
    gate.install(path, str(slot["id"]), job + ":QUERY", session, account, None, packet["execution_grants"]["QUERY"])
    signer = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    fake_native = path, signer, {g["id"]: g for g in packet["execution_grants"]["QUERY"]}, account
    native_complete(fake_native)
    native_wire(fake_native, "query-dataflow-source")
    own = gate.nas_native_navigation_source(path, job + ":QUERY", session, account)
    reference = {
        "identifier": NATIVE_IDS["create-dataflow"],
        "resourceType": "ALIYUN::NAS::DataFlow",
        "regionId": "cn-hangzhou",
        "accountScope": own["reference_scope"],
        "sources": [
            {
                "toolUseId": own["tool_use_id"],
                "turnId": "own-turn",
                "sourceKind": "tool_result",
                "evidence": "api_response",
                "product": "nas",
                "action": "DescribeDataFlows",
            }
        ],
    }
    if damage == "foreign_id":
        reference["identifier"] = "df-business"
    elif damage == "foreign_tool":
        reference["sources"][0]["toolUseId"] = "old-query"
    elif damage == "unhealthy":
        with gate.database(path) as db:
            row = db.execute("SELECT id,response FROM attempts WHERE grant_id='query-dataflow-source'").fetchone()
            raw = json.loads(row["response"])
            raw["body"]["DataFlowInfo"]["DataFlow"][0]["Status"] = "Misconfigured"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (dumps(raw), row["id"]))
    elif damage == "cleanup":
        gate.cleanup(path, str(slot["id"]), job + ":QUERY", session)
    json_write(output / "QUERY-product-source.json", {"session_id": session, "references": {"items": [reference]}})
    calls = []

    def browser(request):
        calls.append(request)
        if request["operation"] == "snapshot":
            return {"session_id": session, "unlocked": True, "busy": False, "preview_open": False}
        if request["operation"] == "request":
            assert request["text"] == packet["cleanup_requests"]["QUERY"]
            return {"session_id": session, "submitted": True}
        if request["operation"] == "source":
            evidence = output / "QUERY-cleanup-source.json"
            json_write(evidence, {"session_id": session, "offline_fake": True})
            return {"session_id": session, "source_evidence": str(evidence)}
        assert request["operation"] == "click" and request["mode"] == "iframe"
        assert request["source_identifier"] == "df-own"
        assert request["source_card_tool_use_id"] == own["tool_use_id"]
        screenshot = output / (scene + ".png")
        screenshot.write_bytes(PNG)
        return {"session_id": session, "actual_ui_click": True, "screenshot": str(screenshot)}

    monkeypatch.setattr(runtime, "browser", browser)
    if damage:
        with pytest.raises(WorkflowError):
            runtime.tool(job, token, "click", {"scene": scene})
        assert not calls
    else:
        result = runtime.tool(job, token, "click", {"scene": scene})
        assert result["source_evidence"] == (output / "QUERY-product-source.json").relative_to(store.root).as_posix()
        assert result["source_sha256"] == digest(output / "QUERY-product-source.json")
        assert len(calls) == 1
        runtime.tool(job, token, "cleanup", {"dimension": "QUERY"})
        for target in ("dataflow", "export", "protocol", "fileset", "filesystem", "bucket"):
            native_wire(fake_native, "delete-" + target)
            native_wire(fake_native, "delete-" + target + "-absence")
        for side in ("filesystem", "export"):
            native_wire(fake_native, "read-" + side + "-implicit-sg-absence")
            native_wire(fake_native, "read-" + side + "-eni-absence")
            for target in ("vswitch", "vpc"):
                native_wire(fake_native, "delete-" + side + "-" + target)
                native_wire(fake_native, "delete-" + side + "-" + target + "-absence")
        cleanup = runtime.tool(job, token, "cleanup-source", {"dimension": "QUERY"})
        records = cleanup["original_cleanup_responses"]
        assert len([row for row in records if row["kind"] == "original_cleanup_delete_response"]) == 10
        assert cleanup["absence_verified"] is False and cleanup["eligible_for_automatic_verdict"] is False
        assert next(row for row in records if row["grant_id"] == "delete-bucket")["response"]["status"] == 204
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0  # Click alone cannot award PASS.


def test_native_closed_variant_cannot_reuse_another_variant_admission(reviewed):
    store, packet, _ = reviewed
    store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")
    changed = copy.deepcopy(packet)
    current = next(g for g in packet["execution_grants"]["QUERY"] if g["id"] == "create-filesystem")
    other = "bandwidth_omission_bwo1" if "Bandwidth" in current["call"]["params"] else "legacy"
    changed["execution_grants"]["QUERY"] = nas_native_plan(NATIVE_NAME, variant=other)
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        with pytest.raises(WorkflowError):
            admitted(store.root, db, task, changed)
    assert execution_digest(changed) != execution_digest(packet)
