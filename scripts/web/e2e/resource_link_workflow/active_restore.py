"""OPS recovery of a falsely closed, still-active Sol repair; never start a turn."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .files import WorkflowError, contained, digest, json_write, read_json
from .runtime import exclusive


def restore(runtime, job_id: str, evidence: str) -> dict:
    with exclusive(runtime.root / "jobs" / job_id / "launch.lock"):
        return _restore(runtime, job_id, evidence)


def _restore(runtime, job_id: str, evidence: str) -> dict:
    path = contained(runtime.root, evidence)
    raw = read_json(path)
    age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(raw["read_at"]).timestamp()
    with runtime.store.transaction() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (not job or job["role"] != "sol" or job["purpose"] != "repair" or job["state"] != "complete"
                or job["submission"] != "confirmed" or not 0 <= age < 60
                or db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or db.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or db.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
            raise WorkflowError("paused, drained, exact falsely closed original Sol repair required")
        exact = [t for t in raw.get("thread", {}).get("turns", []) if t.get("id") == job["turn_id"]]
        task = db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
        latest = db.execute("SELECT id FROM jobs WHERE task_id=? ORDER BY created DESC,rowid DESC",
                            (job["task_id"],)).fetchone()
        if (path.parent != runtime.root / "jobs" / job_id or not path.name.startswith("ops-thread-read-")
                or raw.get("method") != "thread/read" or raw.get("evidence_scope") != "exact_bound_turn_full"
                or raw.get("thread", {}).get("id") != job["thread_id"] or len(exact) != 1
                or exact[0].get("status") != "inProgress" or exact[0].get("itemsView") != "full"
                or any(raw.get("job_binding", {}).get(k) != job[k] for k in
                       ("id", "task_id", "role", "state", "thread_id", "turn_id", "submission"))
                or latest[0] != job_id or task["state"] != "blocked" or task["slot"] is None):
            raise WorkflowError("fresh full owning-backend active turn and retained task required")
        metadata = raw.get("original_responses", {}).get("metadata", {})
        if (metadata.get("method") != "thread/read"
                or metadata.get("params") != {"threadId": job["thread_id"], "includeTurns": False}
                or metadata.get("result", {}).get("thread", {}).get("id") != job["thread_id"]):
            raise WorkflowError("original owning-backend RPC evidence required")
        directory = runtime.root / "jobs" / job_id
        launch = read_json(directory / "launch.json")
        try:
            runtime.assert_process(launch["pid"], launch["identity"])
        except subprocess.CalledProcessError as error:
            if error.returncode != 1 or (error.stdout or "").strip():
                raise WorkflowError("cannot establish original adapter absence") from error
        else:
            raise WorkflowError("original adapter still live; do not attach a second bridge")
        trace = read_json(path)["transport_evidence"]
        events = [json.loads(line) for line in contained(runtime.root, trace["path"]).read_text().splitlines()]
        if any(e.get("method") == "turn/completed" and e.get("params", {}).get("turn", {}).get("id")
               == job["turn_id"] for e in events):
            raise WorkflowError("original trace already has a terminal event")
        marker = directory / "active-restore.json"
        if marker.exists():
            raise WorkflowError("active bridge recovery already submitted; reconcile, never repeat")
        adapter = Path(__file__).with_name("adapter.mjs")
        if not adapter.is_file():
            raise WorkflowError("original adapter script is unavailable")
        proof = {"job_id": job_id, "thread_id": job["thread_id"], "turn_id": job["turn_id"],
                 "evidence": evidence, "sha256": digest(path), "no_new_turn": True, "cloud_quota_added": 0}
        json_write(marker, proof, private=True)
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
        db.execute("UPDATE tasks SET state='repairing',reason=? WHERE id=?",
                   ("OPS restored falsely closed original active Sol repair; no new turn", job["task_id"]))
        runtime.store.event(db, "ops_false_terminal_restored", proof)
    with (directory / "active-restore.log").open("ab") as log:
        os.chmod(directory / "active-restore.log", 0o600)
        process = subprocess.Popen([runtime.config.get("node", "node"),
                                    str(adapter),
                                    str(runtime.root), job_id, "--attach"], cwd=runtime.config["repo"],
                                   stdout=log, stderr=log, start_new_session=os.name != "nt")
    json_write(directory / "active-restore-launch.json", {**proof, "pid": process.pid,
                "identity": runtime.process_identity(process.pid)}, private=True)
    return {"original_active_job_restored": job_id, "pid": process.pid, "no_new_turn": True,
            "cloud_quota_added": 0, "pass_added": False}
