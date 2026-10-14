"""Explicit ROS parent metadata reads retain the original source and wire history."""

import copy
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.additional_source import validate_additional_source
from scripts.web.e2e.resource_link_workflow.creation import (
    authorization_path,
    execution_digest,
    validate_authorization,
    validate_execution,
)
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write


@pytest.fixture
def parent_retest(tmp_path):
    job, region = "a" * 32, "cn-hangzhou"
    binding = {
        "resource_type": "ALIYUN::ECS::SNatEntry",
        "stack_id": "offline-stack",
        "logical_resource_id": "SNatEntry",
        "identifier": "snat-offline123",
        "snat_table_id": "stb-offline123",
        "nat_gateway_id": "ngw-offline123",
        "region_id": region,
    }
    identity = {
        "id": "identity",
        "kind": "identity",
        "quota": 1,
        "call": {
            "product": "sts",
            "action": "GetCallerIdentity",
            "version": "2015-04-01",
            "region_id": region,
            "params": {},
        },
    }
    resource = {
        "id": "resource",
        "kind": "read",
        "quota": 1,
        "read_policy": {"phase": "execute"},
        "call": {
            "product": "ros",
            "action": "GetStackResource",
            "version": "2019-09-10",
            "region_id": region,
            "params": {"RegionId": region, "StackId": "offline-stack", "LogicalResourceId": "SNatEntry"},
        },
    }
    old = {
        "task_id": "plan:R0020",
        "budget_cents": 0,
        "requests": {"ROS": "Read existing ROS resource"},
        "scenes": {
            "R0020-ROS-1-open": {
                "account": "fake",
                "region": region,
                "identifier": binding["identifier"],
                "expected_type": binding["resource_type"],
            }
        },
        "execution_grants": {"ROS": [identity, resource]},
    }
    json_write(tmp_path / "inputs/old.json", old)
    json_write(tmp_path / "inputs/old-review.json", {"approved": True})
    json_write(tmp_path / "inputs/repair.json", {"offline_fixture": True})
    json_write(
        authorization_path(tmp_path, old),
        {
            "schema_version": 3,
            "authorized": True,
            "identity_mode": "existing_account",
            "task_id": old["task_id"],
            "resource_ids": ["R0020"],
            "account": "fake",
            "max_budget_cents": 0,
            "execution_sha256": execution_digest(old),
            "lifetime": "task",
            "expires_at": None,
            "authorization_evidence": "inputs/old-review.json",
            "authorization_sha256": digest(tmp_path / "inputs/old-review.json"),
        },
    )
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        db.executescript("CREATE TABLE jobs(id,task_id,role,state,turn_id); CREATE TABLE owned(task_id,state);")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job, old["task_id"], "glm", "complete", "original-turn"))
    with sqlite3.connect(tmp_path / "execution-gate.sqlite3") as db:
        db.executescript(
            "CREATE TABLE grants(scope,id,authority,quota,used,kind,call);"
            "CREATE TABLE attempts(scope,grant_id,state,response); CREATE TABLE authority_limits(authority,id,used);"
            "CREATE TABLE identities(scope,account);"
        )
        scope, authority = job + ":ROS", execution_digest(old) + ":ROS"
        db.execute("INSERT INTO identities VALUES(?,?)", (scope, "fake"))
        for entry in old["execution_grants"]["ROS"]:
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?)",
                (
                    scope,
                    entry["id"],
                    authority,
                    1,
                    1,
                    entry["kind"],
                    canonical(entry["call"]),
                ),
            )
            db.execute("INSERT INTO authority_limits VALUES(?,?,?)", (authority, entry["id"], 1))
            response = (
                {"AccountId": "fake"}
                if entry["kind"] == "identity"
                else {
                    "StackId": binding["stack_id"],
                    "LogicalResourceId": binding["logical_resource_id"],
                    "ResourceType": binding["resource_type"],
                    "PhysicalResourceId": binding["identifier"],
                    "Status": "CREATE_COMPLETE",
                    "RequestId": "offline-request",
                }
            )
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?)",
                (
                    scope,
                    entry["id"],
                    "succeeded",
                    json.dumps(response),
                ),
            )
    new = copy.deepcopy(old)
    new["execution_grants"]["ROS"][1]["call"]["params"]["ShowResourceAttributes"] = True
    for identifier, action, params in (
        (
            "snat-parent",
            "DescribeSnatTableEntries",
            {"SnatTableId": binding["snat_table_id"], "SnatEntryId": binding["identifier"]},
        ),
        ("gateway-parent", "DescribeNatGateways", {"NatGatewayId": binding["nat_gateway_id"]}),
    ):
        new["execution_grants"]["ROS"].append(
            {
                "id": identifier,
                "kind": "read",
                "quota": 1,
                "read_policy": {"phase": "execute"},
                "call": {
                    "product": "vpc",
                    "action": action,
                    "version": "2016-04-28",
                    "region_id": region,
                    "params": {"RegionId": region, **params, "PageNumber": 1, "PageSize": 10},
                },
            }
        )
    new["additional_source_request"] = {
        "request_id": "explicit-ros-parent-retest-001",
        "dimension": "ROS",
        "prior_scope": job + ":ROS",
        "predecessor_proposal": "inputs/old.json",
        "predecessor_sha256": digest(tmp_path / "inputs/old.json"),
        "remediation_evidence": "inputs/repair.json",
        "remediation_sha256": digest(tmp_path / "inputs/repair.json"),
        "ros_snat_parent_binding": binding,
    }
    proof = {
        "kind": "ops_additional_source_review",
        "approved": True,
        "additional_source_approved": copy.deepcopy(new["additional_source_request"]),
        "carry_forward_dimensions": {},
    }
    return tmp_path, old, new, proof


