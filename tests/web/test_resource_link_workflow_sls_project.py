"""Offline exact signed project-row bindings for native SLS title capture."""

import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_sls_project_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def fixture():
    binding = {
        "console_inspection": "sls_project_metadata",
        "region": "cn-hangzhou",
        "account": "1234567890123456",
        "render_text": ["offline-project", "offline-description"],
    }
    inspection = console_inspection(binding, "alicloud_log_project", "offline-project")
    ref = {
        "provider": "aliyun",
        "consolePartition": "aliyun",
        "resourceType": "alicloud_log_project",
        "identifier": "offline-project",
        "scopeKind": "account",
        "accountScope": "offline-signed",
        "attributes": {"projectName": "offline-project"},
        "sources": [
            {
                "toolUseId": "offline-call",
                "product": "sls",
                "action": "ListProject",
                "sourcePath": "/result/projects/0/projectName",
                "requestRegionId": "cn-hangzhou",
                "evidence": "api_response",
                "sourceKind": "tool_result",
            }
        ],
    }
    row = {
        "projectName": "offline-project",
        "description": "offline-description",
        "region": "cn-hangzhou",
        "owner": "1234567890123456",
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "sls",
            "action": "ListProject",
            "version": "2020-12-30",
            "region_id": "cn-hangzhou",
            "params": {"projectName": "offline-project", "offset": 0, "size": 100},
        },
        "results": [{"toolUseId": "offline-call", "isError": False, "content": json.dumps({"projects": [row]})}],
    }
    return binding, inspection, ref, tool, row


@pytest.mark.parametrize("resource_type", ["ALIYUN::SLS::Project", "alicloud_log_project"])
def test_current_own_signed_alias_row_supplies_full_tooltip_fields(resource_type):
    binding, _, ref, tool, _ = fixture()
    ref["resourceType"] = resource_type
    inspection = console_inspection(binding, resource_type, "offline-project")
    assert bind_sls_project_source(inspection, [ref], {"offline-call": tool})["source_tool_use_id"] == "offline-call"
    assert "description" not in ref["attributes"]


@pytest.mark.parametrize("field", ["projectName", "description", "region", "owner"])
def test_fields_from_another_project_or_account_rejected(field):
    _, inspection, ref, tool, row = fixture()
    row[field] = "other"
    tool["results"][0]["content"] = json.dumps({"projects": [row]})
    with pytest.raises(WorkflowError, match="own signed"):
        bind_sls_project_source(inspection, [ref], {"offline-call": tool})


@pytest.mark.parametrize(
    "damage",
    [
        "version",
        "action",
        "region",
        "filter",
        "tool",
        "result-owner",
        "error",
        "source-path",
        "scope",
        "provider",
        "partition",
        "duplicate",
        "duplicate-row",
        "source-kind",
        "source-region",
        "json",
        "index",
        "code",
    ],
)
def test_ambiguous_or_wrong_provenance_rejected(damage):
    _, inspection, ref, tool, row = fixture()
    refs = [ref]
    if damage in {"version", "action", "region"}:
        tool["input"][{"region": "region_id"}.get(damage, damage)] = "wrong"
    elif damage == "filter":
        tool["input"]["params"]["projectName"] = "other"
    elif damage == "tool":
        tool["toolName"] = "other"
    elif damage == "result-owner":
        tool["results"][0]["toolUseId"] = "other"
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "source-path":
        ref["sources"][0]["sourcePath"] = "/result/projects/0/description"
    elif damage == "scope":
        ref["regionId"] = "cn-hangzhou"
    elif damage in {"provider", "partition"}:
        ref[{"partition": "consolePartition"}.get(damage, damage)] = "other"
    elif damage == "duplicate":
        refs.append(ref)
    elif damage == "duplicate-row":
        tool["results"][0]["content"] = json.dumps({"projects": [row, row]})
    elif damage == "source-kind":
        ref["sources"][0]["sourceKind"] = "model"
    elif damage == "source-region":
        ref["sources"][0]["requestRegionId"] = "cn-beijing"
    elif damage == "json":
        tool["results"][0]["content"] = "not-json"
    elif damage == "index":
        ref["sources"][0]["sourcePath"] = "/result/projects/1/projectName"
    elif damage == "code":
        tool["results"][0]["content"] = json.dumps({"Code": "Denied", "projects": [row]})
    with pytest.raises(WorkflowError):
        bind_sls_project_source(inspection, refs, {"offline-call": tool})


@pytest.mark.parametrize(
    "patch",
    [
        {"account": "wrong"},
        {"region": "global"},
        {"region": "cn-beijing"},
        {"render_text": ["offline-project"]},
        {"render_text": ["other", "description"]},
        {"render_text": ["offline-project", ""]},
    ],
)
def test_invalid_tooltip_binding_rejected(patch):
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection({**binding, **patch}, "alicloud_log_project", "offline-project")


def test_other_resource_cannot_borrow_sls_inspection():
    binding, _, _, _, _ = fixture()
    with pytest.raises(WorkflowError):
        console_inspection(binding, "ALIYUN::OSS::Bucket", "offline-project")
