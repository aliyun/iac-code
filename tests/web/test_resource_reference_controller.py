from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from iac_code.agent.message import Message, ToolResultBlock, ToolUseBlock
from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import RESOURCE_REFERENCES_METADATA_KEY, resource_reference_metadata
from iac_code.web.events import WebEventBuffer, WebEventTranslator
from iac_code.web.resource_link_catalog import resource_link_catalog_payload
from iac_code.web.resource_reference_controller import ResourceReferenceController
from iac_code.web.resource_references import ResourceReferenceStore, resource_fact_to_web


@pytest.mark.asyncio
async def test_cms_spaced_ros_locator_can_save_missing_site_context(tmp_path):
    from iac_code.tools.cloud.resource_references import stack_resource_reference
    from iac_code.web.resource_sources import read_visible_source

    name = "ack_Default Contact Group"
    row = {"ResourceType": "ALIYUN::CMS::ContactGroup", "PhysicalResourceId": name}
    fact = stack_resource_reference(
        row, stack_id="offline-stack", region_id="cn-hangzhou", tool_use_id="offline-tool",
        source_path="/Resources/0", account_scope="fake-account",
    )
    history = {"messages": [], "tools": {"offline-tool": {
        "toolName": "aliyun_api", "toolUseId": "offline-tool",
        "input": {"product": "ros", "action": "ListStackResources", "params": {"StackId": "offline-stack"}},
        "results": [{"content": json.dumps({"Resources": [row]}), "resourceReferences": [resource_fact_to_web(fact)]}],
    }}}
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *_: None)
    snapshot = controller.hydrate(history)
    reference = next(item for item in snapshot["items"] if item["resourceType"] == "ALIYUN::CMS::ContactGroup")
    source = {"tool_use_id": "offline-tool", "source_path": "/tool"}
    result = await controller.resolve_choice(history, {
        "expectedRevision": snapshot["revision"], "source": resource_fact_to_web(source),
        "sourceDigest": read_visible_source(history, source).digest,
        "group": ["id", name, "PhysicalResourceId", "ALIYUN::CMS::ContactGroup", None, None, None, [],
                  {"kind": "field", "field": "PhysicalResourceId", "typed": True}, None],
        "identifier": name, "identifierKind": "id", "referenceId": reference["referenceId"],
        "context": {"accountScope": "fake-account", "consolePartition": "aliyun", "attributes": {}},
    })
    assert result["status"] == "saved"
    assert result["snapshot"]["bindings"][0]["context"]["consolePartition"] == "aliyun"
    await controller.close()


def role(tool="tool-1", name="DeployRole", **overrides):
    return {
        "resource_type": "ALIYUN::RAM::Role",
        "identifier": name,
        "identifier_kind": "name",
        "identifier_field": "RoleName",
        "evidence": "api_response",
        "account_scope": "fake-account",
        "console_partition": "aliyun",
        "sources": [{"tool_use_id": tool, "source_path": "/result/Role/RoleName"}],
        **overrides,
    }


def transcript(*facts):
    return {
        "messages": [],
        "tools": {
            fact["sources"][0]["tool_use_id"]: {"results": [{"resourceReferences": [resource_fact_to_web(fact)]}]}
            for fact in facts
        },
    }


@pytest.fixture
def bridge(tmp_path):
    events = WebEventBuffer("session-1")
    controller = ResourceReferenceController(tmp_path / "session" / "web-resource-references.json", events.append)
    events.resource_observer = controller.on_event
    return controller, events


@pytest.mark.asyncio
async def test_live_batch_deduplicates_persists_and_restores_before_terminal_result(bridge):
    controller, events = bridge
    payload = {
        "kind": "stack.progress",
        "toolUseId": "tool-1",
        "stackId": "fake-stack",
        "regionId": "cn-hangzhou",
        "accountScope": "fake-account",
        "consolePartition": "aliyun",
        "resources": [
            {
                "name": "LogicalRole",
                "resource_type": "ALIYUN::RAM::Role",
                "physical_resource_id": "DeployRole",
                "attributes": {"RoleName": "DeployRole"},
            }
        ],
    }
    events.append("pipeline.event", payload)
    events.append("pipeline.event", {**payload, "elapsedSeconds": 7})
    assert not controller.path.exists()
    assert await controller.flush()
    delta = [event for event in events.replay_after(0) if event["type"] == "resource.references"]
    assert len(delta) == 1
    role_ref = next(item for item in delta[0]["payload"]["upserts"] if item["identifierKind"] == "name")
    assert role_ref["identifier"] == "DeployRole"
    assert "regionId" not in role_ref
    assert role_ref["sources"][0]["requestRegionId"] == "cn-hangzhou"
    assert role_ref["sources"][0]["observedAt"] > 0
    assert controller.store.revision == 1
    events.append("pipeline.event", {**payload, "elapsedSeconds": 9})
    assert await controller.flush()
    assert controller.store.revision == 1
    restored = ResourceReferenceController(controller.path, events.append)
    snapshot = restored.hydrate({"tools": {"tool-1": {"results": []}}})
    assert len(snapshot["items"]) == 2
    assert any(item["referenceId"] == role_ref["referenceId"] for item in snapshot["items"])
    await controller.close()
    await restored.close()


@pytest.mark.asyncio
async def test_timer_batches_without_blocking_text_events(bridge):
    controller, events = bridge
    controller.batch_seconds = 0.001
    saved = asyncio.Event()

    async def capture_write():
        saved.set()
        return True

    events.append("tool.result", {"toolUseId": "tool-1", "resourceReferences": [resource_fact_to_web(role())]})
    events.append("assistant.text.delta", {"messageId": "m1", "delta": "still streaming"})
    assert controller._store is None
    with patch.object(ResourceReferenceStore, "flush", side_effect=capture_write):
        # The real write is replaced; align the durable revision for this timer test.
        original = controller._drain

        def drain():
            original()
            if controller._store:
                controller.store.persisted_revision = controller.store.revision

        with patch.object(controller, "_drain", side_effect=drain):
            await asyncio.wait_for(saved.wait(), timeout=1)
            await asyncio.sleep(0)
    assert events.replay_after(0)[1]["type"] == "assistant.text.delta"
    await controller.close()


@pytest.mark.asyncio
async def test_live_tool_fact_uses_web_owned_message_and_turn_provenance(bridge):
    controller, events = bridge
    events.append(
        "tool.started",
        {
            "toolUseId": "tool-1",
            "toolName": "aliyun_api",
            "messageId": "assistant-current",
            "turnId": "turn-current",
            "input": {"product": "ram", "action": "GetRole"},
        },
    )
    supplied = role(message_id="untrusted-message", turn_id="untrusted-turn")
    events.append("tool.result", {"toolUseId": "tool-1", "resourceReferences": [resource_fact_to_web(supplied)]})

    await controller.flush()
    source = controller.store.snapshot()["items"][0]["sources"][0]
    assert source["messageId"] == "assistant-current"
    assert source["turnId"] == "turn-current"

    await controller.close()


