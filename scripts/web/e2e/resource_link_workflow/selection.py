"""OPS-reviewed partial execution, without shrinking a resource's frozen obligations."""

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


def selected_scenes(root: Path, packet: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    all_scenes = expected_scenes(spec)
    selection = packet.get("execution_selection")
    if selection is None:
        if "preparation_selection" in packet or "preparation_selection_continuation" in packet:
            raise WorkflowError("initial preparation selection requires an explicit execution selection")
        return all_scenes
    if not isinstance(selection, dict) or set(selection) != {"dimensions", "deferred_scenes"}:
        raise WorkflowError("execution selection needs exact dimensions and deferred scene evidence")
    dimensions = selection["dimensions"]
    needed = {scene["dimension"] for scene in all_scenes}
    if (
        not isinstance(dimensions, list) or not dimensions
        or any(not isinstance(dimension, str) for dimension in dimensions)
        or len(set(dimensions)) != len(dimensions) or not set(dimensions) < needed
    ):
        raise WorkflowError("execution selection must be a nonempty strict subset of missing dimensions")
    active = [scene for scene in all_scenes if scene["dimension"] in dimensions]
    deferred = selection["deferred_scenes"]
    if not isinstance(deferred, dict) or set(deferred) != {
        scene["id"] for scene in all_scenes if scene["dimension"] not in dimensions
    }:
        raise WorkflowError("selection must retain evidence for every deferred missing scene")
    if "preparation_selection" in packet:
        validate_preparation_selection(root, packet)
        return active
    if "preparation_selection_continuation" in packet:
        raise WorkflowError("preparation continuation must retain its original preparation selection")
    for scene, evidence in deferred.items():
        if (
            not isinstance(evidence, dict) or set(evidence) != {"reason", "path", "sha256"}
            or not isinstance(evidence["reason"], str) or not evidence["reason"].strip()
            or not isinstance(evidence["path"], str)
        ):
            raise WorkflowError("deferred scene requires precise reason and unchanged original receipt")
        path = contained(root, evidence["path"])
        if not path.is_file() or path.parent != (root / "receipts").resolve() or digest(path) != evidence["sha256"]:
            raise WorkflowError("deferred original acceptance receipt changed or is outside receipts")
        receipt = read_json(path)
        if (
            receipt.get("task_id") != packet.get("task_id")
            or not receipt.get("sol_thread") or not receipt.get("sol_turn")
            or not any(item.get("scene") == scene and item.get("status") in {
                "execution_gap", "product_gap", "fail"
            } for item in receipt.get("decisions", []))
        ):
            raise WorkflowError("deferred scene must be this task's independently accepted original gap")
        with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as connection:
            job = connection.execute(
                "SELECT task_id,role,purpose,state,thread_id,turn_id FROM jobs WHERE id=?", (path.stem,),
            ).fetchone()
            verdicts = connection.execute(
                "SELECT receipt FROM verdicts WHERE task_id=? AND scene=? "
                "AND status IN ('execution_gap','product_gap','fail')", (packet["task_id"], scene),
            ).fetchall()
        recorded = any(json.loads(row[0]) in receipt["decisions"] for row in verdicts)
        binding = (packet["task_id"], "sol", "review", "complete", receipt["sol_thread"], receipt["sol_turn"])
        if job != binding or not recorded:
            raise WorkflowError("deferred receipt must bind a terminal independent Sol acceptance in SQLite")
    return active


def validate_preparation_selection(root: Path, packet: dict[str, Any], *, admission: bool = False) -> None:
    """Initial QUERY may precede missing managed TF, never invent a pre-execution verdict.

    Admission proves this task has never executed. Runtime validation keeps the
    immutable original preparation blocker, frozen scope and full OPS approval;
    it must not demand zero history after the legitimately approved GLM starts.
    """
    marker = packet.get("preparation_selection")
    continuation = "preparation_selection_continuation" in packet
    required = {"job_id", "thread_id", "turn_id", "path", "sha256", "frozen_plan_sha256"}
    if (
        not isinstance(marker, dict) or set(marker) != required
        or any(not isinstance(marker[key], str) or not marker[key] for key in required)
        or packet.get("execution_selection", {}).get("dimensions") != ["QUERY"]
        or set(packet.get("requests", {})) != {"QUERY"}
        or set(packet.get("execution_grants", {})) != {"QUERY"}
        or type(packet.get("budget_cents")) is not int or packet["budget_cents"] != 0
        or packet.get("cleanup_requests") not in (None, {})
        or "additional_source_request" in packet or "query_identity_continuation" in packet
        or ("additional_source_requests" in packet and not continuation)
    ):
        raise WorkflowError("initial preparation selection is only an independently reviewed first readonly QUERY")
    entries = packet["execution_grants"]["QUERY"]
    if (
        not isinstance(entries, list) or not entries
        or any(not isinstance(entry, dict) or entry.get("kind") not in {"identity", "read"}
               or type(entry.get("quota")) is not int or entry["quota"] != 1
               or not isinstance(entry.get("read_policy", {}), dict)
               or entry.get("read_policy", {}).get("phase", "execute") != "execute" for entry in entries)
    ):
        raise WorkflowError("initial preparation selection cannot grant writes, cleanup or continued quotas")
    path = contained(root, marker["path"])
    resource_id = packet["task_id"].rsplit(":", 1)[-1]
    if (path.parent != (root / "inputs" / resource_id).resolve()
            or not path.is_file() or digest(path) != marker["sha256"]):
        raise WorkflowError("initial preparation blocker must be this resource's unchanged original input")
    handoff = read_json(path)
    if (
        handoff.get("kind") != "preparation_missing_input_handoff"
        or handoff.get("schema_version") != 1 or handoff.get("task_id") != packet["task_id"]
        or handoff.get("job_id") != marker["job_id"] or handoff.get("purpose") != "prepare"
        or handoff.get("ready_for_approve") is not False
    ):
        raise WorkflowError("initial selection needs the real original Sol preparation input blocker")
    with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as connection:
        job = connection.execute(
            "SELECT task_id,role,purpose,state,thread_id,turn_id FROM jobs WHERE id=?", (marker["job_id"],),
        ).fetchone()
        task = connection.execute("SELECT run_id,attempt,packet FROM tasks WHERE id=?", (packet["task_id"],)).fetchone()
        run = connection.execute("SELECT plan FROM runs WHERE id=?", (task[0],)).fetchone() if task else None
        events = [json.loads(row[0]) for row in connection.execute("SELECT data FROM events WHERE kind='task_blocked'")]
        jobs = connection.execute("SELECT id,role FROM jobs WHERE task_id=?", (packet["task_id"],)).fetchall()
        has_results = any(connection.execute(
            f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (packet["task_id"],),
        ).fetchone() for table in ("owned", "observations", "verdicts")) if admission else False
    binding = (packet["task_id"], "sol", "prepare", "complete", marker["thread_id"], marker["turn_id"])
    artifact = {"path": marker["path"], "sha256": marker["sha256"]}
    if job != binding or not run or hashlib.sha256(run[0].encode()).hexdigest() != marker["frozen_plan_sha256"]:
        raise WorkflowError("initial selection must bind the terminal original preparation and frozen plan")
    if not any(event.get("task_id") == packet["task_id"]
               and isinstance(event.get("handoff"), dict)
               and all(event["handoff"].get(key) == marker[key] for key in ("job_id", "thread_id", "turn_id"))
               and artifact in event["handoff"].get("artifacts", []) for event in events):
        raise WorkflowError("initial blocker was not handed off by the original Sol in SQLite")
    frozen = next((spec for spec in json.loads(run[0])["resources"] if spec["id"] == resource_id), None)
    scenes = expected_scenes(frozen) if frozen else []
    all_ids = {scene["id"] for scene in scenes}
    deferred = packet["execution_selection"]["deferred_scenes"]
    if (
        not frozen or frozen.get("retest_scope", {}).get("kind") != "failed_navigation_managed"
        or {scene["dimension"] for scene in scenes} != {"QUERY", "TF"}
        or set(handoff.get("frozen_scope", {}).get("scene_ids", [])) != all_ids
        or set(deferred) != {scene["id"] for scene in scenes if scene["dimension"] == "TF"}
        or any(not isinstance(value, dict) or set(value) != {"reason", "path", "sha256"}
               or value.get("path") != marker["path"] or value.get("sha256") != marker["sha256"]
               or not isinstance(value.get("reason"), str) or not value["reason"].strip()
               for value in deferred.values())
    ):
        raise WorkflowError("initial selection must preserve every frozen missing TF scene with its real blocker")
    if continuation:
        _validate_preparation_continuation(root, packet)
        return
    if not admission:
        return
    if not task or task[1] != 0 or task[2] is not None or has_results or any(role == "glm" for _, role in jobs):
        raise WorkflowError("initial preparation selection cannot replace an approved or previously executed task")
    gate = root / "execution-gate.sqlite3"
    if gate.exists():
        ids = {job_id for job_id, _ in jobs}
        with closing(sqlite3.connect(f"file:{gate.as_posix()}?mode=ro", uri=True)) as connection:
            for table in ("grants", "attempts", "active"):
                if any((row[0] or "").partition(":")[0] in ids
                       for row in connection.execute(f"SELECT scope FROM {table}")):
                    raise WorkflowError("initial preparation selection cannot reset existing wire capabilities/calls")
    from .creation import execution_digest

    for approval_path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(approval_path)
        if (approval.get("task_id") == packet["task_id"]
                and approval.get("execution_sha256") != execution_digest(packet)):
            raise WorkflowError("initial selection cannot introduce a second operation authority")


def _validate_preparation_continuation(root: Path, packet: dict[str, Any]) -> None:
    """Retain a genuine never-executed TF blocker, not an invented TF verdict.

    One separately reviewed additional QUERY remains subject to the normal
    predecessor wire/terminal/cleanup checks at operation admission. These
    immutable original bindings also survive the new legitimate running turn.
    """
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

    from .creation import execution_authority, validate_authorization

    marker = packet["preparation_selection_continuation"]
    requests = packet.get("additional_source_requests")
    if (not isinstance(marker, dict) or set(marker) != {"initial_proposal", "initial_sha256"}
            or any(not isinstance(value, str) or not value for value in marker.values())
            or not isinstance(requests, list) or len(requests) != 1
            or not isinstance(requests[0], dict) or requests[0].get("dimension") != "QUERY"):
        raise WorkflowError("preparation continuation needs one independently reviewed additional QUERY")
    path = contained(root, marker.get("initial_proposal", "missing"))
    if (path.parent != (root / "inputs" / packet["task_id"].rsplit(":", 1)[-1]).resolve()
            or not path.is_file() or digest(path) != marker.get("initial_sha256")):
        raise WorkflowError("preparation continuation initial proposal changed or belongs to another resource")
    old = read_json(path)
    request = requests[0]
    if (old.get("task_id") != packet["task_id"]
            or any(key in old for key in ("additional_source_request", "additional_source_requests",
                                          "query_identity_continuation", "preparation_selection_continuation"))
            or old.get("preparation_selection") != packet["preparation_selection"]
            or old.get("execution_selection") != packet["execution_selection"]
            or old.get("execution_grants") != packet["execution_grants"]
            or request.get("predecessor_proposal") != marker["initial_proposal"]
            or request.get("predecessor_sha256") != marker["initial_sha256"]):
        raise WorkflowError("preparation continuation must retain original blocker, selection and exact calls")
    validate_authorization(root, packet["task_id"].rsplit(":", 1)[-1], old)
    scope = request.get("prior_scope", "")
    with closing(sqlite3.connect(f"file:{(root / 'state.sqlite3').as_posix()}?mode=ro", uri=True)) as state:
        job = state.execute("SELECT task_id,role,state,turn_id FROM jobs WHERE id=?", (scope.split(":")[0],)).fetchone()
    if (not job or job[:3] != (packet["task_id"], "glm", "complete") or not job[3]
            or scope != scope.split(":")[0] + ":QUERY"):
        raise WorkflowError("preparation continuation needs its terminal original QUERY worker")
    authority = execution_authority(root, old, "QUERY")
    accounts = {value["account"] for value in old["scenes"].values()}
    if accounts != {value["account"] for value in packet["scenes"].values()} or len(accounts) != 1:
        raise WorkflowError("preparation continuation signed account changed")
    with closing(sqlite3.connect(f"file:{(root / 'execution-gate.sqlite3').as_posix()}?mode=ro", uri=True)) as gate:
        if gate.execute("SELECT DISTINCT account FROM identities WHERE scope=?", (scope,)).fetchall() != [
            (next(iter(accounts)),)
        ]:
            raise WorkflowError("preparation continuation needs original signed same-account identity")
        for entry in old["execution_grants"]["QUERY"]:
            binding = gate.execute(
                "SELECT kind,call,authority,quota,used FROM grants WHERE scope=? AND id=?", (scope, entry["id"]),
            ).fetchone()
            attempts = gate.execute(
                "SELECT state FROM attempts WHERE scope=? AND grant_id=?", (scope, entry["id"]),
            ).fetchall()
            limit = gate.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, entry["id"]),
            ).fetchone()
            if (binding != (entry["kind"], canonical(entry["call"]), authority, 1, 1)
                    or attempts != [("succeeded",)] or limit != (1,)):
                raise WorkflowError("preparation continuation must preserve exhausted original signed wire calls")


