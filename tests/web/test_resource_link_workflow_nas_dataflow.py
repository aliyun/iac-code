"""Offline same-source DataFlow metadata capture contracts."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import bind_nas_dataflow_source
from scripts.web.e2e.resource_link_workflow.files import WorkflowError
from scripts.web.e2e.resource_link_workflow.runtime import console_inspection


def specimen():
    binding = {
        "console_inspection": "nas_dataflow_metadata",
        "file_system_id": "cpfs-own",
        "fileset_id": "fset-own",
        "region": "cn-hangzhou",
        "render_text": ["df-own", "/own/", "oss://offline-bucket"],
    }
    inspection = console_inspection(binding, "ALIYUN::NAS::DataFlow", "df-own")
    ref = {
        "resourceType": "ALIYUN::NAS::DataFlow",
        "identifier": "df-own",
        "regionId": "cn-hangzhou",
        "sources": [
            {
                "toolUseId": "own",
                "product": "nas",
                "action": "DescribeDataFlows",
                "evidence": "api_response",
                "requestRegionId": "cn-hangzhou",
                "sourcePath": "/result/DataFlowInfo/DataFlow/0/DataFlowId",
            }
        ],
    }
    raw = {
        "RequestId": "offline",
        "NextToken": "",
        "DataFlowInfo": {
            "DataFlow": [
                {
                    "DataFlowId": "df-own",
                    "FileSystemId": "cpfs-own",
                    "FsetId": "fset-own",
                    "FileSystemPath": "/own/",
                    "SourceStorage": "oss://offline-bucket",
                    "Status": "Running",
                    "ErrorMessage": "",
                }
            ]
        },
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "nas",
            "action": "DescribeDataFlows",
            "version": "2017-06-26",
            "region_id": "cn-hangzhou",
            "params": {
                "FileSystemId": "cpfs-own",
                "MaxResults": 20,
                "Filters.1.Key": "DataFlowIds",
                "Filters.1.Value": "df-own",
            },
        },
        "results": [{"toolUseId": "own", "isError": False, "content": json.dumps(raw)}],
    }
    return binding, inspection, ref, tool, raw


def test_exact_same_query_native_metadata():
    _, inspection, ref, tool, _ = specimen()
    assert bind_nas_dataflow_source(inspection, [ref], {"own": tool})["source_tool_use_id"] == "own"


@pytest.mark.parametrize(
    "damage",
    [
        "DataFlowId",
        "FileSystemId",
        "FsetId",
        "FileSystemPath",
        "SourceStorage",
        "Status",
        "ErrorMessage",
        "duplicate",
        "page",
        "request",
        "error",
        "unfinished",
        "pointer",
        "scope",
        "version",
        "region",
        "owner",
    ],
)
def test_changed_or_foreign_source_rejected(damage):
    _, inspection, ref, tool, raw = specimen()
    if damage in raw["DataFlowInfo"]["DataFlow"][0]:
        raw["DataFlowInfo"]["DataFlow"][0][damage] = "wrong"
    elif damage == "duplicate":
        raw["DataFlowInfo"]["DataFlow"].append(copy.deepcopy(raw["DataFlowInfo"]["DataFlow"][0]))
    elif damage == "page":
        raw["NextToken"] = "next"
    elif damage == "request":
        raw.pop("RequestId")
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "unfinished":
        tool["status"] = "running"
    elif damage == "pointer":
        ref["sources"][0]["sourcePath"] = "/other"
    elif damage == "scope":
        tool["input"]["params"]["Filters.1.Value"] = "df-other"
    elif damage == "version":
        tool["input"]["version"] = "other"
    elif damage == "region":
        ref["regionId"] = "cn-beijing"
    else:
        tool["results"][0]["toolUseId"] = "other"
    tool["results"][0]["content"] = json.dumps(raw)
    with pytest.raises(WorkflowError):
        bind_nas_dataflow_source(inspection, [ref], {"own": tool})


@pytest.mark.parametrize(
    "patch",
    [
        {"file_system_id": "other"},
        {"fileset_id": "other"},
        {"region": "global"},
        {"render_text": ["df-own"]},
        {"render_text": ["df-own", "/own/", "https://example.com"]},
    ],
)
def test_binding_does_not_guess_parent_or_weaken_fields(patch):
    binding, *_ = specimen()
    with pytest.raises(WorkflowError):
        console_inspection({**binding, **patch}, "ALIYUN::NAS::DataFlow", "df-own")
