"""Offline admission contracts; signed child gate is independently tested at its layer."""

import copy
import hashlib
import json

import pytest

from scripts.web.e2e.resource_link_workflow import temporary_samples as samples
from scripts.web.e2e.resource_link_workflow.creation import execution_digest
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    root = tmp_path
    directory = root / "inputs/R0039"
    directory.mkdir(parents=True)
    task = {
        "id": "test:R0039",
        "run_id": "test",
        "spec": json.dumps({"key": "ALIYUN::NAS::MountTarget", "retest_scope": {"kind": "failed_navigation_managed"}}),
    }
    run = {"plan": '{"frozen":true}'}
    binding = {
        "region": "cn-hangzhou",
        "zone_id": "cn-hangzhou-f",
        "description": "sol-r0039-empty-test",
        "access_group_name": "restricted",
        "vpc_id": "vpc-test",
        "vswitch_id": "vsw-test",
        "stack_name": "sol-r0039-tf-test",
        "empty_general_only": True,
    }
    hcl = """terraform { required_providers { alicloud = { source = "aliyun/alicloud" version = "= 1.260.0" } } }
provider "alicloud" { region = "cn-hangzhou" }
resource "alicloud_nas_file_system" "filesystem" {
 file_system_type = "standard" protocol_type = "NFS" storage_type = "Performance"
 zone_id = "cn-hangzhou-f" description = "sol-r0039-empty-test"
}
resource "alicloud_nas_mount_target" "mount_target" {
 file_system_id = alicloud_nas_file_system.filesystem.id access_group_name = "restricted"
 network_type = "Vpc" vpc_id = "vpc-test" vswitch_id = "vsw-test"
}
output "file_system_id" { value = alicloud_nas_file_system.filesystem.id }
output "mount_target_domain" { value = alicloud_nas_mount_target.mount_target.mount_target_domain }"""
    template = json.dumps(
        {"ROSTemplateFormatVersion": "2015-09-01", "Transform": "Aliyun::Terraform-v1.5", "Workspace": {"main.tf": hcl}}
    )
    binding["template_sha256"] = hashlib.sha256(template.encode()).hexdigest()
    create = {
        "id": "create",
        "kind": "create",
        "quota": 1,
        "call": {
            "product": "ros",
            "action": "CreateStack",
            "region_id": "cn-hangzhou",
            "params": {"StackName": binding["stack_name"], "TemplateBody": template},
        },
    }
    delete = {
        "id": "delete",
        "kind": "delete",
        "quota": 1,
        "parent": "create",
        "call": {
            "product": "ros",
            "action": "DeleteStack",
            "region_id": "cn-hangzhou",
            "params": {"StackId": "$created_id"},
        },
    }
    reads = [
        {
            "id": action + phase,
            "kind": "read",
            "quota": 1,
            "parent": "create",
            "call": {"product": "nas", "action": action, "region_id": "cn-hangzhou", "params": {}},
            "read_policy": {
                "phase": phase,
                **({"required_cleanup": True, "after_delete": "create"} if phase == "cleanup" else {}),
            },
        }
        for action in ("DescribeFileSystems", "DescribeMountTargets")
        for phase in ("execute", "cleanup")
    ]
    packet = {
        "task_id": task["id"],
        "profile": "resource-link-test",
        "terraform": "ros_managed_terraform",
        "budget_cents": 0,
        "scenes": {"offline": {"account": "1234567890123456"}},
        "execution_grants": {"TF": [create, delete, *reads]},
    }
    human = {
        "kind": "human_temporary_source_authorization",
        "approved": True,
        "task_id": task["id"],
        "plan_id": "test",
        "human_question_verbatim": "one empty filesystem and mount, delete afterwards?",
        "human_reply_verbatim": "yes",
        "scope": {
            "source_dimension": "TF",
            "execution": "ros_managed_terraform",
            **{
                key: 1
                for key in (
                    "new_empty_general_purpose_filesystems_max",
                    "new_mount_targets_max",
                    "new_managed_stacks_max",
                )
            },
            **{
                key: True
                for key in (
                    "reuse_verified_existing_vpc_vswitch_restricted_access_group_only",
                    "no_existing_filesystem_access_changes",
                    "no_data_mount_read_write",
                    "no_compute_disk_eip_extreme_nas_or_addons",
                    "no_iam_writes",
                    "delete_all_new_response_bound_objects_after_test",
                )
            },
        },
    }

    def save(name, value):
        path = directory / name
        json_write(path, value)
        return {"path": path.relative_to(root).as_posix(), "sha256": digest(path)}

    original = save("raw.json", {"fixture": "offline-only"})
    network = {
        "kind": "ops_nas_restricted_existing_network_source",
        "task_id": task["id"],
        "account": "1234567890123456",
        "region": "cn-hangzhou",
        "signed_sources": [original],
        "vpc": {"id": "vpc-test"},
        "vswitch": {"id": "vsw-test", "vpc": "vpc-test"},
        "filesystem_zone": {"selected": "cn-hangzhou-f"},
        "restricted_access_group": {"name": "restricted", "rules": {"rows": [], "total_count": 0}},
    }
    proof = {
        "schema_version": 1,
        "kind": "ops_empty_general_nas_admission",
        "approved": True,
        "task_id": task["id"],
        "plan_id": "test",
        "account": "1234567890123456",
        "frozen_plan_sha256": hashlib.sha256(run["plan"].encode()).hexdigest(),
        "human_authorization": save("human.json", human),
        "sample_binding": binding,
    }
    pricing = {
        "kind": "ops_nas_empty_general_operation_pricing_review",
        "approved": True,
        "task_id": task["id"],
        "account": "1234567890123456",
        "expected_cost_cents": 0,
        "human_authorization": proof["human_authorization"],
        "no_file_data_operations": True,
        "cleanup_required": True,
        "estimate_basis": "fixture",
        "limitations": ["fixture"],
        "official_sources": [original],
    }
    calls = []
    monkeypatch.setattr(samples, "validate_execution", lambda *args, **kwargs: calls.append((args, kwargs)))

    def review(mutate=None):
        p, h, n, fee, evidence = map(copy.deepcopy, (packet, human, network, pricing, proof))
        if mutate:
            mutate(p, h, n, fee, evidence)
        evidence["human_authorization"] = save("human.json", h)
        evidence["proposal"] = save("proposal.json", p)
        evidence["execution_sha256"] = execution_digest(p)
        fee["execution_sha256"] = execution_digest(p)
        fee["human_authorization"] = evidence["human_authorization"]
        evidence["pricing_review"] = save("pricing.json", fee)
        evidence["network_review"] = save("network.json", n)
        save("admission.json", evidence)
        return samples.reviewed_empty_general_nas(root, task, run, directory / "admission.json", evidence)

    return review, calls


