"""Offline own-parent child source binding; never accesses a live account."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import bind_router_interface_source, console_inspection


def source():
    binding = {
        "console_inspection": "router_interface_vbr_child",
        "region": "cn-hangzhou",
        "parent_router_id": "vbr-own",
        "render_text": ["offline-child", "vpc-peer"],
    }
    inspection = console_inspection(binding, "ALIYUN::VPC::RouterInterface", "ri-child")
    reference = {
        "resourceType": "ALIYUN::VPC::RouterInterface",
        "identifier": "ri-child",
        "regionId": "cn-hangzhou",
        "attributes": {"RouterId": "vbr-own", "RouterType": "VBR"},
        "sources": [
            {
                "toolUseId": "offline-query",
                "evidence": "api_response",
                "product": "vpc",
                "action": "DescribeRouterInterfaces",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }
    row = {
        "RouterInterfaceId": "ri-child",
        "RouterType": "VBR",
        "RouterId": "vbr-own",
        "Name": "offline-child",
        "OppositeRouterId": "vrt-peer",
        "OppositeVpcInstanceId": "vpc-peer",
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "vpc",
            "action": "DescribeRouterInterfaces",
            "region_id": "cn-hangzhou",
            "params": {"RegionId": "cn-hangzhou", "PageNumber": 1, "PageSize": 10},
        },
        "results": [
            {
                "toolUseId": "offline-query",
                "isError": False,
                "content": json.dumps({"RouterInterfaceSet": {"RouterInterfaceType": [row]}}),
            }
        ],
    }
    return binding, inspection, reference, tool, row


def test_child_markers_come_from_one_original_api_row_not_manufactured_locator_attributes():
    _, inspection, reference, tool, _ = source()
    assert "Name" not in reference["attributes"]
    assert bind_router_interface_source(inspection, [reference], {"offline-query": tool}) == {
        **inspection,
        "source_tool_use_id": "offline-query",
    }


@pytest.mark.parametrize("wrong_vpc", [None, "vpc-other"])
def test_opposite_router_id_cannot_substitute_for_opposite_vpc_instance_id(wrong_vpc):
    _, inspection, reference, tool, row = source()
    row["OppositeRouterId"] = inspection["peer_vpc"]
    if wrong_vpc is None:
        row.pop("OppositeVpcInstanceId")
    else:
        row["OppositeVpcInstanceId"] = wrong_vpc
    tool["results"][0]["content"] = json.dumps({"RouterInterfaceSet": {"RouterInterfaceType": [row]}})
    with pytest.raises(WorkflowError, match="signed product source row"):
        bind_router_interface_source(inspection, [reference], {"offline-query": tool})


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_router_id", "vpc-peer"),
        ("parent_router_id", "vbr-other"),
        ("region", "us-west-1"),
        ("name_prefix", "offline-child"),
        ("render_text", []),
        ("render_text", ["ri-child", "vpc-peer"]),
        ("render_text", ["offline-child", "vbr-peer"]),
        ("render_text", ["", "vpc-peer"]),
    ],
)
def test_child_inspection_requires_reviewed_complete_binding(field, value):
    binding, *_ = source()
    binding[field] = value
    # A syntactically valid but wrong own-parent is rejected by source binding,
    # not this shape-only parser.
    if value == "vbr-other":
        _, _, reference, tool, _ = source()
        inspection = console_inspection(binding, "ALIYUN::VPC::RouterInterface", "ri-child")
        with pytest.raises(WorkflowError, match="signed product source row"):
            bind_router_interface_source(inspection, [reference], {"offline-query": tool})
    else:
        with pytest.raises(WorkflowError, match="RouterInterface inspection"):
            console_inspection(binding, "ALIYUN::VPC::RouterInterface", "ri-child")


@pytest.mark.parametrize(
    "damage",
    [
        "reference_parent",
        "reference_type",
        "reference_region",
        "source_action",
        "source_region",
        "source_kind",
        "source_tool",
        "tool_status",
        "tool_name",
        "request_action",
        "request_region",
        "params_region",
        "result_error",
        "result_tool",
        "invalid_json",
        "duplicate",
        "split_rows",
        "row_parent",
        "row_child",
        "row_name",
        "row_peer",
        "row_router_type",
    ],
)
def test_wrong_or_ambiguous_source_cannot_authorize_native_child_lookup(damage):
    _, inspection, reference, tool, row = source()
    refs = [reference]
    if damage == "reference_parent":
        reference["attributes"]["RouterId"] = "vbr-other"
    elif damage == "reference_type":
        reference["resourceType"] = "ALIYUN::VPC::VRouter"
    elif damage == "reference_region":
        reference["regionId"] = "cn-beijing"
    elif damage.startswith("source_"):
        key = {
            "source_action": "action",
            "source_region": "requestRegionId",
            "source_kind": "evidence",
            "source_tool": "toolUseId",
        }[damage]
        reference["sources"][0][key] = "other"
    elif damage in {"tool_status", "tool_name"}:
        tool["status" if damage == "tool_status" else "toolName"] = "other"
    elif damage in {"request_action", "request_region"}:
        tool["input"]["action" if damage == "request_action" else "region_id"] = "other"
    elif damage == "params_region":
        tool["input"]["params"]["RegionId"] = "cn-beijing"
    elif damage in {"result_error", "result_tool"}:
        tool["results"][0]["isError" if damage == "result_error" else "toolUseId"] = True
    elif damage == "invalid_json":
        tool["results"][0]["content"] = "invalid"
    elif damage == "duplicate":
        refs.append(copy.deepcopy(reference))
    else:
        if damage == "split_rows":
            other = {**row, "RouterInterfaceId": "ri-other"}
            row["Name"] = "wrong"
            rows = [row, other]
        else:
            key = {
                "row_parent": "RouterId",
                "row_child": "RouterInterfaceId",
                "row_name": "Name",
                "row_peer": "OppositeVpcInstanceId",
                "row_router_type": "RouterType",
            }[damage]
            row[key] = "other"
            rows = [row]
        tool["results"][0]["content"] = json.dumps({"RouterInterfaceSet": {"RouterInterfaceType": rows}})
    with pytest.raises(WorkflowError, match="signed product source row"):
        bind_router_interface_source(inspection, refs, {"offline-query": tool})
