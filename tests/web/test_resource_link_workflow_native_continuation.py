"""Retained-generation controller recovery: offline only; never cloud credentials."""
# ruff: noqa: F811

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow import native_continuation as continuation
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.server import reconcile
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from tests.web.test_resource_link_execution_gate import (  # noqa: F401
    native,
    native_filesystem,
    native_renew,
    rotated_native,
)
from tests.web.test_resource_link_workflow import plan, reserve


@pytest.fixture
def retained(native, tmp_path, monkeypatch):
    native_filesystem(native)
    store = Store.initialize(tmp_path / "state")
    plan(store)
    task_id = store.status()["tasks"][0]["id"]
    repo = tmp_path / "repo"
    code = repo / "src/gate.py"
    code.parent.mkdir(parents=True)
    code.write_text("# Offline repaired gate\n", encoding="utf-8")
    json_write(store.root / "environment.json", {"repo": str(repo)})
    packet = {
        "task_id": task_id,
        "requests": {"QUERY": "Offline request"},
        "execution_grants": {"QUERY": list(native[2].values())},
        "inputs": [],
        "scenes": {"QUERY": {"account": "123"}},
    }
    json_write(store.root / "packets/original/packet.json", packet)
    json_write(
        store.root / "jobs/native/output/self-created-session.json",
        {
            "job_id": "native",
            "target": "target-0",
            "origin": "http://127.0.0.1:16100",
            "session_id": "ws~native",
        },
    )
    for ident, role, purpose in (("native", "glm", "dispatch"), ("repair", "sol", "repair")):
        (store.root / "jobs" / ident).mkdir(parents=True, exist_ok=True)
        binding = {
            "id": ident,
            "task_id": task_id,
            "role": role,
            "purpose": purpose,
            "state": "complete",
            "thread_id": role + "-thread",
            "turn_id": ident + "-turn",
            "submission": "result_staged",
        }
        (store.root / f"jobs/{ident}/raw-rollout.jsonl").write_text(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"threadId": role + "-thread", "turn": {"id": ident + "-turn", "status": "completed"}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        json_write(
            store.root / f"jobs/{ident}/ops-thread-read-0001.json",
            {
                "method": "thread/read",
                "job_binding": binding,
                "transport_evidence": {"path": f"jobs/{ident}/raw-rollout.jsonl", "state": "present"},
                "thread": {
                    "id": role + "-thread",
                    "turns": [{"id": ident + "-turn", "status": "completed"}],
                },
            },
        )
    json_write(
        store.root / "inputs/repair.json",
        {
            "kind": "product_repair_preserved_native_signer",
            "task_id": task_id,
            "job_id": "repair",
            "formal_test": False,
            "cloud_calls": 0,
            "pass_added": False,
            "changes": [{"path": "src/gate.py", "sha256": digest(code)}],
        },
    )
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='blocked',attempt=2,slot=0,packet='packets/original/packet.json'")
        db.execute("UPDATE slots SET task_id=?,target='target-0' WHERE id=0", (task_id,))
        for index, (ident, role, purpose) in enumerate(
            (
                ("native", "glm", "dispatch"),
                ("repair", "sol", "repair"),
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
        for ident, state in (("owner", "present"), ("implicit", "unknown")):
            db.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    task_id,
                    "QUERY",
                    dumps({"id": ident}),
                    state,
                    "jobs/native/output/source.json",
                ),
            )
    store.pause(True)
    target = store.root / "execution-gate.sqlite3"
    shutil.copy2(native[0], target)
    # Only authorization/admission are stubbed; real gate graph, signer and journal checks run.
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.creation.validate_authorization", lambda *a: None)
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.creation.execution_authority", lambda *a: "native:QUERY"
    )
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.temporary_samples.admitted", lambda *a: {"QUERY": {}})
    monkeypatch.setenv(gate.ENV, str(target))
    return Runtime(store), task_id, (target, native[1], native[2])


def approved(runtime):
    proof = continuation.inspect(runtime, "native", "repair.json")
    json_write(runtime.root / "inputs/approved.json", {**proof, "approved": True}, private=True)
    return proof


