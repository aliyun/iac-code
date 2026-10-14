"""OPS-reviewed continuation of one retained NAS generation; never reinstall a packet."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from .files import WorkflowError, contained, digest, read_json


def terminal(root: Path, job: Any, *, statuses=("completed",)) -> dict[str, str]:
    files = sorted((root / "jobs" / job["id"]).glob("ops-thread-read-*.json"))
    if not files:
        raise WorkflowError("fresh owning-backend terminal evidence required")
    path = files[-1]
    raw = read_json(path)
    turns = [t for t in raw.get("thread", {}).get("turns", []) if t.get("id") == job["turn_id"]]
    binding = raw.get("job_binding", {})
    if (raw.get("method") != "thread/read" or raw.get("thread", {}).get("id") != job["thread_id"]
            or len(turns) != 1 or turns[0].get("status") not in statuses
            or any(binding.get(key) != job[key] for key in
                   ("id", "task_id", "role", "state", "thread_id", "turn_id", "submission"))):
        raise WorkflowError("exact full terminal turn required; notification is not evidence")
    transport = raw.get("transport_evidence", {})
    transport_path = contained(root, transport.get("path", ""))
    if transport.get("state") != "present" or not transport_path.is_file():
        raise WorkflowError("owning backend transport trace required for exact terminal reconciliation")
    events = [json.loads(line) for line in transport_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    terminal_events = [event for event in events if event.get("method") == "turn/completed"
                       and event.get("params", {}).get("threadId") == job["thread_id"]
                       and event.get("params", {}).get("turn", {}).get("id") == job["turn_id"]
                       and event.get("params", {}).get("turn", {}).get("status") in statuses]
    if len(terminal_events) != 1:
        raise WorkflowError("exact owning-backend completed event required")
    return {"path": str(path.relative_to(root)), "sha256": digest(path),
            "transport_path": str(transport_path.relative_to(root)), "transport_sha256": digest(transport_path)}


def inspect(runtime, original_job: str, repair_evidence: str) -> dict[str, Any]:
    """Read-only proposal. The independent OPS review, not this projection, approves recovery."""
    root = runtime.root
    with runtime.store.transaction() as db:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("native continuation requires paused, drained maintenance")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (original_job,)).fetchone()
        if not job or job["role"] != "glm" or job["purpose"] != "dispatch" or job["state"] != "complete":
            raise WorkflowError("original completed dispatch required")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
        latest = db.execute("SELECT id FROM jobs WHERE task_id=? AND role='glm' ORDER BY created DESC,rowid DESC",
                            (task["id"],)).fetchone()
        slot = db.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
        if (task["state"] != "blocked" or not slot or slot["task_id"] != task["id"]
                or not slot["target"] or latest[0] != original_job):
            raise WorkflowError("retain original blocked task, occupied target and latest GLM")
        packet_path = contained(root, task["packet"])
        packet = read_json(packet_path)
        from .creation import execution_authority, validate_authorization
        from .temporary_samples import admitted

        validate_authorization(root, json.loads(task["spec"])["id"], packet)
        if set(packet["requests"]) != {"QUERY"} or "QUERY" not in admitted(root, db, task, packet):
            raise WorkflowError("only independently admitted original native NAS QUERY graph")
        gate.validate_nas_native_plan(packet["execution_grants"]["QUERY"])
        session_path = root / "jobs" / original_job / "output/self-created-session.json"
        session = read_json(session_path)
        if any(session.get(k) != v for k, v in (
            ("job_id", original_job), ("target", slot["target"]), ("origin", slot["origin"]),
        )):
            raise WorkflowError("original self-created full session/target/origin required")
        repair_path = contained(root / "inputs", repair_evidence)
        repair = read_json(repair_path)
        repair_job = db.execute("SELECT * FROM jobs WHERE id=?", (repair.get("job_id"),)).fetchone()
        if (repair.get("kind") != "product_repair_preserved_native_signer"
                or repair.get("task_id") != task["id"] or repair.get("formal_test") is not False
                or repair.get("cloud_calls") != 0 or repair.get("pass_added") is not False
                or not repair_job or repair_job["task_id"] != task["id"]
                or repair_job["purpose"] != "repair" or repair_job["state"] != "complete"):
            raise WorkflowError("independent completed preserved product repair required")
        for change in repair["changes"]:
            if digest(contained(Path(runtime.config["repo"]), change["path"])) != change["sha256"]:
                raise WorkflowError("product repair changed since independent handoff")
        for entry in packet["inputs"]:
            if digest(contained(root, entry["path"])) != entry["sha256"]:
                raise WorkflowError("original packet input changed")
        owners = [dict(r) for r in db.execute(
            "SELECT * FROM owned WHERE task_id=? AND state!='deleted' ORDER BY id", (task["id"],),
        )]
        if not owners:
            raise WorkflowError("retained original ownership required")
        for observation in db.execute("SELECT data FROM observations WHERE task_id=? AND attempt=?",
                                      (task["id"], task["attempt"])):
            if json.loads(observation[0]).get("actual_ui_click") is True:
                raise WorkflowError("continuation cannot replay an actual click")
        result = {
            "schema_version": 1, "kind": "ops_native_signer_continuation_review", "approved": False,
            "task_id": task["id"], "original_job_id": original_job, "attempt": task["attempt"],
            "thread_id": job["thread_id"], "original_turn_id": job["turn_id"], "slot": slot["id"],
            "target": slot["target"], "origin": slot["origin"], "session": session["session_id"],
            "scope": original_job + ":QUERY", "authority": execution_authority(root, packet, "QUERY"),
            "account": next(iter(packet["scenes"].values()))["account"],
            "packet": {"path": task["packet"], "sha256": digest(packet_path)},
            "self_created_session": {"path": str(session_path.relative_to(root)), "sha256": digest(session_path)},
            "repair": {"path": str(repair_path.relative_to(root)), "sha256": digest(repair_path)},
            "original_terminal": terminal(root, job), "repair_terminal": terminal(root, repair_job),
            "ownership": owners, "additional_identity_reads": 1,
            "grant_id": "ops-native-identity-" + original_job[:12],
            "old_calls_refilled": False, "new_creation_quota": 0, "pass_added": False,
        }
    with sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True) as wire:
        wire.row_factory = sqlite3.Row
        active = wire.execute("SELECT * FROM active WHERE slot=?", (str(result["slot"]),)).fetchone()
        if (not active or any(active[k] != result[k] for k in ("scope", "session", "account"))
                or active["phase"] not in {"execute", "cleanup"}
                or (active["expires"] is not None and active["expires"] <= time.time())):
            raise WorkflowError("original active authority/session expired or changed")
        grants = wire.execute("SELECT * FROM grants WHERE scope=? ORDER BY id", (result["scope"],)).fetchall()
        rows = wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid",
                            (result["scope"],)).fetchall()
        if (len(grants) != len(packet["execution_grants"]["QUERY"]) or not rows
                or any(r["state"] != "succeeded" or r["session"] != result["session"] for r in rows)
                or any(r["grant_id"].startswith("delete-") for r in rows)
                or wire.execute("SELECT 1 FROM cleanup_identity_reviews WHERE scope=?", (result["scope"],)).fetchone()):
            raise WorkflowError("unknown/failed/deleted/already-renewed original generation cannot restart")
        identity = next((r for r in rows if r["grant_id"] == "identity"), None)
        if (not identity or not identity["tool_use_id"] or not identity["credential"]
                or not identity["reference_scope"] or any(r["credential"] != identity["credential"]
                or r["reference_scope"] != identity["reference_scope"] for r in rows)):
            raise WorkflowError("one immutable original signer lineage required")
        root_grant = next(g for g in grants if g["id"] == "identity")
        if (not gate.nas_native_success(json.loads(root_grant["call"]), json.loads(identity["response"]), "identity")
                or json.loads(identity["response"])["body"].get("AccountId") != result["account"]):
            raise WorkflowError("original real signed account proof required")
        result.update(original_phase=active["phase"], original_identity_attempt=identity["id"],
                      identity_call=json.loads(root_grant["call"]), preserved_grants=[],
                      preserved_attempts=[{"id": r["id"], "sha256": gate.nas_native_snapshot(r)} for r in rows])
        for grant in grants:
            approved = next((g for g in packet["execution_grants"]["QUERY"] if g["id"] == grant["id"]), None)
            if approved is None or any(grant[key] != value for key, value in {
                "kind": approved["kind"], "quota": approved["quota"],
                "call": gate.canonical(gate.api_call(approved["call"])),
                "parent": approved.get("parent"), "response_path": gate.canonical(approved.get("response_path")),
                "target_param": approved.get("target_param"), "target_encoding": approved.get("target_encoding"),
                "read_policy": gate.canonical(approved["read_policy"]) if "read_policy" in approved else None,
            }.items()):
                raise WorkflowError("installed original grant differs from approved packet")
            limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                 (grant["authority"], grant["id"])).fetchone()
            if (grant["authority"] != result["authority"] or not limit or limit[0] != grant["used"]
                    or not 0 <= grant["used"] <= grant["quota"]):
                raise WorkflowError("original cumulative authority counters must be unchanged")
            result["preserved_grants"].append({"id": grant["id"], "used": grant["used"],
                "definition_sha256": gate.nas_native_snapshot({k: v for k, v in dict(grant).items() if k != "used"})})
    return result


def load(root: Path, db, task_id: str) -> dict[str, Any]:
    saved = db.execute("SELECT value FROM meta WHERE key=?", ("native_continuation:" + task_id,)).fetchone()
    if not saved:
        raise WorkflowError("missing independent native continuation review")
    recovery = json.loads(saved[0])
    path = contained(root, recovery["evidence"])
    if digest(path) != recovery["evidence_sha256"] or read_json(path).get("approved") is not True:
        raise WorkflowError("native continuation review changed")
    return recovery


def inspect_managed(runtime, original_job: str, *, renew_identity: bool = False,
                    capture_only: bool = False) -> dict[str, Any]:
    """Recover unused reads of an already-created MSE stack, with no new grant.

    Optional reviewed identity renewal never replaces an old signer record or
    replenishes resource calls; the new signer must actually prove the same account.
    """
    from .creation import execution_authority, validate_authorization
    from .phases import assert_stack_source_ready
    from .temporary_samples import admitted

    root = runtime.root
    with runtime.store.transaction() as db:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("managed continuation requires paused, drained maintenance")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (original_job,)).fetchone()
        if not job or job["role"] != "glm" or job["purpose"] != "dispatch" or job["state"] != "complete":
            raise WorkflowError("original completed dispatch required")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
        spec = json.loads(task["spec"])
        slot = db.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
        latest = db.execute("SELECT id FROM jobs WHERE task_id=? AND role='glm' ORDER BY rowid DESC LIMIT 1",
                            (task["id"],)).fetchone()
        review = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='sol' ORDER BY rowid DESC LIMIT 1",
                            (task["id"],)).fetchone()
        prior = db.execute("SELECT value FROM meta WHERE key=?", ("native_continuation:" + task["id"],)).fetchone()
        previous = json.loads(prior[0]) if prior else None
        failed = None
        if latest[0] != original_job and previous and previous.get("kind") == "managed_read_continuation":
            failed = db.execute("SELECT * FROM jobs WHERE id=?", (latest[0],)).fetchone()
            if (previous["original_job_id"] != original_job or failed["purpose"] != "continuation"
                    or failed["state"] != "complete" or failed["thread_id"] != job["thread_id"]):
                raise WorkflowError("managed retry must retain same original terminal GLM")
        capacity_terminal = None
        if review and review["submission"] != "result_staged":
            # A capacity rejection may interrupt read-only inspection. It is not
            # acceptance. Keep it failed and retain the earlier completed review.
            capacity_terminal = terminal(root, review, statuses=("failed",))
            raw = read_json(contained(root, capacity_terminal["path"]))
            turn = next(t for t in raw["thread"]["turns"] if t["id"] == review["turn_id"])
            trace = contained(root, capacity_terminal["transport_path"])
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
            requests = [e.get("params", {}) for e in events if e.get("method") == "script/request"]
            commands = [i for i in turn.get("items", []) if i["type"] == "commandExecution"]
            if (turn.get("error", {}).get("codexErrorInfo") != "serverOverloaded"
                    or any(r.get("operation") not in {"context", "read", "read_artifact", "inspect",
                                                       "transport_evidence"} for r in requests)
                    or any("workflow-agent.py" not in i.get("command", "") or i.get("exitCode") != 0
                           for i in commands)
                    or len(commands) != len(requests)):
                raise WorkflowError("unreviewed model failure is not an independent review")
            review = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='sol' AND purpose='review' "
                                "AND submission='result_staged' ORDER BY rowid DESC LIMIT 1", (task["id"],)).fetchone()
        if (renew_identity and capture_only):
            raise WorkflowError("capture-only cannot append identity or resource grants")
        if (spec.get("key") != "ALIYUN::MSE::Cluster"
                or task["state"] not in ({"blocked", "reported"} if capture_only else {"blocked"})
                or not slot or slot["task_id"] != task["id"] or not slot["target"]
                or (latest[0] != original_job and not failed) or not review or review["purpose"] != "review"
                or review["state"] != "complete"):
            raise WorkflowError("retained MSE generation and independent terminal Sol review required")
        packet_path = contained(root, task["packet"])
        packet = read_json(packet_path)
        validate_authorization(root, task["resource_id"], packet)
        from .plans import expected_scenes

        scenes = expected_scenes(spec)
        if (set(packet["requests"]) != {"TF"} or "TF" not in admitted(root, db, task, packet)
                or not scenes or any(s["mode"] != "open" or s["dimension"] != "TF" for s in scenes)):
            raise WorkflowError("only original admitted hosted Terraform missing navigation")
        if (set(packet["execution_grants"]) != {"TF"}
                or set(packet["scenes"]) != {s["id"] for s in scenes}):
            raise WorkflowError("exact original hosted Terraform packet required")
        for entry in packet["inputs"]:
            if digest(contained(root, entry["path"])) != entry["sha256"]:
                raise WorkflowError("original packet input changed")
        output = root / "jobs" / original_job / "output"
        session_path = output / "self-created-session.json"
        session = read_json(session_path)
        intent = read_json(output / "new-session-intent.json")
        if (any(session.get(k) != v for k, v in (("job_id", original_job), ("target", slot["target"]),
                                                ("origin", slot["origin"])))
                or not session.get("session_id") or session["session_id"] == intent.get("previous_session")):
            raise WorkflowError("original self-created full session binding required")
        if any(json.loads(r[0]).get("actual_ui_click") is True for r in db.execute(
                "SELECT data FROM observations WHERE task_id=?", (task["id"],))):
            raise WorkflowError("managed continuation cannot replay an actual click")
        owners = [dict(r) for r in db.execute(
            "SELECT * FROM owned WHERE task_id=? AND state!='deleted' ORDER BY id", (task["id"],))]
        if not owners:
            raise WorkflowError("retained original ownership required")
        result = {"schema_version": 1, "kind": "ops_managed_read_continuation_review", "approved": False,
                  "task_id": task["id"], "original_job_id": original_job, "attempt": task["attempt"],
                  "thread_id": job["thread_id"], "original_turn_id": job["turn_id"], "slot": slot["id"],
                  "target": slot["target"], "origin": slot["origin"], "session": session["session_id"],
                  "scope": original_job + ":TF", "authority": execution_authority(root, packet, "TF"),
                  "account": next(iter(packet["scenes"].values()))["account"],
                  "packet": {"path": task["packet"], "sha256": digest(packet_path)},
                  "self_created_session": {"path": str(session_path.relative_to(root)),
                                           "sha256": digest(session_path)},
                  "original_terminal": terminal(root, job), "review_terminal": terminal(root, review),
                  "ownership": owners, "additional_identity_reads": 0, "new_creation_quota": 0,
                  "old_calls_refilled": False, "pass_added": False,
                  "original_artifacts": [{"path": str(p.relative_to(root)), "sha256": digest(p)}
                                         for p in sorted(output.glob("*.json"))]}
        if failed:
            previous_path = contained(root, previous["evidence"])
            if digest(previous_path) != previous["evidence_sha256"]:
                raise WorkflowError("original managed review changed")
            result.update(previous_review={"path": previous["evidence"], "sha256": digest(previous_path)},
                          failed_job_id=failed["id"], failed_terminal=terminal(root, failed),
                          previous_continuation_count=db.execute(
                              "SELECT count(*) FROM jobs WHERE task_id=? AND purpose='continuation'", (task["id"],),
                          ).fetchone()[0], capacity_failure=capacity_terminal,
                          failed_artifacts=[{"path": str(p.relative_to(root)), "sha256": digest(p)}
                                            for p in sorted((root / "jobs" / failed["id"] / "output").glob("*.json"))])
        if capture_only:
            if not failed or failed["submission"] != "result_staged":
                raise WorkflowError("capture-only needs latest completed original-thread continuation")
            source = root / "jobs" / failed["id"] / "output/TF-product-source.json"
            value = read_json(source)
            if value.get("session_id") != result["session"]:
                raise WorkflowError("capture-only must preserve the latest full-session source")
            result.update(capture_only=True, capture_source={"path": str(source.relative_to(root)),
                                                           "sha256": digest(source)})
    path = root / "execution-gate.sqlite3"
    with gate.database(path) as wire:
        active = wire.execute("SELECT * FROM active WHERE slot=?", (str(slot["id"]),)).fetchone()
        grants = wire.execute("SELECT * FROM grants WHERE scope=? ORDER BY id", (result["scope"],)).fetchall()
        attempts = wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid",
                                (result["scope"],)).fetchall()
        extensions = gate.cleanup_identity_extensions(wire, result["scope"]) if capture_only else []
        if (not active or any(active[k] != result[k] for k in ("scope", "session", "account"))
                or active["phase"] not in {"execute", "cleanup"}
                or (active["expires"] is not None and active["expires"] <= time.time())
                or not gate.mse_managed_scope(wire, result["scope"])
                or not attempts or any(a["state"] != "succeeded" or a["session"] != result["session"]
                                       for a in attempts)
                or any(g["kind"] == "delete" and g["used"] for g in grants)
                or len(grants) != len(packet["execution_grants"]["TF"]) + len(extensions)):
            raise WorkflowError("original successful undeleted MSE authority required")
        identity_ids = {g["id"] for g in grants if g["kind"] == "identity"}
        identities = [a for a in attempts if a["grant_id"] in identity_ids]
        if (not capture_only and (len(identities) != 1 or not identities[0]["credential"]
                or any(a["credential"] != identities[0]["credential"] for a in attempts))):
            raise WorkflowError("original signer must remain unchanged; no implicit renewal")
        creates = [g for g in grants if g["kind"] == "create"]
        if len(creates) != 1 or creates[0]["quota"] != 1 or creates[0]["used"] != 1:
            raise WorkflowError("one successful original creation, never new creation capacity")
        preserved = []
        gate.validate_grants(packet["execution_grants"]["TF"], require_read_policy=True)
        for g in grants:
            if capture_only and any(g["id"] == extension["id"] for extension in extensions):
                limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                     (g["authority"], g["id"])).fetchone()
                signed = [a for a in identities if a["grant_id"] == g["id"]]
                if (g["used"] != 1 or not limit or limit[0] != 1 or len(signed) != 1
                        or json.loads(signed[0]["response"]).get("AccountId") != result["account"]):
                    raise WorkflowError("capture-only requires already-consumed actual same-account identity")
                preserved.append(dict(g))
                continue
            approved = next((e for e in packet["execution_grants"]["TF"] if e["id"] == g["id"]), None)
            expected = {} if approved is None else {
                **approved, "call": gate.canonical(gate.api_call(approved["call"])),
                "response_path": gate.canonical(approved.get("response_path")),
                "read_policy": gate.canonical(approved["read_policy"]) if "read_policy" in approved else None,
            }
            if (approved is None or g["authority"] != result["authority"]
                    or any(g[k] != expected.get(k) for k in
                           ("kind", "call", "quota", "parent", "response_path", "target_param",
                            "target_encoding", "read_policy"))):
                raise WorkflowError("installed original grant differs from exact approved packet")
            limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                 (g["authority"], g["id"])).fetchone()
            if not limit or limit[0] != g["used"] or not 0 <= g["used"] <= g["quota"]:
                raise WorkflowError("original cumulative counters changed")
            preserved.append(dict(g))
        if not capture_only and not any(g["kind"] == "read"
                   and json.loads(g["read_policy"] or "{}").get("phase") == "execute"
                   and g["used"] < g["quota"] for g in grants):
            raise WorkflowError("no unused original source reads remain")
        result.update(original_phase=active["phase"], wire_sha256=gate.nas_native_snapshot({
            "grants": preserved, "attempts": [dict(a) for a in attempts]}))
        if renew_identity:
            if wire.execute("SELECT 1 FROM cleanup_identity_reviews WHERE scope=?", (result["scope"],)).fetchone():
                raise WorkflowError("original managed identity supplement already exists; no refill")
            root_create = next(a for a in attempts if a["grant_id"] == creates[0]["id"])
            identity_grant = next(g for g in grants if g["kind"] == "identity")
            result.update(renew_identity=True, additional_identity_reads=1,
                          identity_call=json.loads(identity_grant["call"]),
                          identity_grant_id="ops-cleanup-identity-" + gate.nas_native_snapshot({
                              "scope": result["scope"]})[:16],
                          root_creation={"attempt_id": root_create["id"], "grant_id": root_create["grant_id"],
                                         "response_sha256": gate.nas_native_snapshot(
                                             json.loads(root_create["response"]))})
        if failed and not capture_only and read_json(previous_path)["wire_sha256"] != result["wire_sha256"]:
            raise WorkflowError("unchanged-wire managed retry cannot conceal new or unknown calls")
    if capture_only:
        from .phases import assert_managed_source_reads_complete

        assert_managed_source_reads_complete(path, result["scope"])
        refs = runtime.proven_references(original_job, "TF", result["session"], result["account"],
                                        value.get("references", {}).get("items", []))
        owned_refs = runtime.created_references(original_job, "TF", result["session"], refs)
        if not any(ref.get("resourceType") in {scene["source_type"] for scene in scenes} for ref in owned_refs):
            raise WorkflowError("capture-only needs signed exact original-created hosted member references")
    assert_stack_source_ready(path, result["scope"], cleanup=False)
    snapshot = runtime.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
    if (snapshot.get("session_id") != result["session"] or snapshot.get("busy")
            or not snapshot.get("unlocked") or snapshot.get("preview_open")):
        raise WorkflowError("original unlocked idle MSE session required")
    return result


def queue_managed(runtime, original_job: str, evidence: str) -> dict[str, Any]:
    path = contained(runtime.root / "inputs", evidence)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise WorkflowError("owner-only independent managed continuation review required")
    proof = read_json(path)
    candidate = inspect_managed(runtime, original_job, renew_identity=proof.get("renew_identity") is True,
                                capture_only=proof.get("capture_only") is True)
    if proof != {**candidate, "approved": True}:
        raise WorkflowError("independent review must bind exact retained generation and wire snapshot")
    with runtime.store.transaction() as db, gate.database(runtime.root / "execution-gate.sqlite3") as wire:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
                or (db.execute("SELECT 1 FROM meta WHERE key=?",
                               ("native_continuation:" + proof["task_id"],)).fetchone()
                    and not proof.get("previous_review"))):
            raise WorkflowError("managed continuation already queued or model maintenance changed")
        grants = [dict(g) for g in wire.execute("SELECT * FROM grants WHERE scope=? ORDER BY id", (proof["scope"],))]
        attempts = [dict(a) for a in wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid",
                                                (proof["scope"],))]
        active = wire.execute("SELECT * FROM active WHERE slot=?", (str(proof["slot"]),)).fetchone()
        if (gate.nas_native_snapshot({"grants": grants, "attempts": attempts}) != proof["wire_sha256"]
                or not active or active["scope"] != proof["scope"] or active["session"] != proof["session"]
                or active["phase"] != proof["original_phase"]):
            raise WorkflowError("original MSE wire changed before registration")
        if proof.get("renew_identity"):
            if wire.execute("SELECT 1 FROM cleanup_identity_reviews WHERE scope=?", (proof["scope"],)).fetchone():
                raise WorkflowError("one-shot managed signer renewal already registered")
            wire.execute("INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,'identity',?,1,0,?)",
                         (proof["scope"], proof["identity_grant_id"], gate.canonical(proof["identity_call"]),
                          proof["authority"]))
            wire.execute("INSERT INTO authority_limits VALUES(?,?,0)",
                         (proof["authority"], proof["identity_grant_id"]))
            wire.execute("INSERT INTO cleanup_identity_reviews VALUES(?,?,?,?,?,?,?)", (
                proof["scope"], proof["identity_grant_id"], proof["session"], proof["account"], proof["authority"],
                str(path), digest(path),
            ))
        wire.execute("UPDATE active SET phase='execute' WHERE slot=? AND scope=? AND session=?",
                     (str(proof["slot"]), proof["scope"], proof["session"]))
        recovery = {"kind": "managed_read_continuation", "original_job_id": original_job, "dimension": "TF",
                    "scope": proof["scope"], "session_id": proof["session"],
                    "evidence": str(path.relative_to(runtime.root)), "evidence_sha256": digest(path)}
        if proof.get("renew_identity"):
            recovery["identity_grant_id"] = proof["identity_grant_id"]
        if proof.get("capture_only"):
            recovery["capture_source"] = proof["capture_source"]
        if proof.get("previous_review"):
            db.execute("INSERT INTO meta VALUES(?,?)", ("managed_continuation_history:" + proof["failed_job_id"],
                                                        json.dumps(proof["previous_review"])))
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
                   ("native_continuation:" + proof["task_id"], json.dumps(recovery)))
        db.execute("UPDATE tasks SET state='continuation_pending',reason=NULL WHERE id=?", (proof["task_id"],))
        runtime.store.event(db, "ops_managed_read_continuation_queued", recovery)
    return {"continuation_queued": proof["task_id"], "only_luna_dispatches": True,
            "new_creation_quota": 0, "additional_identity_reads": proof["additional_identity_reads"],
            "old_calls_refilled": False}


def queue(runtime, original_job: str, evidence: str) -> dict[str, Any]:
    root = runtime.root
    path = contained(root / "inputs", evidence)
    if (root / "inputs" / evidence).is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077):
        raise WorkflowError("owner-only independent native recovery review required")
    proof = read_json(path)
    proof_sha = digest(path)
    candidate = inspect(runtime, original_job, str(Path(proof["repair"]["path"]).relative_to("inputs")))
    if proof != {**candidate, "approved": True}:
        raise WorkflowError("OPS review must exactly bind current original generation and remaining authority")
    # Both durable databases are backed up before this trusted operation. A crash
    # between commits fails closed: never re-append the one-shot identity grant.
    with runtime.store.transaction() as db, gate.database(root / "execution-gate.sqlite3") as wire:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (proof["task_id"],)).fetchone()
        slot = db.execute("SELECT * FROM slots WHERE id=?", (proof["slot"],)).fetchone()
        owners = [dict(r) for r in db.execute(
            "SELECT * FROM owned WHERE task_id=? AND state!='deleted' ORDER BY id", (proof["task_id"],),
        )]
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
                or task["state"] != "blocked" or task["attempt"] != proof["attempt"]
                or task["packet"] != proof["packet"]["path"] or owners != proof["ownership"]
                or not slot or slot["task_id"] != task["id"]
                or any(slot[k] != proof[k] for k in ("target", "origin"))
                or digest(path) != proof_sha
                or read_json(path) != proof):
            raise WorkflowError("model started during native recovery review")
        active = wire.execute("SELECT * FROM active WHERE slot=?", (str(proof["slot"]),)).fetchone()
        rows = wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid", (proof["scope"],)).fetchall()
        if (not active or any(active[k] != proof[k] for k in ("scope", "session", "account"))
                or active["phase"] != proof["original_phase"]
                or (active["expires"] is not None and active["expires"] <= time.time())
                or [{"id": r["id"], "sha256": gate.nas_native_snapshot(r)} for r in rows] != proof["preserved_attempts"]
                or wire.execute("SELECT count(*) FROM grants WHERE scope=?", (proof["scope"],)).fetchone()[0]
                != len(proof["preserved_grants"])):
            raise WorkflowError("original generation changed before registration")
        original = wire.execute("SELECT * FROM grants WHERE scope=? AND id='identity'", (proof["scope"],)).fetchone()
        for grant in proof["preserved_grants"]:
            row = wire.execute("SELECT * FROM grants WHERE scope=? AND id=?", (proof["scope"], grant["id"])).fetchone()
            if not row or row["used"] != grant["used"] or gate.nas_native_snapshot(
                {k: v for k, v in dict(row).items() if k != "used"}
            ) != grant["definition_sha256"]:
                raise WorkflowError("original wire changed before registration")
        wire.execute("INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,'identity',?,1,0,?)",
                     (proof["scope"], proof["grant_id"], original["call"], proof["authority"]))
        wire.execute("INSERT INTO authority_limits VALUES(?,?,0)", (proof["authority"], proof["grant_id"]))
        wire.execute("INSERT INTO cleanup_identity_reviews VALUES(?,?,?,?,?,?,?)", (
            proof["scope"], proof["grant_id"], proof["session"], proof["account"], proof["authority"],
            str(path), digest(path),
        ))
        # Original cleanup intent never reached the wire. Keep that intent and all
        # original evidence immutable; only the reviewed original phase can resume.
        wire.execute("UPDATE active SET phase='execute' WHERE slot=? AND scope=? AND session=?",
                     (str(proof["slot"]), proof["scope"], proof["session"]))
        active = wire.execute("SELECT * FROM active WHERE slot=?", (str(proof["slot"]),)).fetchone()
        if gate.nas_native_continuation(wire, active) is None:
            raise WorkflowError("product rejected exact native signer review")
        recovery = {"original_job_id": original_job, "dimension": "QUERY", "evidence": str(path.relative_to(root)),
                    "evidence_sha256": digest(path), "scope": proof["scope"], "session_id": proof["session"]}
        db.execute("INSERT INTO meta VALUES(?,?)", ("native_continuation:" + proof["task_id"], json.dumps(recovery)))
        db.execute("UPDATE tasks SET state='continuation_pending',reason=NULL WHERE id=?", (proof["task_id"],))
        runtime.store.event(db, "ops_native_continuation_queued", {
            "task_id": proof["task_id"], **recovery, "original_counters_unchanged": True, "pass_added": False,
        })
    return {"continuation_queued": proof["task_id"], "no_glm_dispatched": True,
            "old_calls_refilled": False, "additional_identity_reads": 1, "pass_added": False}


def delivery(root: Path, recovery: dict[str, Any], stage: str) -> str:
    """Fixed natural request; product injects the original current machine contract."""
    if stage not in {"identity", "source", "cleanup"}:
        raise WorkflowError("native continuation stage must be identity/source/cleanup")
    review_path = contained(root, recovery["evidence"])
    if digest(review_path) != recovery["evidence_sha256"]:
        raise WorkflowError("native continuation review changed")
    if recovery.get("kind") == "managed_read_continuation":
        with gate.database(root / "execution-gate.sqlite3") as wire:
            extension = wire.execute("SELECT * FROM grants WHERE scope=? AND id=?",
                                     (recovery["scope"], recovery.get("identity_grant_id"))).fetchone()
            active = wire.execute("SELECT * FROM active WHERE scope=? AND session=?",
                                  (recovery["scope"], recovery["session_id"])).fetchone()
            if (stage != "identity" or not active or active["phase"] != "execute"
                    or not extension or extension["used"] != 0
                    or extension["id"] not in {e["id"] for e in gate.cleanup_identity_extensions(
                        wire, active["scope"])}):
                raise WorkflowError("only unused independently reviewed managed identity renewal permitted")
        return ("OPS已独立批准一次原会话同账号身份续核。只执行当前机器契约中的 STS GetCallerIdentity，"
                "等待真实账号结果后立即结束。禁止同时查询资源、创建或删除；不能重放旧身份额度。")
    with gate.database(root / "execution-gate.sqlite3") as wire:
        active = wire.execute("SELECT * FROM active WHERE scope=? AND session=?",
                              (recovery["scope"], recovery["session_id"])).fetchone()
        phases = {"execute", "cleanup"} if stage == "identity" else {"cleanup" if stage == "cleanup" else "execute"}
        if not active or active["phase"] not in phases:
            raise WorkflowError("native continuation must retain original active phase/session")
        chain = gate.nas_native_continuation(wire, active)
        if not chain:
            raise WorkflowError("missing reviewed signer continuation")
        extension, signer, _ = chain
        if stage == "identity":
            if extension["used"] != 0:
                raise WorkflowError("one-shot identity already used; do not replay")
            return ("OPS已独立批准一次原会话身份续核。只执行机器契约中的 STS GetCallerIdentity，"
                    "等待真实账号结果后结束。禁止同时查询资源、创建或删除。")
        if extension["used"] != 1 or signer["grant_id"] != extension["id"]:
            raise WorkflowError("actual renewed same-account STS must succeed before continuation")
    if stage == "cleanup":
        return ("继续刚才原样本的反向清理，仅按当前系统机器契约完成原剩余删除与只读消失核查。"
                "已成功或结果未知的写操作不重发，不创建、不改名，不恢复额度。保留所有真实失败和未消失状态。")
    return ("继续同一会话原样本尚未完成的依赖操作。沿用已成功创建的文件系统及网络，不重新创建这些资源。"
            "先核对原文件系统真实状态，然后严格依照当前系统机器契约完成尚未调用的原依赖及数据流，"
            "取得自身健康查询与结构化引用后结束，保留资源供外部截图。不是新计划，不改名、不补充额度，"
            "不重复身份、已成功或未知的写操作，不删除。")