@pytest.mark.asyncio
async def test_hydration_does_not_import_hidden_tool_facts_or_hidden_source_attributes(bridge):
    controller, events = bridge
    first, hidden = role(), role("hidden-tool", attributes={"RoleId": "123456789"})
    controller.store.observe([first, hidden])
    reference_id = next(iter(controller.store.references))
    assert controller.store.references[reference_id]["attributes"]["RoleId"] == "123456789"
    snapshot = controller.hydrate(transcript(first))
    assert snapshot["items"][0]["referenceId"] == reference_id
    assert snapshot["items"][0]["attributes"] == {}
    assert len(snapshot["items"][0]["sources"]) == 1
    await controller.flush()
    restored = ResourceReferenceController(controller.path, events.append)
    assert restored.hydrate({"tools": {}})["items"] == []
    await restored.close()
    await controller.close()


def test_projection_cannot_claim_other_tool_sources(bridge):
    controller, events = bridge
    events.append("tool.result", {"toolUseId": "tool-1", "resourceReferences": [resource_fact_to_web(role("hidden"))]})
    assert controller.snapshot()["items"] == []


def test_legacy_recovery_uses_only_visible_known_api_fields_and_explicit_region(bridge):
    controller, _events = bridge
    data = {
        "tools": {
            "role-tool": {
                "toolName": "aliyun_api",
                "input": {"product": "ram", "action": "GetRole", "params": {"RoleName": "DeployRole"}},
                "results": [{"content": '{"Role":{"RoleName":"DeployRole","RoleId":"123456"}}'}],
            },
            "unknown": {"toolName": "bash", "results": [{"content": '{"RoleName":"HiddenRole"}'}]},
        }
    }
    snapshot = controller.hydrate(data)
    assert snapshot["items"]
    assert {item["identifier"] for item in snapshot["items"]} == {"DeployRole"}
    for item in snapshot["items"]:
        assert "accountScope" not in item and "regionId" not in item
        assert all(source["observedAt"] == 0 for source in item["sources"])
    assert controller.hydrate(data)["revision"] == snapshot["revision"]


def test_legacy_recovery_reuses_one_proved_account_only_within_the_same_turn(bridge):
    controller, _events = bridge
    instance = {
        "resource_type": "ALIYUN::ECS::Instance",
        "identifier": "i-instance111111",
        "identifier_kind": "id",
        "identifier_field": "InstanceId",
        "evidence": "api_response",
        "region_id": "cn-hangzhou",
        "account_scope": "fake-account",
        "console_partition": "aliyun",
        "sources": [{"tool_use_id": "instance-tool", "source_path": "/result/Instances/0/InstanceId"}],
    }
    data = {
        "tools": {
            "instance-tool": {
                "toolName": "aliyun_api",
                "turnId": "turn-current",
                "results": [{"resourceReferences": [resource_fact_to_web(instance)]}],
            },
            "disk-tool": {
                "toolName": "aliyun_api",
                "turnId": "turn-current",
                "input": {"product": "ecs", "action": "DescribeDisks", "region_id": "cn-hangzhou"},
                "results": [{"content": '{"Disks":{"Disk":[{"DiskId":"d-disk111111"}]}}'}],
            },
        }
    }

    snapshot = controller.hydrate(data)
    disk = next(item for item in snapshot["items"] if item["identifier"] == "d-disk111111")
    assert disk["accountScope"] == "fake-account"
    assert disk["consolePartition"] == "aliyun"
    assert disk["regionId"] == "cn-hangzhou"


def test_hydration_enriches_legacy_source_with_recovered_turn_owner(bridge):
    controller, _events = bridge
    tool = {
        "toolName": "aliyun_api",
        "input": {"product": "ecs", "action": "DescribeKeyPairs", "region_id": "cn-hangzhou"},
        "results": [{"content": '{"KeyPairs":{"KeyPair":[{"KeyPairName":"ha"}]}}'}],
    }

    first = controller.hydrate({"tools": {"key-tool": tool}})
    assert "turnId" not in first["items"][0]["sources"][0]

    recovered = controller.hydrate({"tools": {"key-tool": {**tool, "turnId": "turn-current"}}})
    assert recovered["items"][0]["sources"][0]["turnId"] == "turn-current"


def test_legacy_recovery_does_not_borrow_context_across_turns_or_conflicting_accounts(bridge):
    controller, _events = bridge

    def known(tool_id: str, turn_id: str, account: str):
        fact = role(tool_id, account_scope=account)
        return {
            "toolName": "aliyun_api",
            "turnId": turn_id,
            "results": [{"resourceReferences": [resource_fact_to_web(fact)]}],
        }

    legacy = {
        "toolName": "aliyun_api",
        "turnId": "turn-current",
        "input": {"product": "ecs", "action": "DescribeDisks", "region_id": "cn-hangzhou"},
        "results": [{"content": '{"Disks":{"Disk":[{"DiskId":"d-disk111111"}]}}'}],
    }
    for tools in (
        {"other-turn": known("other-turn", "turn-other", "account-a"), "disk-tool": legacy},
        {
            "known-a": known("known-a", "turn-current", "account-a"),
            "known-b": known("known-b", "turn-current", "account-b"),
            "disk-tool": legacy,
        },
    ):
        snapshot = controller.hydrate({"tools": tools})
        disk = next(item for item in snapshot["items"] if item["identifier"] == "d-disk111111")
        assert "accountScope" not in disk
        assert "consolePartition" not in disk


@pytest.mark.asyncio
async def test_same_tool_result_replacement_retracts_alias_and_attributes_without_changing_identity(bridge):
    controller, events = bridge
    before = role(
        attributes={"RoleId": "123456789"},
        aliases=[{"identifier": "123456789", "identifier_kind": "id", "identifier_field": "RoleId"}],
    )
    initial = controller.hydrate(transcript(before))
    reference_id = initial["items"][0]["referenceId"]
    after = controller.hydrate(transcript(role()))
    assert after["items"][0]["referenceId"] == reference_id
    assert after["items"][0]["attributes"] == {}
    assert after["items"][0]["aliases"] == []
    assert controller.hydrate(transcript(role()))["revision"] == after["revision"]
    await controller.flush()
    restored = ResourceReferenceController(controller.path, events.append)
    assert restored.hydrate(transcript(role()))["items"] == after["items"]
    await restored.close()
    await controller.close()


