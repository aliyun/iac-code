"""A successor preserves failed history and all original authority; no real cloud calls."""

import copy
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.plan_handoff import canonical_sha
from tests.web.test_resource_link_workflow_remaining_review import fixture_review


def handoff_fixture(tmp_path):
    plan, _, store = fixture_review(tmp_path)
    store.import_plan(plan)
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='blocked',reason='Original failure, not success',attempt=2")
        connection.execute("INSERT INTO meta VALUES(?,?)", ("sol_thread:" + plan["plan_id"], "sole-existing-sol"))
        connection.execute("INSERT INTO budget_reservations VALUES(?,?)", (plan["plan_id"] + ":R0042", 17))
        connection.execute(
            "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
            (plan["plan_id"] + ":R0042", "R0042-ROS-1-iframe", "fail", "original-receipt", 123),
        )
        snapshot = [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY id")]
    successor = copy.deepcopy(plan)
    successor["plan_id"] = "remaining-successor"
    scenes = sorted(scene for resource in successor["resources"] for scene in resource["retest_scope"]["scene_ids"])
    human = {
        "approved": True,
        "plan_id": successor["plan_id"],
        "scene_ids": scenes,
        "verbatim_user_message": "Complete these remaining navigation tests; preserve original results.",
    }
    json_write(store.root / "inputs/human.json", human)
    proof = {
        "schema_version": 1,
        "kind": "ops_plan_successor_review",
        "approved": True,
        "reason": "Independent paused/drained read-only successor review",
        "predecessor_plan_id": plan["plan_id"],
        "successor_plan_id": successor["plan_id"],
        "predecessor_frozen_sha256": canonical_sha(plan),
        "successor_frozen_sha256": canonical_sha(successor),
        "predecessor_tasks_sha256": canonical_sha(snapshot),
        "scene_ids": scenes,
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(store.root / "inputs/human.json"),
        "reuse_sol_thread": "sole-existing-sol",
    }
    json_write(store.root / "inputs/handoff.json", proof)
    return store, plan, successor, snapshot, proof


def test_successor_is_atomic_idempotent_and_preserves_history_and_sol(tmp_path):
    store, original, successor, snapshot, _ = handoff_fixture(tmp_path)
    assert store.import_plan(successor, successor_review="inputs/handoff.json") == successor["plan_id"]
    revision = store.status()["revision"]
    assert store.import_plan(successor, successor_review="inputs/handoff.json") == successor["plan_id"]
    assert store.status()["revision"] == revision
    with store.transaction() as connection:
        old = [
            dict(row)
            for row in connection.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (original["plan_id"],))
        ]
        for before, after in zip(snapshot, old):
            assert after == {**before, "state": "superseded"}
        assert (
            json.loads(connection.execute("SELECT plan FROM runs WHERE id=?", (original["plan_id"],)).fetchone()[0])
            == original
        )
        assert connection.execute("SELECT scene,status,receipt,created FROM verdicts").fetchone()[:] == (
            "R0042-ROS-1-iframe",
            "fail",
            "original-receipt",
            123,
        )
        assert connection.execute("SELECT amount FROM budget_reservations").fetchone()[0] == 17
        assert store.plan_sol_thread(connection, successor["plan_id"]) == "sole-existing-sol"
        assert not store.finalizable(connection)
    assert [run["plan_id"] for run in store.status()["active_plans"]] == [successor["plan_id"]]
    store.pause(False)
    assert store.status()["progress"]["next_candidate"]["task_id"].startswith(successor["plan_id"] + ":")


@pytest.mark.parametrize(
    "change,match",
    [
        ("unpaused", "paused"),
        ("owned", "cleanup"),
        ("job", "drained"),
        ("slot", "slots"),
        ("stale_snapshot", "snapshot"),
        ("sha", "SHA"),
        ("drop_scene", "every exact scene"),
        ("sol", "unique Sol"),
        ("write", "executed writes"),
        ("unknown", "unknown calls"),
        ("active_capability", "capabilities"),
    ],
)
def test_unsafe_successor_rejected_without_mutating_predecessor(tmp_path, change, match):
    store, original, successor, snapshot, proof = handoff_fixture(tmp_path)
    with store.transaction() as connection:
        if change == "unpaused":
            connection.execute("UPDATE meta SET value='false' WHERE key='paused'")
        elif change == "owned":
            connection.execute(
                "INSERT INTO owned VALUES('owner',?,'TF','{}','present','original')", (snapshot[0]["id"],)
            )
        elif change == "job":
            connection.execute(
                "INSERT INTO jobs VALUES('live',?,'glm','execute','unknown',NULL,NULL,'x','unknown',0)",
                (snapshot[0]["id"],),
            )
        elif change == "slot":
            connection.execute("UPDATE slots SET task_id=? WHERE id=0", (snapshot[0]["id"],))
        elif change == "stale_snapshot":
            connection.execute("UPDATE tasks SET attempt=3 WHERE id=?", (snapshot[0]["id"],))
        elif change in {"write", "unknown", "active_capability"}:
            connection.execute(
                "INSERT INTO jobs VALUES('old-job',?,'glm','execute','complete','thread','turn','x','done',0)",
                (snapshot[0]["id"],),
            )
    if change in {"write", "unknown", "active_capability"}:
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as wire:
            wire.executescript(gate.DDL)
            if change == "active_capability":
                wire.execute("INSERT INTO active VALUES('0','old-job:TF','session','account',NULL,'execute')")
            else:
                wire.execute(
                    "INSERT INTO grants(scope,id,kind,call,quota,used) VALUES('old-job:TF','operation',?,'{}',1,1)",
                    ("create" if change == "write" else "read",),
                )
                wire.execute(
                    "INSERT INTO attempts VALUES('attempt','old-job:TF','operation','session',?,'{}','signer',0)",
                    ("unknown" if change == "unknown" else "succeeded",),
                )
    if change == "sha":
        proof["successor_frozen_sha256"] = "changed"
    elif change == "drop_scene":
        proof["scene_ids"] = proof["scene_ids"][:-1]
    elif change == "sol":
        proof["reuse_sol_thread"] = "another-sol"
    json_write(store.root / "inputs/handoff.json", proof)
    with pytest.raises(WorkflowError, match=match):
        store.import_plan(successor, successor_review="inputs/handoff.json")
    with store.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM runs WHERE id=?", (successor["plan_id"],)).fetchone()
        assert not connection.execute("SELECT 1 FROM tasks WHERE state='superseded'").fetchone()
        assert (
            json.loads(connection.execute("SELECT plan FROM runs WHERE id=?", (original["plan_id"],)).fetchone()[0])
            == original
        )


def test_failed_import_rolls_back_handoff_and_owner_index_migration(tmp_path):
    store, original, successor, snapshot, proof = handoff_fixture(tmp_path)
    successor["resources"][0]["name"] = "incorrect identity"
    proof["successor_frozen_sha256"] = canonical_sha(successor)
    json_write(store.root / "inputs/handoff.json", proof)
    with pytest.raises(WorkflowError, match="permanent catalog identity"):
        store.import_plan(successor, successor_review="inputs/handoff.json")
    with store.transaction() as connection:
        assert [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY id")] == snapshot
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert not connection.execute("SELECT 1 FROM events WHERE kind='plan_superseded'").fetchone()
