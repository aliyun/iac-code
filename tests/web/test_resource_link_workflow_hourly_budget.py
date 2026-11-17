"""Offline hourly allowance registration; no cloud or user configuration."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from scripts.web.e2e.resource_link_workflow.__main__ import parser
from scripts.web.e2e.resource_link_workflow.creation import execution_digest
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, initialize
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.hourly_budget import (
    approved_cost,
    can_reserve,
    holds,
    operation_review,
    policy,
    register,
    release_verified,
    reserve,
)
from scripts.web.e2e.resource_link_workflow.plans import expected_scenes
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.server import reconcile
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from tests.web.test_resource_link_workflow import bound, observation, packet, resource, token
from tests.web.test_resource_link_workflow import reserve as dispatch_action


def make_reviewed(tmp_path, numbers=(33,)):
    store = Store.initialize(tmp_path / "state")
    plan = {"schema_version": 1, "plan_id": "hourly-offline", "resources": [resource(n) for n in numbers]}
    if len(numbers) == 4:
        plan["initial_wave"] = 4
    store.import_plan(plan)
    scenes = sorted(item["id"] for entry in plan["resources"] for item in expected_scenes(entry))
    identifiers = sorted(entry["id"] for entry in plan["resources"])
    human = {
        "schema_version": 1,
        "kind": "human_hourly_fee_authorization",
        "approved": True,
        "plan_id": plan["plan_id"],
        "currency": "CNY",
        "resource_ids": identifiers,
        "scene_ids": scenes,
        "max_concurrent_hourly_cents": 1000,
        "total_budget_cents": None,
        "verbatim_user_message": "OFFLINE FAKE HUMAN AUTHORIZATION ONLY",
    }
    json_write(store.root / "inputs/human.json", human)
    proof = {
        "schema_version": 1,
        "kind": "ops_run_hourly_fee_review",
        "approved": True,
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": hashlib.sha256(dumps(plan).encode()).hexdigest(),
        "currency": "CNY",
        "max_concurrent_hourly_cents": 1000,
        "resource_ids": identifiers,
        "scene_ids": scenes,
        "human_authorization": {"path": "inputs/human.json", "sha256": digest(store.root / "inputs/human.json")},
    }
    json_write(store.root / "inputs/review.json", proof)
    return store, human, proof


@pytest.fixture
def reviewed(tmp_path):
    return make_reviewed(tmp_path)


def test_hourly_registration_is_idempotent_and_never_converts_total_budget(reviewed):
    store, _, _ = reviewed
    tables = ("runs", "tasks", "slots", "jobs", "verdicts", "owned", "budget_reservations", "actions")
    with store.transaction() as connection:
        before = {name: list(map(tuple, connection.execute(f"SELECT * FROM {name}"))) for name in tables}
    result = register(store, "hourly-offline", "inputs/review.json")
    assert result["no_cloud_call"] and not result["already_recorded"]
    assert result["hourly_fee_authorization"]["total_budget_cents"] is None
    assert result["hourly_fee_authorization"]["max_concurrent_hourly_cents"] == 1000
    revision = store.status()["revision"]
    assert register(store, "hourly-offline", "inputs/review.json")["already_recorded"]
    assert store.status()["revision"] == revision
    with store.transaction() as connection:
        assert policy(store.root, connection, "hourly-offline")["operation_review_required"]
        for name in tables:
            assert list(map(tuple, connection.execute(f"SELECT * FROM {name}"))) == before[name]
    assert Store(store.root).status()["active_plans"][0]["budget_cents"] == 0
    assert (
        Store(store.root).status()["active_plans"][0]["hourly_fee_authorization"]["max_concurrent_hourly_cents"] == 1000
    )
    args = parser().parse_args(["register-hourly-fee", "hourly-offline", "--evidence", "inputs/review.json"])
    assert args.command == "register-hourly-fee"


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", False),
        ("kind", "worker_summary"),
        ("plan_id", "other"),
        ("frozen_plan_sha256", "wrong"),
        ("resource_ids", ["R0034"]),
        ("scene_ids", []),
        ("currency", "USD"),
        ("max_concurrent_hourly_cents", True),
        ("max_concurrent_hourly_cents", 0),
        ("max_concurrent_hourly_cents", 1001),
    ],
)
def test_hourly_rejects_unapproved_drifting_or_excessive_review(reviewed, field, value):
    store, _, proof = reviewed
    proof[field] = value
    json_write(store.root / "inputs/review.json", proof)
    with pytest.raises(WorkflowError):
        register(store, "hourly-offline", "inputs/review.json")
    with store.transaction() as connection:
        assert policy(store.root, connection, "hourly-offline") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", False),
        ("kind", "worker_summary"),
        ("verbatim_user_message", ""),
        ("max_concurrent_hourly_cents", True),
        ("max_concurrent_hourly_cents", 999),
        ("total_budget_cents", 1000),
        ("resource_ids", []),
        ("scene_ids", []),
        ("currency", "USD"),
    ],
)
def test_hourly_rejects_incompatible_human_authorization(reviewed, field, value):
    store, human, proof = reviewed
    human[field] = value
    json_write(store.root / "inputs/human.json", human)
    proof["human_authorization"]["sha256"] = digest(store.root / "inputs/human.json")
    json_write(store.root / "inputs/review.json", proof)
    with pytest.raises(WorkflowError, match="human"):
        register(store, "hourly-offline", "inputs/review.json")


def test_hourly_detects_tampering_after_restart(reviewed):
    store, human, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    human["max_concurrent_hourly_cents"] = 2000
    json_write(store.root / "inputs/human.json", human)
    with Store(store.root).transaction() as connection, pytest.raises(WorkflowError, match="SHA changed"):
        policy(store.root, connection, "hourly-offline")


@pytest.mark.parametrize("blocker", ["unpaused", "job", "action", "complete"])
def test_hourly_needs_drained_maintenance_and_incomplete_plan(reviewed, blocker):
    store, _, _ = reviewed
    if blocker == "unpaused":
        store.pause(False)
    else:
        with store.transaction() as connection:
            if blocker == "job":
                connection.execute(
                    "INSERT INTO jobs VALUES('job','hourly-offline:R0033','sol','prepare',"
                    "'unknown','thread','turn','cap','unknown',0)"
                )
            elif blocker == "action":
                connection.execute("INSERT INTO actions VALUES('action',NULL,'prepare','issued',0)")
            else:
                connection.execute("UPDATE tasks SET state='complete'")
    with pytest.raises(WorkflowError):
        register(store, "hourly-offline", "inputs/review.json")


def test_hourly_rejects_outside_input_and_replacement_evidence(reviewed):
    store, _, proof = reviewed
    json_write(store.root / "outside.json", proof)
    with pytest.raises(WorkflowError, match="escapes"):
        register(store, "hourly-offline", "../outside.json")
    register(store, "hourly-offline", "inputs/review.json")
    json_write(store.root / "inputs/copy.json", proof)
    with pytest.raises(WorkflowError, match="replaced"):
        register(store, "hourly-offline", "inputs/copy.json")
    with store.transaction() as connection:
        assert len(list(connection.execute("SELECT * FROM events WHERE kind='run_hourly_fee_registered'"))) == 1
        assert (
            json.loads(
                connection.execute("SELECT value FROM meta WHERE key='hourly_fee:hourly-offline'").fetchone()[0]
            )["max_concurrent_hourly_cents"]
            == 1000
        )


def operation_fixture(store, task, amount=600):
    """Synthetic cost/source data only: this is not a real pricing or cloud test."""
    json_write(store.root / "inputs/cloud.json", {"fixture": "offline, not cloud evidence"})
    value = packet(store, {"task_id": task["id"], "expected_scenes": expected_scenes(json.loads(task["spec"]))})
    prefix = f"inputs/{task['resource_id']}"
    json_write(store.root / f"{prefix}-official.json", {"fixture": "offline official-price stand-in"})
    estimate = {
        "schema_version": 1,
        "kind": "ops_hourly_cost_inventory",
        "approved": True,
        "task_id": task["id"],
        "currency": "CNY",
        "account": "fixture",
        "execution_grants_sha256": hashlib.sha256(canonical(value["execution_grants"]).encode()).hexdigest(),
        "max_hourly_cents": amount,
        "includes_all_dependencies": True,
        "includes_request_and_network_costs": True,
        "components": [
            {
                "id": "sample-and-dependencies",
                "max_hourly_cents": amount,
                "minimum_billing_seconds": 3600,
                "billing_mode": "pay_as_you_go",
                "estimate_basis": "offline bounded fixture",
                "limitations": "not a real price",
                "bounded_usage": True,
                "official_sources": [
                    {"path": f"{prefix}-official.json", "sha256": digest(store.root / f"{prefix}-official.json")}
                ],
            }
        ],
    }
    json_write(store.root / f"{prefix}-cost.json", estimate)
    value["hourly_cost"] = {
        "max_hourly_cents": amount,
        "cost_evidence": {
            "path": f"{prefix}-cost.json",
            "sha256": digest(store.root / f"{prefix}-cost.json"),
        },
    }
    with store.transaction() as connection:
        allowance = policy(store.root, connection, task["run_id"])
    proof = {
        "approved": True,
        "hourly_operation_review": {
            "schema_version": 1,
            "kind": "ops_hourly_operation_review",
            "approved": True,
            "plan_id": task["run_id"],
            "task_id": task["id"],
            "execution_sha256": execution_digest(value),
            "hourly_cost": value["hourly_cost"],
            "hourly_permission": allowance["evidence"],
        },
    }
    return value, estimate, proof


def authorize_fixture(store, task, amount=600):
    value, estimate, proof = operation_fixture(store, task, amount)
    prefix = f"inputs/{task['resource_id']}"
    proposal = f"{prefix}-proposal.json"
    evidence = f"{prefix}-review.json"
    json_write(store.root / proposal, value)
    proof["proposal_sha256"] = digest(store.root / proposal)
    json_write(store.root / evidence, proof)
    Runtime(store).authorize_creation(store.root / proposal, evidence, 0)
    return value, estimate, proof


def test_positive_hourly_cost_has_distinct_digest_and_requires_exact_ops_approval(reviewed):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
    value, _, _ = operation_fixture(store, task)
    without = {k: v for k, v in value.items() if k != "hourly_cost"}
    assert execution_digest(value) != execution_digest(without)
    with store.transaction() as connection, pytest.raises(WorkflowError, match="not independently authorized"):
        approved_cost(store.root, connection, task, value)
    value, _, _ = authorize_fixture(store, task)
    with store.transaction() as connection:
        approved = approved_cost(store.root, connection, task, value)
        assert approved["max_hourly_cents"] == 600
        assert connection.execute("SELECT budget FROM runs").fetchone()[0] == 0
        assert not holds(connection)  # Authorization is not dispatch or a cost hold.
    changed = {**value, "requests": {"QUERY": "different full proposal"}}
    with store.transaction() as connection, pytest.raises(WorkflowError, match="complete independently reviewed"):
        approved_cost(store.root, connection, task, changed)


@pytest.mark.parametrize(
    "damage",
    [
        "no_policy",
        "wrong_task",
        "wrong_account",
        "calls",
        "dependencies",
        "network",
        "sum",
        "negative",
        "bool_price",
        "minimum",
        "subscription",
        "unbounded",
        "no_sources",
        "source_changed",
        "duplicate",
        "free_paid",
        "review_changed",
        "too_expensive",
    ],
)
def test_exact_hourly_operation_rejects_incomplete_or_drifting_costs(reviewed, damage):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
    value, estimate, proof = operation_fixture(store, task)
    component = estimate["components"][0]
    if damage == "no_policy":
        with store.transaction() as connection:
            connection.execute("DELETE FROM meta WHERE key LIKE 'hourly_fee:%'")
    elif damage == "wrong_task":
        value["task_id"] = "other"
    elif damage == "wrong_account":
        estimate["account"] = "other"
    elif damage == "calls":
        estimate["execution_grants_sha256"] = "wrong"
    elif damage in {"dependencies", "network"}:
        estimate["includes_all_dependencies" if damage == "dependencies" else "includes_request_and_network_costs"] = (
            False
        )
    elif damage == "sum":
        component["max_hourly_cents"] -= 1
    elif damage in {"negative", "bool_price"}:
        component["max_hourly_cents"] = -1 if damage == "negative" else True
    elif damage == "minimum":
        component["minimum_billing_seconds"] = -1
    elif damage in {"subscription", "free_paid"}:
        component["billing_mode"] = "subscription" if damage == "subscription" else "free"
    elif damage == "unbounded":
        component["bounded_usage"] = False
    elif damage == "no_sources":
        component["official_sources"] = []
    elif damage == "source_changed":
        json_write(store.root / component["official_sources"][0]["path"], {"tampered": True})
    elif damage == "duplicate":
        estimate["components"].append(dict(component))
    elif damage == "review_changed":
        proof["hourly_operation_review"]["execution_sha256"] = "wrong"
    else:
        value["hourly_cost"]["max_hourly_cents"] = 1001
    path = value["hourly_cost"]["cost_evidence"]["path"]
    json_write(store.root / path, estimate)
    value["hourly_cost"]["cost_evidence"]["sha256"] = digest(store.root / path)
    # Update the exact review except the deliberately invalid execution binding.
    if damage != "review_changed":
        proof["hourly_operation_review"]["execution_sha256"] = execution_digest(value)
    with store.transaction() as connection, pytest.raises(WorkflowError):
        operation_review(store.root, connection, task, value, proof)


def test_hourly_review_full_proposal_sha_and_original_price_are_immutable(reviewed):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
    value, _, proof = authorize_fixture(store, task)
    proposal = f"inputs/{task['resource_id']}-proposal.json"
    evidence = f"inputs/{task['resource_id']}-review.json"
    proof["proposal_sha256"] = "wrong"
    json_write(store.root / evidence, proof)
    with pytest.raises(WorkflowError, match="final full proposal SHA"):
        Runtime(store).authorize_creation(store.root / proposal, evidence, 0)
    with store.transaction() as connection, pytest.raises(WorkflowError, match="SHA changed"):
        approved_cost(store.root, connection, task, value)


def test_concurrent_hourly_reservations_serialize_and_survive_restart(tmp_path):
    store, _, _ = make_reviewed(tmp_path, (33, 34))
    register(store, "hourly-offline", "inputs/review.json")
    with store.transaction() as connection:
        tasks = list(connection.execute("SELECT * FROM tasks ORDER BY id"))
    costs = []
    for task in tasks:
        value, _, _ = authorize_fixture(store, task)
        with store.transaction() as connection:
            costs.append(approved_cost(store.root, connection, task, value))

    def attempt(index):
        try:
            with Store(store.root).transaction() as connection:
                reserve(store, connection, costs[index], f"job-{index}")
            return True
        except WorkflowError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(results) == 1
    with Store(store.root).transaction() as connection:
        active = holds(connection)
        assert len(active) == 1 and active[0]["max_hourly_cents"] == 600
        assert "expires_at" not in active[0]
        assert connection.execute("SELECT budget FROM runs").fetchone()[0] == 0
        assert not connection.execute("SELECT 1 FROM budget_reservations").fetchone()
        # A failed transaction adds no event and does not spend/restore cloud calls.
        assert connection.execute("SELECT count(*) FROM events WHERE kind='hourly_cost_reserved'").fetchone()[0] == 1


def test_hold_cannot_be_inherited_or_released_by_elapsed_time_or_other_job(reviewed):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
    value, _, _ = authorize_fixture(store, task)
    with store.transaction() as connection:
        cost = approved_cost(store.root, connection, task, value)
        reserve(store, connection, cost, "original-job")
        reserve(store, connection, cost, "original-job")  # Same transaction replay is idempotent.
        assert not can_reserve(store.root, connection, task, value)
    for new_job in ("other-job", "new-attempt"):
        with store.transaction() as connection, pytest.raises(WorkflowError, match="may still be alive"):
            reserve(store, connection, cost, new_job)
    for basis in (
        {"kind": "expired", "evidence": "clock"},
        {"kind": "proved_no_execution", "evidence": "other-turn", "job_id": "other-job"},
    ):
        with store.transaction() as connection, pytest.raises(WorkflowError):
            release_verified(store, connection, task["id"], basis)
    with store.transaction() as connection:
        assert len(holds(connection)) == 1
        connection.execute("UPDATE tasks SET state='complete'")
        assert not store.finalizable(connection)
        release_verified(
            store,
            connection,
            task["id"],
            {
                "kind": "proved_no_execution",
                "evidence": "offline verified no-turn fixture",
                "job_id": "original-job",
            },
        )
        assert holds(connection) == []
        saved = json.loads(connection.execute("SELECT value FROM meta WHERE key LIKE 'hourly_hold:%'").fetchone()[0])
        assert saved["state"] == "released" and saved["job_id"] == "original-job"
        assert store.finalizable(connection)


def test_outstanding_owner_prevents_cost_release_even_with_cleanup_claim(reviewed):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
    value, _, _ = authorize_fixture(store, task)
    with store.transaction() as connection:
        reserve(store, connection, approved_cost(store.root, connection, task, value), "job")
        connection.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)", ("owner", task["id"], "CREATE", "{}", "present", "offline")
        )
    with store.transaction() as connection, pytest.raises(WorkflowError, match="owned resources remain"):
        release_verified(
            store,
            connection,
            task["id"],
            {
                "kind": "sol_verified_cleanup",
                "evidence": "claim without actual cleanup",
            },
        )
    with Store(store.root).transaction() as connection:
        assert len(holds(connection)) == 1


@pytest.mark.parametrize("wire_touched", [False, True])
def test_precise_no_turn_reconcile_releases_hourly_cost_only_when_execution_proved_absent(
    reviewed,
    monkeypatch,
    wire_touched,
):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    store.pause(False)
    preparation = dispatch_action(store, "prepare")
    key, thread, turn = bound(store, preparation)
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
    value, _, _ = authorize_fixture(store, task)
    store.approve_packet(preparation["job_id"], key, value)
    store.finish_job(preparation["job_id"], thread, turn, "completed")
    worker = dispatch_action(store, "dispatch")
    worker_key = token(store, worker)
    store.mark_submission(worker["job_id"], worker_key)
    store.bind(worker["job_id"], worker_key, "fixture-worker", None)
    store.park(worker["job_id"], "policy rejected before turn/start")
    folder = store.root / "jobs" / worker["job_id"]
    records = [
        {"method": "transport/thread_created", "params": {"threadId": "fixture-worker"}},
        {
            "method": "transport/adapter_error",
            "params": {"message": "backend did not confirm read-only/no-network/never policy"},
        },
    ]
    (folder / "raw-rollout.jsonl").write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
    json_write(folder / "launch.json", {"pid": 123, "identity": "offline-dead-process"})
    gate = store.root / "execution-gate.sqlite3"
    initialize(gate)
    if wire_touched:
        with sqlite3.connect(gate) as connection:
            connection.execute("INSERT INTO active(slot,scope) VALUES(?,?)", ("0", worker["job_id"] + ":QUERY"))
    monkeypatch.setattr(Runtime, "assert_process", lambda *_: (_ for _ in ()).throw(WorkflowError("dead")))
    raw = {
        "method": "thread/read",
        "read_at": datetime.now(timezone.utc).isoformat(),
        "thread": {
            "id": "fixture-worker",
            "turns": [{"id": "prior-turn", "status": "completed"}],
        },
    }
    result = reconcile(store, worker["job_id"], raw)
    assert result["retry_safe"] is not wire_touched
    with Store(store.root).transaction() as connection:
        assert len(holds(connection)) == (1 if wire_touched else 0)
        record = json.loads(connection.execute("SELECT value FROM meta WHERE key LIKE 'hourly_hold:%'").fetchone()[0])
        if not wire_touched:
            assert record["release_basis"]["kind"] == "proved_no_execution"
            assert record["release_basis"]["job_id"] == worker["job_id"]
            assert (
                record["release_basis"]["evidence"]["raw_sha256"]
                == hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            )


def test_later_dispatch_failure_rolls_back_cost_hold_slot_and_budget_together(reviewed, monkeypatch):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    store.pause(False)
    job = dispatch_action(store, "prepare")
    key, thread, turn = bound(store, job)
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
    value, _, _ = authorize_fixture(store, task)
    store.approve_packet(job["job_id"], key, value)
    store.finish_job(job["job_id"], thread, turn, "completed")
    action = store.next_action()["actions"][0]
    original_event = store.event

    def failed_event(connection, kind, data):
        if kind == "job_reserved":
            raise WorkflowError("offline failure after hourly reservation")
        return original_event(connection, kind, data)

    monkeypatch.setattr(store, "event", failed_event)
    with pytest.raises(WorkflowError, match="after hourly reservation"):
        store.execute(action["id"])
    with Store(store.root).transaction() as connection:
        assert holds(connection) == []
        assert not connection.execute("SELECT 1 FROM slots WHERE task_id IS NOT NULL").fetchone()
        assert not connection.execute("SELECT 1 FROM budget_reservations").fetchone()
        assert not connection.execute("SELECT 1 FROM jobs WHERE role='glm'").fetchone()
        assert connection.execute("SELECT state FROM actions WHERE id=?", (action["id"],)).fetchone()[0] == "issued"


def test_parallel_other_run_cannot_evade_the_stricter_existing_hourly_limit(reviewed):
    store, _, _ = reviewed
    register(store, "hourly-offline", "inputs/review.json")
    other = {"schema_version": 1, "plan_id": "other-hourly-run", "resources": [resource(34)]}
    store.import_plan(other)
    scene_ids = sorted(item["id"] for item in expected_scenes(other["resources"][0]))
    human = {
        "schema_version": 1,
        "kind": "human_hourly_fee_authorization",
        "approved": True,
        "plan_id": other["plan_id"],
        "currency": "CNY",
        "resource_ids": ["R0034"],
        "scene_ids": scene_ids,
        "max_concurrent_hourly_cents": 2000,
        "total_budget_cents": None,
        "verbatim_user_message": "OFFLINE FAKE AUTHORIZATION ONLY",
    }
    json_write(store.root / "inputs/other-human.json", human)
    proof = {
        "schema_version": 1,
        "kind": "ops_run_hourly_fee_review",
        "approved": True,
        "plan_id": other["plan_id"],
        "frozen_plan_sha256": hashlib.sha256(dumps(other).encode()).hexdigest(),
        "currency": "CNY",
        "resource_ids": ["R0034"],
        "scene_ids": scene_ids,
        "max_concurrent_hourly_cents": 2000,
        "human_authorization": {
            "path": "inputs/other-human.json",
            "sha256": digest(store.root / "inputs/other-human.json"),
        },
    }
    json_write(store.root / "inputs/other-review.json", proof)
    register(store, other["plan_id"], "inputs/other-review.json")
    with store.transaction() as connection:
        tasks = list(connection.execute("SELECT * FROM tasks ORDER BY resource_id"))
    for task in tasks:
        value, _, _ = authorize_fixture(store, task, 600)
        with store.transaction() as connection:
            cost = approved_cost(store.root, connection, task, value)
        if task["resource_id"] == "R0033":
            with store.transaction() as connection:
                reserve(store, connection, cost, "first-run-job")
        else:
            with store.transaction() as connection, pytest.raises(WorkflowError, match="currently reserved"):
                reserve(store, connection, cost, "other-run-job")
    with store.transaction() as connection:
        assert sum(item["max_hourly_cents"] for item in holds(connection)) == 600


def test_real_dispatch_transaction_selects_affordable_resource_then_releases_on_accept(tmp_path):
    store, _, _ = make_reviewed(tmp_path, (33, 34, 35, 36))
    register(store, "hourly-offline", "inputs/review.json")
    for number in range(4):
        store.set_target(number, f"offline-target-{number}")
    store.pause(False)
    for amount in (600, 600, 300, 100):
        job = dispatch_action(store, "prepare")
        key, thread, turn = bound(store, job)
        task_id = store.context(job["job_id"], key)["task_id"]
        with store.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        value, _, _ = authorize_fixture(store, task, amount)
        store.approve_packet(job["job_id"], key, value)
        store.finish_job(job["job_id"], thread, turn, "completed")
    first = dispatch_action(store, "dispatch")
    key, thread, turn = bound(store, first)
    first_context = store.context(first["job_id"], key)
    task_id = first_context["task_id"]
    assert task_id.endswith("R0033")
    scenes = first_context["expected_scenes"]
    assert sum(x["max_hourly_cents"] for x in first_context["hourly_cost_holds"]) == 600
    # Ready R0034 costs 600, so it must not starve affordable R0035/R0036.
    for expected in ("R0035", "R0036"):
        worker = dispatch_action(store, "dispatch")
        worker_key, _, _ = bound(store, worker)
        assert store.context(worker["job_id"], worker_key)["task_id"].endswith(expected)
    status = Store(store.root).status()
    assert sum(x["max_hourly_cents"] for x in status["active_plans"][0]["hourly_cost_holds"]) == 1000
    assert any(x["category"] == "hourly_cost_capacity" for x in status["progress"]["waiting_on"])
    assert store.next_action()["actions"] == []
    values = [observation(store, first, scene["id"]) for scene in scenes]
    for item in values:
        store.record_observation(first["job_id"], key, item)
    store.submit(first["job_id"], key, "offline fixture only")
    store.finish_job(first["job_id"], thread, turn, "completed")
    review_job = dispatch_action(store, "review")
    review_key, review_thread, review_turn = bound(store, review_job)
    for item in values:
        store.inspected(review_job["job_id"], review_key, item["screenshot"])
    context = store.context(review_job["job_id"], review_key)
    receipt = {
        "task_id": task_id,
        "attempt": context["attempt"],
        "cleanup": [],
        "decisions": [
            {
                "scene": scene["id"],
                "status": "pass",
                "reason": "offline synthetic PNG, not real acceptance",
                "identity_verified": True,
                "login_verified": True,
                "render_verified": True,
            }
            for scene in scenes
        ],
    }
    store.accept(review_job["job_id"], review_key, receipt)
    store.finish_job(review_job["job_id"], review_thread, review_turn, "completed")
    next_worker = dispatch_action(store, "dispatch")
    next_key, _, _ = bound(store, next_worker)
    assert store.context(next_worker["job_id"], next_key)["task_id"].endswith("R0034")
    with store.transaction() as connection:
        assert sum(x["max_hourly_cents"] for x in holds(connection)) == 1000
        assert connection.execute("SELECT sum(amount) FROM budget_reservations").fetchone()[0] == 0
        record = json.loads(
            connection.execute("SELECT value FROM meta WHERE key LIKE ?", (f"hourly_hold:{task_id}:%",)).fetchone()[0]
        )
        assert record["state"] == "released" and record["release_basis"]["kind"] == "sol_verified_cleanup"
        assert digest(store.root / record["release_basis"]["evidence"]) == record["release_basis"]["sha256"]
