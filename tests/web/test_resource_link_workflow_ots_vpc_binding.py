"""Offline, exact signed provenance for native OTS binding-row capture."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_ots_vpc_binding_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def fixture():
    binding = {
        "console_inspection": "ots_vpc_binding_row",
        "instance_name": "offline-parent",
        "region": "cn-hangzhou",
        "render_text": ["offline-binding", "vpc-offline"],
    }
    inspection = console_inspection(binding, "ALIYUN::OTS::VpcBinder", "offline-binding")
    attributes = {
        "InstanceName": "offline-parent",
        "InstanceVpcName": "offline-binding",
        "VpcId": "vpc-offline",
        "RegionNo": "cn-hangzhou",
    }
    reference = {
        "resourceType": "ALIYUN::OTS::VpcBinder",
        "identifier": "offline-binding",
        "regionId": "cn-hangzhou",
        "attributes": attributes,
        "sources": [
            {
                "toolUseId": "offline-call",
                "product": "tablestore",
                "action": "ListVpcInfoByInstance",
                "requestRegionId": "cn-hangzhou",
                "evidence": "api_response",
                "sourceKind": "tool_result",
            }
        ],
    }
    row = {k: v for k, v in attributes.items() if k != "InstanceName"}
    raw = {"InstanceName": "offline-parent", "VpcList": [row]}
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "tablestore",
            "action": "ListVpcInfoByInstance",
            "version": "2020-12-09",
            "region_id": "cn-hangzhou",
            "params": {"InstanceName": "offline-parent", "PageNum": 1, "PageSize": 20},
        },
        "results": [{"isError": False, "content": json.dumps(raw)}],
    }
    return binding, inspection, reference, tool, raw


def test_same_signed_parent_and_unique_binding_required():
    _, inspection, reference, tool, _ = fixture()
    result = bind_ots_vpc_binding_source(inspection, [reference], {"offline-call": tool})
    assert result["source_tool_use_id"] == "offline-call"


@pytest.mark.parametrize("field", ["InstanceName", "InstanceVpcName", "VpcId", "RegionNo"])
def test_original_reference_fields_must_agree(field):
    _, inspection, reference, tool, _ = fixture()
    reference["attributes"][field] = "wrong"
    with pytest.raises(WorkflowError):
        bind_ots_vpc_binding_source(inspection, [reference], {"offline-call": tool})


@pytest.mark.parametrize(
    "damage",
    [
        "parent",
        "name",
        "vpc",
        "region",
        "duplicate",
        "version",
        "product",
        "action",
        "error",
        "unfinished",
        "tool-owner",
        "model",
        "missing",
    ],
)
def test_invalid_raw_or_source_binding_rejected(damage):
    _, inspection, reference, tool, raw = fixture()
    if damage == "parent":
        raw["InstanceName"] = "other-parent"
    elif damage in {"name", "vpc", "region"}:
        raw["VpcList"][0][{"name": "InstanceVpcName", "vpc": "VpcId", "region": "RegionNo"}[damage]] = "other"
    elif damage == "duplicate":
        raw["VpcList"].append(copy.deepcopy(raw["VpcList"][0]))
    elif damage in {"version", "product", "action"}:
        tool["input"][damage] = "other"
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "unfinished":
        tool["status"] = "running"
    elif damage == "tool-owner":
        tool["results"][0]["toolUseId"] = "other-call"
    elif damage == "model":
        reference["sources"][0]["sourceKind"] = "model"
    elif damage == "missing":
        raw = {}
    tool["results"][0]["content"] = json.dumps(raw)
    with pytest.raises(WorkflowError):
        bind_ots_vpc_binding_source(inspection, [reference], {"offline-call": tool})


@pytest.mark.parametrize(
    "patch",
    [
        {"instance_name": "https://other"},
        {"region": "global"},
        {"render_text": ["offline-binding"]},
        {"render_text": ["other", "vpc-offline"]},
        {"render_text": ["offline-binding", "bad-vpc"]},
    ],
)
def test_invalid_packet_binding_rejected(patch):
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection({**binding, **patch}, "ALIYUN::OTS::VpcBinder", "offline-binding")


def test_other_resource_cannot_borrow_inspector():
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection(binding, "ALIYUN::OTS::Instance", "offline-binding")
