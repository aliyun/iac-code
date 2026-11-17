from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from iac_code.tools.cloud.resource_references import resource_spec
from iac_code.web.resource_link_catalog import resource_link_catalog_payload
from iac_code.web.resource_reference_controller import ResourceReferenceController
from iac_code.web.resource_references import ResourceRevisionConflictError, resource_fact_to_web
from iac_code.web.resource_sources import (
    VisibleResourceSource,
    canonical_source_json,
    read_visible_source,
    resource_binding_source_key,
    source_contains_locator,
    tool_source_value,
)


def transcript():
    fact = {
        "resource_type": "ALIYUN::RAM::Role",
        "identifier": "DeployRole",
        "identifier_kind": "name",
        "identifier_field": "RoleName",
        "account_scope": "fake-account",
        "console_partition": "aliyun",
        "evidence": "api_response",
        "sources": [{"tool_use_id": "tool-1", "source_path": "/Role/RoleName"}],
    }
    return {
        "messages": [{"messageId": "user-1", "role": "user", "text": "查看 RAM 角色 `DeployRole`。"}],
        "tools": {
            "tool-1": {
                "toolUseId": "tool-1",
                "toolName": "aliyun_api",
                "input": {"product": "ram", "action": "GetRole", "params": {"RoleName": "DeployRole"}},
                "results": [
                    {
                        "content": '{"Role":{"RoleName":"DeployRole"}}',
                        "resourceReferences": [resource_fact_to_web(fact)],
                    }
                ],
            }
        },
    }


def choice(controller, visible, *, source=None):
    source = source or {"message_id": "user-1", "source_path": "/text"}
    snapshot = controller.hydrate(visible)
    ref = snapshot["items"][0]
    group = [
        "name",
        "DeployRole",
        "RoleName",
        "ALIYUN::RAM::Role",
        None,
        None,
        None,
        [],
        {"kind": "explicit_type"},
        None,
    ]
    return {
        "expectedRevision": snapshot["revision"],
        "source": resource_fact_to_web(source),
        "sourceDigest": read_visible_source(visible, source).digest,
        "group": group,
        "identifier": "DeployRole",
        "identifierKind": "name",
        "referenceId": ref["referenceId"],
        "context": {"accountScope": "fake-account", "consolePartition": "aliyun", "attributes": {}},
    }


@pytest.mark.asyncio
async def test_choice_roundtrip_revalidates_source_without_storing_body(tmp_path):
    events = []
    path = tmp_path / "resources.json"
    controller = ResourceReferenceController(path, lambda kind, payload: events.append((kind, payload)))
    visible = transcript()
    request = choice(controller, visible)
    result = await controller.resolve_choice(visible, request)
    assert result["status"] == "saved"
    assert result["sourceKey"] == resource_binding_source_key(request["sourceDigest"], request["group"])
    assert result["snapshot"]["bindings"][0]["context"]["accountScope"] == "fake-account"
    assert "regionId" not in result["snapshot"]["bindings"][0]["context"]
    assert visible["messages"][0]["text"] not in path.read_text(encoding="utf-8")
    await controller.close()

    restored = ResourceReferenceController(path, lambda *args: None)
    snapshot = restored.hydrate(visible)
    assert snapshot["bindings"] == result["snapshot"]["bindings"]
    # Re-saving the same selection does not increment revision or write again.
    retry = {**request, "expectedRevision": snapshot["revision"]}
    assert (await restored.resolve_choice(visible, retry))["revision"] == snapshot["revision"]
    await restored.close()


@pytest.mark.asyncio
async def test_changed_source_revokes_binding_but_keeps_resource_fact(tmp_path):
    events = []
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda kind, payload: events.append(payload))
    visible = transcript()
    request = choice(controller, visible)
    await controller.resolve_choice(visible, request)
    visible["messages"][0]["text"] = "这条消息已经改成普通文本。"
    after = controller.hydrate(visible)
    assert after["items"]
    assert not after["bindings"]
    assert any(event.get("removedBindings") for event in events)
    with pytest.raises(ResourceRevisionConflictError):
        await controller.resolve_choice(visible, {**request, "expectedRevision": after["revision"]})
    await controller.close()


