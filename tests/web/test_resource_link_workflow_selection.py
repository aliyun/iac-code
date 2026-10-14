"""Staged dimensions retain frozen obligations and original cumulative authority, offline only."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow import selection
from scripts.web.e2e.resource_link_workflow.additional_source import dispatch_reservation_increment
from scripts.web.e2e.resource_link_workflow.creation import authorization_path, execution_authority, execution_digest
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.plans import expected_scenes
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store
from tests.web.test_resource_link_workflow import bound, observation, packet, reserve, resource


@pytest.fixture
def staged(tmp_path):
    store = Store.initialize(tmp_path / "state")
    json_write(store.root / "inputs/cloud.json", {"fixture": "offline source, not actual cloud evidence"})
    spec = resource()
    spec["dimensions"]["ROS"] = copy.deepcopy(spec["dimensions"]["QUERY"])
    spec["dimensions"]["ROS"]["contracts"][0]["id"] = "R0001-ROS-1"
    frozen = {"schema_version": 1, "plan_id": "offline", "resources": [spec]}
    store.import_plan(frozen, budget_cents=100)
    store.pause(False)
    prepare = reserve(store, "prepare")
    key, thread, turn = bound(store, prepare)
    context = store.context(prepare["job_id"], key)
    original = packet(store, context)
    original["requests"]["ROS"] = "Create only the reviewed offline fixture, then stop."
    original["cleanup_requests"] = {"ROS": "Delete only the original response-bound offline fixture."}
    original["cleanup_recipe"] = {"ROS": "Original response binding, then bounded absence read."}
    original["execution_grants"]["ROS"] = [
        copy.deepcopy(original["execution_grants"]["QUERY"][0]),
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": {
                "product": "ros",
                "action": "CreateStack",
                "region_id": "test",
                "params": {"StackName": "offline-only", "TemplateBody": "{}"},
            },
        },
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "call": {
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "test",
                "params": {"StackId": "$created_id"},
            },
        },
    ]
    json_write(store.root / "inputs/original.json", original)
    json_write(store.root / "inputs/original-review.json", {"approved": True, "fixture": "offline OPS review"})
    runtime = Runtime(store)
    runtime.authorize_creation(store.root / "inputs/original.json", "inputs/original-review.json", 100)
    store.approve_packet(prepare["job_id"], key, original)
    store.finish_job(prepare["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    store.submit(worker["job_id"], key, "Offline fixture: no cloud operation or click executed.")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    receipt = {
        "task_id": context["task_id"],
        "attempt": 1,
        "cleanup": [],
        "decisions": [
            {"scene": scene["id"], "status": "execution_gap", "reason": "Offline known execution gap"}
            for scene in context["expected_scenes"]
        ],
    }
    store.accept(review["job_id"], key, receipt)
    store.finish_job(review["job_id"], thread, turn, "completed")
    repair = reserve(store, "repair")
    key, thread, turn = bound(store, repair)
    store.block(repair["job_id"], key, "Offline deferred ROS needs independent OPS decision")
    store.finish_job(repair["job_id"], thread, turn, "completed")
    store.retry_blocked(context["task_id"], "Offline maintenance fixed staged selection")
    prepare = reserve(store, "prepare")
    key, thread, turn = bound(store, prepare)
    proposal = copy.deepcopy(original)
    proposal["requests"] = {"QUERY": original["requests"]["QUERY"]}
    proposal["execution_grants"] = {"QUERY": original["execution_grants"]["QUERY"]}
    proposal["scenes"] = {k: v for k, v in proposal["scenes"].items() if "-QUERY-" in k}
    proposal["cleanup_requests"] = {}
    proposal["cleanup_recipe"] = {"QUERY": "Read only; no new owned resources."}
    receipt_path = f"receipts/{review['job_id']}.json"
    proposal["execution_selection"] = {
        "dimensions": ["QUERY"],
        "deferred_scenes": {
            scene["id"]: {
                "reason": "Offline original ROS execution gap, still required",
                "path": receipt_path,
                "sha256": digest(store.root / receipt_path),
            }
            for scene in context["expected_scenes"]
            if scene["dimension"] == "ROS"
        },
    }
    proof = {
        "approved": True,
        "task_id": proposal["task_id"],
        "execution_selection_approved": copy.deepcopy(proposal["execution_selection"]),
        "carry_forward_dimensions": {
            "QUERY": {
                "proposal": "inputs/original.json",
                "proposal_sha256": digest(store.root / "inputs/original.json"),
                "execution_sha256": execution_digest(original),
            }
        },
    }
    return store, runtime, frozen, original, proposal, proof, prepare, key, thread, turn


def authorize(staged):
    store, runtime, _, _, proposal, proof, *_ = staged
    json_write(store.root / "inputs/selected.json", proposal)
    proof["proposal_sha256"] = digest(store.root / "inputs/selected.json")
    json_write(store.root / "inputs/selected-review.json", proof)
    runtime.authorize_creation(store.root / "inputs/selected.json", "inputs/selected-review.json", 100)


def approve(staged):
    store, _, _, _, proposal, _, job, key, thread, turn = staged
    authorize(staged)
    store.approve_packet(job["job_id"], key, proposal)
    store.finish_job(job["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    return worker, bound(store, worker)


def test_staged_query_acceptance_keeps_deferred_resource_blocked_and_reported(staged):
    store, _, frozen, original, proposal, _, *_ = staged
    worker, (key, thread, turn) = approve(staged)
    context = store.context(worker["job_id"], key)
    assert len(context["expected_scenes"]) == 2 and len(context["all_expected_scenes"]) == 5
    assert context["execution_selection"] == proposal["execution_selection"]
    assert execution_authority(store.root, proposal, "QUERY") == execution_authority(store.root, original, "QUERY")
    values = [observation(store, worker, scene["id"]) for scene in context["expected_scenes"]]
    for value in values:
        store.record_observation(worker["job_id"], key, value)
    with pytest.raises(WorkflowError, match="unexpected|outside|scene"):
        store.record_observation(worker["job_id"], key, observation(store, worker, "R0001-ROS-1-iframe"))
    store.submit(worker["job_id"], key, "Offline original PNG fixture, not actual resource PASS")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    assert len(store.context(review["job_id"], key)["expected_scenes"]) == 2
    for value in values:
        store.inspected(review["job_id"], key, value["screenshot"])
    receipt = {
        "task_id": proposal["task_id"],
        "attempt": 2,
        "cleanup": [],
        "decisions": [
            {
                "scene": value["scene"],
                "status": "pass",
                "reason": "Offline visual fixture only",
                "identity_verified": True,
                "login_verified": True,
                "render_verified": True,
            }
            for value in values
        ],
    }
    with pytest.raises(WorkflowError, match="all and only"):
        store.accept(
            review["job_id"],
            key,
            {
                **receipt,
                "decisions": receipt["decisions"]
                + [{"scene": "R0001-ROS-creation", "status": "execution_gap", "reason": "Must not re-record deferred"}],
            },
        )
    store.accept(review["job_id"], key, receipt)
    store.finish_job(review["job_id"], thread, turn, "completed")
    task = store.status()["tasks"][0]
    assert task["state"] == "blocked" and task["slot"] is None
    assert "deferred" in task["reason"] and store.status()["can_finalize"] is False
    assert all(slot["task_id"] is None for slot in store.status()["slots"])
    with sqlite3.connect(store.path) as connection:
        spec = json.loads(connection.execute("SELECT spec FROM tasks").fetchone()[0])
        assert json.loads(connection.execute("SELECT plan FROM runs").fetchone()[0]) == frozen
        assert len(connection.execute("SELECT * FROM verdicts WHERE status='pass'").fetchall()) == 2
        assert len(connection.execute("SELECT * FROM verdicts WHERE scene LIKE 'R0001-ROS%'").fetchall()) == 3
    assert {scene["id"] for scene in expected_scenes(spec)} == set(proposal["execution_selection"]["deferred_scenes"])
    assert spec["dimensions"]["ROS"] == frozen["resources"][0]["dimensions"]["ROS"]


@pytest.mark.parametrize(
    "damage",
    [
        "empty",
        "full",
        "duplicate",
        "unknown",
        "missing_scene",
        "extra_scene",
        "reason",
        "sha",
        "task",
        "thread",
        "receipt_job",
        "unrecorded",
    ],
)
def test_selection_rejects_unproven_or_erased_deferrals(staged, damage):
    store, _, _, _, proposal, *_ = staged
    selected = proposal["execution_selection"]
    deferred = selected["deferred_scenes"]
    first = next(iter(deferred))
    if damage in {"empty", "full", "duplicate", "unknown"}:
        selected["dimensions"] = {
            "empty": [],
            "full": ["QUERY", "ROS"],
            "duplicate": ["QUERY", "QUERY"],
            "unknown": ["TF"],
        }[damage]
    elif damage == "missing_scene":
        del deferred[first]
    elif damage == "extra_scene":
        deferred["not-required"] = copy.deepcopy(deferred[first])
    elif damage in {"reason", "sha"}:
        deferred[first]["reason" if damage == "reason" else "sha256"] = ""
    else:
        path = store.root / deferred[first]["path"]
        value = read_json(path)
        if damage == "task":
            value["task_id"] = "another-task"
        elif damage == "thread":
            value["sol_thread"] = "different-sol"
        elif damage == "unrecorded":
            value["decisions"][0]["reason"] = "unrecorded receipt"
        else:
            path = store.root / "receipts" / ("a" * 32 + ".json")
            for item in deferred.values():
                item["path"] = str(path.relative_to(store.root))
        json_write(path, value)
        for item in deferred.values():
            item["sha256"] = digest(path)
    with sqlite3.connect(store.path) as connection:
        spec = json.loads(connection.execute("SELECT spec FROM tasks").fetchone()[0])
    with pytest.raises(WorkflowError):
        selection.selected_scenes(store.root, proposal, spec)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_approval",
        "scope",
        "proposal",
        "carry_missing",
        "carry_extra",
        "quota",
        "old_sha",
        "authority",
        "evidence",
    ],
)
def test_selection_requires_independent_review_and_original_authority(staged, damage):
    store, runtime, _, _, proposal, proof, *_ = staged
    if damage == "missing_approval":
        proof["approved"] = False
    elif damage == "scope":
        proof["execution_selection_approved"]["dimensions"] = ["ROS"]
    elif damage == "carry_missing":
        proof["carry_forward_dimensions"] = {}
    elif damage == "carry_extra":
        proof["carry_forward_dimensions"]["ROS"] = copy.deepcopy(proof["carry_forward_dimensions"]["QUERY"])
    elif damage == "quota":
        proposal["execution_grants"]["QUERY"][0]["quota"] += 1
    elif damage == "old_sha":
        proof["carry_forward_dimensions"]["QUERY"]["proposal_sha256"] = "0" * 64
    if damage in {"authority", "evidence"}:
        authorize(staged)
        if damage == "authority":
            path = authorization_path(store.root, proposal)
            value = read_json(path)
            value["dimension_authorities"]["QUERY"] = execution_digest(proposal) + ":QUERY"
            json_write(path, value)
        else:
            json_write(store.root / "inputs/selected-review.json", {"approved": True})
        with pytest.raises(WorkflowError):
            selection.validate_authorization(store.root, proposal)
    elif damage == "proposal":
        json_write(store.root / "inputs/selected.json", proposal)
        proof["proposal_sha256"] = "0" * 64
        json_write(store.root / "inputs/selected-review.json", proof)
        with pytest.raises(WorkflowError, match="full final proposal"):
            runtime.authorize_creation(store.root / "inputs/selected.json", "inputs/selected-review.json", 100)
    else:
        with pytest.raises(WorkflowError):
            authorize(staged)


@pytest.mark.parametrize("operation", ["request", "source", "cleanup", "cleanup-source", "continue-read"])
def test_deferred_dimensions_cannot_install_capabilities_or_touch_browser(staged, monkeypatch, operation):
    store, runtime, *_ = staged
    worker, (key, _, _) = approve(staged)
    with store.transaction() as connection:
        connection.execute("UPDATE slots SET target='offline-isolated-target'")
    monkeypatch.setattr(runtime, "browser", lambda *_: pytest.fail("deferred dimension cannot touch browser"))
    with pytest.raises(WorkflowError, match="outside.*active execution scope"):
        runtime.tool(worker["job_id"], key, operation, {"dimension": "ROS"})
    assert not (store.root / "execution-gate.sqlite3").exists()


@pytest.mark.parametrize("operation", ["click", "evidence"])
def test_deferred_scenes_cannot_capture_or_replay_browser_evidence(staged, monkeypatch, operation):
    store, runtime, *_ = staged
    worker, (key, _, _) = approve(staged)
    with store.transaction() as connection:
        connection.execute("UPDATE slots SET target='offline-isolated-target'")
    monkeypatch.setattr(runtime, "browser", lambda *_: pytest.fail("deferred scene cannot touch browser"))
    with pytest.raises(WorkflowError, match="scene is outside"):
        runtime.tool(worker["job_id"], key, operation, {"scene": "R0001-ROS-1-iframe"})


@pytest.mark.parametrize("field", ["requests", "scenes", "inputs", "proposal_file"])
def test_selection_binds_full_proposal_not_only_grant_digest(staged, field):
    store, _, _, _, proposal, _, job, key, *_ = staged
    authorize(staged)
    if field == "requests":
        proposal["requests"]["QUERY"] += " drift"
    elif field == "scenes":
        next(iter(proposal["scenes"].values()))["selector"] = "altered"
    elif field == "inputs":
        proposal["inputs"] = []
    else:
        json_write(store.root / "inputs/selected.json", {**proposal, "altered": True})
    with pytest.raises(WorkflowError, match="full proposal"):
        store.approve_packet(job["job_id"], key, proposal)


@pytest.mark.parametrize("reuse", [True, False])
def test_paid_selection_reuses_original_envelope_only_after_exact_fee_review(staged, reuse):
    store, _, _, _, proposal, proof, *_ = staged
    proposal["budget_cents"] = 100
    json_write(
        store.root / "inputs/human.json",
        {
            "kind": "human_fee_authorization",
            "approved": True,
            "task_id": proposal["task_id"],
            "plan_id": "offline",
            "max_budget_cents": 100,
            "verbatim_user_message": "offline test permission, not real user authority",
        },
    )
    review = {
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(store.root / "inputs/human.json"),
        "cost_evidence": "inputs/cost.json",
    }
    cost = {
        "kind": "ops_cumulative_fee_review",
        "approved": True,
        "task_id": proposal["task_id"],
        "account": "fixture",
        "budget_cents": 100,
        "execution_sha256": execution_digest(proposal),
        "additional_operations": [],
        "human_authorization": review["human_authorization"],
        "human_sha256": review["human_sha256"],
        "includes_previous_operations": True,
        "estimate_basis": "offline cumulative prior and remaining envelope",
        "limitations": "Not actual billing",
        "official_sources": [{"path": "inputs/cloud.json", "sha256": digest(store.root / "inputs/cloud.json")}],
    }
    json_write(store.root / "inputs/cost.json", cost)
    review["cost_sha256"] = digest(store.root / "inputs/cost.json")
    proof["paid_operation_review"] = review
    with store.transaction() as connection:
        connection.execute("UPDATE budget_reservations SET amount=100")
    if reuse:
        proof["budget_reservation_reuse"] = {
            "task_id": proposal["task_id"],
            "reserved_cents": 100,
            "cost_evidence": review["cost_evidence"],
            "cost_sha256": review["cost_sha256"],
        }
    authorize(staged)
    assert dispatch_reservation_increment(store.root, proposal) == (0 if reuse else 100)
    if reuse:
        job, key, thread, turn = staged[6:]
        store.approve_packet(job["job_id"], key, proposal)
        store.finish_job(job["job_id"], thread, turn, "completed")
        reserve(store, "dispatch")
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("SELECT amount FROM budget_reservations").fetchone() == (100,)


def test_paid_selection_cannot_waive_fee_review(staged):
    staged[4]["budget_cents"] = 100
    with pytest.raises(WorkflowError, match="human permission"):
        authorize(staged)


def test_selection_loading_and_legacy_execution_digest_are_preserved(staged, monkeypatch):
    _, runtime, _, original, _, *_ = staged
    expected = {
        "task_id": original["task_id"],
        "budget_cents": original["budget_cents"],
        "execution_grants": original["execution_grants"],
        "accounts": ["fixture"],
    }
    canonical = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert execution_digest(original) == hashlib.sha256(canonical.encode()).hexdigest()
    assert runtime.attest_control()["execution_selection"]["loaded_sha256"] == digest(Path(selection.__file__))
    monkeypatch.setattr(selection, "LOADED_SHA", "stale-selection")
    with pytest.raises(WorkflowError, match="selection code changed"):
        runtime.attest_control()
