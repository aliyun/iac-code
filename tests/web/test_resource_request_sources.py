"""Auxiliary text is a versioned appearance source, never cloud authority."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from iac_code.tools.cloud.resource_references import resource_spec
from iac_code.web.events import WebEventBuffer
from iac_code.web.resource_reference_controller import ResourceReferenceController
from iac_code.web.resource_references import ResourceRevisionConflictError, resource_fact_to_web
from iac_code.web.resource_sources import (
    clean_source,
    read_visible_source,
    request_source_texts,
    source_contains_locator,
    with_error_source,
    with_request_sources,
)


def pending_session():
    requests = {
        key: SimpleNamespace(request_id=key, session_id="session", payload={"question": "RAM role DeployRole"})
        for key in ("first", "second")
    }
    return SimpleNamespace(session_id="session", pending_questions=requests)


def visible_transcript():
    fact = {
        "resourceType": "ALIYUN::RAM::Role",
        "identifier": "DeployRole",
        "identifierKind": "name",
        "identifierField": "RoleName",
        "accountScope": "fake-account",
        "consolePartition": "aliyun",
        "evidence": "api_response",
        "sources": [{"toolUseId": "role-tool", "sourcePath": "/Role/RoleName"}],
    }
    return {
        "messages": [],
        "tools": {
            "role-tool": {
                "toolName": "aliyun_api",
                "input": {"product": "ram", "action": "GetRole", "params": {"RoleName": "DeployRole"}},
                "results": [{"content": '{"Role":{"RoleName":"DeployRole"}}', "resourceReferences": [fact]}],
            }
        },
    }


def request_choice(controller, visible, source):
    snapshot = controller.hydrate(visible)
    reference = snapshot["items"][0]
    return {
        "expectedRevision": snapshot["revision"],
        "source": resource_fact_to_web(source),
        "sourceDigest": read_visible_source(visible, source).digest,
        "group": [
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
        ],
        "identifier": "DeployRole",
        "identifierKind": "name",
        "referenceId": reference["referenceId"],
        "context": {"accountScope": "fake-account", "consolePartition": "aliyun", "attributes": {}},
    }


def test_pipeline_request_projection_matches_reload_fallback_and_live_precedence():
    session = pending_session()
    session.mode = "pipeline"
    snapshot = {
        "pendingInput": {"kind": "ask_user_question", "inputId": "recovered", "question": "RAM role DeployRole"}
    }
    source = {"source_id": "blocking:question:recovered", "source_path": "/prompt"}
    visible = with_request_sources({}, session, snapshot)
    assert read_visible_source(visible, source).value == "RAM role DeployRole"
    snapshot["pendingInput"]["inputId"] = "first"
    snapshot["pendingInput"]["question"] = "Hidden stale prompt"
    assert (
        read_visible_source(
            with_request_sources({}, session, snapshot), {**source, "source_id": "blocking:question:first"}
        ).value
        == "RAM role DeployRole"
    )
    fallback = {
        "control": {
            "waitingInput": {"kind": "ask_user_question", "toolUseId": "recovered", "prompt": "RAM role DeployRole"}
        }
    }
    assert (
        read_visible_source(
            with_request_sources({}, session, fallback), {**source, "source_id": "blocking:question:ask-recovered"}
        ).value
        == "RAM role DeployRole"
    )
    for invalid in (None, {}, {"pendingInput": {"kind": "confirm", "inputId": "recovered"}}):
        assert source["source_id"] not in with_request_sources({}, session, invalid)["resourceRequestSources"]
    session.mode = "normal"
    assert (
        "blocking:question:ask-recovered" not in with_request_sources({}, session, fallback)["resourceRequestSources"]
    )


def test_http_pipeline_request_choice_revalidates_current_snapshot(tmp_path, monkeypatch):
    from iac_code.web import pipeline_actions
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="pipeline-choice")
    session.mode, session.context_id, session.task_id = "pipeline", "fake-context", "fake-task"
    transcript = visible_transcript()
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: copy.deepcopy(transcript))
    snapshot = {
        "pendingInput": {"kind": "ask_user_question", "inputId": "recovered", "question": "RAM role DeployRole"}
    }
    loads = []

    async def load(**kwargs):
        loads.append(kwargs)
        return copy.deepcopy(snapshot)

    monkeypatch.setattr(pipeline_actions, "load_pipeline_snapshot", load)
    source = {"source_id": "blocking:question:recovered", "source_path": "/prompt"}
    controller = session.resource_references
    payload = request_choice(controller, with_request_sources(transcript, session, snapshot), source)
    with TestClient(create_app(session_manager=manager)) as client:
        prefix = f"/api/sessions/{session.web_session_id}"
        result = client.post(prefix + "/resource-references/resolve", json=payload)
        assert result.status_code == 200 and result.json()["status"] == "saved", result.text
        response = client.get(prefix + "/messages").json()
        assert response["resourceReferences"]["bindings"] and "resourceRequestSources" not in response
        snapshot["pendingInput"]["question"] = "Revised RAM role DeployRole"
        assert not client.get(prefix + "/resource-references").json()["bindings"]
        payload = request_choice(controller, with_request_sources(transcript, session, snapshot), source)
        original_flush = controller.flush

        async def changed_during_save():
            snapshot.clear()
            return await original_flush()

        monkeypatch.setattr(controller, "flush", changed_during_save)
        assert client.post(prefix + "/resource-references/resolve", json=payload).status_code == 409
        assert not client.get(prefix + "/resource-references").json()["bindings"]
        assert loads and all(value == {"context_id": "fake-context", "task_id": "fake-task"} for value in loads)


def test_only_rendered_request_text_enters_the_projection():
    assert request_source_texts(
        "permission",
        {
            "message": "RAM role DeployRole",
            "description": "hidden fallback",
            "toolInput": {"RoleName": "HiddenRole"},
            "command": "echo i-design000001",
            "choices": [{"label": "RAM role ChoiceRole"}],
        },
    ) == {"/prompt": "RAM role DeployRole", "/command": "echo i-design000001"}
    fields = [
        {
            "name": "role",
            "label": "RAM role DeployRole",
            "required": True,
            "description": "Use RAM role OtherRole",
            "default": "NeverRead",
            "enum": ["HiddenRole"],
        }
    ]
    assert request_source_texts("elicitation", {"mode": "form", "fields": fields}) == {
        "/fields/0/label": {"fieldName": "role", "text": "RAM role DeployRole *"},
        "/fields/0/description": {"fieldName": "role", "text": "Use RAM role OtherRole"},
    }
    assert request_source_texts("elicitation", {"mode": "url", "url": "https://example.test", "fields": fields}) == {}
    session = pending_session()
    session.pending_questions["second"].session_id = "other-session"
    original = {"messages": [], "tools": {}, "resourceRequestSources": {"forged": {"/prompt": "hidden"}}}
    result = with_request_sources(original, session)
    assert list(result["resourceRequestSources"]) == ["blocking:question:first"]
    assert "forged" in original["resourceRequestSources"], "projection must not mutate a caller's transcript"


def test_parameter_identity_versions_identical_text_but_cannot_supply_locator_evidence():
    session = pending_session()
    request = SimpleNamespace(
        request_id="form",
        session_id="session",
        payload={
            "mode": "form",
            "fields": [
                {"name": "first", "label": "A", "description": "Same text"},
                {"name": "second", "label": "A", "description": "Same text"},
            ],
        },
    )
    session.pending_elicitations = {"form": request}
    descriptor = {"source_id": "blocking:elicitation:form", "source_path": "/fields/0/description"}
    original = read_visible_source(with_request_sources({}, session), descriptor)
    request.payload["fields"].reverse()
    moved = read_visible_source(with_request_sources({}, session), descriptor)
    assert original.value == moved.value and original.digest != moved.digest
    request.payload["fields"][0]["name"] = "RAM role DeployRole"
    hidden = read_visible_source(with_request_sources({}, session), descriptor)
    assert not source_contains_locator(hidden, "DeployRole", "name", resource_spec("ALIYUN::RAM::Role"))


@pytest.mark.parametrize(
    "source",
    [
        {"source_id": "session-error", "source_path": "/prompt"},
        {"source_id": "blocking:question:first", "source_path": "/command"},
        {"source_id": "blocking:permission:first", "source_path": "/fields/0/label"},
        {"source_id": "blocking:elicitation:first", "source_path": "/fields/00/description"},
        {"source_id": "blocking:elicitation:first", "source_path": "/fields/0/default"},
        {"source_id": "blocking:question:first", "source_path": "/options/0/label"},
        {"source_id": "blocking:question:first", "source_path": "/prompt", "tool_use_id": "role-tool"},
    ],
)
def test_request_sources_reject_non_display_paths_and_multiple_owners(source):
    with pytest.raises(ValueError, match="resource_source_invalid"):
        clean_source(source)


@pytest.mark.asyncio
async def test_request_choice_restores_only_while_the_exact_request_field_survives(tmp_path):
    session = pending_session()
    source = {"source_id": "blocking:question:first", "source_path": "/prompt"}
    visible = with_request_sources(visible_transcript(), session)
    path = tmp_path / "resources.json"
    controller = ResourceReferenceController(path, lambda *args: None)
    payload = request_choice(controller, visible, source)
    result = await controller.resolve_choice(visible, payload)
    assert result["status"] == "saved"
    assert len(result["snapshot"]["bindings"]) == 1
    assert "RAM role DeployRole" not in path.read_text(encoding="utf-8")
    await controller.close()
    restored = ResourceReferenceController(path, lambda *args: None)
    assert restored.hydrate(visible)["bindings"] == result["snapshot"]["bindings"]
    session.pending_questions["second"].payload["question"] = "Unrelated request changed"
    assert restored.hydrate(with_request_sources(visible_transcript(), session))["bindings"]
    session.pending_questions["first"].payload["question"] = "RAM role ReplacementRole"
    snapshot = restored.hydrate(with_request_sources(visible_transcript(), session))
    assert not snapshot["bindings"] and snapshot["items"]
    with pytest.raises(ResourceRevisionConflictError, match="resource_source_changed"):
        await restored.resolve_choice(
            with_request_sources(visible_transcript(), session), {**payload, "expectedRevision": snapshot["revision"]}
        )
    session.pending_questions.clear()
    with pytest.raises(ValueError, match="resource_source_unavailable"):
        read_visible_source(with_request_sources(visible_transcript(), session), source)
    await restored.close()


def test_javascript_request_digest_and_index_owner_match_python(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    modules = (Path(__file__).parents[2] / "src/iac_code/web/static/js").as_uri()
    source = {"source_id": "blocking:question:first", "source_path": "/prompt"}
    visible = with_request_sources(visible_transcript(), pending_session())
    expected = read_visible_source(visible, source).digest
    script = (
        f"const base={json.dumps(modules)};\n"
        + """
