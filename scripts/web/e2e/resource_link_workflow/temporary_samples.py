"""OPS-only exact temporary-source admission; frozen navigation targets stay immutable."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow.execution_gate import canonical

from .creation import (
    authorization_path,
    carry_forward_authorities,
    execution_digest,
    validate_authorization,
    validate_execution,
)
from .files import WorkflowError, contained, digest, read_json

LOADED_SHA = digest(Path(__file__))


def reviewed_scope(root: Path, task: Any, run: Any, evidence: str) -> dict[str, Any]:
    proof_path = contained(root / "inputs", evidence.removeprefix("inputs/"))
    proof = read_json(proof_path)
    if proof.get("kind") == "ops_native_rocketmq_sample_admission":
        return reviewed_native_rocketmq(root, task, run, proof_path, proof)
    if proof.get("kind") == "ops_native_nas_query_sample_admission":
        return reviewed_native_nas_query(root, task, run, proof_path, proof)
    if proof.get("kind") == "ops_hourly_managed_tf_sample_admission":
        return reviewed_hourly_managed_tf(root, task, run, proof_path, proof)
    if proof.get("kind") == "ops_additional_empty_general_nas_admission":
        from .additional_nas import reviewed_additional_nas

        return reviewed_additional_nas(root, task, run, proof_path, proof)
    if proof.get("kind") == "ops_additional_empty_table_admission":
        return reviewed_additional_empty_table(root, task, run, proof_path, proof)
    if proof.get("kind") == "ops_empty_general_nas_admission":
        return reviewed_empty_general_nas(root, task, run, proof_path, proof)
    human_path = contained(root / "inputs", str(proof.get("human_authorization", "")).removeprefix("inputs/"))
    human = read_json(human_path)
    proposal_path = contained(root / "inputs", str(proof.get("proposal", "")).removeprefix("inputs/"))
    packet = read_json(proposal_path)
    spec = json.loads(task["spec"])
    required = {"schema_version": 1, "kind": "ops_temporary_sample_review", "approved": True,
                "task_id": task["id"], "plan_id": task["run_id"],
                "frozen_plan_sha256": hashlib.sha256(run["plan"].encode()).hexdigest()}
    if any(proof.get(key) != value for key, value in required.items()) or (
        spec.get("retest_scope", {}).get("kind") != "failed_navigation_managed"
        or packet.get("task_id") != task["id"] or proof.get("proposal_sha256") != digest(proposal_path)
        or proof.get("human_authorization_sha256") != digest(human_path)
        or human.get("kind") != "human_fee_authorization" or human.get("approved") is not True
        or human.get("task_id") != task["id"] or human.get("plan_id") != task["run_id"]
        or not human.get("verbatim_user_message")
        or type(packet.get("budget_cents")) is not int or not 0 < packet["budget_cents"] <= run["budget"]
        or type(human.get("max_budget_cents")) is not int or packet["budget_cents"] > human["max_budget_cents"]
    ):
        raise WorkflowError("exact frozen task/proposal and original human temporary-source permission required")
    scope = human.get("scope", {})
    flags = ("existing_verified_accessible_parent_only", "delete_created_tables_after_test", "no_data_rows",
             "no_indexes", "no_new_instance", "no_existing_network_or_iam_changes")
    if any(scope.get(flag) is not True for flag in flags) or (
        scope.get("native_ros_empty_tables_max") != 1 or scope.get("ros_managed_terraform_empty_tables_max") != 1
    ):
        raise WorkflowError("temporary sample permission must be exactly two empty existing-parent OTS tables")
    grants = packet.get("execution_grants", {})
    if set(grants) != {"ROS", "TF"} or proof.get("execution_sha256") != execution_digest(packet):
        raise WorkflowError("review must bind exactly the original ROS and managed TF execution")
    validate_execution(packet, {"ROS", "TF"}, require_read_policy=True)
    bindings = proof.get("sample_bindings", {})
    if set(bindings) != set(grants) or spec.get("key") != "ALIYUN::OTS::Table":
        raise WorkflowError("temporary sample exception is limited to OTS Table")
    for dimension, entries in grants.items():
        writes = [entry for entry in entries if entry["kind"] in {"create", "delete"}]
        if len(writes) != 2 or {entry["kind"] for entry in writes} != {"create", "delete"} or any(
            entry["quota"] != 1 or entry["call"]["product"] != "ros"
            or entry["call"]["action"] != {"create": "CreateStack", "delete": "DeleteStack"}[entry["kind"]]
            for entry in writes
        ):
            raise WorkflowError("one response-bound ROS stack create/delete per source, no other writes")
        create = next(entry for entry in writes if entry["kind"] == "create")
        template = create["call"]["params"].get("TemplateBody")
        binding = bindings[dimension]
        if not isinstance(template, str) or binding.get("template_sha256") != hashlib.sha256(
            template.encode()
        ).hexdigest() or any(not binding.get(key) for key in ("instance_name", "table_name", "region")):
            raise WorkflowError("independently reviewed complete inline template and exact existing parent required")
        if binding["region"] != create["call"]["region_id"] or binding.get("empty_table_only") is not True:
            raise WorkflowError("temporary sample must remain an empty table in its reviewed domestic region")
        parent = binding["instance_name"]
        if dimension == "ROS":
            value = json.loads(template)
            resources = value.get("Resources", {})
            if len(resources) != 1:
                raise WorkflowError("native sample cannot create dependencies")
            child = next(iter(resources.values()))
            props = child.get("Properties", {})
            if child.get("Type") != "ALIYUN::OTS::Table" or props.get("InstanceName") != parent or (
                props.get("TableName") != binding["table_name"]
                or props.get("ReservedThroughput") != {"Read": 0, "Write": 0}
            ):
                raise WorkflowError("native empty table must bind the reviewed literal parent and zero reserved CU")
        else:
            value = json.loads(template)
            if value.get("Transform") != "Aliyun::Terraform-v1.5" or set(value.get("Workspace", {})) != {"main.tf"}:
                raise WorkflowError("Terraform source must be ROS hosted and fully inline")
            # OPS reviews all HCL semantics against pinned official schema; its
            # exact bytes/grants are frozen here, not interpreted or expanded by models.
            hcl = value["Workspace"]["main.tf"]
            if re.findall(r'\bresource\s+"([^"]+)"', hcl) != ["alicloud_ots_table"] or re.search(
                r'\b(module|data|provisioner|count|for_each|dynamic)\b', hcl
            ) or f'instance_name = "{parent}"' not in hcl or f'table_name = "{binding["table_name"]}"' not in hcl:
                raise WorkflowError("managed sample permits one literal OTS table, no dependency or dynamic execution")
    if bindings["ROS"]["instance_name"] != bindings["TF"]["instance_name"] or (
        bindings["ROS"]["table_name"] == bindings["TF"]["table_name"]
    ):
        raise WorkflowError("two distinct table names must use the same verified existing parent")
    return {**required, "evidence": proof_path.relative_to(root).as_posix(), "evidence_sha256": digest(proof_path),
            "execution_sha256": execution_digest(packet), "execution_grants": grants,
            "budget_cents": packet["budget_cents"],
            "accounts": sorted({item["account"] for item in packet["scenes"].values()}),
            "sample_bindings": bindings, "operation_authorization_required": True}


def reviewed_native_rocketmq(root: Path, task: Any, run: Any, path: Path, proof: dict[str, Any]) -> dict[str, Any]:
    """One initially reviewed empty standard native ROS source; QUERY stays read-only."""
    from .hourly_budget import _input, policy
    from .plans import expected_scenes

    proposal_path, packet = _input(root, proof.get("proposal"))
    _, human = _input(root, proof.get("human_authorization"))
    _, source = _input(root, proof.get("source_review"))
    _, cost = _input(root, proof.get("cost_review"))
    _, operation = _input(root, proof.get("operation_review"))
    spec = json.loads(task["spec"])
    scenes = {s["id"] for s in expected_scenes(spec)}
    grants = packet.get("execution_grants", {})
    accounts = sorted({s.get("account") for s in packet.get("scenes", {}).values()})
    required = {"schema_version": 1, "kind": "ops_native_rocketmq_sample_admission", "approved": True,
                "task_id": task["id"], "plan_id": task["run_id"],
                "frozen_plan_sha256": hashlib.sha256(run["plan"].encode()).hexdigest()}
    if (any(proof.get(k) != v for k, v in required.items())
            or spec.get("key") != "ALIYUN::ROCKETMQ::Instance"
            or spec.get("retest_scope", {}).get("kind") != "failed_navigation_managed"
            or not scenes or not any("-ROS-" in s for s in scenes)
            or any(not ("-ROS-" in s or "-QUERY-" in s) for s in scenes)
            or set(packet.get("scenes", {})) != scenes or set(grants) not in ({"ROS"}, {"ROS", "QUERY"})
            or packet.get("task_id") != task["id"] or packet.get("profile") != "resource-link-test"
            or packet.get("terraform") != "ros_managed_terraform"
            or packet.get("budget_cents") != 0 or type(packet.get("budget_cents")) is not int
            or "hourly_cost" in packet or len(accounts) != 1
            or re.fullmatch(r"[0-9]{16}", str(accounts[0])) is None
            or proof.get("execution_sha256") != execution_digest(packet)
            or operation.get("approved") is not True or operation.get("task_id") != task["id"]
            or operation.get("proposal_sha256") != digest(proposal_path)
            or any(packet.get(k) for k in ("additional_source_request", "additional_source_requests",
                                          "execution_selection", "carry_forward_dimensions"))
            or not isinstance(packet.get("cleanup_requests", {}).get("ROS"), str)
            or not packet.get("cleanup_recipe")):
        raise WorkflowError("native RocketMQ source needs the exact initial frozen ROS/readonly QUERY packet")
    scope = human.get("scope", {})
    if (human.get("kind") != "human_temporary_source_authorization" or human.get("approved") is not True
            or human.get("task_id") != task["id"] or human.get("plan_id") != task["run_id"]
            or not human.get("verbatim_user_message")
            or scope.get("native_ros_empty_standard_rocketmq_instance_max") != 1
            or any(scope.get(k) is not True for k in (
                "no_topics", "no_groups", "no_messages", "no_existing_business_resource_mutation",
                "delete_own_stack_and_instance_after_test", "operation_review_required"))):
        raise WorkflowError("original human permission for one empty native standard instance required")
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if policy(root, db, task["run_id"]) is None:
            raise WorkflowError("original same-plan fee permission required; zero is not a generic free assertion")
    validate_execution(packet, set(grants), require_read_policy=True,
                       readonly_dimensions={"QUERY"} if "QUERY" in grants else set())
    writes = [g for g in grants["ROS"] if g["kind"] in {"create", "delete"}]
    if (len(writes) != 2 or {g["kind"] for g in writes} != {"create", "delete"}
            or any(g["quota"] != 1 or (g["call"]["product"], g["call"]["version"], g["call"]["action"])
                   != ("ros", "2019-09-10", "CreateStack" if g["kind"] == "create" else "DeleteStack")
                   for g in writes)):
        raise WorkflowError("one native stack create and response-bound delete, no other writes")
    create = next(g for g in writes if g["kind"] == "create")
    delete = next(g for g in writes if g["kind"] == "delete")
    params = create["call"]["params"]
    body = params.get("TemplateBody")
    if not isinstance(body, str):
        raise WorkflowError("complete inline native template required")
    template = json.loads(body)
    resources = template.get("Resources", {})
    if (set(template) - {"ROSTemplateFormatVersion", "Resources", "Outputs", "Parameters"}
            or template.get("ROSTemplateFormatVersion") != "2015-09-01"
            or template.get("Parameters", {}) != {} or len(resources) != 1):
        raise WorkflowError("one static native instance, no transforms or dependencies")
    logical, child = next(iter(resources.items()))
    props = child.get("Properties", {})
    name, region = props.get("InstanceName"), create["call"]["region_id"]
    if (set(child) != {"Type", "Properties"} or child["Type"] != spec["key"]
            or set(props) - {"InstanceName", "Remark", "DeletionForce"}
            or props.get("DeletionForce") is not False or not isinstance(name, str)
            or not re.fullmatch(r"iac-link-[A-Za-z0-9_-]{3,54}", name)
            or not isinstance(props.get("Remark", ""), str) or len(props.get("Remark", "")) > 128
            or params.get("StackName") != name or params.get("RegionId") != region
            or not re.fullmatch(r"cn-[a-z0-9-]+", region)
            or params.get("DisableRollback") is not False
            or set(params) != {"RegionId", "StackName", "TemplateBody", "TimeoutInMinutes", "DisableRollback"}
            or delete.get("parent") != create["id"] or delete.get("response_path") != ["StackId"]
            or delete.get("target_param") != "StackId"
            or delete["call"]["params"] != {"RegionId": region, "StackId": "$created_id", "RetainAllResources": False}):
        raise WorkflowError("native source must be own empty standard instance with no forced/business deletion")
    for output in template.get("Outputs", {}).values():
        if (not isinstance(output, dict) or set(output) != {"Value"}
                or output["Value"] not in [{"Fn::GetAtt": [logical, a]}
                                          for a in ("InstanceId", "InstanceName", "InstanceType")]):
            raise WorkflowError("native outputs may only project this own instance identity")
    for dimension, entries in grants.items():
        if len(entries) != (9 if dimension == "ROS" else 2):
            raise WorkflowError("one identity and the complete bounded source/cleanup read graph only")
        for entry in entries:
            call = entry["call"]
            if call["region_id"] != region:
                raise WorkflowError("one domestic region for source and readonly query")
            if entry["kind"] in {"create", "delete", "identity"}:
                if entry["quota"] != 1:
                    raise WorkflowError("native identity and writes are single use")
                if dimension == "QUERY" and entry["kind"] != "identity":
                    raise WorkflowError("business QUERY never gains writes")
                continue
            if call["product"] == "ons":
                from scripts.web.e2e.resource_link_workflow.execution_gate import is_ons_instance_list_read
                if not is_ons_instance_list_read(call) or entry["quota"] != 1 or entry.get("parent"):
                    raise WorkflowError("only complete single regional ONS inventory reads")
                continue
            if (dimension != "ROS" or call["product"] != "ros" or call["version"] != "2019-09-10"
                    or call["action"] not in {"GetStack", "ListStackResources", "GetStackResource"}
                    or entry.get("parent") != create["id"] or entry.get("response_path") != ["StackId"]
                    or entry.get("target_param") != "StackId" or call["params"].get("StackId") != "$created_id"
                    or call["params"].get("RegionId") != region
                    or (call["action"] == "GetStackResource" and call["params"].get("LogicalResourceId") != logical)
                    or entry["quota"] > (61 if call["action"] == "GetStack" else 1)):
                raise WorkflowError("native metadata reads must remain bound to its own single original stack")
    required_cleanup = {g["call"]["action"] for g in grants["ROS"]
                        if g.get("read_policy", {}).get("required_cleanup") is True}
    if not {"GetStack", "OnsInstanceInServiceList"} <= required_cleanup:
        raise WorkflowError("both original stack terminal and complete own-instance absence evidence required")
    binding = {"template_sha256": hashlib.sha256(body.encode()).hexdigest(), "region": region,
               "stack_name": name, "instance_name": name, "logical_resource_id": logical,
               "resource_type": spec["key"]}
    expected = {"kind": "ops_native_rocketmq_source_review", "approved": True, "task_id": task["id"],
                "account": accounts[0], "execution_sha256": execution_digest(packet), "sample_binding": binding,
                "full_inline_template_reviewed": True, "implicit_dependencies_reviewed": True,
                "no_existing_resource_mutation": True, "all_new_resources_response_bound_cleanup": True}
    expected_cost = {"kind": "ops_empty_native_rocketmq_cost_review", "approved": True,
                     "task_id": task["id"], "account": accounts[0], "currency": "CNY", "budget_cents": 0,
                     "execution_sha256": execution_digest(packet), "estimated_incremental_hourly_cents": 0,
                     "includes_all_dependencies": True, "includes_request_and_network_costs": True}
    if (any(source.get(k) != v for k, v in expected.items())
            or any(cost.get(k) != v for k, v in expected_cost.items())):
        raise WorkflowError("independent source and expected-zero cost reviews must bind the complete execution")
    for review in (source, cost):
        if not review.get("estimate_basis") or not review.get("limitations") or not review.get("official_sources"):
            raise WorkflowError("original official source/cost evidence and limitations required")
        for reference in review["official_sources"]:
            _input(root, reference)
    return {**required, "evidence": path.relative_to(root).as_posix(), "evidence_sha256": digest(path),
            "execution_sha256": execution_digest(packet), "execution_grants": {"ROS": grants["ROS"]},
            "budget_cents": 0, "accounts": accounts, "sample_bindings": {"ROS": binding},
            "operation_authorization_required": True}


def native_rocketmq_followup_authority(
    root: Path, connection: Any, task: Any, packet: dict[str, Any], saved: dict[str, Any],
    review: dict[str, Any] | None,
) -> dict[str, str]:
    """Bind an explicit reviewed follow-up, never override it with the old sample allowance."""
    from .additional_source import validate_additional_source, validate_paid_operation_review
    from .hourly_budget import _input, policy

    admitting = review is not None
    approval = None
    if review is None:
        path = authorization_path(root, packet)
        if not path.is_file():
            raise WorkflowError("native RocketMQ follow-up requires its own explicit OPS review")
        validate_authorization(root, task["resource_id"], packet)
        approval = read_json(path)
        review = read_json(contained(root, approval["authorization_evidence"]))
    original_review = read_json(contained(root, saved["evidence"]))
    _, original = _input(root, original_review["proposal"])
    requests = packet.get("additional_source_requests", [])
    native = review.get("native_rocketmq_followup", {})
    expected = {"kind": "ops_native_rocketmq_followup_review", "approved": True,
                "task_id": task["id"], "execution_sha256": execution_digest(packet),
                "proposal_sha256": review.get("proposal_sha256"),
                "original_admission": {"path": saved["evidence"], "sha256": saved["evidence_sha256"]},
                "within_existing_task_and_hourly_scope": True, "not_unlimited_rebuild": True,
                "creation_after_failed_capture_and_verified_cleanup": True}
    if (not isinstance(native, dict) or any(native.get(k) != v for k, v in expected.items())
            or review.get("kind") != "ops_additional_sources_review" or review.get("approved") is not True
            or review.get("additional_sources_approved") != requests
            or set(packet["execution_grants"]) not in ({"ROS"}, {"ROS", "QUERY"})
            or {r.get("dimension") for r in requests} != set(packet["execution_grants"])
            or len(requests) != len(packet["execution_grants"])
            or policy(root, connection, task["run_id"]) is None
            or any(packet["execution_grants"][dim] != original["execution_grants"][dim]
                   for dim in packet["execution_grants"])
            or any(r.get("predecessor_proposal") != original_review["proposal"]["path"]
                   or r.get("predecessor_sha256") != original_review["proposal"]["sha256"] for r in requests)):
        raise WorkflowError("native RocketMQ follow-up must preserve original calls and independently reviewed scope")
    proposal_path, proposed = _input(root, native.get("proposal"))
    if proposed != packet or digest(proposal_path) != native["proposal_sha256"]:
        raise WorkflowError("native RocketMQ follow-up must bind the full unchanged final proposal")
    receipt_ref = native.get("predecessor_cleanup_receipt", {})
    if (not isinstance(receipt_ref, dict) or set(receipt_ref) != {"path", "sha256"}
            or not isinstance(receipt_ref["path"], str) or not receipt_ref["path"].startswith("receipts/")):
        raise WorkflowError("native RocketMQ follow-up requires the original program receipt, not an input copy")
    receipt_path = contained(root, receipt_ref["path"])
    if digest(receipt_path) != receipt_ref["sha256"]:
        raise WorkflowError("native RocketMQ predecessor receipt changed")
    receipt = read_json(receipt_path)
    accepted = [json.loads(row[0]) for row in connection.execute(
        "SELECT data FROM events WHERE kind='acceptance_committed'")]
    event = next((e for e in accepted if e.get("task_id") == task["id"]
                  and e.get("receipt_path") == receipt_path.relative_to(root).as_posix()
                  and e.get("receipt_sha256") == digest(receipt_path)), None)
    sol = connection.execute("SELECT role,purpose,state,thread_id,turn_id FROM jobs WHERE id=?",
                             (event.get("job_id"),)).fetchone() if event else None
    cleanup = receipt.get("cleanup", [])
    if (receipt.get("task_id") != task["id"] or not sol or tuple(sol[:3]) != ("sol", "review", "complete")
            or sol[3] != receipt.get("sol_thread") or sol[4] != receipt.get("sol_turn")
            or len(cleanup) != 2 or len({c.get("ownership_id") for c in cleanup}) != 2
            or not any(d.get("scene") == task["resource_id"] + "-ROS-1-open" and d.get("status") == "fail"
                       for d in receipt.get("decisions", []))):
        raise WorkflowError("native RocketMQ follow-up requires original independent failed-click/cleanup acceptance")
    resources = []
    for item in cleanup:
        row = connection.execute("SELECT task_id,dimension,state,resource,evidence FROM owned WHERE id=?",
                                 (item.get("ownership_id"),)).fetchone()
        if (item.get("confirmed_absent") is not True or not row
                or tuple(row[:3]) != (task["id"], "ROS", "deleted")):
            raise WorkflowError("native RocketMQ follow-up cannot abandon original ownership")
        proof = read_json(contained(root, item["evidence"]))
        if (json.loads(row[4]) != {"path": item["evidence"], "sha256": digest(contained(root, item["evidence"]))}
                or proof.get("ownership_id") != item["ownership_id"] or proof.get("resource") != json.loads(row[3])
                or proof.get("absent") is not True or proof.get("failed") is not False
                or digest(contained(root, proof["source_evidence"])) != proof.get("source_sha256")):
            raise WorkflowError("native RocketMQ original cleanup proof changed")
        resources.append(json.loads(row[3]))
    ros_scope = next(r["prior_scope"] for r in requests if r["dimension"] == "ROS")
    with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as wire:
        rows = wire.execute("SELECT a.response FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                            "WHERE a.scope=? AND a.state='succeeded' AND g.kind='create'", (ros_scope,)).fetchall()
    stack_ids = {json.loads(row[0]).get("StackId") for row in rows}
    if (len(stack_ids) != 1 or not next(iter(stack_ids))
            or sum(r.get("action") == "CreateStack" and r.get("id") in stack_ids for r in resources) != 1
            or sum(r.get("resource_type") == "ALIYUN::ROCKETMQ::Instance" and r.get("stack_id") in stack_ids
                   for r in resources) != 1):
        raise WorkflowError("native RocketMQ cleanup receipt must belong to this exact predecessor root/member")
    _, source = _input(root, native.get("source_review"))
    _, cost = _input(root, native.get("cost_review"))
    account = saved["accounts"][0]
    common = {"approved": True, "task_id": task["id"], "account": account,
              "execution_sha256": execution_digest(packet)}
    source_fields = {**common, "kind": "ops_native_rocketmq_source_review",
                     "sample_binding": saved["sample_bindings"]["ROS"],
                     "full_inline_template_reviewed": True, "implicit_dependencies_reviewed": True,
                     "no_existing_resource_mutation": True, "all_new_resources_response_bound_cleanup": True}
    cost_fields = {**common, "kind": "ops_empty_native_rocketmq_cost_review", "currency": "CNY",
                   "budget_cents": 0, "estimated_incremental_hourly_cents": 0,
                   "includes_all_dependencies": True, "includes_request_and_network_costs": True}
    if any(source.get(k) != v for k, v in source_fields.items()) or any(
        cost.get(k) != v for k, v in cost_fields.items()
    ):
        raise WorkflowError("native RocketMQ follow-up requires current exact source and zero-cost reviews")
    for evidence in (source, cost):
        if (not evidence.get("estimate_basis") or not evidence.get("limitations")
                or not evidence.get("official_sources")):
            raise WorkflowError("native RocketMQ follow-up requires actual official fee/dependency evidence")
        for ref in evidence["official_sources"]:
            _input(root, ref)
    # New admission rechecks consumed old writes and verified original cleanup.
    # Reading an already approved generation must NOT require its own current
    # owners to be absent; those are precisely what the upcoming review cleans.
    if admitting:
        validate_additional_source(root, packet, review)
    else:
        validate_paid_operation_review(root, packet, review)
    request = next(r for r in requests if r["dimension"] == "ROS")
    authority = hashlib.sha256(canonical({"task_id": task["id"], "operation": request}).encode()).hexdigest() + ":ROS"
    if approval and approval.get("dimension_authorities", {}).get("ROS") != authority:
        raise WorkflowError("native RocketMQ follow-up cannot inherit the exhausted original authority")
    return {"ROS": authority}


def reviewed_hourly_managed_tf(root: Path, task: Any, run: Any, path: Path, proof: dict[str, Any]) -> dict[str, Any]:
    """First hosted TF source under registered hourly permission, not a refill.

    OPS reviews complete literal HCL and implicit dependencies; the program
    freezes those bytes, exact capabilities, itemized price and target scenes.
    Separate operation authorization and signed wire/PNG acceptance still apply.
    """
    from .hourly_budget import _input, operation_review
    from .plans import expected_scenes

    required = {"schema_version": 1, "kind": "ops_hourly_managed_tf_sample_admission", "approved": True,
                "task_id": task["id"], "plan_id": task["run_id"],
                "frozen_plan_sha256": hashlib.sha256(run["plan"].encode()).hexdigest()}
    _, packet = _input(root, proof.get("proposal"))
    _, source = _input(root, proof.get("source_review"))
    _, operation = _input(root, proof.get("operation_review"))
    spec = json.loads(task["spec"])
    scenes = {item["id"] for item in expected_scenes(spec)}
    grants = packet.get("execution_grants", {})
    accounts = sorted({item.get("account") for item in packet.get("scenes", {}).values()})
    if (any(proof.get(k) != v for k, v in required.items())
            or spec.get("retest_scope", {}).get("kind") != "failed_navigation_managed"
            or not scenes or any("-TF-" not in scene for scene in scenes)
            or set(packet.get("scenes", {})) != scenes or set(grants) != {"TF"}
            or packet.get("task_id") != task["id"] or packet.get("profile") != "resource-link-test"
            or packet.get("terraform") != "ros_managed_terraform"
            or type(packet.get("budget_cents")) is not int or packet["budget_cents"] != 0
            or len(accounts) != 1 or re.fullmatch(r"[0-9]{16}", str(accounts[0])) is None
            or proof.get("execution_sha256") != execution_digest(packet)
            or operation.get("proposal_sha256") != proof["proposal"]["sha256"]
            or packet.get("additional_source_request") or packet.get("additional_source_requests")
            or packet.get("execution_selection") or packet.get("carry_forward_dimensions")
            or not isinstance(packet.get("requests", {}).get("TF"), str)
            or not isinstance(packet.get("cleanup_requests", {}).get("TF"), str)
            or not packet.get("cleanup_recipe")):
        raise WorkflowError("hourly temporary source needs the exact initial frozen TF packet and scenes")
    validate_execution(packet, {"TF"}, require_read_policy=True)
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if operation_review(root, connection, task, packet, operation) is None:
            raise WorkflowError("hourly temporary source needs independently reviewed itemized costs")
    writes = [entry for entry in grants["TF"] if entry["kind"] in {"create", "delete"}]
    if len(writes) != 2 or {entry["kind"] for entry in writes} != {"create", "delete"} or any(
        entry["quota"] != 1 or entry["call"]["product"] != "ros"
        or entry["call"].get("version") != "2019-09-10"
        or entry["call"]["action"] != {"create": "CreateStack", "delete": "DeleteStack"}[entry["kind"]]
        for entry in writes
    ):
        raise WorkflowError("hourly TF source permits one original hosted stack create/delete only")
    create = next(entry for entry in writes if entry["kind"] == "create")
    delete = next(entry for entry in writes if entry["kind"] == "delete")
    body = create["call"]["params"].get("TemplateBody")
    if (not isinstance(body, str) or delete.get("parent") != create["id"]
            or delete.get("response_path") != ["StackId"] or delete.get("target_param") != "StackId"
            or delete["call"]["params"].get("StackId") != "$created_id"
            or delete["call"]["params"].get("RetainAllResources") is not False):
        raise WorkflowError("hourly TF source needs full inline template and response-bound complete cleanup")
    template = json.loads(body)
    if (set(template) != {"ROSTemplateFormatVersion", "Transform", "Workspace"}
            or template["ROSTemplateFormatVersion"] != "2015-09-01"
            or template["Transform"] != "Aliyun::Terraform-v1.5"
            or not isinstance(template["Workspace"], dict) or set(template["Workspace"]) != {"main.tf"}
            or not isinstance(template["Workspace"]["main.tf"], str)):
        raise WorkflowError("temporary Terraform must run in ROS with one fully reviewed main.tf")
    hcl = template["Workspace"]["main.tf"]
    resources = re.findall(r'\bresource\s+"([^"]+)"\s+"([^"]+)"', hcl)
    if (not resources or len(set(resources)) != len(resources)
            or re.search(r'\b(module|data|provisioner|count|for_each|dynamic|variable|backend)\b', hcl)
            or len(re.findall(r'\bprovider\s+"alicloud"', hcl)) != 1
            or not re.search(r'version\s*=\s*"=\s*[0-9]+\.[0-9]+\.[0-9]+"', hcl)):
        raise WorkflowError("hourly TF source needs pinned static reviewed resources without dynamic execution")
    binding = {"template_sha256": hashlib.sha256(body.encode()).hexdigest(),
               "region": create["call"]["region_id"], "stack_name": create["call"]["params"].get("StackName"),
               "resources": [{"type": kind, "name": name} for kind, name in resources]}
    expected_source = {"kind": "ops_hourly_managed_tf_source_review", "approved": True,
                       "task_id": task["id"], "account": accounts[0], "execution_sha256": execution_digest(packet),
                       "sample_binding": binding, "full_inline_template_reviewed": True,
                       "implicit_dependencies_reviewed": True, "no_existing_resource_mutation": True,
                       "all_new_resources_response_bound_cleanup": True}
    if (any(source.get(k) != v for k, v in expected_source.items()) or not binding["stack_name"]
            or not re.fullmatch(r"cn-[a-z0-9-]+", binding["region"])
            or not source.get("estimate_basis") or not source.get("limitations")
            or not isinstance(source.get("official_sources"), list) or not source["official_sources"]):
        raise WorkflowError("independent source review must bind the entire literal hosted stack and dependencies")
    for reference in source["official_sources"]:
        _input(root, reference)
    return {**required, "evidence": path.relative_to(root).as_posix(), "evidence_sha256": digest(path),
            "execution_sha256": execution_digest(packet), "execution_grants": grants,
            "budget_cents": packet["budget_cents"], "accounts": accounts,
            "sample_bindings": {"TF": binding}, "hourly_cost": packet["hourly_cost"],
            "operation_authorization_required": True}


def reviewed_native_nas_query(root: Path, task: Any, run: Any, path: Path, proof: dict[str, Any]) -> dict[str, Any]:
    """One initial own DataFlow sample, not CREATE/ROS/TF credit or a quota refill."""
    from .hourly_budget import _input, operation_review
    from .plans import expected_scenes

    required = {"schema_version": 1, "kind": "ops_native_nas_query_sample_admission", "approved": True,
                "task_id": task["id"], "plan_id": task["run_id"],
                "frozen_plan_sha256": hashlib.sha256(run["plan"].encode()).hexdigest()}
    proposal_path, packet = _input(root, proof.get("proposal"))
    _, source = _input(root, proof.get("source_review"))
    _, operation = _input(root, proof.get("operation_review"))
    spec = json.loads(task["spec"])
    scenes = {item["id"] for item in expected_scenes(spec)}
    grants = packet.get("execution_grants", {})
    accounts = sorted({str(item.get("account")) for item in packet.get("scenes", {}).values()})
    if (any(proof.get(k) != v for k, v in required.items())
            or spec.get("key") != "ALIYUN::NAS::DataFlow"
            or spec.get("retest_scope", {}).get("kind") != "failed_navigation_managed"
            or scenes != {task["resource_id"] + "-QUERY-1-iframe"}
            or set(packet.get("scenes", {})) != scenes or set(grants) != {"QUERY"}
            or packet.get("task_id") != task["id"] or packet.get("profile") != "resource-link-test"
            or packet.get("terraform") != "ros_managed_terraform"
            or type(packet.get("budget_cents")) is not int or packet["budget_cents"] != 0
            or len(accounts) != 1 or re.fullmatch(r"[0-9]{16}", accounts[0]) is None
            or proof.get("execution_sha256") != execution_digest(packet)
            or operation.get("proposal_sha256") != digest(proposal_path)
            or any(packet.get(key) for key in ("additional_source_request", "additional_source_requests",
                                               "execution_selection", "carry_forward_dimensions"))
            or not isinstance(packet.get("requests", {}).get("QUERY"), str)
            or not packet["requests"]["QUERY"].strip()
            or any(item.get("region") != "cn-hangzhou" for item in packet["scenes"].values())):
        raise WorkflowError("native NAS sample needs the exact initial frozen QUERY iframe packet")
    validate_execution(packet, {"QUERY"}, require_read_policy=True, native_nas_query_sample=True)
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if operation_review(root, connection, task, packet, operation) is None:
            raise WorkflowError("native NAS source needs independently reviewed itemized hourly costs")
    name = next(entry["call"]["params"]["bucket"] for entry in grants["QUERY"] if entry["id"] == "create-bucket")
    binding = {"name": name, "region": "cn-hangzhou", "execution_grants_sha256": hashlib.sha256(
        canonical(grants).encode()).hexdigest()}
    expected = {"kind": "ops_native_nas_query_source_review", "approved": True, "task_id": task["id"],
                "account": accounts[0], "execution_sha256": execution_digest(packet), "sample_binding": binding,
                "full_native_graph_reviewed": True, "implicit_dependencies_reviewed": True,
                "no_existing_resource_mutation": True, "all_new_resources_response_bound_cleanup": True,
                "no_object_io": True, "no_posix_client": True}
    if (any(source.get(k) != v for k, v in expected.items()) or not source.get("estimate_basis")
            or not source.get("limitations") or not isinstance(source.get("official_sources"), list)
            or not source["official_sources"]):
        raise WorkflowError("native NAS source review must bind the complete own dependency graph and cleanup")
    for reference in source["official_sources"]:
        _input(root, reference)
    return {**required, "evidence": path.relative_to(root).as_posix(), "evidence_sha256": digest(path),
            "proposal": proposal_path.relative_to(root).as_posix(), "proposal_sha256": digest(proposal_path),
            "execution_sha256": execution_digest(packet), "execution_grants": grants,
            "budget_cents": packet["budget_cents"], "accounts": accounts, "sample_bindings": {"QUERY": binding},
            "hourly_cost": packet["hourly_cost"], "operation_authorization_required": True}


def nas_task_lifetime_authorization(human: dict[str, Any]) -> bool:
    """Human delegation removes repeat questions, not per-operation OPS checks."""
    scope = human.get("scope", {})
    if scope.get("authorization_lifetime") != "task":
        return False
    if (
        not human.get("verbatim_user_message")
        or scope.get("ops_reviewed_rebuilds_until_remaining_scenes_complete") is not True
        or scope.get("sample_limit_basis") != "per_generation"
        or scope.get("cleanup_before_next_generation") is not True
        or scope.get("no_new_cost_allowance") is not True
    ):
        raise WorkflowError("task-lifetime NAS permission needs explicit same-scope human delegation")
    return True


def reviewed_empty_general_nas(
    root: Path, task: Any, run: Any, path: Path, proof: dict[str, Any],
) -> dict[str, Any]:
    """One human-approved empty standard filesystem + mount in one hosted stack.

    This admits a frozen source exception, not a cloud operation. Exact operations
    still need independent OPS authorization and the gate's signed child binding.
    Reusing the ordinary immutable task key prevents attempt/name based refills.
    """
    required = {"schema_version": 1, "kind": "ops_empty_general_nas_admission", "approved": True,
                "task_id": task["id"], "plan_id": task["run_id"],
                "frozen_plan_sha256": hashlib.sha256(run["plan"].encode()).hexdigest()}

    def material(reference: Any) -> dict[str, Any]:
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise WorkflowError("NAS admission needs exact immutable inputs references")
        source = contained(root / "inputs", str(reference["path"]).removeprefix("inputs/"))
        if digest(source) != reference["sha256"]:
            raise WorkflowError("NAS admission source changed")
        return read_json(source)

    human = material(proof.get("human_authorization"))
    packet = material(proof.get("proposal"))
    pricing = material(proof.get("pricing_review"))
    network = material(proof.get("network_review"))
    spec = json.loads(task["spec"])
    human_scope = human.get("scope", {})
    task_lifetime = nas_task_lifetime_authorization(human)
    flags = ("reuse_verified_existing_vpc_vswitch_restricted_access_group_only",
             "no_existing_filesystem_access_changes", "no_data_mount_read_write",
             "no_compute_disk_eip_extreme_nas_or_addons", "no_iam_writes",
             "delete_all_new_response_bound_objects_after_test")
    if (
        any(proof.get(key) != value for key, value in required.items())
        or spec.get("key") != "ALIYUN::NAS::MountTarget"
        or spec.get("retest_scope", {}).get("kind") != "failed_navigation_managed"
        or packet.get("task_id") != task["id"] or packet.get("profile") != "resource-link-test"
        or packet.get("terraform") != "ros_managed_terraform"
        or type(packet.get("budget_cents")) is not int or packet["budget_cents"] != 0
        or not re.fullmatch(r"[0-9]{16}", str(proof.get("account", "")))
        or sorted({item.get("account") for item in packet.get("scenes", {}).values()}) != [proof.get("account")]
        or proof.get("execution_sha256") != execution_digest(packet)
        or set(packet.get("execution_grants", {})) != {"TF"}
        or human.get("kind") != "human_temporary_source_authorization" or human.get("approved") is not True
        or human.get("task_id") != task["id"] or human.get("plan_id") != task["run_id"]
        or (not task_lifetime and (
            not human.get("human_question_verbatim") or not human.get("human_reply_verbatim")
        ))
        or human_scope.get("source_dimension") != "TF"
        or human_scope.get("execution") != "ros_managed_terraform"
        or any(human_scope.get(flag) is not True for flag in flags)
        or any(human_scope.get(key) != 1 for key in (
            "new_empty_general_purpose_filesystems_max", "new_mount_targets_max", "new_managed_stacks_max"))
    ):
        raise WorkflowError("NAS source exception requires exact frozen task and one human-approved empty stack")
    for review in (pricing, network):
        if review.get("task_id") != task["id"] or review.get("account") != proof.get("account"):
            raise WorkflowError("NAS reviews must bind their own task and signed account")
    if (
        pricing.get("kind") != "ops_nas_empty_general_operation_pricing_review"
        or pricing.get("approved") is not True or type(pricing.get("expected_cost_cents")) is not int
        or pricing["expected_cost_cents"] != 0
        or pricing.get("execution_sha256") != execution_digest(packet)
        or pricing.get("human_authorization") != proof["human_authorization"]
        or pricing.get("no_file_data_operations") is not True
        or pricing.get("cleanup_required") is not True
        or not pricing.get("estimate_basis") or not pricing.get("limitations")
        or not isinstance(pricing.get("official_sources"), list) or not pricing["official_sources"]
        or network.get("kind") != "ops_nas_restricted_existing_network_source"
        or not isinstance(network.get("signed_sources"), list) or not network["signed_sources"]
    ):
        raise WorkflowError("NAS needs independently reviewed exact zero-cost and existing restricted network sources")
    for reference in [*network["signed_sources"], *pricing["official_sources"]]:
        source = contained(root / "inputs", str(reference["path"]).removeprefix("inputs/"))
        if digest(source) != reference["sha256"]:
            raise WorkflowError("NAS raw official or signed evidence changed")
    binding = proof.get("sample_binding", {})
    region = binding.get("region", "")
    literals = ("zone_id", "description", "access_group_name", "vpc_id", "vswitch_id", "stack_name")
    if (
        not re.fullmatch(r"cn-[a-z]+", region)
        or any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(binding.get(key, ""))) for key in literals)
        or binding.get("empty_general_only") is not True
        or network.get("region") != region or network.get("vpc", {}).get("id") != binding["vpc_id"]
        or network.get("vswitch", {}).get("id") != binding["vswitch_id"]
        or network.get("vswitch", {}).get("vpc") != binding["vpc_id"]
        or network.get("restricted_access_group", {}).get("name") != binding["access_group_name"]
        or network.get("restricted_access_group", {}).get("rules", {}).get("rows") != []
        or network.get("restricted_access_group", {}).get("rules", {}).get("total_count") != 0
        or network.get("filesystem_zone", {}).get("selected") != binding["zone_id"]
    ):
        raise WorkflowError("NAS literal parent/network bindings differ from reviewed original sources")
    entries = packet["execution_grants"]["TF"]
    writes = [entry for entry in entries if entry.get("kind") in {"create", "delete"}]
    if len(writes) != 2 or {entry["kind"] for entry in writes} != {"create", "delete"} or any(
        entry.get("quota") != 1 or entry["call"].get("product") != "ros"
        or entry["call"].get("region_id") != region
        or entry["call"].get("action") != {"create": "CreateStack", "delete": "DeleteStack"}[entry["kind"]]
        for entry in writes
    ):
        raise WorkflowError("NAS permits only one owning hosted stack create and response-bound delete")
    create = next(entry for entry in writes if entry["kind"] == "create")
    template = create["call"]["params"].get("TemplateBody", "")
    if create["call"]["params"].get("StackName") != binding["stack_name"] or (
        binding.get("template_sha256") != hashlib.sha256(template.encode()).hexdigest()
    ):
        raise WorkflowError("NAS full inline template and unique stack name must remain immutable")
    try:
        value = json.loads(template)
        hcl = value["Workspace"]["main.tf"]
    except (ValueError, KeyError, TypeError) as error:
        raise WorkflowError("NAS requires decoded hosted Terraform main.tf") from error
    expected = f'''terraform {{ required_providers {{
 alicloud = {{ source = "aliyun/alicloud" version = "= 1.260.0" }} }} }}
provider "alicloud" {{ region = "{region}" }}
resource "alicloud_nas_file_system" "filesystem" {{
 file_system_type = "standard" protocol_type = "NFS" storage_type = "Performance"
 zone_id = "{binding['zone_id']}" description = "{binding['description']}"
}}
resource "alicloud_nas_mount_target" "mount_target" {{
 file_system_id = alicloud_nas_file_system.filesystem.id access_group_name = "{binding['access_group_name']}"
 network_type = "Vpc" vpc_id = "{binding['vpc_id']}" vswitch_id = "{binding['vswitch_id']}"
}}
output "file_system_id" {{ value = alicloud_nas_file_system.filesystem.id }}
output "mount_target_domain" {{ value = alicloud_nas_mount_target.mount_target.mount_target_domain }}'''

    def tokens(text: str) -> list[str]:
        return re.findall(r'"(?:[^"\\]|\\.)*"|[^\s"]+', text)

    if (
        set(value) != {"ROSTemplateFormatVersion", "Transform", "Workspace"}
        or value["ROSTemplateFormatVersion"] != "2015-09-01"
        or value["Transform"] != "Aliyun::Terraform-v1.5" or set(value["Workspace"]) != {"main.tf"}
        or not isinstance(hcl, str) or tokens(hcl) != tokens(expected)
    ):
        raise WorkflowError("NAS only admits the exact pinned empty standard filesystem + own mount grammar")
    # Product gate validates typed, signed same-stack child reads and strict cleanup phases.
    validate_execution(packet, {"TF"}, require_read_policy=True)
    for action in ("DescribeFileSystems", "DescribeMountTargets"):
        reads = [entry for entry in entries if entry["call"].get("product") == "nas"
                 and entry["call"].get("action") == action]
        if len(reads) != 2 or {entry.get("read_policy", {}).get("phase") for entry in reads} != {"execute", "cleanup"}:
            raise WorkflowError("NAS needs own exact child survival and post-delete absence reads")
        if any(entry.get("kind") != "read" or entry.get("parent") != create["id"] for entry in reads):
            raise WorkflowError("NAS child reads cannot target pre-existing or borrowed resources")
        cleanup = next(entry for entry in reads if entry["read_policy"]["phase"] == "cleanup")
        if cleanup["read_policy"].get("required_cleanup") is not True or (
            cleanup["read_policy"].get("after_delete") != create["id"]
        ):
            raise WorkflowError("NAS both child disappearance reads are mandatory after owning stack deletion")
    return {**required, "evidence": path.relative_to(root).as_posix(), "evidence_sha256": digest(path),
            "execution_sha256": execution_digest(packet), "execution_grants": packet["execution_grants"],
            "budget_cents": 0, "accounts": [proof["account"]], "sample_bindings": {"TF": binding},
            "operation_authorization_required": True}


def nas_prior_queries_only(root: Path, connection: Any, task_id: str) -> None:
    """A first NAS TF sample may follow already-terminal QUERY, never a prior write.

    Inspect all original workers, not merely the latest attempt. No old grant,
    attempt, authority, accepted query result or ownership record is modified.
    """
    workers = list(connection.execute("SELECT id,state FROM jobs WHERE task_id=? AND role='glm'", (task_id,)))
    if connection.execute("SELECT 1 FROM owned WHERE task_id=?", (task_id,)).fetchone() or any(
        row[1] != "complete" for row in workers
    ):
        raise WorkflowError("first NAS sample cannot replace any prior creation generation or live worker")
    gate = root / "execution-gate.sqlite3"
    if workers and not gate.is_file():
        raise WorkflowError("original NAS query journal required before first TF sample")
    if not gate.is_file():
        return
    with closing(sqlite3.connect(f"{gate.resolve().as_uri()}?mode=ro", uri=True)) as journal:
        for worker, _ in workers:
            rows = journal.execute(
                "SELECT a.scope,a.state,g.kind FROM attempts a LEFT JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope LIKE ?", (worker + ":%",),
            )
            if any(scope != worker + ":QUERY" or state != "succeeded" or kind not in {"identity", "read"}
                   for scope, state, kind in rows):
                raise WorkflowError("first NAS sample cannot refill TF/ROS/CREATE or bypass unknown original calls")


def admitted(
    root: Path, connection: Any, task: Any, packet: dict[str, Any] | None,
    *, review: dict[str, Any] | None = None,
) -> dict[str, str]:
    from .additional_nas import additional_nas_authority

    operation = additional_nas_authority(root, connection, task, packet)
    if operation:
        return operation
    operation = additional_empty_table_authority(root, connection, task, packet)
    if operation:
        return operation
    row = connection.execute("SELECT value FROM meta WHERE key=?", ("temporary_samples:" + task["id"],)).fetchone()
    if not row:
        return {}
    saved = json.loads(row[0])
    run = connection.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
    if reviewed_scope(root, task, run, saved["evidence"]) != saved:
        raise WorkflowError("temporary sample review changed; preserve original admission and quotas")
    if packet is None:
        return {}
    if saved["kind"] == "ops_empty_general_nas_admission" and any(
        item.get("dimension") == "TF" for item in packet.get("additional_source_requests", [])
    ):
        raise WorkflowError("additional NAS requires its separate exact human admission, not the original allowance")
    if packet.get("budget_cents") != saved["budget_cents"] or sorted({
        item["account"] for item in packet["scenes"].values()
    }) != saved["accounts"]:
        raise WorkflowError("temporary sample budget or signed account scope changed")
    if saved["kind"] == "ops_native_rocketmq_sample_admission" and packet.get("additional_source_requests"):
        return native_rocketmq_followup_authority(root, connection, task, packet, saved, review)
    if saved["kind"] == "ops_native_nas_query_sample_admission":
        if packet.get("additional_source_requests"):
            from .additional_source import validate_additional_source
            from .nas_correction import reviewed_correction

            approval = None
            correction_review = review
            if correction_review is None:
                validate_authorization(root, task["resource_id"], packet)
                approval = read_json(authorization_path(root, packet))
                correction_review = read_json(contained(root, approval["authorization_evidence"]))
            if approval is None:
                if connection.execute("SELECT 1 FROM owned WHERE task_id=? AND state!='deleted'",
                                      (task["id"],)).fetchone():
                    raise WorkflowError("NAS corrective operation requires all original owners cleaned")
                validate_additional_source(root, packet, correction_review)
            request = packet["additional_source_requests"][0]
            old = read_json(contained(root, request["predecessor_proposal"]))
            reviewed_correction(root, old, packet, request, correction_review)
            authority = hashlib.sha256(canonical({"task_id": task["id"], "operation": request}).encode()).hexdigest()
            authority += ":QUERY"
            if approval and approval.get("dimension_authorities", {}).get("QUERY") != authority:
                raise WorkflowError("NAS corrected operation must retain its distinct reviewed authority")
            return {"QUERY": authority}
        proposal = contained(root, saved["proposal"])
        if digest(proposal) != saved["proposal_sha256"] or read_json(proposal) != packet:
            raise WorkflowError("native NAS sample must retain its entire original proposal and cumulative quotas")
        return {"QUERY": saved["execution_sha256"] + ":QUERY"}
    # A failed plan never created the permitted table. Its immutable admission
    # stays intact; an independently reviewed correction gets a NEW operation,
    # not replenished writes under the old authority.
    correction_review = review
    correction_approval = None
    if correction_review is None and packet.get("additional_source_requests"):
        path = authorization_path(root, packet)
        if path.is_file():
            validate_authorization(root, task["resource_id"], packet)
            correction_approval = read_json(path)
            correction_review = read_json(contained(root, correction_approval["authorization_evidence"]))
    if correction_review and correction_review.get("temporary_template_corrections"):
        from .additional_source import validate_additional_source, validate_paid_operation_review

        if correction_approval is None:
            # New OPS admission must still require every prior owner cleaned.
            validate_additional_source(root, packet, correction_review)
        else:
            # Reading the already authorized execution's context is not a new
            # admission. Its own owner may await the very cleanup review whose
            # context we are building. Recheck immutable approval, historical
            # correction and fee evidence without requiring that review first.
            validate_paid_operation_review(root, packet, correction_review)
        request = packet["additional_source_requests"][0]
        old = read_json(contained(root, request["predecessor_proposal"]))
        if not reviewed_tf_template_correction(root, old, packet, request, correction_review):
            raise WorkflowError("temporary correction needs its exact independent review")
        correction = correction_review["temporary_template_corrections"][request["request_id"]]
        if (saved["kind"] == "ops_hourly_managed_tf_sample_admission"
                and packet.get("hourly_cost") != saved["hourly_cost"]
                and correction.get("kind") != "ops_mse_tf_professional_correction"):
            raise WorkflowError("temporary hosted source cannot replace its original hourly cost review")
        authority = hashlib.sha256(canonical({"task_id": packet["task_id"], "operation": request}).encode()).hexdigest()
        authority += ":TF"
        if correction_approval and correction_approval.get("dimension_authorities", {}).get("TF") != authority:
            raise WorkflowError("corrected sample must retain its distinct reviewed operation authority")
        return {"TF": authority}
    if (saved["kind"] == "ops_hourly_managed_tf_sample_admission"
            and packet.get("hourly_cost") != saved["hourly_cost"]):
        raise WorkflowError("temporary hosted source cannot replace its original hourly cost review")
    bindings = {}
    continued = set()
    for dimension, entries in packet.get("execution_grants", {}).items():
        original = saved["execution_grants"].get(dimension)
        if original is None:
            continue
        if entries == original:
            bindings[dimension] = saved["execution_sha256"] + ":" + dimension
            continue
        # A reviewed read continuation can retain this immutable sample, never
        # replace its template/parent/name or replenish a write entitlement.
        if len(entries) != len(original):
            continue
        if all(
            {key: value for key, value in entry.items() if key != "quota"}
            == {key: value for key, value in prior.items() if key != "quota"}
            and type(entry.get("quota")) is int
            and (entry["quota"] >= prior["quota"] if prior["kind"] in {"identity", "read"}
                 else entry["quota"] == prior["quota"])
            for entry, prior in zip(entries, original)
        ):
            continued.add(dimension)
    if not continued:
        return bindings
    admission = review is not None
    approval = None
    if review is None:
        path = authorization_path(root, packet)
        if not path.is_file():
            return bindings
        validate_authorization(root, task["resource_id"], packet)
        approval = read_json(path)
        review = read_json(contained(root, approval["authorization_evidence"]))
    authorities = carry_forward_authorities(root, packet, review, review_admission=admission)
    for dimension in continued:
        source = review.get("carry_forward_dimensions", {}).get(dimension, {})
        expected = saved["execution_sha256"] + ":" + dimension
        if not any(source.get(key) is not None for key in ("identity_continuation", "read_continuation")):
            raise WorkflowError("temporary sample read changes need explicit OPS continuation review")
        if authorities.get(dimension) != expected or (
            approval is not None and approval.get("dimension_authorities", {}).get(dimension) != expected
        ):
            raise WorkflowError("temporary sample continuation must retain its original cumulative authority")
        bindings[dimension] = expected
    return bindings


def reviewed_additional_empty_table(
    root: Path, task: Any, run: Any, path: Path, proof: dict[str, Any],
) -> dict[str, Any]:
    """One separately human-approved generation, never a refill of the original admission.

    Admission validates the predecessor's actual consumed writes via additional_source.
    Runtime revalidation binds immutable bytes without requiring the new owner to have
    been deleted before its own cleanup context can be read.
    """
    from .additional_source import validate_paid_operation_review
    def original(field: str) -> tuple[Path, dict[str, Any]]:
        source = contained(root / "inputs", str(proof.get(field, "")).removeprefix("inputs/"))
        if not source.is_file() or digest(source) != proof.get(field + "_sha256"):
            raise WorkflowError("additional empty-table admission original evidence changed")
        return source, read_json(source)

    human_path, human = original("human_authorization")
    proposal_path, packet = original("proposal")
    _, operation_review = original("operation_review")
    spec = json.loads(task["spec"])
    scene = task["resource_id"] + "-TF-1-iframe"
    required = {"schema_version": 1, "kind": "ops_additional_empty_table_admission", "approved": True,
                "task_id": task["id"], "plan_id": task["run_id"],
                "frozen_plan_sha256": hashlib.sha256(run["plan"].encode()).hexdigest()}
    flags = ("includes_previous_operations_in_budget", "existing_verified_accessible_parent_only",
             "delete_created_tables_after_test", "no_data_rows", "no_indexes", "no_new_instance",
             "no_existing_network_or_iam_changes")
    scope = human.get("scope", {})
    requests = packet.get("additional_source_requests", [])
    if (
        any(proof.get(key) != value for key, value in required.items())
        or human.get("kind") != "human_fee_authorization" or human.get("approved") is not True
        or human.get("task_id") != task["id"] or human.get("plan_id") != task["run_id"]
        or not human.get("verbatim_user_message")
        or scope.get("additional_ros_managed_terraform_empty_tables_max") != 1
        or any(scope.get(flag) is not True for flag in flags)
        or spec.get("key") != "ALIYUN::OTS::Table"
        or spec.get("retest_scope", {}).get("kind") != "failed_navigation_managed"
        or scene not in spec.get("retest_scope", {}).get("scene_ids", [])
        or packet.get("task_id") != task["id"] or set(packet.get("scenes", {})) != {scene}
        or set(packet.get("execution_grants", {})) != {"TF"}
        or type(packet.get("budget_cents")) is not int or not 0 < packet["budget_cents"] <= run["budget"]
        or type(human.get("max_budget_cents")) is not int or packet["budget_cents"] > human["max_budget_cents"]
        or proof.get("execution_sha256") != execution_digest(packet)
        or len(requests) != 1 or requests[0].get("dimension") != "TF"
        or operation_review.get("proposal_sha256") != digest(proposal_path)
        or operation_review.get("kind") != "ops_additional_sources_review"
        or operation_review.get("approved") is not True
        or operation_review.get("additional_sources_approved") != requests
        or operation_review.get("carry_forward_dimensions") != {}
        or operation_review.get("temporary_template_corrections")
        or operation_review.get("paid_operation_review", {}).get("human_authorization")
        != human_path.relative_to(root).as_posix()
        or operation_review.get("paid_operation_review", {}).get("human_sha256") != digest(human_path)
    ):
        raise WorkflowError("one exact human-approved additional OTS TF iframe operation required")
    validate_execution(packet, {"TF"}, require_read_policy=True)
    validate_paid_operation_review(root, packet, operation_review)
    old_path = contained(root, requests[0].get("predecessor_proposal", "missing"))
    if digest(old_path) != requests[0].get("predecessor_sha256"):
        raise WorkflowError("additional empty table predecessor changed")
    if packet["execution_grants"]["TF"] != read_json(old_path).get("execution_grants", {}).get("TF"):
        raise WorkflowError("additional empty table must retain the previously verified exact template and calls")
    entries = packet["execution_grants"]["TF"]
    writes = [entry for entry in entries if entry["kind"] in {"create", "delete"}]
    binding = proof.get("sample_binding", {})
    if (
        len(writes) != 2 or {entry["kind"] for entry in writes} != {"create", "delete"}
        or any(entry["quota"] != 1 or entry["call"]["product"] != "ros"
               or entry["call"]["action"] != {"create": "CreateStack", "delete": "DeleteStack"}[entry["kind"]]
               for entry in writes)
        or binding.get("empty_table_only") is not True
        or any(not isinstance(binding.get(key), str) or not binding[key]
               for key in ("instance_name", "table_name", "region", "template_sha256"))
    ):
        raise WorkflowError("one empty response-bound managed table and exact existing parent required")
    create = next(entry for entry in writes if entry["kind"] == "create")
    body = create["call"]["params"].get("TemplateBody")
    if not isinstance(body, str) or hashlib.sha256(body.encode()).hexdigest() != binding["template_sha256"]:
        raise WorkflowError("additional table full inline template changed")
    value = json.loads(body)
    hcl = value.get("Workspace", {}).get("main.tf", "")
    if (
        value.get("Transform") != "Aliyun::Terraform-v1.5" or set(value.get("Workspace", {})) != {"main.tf"}
        or binding["region"] != create["call"]["region_id"]
        or re.findall(r'\bresource\s+"([^"]+)"', hcl.replace('\\"', '"')) != ["alicloud_ots_table"]
        or re.search(r'\b(module|data|provisioner|count|for_each|dynamic)\b', hcl)
        or any(f'{key} = "{binding[key]}"' not in hcl.replace('\\"', '"')
               for key in ("instance_name", "table_name"))
    ):
        raise WorkflowError("additional table cannot create dependencies or change parent/name")
    return {**required, "evidence": path.relative_to(root).as_posix(), "evidence_sha256": digest(path),
            "human_sha256": digest(human_path), "proposal": proposal_path.relative_to(root).as_posix(),
            "human_permission_id": hashlib.sha256(canonical({
                "task_id": task["id"],
                "question": human.get("human_question_verbatim", human["verbatim_user_message"]),
                "reply": human.get("human_reply_verbatim", human["verbatim_user_message"]),
            }).encode()).hexdigest(),
            "proposal_sha256": digest(proposal_path), "execution_sha256": execution_digest(packet),
            "operation_authorization_required": True}


def additional_empty_table_authority(
    root: Path, connection: Any, task: Any, packet: dict[str, Any] | None,
) -> dict[str, str]:
    if packet is None:
        return {}
    rows = connection.execute("SELECT value FROM meta WHERE key LIKE ?",
                              ("additional_empty_table:" + task["id"] + ":%",)).fetchall()
    run = connection.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
    if rows:
        initial = connection.execute("SELECT value FROM meta WHERE key=?",
                                     ("temporary_samples:" + task["id"],)).fetchone()
        if not initial:
            raise WorkflowError("additional table lost its immutable original admission")
        original_scope = json.loads(initial[0])
        if reviewed_scope(root, task, run, original_scope["evidence"]) != original_scope:
            raise WorkflowError("original temporary sample admission changed")
    for row in rows:
        saved = json.loads(row[0])
        path = contained(root, saved["evidence"])
        if reviewed_additional_empty_table(root, task, run, path, read_json(path)) != saved:
            raise WorkflowError("immutable additional empty-table admission changed")
        original = read_json(contained(root, saved["proposal"]))
        if canonical(packet) == canonical(original):
            request = packet["additional_source_requests"][0]
            authority = hashlib.sha256(canonical({"task_id": task["id"], "operation": request}).encode()).hexdigest()
            return {"TF": authority + ":TF"}
        if packet.get("additional_source_requests") == original.get("additional_source_requests"):
            raise WorkflowError("additional empty-table operation must retain its full reviewed proposal")
    return {}


def reviewed_tf_template_correction(
    root: Path, old: dict[str, Any], packet: dict[str, Any], request: dict[str, Any], proof: dict[str, Any],
) -> bool:
    reviews = proof.get("temporary_template_corrections", {})
    review = reviews.get(request.get("request_id"), {}) if isinstance(reviews, dict) else {}
    if review.get("kind") == "ops_mse_tf_professional_correction":
        from .mse_correction import reviewed_mse_correction

        return reviewed_mse_correction(root, old, packet, request, proof)
    if review.get("kind") == "ops_ots_tf_provider_header_correction":
        from .provider_trial import reviewed_provider_trial

        return reviewed_provider_trial(root, old, packet, request, proof)
    return reviewed_tf_enum_correction(root, old, packet, request, proof)


def reviewed_tf_enum_correction(
    root: Path, old: dict[str, Any], packet: dict[str, Any], request: dict[str, Any], proof: dict[str, Any],
) -> bool:
    """One exact STRING->String correction after a proven pre-apply OTS failure.

    This is deliberately not a general template amendment or retry mechanism.
    Original admission, account, names, calls, budgets and journal are read-only.
    The additional-source validator still checks terminal jobs, consumed writes,
    unknown results and all deleted owners before granting the new operation.
    """
    reviews = proof.get("temporary_template_corrections", {})
    if not reviews:
        return False
    key = request.get("request_id")
    review = reviews.get(key) if isinstance(reviews, dict) else None
    if (
        not isinstance(review, dict) or set(reviews) != {key}
        or review.get("kind") != "ops_ots_tf_primary_key_correction" or review.get("approved") is not True
        or review.get("task_id") != packet["task_id"] or request.get("dimension") != "TF"
        or set(packet.get("execution_grants", {})) != {"TF"}
        or packet.get("additional_source_requests") != [request]
        or set(packet.get("scenes", {})) != {packet["task_id"].rsplit(":", 1)[-1] + "-TF-1-iframe"}
        or review.get("original_table_never_created") is not True
        or not review.get("human_scope_interpretation")
    ):
        raise WorkflowError("only one reviewed remaining OTS TF iframe correction is admitted")
    with closing(sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as state:
        state.row_factory = sqlite3.Row
        task = state.execute("SELECT * FROM tasks WHERE id=?", (packet["task_id"],)).fetchone()
        row = state.execute("SELECT value FROM meta WHERE key=?",
                            ("temporary_samples:" + packet["task_id"],)).fetchone()
        if not task or not row:
            raise WorkflowError("template correction requires the immutable original temporary admission")
        saved = json.loads(row[0])
        run = state.execute("SELECT * FROM runs WHERE id=?", (task["run_id"],)).fetchone()
        if reviewed_scope(root, task, run, saved["evidence"]) != saved:
            raise WorkflowError("original temporary admission changed")
    if review.get("original_admission") != {"path": saved["evidence"], "sha256": saved["evidence_sha256"]} or (
        packet.get("budget_cents") != saved["budget_cents"]
        or {item["account"] for item in packet["scenes"].values()} != set(saved["accounts"])
        or old.get("task_id") != packet["task_id"]
    ):
        raise WorkflowError("correction must preserve original admission, human budget and account")
    original, successor = old.get("execution_grants", {}).get("TF", []), packet["execution_grants"]["TF"]
    immutable = saved["execution_grants"]["TF"]
    if len(original) != len(immutable) or len(successor) != len(original):
        raise WorkflowError("template correction cannot add or remove calls")
    expected = json.loads(json.dumps(original))
    for prior, admitted_entry, new in zip(original, immutable, expected):
        if {k: v for k, v in prior.items() if k != "quota"} != {
            k: v for k, v in admitted_entry.items() if k != "quota"
        } or type(prior.get("quota")) is not int or prior["quota"] < admitted_entry["quota"]:
            raise WorkflowError("predecessor must retain original admitted calls")
        if prior["kind"] in {"create", "delete"} and prior["quota"] != 1:
            raise WorkflowError("original write quota is exactly one")
        if prior["kind"] == "identity" or prior["id"] == "table-name-precheck":
            new["quota"] = 1  # New explicitly reviewed operation, not an old read continuation.
    creates = [entry for entry in expected if entry["kind"] == "create"]
    if len(creates) != 1:
        raise WorkflowError("one original ROS-managed table stack required")
    create = creates[0]
    template = create["call"]["params"]["TemplateBody"]
    value = json.loads(template)
    hcl = value.get("Workspace", {}).get("main.tf", "")
    if (
        value.get("Transform") != "Aliyun::Terraform-v1.5" or set(value.get("Workspace", {})) != {"main.tf"}
        or template.count("STRING") != 1 or not re.search(r'\btype\s*=\s*"STRING"', hcl)
        or 'version = "= 1.140.0"' not in hcl
        or re.findall(r'\bresource\s+"([^"]+)"', hcl) != ["alicloud_ots_table"]
    ):
        raise WorkflowError("correction is limited to the original pinned OTS primary-key enum")
    corrected = template.replace("STRING", "String")
    create["call"]["params"]["TemplateBody"] = corrected
    if (successor != expected
        or review.get("original_template_sha256") != hashlib.sha256(template.encode()).hexdigest()
    ) or (
        review.get("corrected_template_sha256") != hashlib.sha256(corrected.encode()).hexdigest()
    ):
        raise WorkflowError("only one enum and fresh identity/precheck quota of one may change")
    official = review.get("provider_source", {})
    source = contained(root, official.get("path", "missing"))
    if official.get("url") != (
        "https://raw.githubusercontent.com/aliyun/terraform-provider-alicloud/v1.140.0/"
        "alicloud/resource_alicloud_ots_table.go"
    ) or digest(source) != official.get("sha256"):
        raise WorkflowError("correction needs unchanged pinned official provider body")
    body = source.read_text(encoding="utf-8")
    if "validation.StringInSlice" not in body or "string(StringType)}, false)" not in body or (
        "reservedThroughput := new(tablestore.ReservedThroughput)" not in body
    ):
        raise WorkflowError("official pinned schema and zero reserved-throughput semantics required")
    scope = request["prior_scope"]
    with closing(sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True)) as gate:
        gate.row_factory = sqlite3.Row
        create_row = gate.execute("SELECT * FROM attempts WHERE scope=? AND grant_id=?",
                                  (scope, create["id"])).fetchone()
        failure = gate.execute("SELECT * FROM attempts WHERE id=? AND scope=?",
                               (review.get("original_failure_attempt_id"), scope)).fetchone()
        if not create_row or not failure or failure["state"] != "succeeded":
            raise WorkflowError("real original create and successful failure-status read required")
        status = json.loads(failure["response"])
        stack_id = json.loads(create_row["response"]).get("StackId")
        logs = status.get("Log", {}).get("TerraformLogs", [])
        error = "expected primary_key.0.type to be one of [Integer Binary String], got STRING"
        failure_grant = next((entry for entry in original if entry["id"] == failure["grant_id"]), {})
        if (
            not stack_id or status.get("StackId") != stack_id or status.get("StackType") != "Terraform"
            or status.get("Status") != "CREATE_ROLLBACK_COMPLETE" or not status.get("RequestId")
            or status.get("OperationInfo", {}).get("ResourceType") != "alicloud_ots_table"
            or status["OperationInfo"].get("Message") != error
            or not any(log.get("Command") == "plan" and log.get("Stream") == "stderr"
                       and log.get("Content", "").startswith("Error: " + error) for log in logs)
            or any(log.get("Command") == "apply" for log in logs)
            or failure_grant.get("kind") != "read" or failure_grant.get("call", {}).get("action") != "GetStack"
            or failure_grant.get("parent") != create["id"] or failure_grant.get("response_path") != ["StackId"]
            or failure["created"] <= create_row["created"]
        ):
            raise WorkflowError("only a proven pre-apply enum validation failure permits this correction")
        for prior in original:
            binding = gate.execute("SELECT kind,call FROM grants WHERE scope=? AND id=?",
                                   (scope, prior["id"])).fetchone()
            if not binding or tuple(binding) != (prior["kind"], canonical(prior["call"])):
                raise WorkflowError("original signed calls differ from the reviewed predecessor")
        identity = gate.execute("SELECT DISTINCT account FROM identities WHERE scope=?", (scope,)).fetchall()
        if [tuple(row) for row in identity] != [(saved["accounts"][0],)]:
            raise WorkflowError("original operation needs its actual same-account signed identity")
    # Request renaming must not mint a second corrected table operation.
    for path in (root / "creation-authorizations").glob("*.json"):
        approval = read_json(path)
        if approval.get("task_id") != packet["task_id"]:
            continue
        previous_proof = read_json(contained(root, approval["authorization_evidence"]))
        previous_reviews = previous_proof.get("temporary_template_corrections", {})
        enum_reviews = {key: value for key, value in previous_reviews.items()
                        if value.get("kind") == "ops_ots_tf_primary_key_correction"}
        if enum_reviews and (
            previous_reviews != reviews or approval.get("additional_source_requests") != [request]
        ):
            raise WorkflowError("original temporary sample already has its one corrected operation")
    return True
