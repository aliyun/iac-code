"""Offline fault injection, NOT evidence of actual model concurrency or cloud PASS.

Use real SQLite/action/capability/receipt transitions and tiny synthetic PNGs;
no network, model, credentials, browser, or production state is involved.
"""

import json
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow import reports
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.server import Service
from scripts.web.e2e.resource_link_workflow.store import Store
from tests.web.test_resource_link_workflow import (
    bound,
    observation,
    packet,
    plan,
    prepare,
    reserve,
    resource,
    review_fixture,
)


@pytest.fixture
def store(tmp_path):
    value = Store.initialize(tmp_path / "state")
    json_write(value.root / "inputs/cloud.json", {"fixture": "synthetic, not a cloud response"})
    return value


def capture(store, worker, binding):
    key = binding[0]
    for scene in store.context(worker["job_id"], key)["expected_scenes"]:
        store.record_observation(worker["job_id"], key, observation(store, worker, scene["id"]))


def terminal_worker(store, worker, binding):
    key, thread, turn = binding
    store.submit(worker["job_id"], key, "Synthetic offline execution, not a real PASS")
    store.finish_job(worker["job_id"], thread, turn, "completed")


def accepted_review(store):
    job = reserve(store, "review")
    key, thread, turn = bound(store, job)
    context = store.context(job["job_id"], key)
    for value in context["observations"]:
        store.inspected(job["job_id"], key, value["screenshot"])
    store.accept(
        job["job_id"],
        key,
        {
            "task_id": context["task_id"],
            "attempt": context["attempt"],
            "cleanup": [],
            "decisions": [
                {
                    "scene": scene["id"],
                    "status": "pass",
                    "reason": "Synthetic offline pixels only",
                    "identity_verified": True,
                    "login_verified": True,
                    "render_verified": True,
                }
                for scene in context["expected_scenes"]
            ],
        },
    )
    return job, thread, turn


def test_first_wave_prepares_the_sixth_resource_when_the_ready_prefix_conflicts(store):
    locks = ["shared", "shared", "shared", "y", "z", "w"]
    store.import_plan({
        "schema_version": 1, "plan_id": "conflicting-prefix", "initial_wave": 4,
        "resources": [resource(index + 1, locks=[lock]) for index, lock in enumerate(locks)],
    })
    store.pause(False)
    for _ in range(5):
        prepare(store)
    assert store.status()["running"] == 0
    actions = store.next_action()["actions"]
    assert len(actions) == 1, "the sixth resource must be prepared instead of waiting forever"
    action = actions[0]
    assert (action["task_id"], action["kind"]) == ("conflicting-prefix:R0006", "prepare")
    prepare(store)
    workers = [reserve(store, "dispatch") for _ in range(4)]
    for worker in workers:
        bound(store, worker)
    worker_ids = {worker["job_id"] for worker in workers}
    assert {job["task_id"] for job in store.status()["jobs"] if job["id"] in worker_ids} == {
        "conflicting-prefix:R0001", "conflicting-prefix:R0004", "conflicting-prefix:R0005", "conflicting-prefix:R0006"
    }
    assert store.status()["running"] == 4


@pytest.mark.parametrize("current_parent_complete", [False, True])
def test_dependencies_use_the_current_run_instead_of_a_completed_historical_run(store, current_parent_complete):
    def finish_empty_preparation():
        job = reserve(store, "prepare")
        key, thread, turn = bound(store, job)
        json_write(store.root / "inputs/coverage.json", {
            "resources_found": 0, "coverage": ["bounded offline empty fixture"], "failure_blind_spots": [],
        })
        store.classify_preparation(job["job_id"], key, "inputs/coverage.json")
        store.finish_job(job["job_id"], thread, turn, "completed")

    store.import_plan({"schema_version": 1, "plan_id": "old", "resources": [resource(1)]})
    store.pause(False)
    finish_empty_preparation()
    dependent = resource(2)
    dependent["dependencies"] = ["R0001"]
    store.import_plan({"schema_version": 1, "plan_id": "new", "resources": [resource(1), dependent]})
    if current_parent_complete:
        finish_empty_preparation()
        action = store.next_action()["actions"][0]
        assert (action["task_id"], action["kind"]) == ("new:R0002", "prepare")
    else:
        prepare(store)
        worker = reserve(store, "dispatch")
        bound(store, worker)
        assert store.next_action().get("type") == "wait", "a historical completion must not open the new dependency"
        assert [(task["id"], task["state"]) for task in store.status()["tasks"] if task["run_id"] == "new"] == [
            ("new:R0001", "running"), ("new:R0002", "pending")
        ]


