"""Offline exact ScheduledTask row binding; no credentials or cloud calls."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import bind_ess_scheduled_task_source, console_inspection


def fixture():
    binding = {
        "console_inspection": "ess_scheduled_task_metadata",
        "region": "cn-hangzhou",
        "render_text": ["offline-task", "asg-offline"],
    }
    inspection = console_inspection(binding, "ALIYUN::ESS::ScheduledTask", "task123")
    reference = {
        "resourceType": "ALIYUN::ESS::ScheduledTask",
        "identifier": "task123",
        "regionId": "cn-hangzhou",
        "attributes": {"ScheduledTaskName": "offline-task"},
        "sources": [
            {
                "toolUseId": "offline-call",
                "evidence": "api_response",
                "product": "ess",
                "action": "DescribeScheduledTasks",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }
    row = {"ScheduledTaskId": "task123", "ScheduledTaskName": "offline-task", "ScalingGroupId": "asg-offline"}
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "ess",
            "action": "DescribeScheduledTasks",
            "version": "2014-08-28",
            "region_id": "cn-hangzhou",
            "params": {"RegionId": "cn-hangzhou", "PageNumber": 1, "PageSize": 10},
        },
        "results": [
            {
                "toolUseId": "offline-call",
                "isError": False,
                "content": json.dumps({"ScheduledTasks": {"ScheduledTask": [row]}}),
            }
        ],
    }
    return binding, inspection, reference, tool, row


def test_group_is_proven_by_same_actual_api_row_not_invented_locator_attributes():
    _, inspection, reference, tool, _ = fixture()
    assert "ScalingGroupId" not in reference["attributes"]
    assert bind_ess_scheduled_task_source(inspection, [reference], {"offline-call": tool}) == {
        **inspection,
        "source_tool_use_id": "offline-call",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("region", "us-east-1"),
        ("region", None),
        ("name_prefix", "offline-task"),
        ("render_text", None),
        ("render_text", []),
        ("render_text", ["offline-task"]),
        ("render_text", ["", "asg-offline"]),
        ("render_text", ["task123", "asg-offline"]),
        ("render_text", ["offline-task", "asr-rule"]),
        ("render_text", ["offline-task", "asg-offline", "extra"]),
    ],
)
def test_full_group_and_reviewed_domestic_scope_are_mandatory(field, value):
    binding, *_ = fixture()
    binding[field] = value
    with pytest.raises(WorkflowError, match="ESS task inspection"):
        console_inspection(binding, "ALIYUN::ESS::ScheduledTask", "task123")


@pytest.mark.parametrize(
    "damage",
    [
        "reference_type",
        "reference_region",
        "reference_id",
        "reference_name",
        "source_action",
        "source_kind",
        "source_region",
        "source_tool",
        "source_product",
        "tool_status",
        "tool_name",
        "request_action",
        "request_product",
        "request_region",
        "request_version",
        "params_region",
        "result_error",
        "result_tool",
        "invalid_json",
        "duplicate_reference",
        "duplicate_row",
        "split_rows",
        "row_id",
        "row_name",
        "row_group",
    ],
)
def test_unrelated_or_ambiguous_source_cannot_authorize_the_native_task_lookup(damage):
    _, inspection, reference, tool, row = fixture()
    refs = [reference]
    if damage.startswith("reference_"):
        key = {"reference_type": "resourceType", "reference_region": "regionId", "reference_id": "identifier"}
        if damage == "reference_name":
            reference["attributes"]["ScheduledTaskName"] = "other"
        else:
            reference[key[damage]] = "other"
    elif damage.startswith("source_"):
        key = {
            "source_action": "action",
            "source_kind": "evidence",
            "source_region": "requestRegionId",
            "source_tool": "toolUseId",
            "source_product": "product",
        }[damage]
        reference["sources"][0][key] = "other"
    elif damage in {"tool_status", "tool_name"}:
        tool["status" if damage == "tool_status" else "toolName"] = "other"
    elif damage.startswith("request_"):
        tool["input"][damage.removeprefix("request_").replace("region", "region_id")] = "other"
    elif damage == "params_region":
        tool["input"]["params"]["RegionId"] = "cn-beijing"
    elif damage in {"result_error", "result_tool"}:
        tool["results"][0]["isError" if damage == "result_error" else "toolUseId"] = True
    elif damage == "invalid_json":
        tool["results"][0]["content"] = "invalid"
    elif damage == "duplicate_reference":
        refs.append(copy.deepcopy(reference))
    else:
        rows = [row]
        if damage == "duplicate_row":
            rows.append(copy.deepcopy(row))
        elif damage == "split_rows":
            rows.append({**row, "ScheduledTaskId": "other"})
            row["ScalingGroupId"] = "asg-other"
        else:
            row[
                {"row_id": "ScheduledTaskId", "row_name": "ScheduledTaskName", "row_group": "ScalingGroupId"}[damage]
            ] = "other"
        tool["results"][0]["content"] = json.dumps({"ScheduledTasks": {"ScheduledTask": rows}})
    with pytest.raises(WorkflowError, match="signed product source row"):
        bind_ess_scheduled_task_source(inspection, refs, {"offline-call": tool})
