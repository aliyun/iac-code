"""OOS native disclosure binds one original successful row; no live cloud."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import bind_oos_template_source, console_inspection


def fixture():
    binding = {
        "console_inspection": "oos_template_metadata",
        "region": "cn-hangzhou",
        "render_text": ["offline-template", "offline description"],
    }
    inspection = console_inspection(binding, "ALIYUN::OOS::Template", "offline-template")
    reference = {
        "resourceType": "ALIYUN::OOS::Template",
        "identifier": "offline-template",
        "regionId": "cn-hangzhou",
        "attributes": {"TemplateName": "offline-template", "TemplateId": "t-offline"},
        "sources": [
            {
                "toolUseId": "offline-call",
                "evidence": "api_response",
                "product": "oos",
                "action": "ListTemplates",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }
    row = {"TemplateName": "offline-template", "TemplateId": "t-offline", "Description": "offline description"}
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "oos",
            "action": "ListTemplates",
            "version": "2019-06-01",
            "region_id": "cn-hangzhou",
            "params": {"RegionId": "cn-hangzhou", "ShareType": "Private", "MaxResults": 10},
        },
        "results": [{"toolUseId": "offline-call", "isError": False, "content": json.dumps({"Templates": [row]})}],
    }
    return binding, inspection, reference, tool, row


def test_description_is_proven_by_one_own_row_not_manufactured_locator_attribute():
    _, inspection, reference, tool, _ = fixture()
    assert "Description" not in reference["attributes"]
    assert bind_oos_template_source(inspection, [reference], {"offline-call": tool}) == {
        **inspection,
        "source_tool_use_id": "offline-call",
        "template_id": "t-offline",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("region", None),
        ("region", "us-east-1"),
        ("render_text", None),
        ("render_text", []),
        ("render_text", ["offline-template", ""]),
        ("render_text", ["other", "description"]),
        ("render_text", ["offline-template", "offline-template"]),
        ("name_prefix", "other"),
    ],
)
def test_precise_markers_and_domestic_scope_are_mandatory(field, value):
    binding, *_ = fixture()
    binding[field] = value
    with pytest.raises(WorkflowError, match="OOS template inspection"):
        console_inspection(binding, "ALIYUN::OOS::Template", "offline-template")


@pytest.mark.parametrize(
    "damage",
    [
        "reference_type",
        "reference_region",
        "reference_id",
        "reference_name",
        "reference_template_id",
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
        "params_share",
        "result_error",
        "result_tool",
        "invalid_json",
        "duplicate_reference",
        "duplicate_row",
        "split_rows",
        "row_id",
        "row_name",
        "row_description",
    ],
)
def test_wrong_or_ambiguous_source_never_authorizes_disclosure(damage):
    _, inspection, reference, tool, row = fixture()
    refs = [reference]
    if damage.startswith("reference_"):
        if damage in {"reference_name", "reference_template_id"}:
            reference["attributes"]["TemplateName" if damage == "reference_name" else "TemplateId"] = "other"
        else:
            reference[
                {"reference_type": "resourceType", "reference_region": "regionId", "reference_id": "identifier"}[damage]
            ] = "other"
    elif damage.startswith("source_"):
        reference["sources"][0][
            {
                "source_action": "action",
                "source_kind": "evidence",
                "source_region": "requestRegionId",
                "source_tool": "toolUseId",
                "source_product": "product",
            }[damage]
        ] = "other"
    elif damage in {"tool_status", "tool_name"}:
        tool["status" if damage == "tool_status" else "toolName"] = "other"
    elif damage.startswith("request_"):
        tool["input"][damage.removeprefix("request_").replace("region", "region_id")] = "other"
    elif damage.startswith("params_"):
        tool["input"]["params"]["RegionId" if damage == "params_region" else "ShareType"] = "other"
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
            rows.append({**row, "TemplateName": "other"})
            row["Description"] = "other"
        else:
            key = {"row_id": "TemplateId", "row_name": "TemplateName", "row_description": "Description"}[damage]
            row[key] = "other"
        tool["results"][0]["content"] = json.dumps({"Templates": rows})
    with pytest.raises(WorkflowError, match="signed product source row"):
        bind_oos_template_source(inspection, refs, {"offline-call": tool})
