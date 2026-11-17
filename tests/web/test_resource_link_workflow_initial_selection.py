"""Offline first QUERY stage retains missing managed TF; never a fake prior receipt."""

import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow import selection
from scripts.web.e2e.resource_link_workflow.creation import authorization_path, execution_authority, execution_digest
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.plans import expected_scenes
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from tests.web.test_resource_link_workflow import bound, observation, packet, reserve, resource


@pytest.fixture
def initial(tmp_path):
    store = Store.initialize(tmp_path / "state")
    spec = resource()
    spec["dimensions"]["TF"] = copy.deepcopy(spec["dimensions"]["QUERY"])
    spec["dimensions"]["TF"]["execution"] = "ros_managed_terraform"
    spec["dimensions"]["TF"]["contracts"][0]["id"] = "R0001-TF-1"
    # Actual first-stage preparation does not claim support for the deferred
    # managed source. A supported fixture masked the approval-order bug.
    spec["dimensions"]["TF"]["support"] = "unknown"
    plan = {"schema_version": 1, "plan_id": "offline-initial", "resources": [spec]}
    store.import_plan(plan)
    # Offline persisted scope fixture, not a real reviewed plan or cloud source.
    spec["retest_scope"] = {
        "kind": "failed_navigation_managed",
        "scene_ids": [f"R0001-{dimension}-1-{mode}" for dimension in ("QUERY", "TF") for mode in ("iframe", "open")],
    }
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET spec=?", (dumps(spec),))
        connection.execute("UPDATE runs SET plan=?", (dumps(plan),))
    json_write(store.root / "inputs/cloud.json", {"fixture": "offline only"})
    store.pause(False)
    original_job = reserve(store, "prepare")
    key, thread, turn = bound(store, original_job)
    context = store.context(original_job["job_id"], key)
    path = "inputs/R0001/missing-source.json"
    json_write(
        store.root / path,
        {
            "schema_version": 1,
            "kind": "preparation_missing_input_handoff",
            "purpose": "prepare",
            "job_id": original_job["job_id"],
            "task_id": context["task_id"],
            "ready_for_approve": False,
            "frozen_scope": {"scene_ids": [scene["id"] for scene in expected_scenes(spec)]},
            "reason": "Offline TF source unavailable; QUERY fixture may be independently reviewed.",
        },
    )
    store.block(
        original_job["job_id"],
        key,
        "Offline genuine preparation input gap",
        handoff={"category": "input", "artifacts": [{"path": path, "sha256": digest(store.root / path)}]},
    )
    store.finish_job(original_job["job_id"], thread, turn, "completed")
    store.retry_blocked(context["task_id"], "Offline OPS can prepare only first QUERY stage")
    prepare = reserve(store, "prepare")
    key, thread, turn = bound(store, prepare)
    proposal = packet(store, store.context(prepare["job_id"], key))
    proposal["scenes"] = {key: value for key, value in proposal["scenes"].items() if "-QUERY-" in key}
    for entry in proposal["execution_grants"]["QUERY"]:
        entry["quota"] = 1
    proposal["preparation_selection"] = {
        "job_id": original_job["job_id"],
        "thread_id": "thread-" + original_job["job_id"],
        "turn_id": "turn-" + original_job["job_id"],
        "path": path,
        "sha256": digest(store.root / path),
        "frozen_plan_sha256": hashlib.sha256(dumps(plan).encode()).hexdigest(),
    }
    # Use the actual offline transport binding, not guessed test IDs.
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT thread_id,turn_id FROM jobs WHERE id=?", (original_job["job_id"],)).fetchone()
    proposal["preparation_selection"].update(thread_id=row[0], turn_id=row[1])
    proposal["execution_selection"] = {
        "dimensions": ["QUERY"],
        "deferred_scenes": {
            scene["id"]: {
                "reason": "TF still required, original missing source",
                "path": path,
                "sha256": digest(store.root / path),
            }
            for scene in expected_scenes(spec)
            if scene["dimension"] == "TF"
        },
    }
    proof = {
        "kind": "ops_initial_selection_review",
        "approved": True,
        "task_id": proposal["task_id"],
        "execution_selection_approved": copy.deepcopy(proposal["execution_selection"]),
        "preparation_selection_approved": copy.deepcopy(proposal["preparation_selection"]),
        "carry_forward_dimensions": {},
    }
    return store, Runtime(store), proposal, proof, prepare, key, thread, turn


