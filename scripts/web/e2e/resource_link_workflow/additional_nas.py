"""One separately human-approved empty NAS generation, without refilling old writes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

from .files import WorkflowError, contained, digest, read_json

LOADED_SHA = digest(Path(__file__))


def reviewed_additional_nas(
    root: Path, task: Any, run: Any, path: Path, proof: dict[str, Any],
) -> dict[str, Any]:
    from .temporary_samples import nas_task_lifetime_authorization, reviewed_empty_general_nas

    def original(field: str) -> tuple[Path, dict[str, Any]]:
        ref = proof.get(field)
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise WorkflowError("additional NAS requires exact original inputs references")
        source = contained(root / "inputs", str(ref["path"]).removeprefix("inputs/"))
        if digest(source) != ref["sha256"]:
            raise WorkflowError("additional NAS original evidence changed")
        return source, read_json(source)

    human_path, human = original("human_authorization")
    proposal_path, packet = original("proposal")
    initial_path, initial_proof = original("original_admission")
    _, operation = original("operation_review")
    initial = reviewed_empty_general_nas(root, task, run, initial_path, initial_proof)
    # Reuse all exact pinned grammar, account, network, pricing and cleanup checks.
    # The original admission is separately retained, never replaced by this view.
    base = reviewed_empty_general_nas(root, task, run, path, {
        **proof, "kind": "ops_empty_general_nas_admission",
    })
    requests = packet.get("additional_source_requests", [])
    scenes = {task["resource_id"] + "-TF-1-" + mode for mode in ("iframe", "open")}
    if (
        proof.get("kind") != "ops_additional_empty_general_nas_admission"
        or human.get("scope", {}).get("additional_generation_after_original_cleanup") is not True
        or len(requests) != 1 or requests[0].get("dimension") != "TF"
        or packet.get("additional_source_request")
        or set(packet.get("scenes", {})) != scenes
        or not scenes <= set(json.loads(task["spec"]).get("retest_scope", {}).get("scene_ids", []))
        or packet.get("execution_grants") != initial["execution_grants"]
        or proof.get("sample_binding") != initial["sample_bindings"]["TF"]
        or proof.get("network_review") != initial_proof.get("network_review")
        or proof.get("human_authorization") == initial_proof.get("human_authorization")
        or operation.get("kind") != "ops_additional_sources_review" or operation.get("approved") is not True
        or operation.get("proposal_sha256") != digest(proposal_path)
        or operation.get("additional_sources_approved") != requests
        or operation.get("carry_forward_dimensions") != {}
        or operation.get("temporary_template_corrections") or operation.get("paid_operation_review")
    ):
        raise WorkflowError("one explicit extra empty NAS generation with unchanged calls and exact TF scenes required")
    predecessor = contained(root, requests[0].get("predecessor_proposal", "missing"))
    if digest(predecessor) != requests[0].get("predecessor_sha256") or (
        read_json(predecessor).get("execution_grants") != initial["execution_grants"]
    ):
        raise WorkflowError("additional NAS predecessor must retain original exact operations")
    task_lifetime = nas_task_lifetime_authorization(human)
    permission = {"task_id": task["id"]}
    delegated = {}
    if task_lifetime:
        # One successor per actual predecessor, independent of file names,
        # comments, request IDs and attempts. Old quotas are never replenished.
        permission.update(message=human["verbatim_user_message"], lifetime="task")
        delegated = {"authorization_lifetime": "task", "prior_scope": requests[0]["prior_scope"],
                     "standing_permission_id": hashlib.sha256(canonical(permission).encode()).hexdigest()}
        permission["prior_scope"] = requests[0]["prior_scope"]
    else:
        permission.update(question=human["human_question_verbatim"], reply=human["human_reply_verbatim"])
    return {**base, "kind": proof["kind"], "human_sha256": digest(human_path),
            "proposal": str(proposal_path.relative_to(root)), "proposal_sha256": digest(proposal_path),
            "original_admission": proof["original_admission"],
            **delegated, "human_permission_id": hashlib.sha256(canonical(permission).encode()).hexdigest()}


def additional_nas_authority(
    root: Path, connection: Any, task: Any, packet: dict[str, Any] | None,
) -> dict[str, str]:
    """Revalidate sealed operations; current owner need not be deleted before review."""
    if packet is None:
        return {}
    rows = connection.execute("SELECT value FROM meta WHERE key LIKE ?",
                              ("additional_empty_nas:" + task["id"] + ":%",)).fetchall()
    if not rows:
        return {}
    from .temporary_samples import reviewed_scope

    run = connection.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
    initial = connection.execute("SELECT value FROM meta WHERE key=?",
                                 ("temporary_samples:" + task["id"],)).fetchone()
    if not initial:
        raise WorkflowError("additional NAS lost its immutable original admission")
    saved_initial = json.loads(initial[0])
    if reviewed_scope(root, task, run, saved_initial["evidence"]) != saved_initial:
        raise WorkflowError("original NAS temporary admission changed")
    for row in rows:
        saved = json.loads(row[0])
        path = contained(root, saved["evidence"])
        if saved["original_admission"] != {
            "path": saved_initial["evidence"], "sha256": saved_initial["evidence_sha256"],
        } or reviewed_additional_nas(root, task, run, path, read_json(path)) != saved:
            raise WorkflowError("immutable additional NAS admission changed")
        original = read_json(contained(root, saved["proposal"]))
        if canonical(packet) == canonical(original):
            request = packet["additional_source_requests"][0]
            authority = hashlib.sha256(canonical({"task_id": task["id"], "operation": request}).encode()).hexdigest()
            return {"TF": authority + ":TF"}
        if packet.get("additional_source_requests") == original.get("additional_source_requests"):
            raise WorkflowError("additional NAS must retain its full reviewed proposal")
    return {}
