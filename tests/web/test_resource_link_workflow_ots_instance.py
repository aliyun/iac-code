"""Offline binding tests; no accounts, browser, cloud or local configuration."""

import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_ots_instance_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def fixture():
    identifier = "offline-instance"
    endpoint = f"https://{identifier}.cn-hangzhou.ots.aliyuncs.com"
    binding = {
        "console_inspection": "ots_instance_endpoint",
        "region": "cn-hangzhou",
        "render_text": [identifier, endpoint],
    }
    inspection = console_inspection(binding, "ALIYUN::OTS::Instance", identifier)
    source = {
        "toolUseId": "offline-call",
        "evidence": "api_response",
        "sourceKind": "tool_result",
        "product": "ros",
        "action": "GetStackResource",
        "requestRegionId": "cn-hangzhou",
        "stackId": "offline-stack",
        "logicalResourceId": "Instance",
    }
    ref = {
        "resourceType": "ALIYUN::OTS::Instance",
        "identifier": identifier,
        "regionId": "cn-hangzhou",
        "attributes": {"InstanceName": identifier},
        "stackId": "offline-stack",
        "logicalResourceId": "Instance",
        "sources": [source],
    }
    row = {
        "ResourceType": "ALIYUN::OTS::Instance",
        "PhysicalResourceId": identifier,
        "StackId": "offline-stack",
        "LogicalResourceId": "Instance",
        "Status": "CREATE_COMPLETE",
        "ResourceAttributes": [
            {"ResourceAttributeKey": "InstanceName", "ResourceAttributeValue": identifier},
            {"ResourceAttributeKey": "PublicEndpoint", "ResourceAttributeValue": endpoint},
        ],
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "ros",
            "action": "GetStackResource",
            "version": "2019-09-10",
            "region_id": "cn-hangzhou",
            "params": {
                "StackId": "offline-stack",
                "LogicalResourceId": "Instance",
                "ShowResourceAttributes": True,
                "RegionId": "cn-hangzhou",
            },
        },
        "results": [{"isError": False, "content": json.dumps(row)}],
    }
    return inspection, ref, tool, row


def test_own_original_ros_attributes_bound_without_inventing_locator_fields():
    inspection, ref, tool, _ = fixture()
    assert bind_ots_instance_source(inspection, [ref], {"offline-call": tool})["source_tool_use_id"] == "offline-call"
    assert "PublicEndpoint" not in ref["attributes"]


@pytest.mark.parametrize("field", ["PhysicalResourceId", "StackId", "LogicalResourceId", "ResourceType", "Status"])
def test_wrong_child_or_stack_rejected(field):
    inspection, ref, tool, row = fixture()
    row[field] = "wrong"
    tool["results"][0]["content"] = json.dumps(row)
    with pytest.raises(WorkflowError, match="own signed"):
        bind_ots_instance_source(inspection, [ref], {"offline-call": tool})


@pytest.mark.parametrize(
    "damage",
    [
        "endpoint",
        "duplicate_attribute",
        "error",
        "source_kind",
        "region",
        "request_stack",
        "show_attributes",
        "version",
        "duplicate_reference",
    ],
)
def test_unproven_or_ambiguous_source_rejected(damage):
    inspection, ref, tool, row = fixture()
    refs = [ref]
    if damage == "endpoint":
        row["ResourceAttributes"][1]["ResourceAttributeValue"] = "https://other.example"
    elif damage == "duplicate_attribute":
        row["ResourceAttributes"].append(row["ResourceAttributes"][1])
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "source_kind":
        ref["sources"][0]["sourceKind"] = "model"
    elif damage == "region":
        ref["regionId"] = "cn-beijing"
    elif damage == "request_stack":
        tool["input"]["params"]["StackId"] = "other"
    elif damage == "show_attributes":
        tool["input"]["params"]["ShowResourceAttributes"] = False
    elif damage == "version":
        tool["input"]["version"] = "wrong"
    else:
        refs.append(ref)
    tool["results"][0]["content"] = json.dumps(row)
    with pytest.raises(WorkflowError):
        bind_ots_instance_source(inspection, refs, {"offline-call": tool})


@pytest.mark.parametrize("source_type", ["alicloud_ots_instance", "ALIYUN::OTS::Table"])
def test_other_sources_do_not_gain_ros_inspection(source_type):
    with pytest.raises(WorkflowError):
        console_inspection(
            {
                "console_inspection": "ots_instance_endpoint",
                "region": "cn-hangzhou",
                "render_text": ["offline-instance", "https://other"],
            },
            source_type,
            "offline-instance",
        )
