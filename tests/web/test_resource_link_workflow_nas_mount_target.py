"""Offline provenance checks for native NAS MountTarget address tooltips."""

import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_nas_mount_target_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def fixture():
    domain = "offlinefs-target.cn-hangzhou.nas.aliyuncs.com"
    binding = {
        "console_inspection": "nas_mount_target_address",
        "region": "cn-hangzhou",
        "account": "1234567890123456",
        "file_system_id": "offlinefs",
        "render_text": [domain, "vpc-offline"],
    }
    inspection = console_inspection(binding, "ALIYUN::NAS::MountTarget", domain)
    ref = {
        "provider": "aliyun",
        "consolePartition": "aliyun",
        "resourceType": "ALIYUN::NAS::MountTarget",
        "identifier": domain,
        "scopeKind": "regional",
        "accountScope": "offline-signed",
        "regionId": "cn-hangzhou",
        "attributes": {"FileSystemId": "offlinefs", "MountTargetDomain": domain},
        "sources": [
            {
                "toolUseId": "offline-call",
                "product": "nas",
                "action": "DescribeMountTargets",
                "sourcePath": "/result/MountTargets/MountTarget/0/MountTargetDomain",
                "requestRegionId": "cn-hangzhou",
                "evidence": "api_response",
                "sourceKind": "tool_result",
            }
        ],
    }
    row = {"MountTargetDomain": domain, "VpcId": "vpc-offline", "VswId": "vsw-offline", "NetworkType": "Vpc"}
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "nas",
            "action": "DescribeMountTargets",
            "version": "2017-06-26",
            "region_id": "cn-hangzhou",
            "params": {"FileSystemId": "offlinefs", "MountTargetDomain": domain, "PageNumber": 1, "PageSize": 10},
        },
        "results": [
            {
                "toolUseId": "offline-call",
                "isError": False,
                "content": json.dumps({"RequestId": "offline-request", "MountTargets": {"MountTarget": [row]}}),
            }
        ],
    }
    return binding, inspection, ref, tool, row


@pytest.mark.parametrize("resource_type", ["ALIYUN::NAS::MountTarget", "alicloud_nas_mount_target"])
def test_current_own_source_supplies_vswitch_without_invented_reference_attributes(resource_type):
    binding, _, ref, tool, _ = fixture()
    ref["resourceType"] = resource_type
    inspection = console_inspection(binding, resource_type, ref["identifier"])
    result = bind_nas_mount_target_source(inspection, [ref], {"offline-call": tool})
    assert result["source_tool_use_id"] == "offline-call"
    assert result["vswitch_id"] == "vsw-offline"
    assert "VswId" not in ref["attributes"]


@pytest.mark.parametrize(
    "damage",
    [
        "domain",
        "vpc",
        "vswitch",
        "network",
        "parent",
        "reference-domain",
        "region",
        "source-region",
        "source-path",
        "source-kind",
        "source-evidence",
        "version",
        "action",
        "product",
        "filter",
        "tool",
        "result-owner",
        "error",
        "provider",
        "partition",
        "scope",
        "account",
        "duplicate",
        "duplicate-row",
        "json",
        "index",
        "code",
        "request-id",
    ],
)
def test_wrong_or_ambiguous_provenance_rejected(damage):
    _, inspection, ref, tool, row = fixture()
    refs = [ref]
    raw = {"RequestId": "offline-request", "MountTargets": {"MountTarget": [row]}}
    if damage in {"domain", "vpc", "vswitch", "network"}:
        field = {"domain": "MountTargetDomain", "vpc": "VpcId", "vswitch": "VswId", "network": "NetworkType"}[damage]
        row[field] = "wrong"
    elif damage in {"parent", "reference-domain"}:
        ref["attributes"]["FileSystemId" if damage == "parent" else "MountTargetDomain"] = "wrong"
    elif damage == "region":
        ref["regionId"] = "cn-beijing"
    elif damage.startswith("source-"):
        ref["sources"][0][
            {
                "source-region": "requestRegionId",
                "source-path": "sourcePath",
                "source-kind": "sourceKind",
                "source-evidence": "evidence",
            }[damage]
        ] = "wrong"
    elif damage in {"version", "action", "product"}:
        tool["input"][damage] = "wrong"
    elif damage == "filter":
        tool["input"]["params"]["MountTargetDomain"] = "other"
    elif damage == "tool":
        tool["toolName"] = "other"
    elif damage == "result-owner":
        tool["results"][0]["toolUseId"] = "other"
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage in {"provider", "partition", "scope", "account"}:
        ref[{"partition": "consolePartition", "scope": "scopeKind", "account": "accountScope"}.get(damage, damage)] = ""
    elif damage == "duplicate":
        refs.append(ref)
    elif damage == "duplicate-row":
        raw["MountTargets"]["MountTarget"].append(row)
    elif damage == "index":
        ref["sources"][0]["sourcePath"] = "/result/MountTargets/MountTarget/1/MountTargetDomain"
    elif damage == "code":
        raw["Code"] = "Denied"
    elif damage == "request-id":
        del raw["RequestId"]
    tool["results"][0]["content"] = "not-json" if damage == "json" else json.dumps(raw)
    with pytest.raises(WorkflowError, match="own signed"):
        bind_nas_mount_target_source(inspection, refs, {"offline-call": tool})


@pytest.mark.parametrize(
    "patch",
    [
        {"account": "wrong"},
        {"region": "global"},
        {"region": "cn-beijing"},
        {"file_system_id": "other"},
        {"file_system_id": "bad/parent"},
        {"render_text": []},
        {"render_text": ["other", "vpc-offline"]},
        {"render_text": ["offlinefs-target.cn-hangzhou.nas.aliyuncs.com", "wrong"]},
    ],
)
def test_invalid_scene_binding_rejected(patch):
    binding, _, ref, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection({**binding, **patch}, ref["resourceType"], ref["identifier"])


def test_protocol_target_or_other_resource_cannot_borrow_mount_target_inspector():
    binding, _, ref, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection(binding, "ALIYUN::NAS::ProtocolMountTarget", ref["identifier"])