def test_exact_ros_parent_retest_preserves_both_original_databases_and_needs_ops(parent_retest):
    root, old, new, proof = parent_retest
    before = {name: (root / name).read_bytes() for name in ("state.sqlite3", "execution-gate.sqlite3")}
    validate_execution(new, {"ROS"}, require_read_policy=True, readonly_dimensions={"ROS"})
    validate_additional_source(root, new, proof)
    validate_authorization(root, "R0020", old)
    with pytest.raises(WorkflowError, match="explicit OPS authorization"):
        validate_authorization(root, "R0020", new)
    assert before == {name: (root / name).read_bytes() for name in before}


@pytest.mark.parametrize(
    "damage",
    [
        "no_request",
        "query",
        "cross_region",
        "cross_resource",
        "extra_read",
        "write",
        "quota",
        "unbounded",
        "missing_table",
        "wrong_gateway",
        "template_instead",
        "cleanup",
        "identity_params",
        "wrong_version",
        "new_stack",
        "new_logical",
        "unreviewed",
        "source_id",
        "source_type",
        "source_deleted",
        "source_unknown",
        "source_account",
        "source_call",
        "source_unused",
        "active",
        "outstanding",
        "renamed_operation",
    ],
)
def test_ros_parent_retest_rejects_expansion_or_unproven_predecessor(parent_retest, damage):
    root, old, new, proof = parent_retest
    reads = new["execution_grants"]["ROS"]
    binding = new["additional_source_request"]["ros_snat_parent_binding"]
    if damage == "no_request":
        new.pop("additional_source_request")
    elif damage == "query":
        new["additional_source_request"]["dimension"] = "QUERY"
    elif damage == "cross_region":
        reads[-1]["call"]["region_id"] = "cn-beijing"
    elif damage == "cross_resource":
        next(iter(new["scenes"].values()))["identifier"] = "snat-foreign"
    elif damage == "extra_read":
        reads.append(copy.deepcopy(reads[-1]))
    elif damage == "write":
        reads[-1]["kind"] = "create"
    elif damage == "quota":
        reads[-1]["quota"] = 2
    elif damage == "unbounded":
        reads[-1]["call"]["params"]["PageSize"] = 100
    elif damage == "missing_table":
        reads[2]["call"]["params"].pop("SnatTableId")
    elif damage == "wrong_gateway":
        reads[-1]["call"]["params"]["NatGatewayId"] = binding["identifier"]
    elif damage == "template_instead":
        reads[1]["call"]["action"] = "GetTemplate"
    elif damage == "cleanup":
        reads[-1]["read_policy"]["phase"] = "cleanup"
    elif damage == "identity_params":
        reads[0]["call"]["params"]["Extra"] = "not-approved"
    elif damage == "wrong_version":
        reads[2]["call"]["version"] = "unreviewed"
    elif damage in {"new_stack", "new_logical"}:
        key, param = ("stack_id", "StackId") if damage == "new_stack" else ("logical_resource_id", "LogicalResourceId")
        binding[key] = "foreign"
        reads[1]["call"]["params"][param] = "foreign"
        proof["additional_source_approved"] = copy.deepcopy(new["additional_source_request"])
    elif damage == "unreviewed":
        proof["approved"] = False
    elif damage.startswith("source_"):
        with sqlite3.connect(root / "execution-gate.sqlite3") as db:
            if damage == "source_account":
                db.execute("UPDATE identities SET account='foreign'")
            elif damage == "source_call":
                db.execute("UPDATE grants SET call='{}' WHERE id='resource'")
            elif damage == "source_unused":
                db.execute("UPDATE authority_limits SET used=0")
            elif damage == "source_unknown":
                db.execute("UPDATE attempts SET state='unknown'")
            else:
                row = db.execute("SELECT response FROM attempts WHERE grant_id='resource'").fetchone()
                response = json.loads(row[0])
                key, value = {
                    "source_id": ("PhysicalResourceId", "snat-foreign"),
                    "source_type": ("ResourceType", "ALIYUN::VPC::SnatEntry"),
                    "source_deleted": ("Status", "DELETE_COMPLETE"),
                }[damage]
                response[key] = value
                db.execute("UPDATE attempts SET response=? WHERE grant_id='resource'", (json.dumps(response),))
    elif damage in {"active", "outstanding"}:
        with sqlite3.connect(root / "state.sqlite3") as db:
            db.execute(
                "UPDATE jobs SET state='running'"
                if damage == "active"
                else "INSERT INTO owned VALUES('plan:R0020','present')"
            )
    elif damage == "renamed_operation":
        json_write(
            root / "creation-authorizations/prior.json",
            {
                "task_id": new["task_id"],
                "additional_source_request": copy.deepcopy(new["additional_source_request"]),
            },
        )
        new["additional_source_request"]["request_id"] = "renamed-copy-operation-001"
        proof["additional_source_approved"] = copy.deepcopy(new["additional_source_request"])
    with pytest.raises((WorkflowError, TypeError, KeyError)):
        validate_execution(new, {"ROS"}, require_read_policy=True, readonly_dimensions={"ROS"})
        validate_additional_source(root, new, proof)