@pytest.mark.asyncio
async def test_same_tool_result_replacement_removes_old_identifier_but_keeps_other_source(bridge):
    controller, _ = bridge
    initial = controller.hydrate(transcript(role(), role("independent", name="AnotherRole")))
    other_id = next(item["referenceId"] for item in initial["items"] if item["identifier"] == "AnotherRole")
    after = controller.hydrate(transcript(role(name="NewRole"), role("independent", name="AnotherRole")))
    assert {item["identifier"] for item in after["items"]} == {"NewRole", "AnotherRole"}
    assert next(item["referenceId"] for item in after["items"] if item["identifier"] == "AnotherRole") == other_id
    await controller.close()


@pytest.mark.asyncio
async def test_empty_replacement_is_not_proof_for_old_result_facts(bridge):
    controller, _ = bridge
    controller.hydrate(transcript(role()))
    after = controller.hydrate({"tools": {"tool-1": {"results": [{"content": "No resource returned"}]}}})
    assert after["items"] == []
    await controller.close()


@pytest.mark.asyncio
async def test_result_replacement_publishes_one_final_delta_and_revokes_alias_binding(bridge):
    controller, events = bridge
    fact = role(aliases=[{"identifier": "123456789", "identifier_kind": "id", "identifier_field": "RoleId"}])
    before = controller.hydrate(transcript(fact))
    ref_id = before["items"][0]["referenceId"]
    controller.store.bind(
        source_key="old-alias",
        identifier="123456789",
        identifier_kind="id",
        reference_id=ref_id,
        expected_revision=controller.store.revision,
    )
    revision = controller.store.revision
    sequence = events.latest_sequence
    result = controller.hydrate(transcript(role()))
    deltas = [event["payload"] for event in events.replay_after(sequence) if event["type"] == "resource.references"]
    assert len(deltas) == 1
    assert deltas[0]["baseRevision"] == revision
    assert result["revision"] == deltas[0]["revision"] == revision + 1
    assert deltas[0]["upserts"][0]["aliases"] == []
    assert deltas[0]["removedBindings"][0]["identifier"] == "123456789"
    assert result["bindings"] == []
    assert ("ALIYUN::RAM::Role", "id", "123456789") not in controller.store._by_locator
    await controller.close()


@pytest.mark.asyncio
async def test_appended_results_are_independent_versions_and_timestamp_replay_is_idempotent(bridge):
    controller, _ = bridge
    first = role(attributes={"RoleId": "123456789"})
    first["sources"][0]["observed_at"] = 100
    data = transcript(first)
    controller.hydrate(data)
    data["tools"]["tool-1"]["results"].append({"resourceReferences": [resource_fact_to_web(role())]})
    before = controller.hydrate(data)
    assert before["items"][0]["attributes"] == {"RoleId": "123456789"}
    data["tools"]["tool-1"]["results"][0]["resourceReferences"][0]["sources"][0]["observedAt"] = 200
    assert controller.hydrate(data)["revision"] == before["revision"]
    data["tools"]["tool-1"]["results"].pop(0)
    after = controller.hydrate(data)
    assert after["items"][0]["referenceId"] == before["items"][0]["referenceId"]
    assert after["items"][0]["attributes"] == {}
    await controller.close()


@pytest.mark.asyncio
async def test_legacy_sidecar_without_contributions_rebuilds_only_current_visible_facts(bridge):
    controller, events = bridge
    controller.store.observe([role(attributes={"RoleId": "123456789"}), role("hidden", name="HiddenRole")])
    ref_id = next(iter(controller.store.references))
    await controller.flush()
    old = json.loads(controller.path.read_text(encoding="utf-8"))
    old.pop("contributions")
    controller.path.write_text(json.dumps(old), encoding="utf-8")
    restored = ResourceReferenceController(controller.path, events.append)
    after = restored.hydrate(transcript(role()))
    assert len(after["items"]) == 1
    assert after["items"][0]["referenceId"] == ref_id
    assert after["items"][0]["attributes"] == {}
    await restored.close()
    await controller.close()


@pytest.mark.asyncio
async def test_tool_supplied_web_provenance_cannot_preserve_replaced_facts(bridge):
    controller, _ = bridge
    fact = role()
    fact["sources"][0].update(
        source_kind="stack_progress",
        source_request_digest="a" * 64,
        source_revision="b" * 64,
        query_source_digest="c" * 64,
        query_parent_path="/resources/0",
        source_id="web-choice:invented",
    )
    before = controller.hydrate(transcript(fact))
    source = before["items"][0]["sources"][0]
    assert source["sourceKind"] == "tool_result"
    assert source["sourceRevision"] != "b" * 64
    assert "querySourceDigest" not in source and "sourceId" not in source
    assert controller.hydrate({"tools": {"tool-1": {"results": []}}})["items"] == []
    await controller.close()


@pytest.mark.asyncio
async def test_real_translator_streamed_request_and_live_input_replacement(bridge):
    controller, events = bridge
    translator = WebEventTranslator("session-1")
    request = {"region_id": "cn-hangzhou", "stack_id": "original-stack"}
    started = translator.tool_started(tool_use_id="tool-1", tool_name="ros_deploy")
    events.append(started["type"], started["payload"])
    text = json.dumps(request)
    for part in (text[:12], text[12:]):
        event = translator.tool_input_delta(tool_use_id="tool-1", delta=part)
        events.append(event["type"], event["payload"])
    payload = {"kind": "stack.progress", "toolUseId": "tool-1", "stackId": "original-stack", "regionId": "cn-hangzhou"}
    event = events.append("pipeline.event", payload)
    before = controller.snapshot()
    assert (
        before["items"][0]["sources"][0]["observedAt"]
        == datetime.fromisoformat(event["createdAt"].replace("Z", "+00:00")).timestamp()
    )
    completed = translator.tool_finished(
        tool_use_id="tool-1",
        status="input_complete",
        elapsed_ms=None,
        summary={"toolName": "ros_deploy", "input": request},
    )
    events.append(completed["type"], completed["payload"])
    assert controller.snapshot()["revision"] == before["revision"]
    visible = {"tools": {"tool-1": {"toolName": "ros_deploy", "input": request, "results": []}}}
    assert controller.hydrate(visible)["items"] == before["items"]
    changed = copy.deepcopy(completed)
    changed["payload"]["summary"]["input"]["stack_id"] = "replacement-stack"
    events.append(changed["type"], changed["payload"])
    assert controller.snapshot()["items"] == []
    await controller.close()


