"""Single local Server; SQLite is durable, HTTP and model adapters are disposable."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import worker_hosts
from .files import WorkflowError, contained, digest, json_write, read_json
from .runtime import Runtime, exclusive
from .store import Store

SERVER_LOADED_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class Service:
    def __init__(self, store: Store):
        self.store = store
        self.runtime = Runtime(store)
        self.check_adapter_owners()

    def check_adapter_owners(self) -> None:
        # Recheck adopted/restored owners on existing control reads, without a
        # worker deadline or a second scheduler. The launch lock also covers
        # marker writes, so an in-flight launch/restore is never parked here.
        with self.store.transaction() as connection:
            jobs = connection.execute("SELECT id FROM jobs WHERE state IN ('queued','running','submitted')").fetchall()
        for candidate in jobs:
            directory = self.store.root / "jobs" / candidate["id"]
            marker = directory / "launch.json"
            restored = directory / "active-restore.json"
            if not marker.exists() and not restored.exists():
                continue  # Reserved but never launched: explicit same-job launch is safe.
            with ExitStack() as stack:
                try:
                    stack.enter_context(exclusive(directory / "launch.lock"))
                except WorkflowError as error:
                    if isinstance(error.__cause__, (BlockingIOError, PermissionError)):
                        continue
                    raise
                with self.store.transaction() as connection:
                    job = connection.execute("SELECT * FROM jobs WHERE id=?", (candidate["id"],)).fetchone()
                if not job or job["state"] not in {"queued", "running", "submitted"}:
                    continue
                try:
                    if restored.exists():
                        proof = read_json(restored)
                        launch = read_json(directory / "active-restore-launch.json")
                        for field, key in (("job_id", "id"), ("thread_id", "thread_id"), ("turn_id", "turn_id")):
                            if proof.get(field) != job[key] or launch.get(field) != job[key]:
                                raise WorkflowError("restored adapter binding mismatch")
                    else:
                        launch = json.loads(marker.read_text(encoding="utf-8"))
                    self.runtime.assert_process(launch["pid"], launch["identity"])
                except (KeyError, OSError, ValueError, WorkflowError, subprocess.SubprocessError):
                    self.store.park(
                        job["id"], "adapter owner no longer live; exact reconcile before retry", only_active=True
                    )

    def attest_control(self) -> dict[str, Any]:
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != SERVER_LOADED_SHA:
            raise WorkflowError("Server code changed on disk; restart the same Server")
        return self.runtime.attest_control()

    def _reviewed_continuation(self, connection: sqlite3.Connection, job: sqlite3.Row) -> bool:
        """Allow only the one queued job bound to the registered original-generation review."""
        return self.store.reviewed_continuation(connection, job)

    def launch(self, job_id: str) -> dict[str, Any]:
        self.attest_control()
        with exclusive(self.store.root / "jobs" / job_id / "launch.lock"):
            with self.store.transaction() as connection:
                job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not job or job["state"] != "queued" or job["submission"] != "not_submitted":
                    raise WorkflowError("job launch is not repeatable; reconcile precise thread/turn first")
                if self.store.review_only_reason(connection) and job["purpose"] != "cleanup" and (
                    job["role"] != "sol" or job["purpose"] not in {"review", "repair"}
                ) and not self._reviewed_continuation(connection, job):
                    raise WorkflowError("review-only hold forbids this job launch")
                marker = self.store.root / "jobs" / job_id / "launch.json"
                if marker.exists():
                    raise WorkflowError("existing launch marker: do not retry an unknown submission")
                selected = worker_hosts.binding(self.runtime.config, job["role"])
                if selected["host"] == "macmini":
                    link = worker_hosts.require_tunnel(self.runtime, connection=connection)
                    selected["link_fingerprint"] = link["fingerprint"]
                if job["thread_id"]:
                    for previous in connection.execute(
                        "SELECT id,role FROM jobs WHERE thread_id=? AND id!=?", (job["thread_id"], job_id)
                    ):
                        row = connection.execute("SELECT value FROM meta WHERE key=?",
                                                 ("job_backend:" + previous["id"],)).fetchone()
                        owner = json.loads(row[0]) if row else worker_hosts.binding(
                            {**self.runtime.config, "worker_host": "local"}, previous["role"]
                        )
                        keys = ("id", "endpoint", "host", "link_fingerprint")
                        if any(owner.get(key) != selected.get(key) for key in keys):
                            raise WorkflowError(
                                "original model thread belongs to another backend; do not resume elsewhere"
                            )
                if selected["host"] == "macmini":
                    selected["cwd"] = selected["cwd"].rstrip("/") + "/" + job_id
                    worker_hosts.remote_chrome(self.runtime.config, {
                        "operation": "workspace", "cwd": selected["cwd"],
                        "profile": worker_hosts.mini(self.runtime.config)["chrome_profile"],
                    })
                connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                                   ("job_backend:" + job_id, json.dumps(selected)))
                json_write(marker, {"job_id": job_id, "thread_id": job["thread_id"], "role": job["role"],
                                    "worker_backend": selected}, private=True)
            environment = self.runtime.config
            argv = [
                environment.get("node", "node"),
                str(Path(__file__).with_name("adapter.mjs")),
                str(self.store.root),
                job_id,
            ]
            logfile = self.store.root / "jobs" / job_id / "adapter.log"
            with logfile.open("ab") as log:
                os.chmod(logfile, 0o600)
                try:
                    process = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=os.name != "nt")
                except OSError:
                    marker.unlink()  # Popen rejected before creating a child; do not invent an unknown model turn.
                    raise
            try:
                identity = self.runtime.process_identity(process.pid)
            except (WorkflowError, subprocess.SubprocessError):
                self.store.park(job_id, "adapter exited before PID identity recorded; reconcile raw transport")
            else:
                json_write(
                    marker,
                    {"job_id": job_id, "thread_id": job["thread_id"], "role": job["role"],
                     "worker_backend": selected,
                     "pid": process.pid, "identity": identity},
                    private=True,
                )
            threading.Thread(target=self.watch, args=(process, job_id), daemon=True).start()
            return {"job_id": job_id, "adapter_pid": process.pid, "launched": True}

    def watch(self, process: subprocess.Popen, job_id: str) -> None:
        process.wait()
        with self.store.transaction() as connection:
            job = connection.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job and job[0] != "complete":
            self.store.park(
                job_id, "adapter ended without exact terminal confirmation; read-only reconcile required",
                only_active=True,
            )

    def request(self, body: dict[str, Any]) -> Any:
        operation, arguments = body["operation"], body.get("arguments", {})
        if "job_id" in body:
            job, token = body["job_id"], body["token"]
            if operation == "_bind":
                self.store.bind(job, token, arguments["thread"], arguments.get("turn"))
                return {"bound": True}
            if operation == "_submission_boundary":
                self.store.mark_submission(job, token)
                return {"recorded": True}
            if operation == "_terminal":
                with self.store.transaction() as connection:
                    capability = connection.execute("SELECT capability FROM jobs WHERE id=?", (job,)).fetchone()
                    if not capability or not secrets.compare_digest(
                        capability[0], hashlib.sha256(token.encode()).hexdigest()
                    ):
                        raise WorkflowError("invalid terminal job capability")
                self.store.finish_job(job, arguments["thread"], arguments["turn"], arguments["status"])
                from .reports import render_results

                repo = self.runtime.config.get("repo")
                if repo:
                    from . import reports

                    if hashlib.sha256(Path(reports.__file__).read_bytes()).hexdigest() != reports.LOADED_SHA:
                        # Keep exact terminal bookkeeping, but never overwrite
                        # the new report with this interpreter's cached renderer.
                        return {"finished": True, "report_refresh_required": True, "requires_ops": True}
                    render_results(
                        self.store, Path(repo) / "scripts/web/e2e/resource_link_workflow/resource-link-results.html"
                    )
                return {"finished": True}
            return self.runtime.tool(job, token, operation, arguments)
        if operation == "status":
            # Maintenance must remain observable even before the same instance
            # reloads its code. Readiness is distinct from HTTP liveness.
            try:
                control = self.attest_control()
                self.check_adapter_owners()
                control["ready"] = True
            except WorkflowError as error:
                control = {"pid": os.getpid(), "ready": False, "disk_matches_loaded": False,
                           "requires_ops": True, "reason": str(error)}
            return {**self.store.status(), "control_runtime": control,
                    "worker_host": worker_hosts.host(self.runtime.config), "web_host": "local", "sol_host": "local"}
        if operation == "job-binding":
            # OPS reads the durable binding, including completed jobs. Launch markers
            # describe the pre-submission process, not a newly allocated model thread.
            with self.store.transaction() as connection:
                job = connection.execute(
                    "SELECT id,task_id,role,state,thread_id,turn_id,submission FROM jobs WHERE id=?",
                    (arguments["job_id"],),
                ).fetchone()
                backend = connection.execute("SELECT value FROM meta WHERE key=?",
                                             ("job_backend:" + arguments["job_id"],)).fetchone()
            if not job:
                raise WorkflowError("unknown job binding")
            if backend:
                owner = json.loads(backend[0])
                if owner["host"] == "macmini":
                    link = worker_hosts.require_tunnel(self.runtime)
                    if link["fingerprint"] != owner["link_fingerprint"]:
                        raise WorkflowError("restore the original owned Mac mini link before reading this job")
            if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != SERVER_LOADED_SHA:
                raise WorkflowError("reader changed on disk; restart the same Server")
            return {**dict(job), **({"worker_backend": json.loads(backend[0])} if backend else {}), "reader_runtime": {
                "pid": os.getpid(), "path": str(Path(__file__).resolve()), "loaded_sha256": SERVER_LOADED_SHA,
            }}
        if operation == "next":
            try:
                self.attest_control()
            except WorkflowError as error:
                # Do not issue actions from stale code, and do not mistake an
                # OPS reload window for a failed Server or terminal model job.
                return {"type": "wait", "actions": [], "maintenance": True,
                        "requires_ops": True, "reason": str(error)}
            self.check_adapter_owners()
            return self.store.next_action()
        if operation == "execute":
            self.attest_control()
            with self.store.transaction() as connection:
                action = connection.execute("SELECT * FROM actions WHERE id=?", (arguments["action_id"],)).fetchone()
                available = self.store.available_slots(connection)
                free = available[0] if available else None
            if action and action["state"] == "issued" and action["kind"] == "dispatch":
                if not free:
                    raise WorkflowError("no free execution slot")
                try:
                    self.runtime.preflight(dict(free))
                except (WorkflowError, OSError, ValueError, subprocess.SubprocessError) as error:
                    # Raw process/network exceptions may contain private URLs or
                    # stderr; only our bounded WorkflowError messages are public.
                    reason = str(error) if isinstance(error, WorkflowError) else (
                        f"dispatch preflight failed ({type(error).__name__}); OPS inspect the same slot environment"
                    )
                    self.store.defer_dispatch(arguments["action_id"], free["id"], reason)
                    return {"dispatch_deferred": True, "requires_ops": True, "slot": free["id"],
                            "job_reserved": False, "reason": reason}
            if action and action["state"] == "issued" and action["kind"] in {"navigation", "continuation"}:
                with self.store.transaction() as connection:
                    task = connection.execute("SELECT * FROM tasks WHERE id=?", (action["task_id"],)).fetchone()
                    if action["kind"] == "continuation":
                        from . import existing_query, native_continuation

                        recovery = existing_query.load(self.store.root, connection, task["id"]) or (
                            native_continuation.load(self.store.root, connection, task["id"]))
                        recovery = recovery.get("capture_recovery") or recovery
                    else:
                        recovery = self.store.navigation_recovery(connection, task)
                    slot = connection.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
                self.runtime.preflight(dict(slot))
                snapshot = self.runtime.browser({"operation": "snapshot", "origin": slot["origin"],
                                                 "target": slot["target"]})
                if snapshot.get("busy") or snapshot.get("session_id") != recovery["session_id"]:
                    raise WorkflowError("original navigation session changed; OPS recovery required")
            result = self.store.execute(arguments["action_id"])
            if result.get("job_id"):
                result.update(self.launch(result["job_id"]))
            return result
        if operation == "launch":
            return self.launch(arguments["job_id"])
        if operation == "pause":
            if not arguments["paused"]:
                self.attest_control()
            self.store.pause(arguments["paused"])
            return {"paused": arguments["paused"]}
        if operation == "review-only":
            self.store.set_review_only(arguments["reason"])
            return {"review_only": arguments["reason"]}
        if operation == "dispatch-enable":
            self.store.set_review_only(None)
            return {"review_only": None}
        if operation == "slot-recover":
            self.attest_control()
            with self.store.transaction() as connection:
                slot = connection.execute("SELECT * FROM slots WHERE id=?", (arguments["slot"],)).fetchone()
                if not slot or slot["task_id"] is not None:
                    raise WorkflowError("slot recovery requires the original idle slot")
            self.runtime.preflight(dict(slot))
            self.store.recover_slot(arguments["slot"])
            return {"slot_recovered": arguments["slot"], "no_model_or_cloud_call": True}
        if operation == "import":
            self.attest_control()
            return {"plan_id": self.store.import_plan(arguments["plan"], arguments.get("budget_cents", 0),
                                                     arguments.get("successor_review"))}
        raise WorkflowError("unknown scheduler operation")


def serve(root: Path, port: int = 16099) -> None:
    store = Store(root)
    store.check()
    with exclusive(root / "server.lock"):
        service = Service(store)
        token = secrets.token_urlsafe(40)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass  # No URLs, capabilities, prompts or private roots in access logs.

            def do_POST(self) -> None:
                if self.path not in {"/control", "/job"} or not secrets.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + token
                ):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2_000_000:
                        raise WorkflowError("invalid request size")
                    body = json.loads(self.rfile.read(size))
                    if self.path == "/job" and "job_id" not in body:
                        raise WorkflowError("worker cannot use scheduler APIs")
                    result = service.request(body)
                    status = 200
                except WorkflowError as error:
                    result, status = {"error": str(error)}, 409
                except (ValueError, KeyError, OSError, subprocess.SubprocessError):
                    result, status = {"error": "operation rejected; inspect local state and guide for exact gate"}, 409
                encoded = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        json_write(
            root / "server.json",
            {
                "schema_version": 1,
                "origin": f"http://127.0.0.1:{http.server_port}",
                "token": token,
                "pid": os.getpid(),
                "identity": Runtime.process_identity(os.getpid()),
            },
            private=True,
        )
        try:
            http.serve_forever()
        finally:
            http.server_close()


def reconcile(store: Store, job_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """OPS supplies a freshly fetched app-server thread/read response; never model self-report."""
    thread = raw.get("thread", {})
    with store.transaction() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job and job["purpose"] == "continuation" and job["state"] != "unknown":
        raise WorkflowError("continuation reconciliation requires its current unknown lease")
    if job and (not job["thread_id"] or not job["turn_id"]):
        trace = store.root / "jobs" / job_id / "raw-rollout.jsonl"
        trace_state = (
            "missing" if not trace.exists() else "present" if trace.read_text(encoding="utf-8").strip() else "empty"
        )
        pre_context = trace_state in {"missing", "empty"} and (
            job["role"] == "sol" and job["thread_id"] and not job["turn_id"]
            and job["submission"] == "not_submitted"
            and raw.get("evidence_scope") == "all_turns_full_paginated_files"
            and raw.get("transport_evidence") == {
                "path": f"jobs/{job_id}/raw-rollout.jsonl", "state": trace_state,
            }
            and all(raw.get("job_binding", {}).get(key) == job[key]
                    for key in ("id", "role", "thread_id", "turn_id", "submission"))
        )
        if trace_state != "present" and not pre_context:
            raise WorkflowError("no transport trace; do not invent thread/turn or submit again")
        records = [] if pre_context else [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        threads = [item["params"]["threadId"] for item in records if item["method"] == "transport/thread_created"]
        turns = [item["params"] for item in records if item["method"] == "transport/turn_created"]
        # A persisted thread/resume can fail (e.g. desktop holds its writer). The
        # adapter never reaches turn/start before recording thread_created. Only
        # a dead adapter + a fresh full backend read can establish this no-turn case.
        # A policy rejection after resume/bind is also before turn/start. Do not
        # generalize this to arbitrary adapter errors or RPC-unknown submissions.
        policy_rejected = job["submission"] == "thread_bound" and any(
            item["method"] == "transport/adapter_error"
            and item["params"].get("message") in {
                "backend did not confirm read-only/no-network/never policy",
                "backend did not confirm job-workspace/script-network/never policy",
            }
            for item in records
        )
        before_start = not threads or (policy_rejected and threads[-1] == job["thread_id"])
        if job["thread_id"] and before_start and not turns and raw.get("method") == "thread/read":
            from datetime import datetime, timezone

            if raw.get("evidence_scope") == "all_turns_full_paginated_files":
                # Independently reload each unchanged full owning-backend RPC.
                # Summaries in the index never establish absence of job input.
                original = raw.get("original_responses", {})
                metadata = original.get("metadata", {})
                if (metadata.get("method") != "thread/read"
                        or metadata.get("params") != {"threadId": job["thread_id"], "includeTurns": False}
                        or metadata.get("result", {}).get("thread", {}).get("id") != job["thread_id"]):
                    raise WorkflowError("full paginated owning metadata required")
                pages = original.get("pages", [])
                if not pages or len(pages) % 2:
                    raise WorkflowError("complete original full pagination required")
                recovered, seen, cursor = [], set(), None
                for index in range(0, len(pages), 2):
                    listing, binding = pages[index:index + 2]
                    expected = {"threadId": job["thread_id"], "limit": 1,
                                "sortDirection": "desc", "itemsView": "notLoaded"}
                    if cursor:
                        expected["cursor"] = cursor
                    if listing.get("method") != "thread/turns/list" or listing.get("params") != expected:
                        raise WorkflowError("original full pagination chain differs")
                    page_path = contained(store.root, binding.get("path", ""))
                    if (page_path.parent != (store.root / "jobs" / job_id).resolve()
                            or digest(page_path) != binding.get("sha256")):
                        raise WorkflowError("original full turn file changed")
                    full = read_json(page_path)
                    expected["itemsView"] = "full"
                    data = full.get("result", {}).get("data", [])
                    listed = listing.get("result", {}).get("data", [])
                    if (full.get("method") != "thread/turns/list" or full.get("params") != expected
                            or len(data) != 1 or len(listed) != 1 or data[0].get("id") != listed[0].get("id")
                            or data[0].get("itemsView") != "full" or not isinstance(data[0].get("items"), list)
                            or data[0].get("id") in seen or job_id in json.dumps(data[0])):
                        raise WorkflowError("full original turn unavailable, duplicated, or contains this job")
                    seen.add(data[0]["id"])
                    recovered.append({"id": data[0]["id"], "status": data[0]["status"]})
                    cursor = listing["result"].get("nextCursor")
                    if full["result"].get("nextCursor") != cursor or (
                        index + 2 < len(pages) and (not isinstance(cursor, str) or not cursor)
                    ) or (index + 2 == len(pages) and cursor is not None):
                        raise WorkflowError("original history pagination incomplete")
                if thread.get("turns") != recovered:
                    raise WorkflowError("original full turn index differs")

            age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(raw["read_at"]).timestamp()
            launch = json.loads((store.root / "jobs" / job_id / "launch.json").read_text(encoding="utf-8"))
            if not 0 <= age < 60 or thread.get("id") != job["thread_id"] or not thread.get("turns"):
                raise WorkflowError("fresh full existing-thread read required")
            try:
                Runtime(store).assert_process(launch["pid"], launch["identity"])
            except WorkflowError:
                pass
            except subprocess.CalledProcessError as error:
                if error.returncode != 1 or (error.stdout or "").strip():
                    raise WorkflowError("cannot establish adapter absence") from error
            else:
                raise WorkflowError("adapter still live; no-turn reconciliation forbidden")
            if any(t.get("status") not in {"completed", "failed", "interrupted"} for t in thread["turns"]):
                raise WorkflowError("thread has an active or unknown turn")
            if job_id in json.dumps(thread["turns"]):
                raise WorkflowError("job input exists in backend; resolve exact turn instead")
            continuation_retry = (
                job["role"] == "glm" and job["purpose"] == "continuation"
                and job["state"] == "unknown" and job["submission"] == "not_submitted"
                and any(item["method"] == "transport/adapter_error"
                        and item["params"].get("message") == "review-only hold forbids new execution/preparation turns"
                        for item in records)
                and not any(item["method"] in {"transport/thread_created", "transport/turn_created", "item/tool/call"}
                            for item in records)
            )
            if continuation_retry:
                from scripts.web.e2e.resource_link_workflow import execution_gate as gate

                launch_path = store.root / "jobs" / job_id / "launch.json"
                launch = read_json(launch_path)
                if (launch.get("job_id") != job_id or launch.get("thread_id") != job["thread_id"]
                        or launch.get("role") != "glm" or not launch.get("pid") or not launch.get("identity")):
                    raise WorkflowError("exact failed continuation launch marker required")
                with store.transaction() as connection:
                    current = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                    if (not store.review_only_reason(connection)
                            or connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                            or not store.reviewed_continuation(connection, current)):
                        raise WorkflowError("paused exact OPS continuation review required for safe retry")
                    from . import native_continuation
                    from .existing_query import load as existing_query_load

                    if existing_query_load(store.root, connection, job["task_id"]):
                        raise WorkflowError("existing query unknown submission requires exact backend reconciliation")

                    recovery = native_continuation.load(store.root, connection, job["task_id"])
                    proof = read_json(contained(store.root, recovery["evidence"]))
                    wire_uri = f"{(store.root / 'execution-gate.sqlite3').as_uri()}?mode=ro"
                    with sqlite3.connect(wire_uri, uri=True) as wire:
                        wire.row_factory = sqlite3.Row
                        active = wire.execute("SELECT * FROM active WHERE scope=?", (proof["scope"],)).fetchone()
                        attempts = wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid",
                                                (proof["scope"],)).fetchall()
                        grants = wire.execute("SELECT * FROM grants WHERE scope=? ORDER BY id",
                                              (proof["scope"],)).fetchall()
                        extension = wire.execute("SELECT * FROM grants WHERE scope=? AND id=?",
                                                 (proof["scope"], proof["grant_id"])).fetchone()
                        limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                             (proof["authority"], proof["grant_id"])).fetchone()
                        preserved = [
                            {"id": row["id"], "sha256": gate.nas_native_snapshot(row)} for row in attempts
                        ]
                        if (not active or active["session"] != proof["session"]
                                or active["account"] != proof["account"] or active["phase"] != "execute"
                                or (active["expires"] is not None and active["expires"] <= time.time())
                                or preserved != proof["preserved_attempts"] or not extension
                                or extension["kind"] != "identity" or extension["quota"] != 1
                                or extension["used"] != 0 or extension["authority"] != proof["authority"]
                                or gate.canonical(json.loads(extension["call"]))
                                != gate.canonical(proof["identity_call"])
                                or not limit or limit[0] != 0
                                or len(grants) != len(proof["preserved_grants"]) + 1):
                            raise WorkflowError("original continuation wire changed; safe retry refused")
                        for expected in proof["preserved_grants"]:
                            actual = next((row for row in grants if row["id"] == expected["id"]), None)
                            actual_limit = wire.execute(
                                "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                (proof["authority"], expected["id"]),
                            ).fetchone()
                            if (not actual or actual["used"] != expected["used"]
                                    or actual["authority"] != proof["authority"]
                                    or not actual_limit or actual_limit[0] != actual["used"]
                                    or gate.nas_native_snapshot({k: v for k, v in dict(actual).items() if k != "used"})
                                    != expected["definition_sha256"]):
                                raise WorkflowError("original continuation grant changed; safe retry refused")
                    archived = launch_path.with_name(f"launch-not-started-{int(time.time())}.json")
                    if archived.exists():
                        raise WorkflowError("failed launch evidence archive already exists")
                    launch_path.rename(archived)
                    connection.execute(
                        "UPDATE jobs SET state='queued',submission='not_submitted' WHERE id=? AND state='unknown'",
                        (job_id,),
                    )
                    store.event(connection, "native_continuation_no_turn_requeued", {
                        "job_id": job_id, "archived_launch": archived.relative_to(store.root).as_posix(),
                        "read_evidence_sha256": hashlib.sha256(
                            json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
                        ).hexdigest(), "wire_unchanged": True, "no_turn_or_tool_input": True,
                    })
                return {"reconciled": job_id, "terminal": "not_started", "same_job_requeued": True,
                        "wire_unchanged": True, "new_turn_submitted": False}
            with store.transaction() as connection:
                connection.execute("UPDATE jobs SET state='complete',submission='not_started' WHERE id=?", (job_id,))
                retry_safe = False
                gate = store.root / "execution-gate.sqlite3"
                if job["role"] == "glm" and policy_rejected and gate.is_file():
                    # No turn is not a cleanup receipt. Release only if the
                    # independent wire journal has no capability/call for this
                    # job and the task owns no outstanding cloud resource.
                    with sqlite3.connect(f"{gate.as_uri()}?mode=ro", uri=True) as wire:
                        prefix = job_id + ":%"
                        touched = wire.execute("SELECT 1 FROM attempts WHERE scope LIKE ?", (prefix,)).fetchone()
                        active = wire.execute("SELECT 1 FROM active WHERE scope LIKE ?", (prefix,)).fetchone()
                    outstanding = connection.execute(
                        "SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (job["task_id"],)
                    ).fetchone()
                    retry_safe = not touched and not active and not outstanding
                connection.execute(
                    "UPDATE tasks SET state='blocked',reason=? WHERE id=?",
                    ("OPS confirmed failed resume before turn/start; no model or cloud execution", job["task_id"]),
                )
                if retry_safe:
                    task = connection.execute("SELECT packet FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
                    packet = json.loads((store.root / task["packet"]).read_text(encoding="utf-8"))
                    connection.execute(
                        "UPDATE budget_reservations SET amount=MAX(0,amount-?) WHERE task_id=?",
                        (packet.get("budget_cents", 0), job["task_id"]),
                    )
                    from .hourly_budget import release_verified

                    release_verified(store, connection, job["task_id"], {
                        "kind": "proved_no_execution", "job_id": job_id,
                        "evidence": {"raw_sha256": hashlib.sha256(
                            json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
                        ).hexdigest(), "read_at": raw["read_at"], "thread_id": thread["id"]},
                    })
                    connection.execute("UPDATE slots SET task_id=NULL WHERE task_id=?", (job["task_id"],))
                    connection.execute("DELETE FROM locks WHERE task_id=?", (job["task_id"],))
                    connection.execute("UPDATE tasks SET state='ready',slot=NULL WHERE id=?", (job["task_id"],))
                store.event(connection, "no_turn_confirmed", {"job_id": job_id, "thread": thread["id"]})
            return {"reconciled": job_id, "terminal": "not_started", "not_accepted": True, "retry_safe": retry_safe}
        if not threads or not turns or turns[-1]["threadId"] != threads[-1] or thread.get("id") != threads[-1]:
            raise WorkflowError("unconfirmed submission cannot be rebound; preserve lease for OPS investigation")
        with store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET thread_id=?,turn_id=? WHERE id=?", (threads[-1], turns[-1]["turnId"], job_id)
            )
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or thread.get("id") != job["thread_id"] or not job["turn_id"]:
        raise WorkflowError("unknown submission requires manual exact thread/turn resolution; never new again")
    turns = [turn for turn in thread.get("turns", []) if turn.get("id") == job["turn_id"]]
    if len(turns) != 1 or turns[0]["status"] not in {"completed", "interrupted", "failed"}:
        raise WorkflowError("exact turn is not terminal")
    store.finish_job(job_id, thread["id"], job["turn_id"], turns[0]["status"], terminal_raw=raw)
    return {"reconciled": job_id, "terminal": turns[0]["status"]}