import assert from 'node:assert/strict';
const {digestResourceSource}=await import(`${base}/resource_sources.js`);
const {ResourceIndex}=await import(`${base}/resource_index.js`);
const source={sourceId:'blocking:question:first',sourcePath:'/prompt'};
const index=new ResourceIndex();
await index.loadSnapshot({schemaVersion:1,revision:1,items:[{
  referenceId:'ref-role',provider:'aliyun',resourceType:'ALIYUN::RAM::Role',identifier:'DeployRole',
  identifierKind:'name',identifierField:'RoleName',scopeKind:'account',attributes:{},sources:[],aliases:[],
}],bindings:[{source,sourceKey:'test',identifier:'DeployRole',identifierKind:'name',referenceId:'ref-role'}]},
()=>assert.fail('unexpected query'));
assert.equal(index.hasSourceBindings(source),true);
assert.equal(index.hasSourceBindings({...source,sourceId:'blocking:question:second'}),false);
assert.equal(index.hasSourceBindings({...source,sourcePath:'/command'}),false);
console.log(await digestResourceSource(source,'RAM role DeployRole'));
"""
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_http_resolve_uses_current_pending_requests_without_exposing_the_internal_projection(tmp_path, monkeypatch):
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="request-choice")
    pending = pending_session().pending_questions["first"]
    pending.session_id = session.session_id
    session.pending_questions["first"] = pending
    transcript = visible_transcript()
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: copy.deepcopy(transcript))
    source = {"source_id": "blocking:question:first", "source_path": "/prompt"}
    controller = session.resource_references
    payload = request_choice(controller, with_request_sources(transcript, session), source)
    with TestClient(create_app(session_manager=manager)) as client:
        prefix = f"/api/sessions/{session.web_session_id}"
        response = client.post(prefix + "/resource-references/resolve", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "saved"
        messages = client.get(prefix + "/messages").json()
        assert "resourceRequestSources" not in messages
        assert messages["resourceReferences"]["bindings"]
        session.pending_questions.clear()
        assert not client.get(prefix + "/resource-references").json()["bindings"]
        payload["expectedRevision"] = controller.store.revision
        assert client.post(prefix + "/resource-references/resolve", json=payload).status_code == 400


def error_session():
    return SimpleNamespace(session_id="session", events=WebEventBuffer("session", max_events=4))


def error_descriptor(event):
    return {"source_id": f"session-error:{event['sequence']}:{event['createdAt']}", "source_path": "/message"}


def test_error_projection_matches_reducer_lifecycle_without_scanning_hidden_details():
    session = error_session()
    event = session.events.append("error", {"message": "RAM role DeployRole", "turnId": "first"})
    source = error_descriptor(event)
    projection = with_error_source({}, session)
    assert read_visible_source(projection, source).value == "RAM role DeployRole"
    session.events.append("user.message", {"turnId": "first", "text": "same turn"})
    assert with_error_source({}, session) == projection
    session.events.append("user.message", {"turnId": "second", "text": "new turn"})
    assert not with_error_source({}, session)["resourceErrorSources"]
    forged = {"resourceErrorSources": projection["resourceErrorSources"]}
    assert not with_error_source(forged, session)["resourceErrorSources"]
    assert forged == projection, "projection must not mutate the original transcript"
    event = session.events.append("error", {"message": "Try again", "details": "RAM role HiddenRole"})
    value = read_visible_source(with_error_source({}, session), error_descriptor(event))
    assert not source_contains_locator(value, "HiddenRole", "name", resource_spec("ALIYUN::RAM::Role"))
    for _ in range(4):
        session.events.append("status", {})
    assert not with_error_source({}, session)["resourceErrorSources"], "evicted error is not authoritative"
    event = session.events.append("error", {"message": "RAM role DeployRole"})
    event["sessionId"] = "foreign-session"
    assert not with_error_source({}, session)["resourceErrorSources"]


@pytest.mark.parametrize("message", [None, {}, [], 7, ""])
def test_missing_error_message_cannot_retain_a_previous_error_source(message):
    session = error_session()
    session.events.append("error", {"message": "RAM role DeployRole"})
    session.events.append("error", {"message": message, "details": "RAM role HiddenRole"})
    assert not with_error_source({}, session)["resourceErrorSources"]


def test_error_event_identity_and_source_digest_match_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    modules = (Path(__file__).parents[2] / "src/iac_code/web/static/js").as_uri()
    session = error_session()
    event = session.events.append("error", {"message": "RAM role DeployRole", "turnId": "failed-turn"})
    expected = read_visible_source(with_error_source({}, session), error_descriptor(event)).digest
    script = (
        f"const base={json.dumps(modules)}, event={json.dumps(event)};\n"
        + """