def test_six_resources_four_slots_roll_after_review_not_worker_completion_and_survive_restart(store):
    store.import_plan(
        {
            "schema_version": 1,
            "plan_id": "offline-six",
            "initial_wave": 4,
            "resources": [resource(i) for i in range(1, 7)],
        }
    )
    for index in range(4):
        store.set_target(index, f"synthetic-target-{index}")
    store.pause(False)
    for _ in range(4):
        prepare(store)
    workers = []
    for _ in range(4):
        job = reserve(store, "dispatch")
        binding = bound(store, job)
        capture(store, job, binding)
        workers.append((job, binding))
    assert store.status()["running"] == 4
    slots = store.status()["slots"]
    assert len({slot["origin"] for slot in slots}) == len({slot["cwd"] for slot in slots}) == 4
    assert len({slot["target"] for slot in slots}) == 4
    for _ in range(2):
        prepare(store)  # Both replacements are ready while the initial wave runs.
    store = Store(store.root)  # Same durable authority, not a new scheduler.
    assert store.next_action()["type"] == "wait"
    for original in workers[:2]:
        terminal_worker(store, *original)
        assert store.status()["progress"]["free_slots"] == 0  # Terminal alone does not release a slot.
        review, thread, turn = accepted_review(store)
        assert store.status()["progress"]["free_slots"] == 1
        replacement = reserve(store, "dispatch")  # No need to wait for Sol's staged turn to end.
        binding = bound(store, replacement)
        capture(store, replacement, binding)
        workers.append((replacement, binding))
        assert store.status()["running"] == 4
        store.finish_job(review["job_id"], thread, turn, "completed")
    for original in workers[2:]:
        terminal_worker(store, *original)
        review, thread, turn = accepted_review(store)
        store.finish_job(review["job_id"], thread, turn, "completed")
    assert store.next_action()["type"] == "finished"
    assert store.status()["can_finalize"]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE role='glm'").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(DISTINCT thread_id) FROM jobs WHERE role='sol'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM verdicts WHERE status='pass'").fetchone()[0] == 12
        assert not db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()


@pytest.mark.parametrize("verdict,scope", [("execution_gap", "input"), ("product_gap", "product"), ("fail", "product")])
def test_repair_scope_is_from_validated_receipt_not_reason_or_prompt(store, verdict, scope):
    plan(store)
    job, key, thread, turn, receipt, _ = review_fixture(store, (verdict, verdict))
    receipt["decisions"][0]["reason"] = "arbitrary text must not change the repair scope"
    store.accept(job["job_id"], key, receipt)
    store.finish_job(job["job_id"], thread, turn, "completed")
    repair = reserve(store, "repair")
    repair_key, _, _ = bound(store, repair)
    safety = store.context(repair["job_id"], repair_key)["repair_safety"]
    assert safety["repair_scope"] == scope
    assert safety["product_write_allowed"] == (scope == "product")