def authorize(initial):
    store, runtime, proposal, proof, *_ = initial
    json_write(store.root / "inputs/selected.json", proposal)
    proof["proposal_sha256"] = digest(store.root / "inputs/selected.json")
    json_write(store.root / "inputs/review.json", proof)
    runtime.authorize_creation(store.root / "inputs/selected.json", "inputs/review.json", 0)


def test_initial_stage_uses_real_preparation_not_fabricated_verdict_and_keeps_tf(initial):
    store, _, proposal, _, prepare, key, thread, turn = initial
    authorize(initial)
    before = execution_authority(store.root, proposal, "QUERY")
    store.approve_packet(prepare["job_id"], key, proposal)
    store.finish_job(prepare["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    context = store.context(worker["job_id"], key)
    assert len(context["expected_scenes"]) == 2 and len(context["all_expected_scenes"]) == 4
    assert execution_authority(store.root, proposal, "QUERY") == before
    selection.validate_authorization(store.root, proposal)
    values = [observation(store, worker, scene["id"]) for scene in context["expected_scenes"]]
    for value in values:
        store.record_observation(worker["job_id"], key, value)
    store.submit(worker["job_id"], key, "Offline fixture, no actual cloud calls or PASS")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    for value in values:
        store.inspected(review["job_id"], key, value["screenshot"])
    receipt = {
        "task_id": proposal["task_id"],
        "attempt": 1,
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
    store.accept(review["job_id"], key, receipt)
    store.finish_job(review["job_id"], thread, turn, "completed")
    task = store.status()["tasks"][0]
    assert task["state"] == "blocked" and task["slot"] is None and not store.status()["can_finalize"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM verdicts WHERE scene LIKE '%-TF-%'").fetchone()[0] == 0
    with pytest.raises(WorkflowError):
        selection.validate_preparation_selection(store.root, proposal, admission=True)


@pytest.mark.parametrize(
    "damage",
    [
        "sha",
        "thread",
        "job",
        "plan",
        "event",
        "frozen_scope",
        "tf_reason",
        "write",
        "quota",
        "cleanup",
        "additional",
        "selection",
        "carry",
        "review_kind",
        "review_marker",
        "previous_worker",
        "previous_owner",
        "previous_verdict",
        "previous_authority",
        "previous_capability",
        "previous_packet",
    ],
)
def test_initial_selection_rejects_fake_provenance_or_replayed_authority(initial, damage):
    store, _, proposal, proof, *_ = initial
    if damage in {"sha", "thread", "job", "plan"}:
        field = {"sha": "sha256", "thread": "thread_id", "job": "job_id", "plan": "frozen_plan_sha256"}[damage]
        proposal["preparation_selection"][field] = "wrong"
    elif damage == "event":
        with store.transaction() as connection:
            connection.execute("DELETE FROM events WHERE kind='task_blocked'")
    elif damage == "frozen_scope":
        path = store.root / proposal["preparation_selection"]["path"]
        value = read_json(path)
        value["frozen_scope"]["scene_ids"].pop()
        json_write(path, value)
        proposal["preparation_selection"]["sha256"] = digest(path)
    elif damage == "tf_reason":
        next(iter(proposal["execution_selection"]["deferred_scenes"].values()))["reason"] = ""
    elif damage == "write":
        proposal["execution_grants"]["QUERY"][1]["kind"] = "create"
    elif damage == "quota":
        proposal["execution_grants"]["QUERY"][0]["quota"] = 2
    elif damage == "cleanup":
        proposal["cleanup_requests"] = {"QUERY": "delete"}
    elif damage == "additional":
        proposal["additional_source_requests"] = []
    elif damage == "selection":
        del proposal["execution_selection"]
    elif damage == "carry":
        proof["carry_forward_dimensions"] = {"QUERY": {}}
    elif damage == "review_kind":
        proof["kind"] = "generic_approval"
    elif damage == "review_marker":
        proof["preparation_selection_approved"] = {}
    elif damage == "previous_authority":
        json_write(
            store.root / "creation-authorizations/other.json",
            {"task_id": proposal["task_id"], "execution_sha256": "old"},
        )
    elif damage == "previous_capability":
        from scripts.web.e2e.resource_link_workflow import execution_gate as gate

        gate.initialize(store.root / "execution-gate.sqlite3")
        gate.install(
            store.root / "execution-gate.sqlite3",
            slot="0",
            scope=proposal["preparation_selection"]["job_id"] + ":QUERY",
            session="offline",
            account="fixture",
            grants=proposal["execution_grants"]["QUERY"],
            expires=None,
        )
    else:
        with store.transaction() as connection:
            if damage == "previous_worker":
                connection.execute(
                    "UPDATE jobs SET role='glm' WHERE id=?", (proposal["preparation_selection"]["job_id"],)
                )
            elif damage == "previous_packet":
                connection.execute("UPDATE tasks SET packet='{}'")
            elif damage == "previous_owner":
                connection.execute(
                    "INSERT INTO owned VALUES('old',?,'QUERY','{}','deleted','old')", (proposal["task_id"],)
                )
            elif damage == "previous_verdict":
                connection.execute(
                    "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,'fail','{}',0)",
                    (proposal["task_id"], "R0001-QUERY-1-iframe"),
                )
    with pytest.raises(WorkflowError):
        authorize(initial)


def test_initial_selection_binds_full_proposal_and_authority(initial):
    store, _, proposal, _, *_ = initial
    authorize(initial)
    assert execution_authority(store.root, proposal, "QUERY") == execution_digest(proposal) + ":QUERY"
    path = authorization_path(store.root, proposal)
    value = read_json(path)
    value["dimension_authorities"] = {"QUERY": "different"}
    json_write(path, value)
    with pytest.raises(WorkflowError, match="sole reviewed operation authority"):
        selection.validate_authorization(store.root, proposal)


def test_initial_selection_authorize_is_idempotent_but_not_a_second_plan(initial):
    store, _, proposal, proof, *_ = initial
    authorize(initial)
    authorize(initial)
    proposal["execution_grants"]["QUERY"][1]["call"]["params"] = {"drift": "new authority"}
    proof["preparation_selection_approved"] = copy.deepcopy(proposal["preparation_selection"])
    with pytest.raises(WorkflowError, match="second operation authority"):
        authorize(initial)


@pytest.mark.parametrize("field", ["requests", "scenes", "inputs", "proposal_file"])
def test_initial_selection_rejects_full_proposal_drift(initial, field):
    store, _, proposal, _, prepare, key, *_ = initial
    authorize(initial)
    if field == "requests":
        proposal["requests"]["QUERY"] += " changed"
    elif field == "scenes":
        next(iter(proposal["scenes"].values()))["selector"] = "different"
    elif field == "inputs":
        proposal["inputs"] = []
    else:
        json_write(store.root / "inputs/selected.json", {**proposal, "changed": True})
    with pytest.raises(WorkflowError, match="full proposal"):
        store.approve_packet(prepare["job_id"], key, proposal)


def test_initial_stage_still_rejects_unknown_active_query_support(initial):
    store, _, proposal, _, prepare, key, *_ = initial
    authorize(initial)
    with store.transaction() as connection:
        row = connection.execute("SELECT spec FROM tasks WHERE id=?", (proposal["task_id"],)).fetchone()
        spec = json.loads(row[0])
        spec["dimensions"]["QUERY"]["support"] = "unknown"
        connection.execute("UPDATE tasks SET spec=? WHERE id=?", (dumps(spec), proposal["task_id"]))
    with pytest.raises(WorkflowError, match="resolve four dimensions"):
        store.approve_packet(prepare["job_id"], key, proposal)
    assert store.status()["tasks"][0]["state"] == "preparing"


def test_initial_stage_unknown_deferred_support_does_not_bypass_ops_review(initial):
    store, _, proposal, _, prepare, key, *_ = initial
    with pytest.raises(WorkflowError, match="authorization|review"):
        store.approve_packet(prepare["job_id"], key, proposal)
    assert store.status()["tasks"][0]["state"] == "preparing"


@pytest.fixture
def continued_preparation(initial):
    """All wire rows below are isolated offline fixtures, never real cloud evidence."""
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    store, runtime, old, _, prepare, key, thread, turn = initial
    authorize(initial)
    store.approve_packet(prepare["job_id"], key, old)
    store.finish_job(prepare["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    scope = worker["job_id"] + ":QUERY"
    authority = execution_authority(store.root, old, "QUERY")
    gate_path = store.root / "execution-gate.sqlite3"
    gate.initialize(gate_path)
    account = next(iter(old["scenes"].values()))["account"]
    gate.install(
        gate_path,
        slot="0",
        scope=scope,
        session="offline-session",
        account=account,
        grants=old["execution_grants"]["QUERY"],
        expires=None,
        authority=authority,
    )
    with sqlite3.connect(gate_path) as db:
        db.execute("INSERT INTO identities VALUES(?,?,?,?)", (scope, "offline-session", "fake-credential", account))
        for entry in old["execution_grants"]["QUERY"]:
            db.execute("UPDATE grants SET used=1 WHERE scope=? AND id=?", (scope, entry["id"]))
            db.execute("UPDATE authority_limits SET used=1 WHERE authority=? AND id=?", (authority, entry["id"]))
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (entry["id"], scope, entry["id"], "offline-session", "succeeded", "{}", "fake-credential", 1),
            )
    store.submit(worker["job_id"], key, "Offline fixture execution gap, not a cloud test")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    store.accept(
        review["job_id"],
        key,
        {
            "task_id": old["task_id"],
            "attempt": 1,
            "cleanup": [],
            "decisions": [
                {"scene": scene, "status": "execution_gap", "reason": "Offline missing capture fixture"}
                for scene in old["scenes"]
            ],
        },
    )
    store.finish_job(review["job_id"], thread, turn, "completed")
    repair = reserve(store, "repair")
    key, thread, turn = bound(store, repair)
    store.block(repair["job_id"], key, "Offline independent additional source approval needed")
    store.finish_job(repair["job_id"], thread, turn, "completed")
    store.retry_blocked(old["task_id"], "Offline sampler repaired")
    prepare = reserve(store, "prepare")
    key, thread, turn = bound(store, prepare)
    original_path = "inputs/R0001/initial-proposal.json"
    json_write(store.root / original_path, old)
    json_write(store.root / "inputs/R0001/remediation.json", {"offline": "sampler corrected"})
    new = copy.deepcopy(old)
    new["preparation_selection_continuation"] = {
        "initial_proposal": original_path,
        "initial_sha256": digest(store.root / original_path),
    }
    new["additional_source_requests"] = [
        {
            "request_id": "offline-query-sampler-repair-001",
            "dimension": "QUERY",
            "prior_scope": scope,
            "predecessor_proposal": original_path,
            "predecessor_sha256": digest(store.root / original_path),
            "remediation_evidence": "inputs/R0001/remediation.json",
            "remediation_sha256": digest(store.root / "inputs/R0001/remediation.json"),
        }
    ]
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "task_id": new["task_id"],
        "carry_forward_dimensions": {},
        "execution_selection_approved": copy.deepcopy(new["execution_selection"]),
        "preparation_selection_approved": copy.deepcopy(new["preparation_selection"]),
        "preparation_selection_continuation_approved": copy.deepcopy(new["preparation_selection_continuation"]),
        "additional_sources_approved": copy.deepcopy(new["additional_source_requests"]),
    }
    return store, runtime, old, new, proof, prepare, key, thread, turn


def authorize_continuation(fixture):
    store, runtime, _, new, proof, *_ = fixture
    json_write(store.root / "inputs/R0001/continued-proposal.json", new)
    proof["proposal_sha256"] = digest(store.root / "inputs/R0001/continued-proposal.json")
    json_write(store.root / "inputs/R0001/continued-review.json", proof)
    runtime.authorize_creation(
        store.root / "inputs/R0001/continued-proposal.json", "inputs/R0001/continued-review.json", 0
    )


def test_reviewed_query_retry_preserves_real_preparation_deferral_and_all_old_wire(continued_preparation):
    store, _, old, new, _, prepare, key, thread, turn = continued_preparation
    gate_path = store.root / "execution-gate.sqlite3"
    before = gate_path.read_bytes()
    authorize_continuation(continued_preparation)
    authorize_continuation(continued_preparation)  # Same operation, not another allowance.
    authority = execution_authority(store.root, new, "QUERY")
    assert authority != execution_authority(store.root, old, "QUERY")
    store.approve_packet(prepare["job_id"], key, new)
    store.finish_job(prepare["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    context = store.context(worker["job_id"], key)
    assert len(context["expected_scenes"]) == 2 and len(context["all_expected_scenes"]) == 4
    selection.validate_authorization(store.root, new)  # New worker running is not new admission.
    assert execution_authority(store.root, new, "QUERY") == authority
    assert gate_path.read_bytes() == before
    values = [observation(store, worker, scene["id"]) for scene in context["expected_scenes"]]
    for value in values:
        store.record_observation(worker["job_id"], key, value)
    store.submit(worker["job_id"], key, "Offline visual fixture only")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    for value in values:
        store.inspected(review["job_id"], key, value["screenshot"])
    store.accept(
        review["job_id"],
        key,
        {
            "task_id": new["task_id"],
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
        },
    )
    store.finish_job(review["job_id"], thread, turn, "completed")
    assert store.status()["tasks"][0]["state"] == "blocked"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM verdicts WHERE scene LIKE '%-TF-%'").fetchone() == (0,)


@pytest.mark.parametrize(
    "damage",
    [
        "marker",
        "initial_sha",
        "original_changed",
        "deferred_reason",
        "no_marker",
        "no_preparation",
        "extra_operation",
        "empty_operation",
        "dimension",
        "calls",
        "quota",
        "cleanup",
        "paid",
        "review_marker",
        "review_initial",
        "review_kind",
        "review_operations",
        "carry",
        "prior_scope",
        "worker_running",
        "identity",
        "wire_unused",
        "wire_unknown",
        "wire_call",
        "wire_authority",
        "wire_limit",
    ],
)
def test_preparation_continuation_rejects_scope_provenance_or_authority_drift(continued_preparation, damage):
    store, _, _, new, proof, *_ = continued_preparation
    request = new["additional_source_requests"][0]
    if damage == "marker":
        new["preparation_selection_continuation"]["extra"] = True
    elif damage == "initial_sha":
        new["preparation_selection_continuation"]["initial_sha256"] = "0" * 64
    elif damage == "original_changed":
        json_write(store.root / request["predecessor_proposal"], {"changed": True})
    elif damage == "deferred_reason":
        next(iter(new["execution_selection"]["deferred_scenes"].values()))["reason"] += " changed"
    elif damage == "no_marker":
        del new["preparation_selection_continuation"]
    elif damage == "no_preparation":
        del new["preparation_selection"]
    elif damage in {"empty_operation", "extra_operation"}:
        new["additional_source_requests"] = [] if damage == "empty_operation" else [request, copy.deepcopy(request)]
    elif damage == "dimension":
        request["dimension"] = "TF"
    elif damage == "calls":
        new["execution_grants"]["QUERY"][1]["call"]["params"]["drift"] = True
    elif damage == "quota":
        new["execution_grants"]["QUERY"][0]["quota"] = 2
    elif damage == "cleanup":
        new["cleanup_requests"] = {"QUERY": "Delete"}
    elif damage == "paid":
        new["budget_cents"] = 1
    elif damage.startswith("review_"):
        field = {
            "review_marker": "preparation_selection_continuation_approved",
            "review_initial": "preparation_selection_approved",
            "review_kind": "kind",
            "review_operations": "additional_sources_approved",
        }[damage]
        proof[field] = "wrong"
    elif damage == "carry":
        proof["carry_forward_dimensions"] = {"QUERY": {}}
    elif damage == "prior_scope":
        request["prior_scope"] = "a" * 32 + ":QUERY"
    elif damage == "worker_running":
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='complete' WHERE state IN ('queued','running')")
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (request["prior_scope"].split(":")[0],))
    else:
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as db:
            if damage == "identity":
                db.execute("UPDATE identities SET account='other'")
            elif damage == "wire_unused":
                db.execute("UPDATE grants SET used=0")
            elif damage == "wire_unknown":
                db.execute("UPDATE attempts SET state='unknown'")
            elif damage == "wire_call":
                db.execute("UPDATE grants SET call='{}'")
            elif damage == "wire_authority":
                db.execute("UPDATE grants SET authority='different'")
            elif damage == "wire_limit":
                db.execute("UPDATE authority_limits SET used=0")
    with pytest.raises(WorkflowError):
        authorize_continuation(continued_preparation)


@pytest.mark.parametrize("targeted", [False, True])
def test_unknown_support_without_reviewed_stage_remains_blocked(initial, targeted):
    store, _, proposal, _, prepare, key, *_ = initial
    del proposal["execution_selection"]
    del proposal["preparation_selection"]
    if not targeted:
        with store.transaction() as connection:
            row = connection.execute("SELECT spec FROM tasks WHERE id=?", (proposal["task_id"],)).fetchone()
            spec = json.loads(row[0])
            del spec["retest_scope"]
            connection.execute("UPDATE tasks SET spec=? WHERE id=?", (dumps(spec), proposal["task_id"]))
    with pytest.raises(WorkflowError, match="resolve four dimensions"):
        store.approve_packet(prepare["job_id"], key, proposal)
    assert store.status()["tasks"][0]["state"] == "preparing"
