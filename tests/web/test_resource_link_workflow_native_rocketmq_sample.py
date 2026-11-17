"""Offline immutable admission of one empty standard native ROS sample."""

import copy
import hashlib
import json

import pytest

from scripts.web.e2e.resource_link_workflow.creation import execution_digest
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.hourly_budget import register
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow import resource


@pytest.fixture
def reviewed(tmp_path):
    store = Store.initialize(tmp_path / "state")
    spec = resource(83)
    spec["key"] = "ALIYUN::ROCKETMQ::Instance"
    for dim in ("ROS", "QUERY"):
        spec["dimensions"][dim].update(
            support="supported",
            contracts=[
                {
                    "id": "R0083-" + dim + "-1",
                    "source_type": spec["key"],
                    "historical": {"iframe": "pass", "open": "fail"},
                }
            ],
        )
    spec["execution_policy_version"] = 2
    plan = {"schema_version": 1, "plan_id": "offline-native", "resources": [spec]}
    store.import_plan(plan)
    scenes = ["R0083-QUERY-1-open", "R0083-ROS-1-open"]
    spec["retest_scope"] = {"kind": "failed_navigation_managed", "scene_ids": scenes}
    with store.transaction() as db:
        db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
        db.execute("UPDATE runs SET plan=?", (dumps(plan),))
    task = "offline-native:R0083"
    frozen = hashlib.sha256(dumps(plan).encode()).hexdigest()
    json_write(store.root / "inputs/official.json", {"fixture": "OFFLINE FAKE OFFICIAL MATERIAL"})

    def ref(name):
        return {"path": "inputs/" + name, "sha256": digest(store.root / "inputs" / name)}

    human = {
        "schema_version": 1,
        "kind": "human_hourly_fee_authorization",
        "approved": True,
        "plan_id": plan["plan_id"],
        "currency": "CNY",
        "resource_ids": ["R0083"],
        "scene_ids": scenes,
        "max_concurrent_hourly_cents": 1000,
        "total_budget_cents": None,
        "verbatim_user_message": "OFFLINE FAKE AUTHORIZATION",
    }
    json_write(store.root / "inputs/hourly-human.json", human)
    allowance = {
        "schema_version": 1,
        "kind": "ops_run_hourly_fee_review",
        "approved": True,
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": frozen,
        "currency": "CNY",
        "resource_ids": ["R0083"],
        "scene_ids": scenes,
        "max_concurrent_hourly_cents": 1000,
        "human_authorization": ref("hourly-human.json"),
    }
    json_write(store.root / "inputs/allowance.json", allowance)
    register(store, plan["plan_id"], "inputs/allowance.json")
    json_write(
        store.root / "inputs/human.json",
        {
            "kind": "human_temporary_source_authorization",
            "approved": True,
            "task_id": task,
            "plan_id": plan["plan_id"],
            "verbatim_user_message": "OFFLINE FAKE ONE EMPTY SAMPLE PERMISSION",
            "scope": {
                "native_ros_empty_standard_rocketmq_instance_max": 1,
                **{
                    k: True
                    for k in (
                        "no_topics",
                        "no_groups",
                        "no_messages",
                        "no_existing_business_resource_mutation",
                        "delete_own_stack_and_instance_after_test",
                        "operation_review_required",
                    )
                },
            },
        },
    )
    name = "iac-link-offline-native"
    body = json.dumps(
        {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {
                "Instance": {"Type": spec["key"], "Properties": {"InstanceName": name, "DeletionForce": False}}
            },
            "Outputs": {"InstanceId": {"Value": {"Fn::GetAtt": ["Instance", "InstanceId"]}}},
        }
    )

    def call(product, action, params):
        return {
            "product": product,
            "action": action,
            "region_id": "cn-hangzhou",
            "params": params,
            "version": {"ros": "2019-09-10", "ons": "2019-02-14", "sts": "2015-04-01"}[product],
        }

    identity = {"id": "identity", "kind": "identity", "quota": 1, "call": call("sts", "GetCallerIdentity", {})}
    create = {
        "id": "create",
        "kind": "create",
        "quota": 1,
        "call": call(
            "ros",
            "CreateStack",
            {
                "RegionId": "cn-hangzhou",
                "StackName": name,
                "TemplateBody": body,
                "TimeoutInMinutes": 30,
                "DisableRollback": False,
            },
        ),
    }

    def read(ident, action, phase="execute", own=True, required=False):
        params = {"RegionId": "cn-hangzhou", **({"StackId": "$created_id"} if own else {"NeedResourceInfo": True})}
        if action == "GetStackResource":
            params.update(LogicalResourceId="Instance", ShowResourceAttributes=True)
        entry = {
            "id": ident,
            "kind": "read",
            "quota": 61 if action == "GetStack" else 1,
            "call": call("ros" if own else "ons", action, params),
            "read_policy": {"phase": phase},
        }
        if action == "GetStack":
            entry["read_policy"]["min_interval_seconds"] = 30
        if own:
            entry.update(parent="create", response_path=["StackId"], target_param="StackId")
        if required:
            entry["read_policy"].update(required_cleanup=True, after_delete="create")
        return entry

    ros = [
        identity,
        create,
        read("status", "GetStack"),
        read("members", "ListStackResources"),
        read("member", "GetStackResource"),
        read("inventory", "OnsInstanceInServiceList", own=False),
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "call": call(
                "ros", "DeleteStack", {"RegionId": "cn-hangzhou", "StackId": "$created_id", "RetainAllResources": False}
            ),
        },
        read("root-absence", "GetStack", "cleanup", required=True),
        read("instance-absence", "OnsInstanceInServiceList", "cleanup", own=False, required=True),
    ]
    value = {
        "task_id": task,
        "profile": "resource-link-test",
        "terraform": "ros_managed_terraform",
        "budget_cents": 0,
        "requests": {"ROS": "Create only approved own empty native sample", "QUERY": "Readonly business metadata"},
        "cleanup_requests": {"ROS": "Delete only original own stack and verify own instance absence"},
        "cleanup_recipe": "Original generation stack and exact instance disappearance",
        "execution_grants": {
            "ROS": ros,
            "QUERY": [copy.deepcopy(identity), read("query", "OnsInstanceInServiceList", own=False)],
        },
        "scenes": {s: {"account": "1234567890123456", "region": "cn-hangzhou"} for s in scenes},
    }
    json_write(store.root / "inputs/proposal.json", value)
    binding = {
        "template_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "region": "cn-hangzhou",
        "stack_name": name,
        "instance_name": name,
        "logical_resource_id": "Instance",
        "resource_type": spec["key"],
    }
    source = {
        "kind": "ops_native_rocketmq_source_review",
        "approved": True,
        "task_id": task,
        "account": "1234567890123456",
        "execution_sha256": execution_digest(value),
        "sample_binding": binding,
        **{
            k: True
            for k in (
                "full_inline_template_reviewed",
                "implicit_dependencies_reviewed",
                "no_existing_resource_mutation",
                "all_new_resources_response_bound_cleanup",
            )
        },
        "estimate_basis": "offline fake review",
        "limitations": "not real cloud proof",
        "official_sources": [ref("official.json")],
    }
    cost = {
        "kind": "ops_empty_native_rocketmq_cost_review",
        "approved": True,
        "task_id": task,
        "account": "1234567890123456",
        "currency": "CNY",
        "budget_cents": 0,
        "execution_sha256": execution_digest(value),
        "estimated_incremental_hourly_cents": 0,
        "includes_all_dependencies": True,
        "includes_request_and_network_costs": True,
        "estimate_basis": "offline fake empty graph",
        "limitations": "not a real quote",
        "official_sources": [ref("official.json")],
    }
    operation = {"approved": True, "task_id": task, "proposal_sha256": digest(store.root / "inputs/proposal.json")}
    for filename, item in (("source.json", source), ("cost.json", cost), ("operation.json", operation)):
        json_write(store.root / "inputs" / filename, item)
    proof = {
        "schema_version": 1,
        "kind": "ops_native_rocketmq_sample_admission",
        "approved": True,
        "task_id": task,
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": frozen,
        "execution_sha256": execution_digest(value),
        "proposal": ref("proposal.json"),
        "human_authorization": ref("human.json"),
        "source_review": ref("source.json"),
        "cost_review": ref("cost.json"),
        "operation_review": ref("operation.json"),
    }
    json_write(store.root / "inputs/admission.json", proof)
    return store, value, proof


