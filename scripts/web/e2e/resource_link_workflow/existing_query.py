"""Reviewed existing samples: a fresh read-only source, never a fabricated creation.

Keep the original creation scope, owners, reservations and packet. The new query
gets its own scope and actual UI-created session. Cleanup is a separate review.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from .files import WorkflowError, contained, digest, json_write, read_json
from .native_continuation import terminal

LOADED_SHA = digest(Path(__file__))


def load(root: Path, db: Any, task_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", ("existing_query:" + task_id,)).fetchone()
    if not row:
        return None
    record = json.loads(row[0])
    proof = read_json(contained(root, record["evidence"]))
    proof_path = contained(root, record["evidence"])
    if (digest(proof_path) != record["evidence_sha256"] or proof.get("approved") is not True
            or proof.get("kind") != "ops_existing_query_review" or proof.get("task_id") != task_id
            or proof.get("new_creation_quota") != 0 or proof.get("pass_added") is not False
            or (os.name != "nt" and proof_path.stat().st_mode & 0o077)):
        raise WorkflowError("existing query review changed")
    for entry in proof["sources"] + [proof["original_packet"], proof["packet"]]:
        if digest(contained(root, entry["path"])) != entry["sha256"]:
            raise WorkflowError("existing sample provenance changed")
    return {**record, **proof, "capture_recovery": load_capture(root, db, task_id)}


def snapshot_wire(root: Path, scope: str) -> dict[str, Any]:
    with gate.database(root / "execution-gate.sqlite3") as db:
        active = db.execute("SELECT * FROM active WHERE scope=?", (scope,)).fetchall()
        if len(active) != 1 or active[0]["phase"] != "execute":
            raise WorkflowError("original retained execute scope required")
        return {"active": dict(active[0]), "authority_limits": [dict(r) for r in db.execute(
            "SELECT * FROM authority_limits WHERE authority IN (SELECT authority FROM grants WHERE scope=?) "
            "ORDER BY authority,id", (scope,))], "tables": {name: [dict(r) for r in db.execute(
            f"SELECT * FROM {name} WHERE scope=? ORDER BY rowid", (scope,),
        )] for name in ("grants", "attempts")}}


def propose(runtime: Any, task_id: str, handoff: str) -> dict[str, Any]:
    root = runtime.root
    seed_path = contained(root / "inputs", handoff)
    seed = read_json(seed_path)
    if seed.get("kind") != "ops_user_created_nas_sample_handoff" or seed.get("task_id") != task_id:
        raise WorkflowError("exact independently read existing-sample handoff required")
    sources = seed["independent_reads"] + [{"path": seed["human_declaration"],
                                          "sha256": digest(contained(root, seed["human_declaration"]))},
                                         {"path": seed_path.relative_to(root).as_posix(), "sha256": digest(seed_path)}]
    for source in sources:
        if digest(contained(root, source["path"])) != source["sha256"]:
            raise WorkflowError("existing sample raw evidence changed")
    raws = [read_json(contained(root, entry["path"])) for entry in seed["independent_reads"]]
    identity, fs, flow, fset = [raw["body"] for raw in raws]
    binding = {key: seed[key] for key in ("account", "region", "file_system_id", "dataflow_id",
                                         "fileset_id", "file_system_path", "source_storage")}
    flows = flow.get("DataFlowInfo", {}).get("DataFlow")
    filesets = fset.get("Entries", {}).get("Entrie")
    filesystems = fs.get("FileSystems", {}).get("FileSystem")
    if (any(raw.get("exit_code") != 0 or not raw["body"].get("RequestId") for raw in raws)
            or identity.get("AccountId") != binding["account"] or not isinstance(flows, list) or len(flows) != 1
            or not isinstance(filesets, list) or len(filesets) != 1
            or not isinstance(filesystems, list) or len(filesystems) != 1
            or filesystems[0].get("FileSystemId") != binding["file_system_id"]
            or filesystems[0].get("Status") != "Running"
            or filesets[0].get("FsetId") != binding["fileset_id"]
            or filesets[0].get("FileSystemPath") != binding["file_system_path"]
            or filesets[0].get("Status") != "CREATED" or flow.get("NextToken") or fset.get("NextToken")):
        raise WorkflowError("actual same-account healthy bounded sample reads required")
    validate_flow(binding, flows[0])
    query = {**raws[2]["call"], "region_id": binding["region"]}
    expected_params = {"FileSystemId": binding["file_system_id"], "MaxResults": 20,
                       "Filters.1.Key": "DataFlowIds", "Filters.1.Value": binding["dataflow_id"]}
    if query != {"product": "nas", "action": "DescribeDataFlows", "version": "2017-06-26",
                 "region_id": binding["region"], "params": expected_params}:
        raise WorkflowError("exact bounded existing resource query required")
    with runtime.store.transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (not task or task["state"] != "blocked" or task["slot"] is None
                or db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
                or load(root, db, task_id)):
            raise WorkflowError("pause and drain before a unique existing query admission")
        original = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='glm' AND purpose='dispatch' "
                              "ORDER BY created DESC LIMIT 1", (task_id,)).fetchone()
        latest = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='glm' ORDER BY created DESC LIMIT 1",
                            (task_id,)).fetchone()
        review = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='sol' AND purpose='review' "
                            "ORDER BY created DESC LIMIT 1", (task_id,)).fetchone()
        slot = db.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
        owners = [dict(r) for r in db.execute("SELECT * FROM owned WHERE task_id=? ORDER BY id", (task_id,))]
        count = db.execute("SELECT count(*) FROM jobs WHERE task_id=? AND purpose='continuation'",
                           (task_id,)).fetchone()[0]
        old = read_json(contained(root, task["packet"]))
        scene_id = "R0036-QUERY-1-iframe"
        if (slot["task_id"] != task_id or set(old["scenes"]) != {scene_id}
                or any(json.loads(r[0]).get("actual_ui_click") for r in db.execute(
                    "SELECT data FROM observations WHERE task_id=? AND attempt=?", (task_id, task["attempt"])))):
            raise WorkflowError("retain exact missing scene and original occupied slot")
        session = read_json(root / f"jobs/{original['id']}/output/self-created-session.json")
        if session["target"] != slot["target"] or session["origin"] != slot["origin"]:
            raise WorkflowError("original target changed")
        original_packet = {"path": task["packet"], "sha256": digest(contained(root, task["packet"]))}
        terminals = [terminal(root, job) for job in (original, latest, review)]
        for entry in terminals:
            sources.extend([{"path": entry["path"], "sha256": entry["sha256"]},
                            {"path": entry["transport_path"], "sha256": entry["transport_sha256"]}])
        scene = {**old["scenes"][scene_id], "identifier": binding["dataflow_id"],
                 "render_text": [binding["dataflow_id"], binding["file_system_path"], binding["source_storage"]],
                 "source_evidence": sources[2]["path"], "source_sha256": sources[2]["sha256"],
                 "source_binding": {"origin": seed["origin"], "formal_signed_query_required": True}}
        packet = {"task_id": task_id, "profile": "test-guima", "terraform": "ros_managed_terraform",
                  "budget_cents": 0, "inputs": sources[:6], "ui": old["ui"], "scenes": {scene_id: scene},
                  "preserve_pass": True,
                  "requests": {"QUERY": "先单独确认当前云账号，再按系统已审精确只读计划查询已有数据流，"
                               "生成真实资源引用后停止。禁止创建、修改、删除或重试旧预检查。"},
                  "execution_grants": {"QUERY": [
                      {"id": "identity", "kind": "identity", "quota": 1,
                       "call": {"product": "sts", "action": "GetCallerIdentity", "version": "2015-04-01",
                                "region_id": binding["region"], "params": {}}},
                      {"id": "existing-source", "kind": "read", "quota": 1, "call": query,
                       "read_policy": {"phase": "execute"}},
                  ]}}
        packet_path = root / "inputs" / task["resource_id"] / "existing-query-packet.json"
        if packet_path.exists():
            if read_json(packet_path) != packet:
                raise WorkflowError("do not overwrite existing operation packet")
        else:
            json_write(packet_path, packet, private=True)
        result = {"schema_version": 1, "kind": "ops_existing_query_review", "approved": False,
                  "task_id": task_id, "attempt": task["attempt"], "previous_continuation_count": count,
                  "original_job_id": original["id"], "thread_id": original["thread_id"],
                  "original_turn_id": original["turn_id"], "session_id": session["session_id"],
                  "slot": slot["id"], "target": slot["target"], "origin": slot["origin"],
                  "account": binding["account"], "binding": binding, "sources": sources,
                  "original_packet": original_packet, "retained_owners": owners,
                  "packet": {"path": packet_path.relative_to(root).as_posix(), "sha256": digest(packet_path)},
                  "old_scope": original["id"] + ":QUERY", "new_creation_quota": 0, "pass_added": False,
                  "cleanup_owner": "ops_after_original_sol_screenshot_review",
                  "manual_test_resources": [{"id": binding["dataflow_id"], "kind": "DataFlow"},
                                            {"id": binding["fileset_id"], "kind": "Fileset"}]}
    result["retained_wire"] = snapshot_wire(root, result["old_scope"])
    return result


def validate_flow(binding: dict[str, Any], row: dict[str, Any]) -> None:
    fields = {"DataFlowId": "dataflow_id", "FileSystemId": "file_system_id", "FsetId": "fileset_id",
              "FileSystemPath": "file_system_path", "SourceStorage": "source_storage"}
    if (any(row.get(key) != binding[value] for key, value in fields.items())
            or row.get("Status") != "Running" or row.get("ErrorMessage") != ""):
        raise WorkflowError("existing DataFlow identity/dependencies/health changed")


def queue(runtime: Any, evidence: str) -> dict[str, Any]:
    path = contained(runtime.root / "inputs", evidence)
    proof = read_json(path)
    if proof.get("kind") != "ops_existing_query_review" or proof.get("approved") is not True:
        raise WorkflowError("independent existing query approval required")
    with runtime.store.transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (proof["task_id"],)).fetchone()
        if not task:
            raise WorkflowError("existing query task missing")
        original = db.execute("SELECT * FROM jobs WHERE id=?", (proof["original_job_id"],)).fetchone()
        slot = db.execute("SELECT * FROM slots WHERE id=?", (proof["slot"],)).fetchone()
        if (not original or not slot or original["task_id"] != task["id"]
                or original["role"] != "glm" or original["purpose"] != "dispatch"
                or original["thread_id"] != proof["thread_id"] or original["turn_id"] != proof["original_turn_id"]
                or slot["task_id"] != task["id"] or task["slot"] != proof["slot"]
                or any(slot[k] != proof[k] for k in ("target", "origin"))
                or proof["original_packet"]["path"] != task["packet"]
                or proof["old_scope"] != original["id"] + ":QUERY"):
            raise WorkflowError("existing query original task/slot/thread binding changed")
        owners = [dict(r) for r in db.execute("SELECT * FROM owned WHERE task_id=? ORDER BY id", (task["id"],))]
        if (task["state"] != "blocked" or task["attempt"] != proof["attempt"] or owners != proof["retained_owners"]
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
                or snapshot_wire(runtime.root, proof["old_scope"]) != proof["retained_wire"]):
            raise WorkflowError("existing query original state changed")
        record = {"evidence": path.relative_to(runtime.root).as_posix(), "evidence_sha256": digest(path)}
        db.execute("INSERT INTO meta VALUES(?,?)", ("existing_query:" + task["id"], json.dumps(record)))
        load(runtime.root, db, task["id"])
        packet = read_json(contained(runtime.root, proof["packet"]["path"]))
        if (packet.get("task_id") != task["id"] or packet.get("budget_cents") != 0
                or set(packet.get("requests", {})) != {"QUERY"}
                or any(scene.get("account") != proof["account"] for scene in packet["scenes"].values())
                or proof["manual_test_resources"] != [
                    {"id": proof["binding"]["dataflow_id"], "kind": "DataFlow"},
                    {"id": proof["binding"]["fileset_id"], "kind": "Fileset"}]):
            raise WorkflowError("existing query exact account and manual ownership required")
        # Honest external test ownership. Never create a fictional wire attempt.
        for sample in proof["manual_test_resources"]:
            resource = {**sample, "region": proof["binding"]["region"],
                        "file_system_id": proof["binding"]["file_system_id"],
                        "origin": "user_manual_console_creation_not_glm_wire"}
            owner_id = hashlib.sha256((task["id"] + ":external:" + sample["id"]).encode()).hexdigest()
            db.execute("INSERT INTO owned VALUES(?,?,?,?,?,?)", (
                owner_id, task["id"], "QUERY", json.dumps(resource, ensure_ascii=False, sort_keys=True),
                "present", json.dumps({"path": record["evidence"], "sha256": record["evidence_sha256"]}),
            ))
        db.execute("UPDATE tasks SET state='continuation_pending',reason=NULL WHERE id=?", (task["id"],))
        runtime.store.event(db, "ops_existing_query_queued", {"task_id": task["id"], **record,
                            "manual_test_resources": proof["manual_test_resources"], "pass_added": False})
    return {"queued": proof["task_id"], "no_glm_dispatched": True, "no_cloud_calls": True}


def reviewed(root: Path, db: Any, job: Any) -> bool:
    proof = load(root, db, job["task_id"])
    if not proof:
        return False
    if proof.get("capture_recovery"):
        proof = proof["capture_recovery"]
    task = db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
    slot = db.execute("SELECT * FROM slots WHERE id=?", (proof["slot"],)).fetchone()
    original = db.execute("SELECT * FROM jobs WHERE id=?", (proof["original_job_id"],)).fetchone()
    count = db.execute("SELECT count(*) FROM jobs WHERE task_id=? AND purpose='continuation'",
                       (task["id"],)).fetchone()[0]
    return bool(slot and original and slot["task_id"] == task["id"] and task["slot"] == proof["slot"]
                and all(slot[k] == proof[k] for k in ("target", "origin"))
                and original["thread_id"] == proof["thread_id"] and original["turn_id"] == proof["original_turn_id"]
                and task["state"] == "running" and task["attempt"] == proof["attempt"] + 1
                and job["thread_id"] == proof["thread_id"] and job["turn_id"] is None
                and job["submission"] in {"not_submitted", "thread_bound"}
                and count == proof["previous_continuation_count"] + 1)


def references(root: Path, proof: dict[str, Any], job_id: str, session: str, values: list) -> list:
    with gate.database(root / "execution-gate.sqlite3") as db:
        rows = db.execute("SELECT * FROM attempts WHERE scope=? AND session=? AND grant_id='existing-source'",
                          (job_id + ":QUERY", session)).fetchall()
        if len(rows) != 1 or rows[0]["state"] != "succeeded":
            raise WorkflowError("fresh successful signed existing query required")
        raw = json.loads(rows[0]["response"])
        flows = raw.get("DataFlowInfo", {}).get("DataFlow")
        if not isinstance(flows, list) or len(flows) != 1 or raw.get("NextToken"):
            raise WorkflowError("bounded unique existing DataFlow source required")
        validate_flow(proof["binding"], flows[0])
    return [r for r in values if r.get("identifier") == proof["binding"]["dataflow_id"]]


def load_capture(root: Path, db: Any, task_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", ("existing_query_capture:" + task_id,)).fetchone()
    if not row:
        return None
    record = json.loads(row[0])
    file = contained(root, record["evidence"])
    proof = read_json(file)
    if (digest(file) != record["evidence_sha256"] or proof.get("kind") != "ops_existing_query_capture_review"
            or proof.get("task_id") != task_id or proof.get("approved") is not True
            or proof.get("cloud_calls") != 0 or proof.get("pass_added") is not False
            or proof.get("capture_recovery") != {"console_inspection": "nas_dataflow_metadata"}
            or (os.name != "nt" and file.stat().st_mode & 0o077)):
        raise WorkflowError("existing query capture review changed")
    for source in proof["sources"]:
        if digest(contained(root, source["path"])) != source["sha256"]:
            raise WorkflowError("existing query capture original changed")
    return {**record, **proof}


def propose_capture(runtime: Any, task_id: str, diagnosis: str) -> dict[str, Any]:
    root = runtime.root
    path = contained(root / "inputs", diagnosis)
    diagnostic = read_json(path)
    with runtime.store.transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        proof = load(root, db, task_id)
        if (not task or task["state"] != "blocked" or not proof or proof.get("capture_recovery")
                or db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("pause and drain exact blocked existing query for one missing-field capture")
        source = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='glm' ORDER BY rowid DESC LIMIT 1",
                            (task_id,)).fetchone()
        sol = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='sol' ORDER BY rowid DESC LIMIT 1",
                         (task_id,)).fetchone()
        if (source["purpose"] != "continuation" or source["thread_id"] != proof["thread_id"]
                or sol["purpose"] != "review" or db.execute(
                    "SELECT 1 FROM verdicts WHERE task_id=? AND scene='R0036-QUERY-1-iframe' AND status='pass'",
                    (task_id,)).fetchone()):
            raise WorkflowError("original same-thread existing QUERY and independent review required")
        terminals = [terminal(root, job) for job in (source, sol)]
        output = root / "jobs" / source["id"] / "output"
        click = read_json(output / "R0036-QUERY-1-iframe.json")
        session = read_json(output / "self-created-session.json")
        binding = proof["binding"]
        if (click.get("actual_ui_click") is not True or click.get("mode") != "iframe"
                or click.get("session_id") != session["session_id"]
                or not isinstance(click.get("clicked_occurrence"), dict)
                or any((output / name).exists() for name in ("QUERY-cleanup-intent.json", "QUERY-cleanup-source.json"))
                or any(click.get(k) != proof[k] for k in ("target", "account"))
                or diagnostic.get("kind") != "ops_existing_dataflow_metadata_diagnostic"
                or diagnostic.get("formal_test") is not False or diagnostic.get("pass_added") is not False
                or diagnostic.get("cloud_writes") != 0 or diagnostic.get("dialog_cancelled") is not True
                or diagnostic.get("original_landing_unchanged") is not True or diagnostic.get("error")
                or diagnostic.get("source_evidence") != click["source_evidence"]
                or diagnostic.get("source_sha256") != click["source_sha256"]
                or diagnostic.get("session_id") != session["session_id"]
                or diagnostic.get("tooltips") != [binding["file_system_path"]]
                or binding["source_storage"] not in diagnostic.get("dialog_text", "")):
            raise WorkflowError("actual unchanged same-source click and successful native readonly diagnosis required")
        value = read_json(contained(root, click["source_evidence"]))
        references(root, proof, source["id"], session["session_id"], value["references"]["items"])
        sources = [{"path": p.relative_to(root).as_posix(), "sha256": digest(p)} for p in (
            path, contained(root, diagnostic["screenshot"]),
            path.with_name(path.stem + "-path.png"), output / "R0036-QUERY-1-iframe.json",
            contained(root, click["screenshot"]), contained(root, click["source_evidence"]),
            output / "self-created-session.json", contained(root, proof["evidence"]))]
        for entry in terminals:
            sources.extend([{"path": entry["path"], "sha256": entry["sha256"]},
                            {"path": entry["transport_path"], "sha256": entry["transport_sha256"]}])
        return {"kind": "ops_existing_query_capture_review", "approved": False, "task_id": task_id,
                "attempt": task["attempt"], "slot": proof["slot"], "target": proof["target"],
                "origin": proof["origin"], "thread_id": source["thread_id"], "original_job_id": source["id"],
                "original_turn_id": source["turn_id"], "session_id": session["session_id"],
                "previous_continuation_count": db.execute(
                    "SELECT count(*) FROM jobs WHERE task_id=? AND purpose='continuation'", (task_id,)).fetchone()[0],
                "capture_recovery": {"console_inspection": "nas_dataflow_metadata"},
                "wire": snapshot_wire(root, source["id"] + ":QUERY"), "sources": sources,
                "cloud_calls": 0, "pass_added": False}


def queue_capture(runtime: Any, evidence: str) -> dict[str, Any]:
    path = contained(runtime.root / "inputs", evidence)
    proof = read_json(path)
    if proof.get("approved") is not True:
        raise WorkflowError("independent capture review required")
    diagnosis = contained(runtime.root, proof["sources"][0]["path"]).relative_to(runtime.root / "inputs").as_posix()
    proposed = propose_capture(runtime, proof["task_id"], diagnosis)
    if {**proof, "approved": False} != proposed:
        raise WorkflowError("capture task/source/terminal/cumulative wire changed")
    with runtime.store.transaction() as db:
        task = db.execute("SELECT state,attempt FROM tasks WHERE id=?", (proof["task_id"],)).fetchone()
        if (not task or task["state"] != "blocked" or task["attempt"] != proof["attempt"]
                or db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("capture admission state changed")
        record = {"evidence": path.relative_to(runtime.root).as_posix(), "evidence_sha256": digest(path)}
        db.execute("INSERT INTO meta VALUES(?,?)", ("existing_query_capture:" + proof["task_id"], json.dumps(record)))
        load_capture(runtime.root, db, proof["task_id"])
        db.execute("UPDATE tasks SET state='continuation_pending',reason=NULL WHERE id=?", (proof["task_id"],))
        runtime.store.event(db, "ops_existing_query_capture_queued", {"task_id": proof["task_id"], **record})
    return {"queued": proof["task_id"], "no_cloud_calls": True, "no_glm_dispatched": True}
