"""Keep the browser fixture honest about disk-backed normal/pipeline recovery."""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.web.resource_sources import with_request_sources
from iac_code.web.session_manager import WebSessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["selling", "selling_solution_first"])
async def test_snapshot_question_fixture_uses_real_local_recovery(tmp_path, monkeypatch, mode):
    from iac_code.web.pipeline_actions import load_pipeline_snapshot

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/web/e2e"))
    module = import_module("resource_application_fixture")
    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    fixture = module.ResourceApplicationFixture(manager)
    session = fixture.sessions[mode]
    for action in ("start", "change", "finish"):
        fixture.snapshot_question(mode, "snapshot-question-" + action)
        snapshot = await load_pipeline_snapshot(context_id=session.context_id, task_id=session.task_id)
        assert snapshot is not None and not session.pending_questions
        sources = with_request_sources({}, session, snapshot)["resourceRequestSources"]
        if action == "finish":
            assert not sources
        else:
            text = sources["blocking:question:snapshot-question"]["/prompt"]
            assert "RAM role SnapshotOnlyRole" in text
            assert text.startswith("Revised") == (action == "change")
    for item in fixture.sessions.values():
        await item.resource_references.close()


@pytest.mark.asyncio
async def test_auxiliary_fixture_uses_real_pending_request_schema_and_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/web/e2e"))
    fixture_module = import_module("resource_application_fixture")
    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    fixture = fixture_module.ResourceApplicationFixture(manager)
    session = fixture.sessions["normal"]

    async def action(name):
        async def json_body():
            return {"mode": "normal", "action": name}

        assert (await fixture.handle(SimpleNamespace(method="POST", json=json_body))).status_code == 200

    await action("auxiliary-start")
    pending = next(iter(session.pending_elicitations.values()))
    assert pending.payload["mode"] == "form" and len(pending.payload["fields"]) == 2
    projection = with_request_sources(manager.load_visible_transcript(session.session_id), session)
    sources = projection["resourceRequestSources"]
    assert len(sources) == 4
    assert f"blocking:elicitation:{pending.request_id}" in sources
    assert sources[f"blocking:elicitation:{pending.request_id}"]["/fields/0/description"] == {
        "fieldName": "first",
        "text": "Use RAM role AuxiliaryRole-normal; i-application000001.",
    }
    first = next(iter(session.pending_questions.values()))
    await action("auxiliary-change")
    assert first.payload["question"] == "Auxiliary first: revised RAM role AuxiliaryRole-normal"
    assert session.active_turn_task is None
    await action("auxiliary-finish")
    assert not with_request_sources({}, session)["resourceRequestSources"]
    assert pending.future.done() and first.future.done()
    for item in fixture.sessions.values():
        await item.resource_references.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "selling", "selling_solution_first"])
