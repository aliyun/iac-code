"""Offline own-row bindings for native Fileset description tooltip capture."""

import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_nas_fileset_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def fixture():
    binding = {
        "console_inspection": "nas_fileset_description",
        "region": "cn-hangzhou",
        "file_system_id": "cpfs-parent",
        "render_text": ["offline-description", "/offline/"],
    }
    inspection = console_inspection(binding, "ALIYUN::NAS::Fileset", "fset-child")
    ref = {
        "resourceType": "ALIYUN::NAS::Fileset",
        "identifier": "fset-child",
        "regionId": "cn-hangzhou",
        "attributes": {"FileSystemId": "cpfs-parent", "FsetId": "fset-child"},
        "sources": [
            {
                "toolUseId": "offline-call",
                "product": "nas",
                "action": "DescribeFilesets",
                "requestRegionId": "cn-hangzhou",
                "evidence": "api_response",
                "sourceKind": "tool_result",
            }
        ],
    }
    row = {
        "FileSystemId": "cpfs-parent",
        "FsetId": "fset-child",
        "Description": "offline-description",
        "FileSystemPath": "/offline/",
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "nas",
            "action": "DescribeFilesets",
            "version": "2017-06-26",
            "region_id": "cn-hangzhou",
            "params": {"FileSystemId": "cpfs-parent", "MaxResults": 20},
        },
        "results": [{"isError": False, "content": json.dumps({"Entries": {"Entrie": [row]}})}],
    }
    return binding, inspection, ref, tool, row


def test_full_description_bound_from_own_response_without_manufacturing_reference_attributes():
    _, inspection, ref, tool, _ = fixture()
    assert bind_nas_fileset_source(inspection, [ref], {"offline-call": tool})["source_tool_use_id"] == "offline-call"
    assert "Description" not in ref["attributes"]


@pytest.mark.parametrize("field", ["FileSystemId", "FsetId", "Description", "FileSystemPath"])
def test_wrong_or_split_child_fields_rejected(field):
    _, inspection, ref, tool, row = fixture()
    row[field] = "wrong"
    tool["results"][0]["content"] = json.dumps({"Entries": {"Entrie": [row]}})
    with pytest.raises(WorkflowError, match="own signed"):
        bind_nas_fileset_source(inspection, [ref], {"offline-call": tool})


@pytest.mark.parametrize("damage", ["parent", "region", "version", "error", "duplicate", "kind", "tool", "row"])
def test_wrong_or_ambiguous_provenance_rejected(damage):
    _, inspection, ref, tool, row = fixture()
    refs = [ref]
    if damage == "parent":
        tool["input"]["params"]["FileSystemId"] = "cpfs-other"
    elif damage == "region":
        ref["regionId"] = "cn-beijing"
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
        tool["results"][0]["content"] = json.dumps({"Entries": {"Entrie": [row, row]}})
    with pytest.raises(WorkflowError):
        bind_nas_fileset_source(inspection, refs, {"offline-call": tool})


@pytest.mark.parametrize(
    "patch",
    [
        {"file_system_id": "other"},
        {"region": "global"},
        {"render_text": ["description"]},
        {"render_text": ["", "/test/"]},
        {"render_text": ["description", "relative"]},
    ],
)
def test_invalid_ui_binding_rejected(patch):
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection({**binding, **patch}, "ALIYUN::NAS::Fileset", "fset-child")


def test_other_source_cannot_borrow_fileset_strategy():
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection(binding, "alicloud_nas_file_system", "fset-child")
