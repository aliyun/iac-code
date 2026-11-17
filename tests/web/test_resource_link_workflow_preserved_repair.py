"""Offline product diagnosis with owned resources held; no cloud execution or cleanup credit."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime, exclusive
from scripts.web.e2e.resource_link_workflow.server import Service
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from tests.web.test_resource_link_workflow import bound, plan, reserve


@pytest.fixture
def preserved(tmp_path):
    store = Store.initialize(tmp_path / "state")
    plan(store)
    task_id = store.status()["tasks"][0]["id"]
    repo = tmp_path / "repo"
    code = repo / "src/gate.py"
    code.parent.mkdir(parents=True)
    code.write_text("# Offline fake gate preimage\n", encoding="utf-8")
    json_write(store.root / "environment.json", {"repo": str(repo)})
    json_write(store.root / "packets/original/packet.json", {"task_id": task_id, "fixture": True})
    json_write(store.root / "jobs/glm/output/source.json", {"fixture": "not a real cloud response"})
    proof = {
        "schema_version": 1,
        "kind": "ops_preserved_product_gap",
        "task_id": task_id,
        "diagnosis": "Offline signer-lineage diagnosis",
        "formal_test": False,
        "pass_added": False,
        "packet_sha256": digest(store.root / "packets/original/packet.json"),
        "ownership_ids": ["own-present", "own-unknown"],
        "original_job": {"id": "glm", "thread_id": "glm-thread", "turn_id": "glm-turn"},
        "review_job": {"id": "review", "thread_id": "sol-thread", "turn_id": "review-turn"},
        "sources": [
            {"path": "jobs/glm/output/source.json", "sha256": digest(store.root / "jobs/glm/output/source.json")}
        ],
        "code_evidence": [{"path": "src/gate.py", "sha256": digest(code), "fact": "offline preimage"}],
    }
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='blocked',attempt=2,slot=0,packet='packets/original/packet.json'")
        db.execute("UPDATE slots SET task_id=? WHERE id=0", (task_id,))
        for index, (role, purpose, binding) in enumerate(
            (
                ("glm", "dispatch", proof["original_job"]),
                ("sol", "review", proof["review_job"]),
            )
        ):
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    binding["id"],
                    task_id,
                    role,
                    purpose,
                    "complete",
                    binding["thread_id"],
                    binding["turn_id"],
                    "offline-capability",
                    "result_staged",
                    index + 1,
                ),
            )
        for owner, state in (("own-present", "present"), ("own-unknown", "unknown")):
            db.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (
                    owner,
                    task_id,
                    "QUERY",
                    dumps({"id": owner, "region": "cn-hangzhou"}),
                    state,
                    "jobs/glm/output/source.json",
                ),
            )
    store.pause(True)
    json_write(store.root / "inputs/repair.json", proof)
    # A sentinel verifies this operation never touches the independent wire database.
    (store.root / "execution-gate.sqlite3").write_bytes(b"offline wire sentinel")
    return store, task_id, proof, code


def snapshot(store):
    with store.transaction() as db:
        return {
            name: [tuple(row) for row in db.execute(f"SELECT * FROM {name}")]
            for name in (
                "slots",
                "jobs",
                "owned",
                "observations",
                "verdicts",
                "budget_reservations",
                "locks",
            )
        }


def test_preserved_repair_only_queues_same_sol_and_holds_all_resources(preserved):
    store, task_id, _, code = preserved
    before = snapshot(store)
    wire = (store.root / "execution-gate.sqlite3").read_bytes()
    result = Runtime(store).queue_preserved_repair(task_id, "repair.json", "verified offline diagnosis")
    assert result["ownership_and_wire_unchanged"] and result["no_glm_dispatched"]
    assert snapshot(store) == before
    assert (store.root / "execution-gate.sqlite3").read_bytes() == wire
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert (task["state"], task["slot"], task["attempt"], task["packet"]) == (
            "repair",
            0,
            2,
            "packets/original/packet.json",
        )
        request, trigger = store.repair_context(db, task_id)
        assert request["ownership_preserved"] and not request["execution_recovery_authorized"]
        assert trigger["kind"] == "ops_preserved_repair"
    # Repairs change code: original source evidence stays immutable, code preimage is not rechecked at context.
    code.write_text("# Offline repaired gate\n", encoding="utf-8")
    with store.transaction() as db:
        assert store.repair_context(db, task_id)[0]["ownership_preserved"]
    store.pause(False)
    job = reserve(store, "repair")
    token, _, _ = bound(store, job)
    with pytest.raises(WorkflowError, match="cannot approve/reinstall"):
        store.approve_packet(job["job_id"], token, {})


@pytest.mark.parametrize(
    "damage",
    [
        "pause",
        "busy",
        "issued",
        "state",
        "slot",
        "slot_owner",
        "owners",
        "packet",
        "glm",
        "review",
        "source",
        "code",
        "kind",
        "task",
        "formal",
        "no_diagnosis",
        "no_code",
        "no_sources",
    ],
)
def test_preserved_repair_rejects_drift_without_mutating_execution(preserved, damage):
    store, task_id, proof, code = preserved
    with store.transaction() as db:
        if damage == "pause":
            db.execute("UPDATE meta SET value='false' WHERE key='paused'")
        elif damage == "busy":
            db.execute("UPDATE jobs SET state='unknown' WHERE id='glm'")
        elif damage == "issued":
            db.execute("INSERT INTO actions VALUES('issued',?,'repair','issued',0)", (task_id,))
        elif damage == "state":
            db.execute("UPDATE tasks SET state='complete'")
        elif damage == "slot":
            db.execute("UPDATE tasks SET slot=NULL")
        elif damage == "slot_owner":
            db.execute("UPDATE slots SET task_id=NULL WHERE id=0")
        elif damage == "owners":
            db.execute("UPDATE owned SET state='deleted' WHERE id='own-present'")
    if damage in {"glm", "review"}:
        proof["original_job" if damage == "glm" else "review_job"]["turn_id"] = "wrong-turn"
    elif damage == "packet":
        proof["packet_sha256"] = "0" * 64
    elif damage == "source":
        json_write(store.root / proof["sources"][0]["path"], {"changed": True})
    elif damage == "code":
        code.write_text("# Changed concurrently\n", encoding="utf-8")
    elif damage in {"kind", "task", "formal", "no_diagnosis", "no_code", "no_sources"}:
        key, value = {
            "kind": ("kind", "preparation_product_gap"),
            "task": ("task_id", "other"),
            "formal": ("formal_test", True),
            "no_diagnosis": ("diagnosis", ""),
            "no_code": ("code_evidence", []),
            "no_sources": ("sources", []),
        }[damage]
        proof[key] = value
    json_write(store.root / "inputs/repair.json", proof)
    before = snapshot(store)
    with pytest.raises(WorkflowError):
        Runtime(store).queue_preserved_repair(task_id, "repair.json", "diagnosis")
    assert snapshot(store) == before


def test_preserved_repair_context_rejects_changed_original_evidence(preserved):
    store, task_id, proof, _ = preserved
    Runtime(store).queue_preserved_repair(task_id, "repair.json", "diagnosis")
    json_write(store.root / proof["sources"][0]["path"], {"changed": True})
    with store.transaction() as db, pytest.raises(WorkflowError, match="original evidence changed"):
        store.repair_context(db, task_id)


def test_preserved_repair_context_rejects_changed_request(preserved):
    store, task_id, proof, _ = preserved
    Runtime(store).queue_preserved_repair(task_id, "repair.json", "diagnosis")
    proof["diagnosis"] = "changed"
    json_write(store.root / "inputs/repair.json", proof)
    with store.transaction() as db, pytest.raises(WorkflowError, match="request changed"):
        store.repair_context(db, task_id)


def test_preserved_repair_can_resume_after_terminal_repair_using_fresh_bound_proof(preserved):
    store, task_id, proof, _ = preserved
    with store.transaction() as db:
        db.execute("UPDATE jobs SET purpose='repair' WHERE id='review'")
    before = snapshot(store)
    wire = (store.root / "execution-gate.sqlite3").read_bytes()
    result = Runtime(store).queue_preserved_repair(task_id, "repair.json", "terminal repair transport reconciled")
    assert result["no_glm_dispatched"]
    assert snapshot(store) == before
    assert (store.root / "execution-gate.sqlite3").read_bytes() == wire


def test_real_adapter_terminal_cannot_be_supplied_from_desktop_cache(preserved):
    store, _, _, _ = preserved
    directory = store.root / "jobs/glm"
    json_write(directory / "launch.json", {"job_id": "glm"})
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id='glm'")
    with pytest.raises(WorkflowError, match="owning-backend exact terminal"):
        store.finish_job("glm", "glm-thread", "glm-turn", "interrupted")
    with store.transaction() as db:
        assert db.execute("SELECT state FROM jobs WHERE id='glm'").fetchone()[0] == "running"
    (directory / "raw-rollout.jsonl").write_text(
        dumps(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "glm-thread",
                    "turn": {"id": "glm-turn", "status": "completed"},
                },
            }
        )
        + "\n", encoding="utf-8"
    )
    store.finish_job("glm", "glm-thread", "glm-turn", "completed")


@pytest.mark.parametrize(
    "damage", [None, "terminal", "live_adapter", "stale", "wrong_turn", "unpaused", "missing_adapter"]
)
def test_active_restore_attaches_same_turn_without_new_model_or_cloud(preserved, monkeypatch, damage):
    from scripts.web.e2e.resource_link_workflow import active_restore

    store, task_id, _, _ = preserved
    with store.transaction() as db:
        db.execute("UPDATE jobs SET purpose='repair',submission='confirmed' WHERE id='review'")
        job = dict(db.execute("SELECT * FROM jobs WHERE id='review'").fetchone())
        if damage == "unpaused":
            db.execute("UPDATE meta SET value='false' WHERE key='paused'")
    directory = store.root / "jobs/review"
    json_write(directory / "launch.json", {"pid": 123, "identity": "original fake process"})
    (directory / "raw-rollout.jsonl").write_text(
        dumps(
            {
                "method": "transport/turn_created",
                "params": {
                    "threadId": "sol-thread",
                    "turnId": "review-turn",
                },
            }
        )
        + "\n", encoding="utf-8"
    )
    raw = {
        "method": "thread/read",
        "read_at": datetime.now(timezone.utc).isoformat(),
        "evidence_scope": "exact_bound_turn_full",
        "job_binding": job,
        "thread": {
            "id": "sol-thread",
            "turns": [{"id": "review-turn", "itemsView": "full", "status": "inProgress", "items": []}],
        },
        "transport_evidence": {"path": "jobs/review/raw-rollout.jsonl", "state": "present"},
        "original_responses": {
            "metadata": {
                "method": "thread/read",
                "params": {"threadId": "sol-thread", "includeTurns": False},
                "result": {"thread": {"id": "sol-thread"}},
            }
        },
    }
    if damage == "terminal":
        raw["thread"]["turns"][0]["status"] = "completed"
    elif damage == "stale":
        raw["read_at"] = "2020-01-01T00:00:00+00:00"
    elif damage == "wrong_turn":
        raw["thread"]["turns"][0]["id"] = "other"
    elif damage == "missing_adapter":
        monkeypatch.setattr(active_restore, "__file__", str(store.root / "missing-code/active_restore.py"))
    json_write(directory / "ops-thread-read-0001.json", raw)
    runtime = Runtime(store)
    monkeypatch.setattr(
        runtime,
        "assert_process",
        Mock(side_effect=None if damage == "live_adapter" else subprocess.CalledProcessError(1, "ps", output="")),
    )
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "offline restored bridge")
    def launch_bridge(*args, **kwargs):
        with pytest.raises(WorkflowError, match="already owned"):
            with exclusive(directory / "launch.lock"):
                pytest.fail("restore must hold the launch lock until its owner identity is recorded")
        return Mock(pid=456)

    launch = Mock(side_effect=launch_bridge)
    monkeypatch.setattr(active_restore.subprocess, "Popen", launch)
    wire = (store.root / "execution-gate.sqlite3").read_bytes()
    if damage:
        with pytest.raises(WorkflowError):
            active_restore.restore(runtime, "review", "jobs/review/ops-thread-read-0001.json")
        launch.assert_not_called()
        if damage == "missing_adapter":
            assert not (directory / "active-restore.json").exists()
            with store.transaction() as db:
                assert db.execute("SELECT state FROM jobs WHERE id='review'").fetchone()[0] == "complete"
                assert db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "blocked"
    else:
        value = active_restore.restore(runtime, "review", "jobs/review/ops-thread-read-0001.json")
        assert value["no_new_turn"] and value["cloud_quota_added"] == 0
        assert launch.call_args.args[0][-1] == "--attach"
        assert Path(launch.call_args.args[0][1]) == Path(active_restore.__file__).with_name("adapter.mjs")
        assert Path(launch.call_args.args[0][1]).is_file()
        with store.transaction() as db:
            assert db.execute("SELECT state FROM jobs WHERE id='review'").fetchone()[0] == "running"
            assert db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "repairing"
        before_restart = snapshot(store)
        original_launch = (directory / "launch.json").read_bytes()
        current_owner = Mock()
        monkeypatch.setattr(Runtime, "assert_process", current_owner)
        restarted = Service(Store(store.root))
        current_owner.assert_called_once_with(456, "offline restored bridge")
        assert snapshot(restarted.store) == before_restart
        assert (directory / "launch.json").read_bytes() == original_launch
        launch.assert_called_once()
    assert (store.root / "execution-gate.sqlite3").read_bytes() == wire


@pytest.mark.parametrize("damage", ["dead", "job_id", "thread_id", "turn_id", "missing_launch"])
def test_restart_rejects_unverified_restored_bridge(preserved, monkeypatch, damage):
    store, _, _, _ = preserved
    directory = store.root / "jobs/review"
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='running',purpose='repair',submission='confirmed' WHERE id='review'")
    proof = {"job_id": "review", "thread_id": "sol-thread", "turn_id": "review-turn"}
    json_write(directory / "launch.json", {"pid": 123, "identity": "original fake process"})
    json_write(directory / "active-restore.json", proof)
    marker = {**proof, "pid": 456, "identity": "offline restored bridge"}
    if damage in proof:
        marker[damage] = "another-binding"
    if damage != "missing_launch":
        json_write(directory / "active-restore-launch.json", marker)

    def owner(_runtime, pid, identity):
        if damage == "dead":
            raise WorkflowError("offline restored bridge is gone")
        assert (pid, identity) == (456, "offline restored bridge")

    monkeypatch.setattr(Runtime, "assert_process", owner)
    wire = (store.root / "execution-gate.sqlite3").read_bytes()
    restarted = Service(Store(store.root))
    assert restarted.store.status()["jobs"][0]["state"] == "unknown"
    assert read_json(directory / "active-restore.json") == proof
    assert (store.root / "execution-gate.sqlite3").read_bytes() == wire


@pytest.mark.parametrize("operation", ["status", "next"])
@pytest.mark.parametrize("restored", [False, True])
def test_adopted_owner_exit_is_detected_on_existing_control_reads(preserved, monkeypatch, operation, restored):
    store, _, _, _ = preserved
    directory = store.root / "jobs/review"
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='running',purpose='repair',submission='confirmed' WHERE id='review'")
    json_write(directory / "launch.json", {"pid": 123, "identity": "original fake process"})
    if restored:
        proof = {"job_id": "review", "thread_id": "sol-thread", "turn_id": "review-turn"}
        json_write(directory / "active-restore.json", proof)
        json_write(directory / "active-restore-launch.json", {**proof, "pid": 456, "identity": "restored bridge"})
    live = True
    checked = []

    def owner(_runtime, pid, identity):
        checked.append((pid, identity))
        if not live:
            raise WorkflowError("offline owner ended or PID identity changed")

    monkeypatch.setattr(Runtime, "assert_process", owner)
    service = Service(store)
    monkeypatch.setattr(service, "attest_control", lambda: {})
    before = snapshot(store)
    wire = (store.root / "execution-gate.sqlite3").read_bytes()
    marker = (directory / "launch.json").read_bytes()
    service.request({"operation": operation})
    assert snapshot(store) == before
    assert len(checked) == 2
    assert checked[-1] == ((456, "restored bridge") if restored else (123, "original fake process"))
    live = False
    # The normal launch/restore marker-writing window must not be diagnosed as an exited owner.
    with exclusive(directory / "launch.lock"):
        service.request({"operation": operation})
    assert snapshot(store) == before and len(checked) == 2
    service.request({"operation": operation})
    assert store.status()["jobs"][0]["state"] == "unknown"
    after = snapshot(store)
    for table in before.keys() - {"jobs"}:
        assert after[table] == before[table]
    revision = store.status()["revision"]
    service.request({"operation": operation})
    assert store.status()["revision"] == revision, "do not repeatedly park or redispatch the same unknown owner"
    assert (directory / "launch.json").read_bytes() == marker
    assert (store.root / "execution-gate.sqlite3").read_bytes() == wire


def test_terminal_job_is_not_parked_when_adopted_owner_exits(preserved, monkeypatch):
    store, _, _, _ = preserved
    directory = store.root / "jobs/review"
    json_write(directory / "launch.json", {"pid": 123, "identity": "original fake process"})
    monkeypatch.setattr(Runtime, "assert_process", Mock(side_effect=WorkflowError("owner ended")))
    service = Service(store)
    monkeypatch.setattr(service, "attest_control", lambda: {})
    before = snapshot(store)
    revision = store.status()["revision"]
    service.request({"operation": "status"})
    service.request({"operation": "next"})
    assert snapshot(store) == before and store.status()["revision"] == revision


def test_terminal_event_during_owner_recheck_keeps_its_terminal_state(preserved, monkeypatch):
    store, _, _, _ = preserved
    directory = store.root / "jobs/review"
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id='review'")
    service = Service(store)
    json_write(directory / "launch.json", {"pid": 123, "identity": "original fake process"})

    def terminal_then_exit(*args):
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='complete' WHERE id='review'")
        raise WorkflowError("owner exited after terminal confirmation")

    monkeypatch.setattr(service.runtime, "assert_process", terminal_then_exit)
    monkeypatch.setattr(service, "attest_control", lambda: {})
    revision = store.status()["revision"]
    service.request({"operation": "status"})
    service.watch(Mock(), "review")
    with store.transaction() as db:
        assert db.execute("SELECT state FROM jobs WHERE id='review'").fetchone()[0] == "complete"
    assert store.status()["revision"] == revision
