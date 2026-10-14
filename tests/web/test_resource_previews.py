"""Preview choices stay dormant until explicitly displayed content is validated."""

from __future__ import annotations

import copy
import json

import pytest
from starlette.testclient import TestClient

from iac_code.tools.cloud.resource_references import stack_resource_reference
from iac_code.web.resource_previews import PreviewSources, preview_source
from iac_code.web.resource_reference_controller import ResourceReferenceController
from iac_code.web.resource_references import resource_fact_to_web
from iac_code.web.resource_sources import read_visible_source


@pytest.mark.asyncio
async def test_empty_preview_revokes_old_choice_without_breaking_preview(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    source = preview_source(str(tmp_path), "empty.yaml")
    await controller.observe_preview(source, "RAM role PreviewRole", visible())
    await controller.resolve_choice(visible(), choice_payload(controller, source))
    snapshot = await controller.observe_preview(source, "", visible())
    assert not snapshot["bindings"] and not controller.store.preview_choices
    await controller.close()


@pytest.mark.asyncio
async def test_temporarily_unwritable_preview_keeps_valid_dormant_choice(tmp_path):
    path = tmp_path / "references.json"
    source = preview_source(str(tmp_path), "template.yaml")
    original = ResourceReferenceController(path, lambda *args: None)
    await original.observe_preview(source, "RAM role PreviewRole", visible())
    await original.resolve_choice(visible(), choice_payload(original, source))
    await original.close()
    restored = ResourceReferenceController(path, lambda *args: None, writable=lambda: False)
    restored.hydrate({})
    snapshot = await restored.observe_preview(source, "RAM role PreviewRole", {})
    assert not snapshot["bindings"] and len(restored.store.preview_choices) == 1
    restored.writable = lambda: True
    assert (await restored.observe_preview(source, "RAM role PreviewRole", {}))["bindings"]
    await restored.close()


def visible():
    fact = {
        "resourceType": "ALIYUN::RAM::Role",
        "identifier": "SeedRole",
        "identifierKind": "name",
        "identifierField": "RoleName",
        "accountScope": "fake-preview-account",
        "consolePartition": "aliyun",
        "evidence": "api_response",
        "sources": [{"toolUseId": "seed", "sourcePath": "/Role/RoleName"}],
    }
    return {
        "messages": [],
        "tools": {
            "seed": {
                "toolName": "aliyun_api",
                "input": {"product": "ram", "action": "GetRole", "params": {"RoleName": "SeedRole"}},
                "results": [{"content": '{"Role":{"RoleName":"SeedRole"}}', "resourceReferences": [fact]}],
            }
        },
    }


def choice_payload(controller, source, transcript=None):
    snapshot = controller.hydrate(visible() if transcript is None else transcript)
    content = read_visible_source(controller.preview_sources.project({}), source)
    return {
        "expectedRevision": snapshot["revision"],
        "source": resource_fact_to_web(source),
        "sourceDigest": content.digest,
        "identifier": "PreviewRole",
        "identifierKind": "name",
        "group": [
            "name",
            "PreviewRole",
            "RoleName",
            "ALIYUN::RAM::Role",
            None,
            None,
            None,
            [],
            {"kind": "explicit_type"},
            None,
        ],
        "context": {"accountScope": "fake-preview-account", "consolePartition": "aliyun", "attributes": {}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["fake_contact_group", "fake Contact Group"])
async def test_cms_ros_preview_choice_survives_controller_reload(tmp_path, name):
    row = {"ResourceType": "ALIYUN::CMS::ContactGroup", "PhysicalResourceId": name}
    fact = stack_resource_reference(
        row, stack_id="fake-stack", region_id="cn-hangzhou", tool_use_id="fake-tool",
        source_path="/Resources/0", account_scope="fake-account",
    )
    history = {"messages": [], "tools": {"fake-tool": {
        "toolName": "aliyun_api",
        "input": {"product": "ros", "action": "ListStackResources", "params": {"StackId": "fake-stack"}},
        "results": [{"content": json.dumps({"Resources": [row]}), "resourceReferences": [resource_fact_to_web(fact)]}],
    }}}
    path = tmp_path / "references.json"
    source = preview_source(str(tmp_path), "fake-template.json")
    content = json.dumps(row)
    controller = ResourceReferenceController(path, lambda *_: None)
    snapshot = await controller.observe_preview(source, content, history)
    reference = next(item for item in snapshot["items"] if item["resourceType"] == "ALIYUN::CMS::ContactGroup")
    result = await controller.resolve_choice(history, {
        "expectedRevision": snapshot["revision"], "source": resource_fact_to_web(source),
        "sourceDigest": read_visible_source(controller.preview_sources.project(history), source).digest,
        "group": ["id", name, "PhysicalResourceId", "ALIYUN::CMS::ContactGroup", None, None, None, [],
                  {"kind": "field", "field": "PhysicalResourceId", "typed": True}, None],
        "identifier": name, "identifierKind": "id", "referenceId": reference["referenceId"],
        "context": {"accountScope": "fake-account", "consolePartition": "aliyun", "attributes": {}},
    })
    assert result["status"] == "saved"
    assert len(controller.store.preview_choices) == 1
    await controller.close()

    restored = ResourceReferenceController(path, lambda *_: None)
    assert not restored.hydrate(history)["bindings"]  # A bookmark stays dormant until the file is served.
    active = await restored.observe_preview(source, content, history)
    assert active["bindings"][0]["referenceId"] == result["referenceId"]
    assert active["bindings"][0]["context"]["consolePartition"] == "aliyun"
    changed = await restored.observe_preview(source, content + " ", history)
    assert not changed["bindings"] and not restored.store.preview_choices
    await restored.close()


@pytest.mark.asyncio
async def test_dormant_choice_restores_only_after_same_file_content_is_served(tmp_path):
    source = preview_source(str(tmp_path), "template.yaml")
    path = tmp_path / "references.json"
    controller = ResourceReferenceController(path, lambda *args: None)
    await controller.observe_preview(source, "RAM role PreviewRole", visible())
    result = await controller.resolve_choice(visible(), choice_payload(controller, source))
    assert result["status"] == "saved"
    reference_id = result["referenceId"]
    assert len(controller.store.preview_choices) == 1
    assert "RAM role PreviewRole" not in path.read_text(encoding="utf-8")
    assert "template.yaml" not in path.read_text(encoding="utf-8")
    await controller.close()

    restored = ResourceReferenceController(path, lambda *args: None)
    dormant = restored.hydrate({})
    assert not dormant["items"] and not dormant["bindings"]
    assert "previewChoices" not in dormant and len(restored.store.preview_choices) == 1
    other = preview_source(str(tmp_path), "other.yaml")
    assert not (await restored.observe_preview(other, "RAM role PreviewRole", {}))["items"]
    active = await restored.observe_preview(source, "RAM role PreviewRole", {})
    assert [item["referenceId"] for item in active["items"]] == [reference_id]
    assert active["bindings"][0]["referenceId"] == reference_id
    assert active["bindings"][0]["source"] == resource_fact_to_web(source)
    changed = await restored.observe_preview(source, "Revised RAM role PreviewRole", {})
    assert not changed["items"] and not changed["bindings"] and not restored.store.preview_choices
    # Returning to old content after a confirmed change does not revive it.
    assert not (await restored.observe_preview(source, "RAM role PreviewRole", {}))["bindings"]
    await restored.close()


@pytest.mark.asyncio
async def test_evicted_preview_choice_is_dormant_not_lost_and_failure_revokes_it(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    controller.preview_sources = PreviewSources(max_sources=1)
    source = preview_source(str(tmp_path), "one.yaml")
    await controller.observe_preview(source, "RAM role PreviewRole", visible())
    result = await controller.resolve_choice(visible(), choice_payload(controller, source))
    other = preview_source(str(tmp_path), "two.yaml")
    snapshot = await controller.observe_preview(other, "No resource here", visible())
    assert not snapshot["bindings"] and len(controller.store.preview_choices) == 1
    restored = await controller.observe_preview(source, "RAM role PreviewRole", visible())
    assert restored["bindings"][0]["referenceId"] == result["referenceId"]
    controller.forget_preview(source, visible())
    assert not controller.store.bindings and not controller.store.preview_choices
    assert {item["identifier"] for item in controller.snapshot()["items"]} == {"SeedRole"}
    await controller.close()


def test_preview_cache_is_bounded_and_ignores_forged_sources(tmp_path):
    cache = PreviewSources(max_sources=2, max_characters=10)
    source = preview_source(str(tmp_path), "one.yaml")
    assert source == preview_source(str(tmp_path), str(tmp_path / "one.yaml"))
    cache.observe(source, "01234567890")
    assert read_visible_source(cache.project({}), source).value == "01234567890"
    other = preview_source(str(tmp_path), "two.yaml")
    cache.observe(other, "123")
    projection = cache.project({"resourcePreviewSources": {source["source_id"]: {"/content": "hidden"}}})
    with pytest.raises(ValueError, match="resource_source_unavailable"):
        read_visible_source(projection, source)
    assert read_visible_source(projection, other).value == "123"
    cache.clear()
    assert not cache.project({})["resourcePreviewSources"]


@pytest.mark.asyncio
async def test_changed_source_during_save_cannot_restore_a_stale_bookmark(tmp_path, monkeypatch):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    source = preview_source(str(tmp_path), "one.yaml")
    await controller.observe_preview(source, "RAM role PreviewRole", visible())
    payload = choice_payload(controller, source)
    original = controller.flush

    async def changed():
        controller.preview_sources.observe(source, "RAM role DifferentRole")
        controller.hydrate(visible())
        return await original()

    monkeypatch.setattr(controller, "flush", changed)
    with pytest.raises(ValueError, match="resource_choice_changed"):
        await controller.resolve_choice(visible(), payload)
    monkeypatch.setattr(controller, "flush", original)
    changed_snapshot = await controller.observe_preview(source, "RAM role DifferentRole", visible())
    assert not changed_snapshot["bindings"] and not controller.store.preview_choices
    await controller.close()


def test_http_only_explicit_file_preview_reads_content_and_internal_choices_are_not_exposed(tmp_path, monkeypatch):
    from iac_code.web import outputs
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir()
    target = project / "template.yaml"
    target.write_text("Description: RAM role PreviewRole\nResources: {}\n", encoding="utf-8")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(session_id="preview")
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: copy.deepcopy(visible()))
    # Keep existing output discovery out of the read counter: link hydration
    # must not introduce its own filesystem enumeration or file reads.
    monkeypatch.setattr(outputs, "outputs_payload", lambda *args, **kwargs: {"files": []})
    original_read = outputs.read_output_file
    reads = []

    def read(*args, **kwargs):
        reads.append(args[1])
        return original_read(*args, **kwargs)

    monkeypatch.setattr(outputs, "read_output_file", read)
    source = preview_source(str(project), "template.yaml")
    with TestClient(create_app(session_manager=manager)) as client:
        prefix = f"/api/sessions/{session.web_session_id}"
        client.get(prefix + "/messages")
        assert not reads
        response = client.get(prefix + "/outputs/file", params={"path": "template.yaml"})
        assert response.status_code == 200 and len(reads) == 1
        assert response.json()["resourceSource"] == resource_fact_to_web(source)
        payload = choice_payload(session.resource_references, source)
        response = client.post(prefix + "/resource-references/resolve", json=payload)
        assert response.status_code == 200 and response.json()["status"] == "saved", response.text
        messages = client.get(prefix + "/messages").json()
        assert messages["resourceReferences"]["bindings"] and len(reads) == 1
        assert "resourcePreviewSources" not in messages
        assert "preview_choices" not in json.dumps(messages)
        target.write_text("Description: revised RAM role PreviewRole\nResources: {}\n", encoding="utf-8")
        changed = client.get(prefix + "/outputs/file", params={"path": "template.yaml"})
        assert not changed.json()["resourceReferences"]["bindings"] and len(reads) == 2
        assert not session.resource_references.store.preview_choices
        target.write_text("", encoding="utf-8")
        empty = client.get(prefix + "/outputs/file", params={"path": "template.yaml"})
        assert empty.status_code == 200 and empty.json()["content"] == ""
        assert not empty.json()["resourceReferences"]["bindings"] and len(reads) == 3
        target.unlink()
        assert client.get(prefix + "/outputs/file", params={"path": "template.yaml"}).status_code == 404
        assert not session.resource_references.preview_sources.project({})["resourcePreviewSources"]
