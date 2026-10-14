"""OPS checkpoint for retained ROS pixels or a proven pre-click rejection.

This never installs ROS capabilities or adds a resource/identity allowance. The
original creation, source, signer, session and cleanup journal remain authoritative.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from .files import WorkflowError, contained, digest, read_json
from .native_continuation import terminal

LOADED_SHA = digest(Path(__file__))


def preclick_rejection(root: Path, job, scene: str, click: dict) -> dict:
    """A diagnostic product PNG is not a click; require exact terminal trace proof.

    Only the known destination-selection rejection happens before link.click.
    Pending requests, other errors and any successful click remain ineligible.
    """
    proof = terminal(root, job)
    trace = contained(root, proof["transport_path"])
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    captures, pending, rejected = [], False, 0
    error = ("precise reference destination ambiguous or pending; copy the exact current snapshot session_id; "
             "never guess or retry creation")
    for event in events:
        params = event.get("params", {})
        if not event.get("method", "").startswith("script/") or params.get("operation") not in {"click", "evidence"}:
            continue
        if any(params.get(k) != v for k, v in (("jobId", job["id"]), ("threadId", job["thread_id"]),
                                               ("turnId", job["turn_id"]))):
            raise WorkflowError("pre-click trace belongs to another original job/turn")
        if params["operation"] == "evidence":
            if event["method"] == "script/result":
                captures.append(params.get("result"))
            continue
        if event["method"] == "script/request":
            args = params.get("arguments", {})
            if (pending or args.get("scene") != scene
                    or args.get("session_id", click["session_id"]) != click["session_id"]):
                raise WorkflowError("pre-click request changed or remained unknown")
            pending = True
        elif event["method"] == "script/rejected" and pending and params.get("error") == error:
            pending = False
            rejected += 1
        else:
            raise WorkflowError("only an explicit original destination-selection rejection may recover")
    if pending or not rejected or captures != [click]:
        raise WorkflowError("exact rejected clicks and unchanged diagnostic result required")
    output = root / "jobs" / job["id"] / "output"
    if any(output.glob("occurrence-*.json")) or any(output.glob("*-metadata.png")):
        raise WorkflowError("pre-click recovery cannot replay a selected or captured occurrence")
    return {"kind": "original_preclick_destination_rejection", "rejected_clicks": rejected,
            "terminal": proof, "diagnostic_only": True, "actual_ui_click": False}


def inspect(runtime, original_job: str) -> dict:
    root = runtime.root
    with runtime.store.transaction() as db:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("retained capture requires paused drained maintenance")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (original_job,)).fetchone()
        if not job or job["role"] != "glm" or job["purpose"] != "dispatch" or job["state"] != "complete":
            raise WorkflowError("original terminal dispatched GLM required")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
        spec = json.loads(task["spec"])
        slot = db.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
        latest = db.execute("SELECT id FROM jobs WHERE task_id=? AND role='glm' ORDER BY rowid DESC LIMIT 1",
                            (task["id"],)).fetchone()
        review = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='sol' ORDER BY rowid DESC LIMIT 1",
                            (task["id"],)).fetchone()
        if (task["state"] != "blocked" or spec["key"] != "ALIYUN::ROCKETMQ::Instance"
                or not slot or slot["task_id"] != task["id"] or not slot["target"]
                or latest[0] != original_job or not review or review["purpose"] != "review"
                or review["state"] != "complete" or review["submission"] != "result_staged"):
            raise WorkflowError("retained RocketMQ generation and independent terminal Sol inspection required")
        from .creation import execution_authority, validate_authorization
        from .plans import expected_scenes
        from .temporary_samples import admitted

        packet_file = contained(root, task["packet"])
        packet = read_json(packet_file)
        validate_authorization(root, task["resource_id"], packet)
        scenes = expected_scenes(spec)
        if (set(packet["requests"]) != {"ROS", "QUERY"} or set(packet["execution_grants"]) != {"ROS", "QUERY"}
                or set(packet["scenes"]) != {s["id"] for s in scenes}
                or len(scenes) != 2 or {s["dimension"] for s in scenes} != {"ROS", "QUERY"}
                or any(s["mode"] != "open" for s in scenes)
                or set(admitted(root, db, task, packet)) != {"ROS"}):
            raise WorkflowError("only original admitted empty ROS sample and untouched readonly QUERY")
        for entry in packet.get("inputs", []):
            if digest(contained(root, entry["path"])) != entry["sha256"]:
                raise WorkflowError("original packet input changed")
        output = root / "jobs" / original_job / "output"
        session_file = output / "self-created-session.json"
        session = read_json(session_file)
        if (any(session.get(k) != v for k, v in (("job_id", original_job), ("target", slot["target"]),
                                                ("origin", slot["origin"])))
                or not session.get("session_id") or session["session_id"] == read_json(
                    output / "new-session-intent.json").get("previous_session")):
            raise WorkflowError("original self-created complete Web session required")
        ros_scene = next(s for s in scenes if s["dimension"] == "ROS")
        source_file = output / "ROS-product-source.json"
        click_file = output / (ros_scene["id"] + ".json")
        source, click = read_json(source_file), read_json(click_file)
        image = contained(root, click.get("screenshot", ""))
        rejected = None
        if click.get("actual_ui_click") is False:
            if (click.get("scene") != ros_scene["id"] or click.get("target") != slot["target"]
                    or click.get("final_url") != slot["origin"] + "/"
                    or click.get("account") != packet["scenes"][ros_scene["id"]]["account"]
                    or click.get("region") != packet["scenes"][ros_scene["id"]]["region"]):
                raise WorkflowError("pre-click diagnostic must retain the original product/scene identity")
            rejected = preclick_rejection(root, job, ros_scene["id"], click)
        bound_click = (click.get("actual_ui_click") is True
                       and click.get("source_evidence") == str(source_file.relative_to(root))
                       and click.get("source_sha256") == digest(source_file))
        if ((not rejected and not bound_click) or click.get("session_id") != session["session_id"]
                or source.get("session_id") != session["session_id"]
                or not db.execute("SELECT 1 FROM inspected WHERE job_id=? AND artifact=? AND sha=?",
                                  (review["id"], str(image.relative_to(root)), digest(image))).fetchone()
                or db.execute("SELECT 1 FROM verdicts WHERE task_id=? AND status='pass' AND scene IN (?,?)",
                              (task["id"], *[s["id"] for s in scenes])).fetchone()
                or any((output / name).exists() for name in
                       ("QUERY-request-intent.json", "QUERY-product-source.json", "ROS-cleanup-source.json"))):
            raise WorkflowError("independently inspected failed pixels; no PASS, QUERY or cleanup replay")
        owners = [dict(r) for r in db.execute("SELECT * FROM owned WHERE task_id=? AND state!='deleted' ORDER BY id",
                                             (task["id"],))]
        previous = db.execute("SELECT value FROM meta WHERE key=?",
                              ("native_continuation:" + task["id"],)).fetchone()
        if not owners:
            raise WorkflowError("original retained ownership and first capture checkpoint required")
        previous_value = None
        if previous:
            from .native_continuation import load

            previous_value = load(root, db, task["id"])
            old_job = db.execute("SELECT * FROM jobs WHERE id=?",
                                 (previous_value.get("original_job_id"),)).fetchone()
            operations = [r for r in packet.get("additional_source_requests", []) if r.get("dimension") == "ROS"]
            if (len(operations) != 1 or previous_value.get("kind") != "retained_ros_capture"
                    or not old_job or old_job["state"] != "complete" or old_job["role"] != "glm"
                    or old_job["task_id"] != task["id"] or old_job["thread_id"] != job["thread_id"]
                    or old_job["id"] == original_job
                    or previous_value.get("scope") != old_job["id"] + ":ROS"
                    or operations[0].get("prior_scope") != previous_value["scope"]):
                raise WorkflowError("only this explicitly reviewed closed predecessor checkpoint may be archived")
            # admitted() above already revalidated the precise original Sol
            # root/member cleanup receipt. Never replace an active/unknown one.
            with gate.database(root / "execution-gate.sqlite3") as wire:
                scope = previous_value["scope"]
                if (wire.execute("SELECT 1 FROM active WHERE scope=?", (scope,)).fetchone()
                        or wire.execute("SELECT 1 FROM attempts WHERE scope=? AND state!='succeeded'",
                                        (scope,)).fetchone()
                        or wire.execute("SELECT count(*) FROM attempts a JOIN grants g "
                                        "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND g.kind='delete'",
                                        (scope,)).fetchone()[0] != 1):
                    raise WorkflowError("old capture scope must be inactive and successfully deleted, not abandoned")
        result = {"schema_version": 1, "kind": "ops_retained_ros_capture_review", "approved": False,
                  "task_id": task["id"], "original_job_id": original_job, "attempt": task["attempt"],
                  "thread_id": job["thread_id"], "original_turn_id": job["turn_id"], "slot": slot["id"],
                  "target": slot["target"], "origin": slot["origin"], "session": session["session_id"],
                  "scope": original_job + ":ROS", "account": packet["scenes"][ros_scene["id"]]["account"],
                  "authority": execution_authority(root, packet, "ROS"),
                  "packet": {"path": task["packet"], "sha256": digest(packet_file)},
                  "original_terminal": terminal(root, job), "review_terminal": terminal(root, review),
                  "self_created_session": {"path": str(session_file.relative_to(root)), "sha256": digest(session_file)},
                  "capture_source": {"path": str(source_file.relative_to(root)), "sha256": digest(source_file)},
                  "original_click": {"path": str(click_file.relative_to(root)), "sha256": digest(click_file)},
                  "original_png": {"path": str(image.relative_to(root)), "sha256": digest(image)},
                  "ownership": owners, "notice_title": "RocketMQ for AI：全新轻量主题模型上线",
                  "new_creation_quota": 0, "additional_identity_reads": 0, "old_calls_refilled": False,
                  "pass_added": False, "query_authority": execution_authority(root, packet, "QUERY")}
        if rejected:
            result["original_capture_rejection"] = rejected
        if previous_value:
            result["previous_completed_capture"] = previous_value
    candidate = runtime.inspect_cleanup(original_job, "ROS")
    deletes = [g for g in candidate["remaining_calls"] if g["kind"] == "delete"]
    if (len(deletes) != 1 or deletes[0]["remaining"] != 1 or candidate["successful_deletions"]
            or candidate["scope"] != result["scope"] or candidate["session_id"] != result["session"]):
        raise WorkflowError("original response-bound deletion must remain unused")
    with gate.database(root / "execution-gate.sqlite3") as wire:
        grants = [dict(g) for g in wire.execute("SELECT * FROM grants WHERE scope=? ORDER BY id", (result["scope"],))]
        attempts = [dict(a) for a in wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid",
                                                (result["scope"],))]
        extensions = gate.cleanup_identity_extensions(wire, result["scope"])
        if (not attempts or any(a["state"] != "succeeded" or a["session"] != result["session"] for a in attempts)
                or len(extensions) > 1 or len(grants) != len(packet["execution_grants"]["ROS"]) + len(extensions)
                or wire.execute("SELECT 1 FROM grants WHERE scope=?", (original_job + ":QUERY",)).fetchone()
                or wire.execute("SELECT 1 FROM attempts WHERE scope=?", (original_job + ":QUERY",)).fetchone()):
            raise WorkflowError("unknown calls, identity supplements or previously installed QUERY cannot recover")
        for g in grants:
            original = next((v for v in packet["execution_grants"]["ROS"] if v["id"] == g["id"]), None)
            used = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                (result["authority"], g["id"])).fetchone()
            if any(g["id"] == e["id"] for e in extensions):
                if g["used"] != 0 or not used or used[0] != 0 or any(a["grant_id"] == g["id"] for a in attempts):
                    raise WorkflowError("retained capture can consume only the separately reviewed unused identity")
                registration = wire.execute("SELECT * FROM cleanup_identity_reviews WHERE scope=?",
                                            (result["scope"],)).fetchone()
                if any(registration[k] != result[k] for k in ("scope", "authority", "account", "session")):
                    raise WorkflowError("cleanup identity belongs to another original generation")
                result["pending_cleanup_identity"] = {"id": g["id"], "call": json.loads(g["call"]),
                    "evidence": str(Path(registration["evidence"]).relative_to(root)),
                    "sha256": registration["evidence_sha256"]}
                continue
            if (not original or g["authority"] != result["authority"] or not used or used[0] != g["used"]
                    or g["used"] != sum(a["grant_id"] == g["id"] for a in attempts)
                    or not 0 <= g["used"] <= g["quota"]
                    or any(g[k] != v for k, v in {
                        "kind": original["kind"], "quota": original["quota"],
                        "call": gate.canonical(gate.api_call(original["call"])), "parent": original.get("parent"),
                        "response_path": gate.canonical(original.get("response_path")),
                        "target_param": original.get("target_param"),
                        "target_encoding": original.get("target_encoding"),
                        "read_policy": (gate.canonical(original.get("read_policy"))
                                        if original.get("read_policy") else None),
                    }.items())):
                raise WorkflowError("original grants and cumulative counters changed")
        result.update(wire_sha256=gate.nas_native_snapshot({"grants": grants, "attempts": attempts}),
                      original_phase=candidate["original_phase"], delete_call=deletes[0]["call"])
    refs = runtime.proven_references(original_job, "ROS", result["session"], result["account"],
                                    source.get("references", {}).get("items", []))
    refs = runtime.created_references(original_job, "ROS", result["session"], refs)
    identifiers = {r.get("identifier") for r in refs if r.get("resourceType") == spec["key"]}
    identifier = next(iter(identifiers)) if rejected and len(identifiers) == 1 else click.get("source_identifier")
    if not any(r.get("resourceType") == spec["key"] and r.get("identifier") == identifier for r in refs):
        raise WorkflowError("original source must still prove its exact own ROS instance")
    result["identifier"] = identifier
    current = runtime.browser({"operation": "snapshot", "origin": slot["origin"], "target": slot["target"]})
    if (current.get("session_id") != result["session"] or current.get("busy")
            or not current.get("unlocked") or current.get("preview_open")):
        raise WorkflowError("original retained source session must be idle and unlocked")
    return result


def queue(runtime, original_job: str, evidence: str) -> dict:
    path = contained(runtime.root / "inputs", evidence)
    if path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077):
        raise WorkflowError("private immutable OPS capture review required")
    proof = read_json(path)
    if proof != {**inspect(runtime, original_job), "approved": True}:
        raise WorkflowError("OPS review must bind exact original source, old pixels and all remaining calls")
    with runtime.store.transaction() as db:
        if (db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("maintenance changed before capture queue")
        value = {"kind": "retained_ros_capture", "dimension": "ROS", "original_job_id": original_job,
                 "scope": proof["scope"], "session_id": proof["session"],
                 "evidence": str(path.relative_to(runtime.root)), "evidence_sha256": digest(path),
                 "capture_source": proof["capture_source"], "delete_call": proof["delete_call"]}
        if proof.get("pending_cleanup_identity"):
            value["identity_grant_id"] = proof["pending_cleanup_identity"]["id"]
        pointer = "native_continuation:" + proof["task_id"]
        if proof.get("previous_completed_capture"):
            previous = proof["previous_completed_capture"]
            current = db.execute("SELECT value FROM meta WHERE key=?", (pointer,)).fetchone()
            if not current or json.loads(current[0]) != previous:
                raise WorkflowError("completed predecessor pointer changed before capture queue")
            history = "native_continuation_history:" + proof["task_id"] + ":" + previous["scope"]
            archived = db.execute("SELECT value FROM meta WHERE key=?", (history,)).fetchone()
            if archived and json.loads(archived[0]) != previous:
                raise WorkflowError("original capture history cannot be overwritten")
            db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (history, current[0]))
            db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(value), pointer))
        else:
            db.execute("INSERT INTO meta VALUES(?,?)", (pointer, json.dumps(value)))
        db.execute("UPDATE tasks SET state='continuation_pending',reason=NULL WHERE id=?", (proof["task_id"],))
        runtime.store.event(db, "ops_retained_ros_capture_queued", value)
    return {"continuation_queued": proof["task_id"], "only_luna_dispatches": True,
            "new_creation_quota": 0, "additional_identity_reads": 0, "old_calls_refilled": False}


def identity_delivery(root: Path, recovery: dict) -> str:
    """Consume an existing separate OPS identity review; never append another grant."""
    with gate.database(root / "execution-gate.sqlite3") as wire:
        active = wire.execute("SELECT * FROM active WHERE scope=? AND session=?",
                              (recovery["scope"], recovery["session_id"])).fetchone()
        extension = next((g for g in gate.cleanup_identity_extensions(wire, recovery["scope"])
                          if g["id"] == recovery.get("identity_grant_id")), None)
        if not active or active["phase"] != "cleanup" or not extension or extension["used"] != 0:
            raise WorkflowError("only separately approved unused cleanup identity may continue")
    return ("OPS已独立审查原签名到期后的单次身份续核。仅执行下面STS一次，等待实际账号结果后结束，"
            "不同时查询资源、创建或删除，不重放旧身份额度：\n" + extension["call"])
