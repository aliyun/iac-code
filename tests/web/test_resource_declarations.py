"""Explicit text choices are source-scoped declarations, not cloud observations."""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest

from iac_code.tools.cloud.resource_references import resource_spec
from iac_code.web.resource_reference_controller import ResourceReferenceController
from iac_code.web.resource_references import ResourceRevisionConflictError
from iac_code.web.resource_sources import read_visible_source


def visible_source(text):
    seed = {
        "resourceType": "ALIYUN::RAM::Role",
        "identifier": "SeedRole",
        "identifierKind": "name",
        "identifierField": "RoleName",
        "accountScope": "fake-account",
        "consolePartition": "aliyun",
        "evidence": "api_response",
        "sources": [{"toolUseId": "tool-seed", "sourcePath": "/Role/RoleName"}],
    }
    return {
        "messages": [{"messageId": "user-1", "text": text}],
        "tools": {
            "tool-seed": {
                "toolName": "aliyun_api",
                "toolUseId": "tool-seed",
                "input": {"product": "ram", "action": "GetRole", "params": {"RoleName": "SeedRole"}},
                "results": [{"content": '{"Role":{"RoleName":"SeedRole"}}', "resourceReferences": [seed]}],
            }
        },
    }


def request(controller, visible, resource_type, identifier, *, owner="user-1", region="cn-hangzhou", attributes=None):
    spec = resource_spec(resource_type)
    assert spec is not None
    snapshot = controller.hydrate(visible)
    source = {"message_id": owner, "source_path": "/text"}
    return {
        "expectedRevision": snapshot["revision"],
        "source": {"messageId": owner, "sourcePath": "/text"},
        "sourceDigest": read_visible_source(visible, source).digest,
        "group": [
            spec.identifier_kind,
            identifier,
            spec.identifier_field,
            spec.resource_type,
            None,
            None,
            None,
            [],
            {"kind": "explicit_type"} if spec.identifier_kind == "name" else None,
            None,
        ],
        "identifier": identifier,
        "identifierKind": spec.identifier_kind,
        "context": {
            "accountScope": "fake-account",
            "consolePartition": "aliyun",
            "attributes": attributes or {},
            **({"regionId": region} if spec.scope_kind == "regional" else {}),
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_type", "identifier", "text", "attributes"),
    [
        ("ALIYUN::RAM::Role", "NewRole", "RAM role NewRole", {}),
        ("ALIYUN::ECS::Instance", "i-design123456", "实例 i-design123456", {}),
        ("ALIYUN::ECS::Instance", "i-design123456", "Instance i-design123456.", {}),
        ("ALIYUN::FC::Function", "Handler", "FC function Handler", {"ServiceName": "ServiceA"}),
        (
            "ALIYUN::ROS::Stack",
            "11111111-2222-4333-8444-555555555555",
            'StackId: "11111111-2222-4333-8444-555555555555"',
            {},
        ),
    ],
)
async def test_declaration_roundtrip_and_idempotent_save(tmp_path, resource_type, identifier, text, attributes):
    path = tmp_path / "resources.json"
    controller = ResourceReferenceController(path, lambda *args: None)
    visible = visible_source(text)
    body = request(controller, visible, resource_type, identifier, attributes=attributes)
    result = await controller.resolve_choice(visible, body)
    assert result["status"] == "saved"
    target = next(item for item in result["snapshot"]["items"] if item["referenceId"] == result["referenceId"])
    assert target["identifier"] == identifier
    assert target["evidence"] == "user_context"
    assert all(source["sourceId"].startswith("web-choice:") for source in target["sources"])
    assert target["aliases"] == []
    if resource_type == "ALIYUN::RAM::Role":
        assert "regionId" not in target
    await controller.close()
    restored = ResourceReferenceController(path, lambda *args: None)
    after = restored.hydrate(visible)
    assert after["bindings"] == result["snapshot"]["bindings"]
    retry = {**body, "expectedRevision": after["revision"]}
    repeated = await restored.resolve_choice(visible, retry)
    assert repeated["referenceId"] == result["referenceId"]
    assert repeated["revision"] == result["revision"]
    await restored.close()


@pytest.mark.asyncio
async def test_identical_names_in_two_sources_keep_independent_dependencies(tmp_path):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = visible_source("RAM role NewRole")
    visible["messages"].append({"messageId": "user-2", "text": "RAM role NewRole"})
    first = await controller.resolve_choice(visible, request(controller, visible, "ALIYUN::RAM::Role", "NewRole"))
    second = await controller.resolve_choice(
        visible, request(controller, visible, "ALIYUN::RAM::Role", "NewRole", owner="user-2")
    )
    assert first["referenceId"] == second["referenceId"]
    assert len(second["snapshot"]["bindings"]) == 2
    visible["messages"].pop(0)
    after = controller.hydrate(visible)
    assert len(after["bindings"]) == 1
    target = next(item for item in after["items"] if item["referenceId"] == second["referenceId"])
    assert len(target["sources"]) == 1
    visible["messages"][0]["text"] += " changed"
    after = controller.hydrate(visible)
    assert not after["bindings"]
    assert all(item["referenceId"] != second["referenceId"] for item in after["items"])
    assert any(item["identifier"] == "SeedRole" for item in after["items"])
    await controller.close()


@pytest.mark.asyncio
async def test_reselecting_parent_does_not_rewrite_previous_identity(tmp_path):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = visible_source("FC function Handler")
    a = request(controller, visible, "ALIYUN::FC::Function", "Handler", attributes={"ServiceName": "ServiceA"})
    first = await controller.resolve_choice(visible, a)
    b = {
        **a,
        "expectedRevision": controller.store.revision,
        "context": {**a["context"], "attributes": {"ServiceName": "ServiceB"}},
    }
    second = await controller.resolve_choice(visible, b)
    assert second["referenceId"] != first["referenceId"]
    assert all(item["referenceId"] != first["referenceId"] for item in second["snapshot"]["items"])
    target = next(item for item in second["snapshot"]["items"] if item["referenceId"] == second["referenceId"])
    assert target["attributes"] == {"ServiceName": "ServiceB"}
    await controller.close()


@pytest.mark.asyncio
async def test_real_fact_outlives_removed_declaration(tmp_path):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = visible_source("RAM role NewRole")
    result = await controller.resolve_choice(visible, request(controller, visible, "ALIYUN::RAM::Role", "NewRole"))
    tool = copy.deepcopy(visible["tools"]["tool-seed"])
    tool["toolUseId"] = "tool-real"
    tool["input"]["params"]["RoleName"] = "NewRole"
    tool["results"][0]["content"] = '{"Role":{"RoleName":"NewRole"}}'
    fact = tool["results"][0]["resourceReferences"][0]
    fact["identifier"] = "NewRole"
    fact["sources"][0]["toolUseId"] = "tool-real"
    visible["tools"]["tool-real"] = tool
    visible["messages"].clear()
    after = controller.hydrate(visible)
    target = next(item for item in after["items"] if item["referenceId"] == result["referenceId"])
    assert target["evidence"] == "api_response"
    assert target["sources"][0]["toolUseId"] == "tool-real"
    assert len(target["sources"]) == 1 and not after["bindings"]
    await controller.close()


@pytest.mark.asyncio
async def test_declaration_cannot_bypass_an_existing_observed_identity(tmp_path):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = visible_source("RAM role SeedRole")
    body = request(controller, visible, "ALIYUN::RAM::Role", "SeedRole")
    with pytest.raises(ResourceRevisionConflictError, match="resource_reference_required"):
        await controller.resolve_choice(visible, body)
    assert len(controller.store.references) == 1 and not controller.store.bindings
    await controller.close()


@pytest.mark.asyncio
async def test_tombstone_immediately_retracts_declaration_and_binding(tmp_path):
    events = []
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda kind, data: events.append(data))
    visible = visible_source("RAM role NewRole")
    result = await controller.resolve_choice(visible, request(controller, visible, "ALIYUN::RAM::Role", "NewRole"))
    controller.on_event({"type": "assistant.message.tombstone", "payload": {"messageId": "user-1"}})
    assert result["referenceId"] not in controller.store.references
    assert not controller.store.bindings
    assert any(event.get("removedBindings") for event in events)
    assert any(result["referenceId"] in event.get("removedReferenceIds", []) for event in events)
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_type", "identifier", "text"),
    [
        ("ALIYUN::RAM::Role", "admin", "admin"),
        ("ALIYUN::RAM::Role", "admin", 'DisplayName: "admin"'),
        ("ALIYUN::ECS::Instance", "i-design123456", "RequestId: i-design123456"),
        ("ALIYUN::ECS::Instance", "i-design123456", '{"Type":"ALIYUN::REDIS::Instance","InstanceId":"i-design123456"}'),
        ("ALIYUN::ROS::Stack", "11111111-2222-4333-8444-555555555555", "11111111-2222-4333-8444-555555555555"),
        ("ALIYUN::ECS::Instance", "i-design123456", "https://example.test/i-design123456"),
    ],
)
async def test_declaration_requires_independent_resource_semantics(tmp_path, resource_type, identifier, text):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = visible_source(text)
    before = controller.hydrate(visible)
    with pytest.raises(ValueError, match="resource_occurrence_unverified"):
        await controller.resolve_choice(visible, request(controller, visible, resource_type, identifier))
    assert controller.snapshot()["items"] == before["items"]
    assert not controller.store.bindings
    await controller.close()