import assert from 'node:assert/strict';
const {reduceEvent}=await import(`${base}/events.js`);
const {digestResourceSource}=await import(`${base}/resource_sources.js`);
let state=reduceEvent({},event);
const source=state.lastError.resourceSource;
assert.deepEqual(source,{sourceId:`session-error:${event.sequence}:${event.createdAt}`,sourcePath:'/message'});
console.log(await digestResourceSource(source,state.lastError.message));
const repeated=reduceEvent(state,{...event,sequence:event.sequence+1});
assert.notDeepEqual(repeated.lastError.resourceSource,source);
state=reduceEvent(state,{type:'user.message',sequence:3,payload:{turnId:'next-turn',text:'Continue'}});
assert.equal(state.lastError,null);
assert.equal(await digestResourceSource({sourceId:'session-error'},event.payload.message),null);
"""
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_http_error_choices_restore_and_same_text_new_event_revokes_them(tmp_path, monkeypatch):
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="error-choice")
    transcript = visible_transcript()
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: copy.deepcopy(transcript))
    event = session.events.append("error", {"message": "RAM role DeployRole", "turnId": "failed"})
    source = error_descriptor(event)
    controller = session.resource_references
    payload = request_choice(controller, with_error_source(transcript, session), source)
    with TestClient(create_app(session_manager=manager)) as client:
        prefix = f"/api/sessions/{session.web_session_id}"
        response = client.post(prefix + "/resource-references/resolve", json=payload)
        assert response.status_code == 200 and response.json()["status"] == "saved"
        messages = client.get(prefix + "/messages").json()
        assert "resourceErrorSources" not in messages
        assert messages["resourceReferences"]["bindings"] == response.json()["snapshot"]["bindings"]
        repeated = session.events.append("error", dict(event["payload"]))
        current = client.get(prefix + "/resource-references").json()
        assert not current["bindings"] and current["items"]
        payload["expectedRevision"] = current["revision"]
        assert client.post(prefix + "/resource-references/resolve", json=payload).status_code == 400
        payload = request_choice(controller, with_error_source(transcript, session), error_descriptor(repeated))
        response = client.post(prefix + "/resource-references/resolve", json=payload)
        assert response.status_code == 200 and response.json()["status"] == "saved"
        session.events.append("user.message", {"turnId": "next", "text": "Continue"})
        current = client.get(prefix + "/resource-references").json()
        assert not current["bindings"] and current["items"]