@pytest.mark.asyncio
async def test_hydration_reuses_source_proof_only_within_current_snapshot(tmp_path, monkeypatch):
    import iac_code.web.resource_reference_controller as module

    visible = transcript()
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    request = choice(controller, visible)
    await controller.resolve_choice(visible, request)
    second = copy.deepcopy(request)
    second["group"][-1] = "text-object:1"
    second["expectedRevision"] = controller.store.revision
    await controller.resolve_choice(visible, second)
    reads = proofs = 0
    original_read, original_proof = module.read_visible_source, module.source_contains_locator

    def read(*args):
        nonlocal reads
        reads += 1
        return original_read(*args)

    def proof(*args, **kwargs):
        nonlocal proofs
        proofs += 1
        return original_proof(*args, **kwargs)

    monkeypatch.setattr(module, "read_visible_source", read)
    monkeypatch.setattr(module, "source_contains_locator", proof)
    assert len(controller.hydrate(visible)["bindings"]) == 2
    assert (reads, proofs) == (1, 1)
    visible["messages"][0]["text"] = "source replaced"
    assert not controller.hydrate(visible)["bindings"]
    assert reads == 2
    await controller.close()


@pytest.mark.asyncio
async def test_concurrent_choice_cannot_acknowledge_another_tabs_selection(tmp_path, monkeypatch):
    visible = transcript()
    other_tool = copy.deepcopy(visible["tools"]["tool-1"])
    other_tool["toolUseId"] = "tool-2"
    fact = other_tool["results"][0]["resourceReferences"][0]
    fact["accountScope"] = "another-fake-account"
    fact["sources"][0]["toolUseId"] = "tool-2"
    visible["tools"]["tool-2"] = other_tool
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    first = choice(controller, visible)
    other = next(item for item in controller.snapshot()["items"] if item["accountScope"] == "another-fake-account")
    started, release = asyncio.Event(), asyncio.Event()
    original = controller.flush

    async def delayed_flush():
        if not started.is_set():
            started.set()
            await release.wait()
        return await original()

    monkeypatch.setattr(controller, "flush", delayed_flush)
    task = asyncio.create_task(controller.resolve_choice(visible, first))
    await asyncio.wait_for(started.wait(), 2)
    second = {
        **first,
        "expectedRevision": controller.store.revision,
        "referenceId": other["referenceId"],
        "context": {"accountScope": "another-fake-account", "consolePartition": "aliyun", "attributes": {}},
    }
    try:
        assert (await controller.resolve_choice(visible, second))["status"] == "saved"
    finally:
        release.set()
    with pytest.raises(ResourceRevisionConflictError, match="resource_choice_changed"):
        await asyncio.wait_for(task, 2)
    assert controller.snapshot()["bindings"][0]["referenceId"] == other["referenceId"]
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "DeployRole",
        "DisplayName: DeployRole",
        '{"LogicalResourceId":"DeployRole"}',
        "https://example.test/RoleName:DeployRole",
        "RAM role DeployRoleBackup",
    ],
)
async def test_name_choice_requires_independent_source_semantics(tmp_path, text):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = transcript()
    visible["messages"][0]["text"] = text
    request = choice(controller, visible)
    with pytest.raises(ValueError, match="resource_occurrence_unverified"):
        await controller.resolve_choice(visible, request)
    assert not controller.store.bindings
    await controller.close()


