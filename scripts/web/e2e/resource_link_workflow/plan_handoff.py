"""Atomic OPS handoff of drained read-only navigation plans; no history is rewritten."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .files import WorkflowError, contained, digest, read_json
from .plans import expected_scenes

LOADED_SHA = digest(Path(__file__))


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_file(store: Any, evidence: str) -> tuple[Any, dict[str, Any]]:
    path = contained(store.root / "inputs", evidence.removeprefix("inputs/"))
    value = read_json(path)
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "ops_plan_successor_review"
        or (value.get("approved") is not True or not value.get("reason"))
    ):
        raise WorkflowError("exact independent OPS successor review required")
    return path, value


def verify_recorded_handoff(store: Any, connection: sqlite3.Connection, plan: dict[str, Any], evidence: str) -> None:
    path, _ = review_file(store, evidence)
    recorded = [
        json.loads(row[0]) for row in connection.execute("SELECT data FROM events WHERE kind='plan_superseded'")
    ]
    if not any(
        item.get("successor_plan_id") == plan["plan_id"] and item.get("evidence_sha256") == digest(path)
        for item in recorded
    ):
        raise WorkflowError("successor already exists without this exact recorded handoff")


def handoff_readonly_plan(store: Any, connection: sqlite3.Connection, plan: dict[str, Any], evidence: str) -> None:
    """Called inside import's transaction: any failed import rolls the handoff back too.

    Deliberately excludes executed write plans. Their authority/cleanup migration needs
    a separate reviewed protocol, not a new plan ID that replenishes creation quotas.
    """
    path, proof = review_file(store, evidence)
    predecessor = proof.get("predecessor_plan_id")
    previous = connection.execute("SELECT * FROM runs WHERE id=?", (predecessor,)).fetchone()
    if not previous or predecessor == plan["plan_id"] or proof.get("successor_plan_id") != plan["plan_id"]:
        raise WorkflowError("successor review must bind distinct existing and new plans")
    if proof.get("predecessor_frozen_sha256") != hashlib.sha256(previous["plan"].encode("utf-8")).hexdigest() or (
        proof.get("successor_frozen_sha256") != canonical_sha(plan)
    ):
        raise WorkflowError("successor review frozen plan SHA changed")
    if connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true" or any(
        connection.execute(query).fetchone()
        for query in (
            "SELECT 1 FROM jobs WHERE state!='complete'",
            "SELECT 1 FROM actions WHERE state='issued'",
            "SELECT 1 FROM slots WHERE task_id IS NOT NULL",
            "SELECT 1 FROM locks",
            "SELECT 1 FROM owned WHERE state!='deleted'",
        )
    ):
        raise WorkflowError("successor import requires paused, drained models/actions/slots and verified cleanup")
    tasks = [dict(row) for row in connection.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (predecessor,))]
    if proof.get("predecessor_tasks_sha256") != canonical_sha(tasks):
        raise WorkflowError("predecessor task snapshot changed; independently review again")
    unresolved = [task for task in tasks if task["state"] != "complete"]
    if not unresolved or any(task["state"] != "blocked" for task in unresolved):
        raise WorkflowError("only drained blocked navigation tasks may be superseded")
    if connection.execute(
        "SELECT 1 FROM tasks WHERE run_id!=? AND state NOT IN ('complete','superseded')", (predecessor,)
    ).fetchone():
        raise WorkflowError("another incomplete plan cannot be silently superseded")
    scenes = sorted(scene["id"] for resource in plan["resources"] for scene in expected_scenes(resource))
    if proof.get("scene_ids") != scenes or len(scenes) != len(set(scenes)):
        raise WorkflowError("successor review must bind every exact scene")
    required = {scene["id"] for task in unresolved for scene in expected_scenes(json.loads(task["spec"]))}
    if not required <= set(scenes):
        raise WorkflowError("successor cannot drop unresolved predecessor scenes")
    selected_ids = {resource["id"] for resource in plan["resources"]}
    previous_ids = {task["resource_id"] for task in tasks}
    if not selected_ids <= previous_ids or not {task["resource_id"] for task in unresolved} <= selected_ids:
        raise WorkflowError("successor must preserve unresolved identities and not expand resource scope")
    # Human authorization binds scope, not a fabricated total spending allowance.
    human_path = contained(store.root / "inputs", str(proof.get("human_authorization", "")).removeprefix("inputs/"))
    human = read_json(human_path)
    if (
        digest(human_path) != proof.get("human_sha256")
        or human.get("approved") is not True
        or (
            human.get("plan_id") != plan["plan_id"]
            or not human.get("verbatim_user_message")
            or sorted(human.get("scene_ids", [])) != scenes
        )
    ):
        raise WorkflowError("successor needs original human scope authorization and exact SHA")
    for task in tasks:
        if task["resource_id"] not in selected_ids:
            continue
        spec = json.loads(task["spec"])
        if (
            not spec.get("retest_scope")
            or connection.execute("SELECT 1 FROM owned WHERE task_id=?", (task["id"],)).fetchone()
        ):
            raise WorkflowError("this handoff only supports navigation tasks without creation ownership")
    gate = store.root / "execution-gate.sqlite3"
    if gate.exists():
        jobs = {
            row[0]
            for task in tasks
            if task["resource_id"] in selected_ids
            for row in connection.execute("SELECT id FROM jobs WHERE task_id=?", (task["id"],))
        }
        with closing(sqlite3.connect(gate.as_uri() + "?mode=ro", uri=True)) as wire:
            if wire.execute("SELECT 1 FROM active WHERE phase IS NULL OR phase!='closed'").fetchone():
                raise WorkflowError("close original execution capabilities before handoff")
            for scope, state, kind in wire.execute(
                "SELECT a.scope,a.state,g.kind FROM attempts a LEFT JOIN grants g "
                "ON g.scope=a.scope AND g.id=a.grant_id"
            ):
                if scope.split(":", 1)[0] in jobs and (state != "succeeded" or kind not in {"identity", "read"}):
                    raise WorkflowError("successor cannot bypass unknown calls or replenish executed writes")
    sol = store.plan_sol_thread(connection, predecessor)
    if proof.get("reuse_sol_thread") != sol or not sol:
        raise WorkflowError("successor must explicitly preserve the existing unique Sol")
    # Migration is transactional and retains the original task state in the event.
    # Superseded is NOT complete/PASS, and history/spec/attempt/packet stay untouched.
    connection.execute("DROP INDEX one_resource_owner")
    connection.execute(
        "CREATE UNIQUE INDEX one_resource_owner ON tasks(resource_id) WHERE state NOT IN ('complete','superseded')"
    )
    for task in unresolved:
        connection.execute("UPDATE tasks SET state='superseded' WHERE id=?", (task["id"],))
    connection.execute("UPDATE runs SET state='superseded' WHERE id=?", (predecessor,))
    connection.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("sol_thread:" + plan["plan_id"], sol))
    store.event(
        connection,
        "plan_superseded",
        {
            "predecessor_plan_id": predecessor,
            "successor_plan_id": plan["plan_id"],
            "predecessor_frozen_sha256": proof["predecessor_frozen_sha256"],
            "successor_frozen_sha256": proof["successor_frozen_sha256"],
            "prior_task_states": {task["id"]: task["state"] for task in unresolved},
            "evidence": str(path.relative_to(store.root)),
            "evidence_sha256": digest(path),
            "scene_ids": scenes,
            "sol_thread": sol,
            "wire_unchanged": True,
            "history_preserved": True,
            "pass_added": False,
            "cloud_call": False,
        },
    )
