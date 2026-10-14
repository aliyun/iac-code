"""Reviewed retry of a terminal transport failure, retaining the original NAS generation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from . import native_continuation, phases
from .files import WorkflowError, contained, digest, read_json
from .native_identity_recovery import _read_continuation_turn

LOADED_SHA = digest(Path(__file__))


def load(root: Path, connection: Any, task_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT value FROM meta WHERE key=?", ("native_script_retry:" + task_id,)).fetchone()
    if not row:
        return None
    record = json.loads(row[0])
    if not isinstance(record, dict) or not all(k in record for k in (
        "evidence", "evidence_sha256", "original_job_id",
    )):
        raise WorkflowError("native script retry registration is malformed")
    path = contained(root, record["evidence"])
    proof = read_json(path)
    if (path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077)
            or digest(path) != record["evidence_sha256"]
            or proof.get("kind") != "ops_native_script_retry_review" or proof.get("approved") is not True
            or proof.get("task_id") != task_id or proof.get("cloud_quota_added") != 0
            or proof.get("original_job_id") != record["original_job_id"]):
        raise WorkflowError("native script retry review changed")
    previous = connection.execute("SELECT * FROM jobs WHERE id=?", (proof["failed_job_id"],)).fetchone()
    if (not previous or previous["state"] != "complete" or previous["thread_id"] != proof["thread_id"]
            or previous["turn_id"] != proof["failed_turn_id"]):
        raise WorkflowError("native script retry terminal binding changed")
    terminal = proof["failed_terminal"]
    for key, sha in (("path", "sha256"), ("transport_path", "transport_sha256")):
        if digest(contained(root, terminal[key])) != terminal[sha]:
            raise WorkflowError("native script retry terminal evidence changed")
    for entry in (proof["original_review"], proof["repair"]):
        if digest(contained(root, entry["path"])) != entry["sha256"]:
            raise WorkflowError("native script retry bound review changed")
    previous_retry = proof.get("previous_retry")
    visited = {record["evidence"]}
    current = proof
    while previous_retry:
        previous_path = contained(root, previous_retry["path"])
        if previous_retry["path"] in visited or digest(previous_path) != previous_retry["sha256"]:
            raise WorkflowError("native retry immutable history changed")
        visited.add(previous_retry["path"])
        previous = read_json(previous_path)
        if (previous.get("approved") is not True or previous.get("task_id") != task_id
                or previous.get("original_job_id") != proof["original_job_id"]
                or previous.get("cloud_quota_added") != 0
                or previous.get("attempt") != current["attempt"] - 1
                or previous.get("previous_continuation_count") != current["previous_continuation_count"] - 1):
            raise WorkflowError("native retry must preserve the exact preceding review")
        current, previous_retry = previous, previous.get("previous_retry")
    return proof


def resolved_wire(connection: Any, rows: list[Any]) -> bool:
    """Keep actual negative read rows unknown; never admit an unknown write."""
    for row in rows:
        if row["state"] == "succeeded":
            continue
        joined = connection.execute(
            "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param "
            "FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?", (row["id"],),
        ).fetchone()
        if not joined or not (phases.known_original_resolved_read(connection, joined)
                              or rejected_dry_run_cleanup(connection, joined)):
            return False
    return True


def rejected_dry_run_cleanup(connection: Any, row: Any) -> bool:
    """A signed DryRun rejection is not an unknown creation or a successful precheck.

    Permit cleanup of the already-created parents only. Keep the original error
    and consumed precheck quota, and never admit source/creation continuation.
    """
    if row["kind"] != "precheck" or row["grant_id"] != "precheck-dataflow" or row["state"] != "unknown":
        return False
    try:
        active = connection.execute("SELECT * FROM active WHERE scope=? AND session=?",
                                    (row["scope"], row["session"])).fetchone()
        if (not active or active["phase"] != "cleanup" or connection.execute(
                "SELECT 1 FROM attempts WHERE scope=? AND grant_id='create-dataflow'", (row["scope"],)).fetchone()):
            return False
        signer = gate.nas_native_signer(connection, active)
        original, call, raw = gate.nas_native_attempt(connection, active, row["grant_id"], signer, allow_error=True)
        body = raw.get("body") or {}
        return bool(original["id"] == row["id"] and call["product"] == "nas"
                    and call["action"] == "CreateDataFlow" and call["version"] == "2017-06-26"
                    and call["params"].get("DryRun") is True and raw.get("status") == 403
                    and body.get("Code") == "OperationDenied.FsetAlreadyInUse" and body.get("RequestId")
                    and raw.get("headers", {}).get("x-acs-request-id") == body["RequestId"]
                    and body.get("HostId") == "nas." + call["region_id"] + ".aliyuncs.com")
    except (gate.GateDeniedError, KeyError, TypeError, ValueError, AttributeError):
        return False


def _unexecuted_gap(root: Path, db: Any, failed: Any, task: Any, session: str) -> dict[str, Any]:
    """A fresh product repair may recover an explicitly unclicked diagnostic, not a PASS."""
    terminal = native_continuation.terminal(root, failed)
    trace = contained(root, terminal["transport_path"])
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [e["params"] for e in events if e.get("method") == "script/request"]
    if (not any(r.get("operation") == "submit" for r in requests)
            or any(r.get(key) != failed[column] for r in requests for key, column in (
                ("jobId", "id"), ("threadId", "thread_id"), ("turnId", "turn_id")))
            or any(r.get("operation") in {"click", "cleanup", "cleanup-source", "new-session", "request"}
                   or (r.get("operation") == "continue-execution"
                       and r.get("arguments", {}).get("stage") == "cleanup") for r in requests)
            or any(e.get("method") == "script/rejected" and not (
                e.get("params", {}).get("operation") == "continue-execution"
                and e.get("params", {}).get("error") == "native continuation stage must be identity/source/cleanup"
            ) for e in events)):
        raise WorkflowError("product retry requires a submitted gap with no actual/unknown click or cleanup")
    observations = list(db.execute("SELECT * FROM observations WHERE task_id=? AND attempt=? ORDER BY scene",
                                   (task["id"], task["attempt"])))
    evidence = []
    for row in observations:
        data = json.loads(row["data"])
        image = contained(root, data["screenshot"])
        raw = root / "jobs" / failed["id"] / "output" / (row["scene"] + ".json")
        if (data.get("actual_ui_click") is not False or data.get("session_id") != session
                or not image.is_relative_to(root / "jobs" / failed["id"] / "output")
                or not image.is_file() or not raw.is_file() or read_json(raw) != data):
            raise WorkflowError("only original unchanged explicitly unclicked diagnostic observations may retry")
        evidence.append({"scene": row["scene"], "json": str(raw.relative_to(root)), "json_sha256": digest(raw),
                         "screenshot": data["screenshot"], "sha256": digest(image)})
    if not evidence:
        raise WorkflowError("product retry requires actual unclicked diagnostic artifacts")
    return {"kind": "repaired_execution_gap", "observations": evidence}


def inspect(runtime: Any, failed_job_id: str, repair_evidence: str) -> dict[str, Any]:
    root = runtime.root
    with runtime.store.transaction() as db:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("native script retry requires paused and drained maintenance")
        failed = db.execute("SELECT * FROM jobs WHERE id=?", (failed_job_id,)).fetchone()
        if (not failed or failed["role"] != "glm" or failed["purpose"] != "continuation"
                or failed["state"] != "complete" or failed["submission"] not in {"confirmed", "result_staged"}):
            raise WorkflowError("exact terminal failed continuation required")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (failed["task_id"],)).fetchone()
        recovery = native_continuation.load(root, db, task["id"])
        original_path = contained(root, recovery["evidence"])
        original = read_json(original_path)
        continuations = list(db.execute(
            "SELECT * FROM jobs WHERE task_id=? AND purpose='continuation' ORDER BY created,rowid", (task["id"],),
        ))
        previous_retry = load(root, db, task["id"])
        product_gap = previous_retry is not None
        slot = db.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
        if (task["state"] != "blocked" or not continuations or continuations[-1]["id"] != failed_job_id
                or task["attempt"] != original["attempt"] + len(continuations)
                or (not product_gap and len(continuations) != 1)
                or (product_gap and (previous_retry["failed_job_id"] == failed_job_id
                    or previous_retry["previous_continuation_count"] != len(continuations) - 1
                    or previous_retry["attempt"] != task["attempt"] - 1))
                or not slot or slot["task_id"] != task["id"]
                or any(slot[k] != original[k] for k in ("target", "origin"))
                or failed["thread_id"] != original["thread_id"]
                or task["packet"] != original["packet"]["path"]
                or digest(contained(root, task["packet"])) != original["packet"]["sha256"]):
            raise WorkflowError("retry must retain the exact failed original generation")
        owners = [dict(r) for r in db.execute(
            "SELECT * FROM owned WHERE task_id=? AND state!='deleted' ORDER BY id", (task["id"],),
        )]
        if (any(owner not in owners for owner in original["ownership"])
                or (not product_gap and owners != original["ownership"])):
            raise WorkflowError("retry cannot replace or discard original ownership")
        if product_gap:
            transport_failure = _unexecuted_gap(root, db, failed, task, recovery["session_id"])
            records = gate.creations(root / "execution-gate.sqlite3", [recovery["original_job_id"]])
            for owner in owners:
                if owner in original["ownership"]:
                    continue
                binding = json.loads(owner["evidence"])
                if not isinstance(binding, dict) or not all(k in binding for k in ("path", "sha256")):
                    raise WorkflowError("additional retained owner requires immutable creation evidence")
                source = contained(root, binding["path"])
                matches = [r for r in records if r["state"] == "succeeded" and r["dimension"] == "QUERY"
                           and r["resource"] == json.loads(owner["resource"])
                           and r["session_id"] == recovery["session_id"]]
                if (owner["state"] != "present" or owner["dimension"] != "QUERY"
                        or not source.is_relative_to(root / "jobs" / recovery["original_job_id"] / "output")
                        or digest(source) != binding["sha256"] or len(matches) != 1
                        or read_json(source) != matches[0]):
                    raise WorkflowError("additional retained owner must be its exact original signed creation")
        else:
            if db.execute("SELECT 1 FROM observations WHERE task_id=? AND attempt=?",
                          (task["id"], task["attempt"])).fetchone():
                raise WorkflowError("transport retry cannot replay an existing observation")
            transport_failure, _ = _read_continuation_turn(root, failed)
        repair_path = contained(root / "inputs", repair_evidence)
        repair = read_json(repair_path)
        repair_job = db.execute("SELECT * FROM jobs WHERE id=?", (repair.get("job_id"),)).fetchone()
        changes = repair.get("changes", repair.get("files"))
        if (not repair_job or repair_job["task_id"] != task["id"] or repair_job["purpose"] != "repair"
                or repair_job["state"] != "complete" or repair.get("task_id") != task["id"]
                or repair.get("formal_test") is not False or repair.get("cloud_calls") != 0
                or repair.get("pass_added") is not False or not changes):
            raise WorkflowError("independent completed same-generation product repair required")
        if product_gap:
            latest_sol = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='sol' "
                                    "ORDER BY created DESC,rowid DESC", (task["id"],)).fetchone()
            if (latest_sol["id"] != repair_job["id"] or repair_job["created"] <= failed["created"]
                    or repair.get("repair_request", {}).get("path") is None):
                raise WorkflowError("fresh latest Sol product repair must bind this exact execution gap")
            diagnosis = repair["repair_request"]
            diagnosis_path = contained(root, diagnosis["path"])
            diagnosis_raw = read_json(diagnosis_path)
            if (digest(diagnosis_path) != diagnosis["sha256"]
                    or diagnosis_raw.get("kind") != "ops_preserved_product_gap"
                    or diagnosis_raw.get("original_job") != {
                        k: failed[k] for k in ("id", "thread_id", "turn_id")}):
                raise WorkflowError("completed repair does not diagnose the exact failed continuation")
        for change in changes:
            if digest(contained(Path(runtime.config["repo"]), change["path"])) != change["sha256"]:
                raise WorkflowError("native retry product repair changed")
        proof = {
            "schema_version": 1, "kind": "ops_native_script_retry_review", "approved": False,
            "task_id": task["id"], "attempt": task["attempt"], "previous_continuation_count": len(continuations),
            "original_job_id": recovery["original_job_id"], "thread_id": failed["thread_id"],
            "failed_job_id": failed_job_id, "failed_turn_id": failed["turn_id"],
            "original_review": {"path": recovery["evidence"], "sha256": digest(original_path)},
            "failed_terminal": native_continuation.terminal(root, failed), "transport_failure": transport_failure,
            "repair": {"path": str(repair_path.relative_to(root)), "sha256": digest(repair_path)},
            "repair_terminal": native_continuation.terminal(root, repair_job),
            "slot": slot["id"], "target": slot["target"], "origin": slot["origin"],
            "scope": recovery["scope"], "session": recovery["session_id"], "account": original["account"],
            "ownership": owners, "cloud_quota_added": 0, "pass_added": False,
        }
        if product_gap:
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key=?",
                                         ("native_script_retry:" + task["id"],)).fetchone()[0])
            proof["previous_retry"] = {"path": saved["evidence"], "sha256": saved["evidence_sha256"]}
    snapshot = runtime.browser({"operation": "snapshot", "origin": proof["origin"], "target": proof["target"]})
    if (snapshot.get("session_id") != proof["session"] or snapshot.get("busy") is not False
            or snapshot.get("unlocked") is not True):
        raise WorkflowError("retry requires the original unlocked idle full Web session")
    with gate.database(root / "execution-gate.sqlite3") as wire:
        active = wire.execute("SELECT * FROM active WHERE scope=?", (proof["scope"],)).fetchone()
        rows = wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,id", (proof["scope"],)).fetchall()
        try:
            lineage = gate.nas_native_continuation(wire, active) if active else None
        except gate.GateDeniedError as error:
            raise WorkflowError("native retry signed lineage changed") from error
        if (not active or active["phase"] != "execute" or active["session"] != proof["session"]
                or active["account"] != proof["account"] or not rows
                or not resolved_wire(wire, rows)
                or wire.execute("SELECT 1 FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                                "WHERE a.scope=? AND g.kind='delete'", (proof["scope"],)).fetchone()
                or not lineage):
            raise WorkflowError("retry requires intact signed original wire with no unknown calls or deletion")
        proof["wire_attempts"] = [{"id": row["id"], "sha256": gate.nas_native_snapshot(row)} for row in rows]
        proof["wire_grants"] = [dict(row) for row in wire.execute(
            "SELECT * FROM grants WHERE scope=? ORDER BY id", (proof["scope"],),
        )]
    return proof


def queue(runtime: Any, failed_job_id: str, evidence: str) -> dict[str, Any]:
    path = contained(runtime.root / "inputs", evidence)
    proof = read_json(path)
    repair_path = contained(runtime.root, proof["repair"]["path"])
    repair_relative = str(repair_path.relative_to(runtime.root / "inputs"))
    if (path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077)
            or proof.get("approved") is not True
            or {**proof, "approved": False} != inspect(runtime, failed_job_id, repair_relative)):
        raise WorkflowError("independent exact native script retry approval required")
    with runtime.store.transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (proof["task_id"],)).fetchone()
        if (task["state"] != "blocked" or task["attempt"] != proof["attempt"]
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"):
            raise WorkflowError("native script retry changed before registration")
        record = {"original_job_id": proof["original_job_id"], "evidence": str(path.relative_to(runtime.root)),
                  "evidence_sha256": digest(path)}
        if "previous_retry" in proof:
            previous = db.execute("SELECT value FROM meta WHERE key=?",
                                  ("native_script_retry:" + proof["task_id"],)).fetchone()[0]
            db.execute("INSERT INTO meta VALUES(?,?)",
                       ("native_script_retry_history:" + proof["task_id"] + ":" + proof["failed_job_id"], previous))
        db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ("native_script_retry:" + proof["task_id"], json.dumps(record)))
        db.execute("UPDATE tasks SET state='continuation_pending',reason=NULL WHERE id=?", (proof["task_id"],))
        runtime.store.event(db, "ops_native_script_retry_queued", {"task_id": proof["task_id"], **record,
                                                                 "cloud_quota_added": 0, "pass_added": False})
    return {"continuation_queued": proof["task_id"], "no_glm_dispatched": True, "cloud_quota_added": 0}


def inspect_cleanup(runtime: Any, source_job_id: str, screenshot_review: str) -> dict[str, Any]:
    """No quota or call: prove original Sol inspected the missing click before cleanup."""
    root = runtime.root
    path = contained(root / "inputs", screenshot_review)
    visual = read_json(path)
    with runtime.store.transaction() as db:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("native cleanup requires paused and drained review")
        source = db.execute("SELECT * FROM jobs WHERE id=?", (source_job_id,)).fetchone()
        if (not source or source["role"] != "glm" or source["purpose"] != "continuation"
                or source["state"] != "complete"):
            raise WorkflowError("exact terminal native source/click job required")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (source["task_id"],)).fetchone()
        recovery = native_continuation.load(root, db, task["id"])
        retry = load(root, db, task["id"])
        original = read_json(contained(root, recovery["evidence"]))
        latest = db.execute("SELECT * FROM jobs WHERE task_id=? ORDER BY created DESC,rowid DESC",
                            (task["id"],)).fetchone()
        latest_glm = db.execute("SELECT id FROM jobs WHERE task_id=? AND role='glm' ORDER BY created DESC,rowid DESC",
                                (task["id"],)).fetchone()
        if (not retry or task["state"] != "blocked" or task["slot"] != original["slot"]
                or latest["role"] != "sol" or latest["purpose"] != "review" or latest["state"] != "complete"
                or latest_glm[0] != source_job_id
                or visual.get("kind") != "native_screenshot_review" or visual.get("navigation_verified") is not True
                or visual.get("task_id") != task["id"] or visual.get("job_id") != latest["id"]
                or visual.get("source_job_id") != source_job_id
                or digest(contained(root, task["packet"])) != original["packet"]["sha256"]):
            raise WorkflowError("original Sol exact missing-scene screenshot review required")
        observations = list(db.execute("SELECT * FROM observations WHERE task_id=? AND attempt=? ORDER BY scene",
                                       (task["id"], task["attempt"])))
        verified = visual.get("verified_scenes")
        expected = []
        for row in observations:
            data = json.loads(row["data"])
            image = contained(root, data["screenshot"])
            if (not data.get("actual_ui_click") or not image.is_relative_to(root / "jobs" / source_job_id / "output")
                    or not db.execute("SELECT 1 FROM inspected WHERE job_id=? AND artifact=? AND sha=?",
                                      (latest["id"], data["screenshot"], digest(image))).fetchone()):
                raise WorkflowError("each original click PNG needs unchanged independent Sol inspection")
            expected.append({"scene": row["scene"], "screenshot": data["screenshot"], "sha256": digest(image)})
        if not expected or verified != expected:
            raise WorkflowError("screenshot review must bind all and only current actual missing clicks")
        result = {"schema_version": 1, "kind": "ops_native_cleanup_review", "approved": False,
                  "task_id": task["id"], "attempt": task["attempt"], "source_job_id": source_job_id,
                  "original_job_id": recovery["original_job_id"], "dimension": "QUERY", "native": True,
                  "scope": recovery["scope"], "session_id": recovery["session_id"],
                  "authority": original["authority"], "slot": original["slot"], "target": original["target"],
                  "source_terminal": native_continuation.terminal(root, source),
                  "sol_terminal": native_continuation.terminal(root, latest),
                  "screenshot_review": {"path": str(path.relative_to(root)), "sha256": digest(path)},
                  "verified_scenes": expected, "cloud_quota_added": 0, "pass_added": False}
        from .existing_cleanup import restored

        restored_cleanup = restored(root, db, task["id"], recovery["scope"])
    with gate.database(root / "execution-gate.sqlite3") as wire:
        active = wire.execute("SELECT * FROM active WHERE scope=?", (result["scope"],)).fetchone()
        rows = wire.execute(
            "SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid", (result["scope"],),
        ).fetchall()
        if (not active or not (active["phase"] == "execute" or (restored_cleanup and active["phase"] == "cleanup"))
                or active["session"] != result["session_id"]
                or not resolved_wire(wire, rows)
                or wire.execute("SELECT 1 FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                                "WHERE a.scope=? AND g.kind='delete'", (result["scope"],)).fetchone()
                or not gate.nas_native_continuation(wire, active)):
            raise WorkflowError("intact original signed graph with no prior deletion required")
        result["wire_attempts"] = [{"id": row["id"], "sha256": gate.nas_native_snapshot(row)} for row in rows]
        result["wire_grants"] = [dict(row) for row in wire.execute(
            "SELECT * FROM grants WHERE scope=? ORDER BY id", (result["scope"],))]
    return result


def queue_cleanup(runtime: Any, source_job_id: str, evidence: str) -> dict[str, Any]:
    path = contained(runtime.root / "inputs", evidence)
    proof = read_json(path)
    visual = str(contained(runtime.root, proof["screenshot_review"]["path"]).relative_to(runtime.root / "inputs"))
    if (path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077)
            or proof != {**inspect_cleanup(runtime, source_job_id, visual), "approved": True}):
        raise WorkflowError("exact independent native cleanup review required")
    runtime.store.queue_cleanup(proof["task_id"], {
        "original_job_id": proof["original_job_id"], "dimension": "QUERY", "native": True,
        "source_job_id": source_job_id, "evidence": str(path.relative_to(runtime.root)),
        "evidence_sha256": digest(path), "authority": proof["authority"],
    })
    return {"cleanup_queued": proof["task_id"], "only_luna_dispatches": True, "cloud_quota_added": 0,
            "pass_added": False}