@pytest.mark.asyncio
async def test_tool_source_can_be_bound_but_hidden_or_old_tool_content_cannot(tmp_path):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = transcript()
    request = choice(controller, visible, source={"tool_use_id": "tool-1", "source_path": "/tool"})
    assert (await controller.resolve_choice(visible, request))["status"] == "saved"
    visible["tools"]["tool-1"]["input"]["params"]["RoleName"] = "ReplacementRole"
    assert not controller.hydrate(visible)["bindings"]
    missing = {
        **request,
        "expectedRevision": controller.store.revision,
        "source": {"toolUseId": "hidden-tool", "sourcePath": "/tool"},
    }
    with pytest.raises(ValueError, match="resource_source_unavailable"):
        await controller.resolve_choice(visible, missing)
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"url": "https://evil.test"},
        {"queryApi": "DeleteRole"},
        {"expectedRevision": True},
        {"context": {"accountScope": "not-an-observed-account"}},
        {"context": {"attributes": {"Secret": "fake-value"}}},
        {"source": {"messageId": "user-1", "sourcePath": "/hidden"}},
        {"referenceId": "ref-not-visible"},
    ],
)
async def test_choice_rejects_unscoped_targets_and_arbitrary_fields(tmp_path, override):
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    visible = transcript()
    request = {**choice(controller, visible), **override}
    with pytest.raises(ValueError):
        await controller.resolve_choice(visible, request)
    assert not controller.store.bindings
    await controller.close()


def test_source_encoding_has_no_number_array_collision_and_preserves_unicode():
    assert canonical_source_json(1) == canonical_source_json(1.0)
    assert canonical_source_json(1) != canonical_source_json(["number", "3ff0000000000000"])
    assert canonical_source_json({"b": "角色", "a": -0.0}) == canonical_source_json({"a": -0.0, "b": "角色"})
    assert canonical_source_json(0) != canonical_source_json(-0.0)


@pytest.mark.asyncio
async def test_javascript_generated_choices_roundtrip_through_python_store(tmp_path):
    """Do not use hand-written group/digest fixtures to prove wire compatibility."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    modules = (Path(__file__).parents[2] / "src/iac_code/web/static/js").as_uri()
    samples = [
        "RAM 角色 `DeployRole`",
        '{"RoleName":"Deploy\\u0052ole"}',
        "RoleName: DeployRole",
        'resource "alicloud_ram_role" "example" { name = "DeployRole" }',
        "aliyun ram GetRole --RoleName DeployRole",
    ]
    script = tmp_path / "source-contract.mjs"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__="
        + json.dumps(resource_link_catalog_payload(), ensure_ascii=False)
        + ";\n"
        + f"const base = {json.dumps(modules)};\n"
        + r"""
