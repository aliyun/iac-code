"""Explicit human cleanup disposition, never a fabricated absence result."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .files import WorkflowError, contained, digest, read_json


def _is_owned_resource_group(root: Path, resource: dict[str, Any], proof: dict[str, Any]) -> bool:
    resource_type = resource.get("resource_type")
    if resource_type in {"ALIYUN::ResourceManager::ResourceGroup", "alicloud_resource_manager_resource_group"}:
        return True
    # API wire owners retain their original product/action, not a synthetic ROS type.
    if resource_type is not None or (resource.get("product"), resource.get("action")) != (
        "resourcemanager", "CreateResourceGroup",
    ):
        return False
    binding = proof.get("original_creation", {})
    name = binding.get("path")
    scope = proof.get("scope", "")
    if not isinstance(name, str) or not isinstance(scope, str) or not scope.endswith(":CREATE"):
        return False
    job_id = scope.removesuffix(":CREATE")
    path = contained(root, name)
    if not name.startswith(f"jobs/{job_id}/output/") or not path.is_file() or digest(path) != binding.get("sha256"):
        return False
    source = read_json(path)
    group = source.get("response", {}).get("ResourceGroup", {})
    return (
        source.get("job_id") == job_id and source.get("dimension") == "CREATE"
        and source.get("state") == "succeeded" and source.get("resource") == resource
        and bool(source.get("attempt_id")) and bool(source.get("tool_use_id"))
        and bool(source.get("session_id")) and bool(source.get("response", {}).get("RequestId"))
        and bool(resource.get("id")) and bool(resource.get("account")) and bool(resource.get("region"))
        and group.get("Id") == resource["id"] and group.get("AccountId") == resource["account"]
    )


def prior_user_decisions(root: Path, connection: Any, task_id: str) -> list[dict[str, Any]]:
    """Hand off verified human words, never apply a prior owner's disposition to a new owner."""
    import json

    decisions = []
    for row in connection.execute("SELECT data FROM events WHERE kind='ops_user_accepted_pending_delete' ORDER BY id"):
        event = json.loads(row[0])
        if event.get("task_id") != task_id:
            continue
        owner = connection.execute("SELECT * FROM owned WHERE id=? AND task_id=?",
                                   (event["ownership_id"], task_id)).fetchone()
        binding = event["evidence"]
        path = contained(root, binding["path"])
        if (not owner or owner["state"] != "deleted" or json.loads(owner["evidence"]) != binding
                or digest(path) != binding["sha256"]):
            raise WorkflowError("prior human cleanup decision binding changed")
        proof = read_json(path)
        validate(root, task_id, owner["id"], json.loads(owner["resource"]), proof)
        human = read_json(contained(root, proof["human_authorization"]))
        decisions.append({
            "original_user_message": human["original_user_message"],
            "human_authorization": proof["human_authorization"], "human_sha256": proof["human_sha256"],
            "original_ownership_id": owner["id"], "accepted_evidence": binding,
            "actual_absence_verified": False, "accepts_new_owner": False,
            "new_owner_independent_ops_sol_review_required": True,
        })
    return decisions


def validate(root: Path, task_id: str, ownership_id: str, resource: dict[str, Any], proof: dict[str, Any]) -> None:
    """Accept only an exact owned ResourceGroup with original positive PendingDelete evidence."""
    if (
        not _is_owned_resource_group(root, resource, proof)
        or proof.get("kind") != "ops_user_accepted_pending_delete"
        or proof.get("task_id") != task_id or proof.get("ownership_id") != ownership_id
        or proof.get("resource") != resource or proof.get("absent") is not False
        or proof.get("failed") is not False
        or proof.get("cleanup_disposition") != "user_accepted_pending_delete"
    ):
        raise WorkflowError("PendingDelete exception requires exact ResourceGroup and honest absent=false")
    human_path = contained(root, proof["human_authorization"])
    source_path = contained(root, proof["source_evidence"])
    if digest(human_path) != proof.get("human_sha256") or digest(source_path) != proof.get("source_sha256"):
        raise WorkflowError("PendingDelete authorization or original response changed")
    human, source = read_json(human_path), read_json(source_path)
    if (
        human.get("kind") != "human_pending_delete_authorization" or human.get("approved") is not True
        or human.get("task_id") != task_id or human.get("ownership_id") != ownership_id
        or human.get("resource_id") != resource["id"] or not human.get("original_user_message")
        or source.get("task_id") != task_id or source.get("account") != resource.get("account")
        or source.get("original_scope") != proof.get("scope") or source.get("cloud_writes") != 0
    ):
        raise WorkflowError("PendingDelete human authorization/account/task/scope mismatch")
    calls = source.get("calls", [])
    identities = [c for c in calls if c.get("product") == "sts" and c.get("action") == "GetCallerIdentity"
                  and c.get("status_code") == 200 and c.get("response", {}).get("AccountId") == resource["account"]]
    reads = [c for c in calls if c.get("product") == "resourcemanager" and c.get("action") == "ListResourceGroups"
             and c.get("version") == "2020-03-31" and c.get("status_code") == 200
             and c.get("params") == {"ResourceGroupId": resource["id"], "PageNumber": 1, "PageSize": 10}]
    if len(identities) != 1 or len(reads) != 1:
        raise WorkflowError("PendingDelete needs original signed identity and exact bounded group read")
    response = reads[0].get("response", {})
    groups = response.get("ResourceGroups", {}).get("ResourceGroup", [])
    if (
        response.get("TotalCount") != 1 or not response.get("RequestId") or len(groups) != 1
        or groups[0].get("Id") != resource["id"] or groups[0].get("AccountId") != resource["account"]
        or groups[0].get("Status") != "PendingDelete"
    ):
        raise WorkflowError("PendingDelete exception cannot accept active, unknown or different resource")
