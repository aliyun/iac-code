"""Source-bound native export drawer eligibility, entirely offline."""

import json

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import bind_nas_export_source, console_inspection


def source():
    domain = "cpfs-parent-child.cn-beijing.cpfs.aliyuncs.com"
    binding = {
        "console_inspection": "nas_protocol_mount_target_child",
        "region": "cn-beijing",
        "file_system_id": "cpfs-parent",
        "protocol_service_id": "ptc-parent",
        "export_id": "exp-child",
        "render_text": ["offline-name", "/offline/"],
    }
    inspection = console_inspection(binding, "ALIYUN::NAS::ProtocolMountTarget", domain)
    attrs = {"FileSystemId": "cpfs-parent", "ProtocolServiceId": "ptc-parent", "ExportId": "exp-child"}
    ref = {
        "resourceType": "ALIYUN::NAS::ProtocolMountTarget",
        "identifier": domain,
        "regionId": "cn-beijing",
        "attributes": attrs,
        "sources": [
            {
                "toolUseId": "offline-call",
                "product": "nas",
                "action": "DescribeProtocolMountTarget",
                "requestRegionId": "cn-beijing",
                "evidence": "api_response",
            }
        ],
    }
    row = {
        "ProtocolServiceId": "ptc-parent",
        "ExportId": "exp-child",
        "ProtocolMountTargetDomain": domain,
        "Description": "offline-name",
        "Path": "/offline/",
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "nas",
            "action": "DescribeProtocolMountTarget",
            "version": "2017-06-26",
            "region_id": "cn-beijing",
            "params": {"FileSystemId": "cpfs-parent"},
        },
        "results": [{"isError": False, "content": json.dumps({"ProtocolMountTargets": [row]})}],
    }
    return inspection, ref, tool, row


def test_own_child_row_is_bound_without_manufacturing_name_or_path_attributes():
    inspection, ref, tool, _ = source()
    assert bind_nas_export_source(inspection, [ref], {"offline-call": tool})["source_tool_use_id"] == "offline-call"
    assert "Description" not in ref["attributes"]


@pytest.mark.parametrize("field", ["ProtocolServiceId", "ExportId", "ProtocolMountTargetDomain", "Description", "Path"])
def test_sibling_or_split_fields_cannot_authorize_native_child_lookup(field):
    inspection, ref, tool, row = source()
    row[field] = "wrong"
    tool["results"][0]["content"] = json.dumps({"ProtocolMountTargets": [row]})
    with pytest.raises(WorkflowError, match="own signed"):
        bind_nas_export_source(inspection, [ref], {"offline-call": tool})


@pytest.mark.parametrize("damage", ["parent", "region", "error", "duplicate", "version"])
def test_source_provenance_and_unique_parent_remain_mandatory(damage):
    inspection, ref, tool, _ = source()
    refs = [ref]
    if damage == "parent":
        tool["input"]["params"]["FileSystemId"] = "cpfs-other"
    elif damage == "region":
        ref["regionId"] = "cn-hangzhou"
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "version":
        tool["input"]["version"] = "wrong"
    else:
        refs.append(ref)
    with pytest.raises(WorkflowError):
        bind_nas_export_source(inspection, refs, {"offline-call": tool})
