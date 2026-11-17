"""Offline source contracts for native ProtocolService identity capture."""

import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_nas_protocol_service_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def fixture():
    binding = {
        "console_inspection": "nas_protocol_service_identity",
        "region": "cn-beijing",
        "file_system_id": "cpfs-parent",
        "render_text": ["offline-description", "ptc-child"],
    }
    inspection = console_inspection(binding, "ALIYUN::NAS::ProtocolService", "ptc-child")
    ref = {
        "resourceType": "ALIYUN::NAS::ProtocolService",
        "identifier": "ptc-child",
        "regionId": "cn-beijing",
        "attributes": {"FileSystemId": "cpfs-parent", "ProtocolServiceId": "ptc-child"},
        "sources": [
            {
                "toolUseId": "offline-call",
                "product": "nas",
                "action": "DescribeProtocolService",
                "requestRegionId": "cn-beijing",
                "evidence": "api_response",
                "sourceKind": "tool_result",
            }
        ],
    }
    row = {"FileSystemId": "cpfs-parent", "ProtocolServiceId": "ptc-child", "Description": "offline-description"}
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "nas",
            "action": "DescribeProtocolService",
            "version": "2017-06-26",
            "region_id": "cn-beijing",
            "params": {"FileSystemId": "cpfs-parent", "MaxResults": 20},
        },
        "results": [{"isError": False, "content": json.dumps({"ProtocolServices": [row]})}],
    }
    return binding, inspection, ref, tool, row


def test_description_bound_from_raw_without_inventing_reference_attributes():
    _, inspection, ref, tool, _ = fixture()
    bound = bind_nas_protocol_service_source(inspection, [ref], {"offline-call": tool})
    assert bound["source_tool_use_id"] == "offline-call"
    assert "Description" not in ref["attributes"]


@pytest.mark.parametrize("field", ["FileSystemId", "ProtocolServiceId", "Description"])
def test_wrong_or_split_child_fields_rejected(field):
    _, inspection, ref, tool, row = fixture()
    row[field] = "wrong"
    tool["results"][0]["content"] = json.dumps({"ProtocolServices": [row]})
    with pytest.raises(WorkflowError, match="own signed"):
        bind_nas_protocol_service_source(inspection, [ref], {"offline-call": tool})


@pytest.mark.parametrize(
    "damage",
    [
        "parent",
        "region",
        "version",
        "error",
        "duplicate",
        "kind",
        "tool",
        "row",
        "action",
        "unfinished",
        "missing",
        "reference-id",
        "request-region",
    ],
)
def test_invalid_or_ambiguous_provenance_rejected(damage):
    _, inspection, ref, tool, row = fixture()
    refs = [ref]
    if damage == "parent":
        tool["input"]["params"]["FileSystemId"] = "cpfs-other"
    elif damage == "region":
        ref["regionId"] = "cn-hangzhou"
    elif damage == "request-region":
        tool["input"]["region_id"] = "cn-hangzhou"
    elif damage == "version":
        tool["input"]["version"] = "wrong"
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "duplicate":
        refs.append(ref)
    elif damage == "kind":
        ref["sources"][0]["sourceKind"] = "model"
    elif damage == "tool":
        tool["results"][0]["toolUseId"] = "other"
    elif damage == "row":
        tool["results"][0]["content"] = json.dumps({"ProtocolServices": [row, row]})
    elif damage == "action":
        tool["input"]["action"] = "DescribeFilesets"
    elif damage == "unfinished":
        tool["status"] = "running"
    elif damage == "missing":
        tool["results"][0]["content"] = "{}"
    elif damage == "reference-id":
        ref["attributes"]["ProtocolServiceId"] = "ptc-other"
    with pytest.raises(WorkflowError):
        bind_nas_protocol_service_source(inspection, refs, {"offline-call": tool})


@pytest.mark.parametrize(
    "patch",
    [
        {"file_system_id": "other"},
        {"region": "global"},
        {"render_text": ["description"]},
        {"render_text": ["", "ptc-child"]},
        {"render_text": ["description", "ptc-other"]},
    ],
)
def test_invalid_ui_binding_rejected(patch):
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection({**binding, **patch}, "ALIYUN::NAS::ProtocolService", "ptc-child")


def test_other_source_cannot_borrow_strategy():
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection(binding, "ALIYUN::NAS::ProtocolMountTarget", "ptc-child")
