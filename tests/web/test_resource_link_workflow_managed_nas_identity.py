"""A composite provider ID is a locator only for its own original stack member."""

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow.console_sources import managed_nas_mount_identity
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime, navigation_identifier
from scripts.web.e2e.resource_link_workflow.temporary_samples import nas_prior_queries_only
from tests.web import test_resource_link_workflow as common


def fixture():
    identifier = "offlinefs:offlinefs-target.cn-hangzhou.nas.aliyuncs.com"
    ref = {
        "identifier": identifier,
        "resourceType": "alicloud_nas_mount_target",
        "identifierField": "PhysicalResourceId",
        "provider": "aliyun",
        "consolePartition": "aliyun",
        "evidence": "api_response",
        "scopeKind": "regional",
        "regionId": "cn-hangzhou",
        "accountScope": "signed-test",
        "stackId": "own-stack",
        "logicalResourceId": "mount_target",
        "sources": [
            {
                "toolUseId": "members",
                "evidence": "api_response",
                "sourceKind": "tool_result",
                "product": "ros",
                "action": "ListStackResources",
                "stackId": "own-stack",
                "logicalResourceId": "mount_target",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }
    raw = {
        "RequestId": "offline-request",
        "Resources": [
            {
                "ResourceType": "alicloud_nas_mount_target",
                "LogicalResourceId": "mount_target",
                "PhysicalResourceId": identifier,
                "ResourceStatus": "CREATE_COMPLETE",
            },
            {
                "ResourceType": "alicloud_nas_file_system",
                "LogicalResourceId": "filesystem",
                "PhysicalResourceId": "offlinefs",
                "ResourceStatus": "CREATE_COMPLETE",
            },
        ],
    }
    tool = {
        "toolName": "aliyun_api",
        "status": "completed",
        "input": {
            "product": "ros",
            "action": "ListStackResources",
            "version": "2019-09-10",
            "region_id": "cn-hangzhou",
            "params": {"StackId": "own-stack"},
        },
        "results": [{"toolUseId": "members", "isError": False, "content": json.dumps(raw)}],
    }
    return ref, tool, raw


def test_exact_original_managed_member_locator_and_query_alias_preserved():
    ref, tool, _ = fixture()
    domain = ref["identifier"].split(":")[1]
    assert managed_nas_mount_identity(ref, {"members": tool}) == ("offlinefs", domain)
    assert navigation_identifier(ref, {"members": tool}) == domain
    assert ref["identifier"].startswith("offlinefs:")  # source remains composite
    assert navigation_identifier({**ref, "identifier": domain, "identifierField": "MountTargetDomain"}) == domain


@pytest.mark.parametrize("mode", ["iframe", "open"])
@pytest.mark.parametrize("selector_attribute", ["aria-label", "href"])
@pytest.mark.parametrize("identifier_field", ["PhysicalResourceId", "MountTargetDomain"])
def test_managed_mount_click_keeps_source_card_label_separate_from_landing_domain(
    tmp_path,
    monkeypatch,
    mode,
    selector_attribute,
    identifier_field,
):
    """Exercise real click assembly, not only the domain projection helper.

    The original ROS card displays the composite physical ID. A QUERY card can
    display the bare domain, but cannot stand in for the TF member's occurrence.
    """
    store = common.store.__wrapped__(tmp_path)
    common.plan(store)
    common.prepare(store)
    worker = common.reserve(store, "dispatch")
    key, _, _ = common.bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="TF", source_type="alicloud_nas_mount_target", mode=mode)
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
    operator = "=" if selector_attribute == "aria-label" else "*="
    selector = "a.resource-link[data-resource-link][" + selector_attribute + operator + '"{identifier}"]'
    proposal["scenes"][scene["id"]].update(
        selector=selector,
        reference_types=[scene["source_type"]],
        name_prefix="",
        region="cn-hangzhou",
        render_text=["expected-vpc"],
    )
    json_write(store.root / context["packet_path"], proposal)
    reference, tool, _ = fixture()
    reference["identifierField"] = identifier_field
    reference["sources"][0]["turnId"] = "own-turn"
    identifier = reference["identifier"]
    domain = identifier.split(":")[1]
    original = {"session_id": "ws~new", "references": {"items": [reference]}, "messages": {"tools": {"members": tool}}}
    json_write(output / "TF-product-source.json", original)
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    verified = []

    def signed(*args):
        verified.append("signed")
        return args[-1]

    def created(*args):
        assert verified == ["signed"]
        verified.append("created")
        return args[-1]

    monkeypatch.setattr(runtime, "proven_references", signed)
    monkeypatch.setattr(runtime, "created_references", created)

    def browser(request):
        assert verified == ["signed", "created"]
        # Model the observed two different source cards: the ROS link's label
        # is composite, whereas its destination/console identity is not.
        locator = identifier if selector_attribute == "aria-label" else domain
        assert request["selector"] == (
            'details[data-tool-use-id="members"] ' + selector.replace("{identifier}", locator)
        )
        assert request["source_card_turn_id"] == "own-turn"
        assert request["source_identifier"] == identifier
        assert request["render_identifier"] == domain
        assert request["render_text"] == ["expected-vpc"]
        assert request["mode"] == mode
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(common.PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~new"}

    monkeypatch.setattr(runtime, "browser", browser)
    result = runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"], "session_id": "ws~new"})
    assert result["source_identifier"] == identifier
    assert result["navigation_identifier"] == domain
    assert read_json(output / "TF-product-source.json") == original
    assert not store.status()["completed"]  # Mocked pixels/clicks never earn a verdict.


@pytest.mark.parametrize("identifier_field", ["MountTargetDomain", "PhysicalResourceId"])
@pytest.mark.parametrize("status_field", ["Status", "ResourceStatus", "both"])
def test_actual_ros_member_status_and_production_reference_field(identifier_field, status_field):
    """ListStackResources emits Status; the production alias retains its catalog field."""
    ref, tool, raw = fixture()
    ref["identifierField"] = identifier_field
    ref["status"] = "CREATE_COMPLETE"
    for row in raw["Resources"]:
        if status_field != "ResourceStatus":
            row["Status"] = row["ResourceStatus"]
        if status_field == "Status":
            del row["ResourceStatus"]
        row.update(StackId="own-stack", RegionId="cn-hangzhou")
    tool["results"][0]["content"] = json.dumps(raw)
    original = json.dumps((ref, tool), sort_keys=True)
    assert managed_nas_mount_identity(ref, {"members": tool}) == ("offlinefs", ref["identifier"].split(":")[1])
    assert navigation_identifier(ref, {"members": tool}) == ref["identifier"].split(":")[1]
    assert json.dumps((ref, tool), sort_keys=True) == original


@pytest.mark.parametrize(
    "damage",
    [
        "member-status-conflict",
        "parent-status-conflict",
        "missing-status",
        "reference-field",
        "reference-status",
        "member-stack",
        "parent-stack",
        "member-region",
        "parent-region",
        "extra-member",
    ],
)
def test_actual_member_shape_never_hides_conflicting_or_foreign_fields(damage):
    ref, tool, raw = fixture()
    ref["identifierField"] = "MountTargetDomain"
    for row in raw["Resources"]:
        row["Status"] = row.pop("ResourceStatus")
    if damage == "member-status-conflict":
        raw["Resources"][0]["ResourceStatus"] = "CREATE_FAILED"
    elif damage == "parent-status-conflict":
        raw["Resources"][1]["ResourceStatus"] = "DELETE_COMPLETE"
    elif damage == "missing-status":
        del raw["Resources"][0]["Status"]
    elif damage == "reference-field":
        ref["identifierField"] = "ArbitraryId"
    elif damage == "reference-status":
        ref["status"] = "DELETE_COMPLETE"
    elif damage.endswith("stack"):
        raw["Resources"][int(damage.startswith("parent"))]["StackId"] = "foreign"
    elif damage.endswith("region"):
        raw["Resources"][int(damage.startswith("parent"))]["RegionId"] = "cn-beijing"
    else:
        raw["Resources"].append({"ResourceType": "alicloud_ecs_instance", "Status": "CREATE_COMPLETE"})
    tool["results"][0]["content"] = json.dumps(raw)
    with pytest.raises(WorkflowError):
        managed_nas_mount_identity(ref, {"members": tool})


def test_root_selection_does_not_borrow_or_count_managed_children(tmp_path, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import execution_gate as test_execution_gate

    record = {
        "dimension": "TF",
        "session_id": "own-session",
        "state": "succeeded",
        "reference_scope": "signed-test",
        "resource": {"id": "own-stack", "product": "ros", "region": "cn-hangzhou", "account": "offline"},
    }
    children = [
        {
            **record,
            "managed_child": True,
            "resource": {"id": child, "product": "nas", "region": "cn-hangzhou", "account": "offline"},
        }
        for child in ("offlinefs", "offlinefs-target.cn-hangzhou.nas.aliyuncs.com")
    ]
    monkeypatch.setattr(test_execution_gate, "creations", lambda *args: [record, *children])
    runtime = object.__new__(Runtime)
    runtime.root = tmp_path
    ref, _, _ = fixture()
    foreign = {**ref, "stackId": "other-stack"}
    assert runtime.created_references("own-job", "TF", "own-session", [ref, foreign]) == [ref]
    # Child-only exports never stand in for an actual root stack creation.
    monkeypatch.setattr(test_execution_gate, "creations", lambda *args: children)
    with pytest.raises(WorkflowError, match="original wire creation"):
        runtime.created_references("own-job", "TF", "own-session", [ref])


@pytest.mark.parametrize(
    "damage",
    [
        "stack",
        "parent",
        "status",
        "duplicate",
        "domain",
        "missing-request",
        "error",
        "logical",
        "source-region",
        "foreign-type",
    ],
)
def test_borrowed_or_ambiguous_managed_identity_rejected(damage):
    ref, tool, raw = fixture()
    if damage == "stack":
        tool["input"]["params"]["StackId"] = "foreign-stack"
    elif damage == "parent":
        raw["Resources"][1]["PhysicalResourceId"] = "foreignfs"
    elif damage == "status":
        raw["Resources"][0]["ResourceStatus"] = "CREATE_FAILED"
    elif damage == "duplicate":
        raw["Resources"].append(raw["Resources"][1].copy())
    elif damage == "domain":
        ref["identifier"] = "offlinefs:foreign-target.cn-hangzhou.nas.aliyuncs.com"
    elif damage == "missing-request":
        del raw["RequestId"]
    elif damage == "error":
        tool["results"][0]["isError"] = True
    elif damage == "logical":
        raw["Resources"][0]["LogicalResourceId"] = "other"
    elif damage == "source-region":
        ref["sources"][0]["requestRegionId"] = "cn-beijing"
    elif damage == "foreign-type":
        raw["Resources"][0]["ResourceType"] = "ALIYUN::NAS::MountTarget"
    tool["results"][0]["content"] = json.dumps(raw)
    with pytest.raises(WorkflowError):
        managed_nas_mount_identity(ref, {"members": tool})


@pytest.mark.parametrize("damage", [None, "unknown", "TF", "write", "owner", "live", "missing-grant"])
def test_first_sample_cannot_refill_prior_writes_or_hide_unknown_calls(tmp_path, damage):
    with sqlite3.connect(":memory:") as state:
        state.executescript("CREATE TABLE jobs(id,task_id,role,state); CREATE TABLE owned(task_id);")
        state.execute(
            "INSERT INTO jobs VALUES('worker','task','glm',?)", ("running" if damage == "live" else "complete",)
        )
        if damage == "owner":
            state.execute("INSERT INTO owned VALUES('task')")
        with sqlite3.connect(tmp_path / "execution-gate.sqlite3") as wire:
            wire.executescript("CREATE TABLE attempts(scope,state,grant_id); CREATE TABLE grants(scope,id,kind);")
            scope = "worker:" + ("TF" if damage == "TF" else "QUERY")
            wire.execute(
                "INSERT INTO attempts VALUES(?,?,?)", (scope, "unknown" if damage == "unknown" else "succeeded", "read")
            )
            if damage != "missing-grant":
                wire.execute(
                    "INSERT INTO grants VALUES(?,?,?)", (scope, "read", "create" if damage == "write" else "read")
                )
        if damage:
            with pytest.raises(WorkflowError):
                nas_prior_queries_only(tmp_path, state, "task")
        else:
            nas_prior_queries_only(tmp_path, state, "task")
