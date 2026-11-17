"""Revalidate registered OPS network cleanup, without inventing model wire reads."""
from __future__ import annotations

import json
from pathlib import Path

from .files import WorkflowError, contained, digest, read_json


def registered_network_cleanup(root: Path, db, task: str, owner, creation: dict) -> bool:
    if owner["state"] != "deleted":
        return False
    binding = json.loads(owner["evidence"])
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str) or not binding.get("sha256"):
        return False
    registered = False
    for row in db.execute("SELECT data FROM events WHERE kind='hourly_cost_released'"):
        event = json.loads(row[0])
        basis = event.get("release_basis", {})
        proof = basis.get("evidence", {})
        if (event.get("task_id") == task and event.get("job_id") == creation["job_id"]
                and event.get("state") == "released" and basis.get("kind") == "ops_verified_cleanup"
                and proof.get("task_id") == task and {"ownership_id": owner["id"], "evidence": binding["path"]}
                in proof.get("cleanup", [])):
            registered = True
    if not registered:
        return False

    def document(entry):
        file = contained(root, entry["path"])
        if digest(file) != entry["sha256"]:
            raise WorkflowError("registered OPS cleanup original artifact changed")
        return read_json(file)

    value = document(binding)
    resource = json.loads(owner["resource"])
    if value.get("kind") != "ops_original_owner_cleanup_proof":
        return False  # Other external cleanup formats retain their own validators.
    if (value.get("ownership_id") != owner["id"] or value.get("resource") != resource
            or resource != creation["resource"] or resource.get("product") != "vpc"
            or resource.get("action") not in {"CreateVpc", "CreateVSwitch"}
            or value.get("absent") is not True or value.get("failed") is not False
            or value.get("original_wire_unchanged") is not True or value.get("pass_added") is not False
            or value.get("formal_test") is not False):
        raise WorkflowError("registered OPS network cleanup owner mismatch")
    audits, seen = [], set()
    entry = value["audit"]
    while entry:
        if entry["path"] in seen or len(seen) >= 8:
            raise WorkflowError("registered OPS cleanup audit chain is ambiguous")
        seen.add(entry["path"])
        audit = document(entry)
        if (audit.get("kind") != "ops_original_native_network_cleanup_result" or audit.get("task_id") != task
                or audit.get("original_job_id") != creation["job_id"] or audit.get("pass_added") is not False
                or audit.get("original_wire_unchanged") is not True):
            raise WorkflowError("registered OPS cleanup audit belongs to another generation")
        audits.append(audit)
        entry = audit.get("prior_report")
    if audits[0].get("both_original_networks_absence_verified") is not True or audits[0].get("failure_type"):
        raise WorkflowError("registered OPS cleanup has no final successful absence audit")
    facts = [f for f in audits[0]["facts"] if f["ownership_id"] == owner["id"] and f["resource"] == resource]
    if len(facts) != 1:
        raise WorkflowError("registered OPS cleanup original creation binding missing")
    original = document(facts[0]["evidence"])
    if any(original.get(k) != creation.get(k) for k in (
            "job_id", "attempt_id", "dimension", "session_id", "resource", "state", "response")):
        raise WorkflowError("registered OPS cleanup creation response changed")

    def operation(entry):
        raw = document(entry)
        matches = [(audit, e) for audit in audits for e in audit["events"]
                   if e.get("path") == entry["path"] and e.get("sha256") == entry["sha256"]]
        if len(matches) != 1:
            raise WorkflowError("registered OPS cleanup operation is not uniquely audited")
        audit, event = matches[0]
        if (event.get("error") is not False or event.get("code") or raw.get("Code")
                or not raw.get("RequestId") or raw["RequestId"] != event.get("request_id")):
            raise WorkflowError("registered OPS cleanup operation did not succeed")
        identities = [e for e in audit["events"] if e.get("call", {}).get("product") == "sts"
                      and e["call"].get("action") == "GetCallerIdentity"
                      and e.get("error") is False and e["completed_at"] < event["started_at"]]
        if len(identities) != 1:
            raise WorkflowError("registered OPS cleanup needs its signed account identity")
        identity = identities[0]
        signed = document(identity)
        if (signed.get("AccountId") != resource["account"] or not signed.get("RequestId")
                or signed["RequestId"] != identity["request_id"]):
            raise WorkflowError("registered OPS cleanup account mismatch")
        return event, raw

    deleted, _ = operation(value["delete_evidence"])
    observed, raw = operation({"path": value["source_evidence"], "sha256": value["source_sha256"]})
    vpc = resource["action"] == "CreateVpc"
    key, collection = ("VpcId", "Vpcs") if vpc else ("VSwitchId", "VSwitches")
    params = {"RegionId": resource["region"], key: resource["id"]}
    expected = {"product": "vpc", "version": "2016-04-28", "region_id": resource["region"]}
    if (deleted["call"] != {**expected, "action": "DeleteVpc" if vpc else "DeleteVSwitch", "params": params}
            or observed["call"] != {**expected, "action": "DescribeVpcs" if vpc else "DescribeVSwitches",
                                    "params": {**params, "PageNumber": 1, "PageSize": 10}}
            or observed["started_at"] <= deleted["completed_at"]
            or raw.get("TotalCount") != 0 or raw.get(collection, {}).get("Vpc" if vpc else "VSwitch") != []
            or raw.get("PageNumber") != 1 or raw.get("PageSize") != 10 or raw.get("NextToken")):
        raise WorkflowError("registered OPS cleanup needs exact successful delete then bounded empty query")
    return True
