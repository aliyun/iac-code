"""StateConfiguration capture uses a fresh exact bounded signed row, never the seed."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_oos_state_configuration_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def fixture():
    row = {"StateConfigurationId": "sc-offline", "Description": "fixture description", "TemplateName": "ACS-Fixture"}
    binding = {
        "console_inspection": "oos_state_configuration_list",
        "account": "1234567890123456",
        "region": "cn-hangzhou",
        "render_text": [row["StateConfigurationId"]],
        "rationale": {"signed_source_required_fields": copy.deepcopy(row)},
    }
    inspection = console_inspection(binding, "ALIYUN::OOS::StateConfiguration", row["StateConfigurationId"])
    reference = {
        "resourceType": "ALIYUN::OOS::StateConfiguration",
        "identifier": "sc-offline",
        "regionId": "cn-hangzhou",
        "sources": [
            {
                "toolUseId": "offline-call",
                "evidence": "api_response",
                "product": "oos",
                "action": "ListStateConfigurations",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "oos",
            "action": "ListStateConfigurations",
            "version": "2019-06-01",
            "region_id": "cn-hangzhou",
            "params": {"RegionId": "cn-hangzhou", "MaxResults": 10},
        },
        "results": [
            {"isError": False, "toolUseId": "offline-call", "content": json.dumps({"StateConfigurations": [row]})}
        ],
    }
    return binding, inspection, reference, tool, row


def test_metadata_and_full_native_id_are_bound_to_the_same_fresh_row():
    _, inspection, reference, tool, _ = fixture()
    assert bind_oos_state_configuration_source(inspection, [reference], {"offline-call": tool}) == {
        **inspection,
        "source_tool_use_id": "offline-call",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("account", "wrong"),
        ("region", "us-east-1"),
        ("render_text", []),
        ("render_text", ["sc-other"]),
        ("rationale", {}),
    ],
)
def test_native_id_and_domestic_signed_metadata_contract_is_mandatory(field, value):
    binding, *_ = fixture()
    binding[field] = value
    with pytest.raises(WorkflowError, match="OOS state list"):
        console_inspection(binding, "ALIYUN::OOS::StateConfiguration", "sc-offline")


@pytest.mark.parametrize(
    "damage",
    [
        "type",
        "identifier",
        "region",
        "source_kind",
        "source_region",
        "source_action",
        "source_product",
        "tool_id",
        "tool_name",
        "status",
        "request_product",
        "request_action",
        "request_version",
        "request_region_id",
        "params_region",
        "params_limit",
        "params_cursor",
        "isError",
        "result_tool",
        "invalid_json",
        "row_id",
        "row_description",
        "row_template",
        "split_rows",
        "duplicate_row",
        "duplicate_reference",
    ],
)
def test_wrong_stale_unbounded_or_ambiguous_source_is_rejected(damage):
    _, inspection, reference, tool, row = fixture()
    refs = [reference]
    if damage in {"type", "identifier", "region"}:
        reference[{"type": "resourceType", "identifier": "identifier", "region": "regionId"}[damage]] = "wrong"
    elif damage.startswith("source_") or damage == "tool_id":
        key = {
            "source_kind": "evidence",
            "source_region": "requestRegionId",
            "source_action": "action",
            "source_product": "product",
            "tool_id": "toolUseId",
        }[damage]
        reference["sources"][0][key] = "wrong"
    elif damage in {"tool_name", "status"}:
        tool["toolName" if damage == "tool_name" else "status"] = "wrong"
    elif damage.startswith("request_"):
        tool["input"][damage.removeprefix("request_")] = "wrong"
    elif damage.startswith("params_"):
        tool["input"]["params"][
            {"params_region": "RegionId", "params_limit": "MaxResults", "params_cursor": "NextToken"}[damage]
        ] = "wrong"
    elif damage in {"isError", "result_tool", "invalid_json"}:
        tool["results"][0][{"isError": "isError", "result_tool": "toolUseId", "invalid_json": "content"}[damage]] = (
            "wrong"
        )
    elif damage == "duplicate_reference":
        refs.append(copy.deepcopy(reference))
    else:
        rows = [row]
        if damage == "duplicate_row":
            rows.append({**row, "Description": "conflicting duplicate"})
        elif damage == "split_rows":
            rows.append({**row, "StateConfigurationId": "sc-other"})
            row["TemplateName"] = "wrong"
        else:
            row[
                {"row_id": "StateConfigurationId", "row_description": "Description", "row_template": "TemplateName"}[
                    damage
                ]
            ] = "wrong"
        tool["results"][0]["content"] = json.dumps({"StateConfigurations": rows})
    with pytest.raises(WorkflowError, match="signed product source row"):
        bind_oos_state_configuration_source(inspection, refs, {"offline-call": tool})
