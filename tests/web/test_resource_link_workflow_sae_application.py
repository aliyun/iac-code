"""Offline signed SAE metadata binding; no account, browser or network."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_sae_application_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def specimen():
    identifier = "11111111-2222-3333-4444-555555555555"
    binding = {
        "console_inspection": "sae_application_metadata",
        "account": "1234567890123456",
        "region": "cn-shenzhen",
        "render_text": ["testdesc", "desc"],
    }
    inspection = console_inspection(binding, "ALIYUN::SAE::Application", identifier)
    reference = {
        "resourceType": "ALIYUN::SAE::Application",
        "identifier": identifier,
        "regionId": "cn-shenzhen",
        "attributes": {"AppId": identifier, "AppName": "testdesc"},
        "sources": [
            {
                "toolUseId": "offline-call",
                "evidence": "api_response",
                "sourceKind": "tool_result",
                "product": "sae",
                "action": "ListApplications",
                "requestRegionId": "cn-shenzhen",
                "sourcePath": "/result/Data/Applications/0/AppId",
            }
        ],
    }
    row = {"AppId": identifier, "AppName": "testdesc", "AppDescription": "desc", "RegionId": "cn-shenzhen"}
    raw = {
        "Code": 200,
        "Success": True,
        "RequestId": "offline-request",
        "Data": {"Applications": [row], "TotalSize": 1, "CurrentPage": 1, "PageSize": 10},
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "sae",
            "action": "ListApplications",
            "version": "2019-05-06",
            "region_id": "cn-shenzhen",
            "params": {"FieldType": "appIds", "FieldValue": identifier, "CurrentPage": 1, "PageSize": 10},
        },
        "results": [{"toolUseId": "offline-call", "isError": False, "content": json.dumps(raw)}],
    }
    return binding, inspection, reference, tool, raw


def test_exact_signed_bounded_query_metadata():
    _, inspection, reference, tool, _ = specimen()
    result = bind_sae_application_source(inspection, [reference], {"offline-call": tool})
    assert result["source_tool_use_id"] == "offline-call"


@pytest.mark.parametrize(
    "damage",
    [
        "name",
        "description",
        "id",
        "region",
        "duplicate",
        "total",
        "page",
        "error",
        "failed",
        "request",
        "scope",
        "version",
        "action",
        "product",
        "pointer",
        "model",
        "owner",
        "unfinished",
        "ref-name",
        "ref-id",
        "ref-type",
    ],
)
def test_wrong_or_ambiguous_source_rejected(damage):
    _, inspection, reference, tool, raw = specimen()
    if damage in {"name", "description", "id", "region"}:
        raw["Data"]["Applications"][0][
            {"name": "AppName", "description": "AppDescription", "id": "AppId", "region": "RegionId"}[damage]
        ] = "wrong"
    elif damage == "duplicate":
        raw["Data"]["Applications"].append(copy.deepcopy(raw["Data"]["Applications"][0]))
    elif damage in {"total", "page"}:
        raw["Data"][{"total": "TotalSize", "page": "CurrentPage"}[damage]] = 2
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "failed":
        raw["Success"] = False
    elif damage == "request":
        raw.pop("RequestId")
    elif damage == "scope":
        tool["input"]["params"]["FieldValue"] = "other"
    elif damage in {"version", "action", "product"}:
        tool["input"][damage] = "other"
    elif damage == "pointer":
        reference["sources"][0]["sourcePath"] = "/result/Data/Applications/1/AppId"
    elif damage == "model":
        reference["sources"][0]["sourceKind"] = "model"
    elif damage == "owner":
        tool["results"][0]["toolUseId"] = "other"
    elif damage == "unfinished":
        tool["status"] = "running"
    elif damage in {"ref-name", "ref-id"}:
        reference["attributes"][{"ref-name": "AppName", "ref-id": "AppId"}[damage]] = "other"
    else:
        reference["resourceType"] = "ALIYUN::SAE::Namespace"
    tool["results"][0]["content"] = json.dumps(raw)
    with pytest.raises(WorkflowError):
        bind_sae_application_source(inspection, [reference], {"offline-call": tool})


@pytest.mark.parametrize(
    "patch",
    [{"account": "other"}, {"region": "global"}, {"render_text": ["testdesc"]}, {"render_text": ["testdesc", ""]}],
)
def test_binding_must_preserve_fields_account_and_region(patch):
    binding, inspection, *_ = specimen()
    with pytest.raises(WorkflowError):
        console_inspection({**binding, **patch}, inspection["resource_type"], inspection["identifier"])


def test_namespace_or_tf_cannot_borrow_query_inspection():
    binding, inspection, *_ = specimen()
    for source_type in ("ALIYUN::SAE::Namespace", "alicloud_sae_application"):
        with pytest.raises(WorkflowError):
            console_inspection(binding, source_type, inspection["identifier"])