def test_input_repair_runs_alongside_worker_and_cannot_write_or_escalate_in_turn(store, tmp_path):
    plan(store, 3)
    prepare(store)
    first = reserve(store, "dispatch")
    first_binding = bound(store, first)
    prepare(store)
    sibling = reserve(store, "dispatch")
    sibling_binding = bound(store, sibling)
    terminal_worker(store, first, first_binding)
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    context = store.context(review["job_id"], key)
    store.accept(
        review["job_id"],
        key,
        {
            "task_id": context["task_id"],
            "attempt": 1,
            "cleanup": [],
            "decisions": [
                {"scene": scene["id"], "status": "execution_gap", "reason": "synthetic missed click"}
                for scene in context["expected_scenes"]
            ],
        },
    )
    store.finish_job(review["job_id"], thread, turn, "completed")
    repair = reserve(store, "repair")
    repair_key, repair_thread, repair_turn = bound(store, repair)
    context = store.context(repair["job_id"], repair_key)
    assert context["repair_safety"]["repair_scope"] == "input"
    assert not context["repair_safety"]["product_write_allowed"]
    runtime = Runtime(store)
    runtime.config["repo"] = str(tmp_path)
    product = tmp_path / "src/example.py"
    product.parent.mkdir()
    product.write_text("original = True\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="drained exact GLM"):
        runtime.sol_tool(
            context,
            repair_key,
            "repair_file",
            {
                "path": "src/example.py",
                "before_sha256": digest(product),
                "text": "wrong = True\n",
            },
        )
    assert product.read_text(encoding="utf-8") == "original = True\n"
    runtime.sol_tool(context, repair_key, "escalate_repair", {"reason": "synthetic source diagnosis requires code"})
    with pytest.raises(WorkflowError, match="no longer active"):
        store.approve_packet(repair["job_id"], repair_key, packet(store, context))
    store.finish_job(repair["job_id"], repair_thread, repair_turn, "completed")
    ahead = reserve(store, "prepare")  # Read-only preparation is useful while sibling drains.
    ahead_key, ahead_thread, ahead_turn = bound(store, ahead)
    store.approve_packet(ahead["job_id"], ahead_key, packet(store, store.context(ahead["job_id"], ahead_key)))
    store.finish_job(ahead["job_id"], ahead_thread, ahead_turn, "completed")
    waiting = store.next_action()
    assert waiting["type"] == "wait"
    assert waiting["progress"]["waiting_on"][0]["draining_jobs"] == [sibling["job_id"]]
    terminal_worker(store, sibling, sibling_binding)
    sibling_review, sibling_thread, sibling_turn = accepted_gap_review(store)
    store.finish_job(sibling_review["job_id"], sibling_thread, sibling_turn, "completed")
    product_repair = reserve(store, "repair")
    product_key, _, _ = bound(store, product_repair)
    fresh = store.context(product_repair["job_id"], product_key)
    assert fresh["repair_safety"]["product_write_allowed"]
    assert fresh["repair_trigger"]["kind"] == "repair_escalated"


def accepted_gap_review(store):
    job = reserve(store, "review")
    key, thread, turn = bound(store, job)
    context = store.context(job["job_id"], key)
    store.accept(
        job["job_id"],
        key,
        {
            "task_id": context["task_id"],
            "attempt": context["attempt"],
            "cleanup": [],
            "decisions": [
                {"scene": scene["id"], "status": "execution_gap", "reason": "synthetic no click"}
                for scene in context["expected_scenes"]
            ],
        },
    )
    return job, thread, turn


def test_blocked_handoff_is_durable_idempotent_and_does_not_stop_other_resources(store):
    plan(store, 2)
    job = reserve(store, "prepare")
    key, thread, turn = bound(store, job)
    proposal = "inputs/R0001/proposal.json"
    json_write(store.root / proposal, {"fixture": "not an approved execution"})
    handoff = {
        "category": "creation_authorization",
        "artifacts": [{"path": proposal, "sha256": digest(store.root / proposal)}],
    }
    store.block(job["job_id"], key, "exact proposal awaits OPS", handoff)
    with pytest.raises(WorkflowError, match="exact original model turns terminal"):
        store.retry_blocked("offline:R0001", "too early")
    store.finish_job(job["job_id"], thread, turn, "completed")
    before = store.status()["progress"]["attention"]
    assert before[0]["handoff"]["artifacts"] == handoff["artifacts"]
    assert before[0]["model_terminal"]
    with store.transaction() as db:
        store.event(db, "synthetic_unrelated_screenshot", {})
    assert Store(store.root).status()["progress"]["attention"] == before
    other = reserve(store, "prepare")
    assert store.context(other["job_id"], bound(store, other)[0])["resource"]["id"] == "R0002"


@pytest.mark.parametrize("bad", ["sha", "outside", "category"])
def test_handoff_rejects_drift_and_preserves_original_lease(store, bad):
    plan(store)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    value = {
        "category": "input",
        "artifacts": [{"path": "inputs/cloud.json", "sha256": digest(store.root / "inputs/cloud.json")}],
    }
    if bad == "sha":
        value["artifacts"][0]["sha256"] = "wrong"
    elif bad == "outside":
        value["artifacts"][0]["path"] = "../outside"
    else:
        value["category"] = "auto_approve"
    before = store.status()
    with pytest.raises(WorkflowError):
        store.block(job["job_id"], key, "fixture", value)
    assert store.status() == before


def test_unknown_job_never_repeated_or_expired_and_is_exact_ops_handoff(store):
    plan(store)
    job = reserve(store, "prepare")
    key, thread, turn = bound(store, job)
    store.park(job["job_id"], "synthetic transport disconnected")
    before = store.status()
    for _ in range(3):
        result = Store(store.root).next_action()
        assert result["type"] == "wait" and result["actions"] == []
        attention = result["progress"]["attention"][0]
        assert attention["category"] == "unknown_submission"
        assert (attention["job"]["thread_id"], attention["job"]["turn_id"]) == (thread, turn)
        assert result["progress"]["worker_deadline"] is None
        assert result["progress"]["automatic_retry"] is False
    assert store.status() == before
    assert key not in json.dumps(result)


def test_first_wave_deadlock_is_actionable_ops_not_idle_wait_or_lowered_gate(store):
    store.import_plan(
        {"schema_version": 1, "plan_id": "wave", "initial_wave": 4, "resources": [resource(i) for i in range(1, 5)]}
    )
    store.pause(False)
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='ready' WHERE resource_id!='R0004'")
        db.execute("UPDATE tasks SET state='blocked',reason='requires new permission' WHERE resource_id='R0004'")
    result = store.next_action()
    assert result["type"] == "wait" and result["progress"]["requires_ops"]
    assert not result["active_plans"][0]["initial_wave_eligible"]
    assert result["progress"]["attention"][0]["identity"] == "wave:R0004"
    assert not store.status()["jobs"]


@pytest.mark.parametrize("leftover", ["owner", "slot"])
def test_finished_and_can_finalize_both_require_no_retained_resources_or_slots(store, leftover):
    plan(store)
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='complete'")
        if leftover == "owner":
            db.execute("INSERT INTO owned VALUES('owner','offline:R0001','CREATE','{}','present','{}')")
        else:
            db.execute("UPDATE slots SET task_id='offline:R0001' WHERE id=0")
    assert not store.status()["can_finalize"]
    assert store.next_action()["type"] == "wait"
    assert store.status()["progress"]["attention"][0]["category"] == "finalization_incomplete"


def test_loaded_renderer_is_attested_to_prevent_silent_old_html_after_terminal(store, monkeypatch):
    assert Runtime(store).attest_control()["results_renderer"]["loaded_sha256"] == digest(Path(reports.__file__))
    monkeypatch.setattr(reports, "LOADED_SHA", "stale-renderer")
    with pytest.raises(WorkflowError, match="results renderer code changed"):
        Runtime(store).attest_control()


@pytest.mark.parametrize(
    "error", [WorkflowError("synthetic slot Web remains locked"), OSError("private-url-must-not-leak")]
)
def test_failed_preflight_is_durable_slot_handoff_not_repeated_failed_execute_or_new_job(store, monkeypatch, error):
    plan(store, 2)
    prepare(store)
    service = Service(store)
    launches = []
    monkeypatch.setattr(service, "launch", lambda job_id: launches.append(job_id) or {"launched": True})

    def preflight(slot):
        if slot["id"] == 0:
            raise error

    monkeypatch.setattr(service.runtime, "preflight", preflight)
    action = store.next_action()["actions"][0]
    result = service.request({"operation": "execute", "arguments": {"action_id": action["id"]}})
    assert result["dispatch_deferred"] and not result["job_reserved"]
    assert not launches and not store.status()["jobs"]
    fresh = Store(store.root)
    assert fresh.status()["progress"]["available_slots"] == [1, 2, 3]
    attention = fresh.status()["progress"]["attention"]
    assert attention[0]["category"] == "slot_preflight"
    assert attention[0]["slot"]["action_id"] == action["id"]
    assert service.request({"operation": "execute", "arguments": {"action_id": action["id"]}}) == {
        "stale_action_skipped": True,
    }
    next_action = fresh.next_action()["actions"][0]
    assert next_action["id"] != action["id"] and next_action["task_id"] == action["task_id"]
    dispatched = service.request({"operation": "execute", "arguments": {"action_id": next_action["id"]}})
    assert dispatched["job_id"] in launches
    assert fresh.status()["slots"][1]["task_id"] == action["task_id"]
    assert fresh.status()["progress"]["attention"] == attention
    with pytest.raises(type(error)):
        service.request({"operation": "slot-recover", "arguments": {"slot": 0}})
    assert fresh.status()["progress"]["attention"] == attention
    monkeypatch.setattr(service.runtime, "preflight", lambda _slot: None)
    assert service.request({"operation": "slot-recover", "arguments": {"slot": 0}})["slot_recovered"] == 0
    assert not fresh.status()["progress"]["attention"]
    assert len(launches) == 1
    assert "private-url-must-not-leak" not in json.dumps(result)
    assert "private-url-must-not-leak" not in json.dumps(attention)


def test_quarantined_slot_does_not_lower_initial_four_wave_requirement(store):
    store.import_plan(
        {"schema_version": 1, "plan_id": "wave", "initial_wave": 4, "resources": [resource(i) for i in range(1, 5)]}
    )
    store.pause(False)
    for _ in range(4):
        prepare(store)
    action = store.next_action()["actions"][0]
    store.defer_dispatch(action["id"], 0, "synthetic stopped Web")
    assert Store(store.root).next_action()["type"] == "wait"
    assert not store.status()["active_plans"][0]["initial_wave_eligible"]
    assert not store.status()["jobs"]


def test_input_repair_does_not_block_a_ready_sibling_dispatch(store):
    plan(store, 2)
    prepare(store)
    first = reserve(store, "dispatch")
    first_binding = bound(store, first)
    prepare(store)
    terminal_worker(store, first, first_binding)
    review, thread, turn = accepted_gap_review(store)
    store.finish_job(review["job_id"], thread, turn, "completed")
    repair = reserve(store, "repair")
    key, _, _ = bound(store, repair)
    assert store.context(repair["job_id"], key)["repair_safety"]["repair_scope"] == "input"
    sibling = reserve(store, "dispatch")
    assert sibling["role"] == "glm"
    assert len(store.status()["jobs"]) == 2


def test_preflight_checks_loaded_product_before_browser_or_model_operations(store, tmp_path, monkeypatch):
    runtime = Runtime(store)
    runtime.config["repo"] = str(tmp_path)
    json_write(tmp_path / "evidence/resource-link-workflow-owner.json", {"state": str(store.root)})
    slot = {"id": 0, "web_pid": 123, "web_identity": "synthetic"}
    json_write(store.root / "slots/0/execution-gate-process.json", {"pid": 123})
    monkeypatch.setattr(runtime, "assert_process", lambda *_: None)
    monkeypatch.setattr(runtime, "browser", lambda *_: pytest.fail("stale loaded product must not touch browser"))

    def stale(_):
        raise WorkflowError("loaded module changed; restart same Web before execution")

    monkeypatch.setattr(runtime, "attest_web", stale)
    with pytest.raises(WorkflowError, match="loaded module changed"):
        runtime.preflight(slot)
    assert not store.status()["jobs"]


def test_terminal_bookkeeping_is_retained_but_stale_renderer_cannot_overwrite_new_page(store, tmp_path, monkeypatch):
    plan(store)
    job = reserve(store, "prepare")
    key, thread, turn = bound(store, job)
    service = Service(store)
    service.runtime.config["repo"] = str(tmp_path)
    monkeypatch.setattr(reports, "LOADED_SHA", "cached-old-renderer")
    monkeypatch.setattr(reports, "render_results", lambda *_: pytest.fail("must not call stale renderer"))
    result = service.request(
        {
            "job_id": job["job_id"],
            "token": key,
            "operation": "_terminal",
            "arguments": {"thread": thread, "turn": turn, "status": "completed"},
        }
    )
    assert result == {"finished": True, "report_refresh_required": True, "requires_ops": True}
    assert not store.status()["jobs"]
    assert store.status()["tasks"][0]["state"] == "blocked"  # No submission is never an automatic PASS.
    with store.transaction() as db:
        assert not db.execute("SELECT 1 FROM verdicts").fetchone()