def test_empty_nas_admission_still_requires_independent_operation_gate(candidate):
    review, calls = candidate
    result = review()
    assert result["budget_cents"] == 0 and result["operation_authorization_required"] is True
    assert set(result["execution_grants"]) == {"TF"}
    assert calls[0][1] == {"require_read_policy": True}


@pytest.mark.parametrize(
    "change",
    [
        lambda p, h, n, f, e: p.update(budget_cents=1),
        lambda p, h, n, f, e: h["scope"].update(new_empty_general_purpose_filesystems_max=2),
        lambda p, h, n, f, e: h["scope"].update(no_data_mount_read_write=False),
        lambda p, h, n, f, e: n["restricted_access_group"]["rules"].update(rows=[{"allow": "0.0.0.0/0"}]),
        lambda p, h, n, f, e: n["vswitch"].update(vpc="vpc-other"),
        lambda p, h, n, f, e: n["filesystem_zone"].update(selected="cn-hangzhou-i"),
        lambda p, h, n, f, e: f.update(approved=False),
        lambda p, h, n, f, e: f.update(expected_cost_cents=1),
        lambda p, h, n, f, e: p["execution_grants"]["TF"][0].update(quota=2),
        lambda p, h, n, f, e: p["execution_grants"]["TF"].pop(),
        lambda p, h, n, f, e: p["execution_grants"]["TF"][-1]["read_policy"].update(required_cleanup=False),
        lambda p, h, n, f, e: e.update(frozen_plan_sha256="0" * 64),
    ],
)
def test_empty_nas_scope_network_cost_and_cleanup_cannot_expand(candidate, change):
    with pytest.raises(WorkflowError):
        candidate[0](change)


@pytest.mark.parametrize(
    "replacement",
    [
        'resource "alicloud_instance" "extra" {}',
        'data "alicloud_nas_file_systems" "any" {}',
        'module "any" { source = "other" }',
        'output "another" { value = "unexpected" }',
    ],
)
def test_empty_nas_complete_grammar_rejects_extra_resources_even_with_new_digest(candidate, replacement):
    def change(p, h, n, fee, evidence):
        params = p["execution_grants"]["TF"][0]["call"]["params"]
        template = json.loads(params["TemplateBody"])
        template["Workspace"]["main.tf"] += "\n" + replacement
        params["TemplateBody"] = json.dumps(template)
        evidence["sample_binding"]["template_sha256"] = hashlib.sha256(params["TemplateBody"].encode()).hexdigest()

    with pytest.raises(WorkflowError, match="grammar"):
        candidate[0](change)