@pytest.mark.asyncio
async def test_terminal_stack_status_outranks_earlier_progress_across_restore(bridge):
    controller, events = bridge
    progress = {
        "kind": "stack.progress",
        "toolUseId": "tool-1",
        "stackId": "fake-stack",
        "regionId": "cn-hangzhou",
        "status": "CREATE_IN_PROGRESS",
    }
    events.append("pipeline.event", progress)
    before = controller.snapshot()
    terminal = {"stack_id": "fake-stack", "region_id": "cn-hangzhou", "status": "CREATE_COMPLETE"}
    events.append("tool.result", {"toolUseId": "tool-1", "stackResult": terminal})
    after = controller.snapshot()
    assert after["items"][0]["referenceId"] == before["items"][0]["referenceId"]
    assert after["items"][0]["status"] == "CREATE_COMPLETE"
    await controller.flush()
    restored = ResourceReferenceController(controller.path, events.append)
    history = {"tools": {"tool-1": {"results": [{"stackResult": terminal}], "stackProgress": progress}}}
    assert restored.hydrate(history)["items"] == after["items"]
    await restored.close()
    await controller.close()


@pytest.mark.asyncio
async def test_progress_keeps_earlier_lifecycle_but_replaced_result_does_not(bridge):
    controller, events = bridge
    progress = {
        "kind": "stack.progress",
        "toolUseId": "tool-1",
        "stackId": "fake-stack",
        "regionId": "cn-hangzhou",
        "accountScope": "fake-account",
        "consolePartition": "aliyun",
    }
    row = {
        "resource_type": "ALIYUN::RAM::Role",
        "name": "LogicalRole",
        "status": "DELETE_COMPLETE",
        "attributes": {"RoleName": "DeployRole", "RoleId": "111111111"},
    }
    events.append("pipeline.event", {**progress, "resources": [row]})
    old = controller.snapshot()
    ref_id = next(item["referenceId"] for item in old["items"] if item["identifier"] == "DeployRole")
    new = {**row, "status": "CREATE_COMPLETE", "attributes": {"RoleName": "DeployRole", "RoleId": "222222222"}}
    latest = {**progress, "resources": [new]}
    events.append("pipeline.event", latest)
    history = {"tools": {"tool-1": {"results": [], "stackProgress": latest}}}
    snapshot = controller.hydrate(history)
    roles = [item for item in snapshot["items"] if item["identifier"] == "DeployRole"]
    assert len(roles) == 2
    assert next(item for item in roles if item["referenceId"] == ref_id)["status"] == "DELETE_COMPLETE"
    await controller.close()


@pytest.mark.asyncio
async def test_name_only_progress_lifecycles_keep_identity_through_latest_snapshot_and_restart(bridge):
    controller, events = bridge
    progress = {
        "kind": "stack.progress",
        "toolUseId": "tool-1",
        "stackId": "fake-stack",
        "regionId": "cn-hangzhou",
        "accountScope": "fake-account",
        "consolePartition": "aliyun",
    }
    latest = None
    for index in range(5):
        latest = {
            **progress,
            "resources": [
                {
                    "resource_type": "ALIYUN::RAM::Role",
                    "name": "LogicalRole",
                    "status": "DELETE_COMPLETE" if index % 2 else "CREATE_COMPLETE",
                    "attributes": {"RoleName": "DeployRole"},
                }
            ],
        }
        # Real Web envelope shape, fixed observed times; no sleeps or real cloud.
        controller.on_event({"type": "pipeline.event", "payload": latest, "createdAt": f"2026-09-09T00:00:0{index}Z"})
        controller.snapshot()
    snapshot = controller.snapshot()
    roles = [item for item in snapshot["items"] if item["identifier"] == "DeployRole"]
    assert len(roles) == 3
    assert [item["status"] for item in roles] == ["DELETE_COMPLETE", "DELETE_COMPLETE", "CREATE_COMPLETE"]
    assert all(not item["aliases"] and "regionId" not in item for item in roles)
    history = {"tools": {"tool-1": {"results": [], "stackProgress": latest}}}
    assert controller.hydrate(history)["items"] == snapshot["items"]
    assert await controller.flush()
    restored = ResourceReferenceController(controller.path, events.append)
    for _ in range(3):
        replayed = restored.hydrate(history)
        assert replayed["items"] == snapshot["items"]
        assert replayed["revision"] == snapshot["revision"]
    await restored.close()
    await controller.close()


@pytest.mark.asyncio
async def test_historical_progress_survives_terminal_failure_but_not_replaced_request(bridge):
    controller, events = bridge
    request = {"action": "CreateStack", "params": {"StackName": "OriginalStack"}, "region_id": "cn-hangzhou"}
    events.append("tool.started", {"toolUseId": "tool-1", "toolName": "ros_stack", "input": request})
    events.append(
        "pipeline.event",
        {
            "kind": "stack.progress",
            "toolUseId": "tool-1",
            "stackId": "original-stack",
            "regionId": "cn-hangzhou",
            "accountScope": "fake-account",
            "consolePartition": "aliyun",
        },
    )
    await controller.flush()
    saved = {
        "tools": {"tool-1": {"toolName": "ros_stack", "input": request, "results": [{"content": "Polling failed"}]}}
    }
    assert controller.hydrate(saved)["items"][0]["identifier"] == "original-stack"
    saved["tools"]["tool-1"]["input"] = {**request, "params": {"StackName": "ReplacementStack"}}
    assert controller.hydrate(saved)["items"] == []
    await controller.close()


@pytest.mark.asyncio
async def test_tombstone_retracts_pending_and_persisted_facts(bridge):
    controller, events = bridge
    for tool in ("tool-1", "tool-2"):
        events.append("tool.result", {"toolUseId": tool, "resourceReferences": [resource_fact_to_web(role(tool))]})
    await controller.flush()
    events.append("assistant.message.tombstone", {"messageId": "m1", "affectedToolUseIds": ["tool-1", "tool-2"]})
    assert controller.snapshot()["items"] == []
    events.append("tool.result", {"toolUseId": "tool-1", "resourceReferences": [resource_fact_to_web(role())]})
    assert controller.snapshot()["items"] == [], "late results cannot revive a revoked source"
    await controller.close()
    assert ResourceReferenceStore(controller.path).snapshot()["items"] == []


@pytest.mark.asyncio
async def test_deletion_fences_queued_worker_without_recreating_storage(bridge):
    controller, events = bridge
    events.append("tool.result", {"toolUseId": "tool-1", "resourceReferences": [resource_fact_to_web(role())]})
    await controller.flush()
    path = controller.path
    write_queued, release = threading.Event(), threading.Event()
    original = controller.store._write_snapshot

    def delayed_write(data):
        write_queued.set()
        release.wait(timeout=5)
        return original(data)

    events.append("tool.result", {"toolUseId": "tool-2", "resourceReferences": [resource_fact_to_web(role("tool-2"))]})
    with patch.object(controller.store, "_write_snapshot", side_effect=delayed_write):
        task = asyncio.create_task(controller.flush())
        assert await asyncio.to_thread(write_queued.wait, 1)
        controller.close_for_delete()
        path.unlink()
        path.parent.rmdir()
        release.set()
        await task
    assert not path.parent.exists()