def unchanged(runtime):
    with runtime.store.transaction() as db:
        state = {
            name: [dict(r) for r in db.execute(f"SELECT * FROM {name}")]
            for name in (
                "owned",
                "observations",
                "verdicts",
                "budget_reservations",
                "locks",
                "slots",
            )
        }
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        wire = {name: [dict(r) for r in db.execute(f"SELECT * FROM {name}")] for name in ("attempts", "grants")}
    return state, wire


def test_queue_preserves_original_generation_and_requires_real_renewal(retained):
    runtime, task_id, native = retained
    before, wire = unchanged(runtime)
    proof = approved(runtime)
    assert proof["approved"] is False and proof["new_creation_quota"] == 0
    result = continuation.queue(runtime, "native", "approved.json")
    assert result["no_glm_dispatched"] and not result["old_calls_refilled"] and not result["pass_added"]
    after, new_wire = unchanged(runtime)
    assert after == before and new_wire["attempts"] == wire["attempts"]
    assert [g for g in new_wire["grants"] if g["id"] != proof["grant_id"]] == wire["grants"]
    assert len(new_wire["grants"]) == len(wire["grants"]) + 1
    with runtime.store.transaction() as db:
        recovery = continuation.load(runtime.root, db, task_id)
    assert "GetCallerIdentity" in continuation.delivery(runtime.root, recovery, "identity")
    with pytest.raises(WorkflowError, match="actual renewed"):
        continuation.delivery(runtime.root, recovery, "source")
    native_renew(rotated_native(native))
    assert "文件系统" in continuation.delivery(runtime.root, recovery, "source")
    with pytest.raises(WorkflowError, match="one-shot"):
        continuation.delivery(runtime.root, recovery, "identity")
    with pytest.raises(WorkflowError, match="phase"):
        continuation.delivery(runtime.root, recovery, "cleanup")
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    assert "反向清理" in continuation.delivery(runtime.root, recovery, "cleanup")


def test_scheduler_reuses_slot_thread_without_reserving_new_cloud_generation(retained):
    runtime, task_id, _ = retained
    approved(runtime)
    continuation.queue(runtime, "native", "approved.json")
    before = unchanged(runtime)
    runtime.store.set_review_only("Keep normal dispatch held during reviewed original-generation recovery")
    runtime.store.pause(False)
    assert runtime.store.status()["review_only"]
    job = reserve(runtime.store, "continuation")
    with runtime.store.transaction() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert (row["purpose"], row["thread_id"], row["role"]) == ("continuation", "glm-thread", "glm")
    assert task["attempt"] == 3 and task["slot"] == 0
    assert unchanged(runtime) == before
    assert runtime.store.status()["review_only"]


