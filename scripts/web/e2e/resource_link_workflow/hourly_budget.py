"""OPS hourly allowance, exact cost review and transactional concurrent holds.

Registration never authorizes an API or converts hourly cents into total cents.
An hourly hold never expires with model time. Only independently verified cleanup
or a proved no-execution path releases it. Legacy cumulative budgets stay intact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .files import WorkflowError, contained, digest, read_json
from .plans import expected_scenes

if TYPE_CHECKING:
    from .store import Store

LOADED_SHA = digest(Path(__file__))


def _input(root: Path, reference: Any) -> tuple[Path, dict[str, Any]]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise WorkflowError("hourly allowance needs an exact input path/SHA binding")
    name = reference["path"]
    if not isinstance(name, str) or not name.startswith("inputs/"):
        raise WorkflowError("hourly allowance evidence must be inside inputs")
    path = contained(root / "inputs", name.removeprefix("inputs/"))
    if not path.is_file() or digest(path) != reference["sha256"]:
        raise WorkflowError("hourly allowance original evidence SHA changed")
    value = read_json(path)
    if not isinstance(value, dict):
        raise WorkflowError("hourly allowance evidence must be an object")
    return path, value


def review(root: Path, run: Any, reference: dict[str, str]) -> dict[str, Any]:
    """Recheck both original records and the immutable frozen scope on every use."""
    _, proof = _input(root, reference)
    plan_id = run["id"]
    frozen = json.loads(run["plan"])
    frozen_sha = hashlib.sha256(run["plan"].encode("utf-8")).hexdigest()
    resources = sorted(item["id"] for item in frozen["resources"])
    scenes = sorted(scene["id"] for item in frozen["resources"] for scene in expected_scenes(item))
    required = {
        "schema_version": 1, "kind": "ops_run_hourly_fee_review", "approved": True,
        "plan_id": plan_id, "frozen_plan_sha256": frozen_sha, "currency": "CNY",
        "resource_ids": resources, "scene_ids": scenes,
    }
    if any(proof.get(key) != value for key, value in required.items()):
        raise WorkflowError("hourly allowance needs an approved review of the exact frozen scope")
    limit = proof.get("max_concurrent_hourly_cents")
    if type(limit) is not int or limit <= 0:
        raise WorkflowError("hourly allowance must be positive integer cents per hour")
    _, human = _input(root, proof.get("human_authorization"))
    if (
        human.get("schema_version") != 1 or human.get("kind") != "human_hourly_fee_authorization"
        or human.get("approved") is not True or human.get("plan_id") != plan_id
        or human.get("currency") != "CNY" or not human.get("verbatim_user_message")
        or type(human.get("max_concurrent_hourly_cents")) is not int
        or human["max_concurrent_hourly_cents"] < limit
        or "total_budget_cents" not in human or human["total_budget_cents"] is not None
        or sorted(human.get("resource_ids", [])) != resources
        or sorted(human.get("scene_ids", [])) != scenes
    ):
        raise WorkflowError("original human hourly authorization must cover the exact scope and limit")
    return {
        **required, "max_concurrent_hourly_cents": limit, "total_budget_cents": None,
        "evidence": reference, "human_authorization": proof["human_authorization"],
        "operation_review_required": True, "not_cloud_enforced_cap": True,
        "legacy_budget_unchanged": True,
    }


def policy(root: Path, connection: sqlite3.Connection, plan_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT value FROM meta WHERE key=?", ("hourly_fee:" + plan_id,)).fetchone()
    if not row:
        return None
    recorded = json.loads(row[0])
    run = connection.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
    if not run or review(root, run, recorded["evidence"]) != recorded:
        raise WorkflowError("recorded hourly allowance no longer matches its original evidence")
    return recorded


def operation_review(
    root: Path, connection: sqlite3.Connection, task: Any, packet: dict[str, Any], proof: dict[str, Any],
) -> dict[str, Any] | None:
    """Check OPS's full-packet approval and SHA-bound official cost inventory.

    This checks provenance and arithmetic, not whether an official price has
    been interpreted correctly: that remains an independent OPS responsibility.
    budget_cents continues to mean cumulative/non-hourly expenditure; a zero
    there does not claim a positive hourly_cost is free.
    """
    if "hourly_cost" not in packet:
        return None
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

    from .creation import execution_digest

    if task is None or packet.get("task_id") != task["id"]:
        raise WorkflowError("hourly operation must bind an existing exact task")
    allowance = policy(root, connection, task["run_id"])
    if allowance is None:
        raise WorkflowError("hourly operation needs registered human hourly permission")
    cost = packet["hourly_cost"]
    if not isinstance(cost, dict) or set(cost) != {"max_hourly_cents", "cost_evidence"}:
        raise WorkflowError("hourly operation needs an exact cost amount and original evidence")
    amount = cost["max_hourly_cents"]
    if type(amount) is not int or not 0 < amount <= allowance["max_concurrent_hourly_cents"]:
        raise WorkflowError("hourly operation cost is outside the human hourly limit")
    accounts = sorted({item["account"] for item in packet["scenes"].values()})
    if len(accounts) != 1:
        raise WorkflowError("hourly operation must have one exact signed account")
    _, estimate = _input(root, cost["cost_evidence"])
    required = {
        "schema_version": 1, "kind": "ops_hourly_cost_inventory", "approved": True,
        "task_id": task["id"], "currency": "CNY", "account": accounts[0],
        "execution_grants_sha256": hashlib.sha256(canonical(packet["execution_grants"]).encode()).hexdigest(),
        "max_hourly_cents": amount, "includes_all_dependencies": True,
        "includes_request_and_network_costs": True,
    }
    if any(estimate.get(key) != value for key, value in required.items()):
        raise WorkflowError("hourly cost inventory must cover exact calls, account and all dependencies")
    components = estimate.get("components")
    if not isinstance(components, list) or not components:
        raise WorkflowError("hourly cost inventory needs itemized official cost components")
    identifiers = set()
    total = 0
    for item in components:
        if not isinstance(item, dict):
            raise WorkflowError("hourly cost component must be an object")
        identifier = item.get("id")
        price = item.get("max_hourly_cents")
        minimum = item.get("minimum_billing_seconds")
        if (not isinstance(identifier, str) or not identifier or identifier in identifiers
                or type(price) is not int or price < 0 or type(minimum) is not int or minimum < 0
                or item.get("billing_mode") not in {"pay_as_you_go", "free"}
                or (item["billing_mode"] == "free" and price != 0)
                or not isinstance(item.get("estimate_basis"), str) or not item["estimate_basis"].strip()
                or not isinstance(item.get("limitations"), str) or not item["limitations"].strip()
                or item.get("bounded_usage") is not True):
            raise WorkflowError("hourly component needs PAYG/free pricing, minimum billing and bounded usage")
        sources = item.get("official_sources")
        if not isinstance(sources, list) or not sources:
            raise WorkflowError("every hourly component needs original official evidence")
        for source in sources:
            _input(root, source)
        identifiers.add(identifier)
        total += price
    if total != amount:
        raise WorkflowError("hourly cost components must sum to the reserved maximum")
    expected = {
        "schema_version": 1, "kind": "ops_hourly_operation_review", "approved": True,
        "plan_id": task["run_id"], "task_id": task["id"],
        "execution_sha256": execution_digest(packet), "hourly_cost": cost,
        "hourly_permission": allowance["evidence"],
    }
    if proof.get("approved") is not True or proof.get("hourly_operation_review") != expected:
        raise WorkflowError("hourly operation needs independent exact OPS cost review")
    return {"task_id": task["id"], "plan_id": task["run_id"], "execution_sha256": execution_digest(packet),
            "max_hourly_cents": amount, "cost_evidence": cost["cost_evidence"],
            "max_concurrent_hourly_cents": allowance["max_concurrent_hourly_cents"]}


def approved_cost(
    root: Path, connection: sqlite3.Connection, task: Any, packet: dict[str, Any],
) -> dict[str, Any] | None:
    """Revalidate immutable full proposal and its OPS cost review at execution."""
    if "hourly_cost" not in packet:
        return None
    from .creation import authorization_path, execution_digest

    approval_file = authorization_path(root, packet)
    if not approval_file.is_file():
        raise WorkflowError("hourly operation is not independently authorized")
    approval = read_json(approval_file)
    if approval.get("authorized") is not True or approval.get("execution_sha256") != execution_digest(packet):
        raise WorkflowError("hourly operation is not independently authorized")
    _, proof = _input(root, {"path": approval.get("authorization_evidence"),
                             "sha256": approval.get("authorization_sha256")})
    _, original = _input(root, {"path": approval.get("proposal_evidence"),
                                "sha256": approval.get("proposal_sha256")})
    if original != packet or proof.get("proposal_sha256") != approval["proposal_sha256"]:
        raise WorkflowError("hourly operation must retain the complete independently reviewed proposal")
    return operation_review(root, connection, task, packet, proof)


def can_reserve(root: Path, connection: sqlite3.Connection, task: Any, packet: dict[str, Any]) -> bool:
    reviewed = approved_cost(root, connection, task, packet)
    if reviewed is None:
        return True
    active = holds(connection)
    own = [item for item in active if item["task_id"] == task["id"]]
    if own:
        return False  # A new job cannot inherit a possibly living old generation.
    allowance = policy(root, connection, task["run_id"])
    limit = min([allowance["max_concurrent_hourly_cents"],
                 *(item["max_concurrent_hourly_cents"] for item in active)])
    return sum(item["max_hourly_cents"] for item in active) + reviewed["max_hourly_cents"] <= limit


def holds(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """All task-created living/unknown-cost reservations, including other runs."""
    return [value for row in connection.execute("SELECT value FROM meta WHERE key LIKE 'hourly_hold:%'")
            if (value := json.loads(row[0]))["state"] == "held"]


def reserve(store: Store, connection: sqlite3.Connection, reviewed: dict[str, Any], job_id: str) -> None:
    """Must run inside the same BEGIN IMMEDIATE transaction as dispatch."""
    allowance = policy(store.root, connection, reviewed["plan_id"])
    if allowance is None:
        raise WorkflowError("hourly reservation permission is missing")
    key = "hourly_hold:" + reviewed["task_id"] + ":" + reviewed["execution_sha256"]
    existing = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    value = {**reviewed, "state": "held", "job_id": job_id}
    if existing and json.loads(existing[0])["state"] == "held":
        if json.loads(existing[0]) != value:
            raise WorkflowError("hourly reservation changed while resources may still be alive")
        return
    active = holds(connection)
    if any(item["task_id"] == reviewed["task_id"] for item in active):
        raise WorkflowError("previous hourly operation must be safely released before another generation")
    used = sum(item["max_hourly_cents"] for item in active)
    limit = min([allowance["max_concurrent_hourly_cents"],
                 *(item["max_concurrent_hourly_cents"] for item in active)])
    if used + reviewed["max_hourly_cents"] > limit:
        raise WorkflowError("concurrent hourly allowance currently reserved; wait for verified cleanup")
    connection.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value)))
    store.event(connection, "hourly_cost_reserved", value)


def release_verified(store: Store, connection: sqlite3.Connection, task_id: str, basis: dict[str, Any]) -> None:
    """Internal only: called after existing cleanup/no-execution hard checks.

    Never expose as a model/OPS unconditional release command. Preserve the
    released record and evidence in the journal; elapsed time is not a basis.
    """
    if basis.get("kind") not in {
        "sol_verified_cleanup", "ops_verified_cleanup", "proved_no_execution",
    } or not basis.get("evidence"):
        raise WorkflowError("hourly release needs the original verified cleanup or no-execution evidence")
    if connection.execute("SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'", (task_id,)).fetchone():
        raise WorkflowError("hourly cost cannot be released while owned resources remain")
    for item in holds(connection):
        if item["task_id"] != task_id:
            continue
        if basis["kind"] == "proved_no_execution" and basis.get("job_id") != item["job_id"]:
            raise WorkflowError("no-execution proof cannot release another job's hourly cost")
        key = "hourly_hold:" + task_id + ":" + item["execution_sha256"]
        value = {**item, "state": "released", "release_basis": basis}
        connection.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(value), key))
        store.event(connection, "hourly_cost_released", value)


def register(store: Store, plan_id: str, evidence: str) -> dict[str, Any]:
    """OPS-only, paused/drained and immutable; no cloud calls or quota resets."""
    path = contained(store.root / "inputs", evidence.removeprefix("inputs/"))
    if not path.is_file():
        raise WorkflowError("hourly allowance review file is missing")
    reference = {"path": str(path.relative_to(store.root)), "sha256": digest(path)}
    with store.transaction() as connection:
        if connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] != "true":
            raise WorkflowError("pause dispatch before hourly allowance registration")
        if connection.execute(
            "SELECT 1 FROM jobs WHERE state IN ('queued','running','unknown','submitted')"
        ).fetchone() or connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone():
            raise WorkflowError("drain model jobs and issued actions before hourly allowance registration")
        run = connection.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
        if not run or not connection.execute(
            "SELECT 1 FROM tasks WHERE run_id=? AND state NOT IN ('complete','superseded')", (plan_id,)
        ).fetchone():
            raise WorkflowError("hourly allowance requires an incomplete existing plan")
        scope = review(store.root, run, reference)
        previous = policy(store.root, connection, plan_id)
        if previous:
            if previous != scope:
                raise WorkflowError("hourly allowance cannot be replaced with another scope or evidence")
            return {"already_recorded": True, "no_cloud_call": True, "hourly_fee_authorization": previous}
        connection.execute("INSERT INTO meta VALUES(?,?)", ("hourly_fee:" + plan_id, json.dumps(scope)))
        store.event(connection, "run_hourly_fee_registered", scope)
        return {"already_recorded": False, "no_cloud_call": True, "hourly_fee_authorization": scope}