def test_session_messages_and_paginated_endpoint_share_resource_index(tmp_path):
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="resources-session")
    assert session.resource_references is not None
    assert session.resource_references._store is None
    metadata = {RESOURCE_REFERENCES_METADATA_KEY: resource_reference_metadata([role()])}
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tool-1",
                    name="aliyun_api",
                    input={"product": "ram", "action": "GetRole", "params": {"RoleName": "DeployRole"}},
                )
            ],
        ),
    )
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="tool-1", content='{"Role":{"RoleName":"DeployRole"}}', metadata=metadata)
            ],
        ),
    )
    with TestClient(create_app(session_manager=manager)) as client:
        response = client.get(f"/api/sessions/{session.web_session_id}/messages")
        assert response.status_code == 200
        body = response.json()
        assert body["resourceReferences"]["items"][0]["identifier"] == "DeployRole"
        assert body["tools"]["tool-1"]["results"][0]["content"] == '{"Role":{"RoleName":"DeployRole"}}'
        snapshot = client.get(f"/api/sessions/{session.web_session_id}/resource-references")
        assert snapshot.status_code == 200
        assert snapshot.json()["items"] == body["resourceReferences"]["items"]
        conflict = client.get(f"/api/sessions/{session.web_session_id}/resource-references?cursor=0:1")
        assert conflict.status_code == 409
        assert client.get("/api/sessions/no-such-session/resource-references").status_code == 404
    assert json.loads(session.resource_references.path.read_text(encoding="utf-8"))["references"]


def _legacy_snat_history():
    fact = {
        "resource_type": "ALIYUN::ECS::SNatEntry",
        "identifier": "snat-offline0001",
        "identifier_kind": "id",
        "identifier_field": "PhysicalResourceId",
        "evidence": "api_response",
        "account_scope": "fake-account",
        "console_partition": "aliyun",
        "region_id": "cn-hangzhou",
        "stack_id": "stack-offline",
        "logical_resource_id": "SNatEntry",
        "sources": [
            {
                "tool_use_id": "ros",
                "source_path": "/PhysicalResourceId",
                "product": "ros",
                "action": "GetStackResource",
                "evidence": "api_response",
            }
        ],
    }

    def tool(product, action, params, body, references=None):
        return {
            "toolName": "aliyun_api",
            "turnId": "turn-snat",
            "messageId": "message-snat",
            "input": {"product": product, "action": action, "region_id": "cn-hangzhou", "params": params},
            "results": [{"content": json.dumps(body), **({"resourceReferences": references} if references else {})}],
        }

    entry = {
        "SnatEntryId": fact["identifier"],
        "SnatTableId": "stb-offline0001",
        "NatGatewayId": "ngw-offline0001",
        "Status": "Available",
    }
    gateway = {
        "NatGatewayId": entry["NatGatewayId"],
        "NetworkType": "intranet",
        "Status": "Available",
        "RegionId": "cn-hangzhou",
        "SnatTableIds": {"SnatTableId": [entry["SnatTableId"]]},
    }
    paging = {"RegionId": "cn-hangzhou", "PageNumber": 1, "PageSize": 10}
    response = {"RequestId": "fake-request", "PageNumber": 1, "PageSize": 10, "TotalCount": 1}
    return {
        "tools": {
            "ros": tool(
                "ros",
                "GetStackResource",
                {"StackId": "stack-offline", "LogicalResourceId": "SNatEntry"},
                {"PhysicalResourceId": fact["identifier"]},
                [resource_fact_to_web(fact)],
            ),
            "entry": tool(
                "vpc",
                "DescribeSnatTableEntries",
                {**paging, "SnatEntryId": entry["SnatEntryId"], "SnatTableId": entry["SnatTableId"]},
                {**response, "SnatTableEntries": {"SnatTableEntry": [entry]}},
            ),
            "gateway": tool(
                "vpc",
                "DescribeNatGateways",
                {**paging, "NatGatewayId": gateway["NatGatewayId"]},
                {**response, "NatGateways": {"NatGateway": [gateway]}},
            ),
        }
    }


def _legacy_snat(snapshot):
    return next(item for item in snapshot["items"] if item["resourceType"] == "ALIYUN::ECS::SNatEntry")


@pytest.mark.asyncio
async def test_legacy_snat_exact_reads_enrich_and_retract_parent_chain(bridge):
    controller, _ = bridge
    history = _legacy_snat_history()
    snapshot = controller.hydrate(history)
    original = _legacy_snat(snapshot)
    assert original["attributes"] == {
        "NatGatewayId": "ngw-offline0001",
        "SnatTableId": "stb-offline0001",
        "NetworkType": "intranet",
    }, snapshot
    assert {source["toolUseId"] for source in original["sources"]} == {"ros", "entry", "gateway"}
    assert original["stackId"] == "stack-offline"
    # Replacing only raw result content, not its tool ID, must retract context.
    old_result = history["tools"]["gateway"]["results"]
    history["tools"]["gateway"]["results"] = [{"content": "denied", "isError": True}]
    missing = _legacy_snat(controller.hydrate(history))
    assert missing["referenceId"] == original["referenceId"]
    assert missing["attributes"] == {}
    history["tools"]["gateway"]["results"] = old_result
    assert _legacy_snat(controller.hydrate(history))["attributes"] == original["attributes"]
    # A second successful, conflicting response invalidates old derived facts.
    conflicting = copy.deepcopy(history["tools"]["gateway"])
    body = json.loads(conflicting["results"][0]["content"])
    body["NatGateways"]["NatGateway"][0]["NetworkType"] = "internet"
    conflicting["results"][0]["content"] = json.dumps(body)
    history["tools"]["conflict"] = conflicting
    assert _legacy_snat(controller.hydrate(history))["attributes"] == {}
    del history["tools"]["conflict"]
    assert _legacy_snat(controller.hydrate(history))["attributes"] == original["attributes"]
    body = json.loads(history["tools"]["gateway"]["results"][0]["content"])
    body["NatGateways"]["NatGateway"][0]["NetworkType"] = "internet"
    history["tools"]["gateway"]["results"][0]["content"] = json.dumps(body)
    changed = controller.hydrate(history)
    assert len([item for item in changed["items"] if item["resourceType"] == "ALIYUN::ECS::SNatEntry"]) == 1
    assert _legacy_snat(changed)["attributes"]["NetworkType"] == "internet"
    del history["tools"]["entry"]
    assert _legacy_snat(controller.hydrate(history))["attributes"] == {}
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "turn",
        "account",
        "region",
        "entry-id",
        "table",
        "gateway-id",
        "unbounded",
        "network",
        "error",
        "revoked",
        "external",
        "stack",
        "logical",
        "malformed-table",
    ],
)
async def test_legacy_snat_parent_join_rejects_unproved_context(bridge, fault):
    controller, events = bridge
    history = _legacy_snat_history()
    entry, gateway = history["tools"]["entry"], history["tools"]["gateway"]
    if fault == "turn":
        gateway["turnId"] = "another-turn"
    elif fault == "account":
        alien = role("alien", account_scope="another-account")
        history["tools"]["alien"] = {
            "turnId": "turn-snat",
            "results": [{"resourceReferences": [resource_fact_to_web(alien)]}],
        }
    elif fault == "external":
        history["tools"]["ros"]["toolName"] = "external_api"
    elif fault == "stack":
        history["tools"]["ros"]["input"]["params"]["StackId"] = "different-stack"
    elif fault == "logical":
        history["tools"]["ros"]["input"]["params"]["LogicalResourceId"] = "different-logical"
    elif fault == "region":
        gateway["input"]["region_id"] = "cn-beijing"
    elif fault == "entry-id":
        entry["input"]["params"]["SnatEntryId"] = "snat-unrelated"
    elif fault == "gateway-id":
        gateway["input"]["params"]["NatGatewayId"] = "ngw-unrelated"
    elif fault == "unbounded":
        del entry["input"]["params"]["SnatEntryId"]
    elif fault == "error":
        gateway["results"][0]["isError"] = True
    elif fault == "revoked":
        events.append("assistant.message.tombstone", {"affectedToolUseIds": ["gateway"], "messageId": "deleted"})
    else:
        body = json.loads(gateway["results"][0]["content"])
        row = body["NatGateways"]["NatGateway"][0]
        if fault == "table":
            row["SnatTableIds"]["SnatTableId"] = ["stb-unrelated"]
        elif fault == "network":
            row["NetworkType"] = "unknown"
        elif fault == "malformed-table":
            row["SnatTableIds"]["SnatTableId"] = "stb-offline0001"
        gateway["results"][0]["content"] = json.dumps(body)
    assert _legacy_snat(controller.hydrate(history))["attributes"] == {}
    await controller.close()


