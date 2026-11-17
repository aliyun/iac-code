"""Offline same-row console Name projection; never read a secret's value."""

import copy
import json
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime, navigation_identifier
from tests.web import test_resource_link_workflow as common


def fixture():
    reference = {
        "provider": "aliyun",
        "resourceType": "ALIYUN::OOS::SecretParameter",
        "identifier": "p-offline123",
        "identifierField": "PhysicalResourceId",
        "identifierKind": "id",
        "scopeKind": "regional",
        "evidence": "api_response",
        "accountScope": "fake-scope",
        "consolePartition": "aliyun",
        "regionId": "cn-hangzhou",
        "attributes": {"Id": "p-offline123", "PhysicalResourceId": "p-offline123", "Name": "offline-secret"},
        "aliases": [
            {"identifier": "p-offline123", "identifierKind": "id", "identifierField": "Id"},
            {"identifier": "offline-secret", "identifierKind": "name", "identifierField": "Name"},
        ],
        "sources": [
            {
                "toolUseId": "call_offline",
                "turnId": "offline-turn",
                "product": "oos",
                "action": "ListSecretParameters",
                "requestRegionId": "cn-hangzhou",
                "evidence": "api_response",
                "sourceKind": "tool_result",
                "sourcePath": "/result/Parameters/0/Id",
            }
        ],
    }
    raw = {
        "Parameters": [
            {"Id": "p-offline123", "Name": "offline-secret", "Type": "Secret", "Description": "", "ParameterVersion": 1}
        ]
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "oos",
            "action": "ListSecretParameters",
            "version": "2019-06-01",
            "region_id": "cn-hangzhou",
            "params": {"RegionId": "cn-hangzhou", "MaxResults": 10},
        },
        "results": [{"isError": False, "toolUseId": "call_offline", "content": json.dumps(raw)}],
    }
    return reference, {"call_offline": tool}, raw


@pytest.mark.parametrize("identifier_field", ["Id", "PhysicalResourceId"])
def test_name_is_derived_without_mutating_source_identity(identifier_field):
    reference, tools, _ = fixture()
    reference["identifierField"] = identifier_field
    before = copy.deepcopy((reference, tools))
    assert navigation_identifier(reference, tools) == "offline-secret"
    assert (reference, tools) == before
    assert reference["identifier"] == "p-offline123"


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other"),
        ("identifier", "name"),
        ("identifierKind", "name"),
        ("identifierField", "Name"),
        ("scopeKind", "global"),
        ("accountScope", ""),
        ("regionId", "global"),
        ("regionId", "cn-shenzhen"),
        ("consolePartition", "alibabacloud"),
        ("evidence", "model"),
        ("aliases", []),
    ],
)
def test_invalid_locator_context_fails_closed(field, value):
    reference, tools, _ = fixture()
    reference[field] = value
    with pytest.raises(WorkflowError):
        navigation_identifier(reference, tools)


@pytest.mark.parametrize(
    "damage",
    [
        "attribute-id",
        "attribute-physical-id",
        "attribute-name",
        "name-url",
        "name-control",
        "alias-name",
        "alias-id",
        "alias-duplicate",
        "alias-kind",
        "no-tools",
        "source-path",
        "source-index",
        "source-product",
        "source-action",
        "source-region",
        "source-evidence",
        "source-kind",
        "source-tool",
        "tool-name",
        "tool-status",
        "tool-product",
        "tool-action",
        "tool-version",
        "tool-region",
        "param-region",
        "result-error",
        "result-owner",
        "raw-id",
        "raw-name",
        "raw-type",
        "raw-code",
        "raw-duplicate-id",
        "raw-duplicate-name",
        "raw-empty",
        "raw-non-list",
        "raw-malformed",
        "two-results",
    ],
)
def test_cross_row_alias_or_unproven_wire_is_rejected(damage):
    reference, tools, raw = fixture()
    tool = tools["call_offline"]
    source = reference["sources"][0]
    if damage.startswith("attribute-"):
        reference["attributes"][
            {"attribute-id": "Id", "attribute-physical-id": "PhysicalResourceId", "attribute-name": "Name"}[damage]
        ] = "wrong"
    elif damage.startswith("name-"):
        reference["attributes"]["Name"] = "https://other.invalid" if damage == "name-url" else "bad\nname"
    elif damage == "alias-name":
        reference["aliases"][1]["identifier"] = "other"
    elif damage == "alias-id":
        reference["aliases"][0]["identifier"] = "p-other"
    elif damage == "alias-duplicate":
        reference["aliases"].append(copy.deepcopy(reference["aliases"][1]))
    elif damage == "alias-kind":
        reference["aliases"][1]["identifierKind"] = "id"
    elif damage == "no-tools":
        tools = {}
    elif damage.startswith("source-"):
        key = {
            "source-path": "sourcePath",
            "source-index": "sourcePath",
            "source-product": "product",
            "source-action": "action",
            "source-region": "requestRegionId",
            "source-evidence": "evidence",
            "source-kind": "sourceKind",
            "source-tool": "toolUseId",
        }[damage]
        source[key] = "/result/Parameters/1/Id" if damage == "source-index" else "other"
    elif damage == "tool-name":
        tool["toolName"] = "other"
    elif damage == "tool-status":
        tool["status"] = "running"
    elif damage.startswith("tool-"):
        tool["input"][damage[5:] if damage != "tool-region" else "region_id"] = "other"
    elif damage == "param-region":
        tool["input"]["params"]["RegionId"] = "other"
    elif damage == "result-error":
        tool["results"][0]["isError"] = True
    elif damage == "result-owner":
        tool["results"][0]["toolUseId"] = "other"
    elif damage in {"raw-id", "raw-name", "raw-type"}:
        raw["Parameters"][0][{"raw-id": "Id", "raw-name": "Name", "raw-type": "Type"}[damage]] = "other"
    elif damage == "raw-code":
        raw["Code"] = "Denied"
    elif damage.startswith("raw-duplicate"):
        row = copy.deepcopy(raw["Parameters"][0])
        row["Name" if damage == "raw-duplicate-id" else "Id"] = "other"
        raw["Parameters"].append(row)
    elif damage == "raw-empty":
        raw["Parameters"] = []
    elif damage == "raw-non-list":
        raw["Parameters"] = {"0": raw["Parameters"][0]}
    tool["results"][0]["content"] = "malformed" if damage == "raw-malformed" else json.dumps(raw)
    if damage == "two-results":
        tool["results"].append(copy.deepcopy(tool["results"][0]))
    with pytest.raises(WorkflowError):
        navigation_identifier(reference, tools)


