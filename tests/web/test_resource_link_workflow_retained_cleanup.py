"""Registered historical cleanup is not a fabricated model-side wire response."""

import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.retained_cleanup import registered_network_cleanup


@pytest.fixture(params=["Vpc", "VSwitch"])
def retained(tmp_path, request):
    root, task, job = tmp_path, "plan:R0036", "original-job"
    kind = request.param
    resource = {
        "id": "own-network",
        "account": "fake-account",
        "region": "cn-hangzhou",
        "product": "vpc",
        "action": "Create" + kind,
    }

    def save(name, value):
        path = "inputs/" + name + ".json"
        json_write(root / path, value)
        return {"path": path, "sha256": digest(root / path)}

    creation = {
        "job_id": job,
        "attempt_id": "create-attempt",
        "dimension": "QUERY",
        "session_id": "ws~own",
        "resource": resource,
        "state": "succeeded",
        "response": {kind + "Id": resource["id"]},
    }
    params = {"RegionId": resource["region"], kind + "Id": resource["id"]}
    common = {"product": "vpc", "version": "2016-04-28", "region_id": resource["region"]}
    identity_raw = {"AccountId": resource["account"], "RequestId": "signed-account"}
    deleted_raw = {"RequestId": "deleted-own"}
    observed_raw = {
        "RequestId": "observed-own",
        "TotalCount": 0,
        "PageNumber": 1,
        "PageSize": 10,
        "Vpcs" if kind == "Vpc" else "VSwitches": {kind: []},
    }
    identity = save("identity", identity_raw) | {
        "request_id": "signed-account",
        "error": False,
        "started_at": 10,
        "completed_at": 11,
        "call": {"product": "sts", "action": "GetCallerIdentity"},
    }
    deleted = save("delete", deleted_raw) | {
        "request_id": "deleted-own",
        "error": False,
        "started_at": 20,
        "completed_at": 21,
        "call": common | {"action": "Delete" + kind, "params": params},
    }
    observed = save("absence", observed_raw) | {
        "request_id": "observed-own",
        "error": False,
        "started_at": 30,
        "completed_at": 31,
        "call": common
        | {
            "action": "DescribeVpcs" if kind == "Vpc" else "DescribeVSwitches",
            "params": params | {"PageNumber": 1, "PageSize": 10},
        },
    }
    audit = {
        "kind": "ops_original_native_network_cleanup_result",
        "task_id": task,
        "original_job_id": job,
        "pass_added": False,
        "original_wire_unchanged": True,
        "both_original_networks_absence_verified": True,
        "failure_type": None,
        "facts": [{"ownership_id": "own", "resource": resource, "evidence": save("creation", creation)}],
        "events": [identity, deleted, observed],
    }
    proof = {
        "kind": "ops_original_owner_cleanup_proof",
        "ownership_id": "own",
        "resource": resource,
        "absent": True,
        "failed": False,
        "original_wire_unchanged": True,
        "pass_added": False,
        "formal_test": False,
        "audit": save("audit", audit),
        "delete_evidence": deleted,
        "source_evidence": observed["path"],
        "source_sha256": observed["sha256"],
    }
    owner = {
        "id": "own",
        "state": "deleted",
        "resource": json.dumps(resource),
        "evidence": json.dumps(save("owner-proof", proof)),
    }
    event = {
        "task_id": task,
        "job_id": job,
        "state": "released",
        "release_basis": {
            "kind": "ops_verified_cleanup",
            "evidence": {"task_id": task, "cleanup": [{"ownership_id": "own", "evidence": "inputs/owner-proof.json"}]},
        },
    }
    return root, task, owner, creation, proof, audit, event, save


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "not_deleted",
        "unregistered",
        "wrong_registered_job",
        "owner",
        "creation",
        "account",
        "delete_target",
        "absence_target",
        "still_present",
        "pagination",
        "before_delete",
        "failed_delete",
        "pass_flag",
        "changed_raw",
    ],
)
def test_only_registered_exact_external_cleanup_can_satisfy_old_owner(retained, damage):
    root, task, owner, creation, proof, audit, event, save = retained
    if damage == "not_deleted":
        owner["state"] = "present"
    elif damage == "wrong_registered_job":
        event["job_id"] = "other-job"
    elif damage == "owner":
        proof["ownership_id"] = "other-owner"
    elif damage == "creation":
        creation = creation | {"attempt_id": "other-attempt"}
    elif damage == "account":
        changed = save("identity", {"AccountId": "other-account", "RequestId": "signed-account"})
        audit["events"][0].update(changed)
    elif damage in {"delete_target", "absence_target"}:
        target = audit["events"][1 if damage == "delete_target" else 2]
        key = "VpcId" if creation["resource"]["action"] == "CreateVpc" else "VSwitchId"
        target["call"]["params"][key] = "business-network"
    elif damage in {"still_present", "pagination", "changed_raw"}:
        kind = "Vpc" if creation["resource"]["action"] == "CreateVpc" else "VSwitch"
        raw = {
            "RequestId": "observed-own",
            "TotalCount": 0,
            "PageNumber": 1,
            "PageSize": 10,
            "Vpcs" if kind == "Vpc" else "VSwitches": {kind: []},
        }
        raw["TotalCount" if damage != "pagination" else "PageNumber"] = 1 if damage != "pagination" else 2
        changed = save("absence", raw)
        if damage != "changed_raw":
            audit["events"][2].update(changed)
            proof["source_sha256"] = changed["sha256"]
    elif damage == "before_delete":
        audit["events"][2]["started_at"] = 19
    elif damage == "failed_delete":
        audit["events"][1]["error"] = True
    elif damage == "pass_flag":
        proof["pass_added"] = True
    proof["audit"] = save("audit", audit)
    owner["evidence"] = json.dumps(save("owner-proof", proof))
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE events(kind TEXT,data TEXT)")
        if damage != "unregistered":
            db.execute("INSERT INTO events VALUES(?,?)", ("hourly_cost_released", json.dumps(event)))
        if damage in {"not_deleted", "unregistered", "wrong_registered_job"}:
            assert not registered_network_cleanup(root, db, task, owner, creation)
        elif damage:
            with pytest.raises(WorkflowError):
                registered_network_cleanup(root, db, task, owner, creation)
        else:
            assert registered_network_cleanup(root, db, task, owner, creation)