def _snat_history_with_api_metadata(network="intranet"):
    history = _legacy_snat_history()
    gateway = history["tools"]["gateway"]["results"][0]
    body = json.loads(gateway["content"])
    body["NatGateways"]["NatGateway"][0]["NetworkType"] = network
    gateway["content"] = json.dumps(body)
    # Real aliyun_api results carry dedicated metadata and therefore do not use
    # the legacy extraction fallback. Exercise both raw and metadata ownership.
    for tool_id in ("entry", "gateway"):
        tool = history["tools"][tool_id]
        request, result = tool["input"], tool["results"][0]
        facts = extract_api_resource_references(
            **request,
            body=json.loads(result["content"]),
            tool_use_id=tool_id,
            account_scope="fake-account",
            console_partition="aliyun",
        )
        assert facts
        result["resourceReferences"] = [resource_fact_to_web(fact) for fact in facts]
    return history


def _snat_items(snapshot):
    return [
        item
        for item in snapshot["items"]
        if item["resourceType"]
        in {
            "ALIYUN::ECS::SNatEntry",
            "ALIYUN::VPC::SnatEntry",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["intranet", "internet"])
async def test_ros_and_api_snat_share_exact_gateway_context_without_merging_sources(bridge, network):
    controller, _ = bridge
    history = _snat_history_with_api_metadata(network)
    snapshot = controller.hydrate(history)
    items = _snat_items(snapshot)
    assert len(items) == 2
    assert len({item["referenceId"] for item in items}) == 2
    for item in items:
        assert item["attributes"]["NetworkType"] == network
        assert item["attributes"]["SnatTableId"] == "stb-offline0001"
        assert item["attributes"]["NatGatewayId"] == "ngw-offline0001"
    api = next(item for item in items if item["resourceType"] == "ALIYUN::VPC::SnatEntry")
    assert {source["toolUseId"] for source in api["sources"]} == {"entry", "gateway"}
    assert "stackId" not in api
    assert {source["toolUseId"] for source in _legacy_snat(snapshot)["sources"]} == {"ros", "entry", "gateway"}
    await controller.flush()
    restored = ResourceReferenceController(controller.path, lambda *args: None)
    assert restored.hydrate(history)["items"] == snapshot["items"]
    await restored.close()
    # Replacing the same raw response retracts the derived network on both
    # identities, while preserving each base source and reference ID.
    history["tools"]["gateway"]["results"] = [{"content": "denied", "isError": True}]
    retracted = _snat_items(controller.hydrate(history))
    assert {item["referenceId"] for item in retracted} == {item["referenceId"] for item in items}
    assert all("NetworkType" not in item["attributes"] for item in retracted)
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["turn", "account", "region", "table", "entry-id", "unbounded", "owner", "conflict"])
async def test_api_snat_network_requires_its_own_exact_row_and_unique_gateway(bridge, fault):
    controller, _ = bridge
    history = _snat_history_with_api_metadata()
    gateway = history["tools"]["gateway"]
    if fault == "turn":
        gateway["turnId"] = "different-turn"
    elif fault == "account":
        gateway["results"][0]["resourceReferences"][0]["accountScope"] = "different-account"
    elif fault == "region":
        gateway["input"]["region_id"] = "cn-beijing"
    elif fault == "table":
        body = json.loads(gateway["results"][0]["content"])
        body["NatGateways"]["NatGateway"][0]["SnatTableIds"]["SnatTableId"] = ["stb-unrelated"]
        gateway["results"][0]["content"] = json.dumps(body)
    elif fault == "entry-id":
        history["tools"]["entry"]["input"]["params"]["SnatEntryId"] = "snat-unrelated"
    elif fault == "unbounded":
        del history["tools"]["entry"]["input"]["params"]["SnatEntryId"]
    elif fault == "owner":
        # A same-turn reference with the same ID attached to another tool
        # cannot borrow the exact row's identity/revision.
        forged = copy.deepcopy(history["tools"]["entry"])
        for fact in forged["results"][0]["resourceReferences"]:
            for source in fact["sources"]:
                source["toolUseId"] = "wrong-owner"
        forged["input"]["params"]["SnatEntryId"] = "snat-unrelated"
        # Keep dedicated non-SNAT metadata on the real row so legacy
        # extraction does not independently contribute a valid API SNAT fact.
        other = copy.deepcopy(gateway["results"][0]["resourceReferences"][0])
        for source in other["sources"]:
            source["toolUseId"] = "entry"
        history["tools"]["entry"]["results"][0]["resourceReferences"] = [other]
        history["tools"]["wrong-owner"] = forged
    elif fault == "conflict":
        conflicting = copy.deepcopy(gateway)
        body = json.loads(conflicting["results"][0]["content"])
        body["NatGateways"]["NatGateway"][0]["NetworkType"] = "internet"
        conflicting["results"][0]["content"] = json.dumps(body)
        history["tools"]["conflict"] = conflicting
    items = _snat_items(controller.hydrate(history))
    if fault == "owner":
        api = next(
            item
            for item in items
            if item["resourceType"] == "ALIYUN::VPC::SnatEntry"
            and any(source["toolUseId"] == "wrong-owner" for source in item["sources"])
        )
        assert "NetworkType" not in api["attributes"]
    else:
        assert all("NetworkType" not in item["attributes"] for item in items)
    await controller.close()