@pytest.mark.asyncio
async def test_empty_index_account_choice_is_source_scoped_and_survives_without_credentials(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    visible = visible_source("RAM role NewRole")
    visible["tools"] = {}
    visible["messages"].append({"messageId": "user-2", "text": "RAM role NewRole"})
    body = request(controller, visible, "ALIYUN::RAM::Role", "NewRole")
    context = {"account_scope": "credential:fake", "console_partition": "aliyun"}
    choice = controller.offer_account_context(context)["choice"]
    assert not controller.snapshot()["items"] and not controller.path.exists()
    assert controller.offer_account_context(context)["choice"] == choice
    body.update(accountContextId=choice["accountContextId"])
    body["context"]["accountScope"] = choice["accountScope"]
    load = AsyncMock(return_value=context)
    result = await controller.resolve_choice(visible, body, load_account_context=load)
    load.assert_awaited_once()
    assert result["status"] == "saved" and len(result["snapshot"]["items"]) == 1
    target = result["snapshot"]["items"][0]
    assert target["evidence"] == "user_context" and "regionId" not in target
    assert result["snapshot"]["bindings"][0]["source"]["messageId"] == "user-1"
    assert len(result["snapshot"]["bindings"]) == 1
    assert choice["accountContextId"] not in controller.path.read_text(encoding="utf-8")
    await controller.close()
    restored = ResourceReferenceController(controller.path, lambda *args: None)
    snapshot = restored.hydrate(visible)
    assert snapshot["items"] == result["snapshot"]["items"]
    assert snapshot["bindings"] == result["snapshot"]["bindings"]
    assert restored._account_context is None
    # Re-saving a historical selection has no implicit credential read.
    body.pop("accountContextId")
    body["expectedRevision"] = snapshot["revision"]
    no_load = AsyncMock(side_effect=AssertionError("history must not load credentials"))
    assert (await restored.resolve_choice(visible, body, load_account_context=no_load))["status"] == "saved"
    no_load.assert_not_called()
    await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper", ["missing_token", "foreign_token", "account", "partition", "rotated", "removed", "closed"]
)
async def test_account_choice_rejects_unoffered_or_changed_context_before_persistence(tmp_path, tamper):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    visible = visible_source("RAM role NewRole")
    visible["tools"] = {}
    body = request(controller, visible, "ALIYUN::RAM::Role", "NewRole")
    context = {"account_scope": "credential:fake", "console_partition": "aliyun"}
    choice = controller.offer_account_context(context)["choice"]
    body["accountContextId"] = choice["accountContextId"]
    body["context"]["accountScope"] = choice["accountScope"]
    if tamper == "missing_token":
        body.pop("accountContextId")
    if tamper == "foreign_token":
        other = ResourceReferenceController(tmp_path / "other.json", lambda *args: None)
        body["accountContextId"] = other.offer_account_context(context)["choice"]["accountContextId"]
        await other.close()
    if tamper == "account":
        body["context"]["accountScope"] = "credential:other"
    if tamper == "partition":
        body["context"]["consolePartition"] = "alibabacloud"

    async def load():
        if tamper == "closed":
            controller.close_for_delete()
        if tamper == "rotated":
            return {**context, "account_scope": "credential:other"}
        return {} if tamper == "removed" else context

    with pytest.raises(ValueError):
        await controller.resolve_choice(visible, body, load_account_context=load)
    assert not controller.store.references and not controller.store.bindings
    assert not controller.path.exists()
    await controller.close()
