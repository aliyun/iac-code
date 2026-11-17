"""Restore retained creation authority for cleanup after an existing-sample query.

No reinstallation, synthetic creation, counter reset, or new write capability.
The two real scopes remain intact; only the occupied slot's active pointer moves.
"""
from __future__ import annotations

import json
import os
from typing import Any

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from . import existing_query, screenshot_review
from .files import WorkflowError, contained, digest, read_json
from .native_continuation import terminal


def manual_absence(root, existing, reports):
    """Verify actual external deletions and signed empty reads, not a summary."""
    if len(reports) != 3:
        raise WorkflowError("both actual manual deletions and independent absence required")
    binding = existing["binding"]
    expected_actions = {"flow": "DeleteDataFlow", "fileset": "DeleteFileset"}
    verified = []
    for index, entry in enumerate(reports):
        path = contained(root, entry["path"])
        if digest(path) != entry["sha256"]:
            raise WorkflowError("manual cleanup source changed")
        value = read_json(path)
        stage = ("flow", "fileset", "verify")[index]
        if (value.get("kind") != "ops_external_manual_children_cleanup_result" or value.get("stage") != stage
                or value.get("task_id") != existing["task_id"] or value.get("failure_type") is not None
                or value.get("account") != binding["account"] or value.get("region") != binding["region"]
                or value.get("profile") != "resource-link-test" or value.get("pass_added") is not False
                or value.get("origin") != "user_manual_console_creation_not_glm_wire"
                or any(value.get(k) != binding[k] for k in ("file_system_id", "dataflow_id", "fileset_id"))):
            raise WorkflowError("manual cleanup must retain exact external ownership and account")
        raws = []
        for event in value["events"]:
            raw_file = contained(root, event["path"])
            raw = read_json(raw_file)
            if (digest(raw_file) != event["sha256"] or raw.get("exit_code") != 0
                    or raw.get("call") != event["call"] or not raw.get("body", {}).get("RequestId")
                    or raw["body"].get("Code") or raw["body"]["RequestId"] != event["request_id"]):
                raise WorkflowError("actual successful manual cleanup response required")
            raws.append(raw)
        actions = [r["call"]["action"] for r in raws]
        expected = ["GetCallerIdentity", "DescribeDataFlows"]
        if stage != "flow":
            expected.append("DescribeFilesets")
        if stage != "verify":
            expected.append(expected_actions[stage])
        if actions != expected or raws[0]["body"].get("AccountId") != binding["account"]:
            raise WorkflowError("manual cleanup must have exact ordered operations and signed identity")
        for raw in raws[1:]:
            call = raw["call"]
            if (call.get("product") != "nas" or call.get("version") != "2017-06-26"
                    or call.get("region_id") != binding["region"]
                    or call.get("params", {}).get("FileSystemId") != binding["file_system_id"]):
                raise WorkflowError("manual cleanup parent/region/version changed")
            if call["action"] in {"DescribeDataFlows", "DescribeFilesets"}:
                flow = call["action"] == "DescribeDataFlows"
                params = {"FileSystemId": binding["file_system_id"], "MaxResults": 20 if flow else 10,
                          "Filters.1.Key": "DataFlowIds" if flow else "FsetIds",
                          "Filters.1.Value": binding["dataflow_id" if flow else "fileset_id"]}
                if call["params"] != params or raw["body"].get("NextToken"):
                    raise WorkflowError("manual absence must use exact bounded child filters")
                if stage == "verify" or (stage == "fileset" and flow):
                    rows = raw["body"].get("DataFlowInfo" if flow else "Entries", {}).get(
                        "DataFlow" if flow else "Entrie", [])
                    if rows != []:
                        raise WorkflowError("manual child still exists")
            else:
                key = "DataFlowId" if stage == "flow" else "FsetId"
                if call["params"].get(key) != binding["dataflow_id" if stage == "flow" else "fileset_id"]:
                    raise WorkflowError("manual delete target changed")
        if stage == "verify" and value.get("both_manual_children_absent") is not True:
            raise WorkflowError("manual child absence not confirmed")
        verified.append(entry)
    return verified


def retained_wire(db, proof):
    scope = proof["old_scope"]
    old = proof["retained_wire"]
    for table in ("grants", "attempts"):
        actual = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE scope=? ORDER BY rowid", (scope,))]
        if actual != old["tables"][table]:
            raise WorkflowError("original wire changed; never reinstall or reset it")
    limits = [dict(r) for r in db.execute(
        "SELECT * FROM authority_limits WHERE authority IN (SELECT authority FROM grants WHERE scope=?) "
        "ORDER BY authority,id", (scope,))]
    if limits != old["authority_limits"]:
        raise WorkflowError("original cumulative authority changed")
    return old["active"]


