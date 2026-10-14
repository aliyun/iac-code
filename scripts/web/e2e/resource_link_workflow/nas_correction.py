"""One reviewed native NAS parameter correction after a proved rejected order.

The predecessor admission, nine actual calls and their allowances stay immutable.
This is a distinct operation, never a reset or a claim of successful testing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from .files import WorkflowError, contained, digest, read_json
from .nas_rejection import document
from .nas_rejection import validate as validate_rejection

KIND = "ops_nas_native_bandwidth_omission_correction"
LOADED_SHA = digest(Path(__file__))


def reviewed_correction(root: Path, old: dict[str, Any], packet: dict[str, Any],
                        request: dict[str, Any], proof: dict[str, Any]) -> None:
    from .creation import execution_digest, validate_authorization
    from .hourly_budget import operation_review
    from .store import Store
    from .temporary_samples import reviewed_scope

    reviews = proof.get("native_nas_corrections", {})
    review = reviews.get(request.get("request_id"), {})
    task_id = packet["task_id"]
    if (set(reviews) != {request.get("request_id")} or review.get("kind") != KIND
            or review.get("approved") is not True or review.get("task_id") != task_id
            or request.get("dimension") != "QUERY" or packet.get("additional_source_requests") != [request]
            or set(packet.get("execution_grants", {})) != {"QUERY"}
            or set(packet.get("scenes", {})) != {task_id.rsplit(":", 1)[-1] + "-QUERY-1-iframe"}
            or not review.get("human_scope_interpretation")
            or review.get("no_journal_mutation") is not True or review.get("pass_added") is not False):
        raise WorkflowError("one reviewed native NAS remaining-iframe correction required")
    for key in ("reconciliation", "source_review", "original_admission"):
        if not isinstance(review.get(key), dict) or set(review[key]) != {"path", "sha256"}:
            raise WorkflowError("NAS correction requires exact original evidence path/SHA references")
    predecessor = contained(root, request.get("predecessor_proposal", "missing"))
    remediation = contained(root, request.get("remediation_evidence", "missing"))
    if (digest(predecessor) != request.get("predecessor_sha256") or read_json(predecessor) != old
            or digest(remediation) != request.get("remediation_sha256")):
        raise WorkflowError("NAS correction predecessor or terminal diagnosis changed")
    # Preserve the complete original scene, request, cleanup and dependency graph.
    excluded = {"execution_grants", "hourly_cost", "additional_source_requests"}
    if ({k: v for k, v in packet.items() if k not in excluded}
            != {k: v for k, v in old.items() if k not in excluded}):
        raise WorkflowError("NAS correction cannot change original packet outside its reviewed calls and costs")
    entries = old["execution_grants"].get("QUERY", [])
    name = next((g["call"]["params"].get("bucket") for g in entries if g["id"] == "create-bucket"), None)
    if (not name or entries != gate.nas_native_plan(name)
            or packet["execution_grants"]["QUERY"] != gate.nas_native_plan(name, variant="bandwidth_omission_bwo1")):
        raise WorkflowError("NAS correction permits only two Bandwidth omissions and four stable bwo1 tokens")
    if packet.get("hourly_cost", {}).get("max_hourly_cents") != old.get("hourly_cost", {}).get("max_hourly_cents"):
        raise WorkflowError("NAS correction cannot enlarge its original hourly envelope")
    validate_authorization(root, task_id.rsplit(":", 1)[-1], old)
    audit = document(root, review.get("reconciliation", {}))
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as state:
        state.row_factory = sqlite3.Row
        task = state.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        row = state.execute("SELECT value FROM meta WHERE key=?", ("temporary_samples:" + task_id,)).fetchone()
        if not task or not row:
            raise WorkflowError("NAS correction needs its immutable initial admission")
        saved = json.loads(row[0])
        run = state.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
        if (reviewed_scope(root, task, run, saved["evidence"]) != saved
                or saved.get("kind") != "ops_native_nas_query_sample_admission"
                or review.get("original_admission") != {"path": saved["evidence"], "sha256": saved["evidence_sha256"]}
                or review.get("frozen_plan_sha256") != hashlib.sha256(run["plan"].encode()).hexdigest()
                or saved["proposal"] != request["predecessor_proposal"]
                or saved["proposal_sha256"] != request["predecessor_sha256"]
                or old["execution_grants"] != saved["execution_grants"]
                or old["task_id"] != task_id or packet.get("budget_cents") != saved["budget_cents"]
                or sorted({v["account"] for v in packet["scenes"].values()}) != saved["accounts"]):
            raise WorkflowError("NAS original admission, frozen scope, account or budget changed")
        reconciled = validate_rejection(root, state, task_id, audit)
        if (request.get("prior_scope") != audit["original_job"] + ":QUERY"
                or not reconciled <= Store(root).reconciled_creations(state, task_id)):
            raise WorkflowError("NAS correction requires committed exact original rejection reconciliation")
        diagnosis = read_json(remediation)
        if not state.execute("SELECT 1 FROM jobs WHERE task_id=? AND role='sol' AND state='complete' "
                             "AND turn_id IS NOT NULL AND id=?", (task_id, diagnosis.get("job_id"))).fetchone():
            raise WorkflowError("NAS correction needs its exact terminal Sol diagnosis")
        if operation_review(root, state, task, packet, proof) is None:
            raise WorkflowError("NAS correction needs its own full itemized hourly review")
    source = document(root, review.get("source_review", {}))
    required = {"kind": "ops_native_nas_query_source_review", "approved": True, "task_id": task_id,
                "account": saved["accounts"][0], "execution_sha256": execution_digest(packet),
                "sample_binding": {"name": name, "region": "cn-hangzhou", "execution_grants_sha256":
                                   hashlib.sha256(gate.canonical(packet["execution_grants"]).encode()).hexdigest()},
                "full_native_graph_reviewed": True, "implicit_dependencies_reviewed": True,
                "no_existing_resource_mutation": True, "all_new_resources_response_bound_cleanup": True,
                "no_object_io": True, "no_posix_client": True}
    if (any(source.get(k) != v for k, v in required.items()) or not source.get("estimate_basis")
            or not source.get("limitations") or not source.get("official_sources")
            or not review.get("official_sources")):
        raise WorkflowError("NAS corrected complete source/dependency/cleanup review differs")
    for ref in source["official_sources"] + review["official_sources"]:
        path = contained(root, ref.get("path", "missing"))
        if digest(path) != ref.get("sha256"):
            raise WorkflowError("NAS correction official source changed")
    scope, authority = request["prior_scope"], saved["execution_sha256"] + ":QUERY"
    if review.get("original_authority") != authority:
        raise WorkflowError("NAS original authority changed")
    expected_ids = ["identity", "precheck-bucket-absent", "create-filesystem-vpc", "read-filesystem-vpc",
                    "create-filesystem-vswitch", "read-filesystem-vswitch", "read-filesystem-implicit-members",
                    "precheck-filesystem", "create-filesystem"]
    with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as wire:
        attempts = wire.execute("SELECT grant_id,state FROM attempts WHERE scope=? ORDER BY created",
                                (scope,)).fetchall()
        if attempts != [(g, "unknown" if g in {"precheck-bucket-absent", "create-filesystem"} else "succeeded")
                        for g in expected_ids]:
            raise WorkflowError("NAS correction original nine actual calls differ")
        for entry in entries:
            used = int(entry["id"] in expected_ids)
            grant = wire.execute("SELECT kind,call,authority,quota,used FROM grants WHERE scope=? AND id=?",
                                 (scope, entry["id"])).fetchone()
            limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                 (authority, entry["id"])).fetchone()
            if (grant != (entry["kind"], gate.canonical(entry["call"]), authority, entry["quota"], used)
                    or limit != (used,)
                    or wire.execute("SELECT 1 FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                                    "WHERE g.authority=? AND a.scope!=?", (authority, scope)).fetchone()):
                raise WorkflowError("NAS correction cannot refill or split original cumulative allowances")
        # Terminal dispatch revokes active capabilities. Verify historical wire,
        # not a live entitlement, without recreating the original active row.
        wire.row_factory = sqlite3.Row
        rows = wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created", (scope,)).fetchall()
        signed, negative = rows[0], rows[1]
        response = json.loads(negative["response"])
        call = next(g["call"] for g in entries if g["id"] == "precheck-bucket-absent")
        if (any(any(row[k] != signed[k] for k in ("session", "credential", "reference_scope"))
                or not row["tool_use_id"] for row in rows)
                or response.get("call") != call
                or not gate.nas_native_ready("precheck-bucket-absent", call, response)):
            raise WorkflowError("NAS original bucket precheck must prove its known negative, not an unknown read")
    for path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(path)
        if approval.get("task_id") != task_id:
            continue
        prior = read_json(contained(root, approval["authorization_evidence"]))
        if digest(contained(root, approval["authorization_evidence"])) != approval["authorization_sha256"]:
            raise WorkflowError("NAS historical operation approval changed")
        previous = prior.get("native_nas_corrections", {})
        if any(v.get("kind") == KIND for v in previous.values()) and (
                previous != reviews or approval.get("additional_source_requests") != [request]):
            raise WorkflowError("NAS original generation already has its one reviewed bandwidth correction")