@pytest.mark.asyncio
async def test_hydrated_ros_and_api_snat_build_the_same_private_console_destination(bridge, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    controller, _ = bridge
    snapshot = controller.hydrate(_snat_history_with_api_metadata())
    script = tmp_path / "snat-parent-routes.mjs"
    rules = Path(__file__).parents[2] / "src/iac_code/web/static/js/resource_link_rules.js"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__="
        + json.dumps(resource_link_catalog_payload())
        + ";\n"
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps(rules.as_uri())
        + ");\n"
        + "const items = "
        + json.dumps(_snat_items(snapshot))
        + ";\n"
        + """
assert.equal(items.length, 2);
assert.notEqual(items[0].resourceType, items[1].resourceType);
for (const reference of items) {
  const rule = rules.RESOURCE_LINK_RULES.find(item => item.sourceType === reference.resourceType
    && item.partition === 'aliyun');
  assert.equal(rules.buildResourceConsoleUrl(rule, reference),
    'https://vpc.console.aliyun.com/vpc-nat/cn-hangzhou/nats/ngw-offline0001/snats');
}
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    await controller.close()


@pytest.mark.asyncio
async def test_api_snat_parent_context_does_not_require_or_inherit_a_ros_source(bridge):
    controller, _ = bridge
    history = _snat_history_with_api_metadata()
    del history["tools"]["ros"]
    items = _snat_items(controller.hydrate(history))
    assert len(items) == 1
    assert items[0]["resourceType"] == "ALIYUN::VPC::SnatEntry"
    assert items[0]["attributes"]["NetworkType"] == "intranet"
    assert {source["toolUseId"] for source in items[0]["sources"]} == {"entry", "gateway"}
    assert "stackId" not in items[0]
    await controller.close()


def _ots_stack_history(*, managed=False):
    parent, table, stack_id = "offline-parent", "offline_table", "stack-offline-ots"
    template = {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Resources": {
            "Table": {"Type": "ALIYUN::OTS::Table", "Properties": {"InstanceName": parent, "TableName": table}}
        },
    }

    def tool(tool_id, product, action, params, body):
        request = {"product": product, "action": action, "params": params, "region_id": "cn-hangzhou"}
        facts = extract_api_resource_references(
            **request, body=body, tool_use_id=tool_id, account_scope="offline-account", console_partition="aliyun"
        )
        return {
            "toolName": "aliyun_api",
            "toolUseId": tool_id,
            "turnId": "turn-ots",
            "messageId": "message-ots",
            "input": request,
            "results": [
                {"content": json.dumps(body), "resourceReferences": [resource_fact_to_web(fact) for fact in facts]}
            ],
        }

    row = {
        "StackId": stack_id,
        "LogicalResourceId": "Table",
        "Status": "CREATE_COMPLETE",
        "ResourceType": "alicloud_ots_table" if managed else "ALIYUN::OTS::Table",
        "PhysicalResourceId": parent + ":" + table if managed else table,
        "ResourceAttributes": []
        if managed
        else [{"ResourceAttributeKey": "TableName", "ResourceAttributeValue": table}],
    }
    return {
        "tools": {
            "ots-create": tool(
                "ots-create",
                "ros",
                "CreateStack",
                {"RegionId": "cn-hangzhou", "TemplateBody": json.dumps(template)},
                {"RequestId": "offline-create", "StackId": stack_id},
            ),
            "ots-state": tool(
                "ots-state",
                "ros",
                "GetStack",
                {"StackId": stack_id},
                {
                    "RequestId": "offline-state",
                    "Stack": {
                        "StackId": stack_id,
                        "RegionId": "cn-hangzhou",
                        "Status": "CREATE_COMPLETE",
                        "StackType": "Terraform" if managed else "ROS",
                    },
                },
            ),
            "ots-child": tool(
                "ots-child", "ros", "GetStackResource", {"StackId": stack_id, "LogicalResourceId": "Table"}, row
            ),
            "ots-list": tool(
                "ots-list",
                "tablestore",
                "ListTable",
                {"InstanceName": parent},
                {"InstanceName": parent, "Tables": [{"InstanceName": parent, "TableName": table}]},
            ),
        }
    }


def _ots_stack_item(snapshot, resource_type="ALIYUN::OTS::Table"):
    return next(
        item
        for item in snapshot["items"]
        if item["resourceType"] == resource_type and item.get("stackId") == "stack-offline-ots"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["tablestore", "Tablestore", "ots"])
async def test_ots_native_parent_chain_retracts_and_query_stays_independent(bridge, product):
    controller, _ = bridge
    history = _ots_stack_history()
    history["tools"]["ots-list"]["input"]["product"] = product
    snapshot = controller.hydrate(history)
    child = _ots_stack_item(snapshot)
    assert child["attributes"] == {"InstanceName": "offline-parent", "TableName": "offline_table"}
    assert {source["toolUseId"] for source in child["sources"]} == {"ots-create", "ots-state", "ots-child", "ots-list"}
    direct = [
        item for item in snapshot["items"] if item["resourceType"] == "ALIYUN::OTS::Table" and not item.get("stackId")
    ]
    assert len(direct) == 1 and direct[0]["referenceId"] != child["referenceId"]
    # A replaced inline template is a new source version, even with the same
    # StackId/tool IDs. Direct QUERY presence must not retain the old ROS parent.
    saved = history["tools"]["ots-create"]["input"]["params"]["TemplateBody"]
    template = json.loads(saved)
    template["Resources"]["Table"]["Properties"]["InstanceName"] = "different-parent"
    history["tools"]["ots-create"]["input"]["params"]["TemplateBody"] = json.dumps(template)
    assert "InstanceName" not in _ots_stack_item(controller.hydrate(history))["attributes"]
    history["tools"]["ots-create"]["input"]["params"]["TemplateBody"] = saved
    assert _ots_stack_item(controller.hydrate(history))["attributes"]["InstanceName"] == "offline-parent"
    del history["tools"]["ots-state"]
    assert "InstanceName" not in _ots_stack_item(controller.hydrate(history))["attributes"]
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "turn",
        "account",
        "region",
        "stack",
        "logical",
        "error",
        "external",
        "missing-create",
        "missing-survival",
        "template-url",
        "intrinsic",
        "table",
        "type",
        "conflict",
        "not-live",
        "revoked",
        "conflicting-state",
    ],
)
async def test_ots_native_parent_requires_exact_original_sources(bridge, fault):
    controller, events = bridge
    history = _ots_stack_history()
    tools = history["tools"]
    create, state, child, table_list = [tools[key] for key in ("ots-create", "ots-state", "ots-child", "ots-list")]
    if fault == "turn":
        create["turnId"] = "different-turn"
    elif fault == "account":
        create["results"][0]["resourceReferences"][0]["accountScope"] = "different-account"
    elif fault == "region":
        create["input"]["region_id"] = "cn-beijing"
    elif fault == "stack":
        create["results"][0]["content"] = json.dumps({"RequestId": "offline", "StackId": "different-stack"})
    elif fault == "logical":
        child["input"]["params"]["LogicalResourceId"] = "DifferentLogical"
    elif fault == "error":
        create["results"][0]["isError"] = True
    elif fault == "external":
        create["toolName"] = "external_api"
    elif fault == "missing-create":
        del tools["ots-create"]
    elif fault == "missing-survival":
        table_list["results"][0]["content"] = json.dumps({"InstanceName": "offline-parent", "Tables": []})
    elif fault == "template-url":
        create["input"]["params"]["TemplateURL"] = "https://example.invalid/template"
    elif fault in {"intrinsic", "table", "type"}:
        template = json.loads(create["input"]["params"]["TemplateBody"])
        resource = template["Resources"]["Table"]
        if fault == "intrinsic":
            resource["Properties"]["InstanceName"] = {"Ref": "Parent"}
        elif fault == "table":
            resource["Properties"]["TableName"] = "different_table"
        else:
            resource["Type"] = "ALIYUN::OTS::Instance"
        create["input"]["params"]["TemplateBody"] = json.dumps(template)
    elif fault == "conflict":
        extra = copy.deepcopy(create)
        template = json.loads(extra["input"]["params"]["TemplateBody"])
        template["Resources"]["Table"]["Properties"]["InstanceName"] = "different-parent"
        extra["input"]["params"]["TemplateBody"] = json.dumps(template)
        tools["other-create"] = extra
    elif fault == "not-live":
        body = json.loads(state["results"][0]["content"])
        body["Stack"]["Status"] = "CREATE_IN_PROGRESS"
        state["results"][0]["content"] = json.dumps(body)
    elif fault == "conflicting-state":
        other = copy.deepcopy(state)
        other["toolUseId"] = "ots-state-conflict"
        body = json.loads(other["results"][0]["content"])
        body["Stack"]["Status"] = "DELETE_COMPLETE"
        other["results"][0]["content"] = json.dumps(body)
        tools["ots-state-conflict"] = other
    elif fault == "revoked":
        events.append("assistant.message.tombstone", {"affectedToolUseIds": ["ots-create"]})
    assert "InstanceName" not in _ots_stack_item(controller.hydrate(history))["attributes"]
    await controller.close()


@pytest.mark.asyncio
async def test_ots_managed_physical_locator_routes_to_child_not_composite(bridge, tmp_path):
    controller, _ = bridge
    history = _ots_stack_history(managed=True)
    child = _ots_stack_item(controller.hydrate(history), "alicloud_ots_table")
    assert child["identifier"] == "offline-parent:offline_table"
    assert child["attributes"] == {"InstanceName": "offline-parent", "TableName": "offline_table"}
    assert child["aliases"] == []
    assert {source["toolUseId"] for source in child["sources"]} == {"ots-child"}
    node = shutil.which("node")
    if node:
        script = tmp_path / "ots-route.mjs"
        script.write_text(
            "globalThis.__IAC_RESOURCE_LINKS__=" + json.dumps(resource_link_catalog_payload()) + ";\n"
            "import assert from 'node:assert/strict';\n"
            "const rules=await import("
            + json.dumps((Path(__file__).parents[2] / "src/iac_code/web/static/js/resource_link_rules.js").as_uri())
            + ");\nconst ref="
            + json.dumps(child)
            + ";\n"
            "const resolved=rules.resolveResourceOccurrence({...ref,semantic:true,provider:'aliyun'},"
            "{candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});\n"
            "assert.equal(resolved.url,'https://ots.console.aliyun.com/cn-hangzhou/"
            "offline-parent/offline_table/dataManage',JSON.stringify(resolved));\n",
            encoding="utf-8",
        )
        result = subprocess.run([node, str(script)], capture_output=True, encoding="utf-8", timeout=20)
        assert result.returncode == 0, result.stderr
    await controller.close()


@pytest.mark.asyncio
async def test_ots_managed_list_stack_resources_keeps_own_source(bridge):
    controller, _ = bridge
    history = _ots_stack_history(managed=True)
    tool = history["tools"]["ots-child"]
    row = json.loads(tool["results"][0]["content"])
    row["LogicalResourceId"] = "alicloud_ots_table.table"
    body = {"RequestId": "offline-resource-list", "Resources": [row]}
    tool["input"]["action"] = "ListStackResources"
    tool["input"]["params"] = {"StackId": "stack-offline-ots"}
    tool["results"][0]["content"] = json.dumps(body)
    facts = extract_api_resource_references(
        **tool["input"],
        body=body,
        tool_use_id="ots-child",
        account_scope="offline-account",
        console_partition="aliyun",
    )
    tool["results"][0]["resourceReferences"] = [resource_fact_to_web(fact) for fact in facts]
    child = _ots_stack_item(controller.hydrate(history), "alicloud_ots_table")
    assert child["identifier"] == "offline-parent:offline_table"
    assert child["logicalResourceId"] == "alicloud_ots_table.table"
    assert child["attributes"] == {"InstanceName": "offline-parent", "TableName": "offline_table"}
    assert {(source["toolUseId"], source["action"]) for source in child["sources"]} == {
        ("ots-child", "ListStackResources")
    }
    await controller.close()


@pytest.mark.asyncio
async def test_ots_native_final_reads_replace_earlier_polling_state(bridge):
    controller, _ = bridge
    history = _ots_stack_history()
    final = copy.deepcopy(history["tools"]["ots-state"])
    early = history["tools"]["ots-state"]
    body = json.loads(early["results"][0]["content"])
    body["Stack"]["Status"] = "CREATE_IN_PROGRESS"
    early["results"][0]["content"] = json.dumps(body)
    final["toolUseId"] = "ots-final-state"
    history["tools"]["ots-final-state"] = final
    assert _ots_stack_item(controller.hydrate(history))["attributes"]["InstanceName"] == "offline-parent"
    absent = copy.deepcopy(history["tools"]["ots-list"])
    absent["toolUseId"] = "ots-final-list"
    absent["results"][0]["content"] = json.dumps({"InstanceName": "offline-parent", "Tables": []})
    history["tools"]["ots-final-list"] = absent
    assert "InstanceName" not in _ots_stack_item(controller.hydrate(history))["attributes"]
    await controller.close()