def test_other_resource_types_keep_their_primary_identifier():
    reference, tools, _ = fixture()
    reference["resourceType"] = "ALIYUN::OOS::Parameter"
    assert navigation_identifier(reference, tools) == "p-offline123"


@pytest.mark.parametrize("templated", [False, True])
@pytest.mark.parametrize("mode", ["iframe", "open"])
@pytest.mark.parametrize("damage", [None, "session", "wire-scope", "source-name", "duplicate-reference"])
def test_real_click_request_binds_name_but_keeps_primary_source_identity(
    tmp_path,
    monkeypatch,
    templated,
    mode,
    damage,
):
    store = common.store.__wrapped__(tmp_path)
    common.plan(store)
    common.prepare(store)
    worker = common.reserve(store, "dispatch")
    key, _, _ = common.bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="QUERY", source_type="ALIYUN::OOS::SecretParameter", mode=mode)
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "fixture-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    proposal = read_json(store.root / context["packet_path"])
    proposal["scenes"][scene["id"]].update(
        identifier="p-offline123",
        expected_type=scene["source_type"],
        region="cn-hangzhou",
        selector='a.resource-link[aria-label="' + ("{identifier}" if templated else "p-offline123") + '"]',
        render_text=["Secret", "1"],
    )
    json_write(store.root / context["packet_path"], proposal)
    reference, tools, _ = fixture()
    references = [reference]
    if damage == "duplicate-reference":
        other, other_tools, _ = fixture()
        other["attributes"]["Name"] = "other-secret"
        other["aliases"][1]["identifier"] = "other-secret"
        other["sources"][0]["toolUseId"] = "call_other"
        other_tools["call_offline"]["results"][0].update(
            toolUseId="call_other",
            content=json.dumps({"Parameters": [{"Id": "p-offline123", "Name": "other-secret", "Type": "Secret"}]}),
        )
        references.append(other)
        tools["call_other"] = other_tools["call_offline"]
    if damage == "source-name":
        reference["attributes"]["Name"] = "unproven"
    json_write(
        output / "QUERY-product-source.json",
        {
            "session_id": "ws~other" if damage == "session" else "ws~new",
            "references": {"items": references},
            "messages": {"tools": tools},
        },
    )
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)

    def proven(job, dimension, session, account, refs):
        assert (job, dimension, session, account) == (worker["job_id"], "QUERY", "ws~new", "fixture")
        return [] if damage == "wire-scope" else refs

    monkeypatch.setattr(runtime, "proven_references", proven)

    def browser(request):
        assert damage is None, "invalid binding must fail before any browser operation"
        assert request["source_identifier"] == "p-offline123"
        assert request["render_identifier"] == "offline-secret"
        assert request["render_text"] == ["Secret", "1"]
        assert request["mode"] == mode
        locator = "offline-secret" if templated else "p-offline123"
        assert request["selector"] == (
            'details[data-tool-use-id="call_offline"] a.resource-link[aria-label="' + locator + '"]'
        )
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(common.PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~new"}

    monkeypatch.setattr(runtime, "browser", browser)
    if damage:
        with pytest.raises(WorkflowError):
            runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"], "session_id": "ws~new"})
    else:
        result = runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"], "session_id": "ws~new"})
        assert result["source_identifier"] == "p-offline123"
        assert result["navigation_identifier"] == "offline-secret"