def test_exact_no_turn_review_only_rejection_requeues_same_continuation(retained, monkeypatch):
    runtime, task_id, _ = retained
    approved(runtime)
    continuation.queue(runtime, "native", "approved.json")
    runtime.store.set_review_only("Keep normal dispatch held during reviewed original-generation recovery")
    runtime.store.pause(False)
    job = reserve(runtime.store, "continuation")
    runtime.store.pause(True)
    with runtime.store.transaction() as db:
        db.execute("UPDATE jobs SET state='unknown',submission='not_submitted' WHERE id=?", (job["job_id"],))
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        original = db.execute("SELECT * FROM jobs WHERE id='native'").fetchone()
    marker = runtime.root / "jobs" / job["job_id"] / "launch.json"
    json_write(
        marker,
        {
            "job_id": job["job_id"],
            "thread_id": original["thread_id"],
            "role": "glm",
            "pid": 123456,
            "identity": "dead-adapter-fixture",
        },
        private=True,
    )
    records = [
        {"method": "transport/backend_selected", "params": {"role": "glm"}},
        {
            "method": "transport/adapter_error",
            "params": {"message": "review-only hold forbids new execution/preparation turns"},
        },
    ]
    trace = runtime.root / "jobs" / job["job_id"] / "raw-rollout.jsonl"
    trace.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    original_turn = original["turn_id"]
    raw = {
        "method": "thread/read",
        "read_at": datetime.now(timezone.utc).isoformat(),
        "thread": {"id": original["thread_id"], "turns": [{"id": original_turn, "status": "completed", "items": []}]},
    }
    monkeypatch.setattr(
        Runtime, "assert_process", lambda *_args: (_ for _ in ()).throw(WorkflowError("adapter exited"))
    )
    before = unchanged(runtime)
    result = reconcile(runtime.store, job["job_id"], raw)
    assert result == {
        "reconciled": job["job_id"],
        "terminal": "not_started",
        "same_job_requeued": True,
        "wire_unchanged": True,
        "new_turn_submitted": False,
    }
    after = unchanged(runtime)
    assert after[1] == before[1]
    with runtime.store.transaction() as db:
        current = db.execute("SELECT state,submission FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
        task_after = db.execute("SELECT state,attempt,slot FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert tuple(current) == ("queued", "not_submitted")
    assert tuple(task_after) == ("running", task["attempt"], task["slot"])
    assert not marker.exists()
    assert list(marker.parent.glob("launch-not-started-*.json"))
    with pytest.raises(WorkflowError, match="current unknown lease"):
        reconcile(runtime.store, job["job_id"], raw)


@pytest.mark.parametrize(
    "damage",
    [
        "pause",
        "busy",
        "issued",
        "state",
        "slot",
        "owners",
        "terminal",
        "session",
        "repair",
        "packet",
        "wire",
        "grant",
        "counter",
        "approval",
        "permissions",
        "extra",
        "symlink",
    ],
)
def test_recovery_refuses_drift_and_never_mutates_original(retained, damage):
    runtime, _, native = retained
    approved(runtime)
    with runtime.store.transaction() as db:
        if damage == "pause":
            db.execute("UPDATE meta SET value='false' WHERE key='paused'")
        elif damage == "busy":
            db.execute("UPDATE jobs SET state='unknown' WHERE id='native'")
        elif damage == "issued":
            db.execute("INSERT INTO actions VALUES('issued','offline:R0001','dispatch','issued',0)")
        elif damage == "state":
            db.execute("UPDATE tasks SET state='complete'")
        elif damage == "slot":
            db.execute("UPDATE slots SET target='other' WHERE id=0")
        elif damage == "owners":
            db.execute("UPDATE owned SET state='deleted'")
    if damage in {"wire", "grant", "counter"}:
        with gate.database(native[0]) as db:
            db.execute(
                {
                    "wire": "UPDATE attempts SET tool_use_id='changed'",
                    "grant": "UPDATE grants SET quota=quota+1 WHERE id='read-filesystem-ready'",
                    "counter": "UPDATE authority_limits SET used=0 WHERE id='identity'",
                }[damage]
            )
    elif damage == "terminal":
        json_write(runtime.root / "jobs/native/ops-thread-read-0002.json", {"evidence_scope": "notification"})
    elif damage == "session":
        json_write(runtime.root / "jobs/native/output/self-created-session.json", {"session_id": "wrong"})
    elif damage == "repair":
        (Path(runtime.config["repo"]) / "src/gate.py").write_text("changed", encoding="utf-8")
    elif damage == "packet":
        path = runtime.root / "packets/original/packet.json"
        value = read_json(path)
        value["execution_grants"]["QUERY"][0]["quota"] += 1
        json_write(path, value)
    elif damage in {"approval", "extra"}:
        path = runtime.root / "inputs/approved.json"
        value = read_json(path)
        value["approved" if damage == "approval" else "unreviewed"] = False
        json_write(path, value, private=True)
    elif damage == "permissions":
        (runtime.root / "inputs/approved.json").chmod(0o644)
    elif damage == "symlink":
        shutil.move(runtime.root / "inputs/approved.json", runtime.root / "inputs/real.json")
        (runtime.root / "inputs/approved.json").symlink_to("real.json")
    before = unchanged(runtime)
    with pytest.raises((WorkflowError, gate.GateDeniedError)):
        continuation.queue(runtime, "native", "approved.json")
    assert unchanged(runtime) == before


def test_second_registration_and_tampered_review_fail_closed(retained):
    runtime, task_id, _ = retained
    approved(runtime)
    continuation.queue(runtime, "native", "approved.json")
    before = unchanged(runtime)
    with pytest.raises(WorkflowError):
        continuation.queue(runtime, "native", "approved.json")
    assert unchanged(runtime) == before
    with runtime.store.transaction() as db:
        recovery = continuation.load(runtime.root, db, task_id)
    path = runtime.root / "inputs/approved.json"
    json_write(path, {**read_json(path), "new_creation_quota": 1}, private=True)
    with pytest.raises(WorkflowError, match="changed"):
        continuation.delivery(runtime.root, recovery, "identity")
