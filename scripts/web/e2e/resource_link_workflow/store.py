"""SQLite authority. Models never write this database or advance the queue themselves."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from itertools import combinations
from pathlib import Path
from typing import Any, Iterator

from .files import WorkflowError, contained, digest, json_write, read_json
from .ownership import owner_key, register_generation
from .plans import (
    expected_scenes,
    readonly_retest_dimensions,
    retest_dimensions,
    validate_plan,
    validate_review_lineage,
)
from .remaining_review import REVIEW_KIND, SCOPE_KIND, reviewed_targets

STORE_LOADED_SHA = digest(Path(__file__))
DATA_DIRECTORY = Path(__file__).resolve().parent

DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY,canonical TEXT UNIQUE NOT NULL,identity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,plan TEXT NOT NULL,state TEXT NOT NULL,budget INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY,run_id TEXT NOT NULL,resource_id TEXT NOT NULL,spec TEXT NOT NULL,
 state TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,slot INTEGER,packet TEXT,reason TEXT,
 UNIQUE(run_id,resource_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_resource_owner ON tasks(resource_id) WHERE state NOT IN ('complete','superseded');
CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY,origin TEXT UNIQUE NOT NULL,cwd TEXT UNIQUE NOT NULL,
 task_id TEXT UNIQUE,target TEXT,web_pid INTEGER,web_identity TEXT);
CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,task_id TEXT,kind TEXT NOT NULL,
 state TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_action ON actions(state) WHERE state='issued';
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,role TEXT NOT NULL,
 purpose TEXT NOT NULL,state TEXT NOT NULL,thread_id TEXT,turn_id TEXT,capability TEXT NOT NULL,
 submission TEXT NOT NULL,created REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_sol ON jobs(role)
 WHERE role='sol' AND state IN ('queued','running','unknown','submitted');
CREATE UNIQUE INDEX IF NOT EXISTS one_task_job ON jobs(task_id)
 WHERE state IN ('queued','running','unknown','submitted');
CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY,task_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget_reservations(task_id TEXT PRIMARY KEY,amount INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,attempt INTEGER NOT NULL,
 scene TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inspected(job_id TEXT NOT NULL,artifact TEXT NOT NULL,sha TEXT NOT NULL,
 PRIMARY KEY(job_id,artifact));
CREATE TABLE IF NOT EXISTS verdicts(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,scene TEXT NOT NULL,
 status TEXT NOT NULL,receipt TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS owned(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,dimension TEXT NOT NULL,
 resource TEXT NOT NULL,state TEXT NOT NULL,evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "state.sqlite3"
        if not self.path.exists():
            raise WorkflowError("state not initialized; use init, never silently create replacement state")

    @classmethod
    def initialize(cls, root: Path, *, slots: int = 4, base_port: int = 16100) -> Store:
        if not 4 <= slots <= 16 or not 1024 <= base_port <= 65535 - slots:
            raise WorkflowError("at least four slots and valid ports are required")
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        path = root / "state.sqlite3"
        if path.exists():
            store = cls(root)
            store.check()
            return store
        connection = sqlite3.connect(path)
        try:
            # Finish the persistent mode transition before returning a store to
            # concurrent slot requests; racing first connections can SQLITE_BUSY.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(DDL)
            connection.execute("INSERT INTO meta VALUES('schema','1')")
            connection.execute("INSERT INTO meta VALUES('revision','0')")
            connection.execute("INSERT INTO meta VALUES('paused','true')")
            connection.execute("INSERT INTO meta VALUES('sol_thread','')")
            for index in range(slots):
                cwd = root / "slots" / str(index) / "workspace"
                cwd.mkdir(parents=True)
                connection.execute(
                    "INSERT INTO slots(id,origin,cwd) VALUES(?,?,?)",
                    (index, f"http://127.0.0.1:{base_port + index}", str(cwd)),
                )
            connection.commit()
        finally:
            connection.close()
        os.chmod(path, 0o600)
        return cls(root)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] != "1":
                raise WorkflowError("state schema is incompatible; preserve database and migrate under OPS lock")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def check(self) -> None:
        with self.transaction() as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise WorkflowError("database integrity failure")

    @staticmethod
    def event(connection: sqlite3.Connection, kind: str, data: Any) -> None:
        connection.execute("INSERT INTO events(kind,data,created) VALUES(?,?,?)", (kind, dumps(data), time.time()))
        connection.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def import_plan(self, plan: dict[str, Any], budget_cents: int = 0, successor_review: str | None = None) -> str:
        validate_plan(plan)
        if isinstance(budget_cents, bool) or not isinstance(budget_cents, int) or budget_cents < 0:
            raise WorkflowError("budget must be nonnegative integer cents")
        with self.transaction() as connection:
            excluded = validate_review_lineage(plan)
            reviewed = {}
            if lineage := plan.get("review_lineage"):
                original_review = read_json(Path(lineage["path"]))
                if original_review.get("kind") == REVIEW_KIND:
                    reviewed = reviewed_targets(original_review)
            for item in excluded:
                resource_id = item.get("resource_id", item["scene_id"].split("-", 1)[0])
                registry = read_json(DATA_DIRECTORY / "catalog.json")
                candidates = [r for r in registry["resources"] if r["id"] == resource_id]
                candidates.extend(json.loads(row[0]) for row in connection.execute(
                    "SELECT spec FROM tasks WHERE resource_id=? ORDER BY rowid", (resource_id,),
                ))
                if not any(f"{contract['id']}-{mode}" == item["scene_id"] and status == item["status"]
                           for candidate in candidates for source in candidate["dimensions"].values()
                           for contract in source["contracts"] for mode, status in contract["historical"].items()):
                    raise WorkflowError("excluded retest scene lacks an authoritative positive checkpoint")
            old = connection.execute("SELECT plan,budget FROM runs WHERE id=?", (plan["plan_id"],)).fetchone()
            if old:
                if old["plan"] != dumps(plan) or old["budget"] != budget_cents:
                    raise WorkflowError("a frozen plan cannot be edited; create a new plan ID")
                if successor_review:
                    from .plan_handoff import verify_recorded_handoff

                    verify_recorded_handoff(self, connection, plan, successor_review)
                return plan["plan_id"]
            if successor_review:
                from .plan_handoff import handoff_readonly_plan

                handoff_readonly_plan(self, connection, plan, successor_review)
            connection.execute(
                "INSERT INTO runs VALUES(?,?,?,?)", (plan["plan_id"], dumps(plan), "active", budget_cents)
            )
            for spec in plan["resources"]:
                if "retest_scope" in spec:
                    registry = read_json(DATA_DIRECTORY / "catalog.json")
                    canonical = next((item for item in registry["resources"] if item["id"] == spec["id"]), None)
                    if canonical is None or any(canonical[key] != spec[key] for key in ("key", "service", "name")):
                        raise WorkflowError("targeted retest needs the permanent catalog identity")
                    prior_specs = [canonical] + [json.loads(row[0]) for row in connection.execute(
                        "SELECT spec FROM tasks WHERE resource_id=? ORDER BY rowid", (spec["id"],),
                    )]
                    for prior in prior_specs:
                        for dimension, source in prior["dimensions"].items():
                            current = spec["dimensions"][dimension]
                            if source.get("creation_verified") and not current.get("creation_verified"):
                                raise WorkflowError("targeted retest must preserve creation checkpoints")
                            for original in source["contracts"]:
                                positive = {mode: status for mode, status in original["historical"].items()
                                            if status in {"pass", "unsupported"}}
                                if not positive:
                                    continue
                                frozen = next((c for c in current["contracts"] if c["id"] == original["id"]
                                               and c["source_type"] == original["source_type"]), None)
                                if frozen is None or any(frozen["historical"][mode] != status
                                                         for mode, status in positive.items()):
                                    raise WorkflowError("targeted retest must preserve all positive checkpoints")
                    for scene_id in spec["retest_scope"]["scene_ids"]:
                        mode = scene_id.rsplit("-", 1)[-1]
                        contract_id = scene_id.rsplit("-", 1)[0]
                        dimension = next(d for d, source in spec["dimensions"].items()
                                         if any(c["id"] == contract_id for c in source["contracts"]))
                        frozen = next(c for c in spec["dimensions"][dimension]["contracts"] if c["id"] == contract_id)
                        originals = [c for prior in prior_specs for c in prior["dimensions"][dimension]["contracts"]
                                     if c["id"] == contract_id and c["source_type"] == frozen["source_type"]]
                        if "historical_binding" in frozen:
                            from .remaining_review import historical_contract

                            originals.insert(0, historical_contract(spec, dimension, frozen))
                        approved_status = reviewed.get(scene_id, {}).get("baseline_status", "fail")
                        if (not originals or originals[-1]["historical"][mode] != approved_status
                                or any(c["historical"][mode] in {"pass", "unsupported"} for c in originals)
                                or frozen["historical"][mode] != approved_status):
                            raise WorkflowError(
                                "targeted retest only admits actual failed scenes or reviewed pending scenes; "
                                "never PASS or untested"
                            )
                identity = dumps({key: spec[key] for key in ("key", "service", "name")})
                registered = connection.execute(
                    "SELECT * FROM resources WHERE id=? OR canonical=?", (spec["id"], spec["key"])
                ).fetchone()
                if registered and (registered["id"] != spec["id"] or registered["identity"] != identity):
                    raise WorkflowError("resource IDs and canonical identity are permanent")
                connection.execute("INSERT OR IGNORE INTO resources VALUES(?,?,?)", (spec["id"], spec["key"], identity))
                task_id = f"{plan['plan_id']}:{spec['id']}"
                connection.execute(
                    "INSERT INTO tasks(id,run_id,resource_id,spec,state) VALUES(?,?,?,?,?)",
                    (task_id, plan["plan_id"], spec["id"], dumps(spec), "pending"),
                )
            self.event(connection, "plan_imported", {"plan_id": plan["plan_id"], "budget_cents": budget_cents})
        return plan["plan_id"]

    def pause(self, paused: bool) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE meta SET value=? WHERE key='paused'", (dumps(paused),))
            connection.execute("UPDATE actions SET state='stale' WHERE state='issued'")
            self.event(connection, "pause_changed", {"paused": paused})

    def amend_budget(self, plan_id: str, budget_cents: int, evidence: str) -> dict[str, Any]:
        """OPS-reviewed fee allowance; never reimport a plan or reset reservations/calls."""
        if type(budget_cents) is not int or budget_cents <= 0:
            raise WorkflowError("budget amendment requires positive integer cents")
        proof_path = contained(self.root / "inputs", evidence.removeprefix("inputs/"))
        proof = read_json(proof_path)
        proof_sha = digest(proof_path)
        required = {
            "schema_version": 1, "kind": "ops_run_budget_review", "approved": True,
            "plan_id": plan_id, "new_budget_cents": budget_cents,
        }
        if any(proof.get(key) != value for key, value in required.items()):
            raise WorkflowError("exact approved OPS budget review required")
        human_path = contained(self.root / "inputs", str(proof.get("human_authorization", "" )).removeprefix("inputs/"))
        human = read_json(human_path)
        if digest(human_path) != proof.get("human_authorization_sha256") or (
            human.get("kind") != "human_fee_authorization" or human.get("approved") is not True
            or human.get("plan_id") != plan_id or type(human.get("max_budget_cents")) is not int
            or human["max_budget_cents"] < budget_cents or not human.get("verbatim_user_message")
            or human.get("task_id") != proof.get("task_id")
        ):
            raise WorkflowError("original explicit human fee authorization must match the review")
        with self.transaction() as connection:
            if connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true":
                raise WorkflowError("pause dispatch before budget amendment")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE state IN ('queued','running','unknown','submitted')"
            ).fetchone() or connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone():
                raise WorkflowError("drain model jobs and issued actions before budget amendment")
            run = connection.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (proof.get("task_id"),)).fetchone()
            if not run or not task or task["run_id"] != plan_id or task["state"] == "complete":
                raise WorkflowError("budget review must name an incomplete task of this existing run")
            frozen_sha = hashlib.sha256(run["plan"].encode("utf-8")).hexdigest()
            if proof.get("frozen_plan_sha256") != frozen_sha:
                raise WorkflowError("frozen plan SHA changed")
            previous = [json.loads(row[0]) for row in connection.execute(
                "SELECT data FROM events WHERE kind='run_budget_amended'"
            )]
            if any(item.get("evidence_sha256") == proof_sha for item in previous):
                if run["budget"] != budget_cents:
                    raise WorkflowError("consumed budget review cannot change a later allowance")
                return {"plan_id": plan_id, "budget_cents": budget_cents, "already_recorded": True}
            if type(proof.get("old_budget_cents")) is not int or proof["old_budget_cents"] != run["budget"]:
                raise WorkflowError("old run budget changed; independently re-review")
            if budget_cents <= run["budget"]:
                raise WorkflowError("budget amendment only increases the reviewed allowance")
            connection.execute("UPDATE runs SET budget=? WHERE id=?", (budget_cents, plan_id))
            self.event(connection, "run_budget_amended", {
                **required, "task_id": task["id"], "old_budget_cents": run["budget"],
                "frozen_plan_sha256": frozen_sha, "evidence": proof_path.relative_to(self.root).as_posix(),
                "evidence_sha256": proof_sha, "human_authorization_sha256": digest(human_path),
                "reservations_preserved": True, "wire_unchanged": True,
            })
        return {"plan_id": plan_id, "budget_cents": budget_cents, "already_recorded": False,
                "frozen_plan_sha256": frozen_sha, "no_cloud_call": True}

    @staticmethod
    def review_only_reason(connection: sqlite3.Connection) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key='review_only'").fetchone()
        return json.loads(row[0]) if row else None

    def reviewed_continuation(self, connection: sqlite3.Connection, job: sqlite3.Row) -> bool:
        """Validate the unique queued continuation against its immutable OPS review."""
        if job["role"] != "glm" or job["purpose"] != "continuation" or not job["thread_id"]:
            return False
        from . import existing_query

        if existing_query.load(self.root, connection, job["task_id"]):
            return existing_query.reviewed(self.root, connection, job)
        from . import native_continuation

        recovery = native_continuation.load(self.root, connection, job["task_id"])
        proof = read_json(contained(self.root, recovery["evidence"]))
        from . import native_retry

        retry = native_retry.load(self.root, connection, job["task_id"])
        expected_attempt = retry["attempt"] if retry else proof["attempt"]
        expected_count = retry["previous_continuation_count"] + 1 if retry else 1
        if recovery.get("kind") == "managed_read_continuation":
            expected_count = proof.get("previous_continuation_count", 0) + 1
        if retry and (retry["original_job_id"] != recovery["original_job_id"]
                      or retry["original_review"] != {"path": recovery["evidence"],
                                                       "sha256": recovery["evidence_sha256"]}):
            raise WorkflowError("native retry does not retain its original review")
        task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
        original = connection.execute(
            "SELECT * FROM jobs WHERE id=?", (recovery["original_job_id"],)
        ).fetchone()
        slot = connection.execute("SELECT * FROM slots WHERE id=?", (proof["slot"],)).fetchone()
        continuation_count = connection.execute(
            "SELECT count(*) FROM jobs WHERE task_id=? AND purpose='continuation'", (job["task_id"],)
        ).fetchone()[0]
        if (
            proof.get("approved") is not True
            or proof.get("task_id") != job["task_id"]
            or proof.get("original_job_id") != recovery["original_job_id"]
            or proof.get("scope") != recovery["scope"]
            or proof.get("session") != recovery["session_id"]
            or not task
            or task["state"] != "running"
            or task["attempt"] != expected_attempt + 1
            or task["slot"] != proof["slot"]
            or not original
            or original["task_id"] != job["task_id"]
            or original["role"] != "glm"
            or original["purpose"] != "dispatch"
            or original["state"] != "complete"
            or original["thread_id"] != proof["thread_id"]
            or original["turn_id"] != proof["original_turn_id"]
            or job["thread_id"] != proof["thread_id"]
            or job["turn_id"] is not None
            or job["submission"] not in {"not_submitted", "thread_bound"}
            or continuation_count != expected_count
            or not slot
            or slot["task_id"] != job["task_id"]
            or slot["target"] != proof["target"]
            or slot["origin"] != proof["origin"]
        ):
            raise WorkflowError("queued continuation does not match its exact OPS review")
        return True

    def register_hourly_fee(self, plan_id: str, evidence: str) -> dict[str, Any]:
        from .hourly_budget import register

        return register(self, plan_id, evidence)

    def authorize_temporary_samples(self, task_id: str, evidence: str) -> dict[str, Any]:
        """OPS-only immutable sample admission; not a cloud-operation authorization."""
        from .temporary_samples import reviewed_scope

        with self.transaction() as connection:
            if connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true":
                raise WorkflowError("pause before temporary sample admission")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE state IN ('queued','running','unknown','submitted')"
            ).fetchone() or connection.execute(
                "SELECT 1 FROM actions WHERE state='issued'"
            ).fetchone():
                raise WorkflowError("drain model jobs and issued actions before temporary sample admission")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["state"] == "complete":
                raise WorkflowError("temporary samples require an incomplete existing task")
            run = connection.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
            scope = reviewed_scope(self.root, task, run, evidence)
            if scope["kind"] in {"ops_additional_empty_table_admission", "ops_additional_empty_general_nas_admission"}:
                from .additional_source import validate_additional_source

                nas = scope["kind"] == "ops_additional_empty_general_nas_admission"
                prefix = "additional_empty_nas" if nas else "additional_empty_table"
                key = prefix + ":" + task_id + ":" + scope["human_permission_id"]
                old = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                if old:
                    if json.loads(old[0]) != scope:
                        raise WorkflowError("one human additional-sample permission cannot duplicate operations")
                    return {"task_id": task_id, "already_recorded": True, "no_cloud_call": True}
                if not connection.execute(
                    "SELECT 1 FROM meta WHERE key=?", ("temporary_samples:" + task_id,)
                ).fetchone():
                    raise WorkflowError("additional sample requires the immutable original admission")
                proof = read_json(contained(self.root, scope["evidence"]))
                packet = read_json(contained(self.root, scope["proposal"]))
                if nas:
                    if scope.get("authorization_lifetime") == "task":
                        latest = connection.execute(
                            "SELECT id FROM jobs WHERE task_id=? AND role='glm' ORDER BY created DESC LIMIT 1",
                            (task_id,),
                        ).fetchone()
                        if not latest or scope["prior_scope"] != latest[0] + ":TF":
                            raise WorkflowError("task-lifetime NAS successor must follow the latest real worker")
                    initial = json.loads(connection.execute(
                        "SELECT value FROM meta WHERE key=?", ("temporary_samples:" + task_id,),
                    ).fetchone()[0])
                    if scope["original_admission"] != {
                        "path": initial["evidence"], "sha256": initial["evidence_sha256"],
                    } or reviewed_scope(self.root, task, run, initial["evidence"]) != initial:
                        raise WorkflowError("additional NAS must retain the immutable original admission")
                if {item["id"] for item in expected_scenes(json.loads(task["spec"]))} != set(packet["scenes"]):
                    raise WorkflowError("additional empty sample must cover only the exact remaining scenes")
                validate_additional_source(
                    self.root, packet, read_json(contained(
                        self.root, proof["operation_review"]["path"] if nas else proof["operation_review"],
                    )),
                )
                connection.execute("INSERT INTO meta VALUES(?,?)", (key, dumps(scope)))
                self.event(connection, prefix + "_admitted", scope)
                return {"task_id": task_id, "already_recorded": False, "no_cloud_call": True,
                        "operation_authorization_required": True, "frozen_plan_unchanged": True}
            key = "temporary_samples:" + task_id
            old = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if old:
                if json.loads(old[0]) != scope:
                    raise WorkflowError("temporary source admission cannot be replaced to duplicate create quotas")
                return {"task_id": task_id, "already_recorded": True, "no_cloud_call": True}
            if task["attempt"] or connection.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND role='glm'", (task_id,)
            ).fetchone():
                if scope["kind"] != "ops_empty_general_nas_admission":
                    raise WorkflowError("first temporary source admission must precede any GLM execution")
                from .temporary_samples import nas_prior_queries_only

                nas_prior_queries_only(self.root, connection, task_id)
            connection.execute("INSERT INTO meta VALUES(?,?)", (key, dumps(scope)))
            self.event(connection, "temporary_samples_admitted", scope)
            return {"task_id": task_id, "already_recorded": False, "no_cloud_call": True,
                    "operation_authorization_required": True, "frozen_plan_unchanged": True}

    def readonly_dimensions(
        self, connection: sqlite3.Connection, task: Any, packet: dict[str, Any] | None,
        *, review: dict[str, Any] | None = None,
    ) -> set[str]:
        from .temporary_samples import admitted

        return readonly_retest_dimensions(json.loads(task["spec"])) - set(
            admitted(self.root, connection, task, packet, review=review)
        )

    def set_review_only(self, reason: str | None) -> None:
        """OPS hold persists across resume/restart; only existing review/repair may run."""
        if reason is not None and (not isinstance(reason, str) or not reason.strip() or len(reason) > 500):
            raise WorkflowError("review-only requires a bounded non-secret reason")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('review_only',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (dumps(reason),),
            )
            connection.execute("UPDATE actions SET state='stale' WHERE state='issued'")
            self.event(connection, "review_only_changed", {"reason": reason})

    @staticmethod
    def available_slots(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(connection.execute(
            "SELECT * FROM slots s WHERE task_id IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM meta WHERE key='slot_hold:' || s.id) ORDER BY id"
        ))

    def defer_dispatch(self, action_id: str, slot_id: int, reason: str) -> None:
        """Quarantine a failed idle slot before any reservation/submission exists."""
        with self.transaction() as connection:
            slot = connection.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            action = connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if (not slot or slot["task_id"] is not None or not action
                    or (action["kind"], action["state"]) != ("dispatch", "issued")):
                raise WorkflowError("preflight deferral requires the original issued dispatch and an idle slot")
            value = {"slot": slot_id, "origin": slot["origin"], "target": slot["target"], "reason": reason,
                     "task_id": action["task_id"], "action_id": action_id}
            connection.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("slot_hold:" + str(slot_id), dumps(value)))
            connection.execute("UPDATE actions SET state='stale' WHERE id=?", (action_id,))
            self.event(connection, "dispatch_preflight_deferred", value)

    def recover_slot(self, slot_id: int) -> None:
        """Only the OPS control endpoint may call after a fresh original-slot preflight."""
        with self.transaction() as connection:
            slot = connection.execute("SELECT task_id FROM slots WHERE id=?", (slot_id,)).fetchone()
            if not slot or slot[0] is not None:
                raise WorkflowError("slot recovery requires an idle original slot")
            if connection.execute("DELETE FROM meta WHERE key=?", ("slot_hold:" + str(slot_id),)).rowcount:
                self.event(connection, "dispatch_slot_recovered", {"slot": slot_id})

    def status(self) -> dict[str, Any]:
        with self.transaction() as connection:
            tasks = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,run_id,resource_id,state,slot,attempt,reason FROM tasks ORDER BY resource_id"
                )
            ]
            jobs = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,task_id,role,purpose,state,thread_id,turn_id,submission FROM jobs "
                    "WHERE state IN ('queued','running','unknown','submitted')"
                )
            ]
            return {
                "schema_version": 1,
                "paused": connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true",
                "review_only": self.review_only_reason(connection),
                "revision": int(connection.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]),
                "tasks": tasks,
                "active_plans": self.active_plans(connection),
                "jobs": jobs,
                "slots": [dict(row) for row in connection.execute("SELECT * FROM slots")],
                "completed": sum(task["state"] == "complete" for task in tasks),
                "running": sum(job["role"] == "glm" and job["state"] == "running" for job in jobs),
                "reserved": sum(job["role"] == "glm" for job in jobs),
                "can_finalize": self.finalizable(connection),
                "progress": self.progress(connection),
            }

    @staticmethod
    def finalizable(connection: sqlite3.Connection) -> bool:
        """A completed task label alone must not hide a retained lease or owner."""
        from .hourly_budget import holds

        return bool(connection.execute("SELECT 1 FROM tasks").fetchone()) and not any(
            connection.execute(query).fetchone() for query in (
                "SELECT 1 FROM tasks WHERE state NOT IN ('complete','superseded')",
                "SELECT 1 FROM jobs WHERE state!='complete'",
                "SELECT 1 FROM owned WHERE state!='deleted'",
                "SELECT 1 FROM slots WHERE task_id IS NOT NULL",
                "SELECT 1 FROM actions WHERE state='issued'",
            )
        ) and not holds(connection)

    @staticmethod
    def repair_scope(connection: sqlite3.Connection, task_id: str) -> str:
        row = connection.execute("SELECT value FROM meta WHERE key=?", ("repair_scope:" + task_id,)).fetchone()
        # Historical/unclassified repairs keep the conservative shared-code barrier.
        return "input" if row and row[0] == '"input"' else "product"

    @staticmethod
    def set_repair_scope(connection: sqlite3.Connection, task_id: str, scope: str) -> None:
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("repair_scope:" + task_id, dumps(scope)),
        )

    def progress(self, connection: sqlite3.Connection) -> dict[str, Any]:
        """Deterministic handoffs from durable state, without deadlines or automatic retries.

        The handoff ID survives restarts and unrelated screenshots. Consumers can
        deduplicate notifications while still handling every distinct blocker.
        This observation never authorizes work, changes a goal, or expires a job.
        """
        from .hourly_budget import can_reserve, holds

        jobs = [dict(row) for row in connection.execute(
            "SELECT id,task_id,role,purpose,state,thread_id,turn_id,submission FROM jobs WHERE state!='complete'"
        )]
        tasks = list(connection.execute(
            "SELECT * FROM tasks WHERE state NOT IN ('complete','superseded') ORDER BY resource_id"
        ))
        paused = connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true"
        candidate = self._select_candidate(connection)
        attention, waiting = [], []

        def handoff(category: str, identity: str, reason: str, **details: Any) -> None:
            value = {"category": category, "identity": identity, "reason": reason, "owner": "ops", **details}
            value["id"] = hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()
            attention.append(value)

        for job in jobs:
            if job["state"] == "unknown":
                handoff("unknown_submission", job["id"], "Read the exact owning backend; never resubmit", job=job)
        for row in connection.execute("SELECT value FROM meta WHERE key LIKE 'slot_hold:%' ORDER BY key"):
            hold = json.loads(row[0])
            handoff("slot_preflight", str(hold["slot"]), hold["reason"], slot=hold,
                    recovery="OPS restore the same slot, then slot-recover; no model or cloud retry")
        busy_tasks = {job["task_id"] for job in jobs}
        for task in tasks:
            if task["state"] == "ready" and task["packet"]:
                packet = read_json(contained(self.root, task["packet"]))
                if "hourly_cost" in packet and not can_reserve(self.root, connection, task, packet):
                    waiting.append({"category": "hourly_cost_capacity", "task_id": task["id"],
                                    "required_hourly_cents": packet["hourly_cost"]["max_hourly_cents"],
                                    "held": holds(connection), "release": "verified cleanup only; no expiry"})
            if task["state"] == "blocked":
                row = connection.execute(
                    "SELECT value FROM meta WHERE key=?", ("block_handoff:" + task["id"],)
                ).fetchone()
                details = json.loads(row[0]) if row else None
                handoff("blocked_task", task["id"], task["reason"] or "Sol blocker needs independent review",
                        task_id=task["id"], attempt=task["attempt"], slot=task["slot"], handoff=details,
                        model_terminal=task["id"] not in busy_tasks)
            elif task["state"] in {"preparing", "running", "reviewing", "repairing"} and task["id"] not in busy_tasks:
                handoff("orphaned_transition", task["id"], "No nonterminal job owns this transitional state",
                        task_id=task["id"], state=task["state"])
        product_repairs = [task["id"] for task in tasks if task["state"] in {"repair", "repairing"}
                           and self.repair_scope(connection, task["id"]) == "product"]
        if product_repairs:
            waiting.append({"category": "product_repair_barrier", "tasks": product_repairs,
                            "draining_jobs": [job["id"] for job in jobs if job["role"] == "glm"]})
        review_only = self.review_only_reason(connection)
        if review_only:
            handoff("review_only", "dispatch_hold", review_only)
        if candidate is None and not jobs and tasks and not paused and not attention:
            # Dependency/first-wave/lock deadlocks are not a healthy idle wait.
            handoff("scheduler_stalled", "queue", "No eligible action and no active model; inspect frozen gates",
                    task_states={task["id"]: task["state"] for task in tasks},
                    initial_waves=self.active_plans(connection),
                    locks=[dict(row) for row in connection.execute("SELECT * FROM locks")])
        if not tasks and not jobs and not self.finalizable(connection) and not paused:
            handoff("finalization_incomplete", "close", "Completed labels still retain owners, slots, actions or cost",
                    hourly_cost_holds=holds(connection))
        return {
            "mode": "paused" if paused else "finished" if self.finalizable(connection) else
                    "actionable" if candidate else "needs_ops" if attention else "waiting",
            "can_progress": candidate is not None,
            "next_candidate": {"task_id": candidate[0]["id"], "kind": candidate[1]} if candidate else None,
            "requires_ops": bool(attention), "attention": attention, "waiting_on": waiting,
            "active_jobs": jobs,
            "free_slots": connection.execute("SELECT COUNT(*) FROM slots WHERE task_id IS NULL").fetchone()[0],
            "available_slots": [slot["id"] for slot in self.available_slots(connection)],
            "automatic_retry": False, "worker_deadline": None,
        }

    def active_plans(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        """Recover frozen scope from SQLite, not an old chat or a static run guide.

        Historical runs retain state='active'; actual incomplete tasks or active
        jobs determine this view. It is read-only and does not authorize work.
        """
        from .hourly_budget import holds, policy

        values = []
        held_plans = sorted({item["plan_id"] for item in holds(connection)})
        held_placeholders = ",".join("?" for _ in held_plans) or "NULL"
        for run in connection.execute(
            "SELECT * FROM runs r WHERE EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.id "
            "AND t.state NOT IN ('complete','superseded')) "
            "OR EXISTS(SELECT 1 FROM jobs j JOIN tasks t ON t.id=j.task_id WHERE t.run_id=r.id "
            "AND j.state IN ('queued','running','unknown','submitted')) "
            f"OR r.id IN ({held_placeholders}) ORDER BY r.id", held_plans,
        ).fetchall():
            frozen = json.loads(run["plan"])
            tasks = connection.execute(
                "SELECT resource_id,state FROM tasks WHERE run_id=? ORDER BY resource_id", (run["id"],)
            ).fetchall()
            values.append(
                {
                    "plan_id": run["id"],
                    "frozen_plan_sha256": hashlib.sha256(run["plan"].encode("utf-8")).hexdigest(),
                    "resource_ids": [spec["id"] for spec in frozen["resources"]],
                    "remaining_resource_ids": [task["resource_id"] for task in tasks
                                               if task["state"] not in {"complete", "superseded"}],
                    "task_states": {task["resource_id"]: task["state"] for task in tasks},
                    "budget_cents": run["budget"],
                    "hourly_fee_authorization": policy(self.root, connection, run["id"]),
                    "hourly_cost_holds": [item for item in holds(connection) if item["plan_id"] == run["id"]],
                    "initial_wave": frozen.get("initial_wave", 1),
                    "glm_reservation_started": bool(
                        connection.execute(
                            "SELECT 1 FROM jobs j JOIN tasks t ON t.id=j.task_id WHERE t.run_id=? AND j.role='glm'",
                            (run["id"],),
                        ).fetchone()
                    ),
                    "initial_wave_eligible": self.initial_wave_ready(connection, run["id"]),
                }
            )
        return values

    def initial_wave_ready(self, connection: sqlite3.Connection, run_id: str) -> bool:
        """Open a run's first wave only after enough independent packets are ready.

        Any actual GLM reservation permanently opens rolling dispatch, including after recovery.
        """
        if connection.execute(
            "SELECT 1 FROM jobs j JOIN tasks t ON t.id=j.task_id WHERE t.run_id=? AND j.role='glm'",
            (run_id,),
        ).fetchone():
            return True
        minimum = json.loads(connection.execute("SELECT plan FROM runs WHERE id=?", (run_id,)).fetchone()[0]).get(
            "initial_wave", 1
        )
        free = len(self.available_slots(connection))
        if free < minimum:
            return False
        held = {row[0] for row in connection.execute("SELECT name FROM locks")}
        ready = [
            json.loads(row[0])
            for row in connection.execute("SELECT spec FROM tasks WHERE run_id=? AND state='ready'", (run_id,))
        ]
        for wave in combinations(ready, minimum):
            locks = [lock for item in wave for lock in item.get("locks", [])]
            if len(locks) == len(set(locks)) and not held.intersection(locks):
                return True
        return False

    def _select_candidate(self, connection: sqlite3.Connection) -> tuple[sqlite3.Row, str] | None:
        """Same transactional eligibility/priority check for issue and consumption.

        Journal revisions include unrelated screenshots and model bindings. They
        are audit sequence numbers, not leases on this task's scheduling state.
        """
        from .hourly_budget import can_reserve

        if connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true":
            return None
        review_only = self.review_only_reason(connection)
        sol_busy = connection.execute(
            "SELECT 1 FROM jobs WHERE role='sol' AND state IN ('queued','running','unknown','submitted')"
        ).fetchone()
        rows = list(connection.execute("SELECT * FROM tasks ORDER BY resource_id"))
        # A reviewed grouped run freezes its order in runs.plan. Do not let
        # permanent resource numbers silently reorder A/B/C/D priorities, and
        # do not consult mutable task specs or prompts for this decision.
        for run in connection.execute("SELECT id,plan FROM runs"):
            frozen = json.loads(run["plan"])
            if not any(item.get("retest_scope", {}).get("kind") == SCOPE_KIND
                       for item in frozen["resources"]):
                continue
            order = {item["id"]: index for index, item in enumerate(frozen["resources"])}
            own = sorted((row for row in rows if row["run_id"] == run["id"]),
                         key=lambda row: order[row["resource_id"]])
            ordered = iter(own)
            rows = [next(ordered) if row["run_id"] == run["id"] else row for row in rows]
        repair_barrier = any(row["state"] in {"repair", "repairing"}
                             and self.repair_scope(connection, row["id"]) == "product" for row in rows)
        glm_busy = connection.execute(
            "SELECT 1 FROM jobs WHERE role='glm' AND state IN ('queued','running','unknown','submitted')"
        ).fetchone()
        complete = {(row["run_id"], row["resource_id"]) for row in rows if row["state"] == "complete"}
        busy_tasks = {
            row[0]
            for row in connection.execute(
                "SELECT task_id FROM jobs WHERE state IN ('queued','running','unknown','submitted')"
            )
        }
        rows = [row for row in rows if row["id"] not in busy_tasks]
        candidate = next(((row, "cleanup") for row in rows if row["state"] == "cleanup_pending"), None)
        # An exact OPS-reviewed recovery may finish its already-created, held
        # generation under a global review-only hold. This exception cannot
        # admit ordinary dispatch, navigation or another repair.
        if candidate is None and not repair_barrier:
            candidate = next(((row, "continuation") for row in rows if row["state"] == "continuation_pending"), None)
        if candidate is None and not review_only and not repair_barrier:
            candidate = next(((row, "navigation") for row in rows if row["state"] == "navigation_pending"), None)
        if candidate is None and not sol_busy:
            candidate = next(((row, "review") for row in rows if row["state"] == "reported"), None)
            if candidate is None:
                candidate = next(((row, "repair") for row in rows if row["state"] == "repair"
                                  and (not glm_busy or self.repair_scope(connection, row["id"]) == "input")), None)
        if (
            candidate is None
            and not review_only
            and not repair_barrier
            and self.available_slots(connection)
        ):
            held = {row["name"]: row["task_id"] for row in connection.execute("SELECT * FROM locks")}
            candidate = next(
                (
                    (row, "dispatch")
                    for row in rows
                    if row["state"] == "ready"
                    and self.initial_wave_ready(connection, row["run_id"])
                    and all(held.get(name, row["id"]) == row["id"] for name in json.loads(row["spec"]).get("locks", []))
                    and can_reserve(self.root, connection, row, read_json(contained(self.root, row["packet"])))
                ),
                None,
            )
        # Read-only preparation can run while a queued product repair drains GLM.
        # It cannot write product code; dispatch remains closed by the barrier.
        product_writer = any(row["state"] == "repairing"
                             and self.repair_scope(connection, row["id"]) == "product" for row in rows)
        if candidate is None and not sol_busy and not product_writer and not review_only:
            ready_count = sum(row["state"] in {"ready", "preparing"} for row in rows)
            candidate = next(
                (
                    (row, "prepare")
                    for row in rows
                    if row["state"] == "pending"
                    # A conflicting ready prefix must not prevent preparing
                    # the resource that makes the required first wave possible.
                    and (ready_count < 5 or not self.initial_wave_ready(connection, row["run_id"]))
                    and all((row["run_id"], dependency) in complete
                            for dependency in json.loads(row["spec"]).get("dependencies", []))
                ),
                None,
            )
        return candidate

    def next_action(self) -> dict[str, Any]:
        """One outstanding action, recalculated after every execution. Never steal a slow lease."""
        with self.transaction() as connection:
            scope = {"active_plans": self.active_plans(connection), "progress": self.progress(connection)}
            if connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true":
                return {**scope, "type": "paused", "actions": []}
            review_only = self.review_only_reason(connection)
            old = connection.execute("SELECT * FROM actions WHERE state='issued'").fetchone()
            if old:
                if not review_only or old["kind"] in {"review", "repair", "cleanup", "continuation"}:
                    return {**scope, "actions": [dict(old)]}
                connection.execute("UPDATE actions SET state='stale' WHERE id=?", (old["id"],))
            candidate = self._select_candidate(connection)
            if candidate is None:
                finished = self.finalizable(connection)
                return {**scope, "type": "finished" if finished else "wait", "actions": [], "review_only": review_only}
            task, kind = candidate
            revision = int(connection.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            action = {
                "id": uuid.uuid4().hex,
                "task_id": task["id"],
                "kind": kind,
                "state": "issued",
                "revision": revision,
            }
            connection.execute("INSERT INTO actions VALUES(:id,:task_id,:kind,:state,:revision)", action)
            return {**scope, "actions": [action]}

    def execute(self, action_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            action = connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not action:
                raise WorkflowError("unknown action ID")
            if action["state"] != "issued":
                return {"stale_action_skipped": True}
            if self.review_only_reason(connection) and action["kind"] not in {
                "review", "repair", "cleanup", "continuation",
            }:
                connection.execute("UPDATE actions SET state='stale' WHERE id=?", (action_id,))
                return {"stale_action_skipped": True, "review_only": True}
            candidate = self._select_candidate(connection)
            if candidate is None or (candidate[0]["id"], candidate[1]) != (action["task_id"], action["kind"]):
                connection.execute("UPDATE actions SET state='stale' WHERE id=?", (action_id,))
                return {"stale_action_skipped": True}
            task = candidate[0]
            spec = json.loads(task["spec"])
            role = "glm" if action["kind"] in {"dispatch", "cleanup", "navigation", "continuation"} else "sol"
            slot = task["slot"]
            job_id = uuid.uuid4().hex
            if action["kind"] == "continuation":
                from . import existing_query
                from .native_continuation import load
                from .native_retry import load as load_retry

                existing = existing_query.load(self.root, connection, task["id"])
                recovery = ((existing or {}).get("capture_recovery") or existing
                            or load(self.root, connection, task["id"]))
                proof = read_json(contained(self.root, recovery["evidence"]))
                retry = None if existing else load_retry(self.root, connection, task["id"])
                expected_attempt = retry["attempt"] if retry else proof["attempt"]
                held = connection.execute("SELECT * FROM slots WHERE id=?", (slot,)).fetchone()
                if (not held or held["task_id"] != task["id"] or task["attempt"] != expected_attempt
                        or any(held[k] != proof[k] for k in ("target", "origin"))):
                    raise WorkflowError("native continuation must retain original generation and occupied slot")
                # New immutable screenshot observation, not a new cloud generation
                # or a budget/capability reservation. Old unclicked evidence remains.
                connection.execute("UPDATE tasks SET attempt=attempt+1 WHERE id=?", (task["id"],))
                state = "running"
            elif action["kind"] == "navigation":
                recovery = self.navigation_recovery(connection, task)
                held = connection.execute("SELECT task_id,target,origin FROM slots WHERE id=?", (slot,)).fetchone()
                if (not held or held[0] != task["id"] or held[1] != recovery["target"]
                        or held[2] != recovery["origin"]):
                    raise WorkflowError("navigation recovery must retain its exact original slot")
                connection.execute("UPDATE tasks SET attempt=attempt+1 WHERE id=?", (task["id"],))
                state = "running"
            elif action["kind"] == "cleanup":
                held = connection.execute("SELECT task_id FROM slots WHERE id=?", (slot,)).fetchone()
                if task["state"] != "cleanup_pending" or not held or held[0] != task["id"]:
                    raise WorkflowError("cleanup requires the original retained task slot")
                state = "running"
            elif role == "glm":
                available = self.available_slots(connection)
                free = available[0] if available else None
                if not free or task["state"] != "ready":
                    raise WorkflowError("dispatch requires a ready task and a free slot")
                if not self.initial_wave_ready(connection, task["run_id"]):
                    raise WorkflowError("first wave has not reached its approved ready threshold")
                if not task["packet"]:
                    raise WorkflowError("missing approved packet")
                packet = read_json(contained(self.root, task["packet"]))
                from .selection import selected_scenes

                selected_scenes(self.root, packet, spec)
                from .additional_source import dispatch_reservation_increment

                reservation = dispatch_reservation_increment(self.root, packet)
                total = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM budget_reservations b "
                    "JOIN tasks t ON t.id=b.task_id WHERE t.run_id=?",
                    (task["run_id"],),
                )
                used = total.fetchone()[0]
                budget = connection.execute("SELECT budget FROM runs WHERE id=?", (task["run_id"],)).fetchone()[0]
                if used + reservation > budget:
                    raise WorkflowError("aggregate run budget exhausted")
                for name in spec.get("locks", []):
                    owner = connection.execute("SELECT task_id FROM locks WHERE name=?", (name,)).fetchone()
                    if owner and owner[0] != task["id"]:
                        connection.execute("UPDATE actions SET state='stale' WHERE id=?", (action_id,))
                        return {"conflict": name}
                from .hourly_budget import approved_cost, reserve

                hourly = approved_cost(self.root, connection, task, packet)
                if hourly is not None:
                    reserve(self, connection, hourly, job_id)
                slot = free["id"]
                connection.execute("UPDATE slots SET task_id=? WHERE id=?", (task["id"], slot))
                connection.execute(
                    "INSERT INTO budget_reservations VALUES(?,?) ON CONFLICT(task_id) "
                    "DO UPDATE SET amount=amount+excluded.amount",
                    (task["id"], reservation),
                )
                for name in spec.get("locks", []):
                    connection.execute("INSERT OR IGNORE INTO locks VALUES(?,?)", (name, task["id"]))
                state = "running"
                connection.execute("UPDATE tasks SET attempt=attempt+1,slot=? WHERE id=?", (slot, task["id"]))
            else:
                state = {"prepare": "preparing", "review": "reviewing", "repair": "repairing"}[action["kind"]]
            capability = secrets.token_urlsafe(32)
            thread_id = self.plan_sol_thread(connection, task["run_id"])
            if role == "glm":
                previous = connection.execute(
                    "SELECT thread_id FROM jobs WHERE task_id=? AND role='glm' "
                    "AND thread_id IS NOT NULL ORDER BY created DESC LIMIT 1",
                    (task["id"],),
                ).fetchone()
                thread_id = previous[0] if previous else None
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    task["id"],
                    role,
                    action["kind"],
                    "queued",
                    thread_id or None,
                    None,
                    hashlib.sha256(capability.encode()).hexdigest(),
                    "not_submitted",
                    time.time(),
                ),
            )
            connection.execute("UPDATE tasks SET state=? WHERE id=?", (state, task["id"]))
            connection.execute("UPDATE actions SET state='executed' WHERE id=?", (action_id,))
            self.event(connection, "job_reserved", {"job_id": job_id, "task_id": task["id"], "role": role})
            job_path = self.root / "jobs" / job_id / "capability.json"
            json_write(job_path, {"job_id": job_id, "token": capability}, private=True)
            return {"job_id": job_id, "state": "queued", "role": role}

    def finish_job(self, job_id: str, thread: str, turn: str, terminal: str,
                   *, terminal_raw: dict[str, Any] | None = None) -> None:
        """Only transport/OPS may release a model lease, after an exact terminal read."""
        if terminal not in {"completed", "failed", "interrupted"}:
            raise WorkflowError("exact terminal status required")
        # A desktop cache or a caller-supplied status is not the owning model
        # terminal. Real adapter jobs must carry an exact wire notification or
        # the independently fetched owning-backend response used by reconcile.
        directory = self.root / "jobs" / job_id
        if (directory / "launch.json").exists():
            trace = directory / "raw-rollout.jsonl"
            events = ([json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
                      if trace.exists() else [])
            notified = any(event.get("method") == "turn/completed"
                           and event.get("params", {}).get("threadId") == thread
                           and event.get("params", {}).get("turn", {}).get("id") == turn
                           and event.get("params", {}).get("turn", {}).get("status") == terminal for event in events)
            exact = [item for item in (terminal_raw or {}).get("thread", {}).get("turns", [])
                     if item.get("id") == turn]
            reconciled = (terminal_raw is not None and terminal_raw.get("method") == "thread/read"
                          and terminal_raw.get("thread", {}).get("id") == thread and len(exact) == 1
                          and exact[0].get("status") == terminal
                          and terminal_raw.get("job_binding", {}).get("id") == job_id)
            if not notified and not reconciled:
                raise WorkflowError(
                    "owning-backend exact terminal evidence required; desktop cache cannot finish a job"
                )
        with self.transaction() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or (job["thread_id"], job["turn_id"]) != (thread, turn):
                raise WorkflowError("terminal thread/turn mismatch")
            if job["state"] == "complete":
                return
            if job["state"] != "submitted" and job["submission"] != "result_staged":
                connection.execute(
                    "UPDATE tasks SET state=?,reason=? WHERE id=?",
                    (
                        "reported" if job["role"] == "glm" else "blocked",
                        f"{terminal}: missing required submission; no automatic PASS",
                        job["task_id"],
                    ),
                )
            connection.execute("UPDATE jobs SET state='complete' WHERE id=?", (job_id,))
            self.event(connection, "exact_terminal", {"job_id": job_id, "terminal": terminal})

    def navigation_recovery(self, connection, task, evidence=None):
        """Verify immutable original read-only provenance, never mint cloud capacity."""
        if evidence is None:
            saved = connection.execute("SELECT value FROM meta WHERE key=?",
                                       ("navigation_recovery:" + task["id"],)).fetchone()
            if not saved:
                raise WorkflowError("missing reviewed navigation recovery")
            recovery = json.loads(saved[0])
            evidence = recovery["evidence"]
            if digest(contained(self.root, evidence)) != recovery["evidence_sha256"]:
                raise WorkflowError("navigation recovery review changed")
        path = contained(self.root / "inputs", Path(evidence).relative_to("inputs").as_posix())
        proof = read_json(path)
        if (proof.get("schema_version") != 1 or proof.get("kind") != "ops_navigation_only_review"
                or proof.get("approved") is not True or proof.get("task_id") != task["id"]
                or proof.get("cloud_calls") != 0):
            raise WorkflowError("exact OPS navigation-only review required")
        original = connection.execute("SELECT * FROM jobs WHERE id=?", (proof.get("original_job_id"),)).fetchone()
        if (not original or original["task_id"] != task["id"] or original["role"] != "glm"
                or original["purpose"] != "dispatch" or original["state"] != "complete"
                or not original["thread_id"] or not original["turn_id"]):
            raise WorkflowError("original terminal dispatched GLM required")
        retry_id = proof.get("retry_job_id")
        if retry_id is not None:
            retry = connection.execute("SELECT * FROM jobs WHERE id=?", (retry_id,)).fetchone()
            raw_path = contained(self.root, proof.get("retry_terminal_evidence", ""))
            if (not retry or retry["task_id"] != task["id"] or retry["role"] != "glm"
                    or retry["purpose"] != "navigation" or retry["state"] != "complete"
                    or not raw_path.is_file() or digest(raw_path) != proof.get("retry_terminal_sha256")):
                raise WorkflowError("exact terminal unclicked navigation retry evidence required")
            raw = read_json(raw_path)
            turns = [turn for turn in raw.get("thread", {}).get("turns", [])
                     if turn.get("id") == retry["turn_id"]]
            if (raw.get("thread", {}).get("id") != retry["thread_id"] or len(turns) != 1
                    or turns[0].get("status") != "completed"):
                raise WorkflowError("exact owning-backend terminal navigation turn required")
            for scene_id in proof.get("scene_ids", []):
                artifact = self.root / "jobs" / retry_id / "output" / f"{scene_id}.json"
                if (not artifact.is_file() or read_json(artifact).get("actual_ui_click") is not False
                        or read_json(artifact).get("session_id") != proof.get("session_id")):
                    raise WorkflowError("navigation retry cannot replay an actual or unknown click")
            # A recorded successful click tool result cannot be hidden by a later
            # evidence-only snapshot. Inspect the owning turn as well as its JSON.
            for item in turns[0].get("items", []):
                if (item.get("type") == "dynamicToolCall" and item.get("tool") == "resource_workflow"
                        and item.get("arguments", {}).get("operation") == "click"
                        and item.get("success") is not False):
                    raise WorkflowError("navigation retry has successful or unknown click transport")
        packet_path = contained(self.root, task["packet"])
        if proof.get("packet_sha256") != digest(packet_path):
            raise WorkflowError("original navigation packet changed")
        packet = read_json(packet_path)
        spec = json.loads(task["spec"])
        scenes = expected_scenes(spec)
        capture_recovery = proof.get("capture_recovery")
        if capture_recovery is not None:
            capture_profiles = {
                "sae_application_field_capture": (
                    "sae_application_metadata", "ALIYUN::SAE::Application",
                    "sol_sae_viewport_sampler_repair_handoff", {"fail"},
                ),
                "oos_state_configuration_field_capture": (
                    "oos_state_configuration_list", "ALIYUN::OOS::StateConfiguration",
                    "sol_oos_input_repair_capture_handoff", {"execution_gap"},
                ),
            }
            profile = capture_profiles.get(capture_recovery.get("kind")) if isinstance(capture_recovery, dict) else None
            if (
                not profile
                or capture_recovery.get("console_inspection") != profile[0]
                or capture_recovery.get("cloud_calls") != 0 or retry_id is not None
                or any(scene["dimension"] != "QUERY" or scene["source_type"] != profile[1]
                       for scene in scenes)
            ):
                raise WorkflowError("only independently reviewed exact QUERY field capture may recover missing pixels")
            remediation = contained(self.root, capture_recovery.get("remediation_evidence", ""))
            if (not remediation.relative_to(self.root).as_posix().startswith("inputs/" + task["resource_id"] + "/")
                    or digest(remediation) != capture_recovery.get("remediation_sha256")):
                raise WorkflowError("exact own sampler diagnosis required")
            diagnosis = read_json(remediation)
            repair = connection.execute("SELECT * FROM jobs WHERE id=?", (diagnosis.get("job_id"),)).fetchone()
            if (diagnosis.get("kind") != profile[2]
                    or diagnosis.get("task_id") != task["id"] or not repair
                    or repair["task_id"] != task["id"] or repair["role"] != "sol"
                    or repair["purpose"] != "repair" or repair["state"] != "complete"):
                raise WorkflowError("terminal own Sol diagnosis required for capture recovery")
        if (not scenes or proof.get("scene_ids") != [scene["id"] for scene in scenes]
                or any(scene["mode"] not in {"open", "iframe"}
                       or (scene["dimension"] != "ROS" and capture_recovery is None)
                       or scene["dimension"] not in readonly_retest_dimensions(spec) for scene in scenes)):
            raise WorkflowError("navigation recovery only permits exact missing read-only retest scenes")
        output = self.root / "jobs" / original["id"] / "output"
        bound = read_json(output / "self-created-session.json")
        intent = read_json(output / "new-session-intent.json")
        if (bound.get("job_id") != original["id"] or not bound.get("session_id")
                or bound["session_id"] == intent.get("previous_session")
                or any(proof.get(key) != bound.get(key) for key in ("session_id", "target", "origin"))):
            raise WorkflowError("original self-created full session binding changed")
        if proof.get("session_binding_sha256") != digest(output / "self-created-session.json"):
            raise WorkflowError("original session artifact changed")
        dimensions = {scene["dimension"] for scene in scenes}
        if set(proof.get("sources", {})) != dimensions:
            raise WorkflowError("exact original source dimensions required")
        for dimension in dimensions:
            source = output / f"{dimension}-product-source.json"
            item = proof["sources"][dimension]
            if (item != {"path": source.relative_to(self.root).as_posix(), "sha256": digest(source)}
                    or read_json(source).get("session_id") != bound["session_id"]
                    or (output / f"{dimension}-cleanup-intent.json").exists()):
                raise WorkflowError("original navigation source/session changed or cleanup started")
        for scene in scenes:
            old = read_json(output / f"{scene['id']}.json")
            if old.get("session_id") != bound["session_id"]:
                raise WorkflowError("navigation-only recovery cannot replay an actual click")
            verdict = connection.execute("SELECT status FROM verdicts WHERE task_id=? AND scene=? ORDER BY id DESC",
                                         (task["id"], scene["id"])).fetchone()
            if capture_recovery is not None:
                artifacts = capture_recovery.get("original_clicks", {})
                original_json = (output / f"{scene['id']}.json").relative_to(self.root).as_posix()
                png = contained(self.root, old.get("screenshot", ""))
                original_artifact = {"path": original_json, "sha256": digest(output / f"{scene['id']}.json"),
                                     "screenshot": old.get("screenshot"), "screenshot_sha256": digest(png)}
                first_occurrence = read_json(output / f"{scenes[0]['id']}.json").get("clicked_occurrence")
                if (
                    set(artifacts) != {item["id"] for item in scenes}
                    or artifacts[scene["id"]] != original_artifact
                    or old.get("actual_ui_click") is not True or not verdict or verdict[0] not in profile[3]
                    or old.get("source_evidence") != proof["sources"]["QUERY"]["path"]
                    or old.get("source_sha256") != proof["sources"]["QUERY"]["sha256"]
                    or diagnosis.get("source_evidence", {}).get("path") != old.get("source_evidence")
                    or diagnosis.get("source_evidence", {}).get("sha256") != old.get("source_sha256")
                    or not isinstance(old.get("clicked_occurrence"), dict)
                    or old["clicked_occurrence"] != first_occurrence
                ):
                    raise WorkflowError("capture recovery must preserve exact missing clicks, PNG and own source")
            elif (old.get("actual_ui_click") is not False or not verdict
                  or verdict[0] not in {"execution_gap", "product_gap"}):
                raise WorkflowError("independently accepted unclicked gap required; never replay PASS")
        if connection.execute("SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (task["id"],)).fetchone():
            raise WorkflowError("unresolved model ownership forbids navigation recovery")
        from scripts.web.e2e.resource_link_workflow.execution_gate import database, reference_observations

        with database(self.root / "execution-gate.sqlite3") as wire:
            grants = [dict(row) for row in wire.execute("SELECT * FROM grants WHERE scope LIKE ? ORDER BY scope,id",
                                                       (original["id"] + ":%",))]
            attempts = [dict(row) for row in wire.execute("SELECT * FROM attempts WHERE scope LIKE ? ORDER BY id",
                                                         (original["id"] + ":%",))]
            if (not grants or not attempts or any(row["kind"] not in {"identity", "read"} for row in grants)
                    or any(row["state"] != "succeeded" for row in attempts)):
                raise WorkflowError("only successful original read-only wire provenance may recover navigation")
            wire_sha = hashlib.sha256(dumps({"grants": grants, "attempts": attempts}).encode()).hexdigest()
            if proof.get("wire_sha256") != wire_sha:
                raise WorkflowError("original wire/cumulative calls changed")
        for scene in scenes:
            binding = packet["scenes"][scene["id"]]
            refs = read_json(output / f"{scene['dimension']}-product-source.json")["references"]["items"]
            observations = reference_observations(self.root / "execution-gate.sqlite3",
                                                   f"{original['id']}:{scene['dimension']}", bound["session_id"])
            if not any(ref.get("identifier") == binding.get("identifier")
                       and ref.get("resourceType") == binding.get("expected_type", scene["source_type"])
                       and any(item["account"] == binding["account"]
                               and item["reference_scope"] == ref.get("accountScope")
                               and any(source.get("toolUseId") == item["tool_use_id"]
                                       and source.get("evidence") == "api_response"
                                       for source in ref.get("sources", []))
                               for item in observations) for ref in refs):
                raise WorkflowError("original signed exact resource reference required")
            if capture_recovery is not None:
                from .console_sources import bind_oos_state_configuration_source, bind_sae_application_source
                from .runtime import console_inspection

                source_value = read_json(output / "QUERY-product-source.json")
                inspection = console_inspection({**binding, "console_inspection": profile[0]},
                                                scene["source_type"], binding.get("identifier", ""))
                binder = (bind_oos_state_configuration_source if profile[0] == "oos_state_configuration_list"
                          else bind_sae_application_source)
                binder(inspection, refs, source_value.get("messages", {}).get("tools", {}))
        return {**proof, "evidence": path.relative_to(self.root).as_posix(), "evidence_sha256": digest(path)}

    def queue_navigation(self, original_job_id, evidence):
        """OPS checkpoints existing provenance; only Luna reserves the next GLM turn."""
        with self.transaction() as connection:
            if (connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                    or connection.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                    or connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()):
                raise WorkflowError("pause and drain all model jobs/actions before navigation recovery")
            original = connection.execute("SELECT * FROM jobs WHERE id=?", (original_job_id,)).fetchone()
            if not original:
                raise WorkflowError("unknown original navigation job")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (original["task_id"],)).fetchone()
            if task["state"] not in {"blocked", "repair"} or task["slot"] is not None:
                raise WorkflowError("navigation recovery requires a released blocked task")
            recovery = self.navigation_recovery(connection, task, evidence)
            if recovery["original_job_id"] != original_job_id:
                raise WorkflowError("navigation recovery original job mismatch")
            slot = connection.execute("SELECT * FROM slots WHERE target=? AND origin=?",
                                      (recovery["target"], recovery["origin"])).fetchone()
            if not slot or slot["task_id"] is not None:
                raise WorkflowError("original navigation slot unavailable")
            key = "navigation_recovery:" + task["id"]
            previous = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if previous:
                prior = json.loads(previous[0])
                retry_id = recovery.get("retry_job_id")
                latest = connection.execute(
                    "SELECT id FROM jobs WHERE task_id=? AND purpose='navigation' ORDER BY rowid DESC LIMIT 1",
                    (task["id"],),
                ).fetchone()
                if (not retry_id or not latest or latest[0] != retry_id
                        or prior["original_job_id"] != original_job_id
                        or prior["evidence_sha256"] == recovery["evidence_sha256"]
                        or connection.execute("SELECT 1 FROM meta WHERE key=?",
                                              ("navigation_retry:" + retry_id,)).fetchone()):
                    raise WorkflowError("navigation recovery already reviewed; fresh exact unclicked retry required")
                connection.execute("INSERT INTO meta VALUES(?,?)",
                                   ("navigation_retry:" + retry_id, dumps({"previous": prior, "recovery": recovery})))
                self.event(connection, "ops_navigation_retry_reviewed", {"previous": prior, "recovery": recovery})
            elif recovery.get("retry_job_id") is not None:
                raise WorkflowError("navigation retry requires its original recovery")
            for name in json.loads(task["spec"]).get("locks", []):
                if connection.execute("SELECT 1 FROM locks WHERE name=?", (name,)).fetchone():
                    raise WorkflowError("original navigation resource lock unavailable")
                connection.execute("INSERT INTO locks VALUES(?,?)", (name, task["id"]))
            connection.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(recovery)))
            connection.execute("UPDATE slots SET task_id=? WHERE id=?", (task["id"], slot["id"]))
            connection.execute("UPDATE tasks SET state='navigation_pending',slot=?,reason=NULL WHERE id=?",
                               (slot["id"], task["id"]))
            self.event(connection, "ops_navigation_only_reviewed", recovery)
            return {"navigation_queued": task["id"], "only_luna_dispatches": True, "cloud_calls": 0}

    def queue_cleanup(self, task_id: str, recovery: dict[str, Any]) -> None:
        """OPS reviews original provenance; only Luna allocates a cleanup-only model job."""
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND state!='complete'", (task_id,)
            ).fetchone():
                raise WorkflowError("cleanup recovery requires same task model jobs terminal")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (recovery["original_job_id"],)).fetchone()
            if not task or task["state"] not in {"blocked", "reported"} or task["slot"] is None:
                raise WorkflowError("only an unresolved task retaining its original slot may recover cleanup")
            if not job or job["task_id"] != task_id or job["role"] != "glm" or job["state"] != "complete":
                raise WorkflowError("original terminal GLM provenance required")
            key = "cleanup_recovery:" + task_id
            previous = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            # The task pointer selects the one current cleanup job. Recoveries
            # themselves belong to an exact creation generation and dimension,
            # not to the entire resource type for its lifetime. Preserve legacy
            # pointers under their own binding before selecting a different one.
            def binding_key(value: dict[str, Any]) -> str:
                binding = {k: value.get(k) for k in ("original_job_id", "dimension", "authority")}
                return "cleanup_binding:" + task_id + ":" + hashlib.sha256(dumps(binding).encode()).hexdigest()

            if previous:
                prior_pointer = json.loads(previous[0])
                connection.execute("INSERT OR IGNORE INTO meta VALUES(?,?)",
                                   (binding_key(prior_pointer), previous[0]))
            history_key = binding_key(recovery)
            binding_previous = connection.execute("SELECT value FROM meta WHERE key=?", (history_key,)).fetchone()
            retry_id = recovery.get("retry_job_id")
            if binding_previous:
                prior = json.loads(binding_previous[0])
                retry = connection.execute("SELECT * FROM jobs WHERE id=?", (retry_id,)).fetchone()
                if (not retry or retry["task_id"] != task_id or retry["role"] != "glm"
                        or retry["purpose"] != "cleanup" or retry["state"] != "complete"
                        or not retry["thread_id"] or not retry["turn_id"]
                        or any(prior.get(k) != recovery.get(k) for k in ("original_job_id", "dimension", "authority"))
                        or prior.get("evidence_sha256") == recovery.get("evidence_sha256")
                        or connection.execute(
                            "SELECT 1 FROM meta WHERE key=?", ("cleanup_retry:" + retry_id,)
                        ).fetchone()):
                    raise WorkflowError("cleanup recovery already queued; fresh exact terminal retry review required")
                connection.execute("INSERT INTO meta VALUES(?,?)", ("cleanup_retry:" + retry_id, dumps(recovery)))
                self.event(connection, "ops_cleanup_retry_reviewed", {"task_id": task_id, "previous": prior,
                                                                     "recovery": recovery})
                connection.execute("UPDATE meta SET value=? WHERE key=?", (dumps(recovery), history_key))
            else:
                if retry_id is not None:
                    raise WorkflowError("cleanup retry requires an existing original recovery")
                connection.execute("INSERT INTO meta VALUES(?,?)", (history_key, dumps(recovery)))
            if previous and binding_key(json.loads(previous[0])) != history_key:
                self.event(connection, "ops_cleanup_binding_selected", {
                    "task_id": task_id, "previous": json.loads(previous[0]), "recovery": recovery,
                    "previous_review_preserved": True, "original_counters_preserved": True,
                })
            connection.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(recovery)))
            connection.execute(
                "UPDATE tasks SET state='cleanup_pending',reason=? WHERE id=?",
                ("OPS-reviewed original response-bound cleanup only", task_id),
            )
            self.event(connection, "ops_cleanup_queued", {"task_id": task_id, **recovery})

    def set_target(self, slot: int, target: str) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT task_id FROM slots WHERE id=?", (slot,)).fetchone()
            if not row or row[0]:
                raise WorkflowError("only an idle slot may receive an OPS target")
            connection.execute("UPDATE slots SET target=? WHERE id=?", (target, slot))
            self.event(connection, "slot_target", {"slot": slot, "target": target})

    def authorized(self, connection: sqlite3.Connection, job_id: str, token: str) -> sqlite3.Row:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or not secrets.compare_digest(job["capability"], hashlib.sha256(token.encode()).hexdigest()):
            raise WorkflowError("invalid job capability")
        if job["state"] not in {"queued", "running"}:
            raise WorkflowError("job is no longer active")
        return job

    @staticmethod
    def repair_safety(
        connection: sqlite3.Connection, job: sqlite3.Row, task: sqlite3.Row,
    ) -> dict[str, Any]:
        """Expose the existing repair barrier, not a contradictory global pause requirement."""
        active_glm = [row[0] for row in connection.execute(
            "SELECT id FROM jobs WHERE role='glm' AND state!='complete' ORDER BY id"
        )]
        active_sol = [row[0] for row in connection.execute(
            "SELECT id FROM jobs WHERE role='sol' AND state!='complete' ORDER BY id"
        )]
        scope = Store.repair_scope(connection, task["id"])
        barrier = task["state"] == "repairing" and scope == "product"
        sole_repair = (
            job["role"] == "sol" and job["purpose"] == "repair"
            and job["state"] in {"queued", "running"} and active_sol == [job["id"]]
            and bool(job["thread_id"] and job["turn_id"])
        )
        return {
            "job_id": job["id"], "task_id": task["id"],
            "thread_id": job["thread_id"], "turn_id": job["turn_id"],
            "task_state": task["state"], "repair_barrier_active": barrier,
            "repair_scope": scope,
            "new_execution_dispatch_blocked": barrier,
            "nonterminal_glm_jobs": active_glm, "glm_drained": not active_glm,
            "sole_bound_sol_repair": sole_repair,
            "product_write_allowed": sole_repair and barrier and not active_glm,
            "global_pause_required_for_bound_repair": False,
            "meaning": "Input repair cannot write product code and does not drain unrelated GLM. "
                       "Product repair blocks new execution and requires drained exact GLM turns; "
                       "repair_file rechecks SQLite before every source write.",
        }

    def escalate_repair(self, job_id: str, token: str, reason: str) -> None:
        """End an input-only repair before requesting the shared-code barrier.

        The same persistent Sol will receive the product repair through Luna only
        after this exact turn is terminal and GLM is drained. No in-turn privilege
        escalation, cloud retry, or lease expiry is permitted.
        """
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            if (job["role"] != "sol" or job["purpose"] != "repair" or not job["turn_id"]
                    or task["state"] != "repairing" or self.repair_scope(connection, task["id"]) != "input"
                    or not isinstance(reason, str) or not reason.strip()):
                raise WorkflowError("only a bound input repair may escalate with a precise product diagnosis")
            self.set_repair_scope(connection, task["id"], "product")
            connection.execute("UPDATE tasks SET state='repair',reason=? WHERE id=?", (reason, task["id"]))
            connection.execute("UPDATE jobs SET state='submitted',submission='result_staged' WHERE id=?", (job_id,))
            self.event(connection, "repair_escalated", {"task_id": task["id"], "job_id": job_id, "reason": reason})

    def repair_context(self, connection: sqlite3.Connection, task_id: str) -> tuple[Any, Any]:
        """Bind repair to the latest same-task cause; never revive an older OPS diagnosis."""
        causes = []
        for row in connection.execute(
            "SELECT id,kind,data,created FROM events WHERE kind IN "
            "('ops_preparation_repair','ops_input_repair','ops_preserved_repair',"
            "'acceptance_committed','repair_escalated') ORDER BY id DESC"
        ):
            value = json.loads(row["data"])
            if value.get("task_id") == task_id:
                causes.append((row, value))
        if not causes:
            return None, None
        row, value = causes[0]
        trigger = {"event_id": row["id"], "kind": row["kind"], "created": row["created"], "data": value}
        if row["kind"] == "repair_escalated":
            return None, trigger
        if row["kind"] != "acceptance_committed":
            if row["kind"] == "ops_preserved_repair":
                path = contained(self.root, value["evidence"])
                if digest(path) != value["sha256"]:
                    raise WorkflowError("preserved repair request changed")
                for source in read_json(path)["sources"]:
                    if digest(contained(self.root, source["path"])) != source["sha256"]:
                        raise WorkflowError("preserved repair original evidence changed")
            return value, trigger
        if value.get("receipt_path"):
            receipt_path = contained(self.root, value["receipt_path"])
            if digest(receipt_path) != value.get("receipt_sha256"):
                raise WorkflowError("current repair acceptance receipt changed")
            receipt = read_json(receipt_path)
            if receipt.get("task_id") != task_id:
                raise WorkflowError("current repair acceptance receipt task changed")
            trigger["decisions"] = receipt["decisions"]
        else:
            # Historical events have no receipt path. Preserve them as-is and
            # expose only verdicts recorded between the two actual acceptances.
            previous = next((older["created"] for older, _ in causes[1:]
                             if older["kind"] == "acceptance_committed"), 0)
            trigger["decisions"] = [json.loads(item[0]) for item in connection.execute(
                "SELECT receipt FROM verdicts WHERE task_id=? AND created>? AND created<=? ORDER BY id",
                (task_id, previous, row["created"]),
            )]
        return None, trigger

    def context(self, job_id: str, token: str) -> dict[str, Any]:
        from . import native_continuation
        from .hourly_budget import holds, policy
        from .temporary_samples import admitted

        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            slot = connection.execute("SELECT * FROM slots WHERE id=?", (task["slot"],)).fetchone()
            observations = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT data FROM observations WHERE task_id=? AND attempt=?", (task["id"], task["attempt"])
                )
            ]
            repair_request, repair_trigger = (
                self.repair_context(connection, task["id"]) if job["purpose"] == "repair" else (None, None)
            )
            handoff = connection.execute(
                "SELECT value FROM meta WHERE key=?", ("block_handoff:" + task["id"],)
            ).fetchone()
            if repair_request and repair_request.get("diagnostic_only"):
                self.input_repair_proof(task["id"], repair_request["input_path"], repair_request["sha256"])
            all_scenes = expected_scenes(json.loads(task["spec"]))
            operational_packet = read_json(contained(self.root, task["packet"])) if task["packet"] else None
            from . import existing_query

            existing = existing_query.load(self.root, connection, task["id"])
            if existing and (job["purpose"] == "continuation" or job["purpose"] == "review"):
                operational_packet = read_json(contained(self.root, existing["packet"]["path"]))
            else:
                existing = None
            temporary_scope = connection.execute(
                "SELECT value FROM meta WHERE key=?", ("temporary_samples:" + task["id"],)
            ).fetchone()
            active_scenes = all_scenes
            selection = None
            if task["packet"] and (job["role"] == "glm" or job["purpose"] == "review"):
                from .selection import selected_scenes, validate_authorization

                packet = read_json(contained(self.root, task["packet"]))
                if "execution_selection" in packet:
                    validate_authorization(self.root, packet)
                    active_scenes = selected_scenes(self.root, packet, json.loads(task["spec"]))
                    selection = packet["execution_selection"]
            cleanup_recovery = None
            if job["purpose"] == "cleanup":
                cleanup_recovery = json.loads(connection.execute(
                    "SELECT value FROM meta WHERE key=?", ("cleanup_recovery:" + task["id"],),
                ).fetchone()[0])
            return {
                "job_id": job_id,
                "role": job["role"],
                "purpose": job["purpose"],
                "task_id": task["id"],
                "attempt": task["attempt"],
                "resource": json.loads(task["spec"]),
                "readonly_retest_dimensions": ["QUERY"] if existing else sorted(
                    self.readonly_dimensions(connection, task, operational_packet)),
                "temporary_sample_admission": json.loads(temporary_scope[0]) if temporary_scope else None,
                "native_nas_query_sample": not existing and "QUERY" in admitted(
                    self.root, connection, task, operational_packet),
                "existing_query_recovery": existing,
                "hourly_fee_authorization": policy(self.root, connection, task["run_id"]),
                "hourly_cost_holds": holds(connection),
                "expected_scenes": [] if job["purpose"] == "cleanup" else active_scenes,
                "all_expected_scenes": all_scenes,
                "execution_selection": selection,
                "slot": dict(slot) if slot else None,
                "packet_path": existing["packet"]["path"] if existing else task["packet"],
                "task_reason": task["reason"],
                "ops_handoff": json.loads(handoff[0]) if handoff else None,
                "repair_request": repair_request,
                "repair_trigger": repair_trigger,
                "repair_safety": self.repair_safety(connection, job, task) if job["purpose"] == "repair" else None,
                "cleanup_recovery": cleanup_recovery,
                "navigation_recovery": (
                    self.navigation_recovery(connection, task) if job["purpose"] == "navigation" else None
                ) or (existing or {}).get("capture_recovery"),
                "native_continuation": (
                    native_continuation.load(self.root, connection, task["id"])
                    if (job["purpose"] == "continuation" and not existing)
                    or (cleanup_recovery or {}).get("native") else None
                ),
                "observations": observations,
                "register_owned_contract": (
                    {
                        "operation": "register_owned",
                        "dimension": "ROS | TF | CREATE",
                        "resource": "original resource object with nonempty string id and region",
                        "evidence": "state-relative original path STRING, or exactly {path, sha256}",
                        "sha256": "object form requires unchanged original file SHA256",
                        "effect": "ownership only; not cleanup confirmation, creation acceptance or navigation PASS",
                    }
                    if job["role"] == "sol" and job["purpose"] == "review"
                    else None
                ),
                "accept_contract": (
                    {
                        "operation": "accept",
                        "arguments_key": "receipt",
                        "required_receipt_keys": ["task_id", "attempt", "decisions", "cleanup"],
                        "decisions_shape": "ARRAY of objects; not a scene-keyed object",
                        "decision_entry": {
                            "scene": "exact expected_scenes id STRING",
                            "status": "pass | fail | unsupported | no_sample | execution_gap | product_gap",
                            "reason": "precise evidence-backed reason STRING",
                            "evidence": "state-relative original evidence path STRING",
                        },
                        "cleanup_shape": "ARRAY of objects; use [] only when no outstanding ownership",
                        "cleanup_ownership_ids": [
                            row[0]
                            for row in connection.execute(
                                "SELECT id FROM owned WHERE task_id=? AND state!='deleted' ORDER BY id", (task["id"],)
                            )
                        ],
                        "cleanup_entry": {
                            "ownership_id": "exact cleanup_ownership_ids member",
                            "confirmed_absent": True,
                            "evidence": "state-relative verification JSON path STRING; not a path/SHA object",
                        },
                        "historical_deleted_owners": "retain old proofs; exclude from this receipt cleanup",
                        "user_accepted_pending_delete": [
                            json.loads(row[0]) for row in connection.execute(
                                "SELECT data FROM events WHERE kind='ops_user_accepted_pending_delete' ORDER BY id"
                            ) if json.loads(row[0]).get("task_id") == task["id"]
                        ],
                        "render_evidence": {
                            "optional_decision_field": "render_evidence",
                            "shape": {
                                "basis": "original | supplemental", "path": "exact observation screenshot path",
                                "sha256": "unchanged inspected PNG SHA256", "reason": "precise visual basis",
                            },
                            "default": "supplemental when console_inspection exists; otherwise original",
                            "original": (
                                "explicit original basis may retain an incomplete supplemental capture; "
                                "both existing PNGs still require unchanged inspection, source binding and "
                                "readonly metadata safety. Sol must verify all required fields in the original."
                            ),
                            "supplemental": "requires source-bound complete readonly metadata and both PNG inspections",
                        },
                    }
                    if job["role"] == "sol" and job["purpose"] == "review"
                    else None
                ),
                "output_dir": str(self.root / "jobs" / job_id / "output"),
            }

    @staticmethod
    def plan_sol_thread(connection: sqlite3.Connection, run_id: str) -> str | None:
        """Keep one Sol per frozen plan, not one forever across independent runs.

        Existing runs recover their original binding from immutable job history;
        the legacy global meta value is deliberately not inherited by new runs.
        """
        row = connection.execute("SELECT value FROM meta WHERE key=?", ("sol_thread:" + run_id,)).fetchone()
        threads = {job[0] for job in connection.execute(
            "SELECT DISTINCT j.thread_id FROM jobs j JOIN tasks t ON t.id=j.task_id "
            "WHERE t.run_id=? AND j.role='sol' AND j.thread_id IS NOT NULL", (run_id,)
        )}
        if row and row[0]:
            threads.add(row[0])
        if len(threads) > 1:
            raise WorkflowError("conflicting Sol bindings within the same plan; OPS inspect original history")
        return next(iter(threads), None)

    def bind(self, job_id: str, token: str, thread: str, turn: str | None = None) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["thread_id"] and job["thread_id"] != thread:
                raise WorkflowError("cannot rebind a job to a different thread")
            if job["turn_id"] and job["turn_id"] != turn:
                raise WorkflowError("cannot rebind a job to a different turn")
            if job["role"] == "sol":
                run_id = connection.execute("SELECT run_id FROM tasks WHERE id=?", (job["task_id"],)).fetchone()[0]
                saved = self.plan_sol_thread(connection, run_id)
                if saved and saved != thread:
                    raise WorkflowError("only one persistent Sol thread is permitted within this plan")
                connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                                   ("sol_thread:" + run_id, thread))
            connection.execute(
                "UPDATE jobs SET thread_id=?,turn_id=?,submission=?,state=? WHERE id=?",
                (thread, turn, "confirmed" if turn else "thread_bound", "running", job_id),
            )
            self.event(connection, "model_bound", {"job_id": job_id, "thread": thread, "turn": turn})

    def mark_submission(self, job_id: str, token: str) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if (
                self.review_only_reason(connection)
                and job["purpose"] != "cleanup"
                and (job["role"] != "sol" or job["purpose"] not in {"review", "repair"})
                and not self.reviewed_continuation(connection, job)
            ):
                raise WorkflowError("review-only hold forbids new execution/preparation turns")
            if job["submission"] not in {"not_submitted", "thread_bound"}:
                raise WorkflowError("submission is not safe to repeat")
            connection.execute("UPDATE jobs SET submission='may_have_submitted' WHERE id=?", (job_id,))
            self.event(connection, "submission_boundary", {"job_id": job_id})

    def park(self, job_id: str, reason: str, *, only_active: bool = False) -> None:
        with self.transaction() as connection:
            if only_active and not connection.execute(
                "SELECT 1 FROM jobs WHERE id=? AND state IN ('queued','running','submitted')", (job_id,)
            ).fetchone():
                return
            connection.execute(
                "UPDATE jobs SET state='unknown' WHERE id=? AND state IN ('queued','running','submitted')", (job_id,)
            )
            connection.execute(
                "UPDATE tasks SET reason=? WHERE id=(SELECT task_id FROM jobs WHERE id=?)", (reason, job_id)
            )
            self.event(connection, "job_parked", {"job_id": job_id, "reason": reason})

    def approve_packet(self, job_id: str, token: str, packet: dict[str, Any]) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "sol" or job["purpose"] not in {"prepare", "repair"}:
                raise WorkflowError("only Sol preparation/repair may provide a packet")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            request, _ = self.repair_context(connection, task["id"]) if job["purpose"] == "repair" else (None, None)
            if request and request.get("ownership_preserved"):
                raise WorkflowError("preserved repair cannot approve/reinstall execution; hand off to OPS with block")
            if packet.get("task_id") != task["id"] or packet.get("profile") != "test-guima":
                raise WorkflowError("packet task/profile mismatch")
            requests = packet.get("requests", {})
            spec = json.loads(task["spec"])
            from .selection import selected_scenes
            from .selection import validate_authorization as validate_selection_authorization

            active_scenes = selected_scenes(self.root, packet, spec)
            validate_selection_authorization(self.root, packet)
            needed = {scene["dimension"] for scene in active_scenes}
            # Only an independently authorized stage may defer unresolved source
            # support. Keep normal four-dimension and unselected retest checks.
            support_dimensions = set(spec["dimensions"])
            if "retest_scope" in spec:
                support_dimensions = needed if "execution_selection" in packet else retest_dimensions(spec)
            if any(spec["dimensions"][dimension]["support"] == "unknown" for dimension in support_dimensions):
                raise WorkflowError("resolve four dimensions from official evidence before approving execution")
            readonly = self.readonly_dimensions(connection, task, packet)
            from .temporary_samples import admitted

            native_nas_query_sample = "QUERY" in admitted(self.root, connection, task, packet)
            if set(requests) != needed or any(
                not isinstance(text, str) or not text.strip() for text in requests.values()
            ):
                raise WorkflowError("packet must cover exactly the missing source dimensions")
            if packet.get("terraform") != "ros_managed_terraform":
                raise WorkflowError("packet must use ROS hosted Terraform")
            if not isinstance(packet.get("budget_cents"), int) or isinstance(packet["budget_cents"], bool):
                raise WorkflowError("packet budget is required")
            if packet["budget_cents"] < 0:
                raise WorkflowError("negative budget")
            from .creation import execution_digest, requires_execution_review

            if not requires_execution_review(packet) and task["packet"]:
                original = read_json(contained(self.root, task["packet"]))
                if execution_digest(original) != execution_digest(packet):
                    raise WorkflowError(
                        "changed initial QUERY execution requires independent OPS review; "
                        "retain original calls/quotas or use an explicit reviewed continuation"
                    )
            if requires_execution_review(packet):
                from .creation import validate_authorization

                validate_authorization(self.root, task["resource_id"], packet)
                cleanup_requests = packet.get("cleanup_requests", {})
                if any(
                    not isinstance(cleanup_requests.get(dimension), str) or not cleanup_requests[dimension].strip()
                    for dimension in needed - (set() if native_nas_query_sample else {"QUERY"}) - readonly
                ):
                    raise WorkflowError("every creation dimension needs its own approved cleanup request")
            required_inputs = packet.get("inputs", [])
            if not required_inputs:
                raise WorkflowError("source/official evidence cannot be empty")
            for evidence in required_inputs:
                path = contained(self.root, evidence["path"])
                if digest(path) != evidence["sha256"]:
                    raise WorkflowError("packet input SHA mismatch")
            wanted = {scene["id"] for scene in active_scenes}
            if set(packet.get("scenes", {})) != wanted:
                raise WorkflowError("packet needs exact provenance/selector for every missing scene")
            for binding in packet["scenes"].values():
                if not all(
                    binding.get(key) for key in ("selector", "source_evidence", "source_sha256", "account", "region")
                ):
                    raise WorkflowError("scene provenance is incomplete")
                if digest(contained(self.root, binding["source_evidence"])) != binding["source_sha256"]:
                    raise WorkflowError("scene source evidence SHA mismatch")
            from .creation import validate_execution

            validate_execution(packet, needed, require_read_policy=spec.get("execution_policy_version", 1) == 2,
                               readonly_dimensions=readonly, native_nas_query_sample=native_nas_query_sample)
            if any(
                not isinstance(packet.get("ui", {}).get(key), str) or not packet["ui"][key].strip()
                for key in ("new_session_selector", "input_selector", "send_selector", "busy_selector")
            ):
                raise WorkflowError("packet must bind the four reviewed product UI selectors")
            relative = f"packets/{job_id}/packet.json"
            json_write(contained(self.root, relative), packet)
            connection.execute("UPDATE tasks SET packet=?,state='ready',reason=NULL WHERE id=?", (relative, task["id"]))
            connection.execute("UPDATE jobs SET state='submitted',submission='result_staged' WHERE id=?", (job_id,))
            self.event(
                connection, "packet_approved", {"task_id": task["id"], "packet_sha": digest(self.root / relative)}
            )

    def resolve_dimensions(self, job_id: str, token: str, dimensions: dict[str, Any]) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "sol" or job["purpose"] not in {"prepare", "repair"}:
                raise WorkflowError("only Sol can resolve official applicability/aliases")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            spec = json.loads(task["spec"])
            if set(dimensions) != set(spec["dimensions"]):
                raise WorkflowError("all four dimensions required")
            for dimension, source in dimensions.items():
                basis = source.get("official_evidence")
                if not basis or not contained(self.root, basis).is_file():
                    raise WorkflowError("each applicability decision requires saved official evidence")
                old = spec["dimensions"][dimension]
                if "retest_scope" in spec:
                    if dimension not in retest_dimensions(spec) and {
                        k: v for k, v in source.items() if k != "official_evidence"
                    } != {k: v for k, v in old.items() if k != "official_evidence"}:
                        raise WorkflowError("targeted retest cannot alter an unselected dimension")
                    for scene in spec["retest_scope"]["scene_ids"]:
                        contract_id = scene.rsplit("-", 1)[0]
                        before = next((c for c in old["contracts"] if c["id"] == contract_id), None)
                        if before is None:
                            continue
                        after = next((c for c in source.get("contracts", []) if c["id"] == contract_id), None)
                        if after != before:
                            raise WorkflowError("frozen retest contract/source cannot change")
                    if source.get("creation_verified", False) != old.get("creation_verified", False):
                        raise WorkflowError("navigation retest cannot manufacture a creation checkpoint")
                preserved = {
                    c["id"]: c
                    for c in old["contracts"]
                    if any(status in {"pass", "unsupported"} for status in c["historical"].values())
                }
                new = {c["id"]: c for c in source.get("contracts", [])}
                for key, contract in preserved.items():
                    if key not in new or new[key]["source_type"] != contract["source_type"]:
                        raise WorkflowError("cannot remove/rename an accepted precise contract")
                    for mode, status in contract["historical"].items():
                        if status in {"pass", "unsupported"} and new[key]["historical"][mode] != status:
                            raise WorkflowError("cannot discard an accepted checkpoint")
                for key, contract in new.items():
                    for mode, status in contract["historical"].items():
                        prior = next((c for c in old["contracts"] if c["id"] == key), None)
                        if status in {"pass", "unsupported"} and (not prior or prior["historical"][mode] != status):
                            raise WorkflowError("official applicability cannot manufacture a PASS")
                if source.get("creation_verified", False) != old.get("creation_verified", False):
                    raise WorkflowError("official applicability cannot change creation checkpoint")
            spec["dimensions"] = dimensions
            frozen = json.loads(connection.execute("SELECT plan FROM runs WHERE id=?", (task["run_id"],)).fetchone()[0])
            candidate = {**frozen, "resources": [spec if item["id"] == spec["id"] else item
                                                 for item in frozen["resources"]]}
            validate_plan(candidate)
            validate_review_lineage(candidate)
            connection.execute("UPDATE tasks SET spec=? WHERE id=?", (dumps(spec), task["id"]))
            self.event(connection, "official_dimensions_resolved", {"task_id": task["id"]})

    def record_observation(self, job_id: str, token: str, value: dict[str, Any]) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            if job["role"] != "glm" or not job["turn_id"]:
                raise WorkflowError("observations require the exact bound GLM turn")
            from .selection import selected_scenes, validate_authorization

            spec = json.loads(task["spec"])
            allowed_scenes = expected_scenes(spec)
            if task["packet"]:
                packet = read_json(contained(self.root, task["packet"]))
                if "execution_selection" in packet:
                    validate_authorization(self.root, packet)
                    allowed_scenes = selected_scenes(self.root, packet, spec)
            if value.get("scene") not in {scene["id"] for scene in allowed_scenes}:
                raise WorkflowError("unknown scene or preserved PASS replay")
            identity = hashlib.sha256(dumps([task["id"], task["attempt"], value["scene"]]).encode()).hexdigest()
            old = connection.execute("SELECT data FROM observations WHERE id=?", (identity,)).fetchone()
            if old and old[0] != dumps(value):
                raise WorkflowError("original observation is immutable; use a new attempt")
            connection.execute(
                "INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                (identity, task["id"], task["attempt"], value["scene"], dumps(value)),
            )
            self.event(connection, "observation_recorded", {"task_id": task["id"], "scene": value["scene"]})

    def register_owned(self, job_id: str, token: str, dimension: str, resource: dict[str, Any], evidence: str) -> str:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "glm" or dimension not in {"ROS", "TF", "CREATE"}:
                raise WorkflowError("invalid resource ownership registration")
            source = contained(self.root, evidence)
            if not source.is_file() or not resource.get("id") or not resource.get("region"):
                raise WorkflowError("ownership needs raw creation response, ID and region")
            identity = register_generation(
                connection, self.root, job["task_id"], dimension, resource, job_id,
                {"path": evidence, "sha256": digest(source)},
            )
            self.event(connection, "owned_resource", {"ownership_id": identity, "task_id": job["task_id"]})
            return identity

    def submit(self, job_id: str, token: str, report: str) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "glm" or not job["turn_id"] or len(report.encode()) > 64000:
                raise WorkflowError("invalid worker report")
            json_write(
                self.root / "jobs" / job_id / "output" / "submission.json",
                {"job_id": job_id, "thread_id": job["thread_id"], "turn_id": job["turn_id"], "report": report},
            )
            connection.execute("UPDATE jobs SET state='submitted',submission='result_staged' WHERE id=?", (job_id,))
            connection.execute("UPDATE tasks SET state='reported' WHERE id=?", (job["task_id"],))
            # Keep the slot/resources until independent verification and cleanup; a worker terminal is not a close.
            self.event(connection, "worker_reported", {"job_id": job_id})

    def inspected(self, job_id: str, token: str, relative: str) -> dict[str, Any]:
        path = contained(self.root, relative)
        if path.suffix.lower() != ".png" or not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise WorkflowError("acceptance requires a real PNG")
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "sol" or job["purpose"] not in {"review", "repair"}:
                raise WorkflowError("only Sol review or repair can inspect this resource's original screenshots")
            allowed = []
            for row in connection.execute("SELECT data FROM observations WHERE task_id=?", (job["task_id"],)):
                observation = json.loads(row[0])
                allowed.extend((observation.get("screenshot"),
                                observation.get("console_inspection", {}).get("screenshot")))
                allowed.extend(capture.get("screenshot") for capture in
                               observation.get("console_inspection", {}).get("supplemental_captures", []))
            diagnostic_only = False
            registrations = [
                registration
                for row in connection.execute("SELECT data FROM events WHERE kind='ops_input_repair' ORDER BY id DESC")
                if (registration := json.loads(row[0]))["task_id"] == job["task_id"]
            ]
            registered_diagnostic = any(
                relative in registration.get("diagnostic_image_paths", []) for registration in registrations
            )
            if registered_diagnostic and job["purpose"] != "repair":
                raise WorkflowError("OPS diagnostic-only pixels are for repair, not formal review")
            if (relative not in allowed or registered_diagnostic) and job["purpose"] == "repair" and registrations:
                registration = registrations[0]
                proof = self.input_repair_proof(
                    job["task_id"], registration["input_path"], registration["sha256"],
                )
                diagnostic_only = any(item["path"] == relative for item in proof["diagnostic_images"])
            if relative not in allowed and not diagnostic_only:
                raise WorkflowError("screenshot does not belong to this resource")
            sha = digest(path)
            connection.execute("INSERT OR REPLACE INTO inspected VALUES(?,?,?)", (job_id, relative, sha))
            result = {"path": relative, "sha256": sha}
            if diagnostic_only:
                result.update({"evidence_role": "diagnostic_only", "eligible_for_verdict": False})
            return result

    def accept(self, job_id: str, token: str, receipt: dict[str, Any]) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "sol" or job["purpose"] != "review" or not job["turn_id"]:
                raise WorkflowError("acceptance requires the exact bound Sol review turn")
            if not isinstance(receipt, dict):
                raise WorkflowError("receipt must be an object containing task_id, attempt, decisions and cleanup")
            decisions = receipt.get("decisions")
            if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
                raise WorkflowError("receipt.decisions must be an array of objects with scene/status/reason/evidence")
            if any(not isinstance(item.get("scene"), str) or not item["scene"] for item in decisions):
                raise WorkflowError("each receipt.decisions entry needs a nonempty scene string")
            cleanup = receipt.get("cleanup")
            if not isinstance(cleanup, list) or any(not isinstance(item, dict) for item in cleanup):
                raise WorkflowError("receipt.cleanup must be an array of ownership confirmation objects")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            if receipt.get("task_id") != task["id"] or receipt.get("attempt") != task["attempt"]:
                raise WorkflowError("stale acceptance receipt")
            from scripts.web.e2e.resource_link_workflow.execution_gate import creations

            workers = [
                row[0]
                for row in connection.execute("SELECT id FROM jobs WHERE task_id=? AND role='glm'", (task["id"],))
            ]
            reconciled = self.reconciled_creations(connection, task["id"])
            for record in creations(self.root / "execution-gate.sqlite3", workers):
                if record["attempt_id"] in reconciled:
                    continue  # OPS-proved rejection, not success, ownership or a navigation verdict.
                if record["state"] != "succeeded" or not record["resource"]["id"]:
                    raise WorkflowError("wire creation outcome unknown; reconcile original evidence before close")
                identity = owner_key(
                    connection, self.root, task["id"], record["dimension"], record["resource"], record["job_id"],
                )
                owner = connection.execute(
                    "SELECT * FROM owned WHERE id=? AND task_id=? AND dimension=? AND resource=?",
                    (identity, task["id"], record["dimension"], dumps(record["resource"])),
                ).fetchone()
                if not owner:
                    raise WorkflowError("wire-created generation missing from ownership/cleanup ledger")
                from .additional_source import reviewed_external_cleanup
                from .phases import assert_cleanup_reads_complete
                from .retained_cleanup import registered_network_cleanup

                scope = f"{record['job_id']}:{record['dimension']}"
                if not (owner["state"] == "deleted" and (
                    reviewed_external_cleanup(self.root, task["id"], scope, job_id)
                    or registered_network_cleanup(self.root, connection, task["id"], owner, record)
                )):
                    assert_cleanup_reads_complete(self.root / "execution-gate.sqlite3", scope)
            from .selection import selected_scenes, validate_authorization

            active_scenes = expected_scenes(json.loads(task["spec"]))
            selected_packet = None
            if task["packet"]:
                packet = read_json(contained(self.root, task["packet"]))
                if "execution_selection" in packet:
                    validate_authorization(self.root, packet)
                    active_scenes = selected_scenes(self.root, packet, json.loads(task["spec"]))
                    selected_packet = packet
            expected = {scene["id"] for scene in active_scenes}
            if len(decisions) != len(expected) or {item.get("scene") for item in decisions} != expected:
                raise WorkflowError("receipt must classify all and only missing scenes")
            diagnostic_images = {
                path
                for row in connection.execute("SELECT data FROM events WHERE kind='ops_input_repair'")
                if (registration := json.loads(row[0])).get("task_id") == task["id"]
                for path in registration.get("diagnostic_image_paths", [])
            }
            needs_retry = False
            for item in decisions:
                status = item.get("status")
                if status not in {"pass", "fail", "unsupported", "no_sample", "execution_gap", "product_gap"}:
                    raise WorkflowError("invalid verdict")
                if not item.get("reason"):
                    raise WorkflowError("every verdict needs its precise reason")
                observation = connection.execute(
                    "SELECT data FROM observations WHERE task_id=? AND attempt=? AND scene=?",
                    (task["id"], task["attempt"], item["scene"]),
                ).fetchone()
                observation = json.loads(observation[0]) if observation else {}
                if status in {"pass", "fail", "unsupported"}:
                    artifact = observation.get("screenshot")
                    if artifact in diagnostic_images or observation.get("console_inspection", {}).get(
                        "screenshot"
                    ) in diagnostic_images:
                        raise WorkflowError("OPS diagnostic-only pixels cannot substitute formal test observations")
                    view = connection.execute(
                        "SELECT sha FROM inspected WHERE job_id=? AND artifact=?", (job_id, artifact)
                    ).fetchone()
                    if not artifact or not view or view[0] != digest(contained(self.root, artifact)):
                        raise WorkflowError("Sol must inspect unchanged original screenshot before verdict")
                    if not observation.get("source_evidence") or not observation.get("session_id"):
                        raise WorkflowError("screenshot lacks source/session provenance")
                    source = contained(self.root, observation["source_evidence"])
                    if digest(source) != observation.get("source_sha256"):
                        raise WorkflowError("source evidence changed")
                    if (
                        status in {"pass", "fail"}
                        and not observation.get("actual_ui_click")
                        and not item["scene"].endswith("creation")
                    ):
                        raise WorkflowError("unclicked scene is an execution gap, not PASS/FAIL")
                    if status == "pass" and not all(
                        item.get(key) is True for key in ("identity_verified", "login_verified", "render_verified")
                    ):
                        raise WorkflowError("PASS requires explicit visual identity/login/render verification")
                    if (
                        status == "pass" and item["scene"].endswith("-creation")
                        and observation.get("creation_source_verified") is False
                    ):
                        raise WorkflowError("diagnostic creation screenshot lacks current signed creation source")
                    supplemental = observation.get("console_inspection")
                    render_basis = "supplemental" if supplemental else "original"
                    if status == "pass" and "render_evidence" in item:
                        chosen = item["render_evidence"]
                        if not isinstance(chosen, dict) or chosen.get("basis") not in {"original", "supplemental"}:
                            raise WorkflowError("render evidence must select original or supplemental inspected PNG")
                        render_basis = chosen["basis"]
                        chosen_artifact = artifact if render_basis == "original" else (
                            supplemental.get("screenshot") if supplemental else None
                        )
                        if (
                            not chosen_artifact or chosen.get("path") != chosen_artifact
                            or chosen.get("sha256") != digest(contained(self.root, chosen_artifact))
                            or not isinstance(chosen.get("reason"), str) or not chosen["reason"].strip()
                        ):
                            raise WorkflowError("render evidence must bind the unchanged original observation PNG")
                    if status == "pass" and supplemental:
                        supplemental_completed = (
                            supplemental.get("readonly_completed") is True
                            and supplemental.get("original_landing_unchanged") is True
                            and supplemental.get("cloud_writes") == 0
                            if supplemental.get("kind") in {
                                "slb_acl_metadata", "ecs_keypair_metadata", "resource_group_metadata",
                                "router_interface_vbr_child",
                                "ess_scheduled_task_metadata",
                                "oos_template_metadata", "oos_state_configuration_list",
                                "nas_protocol_mount_target_child",
                                "nas_fileset_description",
                                "nas_dataflow_metadata",
                                "nas_protocol_service_identity",
                                "sls_project_metadata",
                                "nas_mount_target_address",
                                "ots_instance_endpoint",
                                "ots_vpc_binding_row",
                                "sae_application_metadata",
                                "rocketmq_instance_notice",
                            }
                            else supplemental.get("kind") in (None, "nas_access_group_metadata")
                            and supplemental.get("dialog_cancelled") is True
                        )
                        supplemental_artifact = supplemental.get("screenshot")
                        if supplemental.get("kind") in {"nas_protocol_mount_target_child", "nas_dataflow_metadata"}:
                            captures = supplemental.get("supplemental_captures", [])
                            required = ({"path", "source"} if supplemental["kind"] == "nas_dataflow_metadata"
                                        else {"export_id", "path", "mount"})
                            if (len(captures) != len(required)
                                    or {capture.get("kind") for capture in captures} != required):
                                raise WorkflowError("NAS metadata requires every unchanged native field capture")
                            for capture in captures:
                                capture_path = capture.get("screenshot")
                                inspected = connection.execute(
                                    "SELECT sha FROM inspected WHERE job_id=? AND artifact=?", (job_id, capture_path)
                                ).fetchone()
                                if (not capture_path or not inspected or capture_path in diagnostic_images
                                        or capture.get("sha256") != inspected[0]
                                        or inspected[0] != digest(contained(self.root, capture_path))):
                                    raise WorkflowError("Sol must inspect every unchanged NAS native tooltip PNG")
                        supplemental_view = connection.execute(
                            "SELECT sha FROM inspected WHERE job_id=? AND artifact=?",
                            (job_id, supplemental_artifact),
                        ).fetchone()
                        original_basis_safe = (
                            render_basis == "original"
                            and supplemental.get("kind") in {
                                "slb_acl_metadata", "ecs_keypair_metadata", "resource_group_metadata"
                            }
                            and supplemental.get("original_landing_unchanged") is True
                            and supplemental.get("cloud_writes") == 0
                        )
                        if (
                            not supplemental_artifact or not supplemental_view
                            or supplemental_view[0] != digest(contained(self.root, supplemental_artifact))
                            or not (original_basis_safe or (
                                supplemental.get("identity_visible") is True and supplemental_completed
                            ))
                        ):
                            raise WorkflowError(
                                "Sol must inspect unchanged source-bound supplemental metadata screenshot"
                            )
                    if status == "unsupported" and (
                        not item["scene"].endswith("-iframe") or not item.get("official_basis")
                    ):
                        raise WorkflowError("unsupported iframe requires a verifiable policy/header basis")
                if status == "no_sample":
                    coverage = item.get("coverage_evidence")
                    if not isinstance(coverage, str) or not contained(self.root, coverage).is_file():
                        raise WorkflowError("no_sample requires saved bounded coverage and failure blind spots")
                if status in {"execution_gap", "product_gap", "fail"}:
                    needs_retry = True
                connection.execute(
                    "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
                    (task["id"], item["scene"], status, dumps(item), time.time()),
                )
            owned = list(connection.execute("SELECT * FROM owned WHERE task_id=? AND state!='deleted'", (task["id"],)))
            for item in decisions:
                if item["status"] == "pass" and item["scene"].endswith("-creation"):
                    dimension = item["scene"].split("-")[-2]
                    if not any(row["dimension"] == dimension for row in owned):
                        # OPS can verify deletion before a retained screenshot is reviewed. Do not
                        # require a new creation merely because the original owner is already deleted.
                        deleted = list(connection.execute(
                            "SELECT * FROM owned WHERE task_id=? AND dimension=? AND state='deleted'",
                            (task["id"], dimension),
                        ))
                        if not deleted:
                            raise WorkflowError("creation PASS requires an owned resource and verified cleanup")
                        for owner in deleted:
                            binding = json.loads(owner["evidence"])
                            proof = contained(self.root, binding.get("path", ""))
                            if not proof.is_file() or digest(proof) != binding.get("sha256"):
                                raise WorkflowError("retained creation cleanup proof changed")
                            verification = read_json(proof)
                            accepted_pending = verification.get("kind") == "ops_user_accepted_pending_delete"
                            if accepted_pending:
                                from .pending_delete import validate

                                validate(
                                    self.root, task["id"], owner["id"], json.loads(owner["resource"]), verification,
                                )
                            if (
                                verification.get("ownership_id") != owner["id"]
                                or verification.get("resource") != json.loads(owner["resource"])
                                or (verification.get("absent") is not True and not accepted_pending)
                                or verification.get("failed") is not False
                            ):
                                raise WorkflowError("retained creation cleanup must bind exact deleted owner")
                            raw_source = contained(self.root, verification.get("source_evidence", ""))
                            if not raw_source.is_file() or digest(raw_source) != verification.get("source_sha256"):
                                raise WorkflowError("retained creation cleanup original query proof changed")
            cleanup = receipt.get("cleanup", [])
            if {item.get("ownership_id") for item in cleanup} != {row["id"] for row in owned}:
                raise WorkflowError("cleanup must account for every owned resource, including failures")
            for item in cleanup:
                proof = contained(self.root, item["evidence"])
                if item.get("confirmed_absent") is not True or not proof.is_file():
                    raise WorkflowError("cleanup is unconfirmed; keep the lease and slot")
                verification = read_json(proof)
                owner = next(row for row in owned if row["id"] == item["ownership_id"])
                if (
                    verification.get("ownership_id") != owner["id"]
                    or verification.get("resource") != json.loads(owner["resource"])
                    or verification.get("absent") is not True
                    or verification.get("failed") is not False
                ):
                    raise WorkflowError("cleanup proof must bind exact resource and successful bounded absence check")
                raw_source = contained(self.root, verification["source_evidence"])
                if digest(raw_source) != verification.get("source_sha256"):
                    raise WorkflowError("cleanup original query proof changed")
                connection.execute(
                    "UPDATE owned SET state='deleted',evidence=? WHERE id=?",
                    (dumps({"path": item["evidence"], "sha256": digest(proof)}), item["ownership_id"]),
                )
            spec = json.loads(task["spec"])
            # Retain precise positive checkpoints in subsequent attempts; never repeat them to fix a sibling.
            for item in decisions:
                if item["status"] not in {"pass", "unsupported"}:
                    continue
                for dimension, source in spec["dimensions"].items():
                    if item["scene"] == f"{spec['id']}-{dimension}-creation" and item["status"] == "pass":
                        source["creation_verified"] = True
                    for contract in source.get("contracts", []):
                        for mode in ("iframe", "open"):
                            if item["scene"] == f"{contract['id']}-{mode}":
                                contract["historical"][mode] = item["status"]
            connection.execute("UPDATE tasks SET spec=? WHERE id=?", (dumps(spec), task["id"]))
            receipt_path = self.root / "receipts" / f"{job_id}.json"
            json_write(receipt_path, {**receipt, "sol_thread": job["thread_id"], "sol_turn": job["turn_id"]})
            connection.execute("UPDATE jobs SET state='submitted',submission='result_staged' WHERE id=?", (job_id,))
            remaining = expected_scenes(spec)
            blocked_reason = task["reason"]
            next_state = "repair" if needs_retry else "complete"
            repair_scope = (
                "product" if any(item["status"] in {"product_gap", "fail"} for item in decisions) else "input"
            )
            if needs_retry:
                self.set_repair_scope(connection, task["id"], repair_scope)
            if selected_packet is not None and remaining and not needs_retry:
                next_state = "blocked"
                blocked_reason = "Partial execution accepted and cleaned; deferred scenes remain unresolved: " + dumps(
                    selected_packet["execution_selection"]["deferred_scenes"]
                )
            connection.execute(
                "UPDATE tasks SET state=?,slot=NULL,reason=? WHERE id=?", (next_state, blocked_reason, task["id"])
            )
            self.revoke_task_capabilities(connection, task["id"])
            from .hourly_budget import release_verified

            release_verified(self, connection, task["id"], {
                "kind": "sol_verified_cleanup", "evidence": receipt_path.relative_to(self.root).as_posix(),
                "sha256": digest(receipt_path), "job_id": job_id,
            })
            connection.execute("UPDATE slots SET task_id=NULL WHERE task_id=?", (task["id"],))
            connection.execute("DELETE FROM locks WHERE task_id=?", (task["id"],))
            self.event(connection, "acceptance_committed", {
                "task_id": task["id"], "retry": needs_retry,
                "job_id": job_id, "attempt": task["attempt"],
                "receipt_path": receipt_path.relative_to(self.root).as_posix(), "receipt_sha256": digest(receipt_path),
                "deferred_remaining": [scene["id"] for scene in remaining] if selected_packet else [],
                "complete": next_state == "complete",
                "repair_scope": repair_scope if needs_retry else None,
            })

    def block(self, job_id: str, token: str, reason: str, handoff: dict[str, Any] | None = None) -> None:
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "sol" or not isinstance(reason, str) or not reason.strip():
                raise WorkflowError("only Sol may classify a precise preparation blocker")
            if handoff is not None:
                if (not isinstance(handoff, dict) or set(handoff) != {"category", "artifacts"}
                        or handoff["category"] not in {"creation_authorization", "input", "environment", "permissions",
                                                       "transport", "cleanup", "user_decision"}
                        or not isinstance(handoff["artifacts"], list) or not handoff["artifacts"]):
                    raise WorkflowError("OPS handoff needs a known category and exact original artifact SHAs")
                for artifact in handoff["artifacts"]:
                    if (not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}
                            or not isinstance(artifact["path"], str) or not artifact["path"].startswith("inputs/")
                            or digest(contained(self.root, artifact["path"])) != artifact["sha256"]):
                        raise WorkflowError("OPS handoff original artifact changed or is outside inputs")
                handoff = {**handoff, "job_id": job_id, "thread_id": job["thread_id"], "turn_id": job["turn_id"]}
            connection.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("block_handoff:" + job["task_id"], dumps(handoff)),
            )
            connection.execute("UPDATE jobs SET state='submitted',submission='result_staged' WHERE id=?", (job_id,))
            connection.execute("UPDATE tasks SET state='blocked',reason=? WHERE id=?", (reason, job["task_id"]))
            self.event(connection, "task_blocked", {"task_id": job["task_id"], "reason": reason, "handoff": handoff})

    def classify_preparation(self, job_id: str, token: str, coverage: str | None = None) -> None:
        """No empty GLM dispatch. Absence of query samples cannot waive supported creation."""
        with self.transaction() as connection:
            job = self.authorized(connection, job_id, token)
            if job["role"] != "sol" or job["purpose"] != "prepare" or not job["turn_id"]:
                raise WorkflowError("preparation classification requires bound Sol turn")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],)).fetchone()
            spec = json.loads(task["spec"])
            if any(source["support"] == "unknown" for dimension, source in spec["dimensions"].items()
                   if "retest_scope" not in spec or dimension in retest_dimensions(spec)):
                raise WorkflowError("unknown support is not no_sample/not_applicable")
            scenes = expected_scenes(spec)
            if scenes:
                if any(scene["dimension"] != "QUERY" for scene in scenes):
                    raise WorkflowError("no existing query sample cannot waive supported creation tests")
                if not coverage:
                    raise WorkflowError("bounded coverage required")
                value = read_json(contained(self.root, coverage))
                if value.get("resources_found") != 0 or not value.get("coverage") or "failure_blind_spots" not in value:
                    raise WorkflowError("no_sample requires actual scope, zero findings and explicit blind spots")
                for scene in scenes:
                    receipt = {
                        "scene": scene["id"],
                        "status": "no_sample",
                        "coverage_evidence": coverage,
                        "reason": "No sample in recorded bounded scope; not a global absence claim",
                    }
                    connection.execute(
                        "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
                        (task["id"], scene["id"], "no_sample", dumps(receipt), time.time()),
                    )
            connection.execute("UPDATE tasks SET state='complete' WHERE id=?", (task["id"],))
            connection.execute("UPDATE jobs SET state='submitted',submission='result_staged' WHERE id=?", (job_id,))
            self.event(connection, "preparation_classified", {"task_id": task["id"], "coverage": coverage})

    def retry_blocked(self, task_id: str, reason: str) -> None:
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["state"] != "blocked" or task["slot"] is not None or not reason:
                raise WorkflowError("only a blocked task without a live execution slot may be re-prepared")
            if connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND state!='complete'", (task_id,)).fetchone():
                raise WorkflowError("blocked retry requires exact original model turns terminal")
            connection.execute("UPDATE tasks SET state='pending',reason=? WHERE id=?", (reason, task_id))
            self.event(connection, "blocked_retry", {"task_id": task_id, "reason": reason})

    def input_repair_proof(self, task_id: str, evidence: str, expected_sha: str | None = None) -> dict[str, Any]:
        """Immutable OPS input bundle, explicitly separate from observations and verdicts."""
        path = contained(self.root / "inputs", evidence)
        proof = read_json(path)
        resource_id = task_id.rsplit(":", 1)[-1]
        if (
            (expected_sha is not None and digest(path) != expected_sha)
            or proof.get("schema_version") != 1 or proof.get("kind") != "ops_input_repair"
            or proof.get("task_id") != task_id or not proof.get("diagnosis")
            or proof.get("formal_test") is not False or proof.get("pass_added") is not False
            or not isinstance(proof.get("sources"), list) or not proof["sources"]
            or not isinstance(proof.get("diagnostic_images"), list)
            or not 1 <= len(proof["diagnostic_images"]) <= 16
            or path.relative_to(self.root / "inputs").parts[0] != resource_id
        ):
            raise WorkflowError("input repair needs unchanged exact task OPS diagnostic-only bundle")
        def original_input(relative: str, *, own_resource: bool = False) -> Path:
            if not isinstance(relative, str) or not relative.startswith("inputs/"):
                raise WorkflowError("input repair original source is outside inputs")
            original = contained(self.root, relative)
            allowed = (self.root / "inputs" / resource_id) if own_resource else (self.root / "inputs")
            try:
                original.relative_to(allowed.resolve())
            except ValueError as exc:
                raise WorkflowError("diagnostic image must belong to this resource inputs") from exc
            if original.relative_to(self.root).as_posix() != relative:
                raise WorkflowError("input repair paths must be canonical state-relative inputs")
            return original

        for source in proof["sources"]:
            if not isinstance(source, dict) or digest(original_input(source.get("path"))) != source.get("sha256"):
                raise WorkflowError("input repair original source changed or is outside inputs")
        seen = set()
        for image in proof["diagnostic_images"]:
            required = {"path", "sha256", "diagnostic_evidence", "diagnostic_sha256", "original_job_id",
                        "source_evidence", "source_sha256"}
            if not isinstance(image, dict) or not required.issubset(image):
                raise WorkflowError("diagnostic image needs exact original image, job and source bindings")
            environment_only = image.get("evidence_role") == "current_console_environment"
            image_path = original_input(image["path"], own_resource=not environment_only)
            if image["path"] in seen:
                raise WorkflowError("diagnostic image must uniquely belong to this resource inputs")
            seen.add(image["path"])
            if (
                image_path.suffix.lower() != ".png" or not image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                or digest(image_path) != image.get("sha256")
            ):
                raise WorkflowError("diagnostic image changed or is not a real PNG")
            diagnostic_path = original_input(image["diagnostic_evidence"], own_resource=True)
            if digest(diagnostic_path) != image.get("diagnostic_sha256"):
                raise WorkflowError("diagnostic source binding changed")
            diagnostic = read_json(diagnostic_path)
            if environment_only:
                from .environment_diagnostic import validate as validate_environment_diagnostic

                validate_environment_diagnostic(self.root, task_id, image, diagnostic)
            if (
                diagnostic.get("formal_test") is not False or diagnostic.get("pass_added") is not False
                or diagnostic.get("cloud_writes") != 0
                or diagnostic.get("original_job", diagnostic.get("original_job_id")) != image["original_job_id"]
                or diagnostic.get("diagnostic_source") != image["source_evidence"]
                or diagnostic.get("diagnostic_source_sha256") != image["source_sha256"]
                or digest(original_input(image["source_evidence"])) != image["source_sha256"]
                or not any(
                    diagnostic.get(field) == image["path"] and diagnostic.get(field + "_sha256") == image["sha256"]
                    for field in ("original_screenshot", "supplemental_screenshot", "screenshot")
                )
            ):
                raise WorkflowError("diagnostic image must bind unchanged original job, source and pixels")
        return proof

    def queue_input_repair(self, task_id: str, evidence: str, reason: str) -> None:
        """OPS registers same-resource diagnostic inputs for the existing sole Sol repair queue."""
        proof = self.input_repair_proof(task_id, evidence)
        path = contained(self.root / "inputs", evidence)
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            accepted_repair = task and task["state"] == "repair" and any(
                value.get("task_id") == task_id and value.get("retry") is True
                for row in connection.execute("SELECT data FROM events WHERE kind='acceptance_committed'")
                if (value := json.loads(row[0]))
            )
            if (
                not task or (task["state"] != "blocked" and not accepted_repair)
                or task["slot"] is not None or not reason.strip()
                or connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or connection.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
                or connection.execute("SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (task_id,)).fetchone()
            ):
                raise WorkflowError(
                    "input repair requires paused, drained, slot-free blocked or accepted repair task "
                    "with verified cleanup"
                )
            for image in proof["diagnostic_images"]:
                origin = connection.execute(
                    "SELECT task_id,role,state,turn_id FROM jobs WHERE id=?", (image["original_job_id"],),
                ).fetchone()
                if not origin or tuple(origin[:3]) != (task_id, "glm", "complete") or not origin[3]:
                    raise WorkflowError("diagnostic belongs to a different or nonterminal original GLM")
            gate = self.root / "execution-gate.sqlite3"
            if gate.exists():
                from scripts.web.e2e.resource_link_workflow.execution_gate import creations

                workers = [row[0] for row in connection.execute(
                    "SELECT id FROM jobs WHERE task_id=? AND role='glm'", (task_id,),
                )]
                reconciled = self.reconciled_creations(connection, task_id)
                for creation in creations(gate, workers):
                    if creation["attempt_id"] in reconciled:
                        continue
                    if creation["state"] != "succeeded" or not creation["resource"]["id"]:
                        raise WorkflowError("input repair cannot bypass unknown creation")
                    owner = owner_key(connection, self.root, task_id, creation["dimension"],
                                      creation["resource"], creation["job_id"])
                    if not connection.execute(
                        "SELECT 1 FROM owned WHERE id=? AND state='deleted'", (owner,),
                    ).fetchone():
                        raise WorkflowError("input repair needs this original generation's verified cleanup")
            if any(json.loads(row[0]).get("sha256") == digest(path) for row in connection.execute(
                "SELECT data FROM events WHERE kind='ops_input_repair'",
            )):
                raise WorkflowError("same input repair bundle already consumed; independently review new evidence")
            connection.execute("UPDATE tasks SET state='repair',reason=? WHERE id=?", (reason, task_id))
            self.set_repair_scope(connection, task_id, "input")
            self.event(connection, "ops_input_repair", {
                "task_id": task_id, "reason": reason, "evidence": path.relative_to(self.root).as_posix(),
                "input_path": path.relative_to(self.root / "inputs").as_posix(), "sha256": digest(path),
                "diagnostic_only": True, "formal_observations_unchanged": True,
                "diagnostic_image_paths": [item["path"] for item in proof["diagnostic_images"]],
            })

    def queue_preserved_repair(self, task_id: str, evidence: str, reason: str) -> None:
        """Queue product diagnosis without requiring deletion of the objects it must recover.

        This is not execution recovery: original slot, packet, ownership, calls,
        unknowns and fees stay held. The sole Sol may repair code, then block for
        independently reviewed OPS continuation; it cannot approve a new packet.
        """
        path = contained(self.root / "inputs", evidence)
        proof = read_json(path)
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if (
                not task or task["state"] != "blocked" or task["slot"] is None or not task["packet"]
                or task["attempt"] < 1 or not reason.strip()
                or connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true"
                or connection.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
                or connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
            ):
                raise WorkflowError(
                    "preserved repair requires paused, drained, executed blocked task retaining its slot"
                )
            slot = connection.execute("SELECT task_id FROM slots WHERE id=?", (task["slot"],)).fetchone()
            owners = sorted(row[0] for row in connection.execute(
                "SELECT id FROM owned WHERE task_id=? AND state!='deleted'", (task_id,),
            ))
            if not slot or slot[0] != task_id or not owners or proof.get("ownership_ids") != owners:
                raise WorkflowError("preserved repair must retain exact occupied slot and outstanding ownership")
            if proof.get("packet_sha256") != digest(contained(self.root, task["packet"])):
                raise WorkflowError("preserved repair original packet changed")
            for role, key in (("glm", "original_job"), ("sol", "review_job")):
                binding = proof.get(key, {})
                latest = connection.execute(
                    "SELECT * FROM jobs WHERE task_id=? AND role=? ORDER BY created DESC,rowid DESC LIMIT 1",
                    (task_id, role),
                ).fetchone()
                if (not latest or latest["state"] != "complete" or not latest["turn_id"]
                        or {k: latest[k] for k in ("id", "thread_id", "turn_id")} != binding
                        or (role == "sol" and latest["purpose"] not in {"review", "repair"})):
                    raise WorkflowError("preserved repair must bind latest terminal original GLM and Sol review/repair")
            sha = digest(path)
            if any(json.loads(row[0]).get("sha256") == sha for row in connection.execute(
                "SELECT data FROM events WHERE kind='ops_preserved_repair'",
            )):
                raise WorkflowError("preserved repair evidence already consumed")
            connection.execute("UPDATE tasks SET state='repair',reason=? WHERE id=?", (reason, task_id))
            self.set_repair_scope(connection, task_id, "product")
            self.event(connection, "ops_preserved_repair", {
                "task_id": task_id, "reason": reason, "evidence": path.relative_to(self.root).as_posix(),
                "input_path": path.relative_to(self.root / "inputs").as_posix(), "sha256": sha,
                "ownership_preserved": True, "execution_recovery_authorized": False,
                "cloud_calls_authorized": False, "pass_count_unchanged": True,
            })

    def queue_preparation_repair(self, task_id: str, evidence: str, reason: str) -> None:
        """OPS queues diagnosis before execution; it cannot erase attempts, ownership or PASS."""
        path = contained(self.root / "inputs", evidence)
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            paused = connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true"
            busy = connection.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
            issued = connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
            executed = connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND role='glm'", (task_id,)).fetchone()
            owned = connection.execute("SELECT 1 FROM owned WHERE task_id=?", (task_id,)).fetchone()
            if (
                not paused
                or not task
                or task["state"] != "blocked"
                or task["attempt"] != 0
                or task["slot"] is not None
                or busy
                or issued
                or executed
                or owned
                or not reason.strip()
            ):
                raise WorkflowError("preparation repair requires paused, drained, never-executed blocked task")
            job_ids = {row[0] for row in connection.execute("SELECT id FROM jobs WHERE task_id=?", (task_id,))}
            gate = self.root / "execution-gate.sqlite3"
            if gate.exists():
                with sqlite3.connect(f"{gate.as_uri()}?mode=ro", uri=True) as wire:
                    live = wire.execute(
                        "SELECT 1 FROM active WHERE (phase IS NULL OR phase IN ('execute','cleanup')) "
                        "AND (expires IS NULL OR expires>?)",
                        (time.time(),),
                    ).fetchone()
                    attempts = wire.execute("SELECT scope,state FROM attempts").fetchall()
                if live or any(state == "unknown" or scope.split(":", 1)[0] in job_ids for scope, state in attempts):
                    if live or any(scope.split(":", 1)[0] in job_ids for scope, _ in attempts):
                        raise WorkflowError("preparation repair cannot bypass live or prior task cloud calls")
                    accepted_tasks = {
                        value["task_id"]
                        for row in connection.execute("SELECT data FROM events WHERE kind='acceptance_committed'")
                        if (value := json.loads(row[0])).get("retry") is False
                    }
                    for scope, state in attempts:
                        if state != "unknown":
                            continue
                        origin = connection.execute(
                            "SELECT j.task_id,j.role,j.state,t.state FROM jobs j JOIN tasks t ON t.id=j.task_id "
                            "WHERE j.id=?", (scope.split(":", 1)[0],),
                        ).fetchone()
                        # Accepted terminal history retains its original negative/OPS-reconciled wire
                        # records. It must not block preparation of an unrelated never-executed task.
                        # Orphan, unfinished, unaccepted and outstanding-owned histories still refuse.
                        if (
                            not origin or tuple(origin[1:]) != ("glm", "complete", "complete")
                            or origin[0] not in accepted_tasks
                            or connection.execute(
                                "SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (origin[0],),
                            ).fetchone()
                        ):
                            raise WorkflowError("preparation repair cannot bypass unresolved unknown cloud calls")
            connection.execute("UPDATE tasks SET state='repair',reason=? WHERE id=?", (reason, task_id))
            self.event(
                connection,
                "ops_preparation_repair",
                {
                    "task_id": task_id,
                    "reason": reason,
                    "evidence": path.relative_to(self.root).as_posix(),
                    "input_path": path.relative_to(self.root / "inputs").as_posix(),
                    "sha256": digest(path),
                },
            )
            self.set_repair_scope(connection, task_id, "product")

    def accept_pending_delete(self, task_id: str, evidence: str) -> dict[str, Any]:
        """OPS-only explicit human disposition. No verdict, cloud call, slot release or quota mutation."""
        from .pending_delete import validate

        path = contained(self.root / "inputs", evidence.removeprefix("inputs/"))
        proof = read_json(path)
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            busy = connection.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
            issued = connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone()
            paused = json.loads(connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0])
            if not paused or not task or task["state"] != "blocked" or busy or issued:
                raise WorkflowError("PendingDelete disposition requires paused, drained blocked task")
            owner = connection.execute(
                "SELECT * FROM owned WHERE id=? AND task_id=?", (proof.get("ownership_id"), task_id),
            ).fetchone()
            if not owner:
                raise WorkflowError("PendingDelete cannot invent ownership")
            validate(self.root, task_id, owner["id"], json.loads(owner["resource"]), proof)
            binding = {"path": path.relative_to(self.root).as_posix(), "sha256": digest(path)}
            if owner["state"] == "deleted":
                if json.loads(owner["evidence"]) != binding:
                    raise WorkflowError("PendingDelete cannot overwrite an existing cleanup disposition")
                return {"already_recorded": True, "actual_absence_verified": False}
            original = json.loads(owner["evidence"])
            if proof.get("original_creation") != original:
                raise WorkflowError("PendingDelete must preserve original creation evidence")
            original_path = contained(self.root, original["path"])
            if digest(original_path) != original["sha256"]:
                raise WorkflowError("PendingDelete original creation source changed")
            connection.execute("UPDATE owned SET state='deleted',evidence=? WHERE id=?", (dumps(binding), owner["id"]))
            self.event(connection, "ops_user_accepted_pending_delete", {
                "task_id": task_id, "ownership_id": owner["id"], "evidence": binding,
                "original_creation": original, "actual_absence_verified": False, "pass_added": False,
            })
            return {"cleanup_disposition": "user_accepted_pending_delete", "actual_absence_verified": False,
                    "pass_added": False, "slot_released": False}

    def reopen_review(self, task_id: str, reason: str, cleanup_proof: dict[str, Any] | None = None) -> None:
        """Re-review retained evidence after fixing access, never replay or discard ownership."""
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            busy = connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND state!='complete'", (task_id,)).fetchone()
            worker = connection.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND role='glm' AND state='complete'", (task_id,)
            ).fetchone()
            generation_review = False
            if task and task["state"] == "pending" and not busy and worker and cleanup_proof is None:
                from scripts.web.e2e.resource_link_workflow.execution_gate import creations

                workers = [row[0] for row in connection.execute(
                    "SELECT id FROM jobs WHERE task_id=? AND role='glm'", (task_id,),
                )]
                for record in creations(self.root / "execution-gate.sqlite3", workers):
                    if record["state"] != "succeeded" or not record["resource"]["id"]:
                        continue
                    identity = owner_key(
                        connection, self.root, task_id, record["dimension"], record["resource"], record["job_id"],
                    )
                    if not connection.execute("SELECT 1 FROM owned WHERE id=?", (identity,)).fetchone():
                        generation_review = True
                        break
            issued = connection.execute(
                "SELECT 1 FROM actions WHERE task_id=? AND state='issued'", (task_id,),
            ).fetchone()
            if (not task or (task["state"] != "blocked" and not generation_review)
                    or busy or issued or not worker or not reason):
                raise WorkflowError("re-review requires exact blocked task with terminal worker and no active job")
            if cleanup_proof is not None:
                if cleanup_proof.get("task_id") != task_id:
                    raise WorkflowError("retained cleanup task mismatch")
                entries = cleanup_proof.get("cleanup", [])
                if len(entries) != len({entry.get("ownership_id") for entry in entries}):
                    raise WorkflowError("duplicate retained cleanup owner")
                for entry in entries:
                    owner = connection.execute(
                        "SELECT * FROM owned WHERE id=? AND task_id=? AND state='deleted'",
                        (entry.get("ownership_id"), task_id),
                    ).fetchone()
                    proof_path = contained(self.root, entry["evidence"])
                    value = read_json(proof_path)
                    if (
                        not owner or value.get("ownership_id") != owner["id"]
                        or value.get("resource") != json.loads(owner["resource"])
                        or value.get("absent") is not True or value.get("failed") is not False
                        or digest(contained(self.root, value["source_evidence"])) != value.get("source_sha256")
                    ):
                        raise WorkflowError("retained cleanup must bind already deleted owner and raw absence")
                    binding = {"path": entry["evidence"], "sha256": digest(proof_path)}
                    self.event(connection, "ops_retained_cleanup_bound", {
                        "task_id": task_id, "ownership_id": owner["id"],
                        "original_evidence": json.loads(owner["evidence"]), "cleanup_evidence": binding,
                    })
                    connection.execute("UPDATE owned SET evidence=? WHERE id=?", (dumps(binding), owner["id"]))
            connection.execute("UPDATE tasks SET state='reported',reason=? WHERE id=?", (reason, task_id))
            self.event(connection, "ops_reopen_review", {"task_id": task_id, "reason": reason})

    def recover_blocked(self, task_id: str, proof: dict[str, Any], reason: str) -> None:
        """OPS recovery, not acceptance: no PASS manufactured and no active turn cancelled implicitly."""
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            busy = connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND state!='complete'", (task_id,)).fetchone()
            if not task or task["state"] != "blocked" or busy or not reason or proof.get("task_id") != task_id:
                raise WorkflowError("OPS recovery needs exact blocked task and all model turns terminal")
            owners = list(connection.execute("SELECT * FROM owned WHERE task_id=? AND state!='deleted'", (task_id,)))
            cleanup = proof.get("cleanup", [])
            if {item.get("ownership_id") for item in cleanup} != {row["id"] for row in owners}:
                raise WorkflowError("OPS recovery cannot abandon any created resource")
            for item in cleanup:
                value = read_json(contained(self.root, item["evidence"]))
                owner = next(row for row in owners if row["id"] == item["ownership_id"])
                if (
                    value.get("ownership_id") != owner["id"]
                    or value.get("resource") != json.loads(owner["resource"])
                    or value.get("absent") is not True
                    or value.get("failed") is not False
                    or digest(contained(self.root, value["source_evidence"])) != value.get("source_sha256")
                ):
                    raise WorkflowError("OPS cleanup proof is not exact verified absence")
            spec = json.loads(task["spec"])
            creation = [
                dim
                for dim, source in spec["dimensions"].items()
                if dim != "QUERY" and source["support"] != "not_applicable"
            ]
            if creation and task["attempt"]:
                uncertainty = read_json(contained(self.root, proof["creation_uncertainty_evidence"]))
                if (
                    set(uncertainty.get("covered_dimensions", [])) != set(creation)
                    or uncertainty.get("confirmed_no_untracked") is not True
                    or digest(contained(self.root, uncertainty["source_evidence"])) != uncertainty.get("source_sha256")
                ):
                    raise WorkflowError("unknown creation outcome must be resolved by original bounded cloud evidence")
            for item in cleanup:
                proof_path = contained(self.root, item["evidence"])
                connection.execute("UPDATE owned SET state='deleted',evidence=? WHERE id=?", (
                    dumps({"path": item["evidence"], "sha256": digest(proof_path)}), item["ownership_id"],
                ))
            self.revoke_task_capabilities(connection, task_id)
            from .hourly_budget import release_verified

            release_verified(self, connection, task_id, {"kind": "ops_verified_cleanup", "evidence": proof})
            connection.execute("UPDATE slots SET task_id=NULL WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM locks WHERE task_id=?", (task_id,))
            connection.execute("UPDATE tasks SET state='repair',slot=NULL,reason=? WHERE id=?", (reason, task_id))
            self.set_repair_scope(connection, task_id, "product")
            self.event(connection, "ops_blocked_recovery", {"task_id": task_id, "reason": reason})

    def reconcile_rejected_creation(self, task_id: str, evidence: str) -> dict[str, Any]:
        """Bind independent OPS absence proof; never rewrite wire outcomes or replenish calls."""
        from .mse_correction import RECONCILIATION_KIND

        audit = read_json(contained(self.root, evidence))
        mse_member = audit.get("kind") == RECONCILIATION_KIND
        with self.transaction() as connection:
            task = connection.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task[0] not in {"blocked", "repair"} or connection.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND state!='complete'", (task_id,)
            ).fetchone() or (not mse_member and connection.execute(
                "SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (task_id,)
            ).fetchone()):
                raise WorkflowError("creation reconciliation requires terminal task turns and no outstanding owners")
            attempts = self._verified_rejected_creation(connection, task_id, evidence)
            sha = digest(contained(self.root, evidence))
            for row in connection.execute("SELECT data FROM events WHERE kind='ops_creation_reconciled'"):
                prior = json.loads(row[0])
                if prior["task_id"] == task_id and set(prior["attempt_ids"]) & attempts:
                    if prior["evidence"] != evidence or prior["sha256"] != sha:
                        raise WorkflowError("original rejection already bound to different evidence")
                    return {"reconciled_attempts": sorted(attempts), "already_recorded": True, "new_pass": 0}
            self.event(connection, "ops_creation_reconciled", {
                "task_id": task_id, "attempt_ids": sorted(attempts), "evidence": evidence, "sha256": sha,
            })
            return {"reconciled_attempts": sorted(attempts), "wire_unchanged": True, "new_pass": 0}

    def reconciled_creations(self, connection: sqlite3.Connection, task_id: str) -> set[str]:
        result: set[str] = set()
        for row in connection.execute("SELECT data FROM events WHERE kind='ops_creation_reconciled'"):
            event = json.loads(row[0])
            if event["task_id"] != task_id:
                continue
            if digest(contained(self.root, event["evidence"])) != event["sha256"]:
                raise WorkflowError("original OPS rejection evidence changed")
            attempts = self._verified_rejected_creation(connection, task_id, event["evidence"])
            if attempts != set(event["attempt_ids"]):
                raise WorkflowError("original reconciled wire attempt changed")
            result.update(attempts)
        return result

    def _verified_rejected_creation(
        self, connection: sqlite3.Connection, task_id: str, evidence: str,
    ) -> set[str]:
        """Explicit ROS schema rejections plus independent absence; preserve unknown wire outcomes."""
        if not evidence.startswith("inputs/"):
            raise WorkflowError("save independent OPS rejection evidence under inputs")
        audit = read_json(contained(self.root, evidence))
        from .nas_rejection import KIND as NAS_REJECTION_KIND
        from .nas_rejection import validate as validate_nas_rejection

        if audit.get("kind") == NAS_REJECTION_KIND:
            return validate_nas_rejection(self.root, connection, task_id, audit)
        from .mse_correction import RECONCILIATION_KIND, reconciled_failed_member

        if audit.get("kind") == RECONCILIATION_KIND:
            return reconciled_failed_member(self.root, connection, task_id, audit)
        job_id = audit.get("original_job", "")
        job = connection.execute("SELECT task_id,role,state,turn_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (not job or tuple(job[:3]) != (task_id, "glm", "complete") or not job[3]
                or audit.get("kind") != "ops_independent_rejected_create_absence"
                or audit.get("task_id") != task_id or audit.get("confirmed_no_residual") is not True
                or audit.get("no_journal_mutation") is not True):
            raise WorkflowError("exact terminal original execution and independent absence evidence required")
        with closing(sqlite3.connect(
            f"file:{(self.root / 'execution-gate.sqlite3').as_posix()}?mode=ro", uri=True,
        )) as gate:
            gate.row_factory = sqlite3.Row
            rows = gate.execute(
                "SELECT a.*,g.kind,g.call,g.authority FROM attempts a LEFT JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope LIKE ? ORDER BY a.created",
                (job_id + ":%",),
            ).fetchall()
        originals = audit.get("original_wire", [])
        if len(rows) not in {3, 4} or len(originals) != len(rows) or any(
            not {"id", "scope", "grant_id", "kind", "call", "state", "response"}.issubset(saved)
            or any(key not in row.keys() or row[key] != value for key, value in saved.items())
            for row, saved in zip(rows, originals)
        ):
            raise WorkflowError("original wire scope or raw evidence differs")
        if len(rows) == 4:
            return self._verified_rejected_role(job_id, rows, audit)
        if [row["kind"] for row in rows] != ["identity", "create", "read"]:
            raise WorkflowError("original wire scope or raw evidence differs")
        identity, create, absence = rows
        call, response = json.loads(create["call"]), json.loads(create["response"])
        params = call.get("params", {})
        region, account = audit.get("region"), audit.get("actual_account")
        template = json.loads(params.get("TemplateBody", "{}"))
        resources = list(template.get("Resources", {}).values())
        message = response.get("Message", "")
        if (create["state"] != "unknown" or call.get("product", "").lower() != "ros"
                or call.get("action") != "CreateStack" or response.get("Code") != "StackValidationFailed"
                or response.get("statusCode") != 400 or not response.get("RequestId") or response.get("StackId")
                or "AddressFamily" not in message or '"IPv4" is not an allowed value [Ipv4, Ipv6]' not in message
                or call.get("region_id") != region or params.get("RegionId") != region
                or len(resources) != 1 or resources[0].get("Type") != "ALIYUN::ECS::PrefixList"
                or resources[0].get("Properties", {}).get("AddressFamily") != "IPv4"
                or identity["state"] != "succeeded" or json.loads(identity["response"]).get("AccountId") != account
                or len({(row["scope"], row["session"], row["credential"]) for row in rows}) != 1
                or create["scope"] != job_id + ":ROS"):
            raise WorkflowError("not the reviewed deterministic ROS PrefixList schema rejection")
        name = resources[0]["Properties"]["PrefixListName"]
        read_call, read_response = json.loads(absence["call"]), json.loads(absence["response"])
        if (absence["state"] != "succeeded" or read_call.get("product", "").lower() != "ecs"
                or read_call.get("action") != "DescribePrefixLists"
                or read_call.get("params") != {"RegionId": region, "PrefixListName": name, "MaxResults": 10}
                or read_response.get("PrefixLists", {}).get("PrefixList") != []
                or read_response.get("NextToken") or not read_response.get("RequestId")):
            raise WorkflowError("original signed complete exact-name child absence required")
        calls = audit.get("calls", [])
        for item in calls:
            path = contained(self.root, item["path"])
            if digest(path) != item.get("sha256") or read_json(path) != item.get("value"):
                raise WorkflowError("independent raw cloud evidence changed")
            value = item["value"]
            if (value.get("exit_code") != 0 or value.get("cloud_write_performed") is not False
                    or value.get("profile") != "test-guima" or value.get("region") != region
                    or not value.get("response", {}).get("RequestId")):
                raise WorkflowError("independent absence read failed or account/region source missing")
        fresh_identity = [item["value"] for item in calls if item["value"].get("product") == "sts"
                          and item["value"].get("action") == "GetCallerIdentity"]
        stacks = [item["value"] for item in calls if item["value"].get("product") == "ros"
                  and item["value"].get("action") == "ListStacks"]
        if (len(fresh_identity) != 1 or fresh_identity[0]["response"].get("AccountId") != account
                or fresh_identity[0].get("parameters") != {} or len(stacks) != 1
                or stacks[0]["parameters"].get("RegionId") != region
                or params.get("StackName") not in [value for key, value in stacks[0]["parameters"].items()
                                                  if key.startswith("StackName.")]
                or stacks[0]["response"].get("TotalCount") != 0 or stacks[0]["response"].get("Stacks") != []):
            raise WorkflowError("fresh signed account and original exact-name stack absence required")
        return {create["id"]}

    def _verified_rejected_role(self, job_id: str, rows: list[sqlite3.Row], audit: dict[str, Any]) -> set[str]:
        """Only the native Role Action-array rejection, with original and independent name absence."""
        try:
            identity, role, stacks, create = rows
            call = json.loads(create["call"])
            response = json.loads(create["response"])
            params = call["params"]
            template = json.loads(params["TemplateBody"])
            resource = template["Resources"]["Role"]
            properties = resource["Properties"]
            name, stack_name = properties["RoleName"], params["StackName"]
            region, account = audit["region"], audit["actual_account"]
            statement = properties["AssumeRolePolicyDocument"]["Statement"]
            if (not isinstance(name, str) or not name or not isinstance(stack_name, str) or not stack_name
                    or not region or not account or list(template["Resources"]) != ["Role"]
                    or resource["Type"] != "ALIYUN::RAM::Role" or len(statement) != 1
                    or statement[0]["Action"] != ["sts:AssumeRole"]
                    or [row["kind"] for row in rows] != ["identity", "read", "read", "create"]
                    or [row["state"] for row in rows] != ["succeeded", "unknown", "succeeded", "unknown"]
                    or any(not {"session", "credential", "authority"}.issubset(saved)
                           for saved in audit["original_wire"])
                    or len({(row["scope"], row["session"], row["credential"], row["authority"])
                           for row in rows}) != 1
                    or not identity["session"] or not identity["credential"] or not identity["authority"]
                    or create["scope"] != job_id + ":ROS"
                    or call["product"].lower() != "ros" or call["action"] != "CreateStack"
                    or call["version"] != "2019-09-10" or call["region_id"] != region
                    or params["RegionId"] != region or response.get("StackId")
                    or response.get("Code") != "StackValidationFailed" or response.get("statusCode") != 400
                    or response.get("HostId") != "ros.aliyuncs.com" or not response.get("RequestId")
                    or response.get("Message") != (
                        "Property error: : Resources.Role.Properties.AssumeRolePolicyDocument.Statement[1].Action: "
                        ": Resource [Role]: Invalid Property Action, Value \"['sts:AssumeRole']\" is not a string"
                    )):
                raise WorkflowError("not the reviewed deterministic native ROS Role Action-array rejection")

            def role_absent(body: dict[str, Any]) -> bool:
                return (body.get("statusCode") == 404 and body.get("Code") == "EntityNotExist.Role"
                        and body.get("HostId") == "ram.aliyuncs.com" and bool(body.get("RequestId"))
                        and body.get("Message") == "The role not exists:" + name)

            def stack_absent(body: dict[str, Any]) -> bool:
                return (body.get("TotalCount") == 0 and body.get("Stacks") == [] and bool(body.get("RequestId"))
                        and body.get("PageNumber") == 1 and body.get("PageSize") == 10
                        and not body.get("NextToken"))

            identity_call, identity_body = json.loads(identity["call"]), json.loads(identity["response"])
            role_call, role_response = json.loads(role["call"]), json.loads(role["response"])
            stack_call, stack_body = json.loads(stacks["call"]), json.loads(stacks["response"])
            if (identity_call != {"product": "sts", "action": "GetCallerIdentity", "version": "2015-04-01",
                                  "region_id": region, "params": {}}
                    or identity_body.get("AccountId") != account or not identity_body.get("RequestId")
                    or role_call != {"product": "ram", "action": "GetRole", "version": "2015-05-01",
                                     "region_id": region, "params": {"RoleName": name}}
                    or role_response.get("status") != 404 or not role_absent(role_response["body"])
                    or role_response.get("headers", {}).get("x-acs-request-id") != role_response["body"]["RequestId"]
                    or stack_call != {"product": "ros", "action": "ListStacks", "version": "2019-09-10",
                                      "region_id": region, "params": {"RegionId": region, "StackName": [stack_name],
                                                                     "PageNumber": 1, "PageSize": 10}}
                    or not stack_absent(stack_body)):
                raise WorkflowError("original signed identity and exact Role/stack negative prechecks required")

            calls = audit["calls"]
            if len(calls) != 3:
                raise WorkflowError("exactly three independent signed Role absence reads required")
            values = []
            for item in calls:
                if not item["path"].startswith("inputs/"):
                    raise WorkflowError("independent raw cloud evidence must remain under inputs")
                path = contained(self.root, item["path"])
                value = item["value"]
                if (digest(path) != item["sha256"] or read_json(path) != value
                        or value.get("cloud_write_performed") is not False
                        or value.get("profile") != "test-guima" or value.get("region") != region):
                    raise WorkflowError("independent raw cloud evidence changed or source differs")
                values.append(value)
            fresh_identity, fresh_stack, fresh_role = values
            if (fresh_identity.get("product") != "sts" or fresh_identity.get("action") != "GetCallerIdentity"
                    or fresh_identity.get("parameters") != {} or fresh_identity.get("exit_code") != 0
                    or fresh_identity["response"].get("AccountId") != account
                    or not fresh_identity["response"].get("RequestId")
                    or fresh_stack.get("product") != "ros" or fresh_stack.get("action") != "ListStacks"
                    or fresh_stack.get("exit_code") != 0
                    or fresh_stack.get("parameters") != {"RegionId": region, "StackName.1": stack_name,
                                                         "PageNumber": 1, "PageSize": 10}
                    or not stack_absent(fresh_stack["response"])
                    or fresh_role.get("product") != "ram" or fresh_role.get("action") != "GetRole"
                    or fresh_role.get("parameters") != {"RoleName": name}
                    or fresh_role.get("success_response") is not None
                    or not role_absent(fresh_role["error_data"])):
                raise WorkflowError("fresh signed account and complete exact-name Role/stack absence required")
            return {create["id"]}
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise WorkflowError("incomplete original Role rejection or independent absence evidence") from error

    def revoke_task_capabilities(self, connection: sqlite3.Connection, task_id: str) -> None:
        """Safe slot release closes permissions without deleting wire/cumulative history."""
        path = self.root / "execution-gate.sqlite3"
        if not path.exists():
            return
        from scripts.web.e2e.resource_link_workflow.execution_gate import database

        with database(path) as wire:
            for job in connection.execute("SELECT id FROM jobs WHERE task_id=? AND role='glm'", (task_id,)):
                wire.execute("UPDATE active SET phase='closed' WHERE scope LIKE ?", (job[0] + ":%",))

    def backup(self, destination: Path) -> None:
        if destination.exists():
            raise WorkflowError("backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.chmod(destination, 0o600)