def propose(runtime: Any, task_id: str, visual: str, reports: list[dict]) -> dict:
    root = runtime.root
    path = contained(root / "inputs", visual)
    value = read_json(path)
    with runtime.store.transaction() as db:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("pause and drain before restoring retained cleanup")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        sol = db.execute("SELECT * FROM jobs WHERE task_id=? ORDER BY created DESC,rowid DESC", (task_id,)).fetchone()
        if (not task or task["state"] != "blocked" or not sol or sol["role"] != "sol"
                or sol["purpose"] != "review" or sol["id"] != value.get("job_id")):
            raise WorkflowError("exact original Sol terminal pixel handoff required")
        sol_terminal = terminal(root, sol)
        screenshot_review.validate(root, runtime.store, {
            "role": "sol", "purpose": "review", "job_id": sol["id"], "task_id": task_id,
            "resource": {"id": task["resource_id"]}, "attempt": task["attempt"], "native_cleanup_order": True,
        }, visual, path.read_text(encoding="utf-8"), connection=db)
        existing = existing_query.load(root, db, task_id)
        if not existing or task["slot"] != existing["slot"]:
            raise WorkflowError("reviewed existing query must retain original slot")
        sources = manual_absence(root, existing, reports)
        original_packet = contained(root, task["packet"])
        if digest(original_packet) != existing["original_packet"]["sha256"]:
            raise WorkflowError("original packet changed")
    with gate.database(root / "execution-gate.sqlite3") as db:
        prior = retained_wire(db, existing)
        active = db.execute("SELECT * FROM active WHERE slot=?", (str(existing["slot"]),)).fetchone()
        if (not active or active["scope"] == prior["scope"] or active["account"] != prior["account"]
                or active["phase"] != "execute"):
            raise WorkflowError("exact existing-query active pointer required")
        grants = [dict(r) for r in db.execute("SELECT * FROM grants WHERE scope=? ORDER BY id", (active["scope"],))]
        attempts = [dict(r) for r in db.execute("SELECT * FROM attempts WHERE scope=? ORDER BY rowid",
                                              (active["scope"],))]
        if (len(grants) != 2 or len(attempts) != 2 or {g["id"] for g in grants} != {"identity", "existing-source"}
                or any(g["kind"] not in {"identity", "read"} or g["used"] != 1 or g["quota"] != 1
                       or g["authority"] != "existing-query:" + existing["evidence_sha256"] for g in grants)
                or any(a["state"] != "succeeded" or a["session"] != active["session"] for a in attempts)):
            raise WorkflowError("completed pure existing query only; never detach a write or unknown outcome")
    return {"kind": "ops_existing_query_cleanup_restore_review", "approved": False, "task_id": task_id,
            "attempt": task["attempt"], "scope": prior["scope"], "session_id": prior["session"],
            "existing_review": {"path": existing["evidence"], "sha256": existing["evidence_sha256"]},
            "visual_review": {"path": path.relative_to(root).as_posix(), "sha256": digest(path)},
            "sol_terminal": sol_terminal, "external_cleanup": sources, "previous_active": dict(active),
            "query_grants": grants, "query_attempts": attempts, "restored_active": {**prior, "phase": "cleanup"},
            "new_creation_quota": 0, "cloud_calls": 0, "pass_added": False}


def restore(runtime: Any, evidence: str) -> dict:
    root = runtime.root
    path = contained(root / "inputs", evidence)
    proof = read_json(path)
    visual = contained(root, proof["visual_review"]["path"]).relative_to(root / "inputs").as_posix()
    if (path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077)
            or proof != {**propose(runtime, proof["task_id"], visual, proof["external_cleanup"]), "approved": True}):
        raise WorkflowError("exact independently approved original-cleanup restoration required")
    with runtime.store.transaction() as db, gate.database(root / "execution-gate.sqlite3") as wire:
        existing = existing_query.load(root, db, proof["task_id"])
        retained_wire(wire, existing)
        active = wire.execute("SELECT * FROM active WHERE slot=?", (proof["previous_active"]["slot"],)).fetchone()
        if not active or dict(active) != proof["previous_active"]:
            raise WorkflowError("active scope changed during cleanup restoration")
        key = "existing_cleanup:" + proof["task_id"]
        if db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
            raise WorkflowError("cleanup scope already restored; never replay")
        active = proof["restored_active"]
        wire.execute("UPDATE active SET scope=?,session=?,account=?,expires=?,phase='cleanup' WHERE slot=?",
                     (active["scope"], active["session"], active["account"], active["expires"], active["slot"]))
        record = {"evidence": path.relative_to(root).as_posix(), "sha256": digest(path)}
        db.execute("INSERT INTO meta VALUES(?,?)", (key, json.dumps(record)))
        runtime.store.event(db, "ops_existing_cleanup_restored", {**record, "task_id": proof["task_id"],
                                                                 "new_creation_quota": 0, "pass_added": False})
    return {"restored_cleanup_only": proof["scope"], "cloud_calls": 0, "new_creation_quota": 0}


def restored(root, db, task_id, scope):
    row = db.execute("SELECT value FROM meta WHERE key=?", ("existing_cleanup:" + task_id,)).fetchone()
    if not row:
        return False
    record = json.loads(row[0])
    path = contained(root, record["evidence"])
    value = read_json(path)
    return (digest(path) == record["sha256"] and value.get("approved") is True
            and value.get("kind") == "ops_existing_query_cleanup_restore_review"
            and value.get("scope") == scope and value.get("task_id") == task_id
            and value.get("new_creation_quota") == 0 and value.get("pass_added") is False)