def validate_review(root: Path, packet: dict[str, Any], review: dict[str, Any]) -> None:
    if "execution_selection" not in packet:
        if ("execution_selection_approved" in review or "preparation_selection" in packet
                or "preparation_selection_continuation" in packet):
            raise WorkflowError("selection review cannot introduce an unproposed execution scope")
        return
    if (
        review.get("approved") is not True
        or review.get("task_id") != packet.get("task_id")
        or review.get("execution_selection_approved") != packet["execution_selection"]
    ):
        raise WorkflowError("partial execution needs independent OPS approval of the exact selection")
    # Independent review is saved by OPS, not inferred from a Sol packet narrative.
    if not review.get("proposal_sha256"):
        raise WorkflowError("selection OPS review must bind the full final proposal SHA")
    if "preparation_selection" in packet:
        continuation = "preparation_selection_continuation" in packet
        if (review.get("kind") != ("ops_additional_sources_review" if continuation else "ops_initial_selection_review")
                or review.get("preparation_selection_approved") != packet["preparation_selection"]
                or review.get("carry_forward_dimensions") != {}
                or (continuation and (
                    review.get("preparation_selection_continuation_approved")
                    != packet["preparation_selection_continuation"]
                    or review.get("additional_sources_approved") != packet["additional_source_requests"]
                ))):
            raise WorkflowError("initial selection needs independent OPS review, not invented carry-forward authority")
        validate_preparation_selection(root, packet)
        return
    extra = packet.get("additional_source_requests", [])
    if "additional_source_request" in packet:
        extra = [packet["additional_source_request"]]
    if not isinstance(extra, list) or any(not isinstance(item, dict) for item in extra):
        raise WorkflowError("selection additional operations require exact reviewed dimensions")
    additional_dimensions = {item.get("dimension") for item in extra}
    carry = review.get("carry_forward_dimensions")
    if not isinstance(carry, dict) or set(carry) != set(packet["execution_grants"]) - additional_dimensions:
        raise WorkflowError("selection must carry every unchanged original dimension authority without new quotas")


def validate_authorization(root: Path, packet: dict[str, Any]) -> None:
    if "execution_selection" not in packet:
        return
    # Even a query-only subset needs an OPS selection review: absence of cloud
    # writes must not permit silently dropping another required dimension.
    from .creation import validate_authorization as validate_creation_authorization

    validate_creation_authorization(root, packet["task_id"].rsplit(":", 1)[-1], packet)