async def test_application_fixture_restores_real_storage_and_sse_after_manager_restart(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/web/e2e"))
    fixture_module = import_module("resource_application_fixture")
    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    fixture = fixture_module.ResourceApplicationFixture(manager)
    session = fixture.sessions[mode]
    transcript = manager.load_visible_transcript(session.session_id)
    assert any(fixture_module.BODY == message["text"] for message in transcript["messages"])
    assert any(fixture_module.THINKING == message["thinking"] for message in transcript["messages"])
    assert transcript["tools"]["application-tool"]["results"][0]["content"] == fixture_module.TOOL_OUTPUT
    assert session.resource_references.hydrate(transcript)["items"] == []

    async def action(name):
        async def json_body():
            return {"mode": mode, "action": name}

        response = await fixture.handle(SimpleNamespace(method="POST", json=json_body))
        assert response.status_code == 200

    await action("facts")
    await action("stream-start")
    await action("stream-name")
    await action("stream-end")
    transcript = manager.load_visible_transcript(session.session_id)
    live = session.resource_references.hydrate(transcript)
    assert {item["identifier"] for item in live["items"]} == {
        fixture_module.ROLE,
        fixture_module.INSTANCE,
        fixture_module.STREAM_ID,
    }
    role = next(item for item in live["items"] if item["identifier"] == fixture_module.ROLE)
    assert role["identifierKind"] == "name" and "regionId" not in role
    assert "assistant.text.delta" in fixture.events_sent[mode]
    assert "tool.result" in fixture.events_sent[mode]
    for name in ("deploy-start", "deploy-identifiers", "deploy-finish"):
        await action(name)
    transcript = manager.load_visible_transcript(session.session_id)
    live = session.resource_references.hydrate(transcript)
    assert len(live["items"]) == 6
    assert not any(item["identifier"] == "PreviewOnlyRole" for item in live["items"])
    preview_path = Path(session.cwd) / ("resource-links-" + mode + ".json")
    assert preview_path.read_text(encoding="utf-8") == fixture_module.TEMPLATE
    await action("preview-change")
    assert preview_path.read_text(encoding="utf-8") == fixture_module.TEMPLATE + "\n"
    changed = session.resource_references.hydrate(manager.load_visible_transcript(session.session_id))
    assert changed["items"] == live["items"]
    stack = next(item for item in live["items"] if item["identifier"] == fixture_module.STACK_ID)
    assert stack["regionId"] == "cn-beijing"
    assert "pipeline.event" in fixture.events_sent[mode]
    result = transcript["tools"]["application-deployment"]["results"][0]["content"]
    assert fixture_module.DEPLOY_ROLE in result and fixture_module.DEPLOY_ID in result
    if mode != "normal":
        progress = transcript["tools"]["application-deployment"]["stackProgress"]
        assert progress["accountScope"] == "fake-application-account"
        assert progress["resources"][1]["physical_resource_id"] == fixture_module.DEPLOY_ID
    await action("recovery-facts")
    transcript = manager.load_visible_transcript(session.session_id)
    live = session.resource_references.hydrate(transcript)
    names = fixture_module.recovery_names(mode)
    assert {item["identifier"] for item in live["items"]} == {
        fixture_module.ROLE,
        fixture_module.INSTANCE,
        fixture_module.STREAM_ID,
        fixture_module.STACK_ID,
        fixture_module.DEPLOY_ROLE,
        fixture_module.DEPLOY_ID,
        names["refresh"],
        names["restart"],
        "fake-approval-stack",
    }
    assert len(live["items"]) == 9
    for kind in ("refresh", "restart"):
        fact = next(item for item in live["items"] if item["identifier"] == names[kind])
        assert fact["resourceType"] == "ALIYUN::FC::Function"
        assert fact["accountScope"] == "fake-application-account"
        assert fact["attributes"] == {"FunctionName": names[kind]}
        assert fact["sources"][0]["logicalResourceId"] == names[kind]
        assert "ServiceName" not in transcript["tools"]["application-query-" + kind]["results"][0]["content"]
    assert not any(item["identifier"] == names["role"] for item in live["items"]), "mere text is not an API fact"
    assert any(message["text"] == f"Application choice: RAM role {names['role']}" for message in transcript["messages"])
    assert session.pending_permissions == {} and session.active_turn_task is None and not session.turn_lock.locked()
    if mode != "normal":
        await action("handoff")
    await session.flush_resource_references()
    await session.resource_references.close()

    # A fresh manager has neither an injected transcript loader nor the original
    # event buffer. All facts must come back from JSONL/journal and the sidecar.
    restored_manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    restored_session = restored_manager.get_session(session.web_session_id)
    assert restored_session is not None
    restored = restored_manager.load_visible_transcript(session.session_id)
    snapshot = restored_session.resource_references.hydrate(restored)
    assert snapshot["items"] == live["items"]
    assert restored["tools"]["application-deployment"]["results"][0]["content"] == result
    assert any(fixture_module.BODY == message["text"] for message in restored["messages"])
    assert any(fixture_module.STREAM_ID in message["text"] for message in restored["messages"])
    if mode != "normal":
        assert restored_session.mode == "normal"
        assert any(message.get("kind") == "pipeline_step" for message in restored["messages"])
    await restored_session.resource_references.close()