def test_admission_is_immutable_and_query_never_gains_write_allowance(reviewed):
    store, packet, _ = reviewed
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert store.readonly_dimensions(db, task, packet) == {"ROS", "QUERY"}
        original = {t: list(map(tuple, db.execute("SELECT * FROM " + t))) for t in ("tasks", "owned", "verdicts")}
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")["no_cloud_call"]
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")["already_recorded"]
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert store.readonly_dimensions(db, task, packet) == {"QUERY"}
        assert admitted(store.root, db, task, packet) == {"ROS": execution_digest(packet) + ":ROS"}
        assert all(list(map(tuple, db.execute("SELECT * FROM " + t))) == rows for t, rows in original.items())
    assert Runtime(store).authorize_creation(store.root / "inputs/proposal.json", "inputs/operation.json", 0)[
        "execution_reviewed"
    ]


@pytest.mark.parametrize(
    "damage",
    [
        "human",
        "cost",
        "operation",
        "source",
        "proposal",
        "official",
        "unapproved",
        "frozen",
        "active",
        "after_worker",
        "unpaused",
    ],
)
def test_reject_changed_evidence_or_retroactive_allowance(reviewed, damage):
    store, packet, proof = reviewed
    if damage in {"human", "cost", "operation", "source", "proposal", "official"}:
        json_write(store.root / "inputs" / (damage + ".json"), {"tampered": True})
    elif damage in {"unapproved", "frozen"}:
        proof["approved" if damage == "unapproved" else "frozen_plan_sha256"] = False
        json_write(store.root / "inputs/admission.json", proof)
    elif damage == "unpaused":
        store.pause(False)
    else:
        with store.transaction() as db:
            if damage == "after_worker":
                db.execute("UPDATE tasks SET attempt=1")
            else:
                db.execute("INSERT INTO actions VALUES('pending',NULL,'prepare','issued',1)")
    with pytest.raises(WorkflowError):
        store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")