import fs from 'node:fs';
import assert from 'node:assert/strict';
const sources = await import(`${base}/resource_sources.js`);
const {scanResourceOccurrences:scan} = await import(`${base}/resource_occurrences.js`);
const {ResourceIndex} = await import(`${base}/resource_index.js`);
const {canonicalResourceType} = await import(`${base}/resource_link_rules.js`);
const input = JSON.parse(fs.readFileSync(0,'utf8'));
const index = new ResourceIndex({canonicalType:canonicalResourceType});
await index.loadSnapshot(input.snapshot,()=>assert.fail('no network'));
const canonical = sources.canonicalSourceJson({'\u{10000}':-0,'\uE000':[1,1.5,true,null,'角色😀']});
if (input.restore) {
  for (const binding of input.snapshot.bindings) {
    const source = input.sources.find((item)=>item.source.messageId===binding.source.messageId);
    const digest = await sources.digestResourceSource(source.source,source.value);
    const match = scan(source.value,{index})[0];
    const key = sources.resourceBindingSourceKey(digest,sources.resourceOccurrenceGroup(match));
    assert.equal(key,binding.sourceKey);
    assert.equal(index.bindingFor({...match,sourceKey:key}).referenceId,binding.referenceId);
  }
  console.log(JSON.stringify({restored:true,canonical}));
} else {
  const requests = [];
  for (const source of input.sources) {
    const match = scan(source.value,{index})[0];
    assert.equal(match.identifier,'DeployRole');
    requests.push({source:source.source,sourceDigest:await sources.digestResourceSource(source.source,source.value),
      group:sources.resourceOccurrenceGroup(match),identifier:match.identifier,identifierKind:match.identifierKind,
      referenceId:input.snapshot.items[0].referenceId,context:{accountScope:'fake-account',consolePartition:'aliyun',attributes:{}}});
  }
  console.log(JSON.stringify({requests,canonical}));
}
""",
        encoding="utf-8",
    )
    visible = transcript()
    visible["messages"] = [{"messageId": f"user-{i}", "text": value} for i, value in enumerate(samples)]
    controller = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    snapshot = controller.hydrate(visible)
    sources = [
        {"source": {"messageId": row["messageId"], "sourcePath": "/text"}, "value": row["text"]}
        for row in visible["messages"]
    ]

    def javascript(snapshot, restore=False):
        result = subprocess.run(
            [node, str(script)],
            input=json.dumps({"snapshot": snapshot, "sources": sources, "restore": restore}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    generated = javascript(snapshot)
    assert generated["canonical"] == canonical_source_json(
        {"\U00010000": -0.0, "\ue000": [1, 1.5, True, None, "角色😀"]}
    )
    for request in generated["requests"]:
        request["expectedRevision"] = controller.store.revision
        assert (await controller.resolve_choice(visible, request))["status"] == "saved"
    await controller.close()
    restored = ResourceReferenceController(tmp_path / "resources.json", lambda *args: None)
    after = restored.hydrate(visible)
    assert len(after["bindings"]) == len(samples)
    assert javascript(after, restore=True)["restored"]
    await restored.close()


@pytest.mark.parametrize(
    ("value", "matches"),
    [
        ("| RoleName |\n| --- |\n| DeployRole |", True),
        ("| DisplayName |\n| --- |\n| DeployRole |", False),
        ('resource "alicloud_ram_role" "DeployRole" {}', False),
        ('resource "alicloud_ram_user" "example" { name = "DeployRole" }', False),
        ('resource "alicloud_ram_role" "example" { name = "${DeployRole}" }', False),
        ("RAM role DeployRole.", True),
        ('RAM role "DeployRole."', False),
        ("RAM role DeployRole...", False),
        ("RAM role DeployRole.suffix.", False),
    ],
)
def test_source_proof_requires_real_name_fields(value, matches):
    spec = resource_spec("ALIYUN::RAM::Role")
    assert spec is not None
    assert source_contains_locator(VisibleResourceSource({}, value), "DeployRole", "name", spec) is matches


def test_tool_digest_parity_and_progress_display_boundaries(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    module = (Path(__file__).parents[2] / "src/iac_code/web/static/js/resource_sources.js").as_uri()
    tool = transcript()["tools"]["tool-1"]
    tool["input"]["ratio"] = 1.0
    tool["stackProgress"] = {
        "stackId": "11111111-2222-4333-8444-555555555555",
        "elapsedSeconds": 1,
        "resources": [
            {
                "resource_type": "ALIYUN::RAM::Role",
                "name": "LogicalOnly",
                "physical_resource_id": "OpaqueValue",
                "attributes": {"RoleName": "ProgressRole", "Secret": "fake-hidden-attribute"},
            }
        ],
    }
    source = {"tool_use_id": "tool-1", "source_path": "/tool"}
    expected = VisibleResourceSource(source, tool_source_value(tool)).digest
    script = tmp_path / "tool-source.mjs"
    script.write_text(
        f"const sources = await import({json.dumps(module)});\n"
        + "import fs from 'node:fs';\n"
        + "const tool = JSON.parse(fs.readFileSync(0,'utf8'));\n"
        + "const value = sources.toolResourceSourceValue(tool);\n"
        + "const digest = await sources.digestResourceSource({toolUseId:'tool-1',sourcePath:'/tool'},value);\n"
        + "console.log(JSON.stringify({value,digest}));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)], input=json.dumps(tool), capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual["digest"] == expected
    assert actual["value"] == tool_source_value(tool)
    assert "fake-hidden-attribute" not in result.stdout
    tool["stackProgress"]["elapsedSeconds"] = 99
    tool["input"] = json.dumps(tool["input"])
    assert VisibleResourceSource(source, tool_source_value(tool)).digest == expected
    spec = resource_spec("ALIYUN::RAM::Role")
    assert spec is not None
    assert source_contains_locator(VisibleResourceSource(source, tool_source_value(tool)), "ProgressRole", "name", spec)
    assert not source_contains_locator(
        VisibleResourceSource(source, tool_source_value(tool)), "LogicalOnly", "name", spec
    )
    tool["stackProgress"]["resources"][0]["attributes"]["RoleName"] = "ReplacementRole"
    assert VisibleResourceSource(source, tool_source_value(tool)).digest != expected


def test_named_fields_do_not_turn_request_identifiers_into_resources():
    spec = resource_spec("ALIYUN::ROS::Stack")
    assert spec is not None
    identifier = "11111111-2222-4333-8444-555555555555"
    for field in ("RequestId", "TaskId", "OperationId"):
        source = VisibleResourceSource({}, json.dumps({field: identifier}))
        assert not source_contains_locator(source, identifier, "id", spec)
        assert not source_contains_locator(
            VisibleResourceSource({}, f"```json\n{source.value}\n```"), identifier, "id", spec
        )
    assert source_contains_locator(
        VisibleResourceSource({}, json.dumps({"StackId": identifier})), identifier, "id", spec
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Instance i-design0000000001.", True),
        ("Instance i-design0000000001. Next sentence.", True),
        ("Instance (i-design0000000001.)", True),
        ("i-design0000000001...", False),
        ("i-design0000000001.suffix.", False),
        ("i-design0000000001/child.", False),
        ('"i-design0000000001."', False),
        ('{"InstanceId":"i-design0000000001."}', False),
        ("InstanceId: i-design0000000001.", False),
        ("https://example.test/i-design0000000001.", False),
    ],
)
def test_id_prose_period_proof_matches_frontend_without_shortening_literals(value, expected):
    spec = resource_spec("ALIYUN::ECS::Instance")
    assert spec is not None
    for require_type in (True, False):
        assert source_contains_locator(
            VisibleResourceSource({}, value), "i-design0000000001", "id", spec, require_type=require_type
        ) is expected


def test_resolve_endpoint_rechecks_source_after_async_persistence(tmp_path, monkeypatch):
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="choice-session")
    controller = session.resource_references
    assert controller is not None
    visible = transcript()
    request = choice(controller, visible)
    reads = 0

    def history(*args, **kwargs):
        nonlocal reads
        reads += 1
        result = copy.deepcopy(visible)
        if reads > 1:
            result["messages"][0]["text"] = "replaced during write"
        return result

    monkeypatch.setattr(manager, "load_visible_transcript", history)
    with TestClient(create_app(session_manager=manager)) as client:
        response = client.post(f"/api/sessions/{session.web_session_id}/resource-references/resolve", json=request)
        assert response.status_code == 409
        assert not controller.store.bindings
        assert client.post("/api/sessions/missing/resource-references/resolve", json=request).status_code == 404


@pytest.mark.parametrize("write_succeeds", [True, False])
def test_resolve_endpoint_roundtrip_and_write_failure(tmp_path, monkeypatch, write_succeeds):
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="choice-session")
    controller = session.resource_references
    assert controller is not None
    visible = transcript()
    request = choice(controller, visible)
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: copy.deepcopy(visible))
    if not write_succeeds:

        def failed_write(*args):
            raise OSError("fake local disk failure")

        monkeypatch.setattr(controller.store, "_write_snapshot", failed_write)
    with TestClient(create_app(session_manager=manager)) as client:
        response = client.post(f"/api/sessions/{session.web_session_id}/resource-references/resolve", json=request)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == ("saved" if write_succeeds else "not_saved")
        assert result["snapshot"]["bindings"][0]["sourceKey"] == result["sourceKey"]
        assert result["persistenceStatus"] == ("saved" if write_succeeds else "write_failed")
        snapshot = client.get(f"/api/sessions/{session.web_session_id}/resource-references").json()
        assert snapshot["bindings"] == result["snapshot"]["bindings"]
        assert visible["messages"][0]["text"] not in json.dumps(result, ensure_ascii=False)
