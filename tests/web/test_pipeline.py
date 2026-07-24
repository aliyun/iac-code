from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from a2a.types import Message, Role, TaskState, TaskStatus, TaskStatusUpdateEvent
from starlette.testclient import TestClient

from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.services.session_storage import SessionStorage


def _event(sequence: int, event_id: str, *, task_id: str = "task-1", context_id: str = "ctx-1") -> dict:
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "sequence": sequence,
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }


def _config_app(monkeypatch, tmp_path: Path):
    from iac_code.web.app import create_app

    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    return create_app(), config_dir


def _write_pipeline_state(config_dir: Path, project: Path, *, session_id: str = "session-1") -> None:
    persistence = A2APersistenceStore(config_dir / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(project)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    pipeline_dir = SessionStorage().session_dir(str(project), session_id) / "pipeline"
    first = _event(1, "evt-1")
    second = _event(2, "evt-2")
    A2APipelineJournal(pipeline_dir).append(first)
    A2APipelineJournal(pipeline_dir).append(second)
    snapshot = reduce_pipeline_events([first])
    snapshot["display"]["messages"].append({"eventId": "msg-1", "text": "first message"})
    snapshot["control"]["inputHistory"].append({"eventId": "input-1", "message": "need input"})
    snapshot["cleanup"] = {
        "status": "in_progress",
        "resourceCount": 1,
        "resources": [{"resourceId": "stack-1", "cleanupStatus": "DELETE_IN_PROGRESS"}],
        "history": [{"eventId": "cleanup-1", "status": "in_progress"}],
    }
    A2APipelineSnapshotStore(pipeline_dir).save(snapshot)


class _RuntimeErrorRecovery:
    async def get_state(self, **_kwargs) -> dict:
        raise RuntimeError("boom at /private/secret/path")


class _RecordingPipelineActionRunner:
    def __init__(
        self,
        *,
        select_result: Any | None = None,
        interrupt_result: Any | None = None,
    ) -> None:
        self.select_result = select_result or SimpleNamespace(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": "candidate_selected"},
            events=[{"kind": "candidate.selected", "queued": False}],
        )
        self.start_result = SimpleNamespace(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": "started"},
            events=[{"kind": "pipeline.started", "queued": False}],
        )
        self.interrupt_result = interrupt_result or SimpleNamespace(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": "interrupt"},
            events=[{"kind": "pipeline.interrupt.judged", "judgeOutcome": "continue"}],
        )
        self.start_calls: list[dict[str, Any]] = []
        self.select_calls: list[dict[str, Any]] = []
        self.interrupt_calls: list[dict[str, Any]] = []
        self.candidate_model_selections: list[Any] = []
        # Resolvers are tracked separately so the call-dict asserts stay exact while
        # Issue 6 wiring (app passes a permission resolver) can still be checked.
        self.permission_resolvers: list[Any] = []

    async def start(
        self,
        session,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection=None,
        event_sink=None,
        permission_resolver=None,
        envelope_observer=None,
    ):
        self.start_calls.append(
            {
                "sessionId": session.session_id,
                "contextId": session.context_id,
                "taskId": session.task_id,
                "message": message,
                "imageIds": list(image_ids),
                "fileRefs": list(file_refs),
            }
        )
        self.permission_resolvers.append(permission_resolver)
        return self.start_result

    async def select_candidate(
        self,
        session,
        selection,
        *,
        model_selection=None,
        event_sink=None,
        permission_resolver=None,
        envelope_observer=None,
    ):
        self.select_calls.append(
            {
                "sessionId": session.session_id,
                "contextId": session.context_id,
                "taskId": session.task_id,
                "encodedInput": selection.encoded_input,
                "candidateName": selection.candidate_name,
                "candidateIndex": selection.candidate_index,
                "parameterOverrides": dict(selection.parameter_overrides),
            }
        )
        self.candidate_model_selections.append(model_selection)
        self.permission_resolvers.append(permission_resolver)
        return self.select_result

    async def interrupt(
        self,
        session,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection=None,
        event_sink=None,
        permission_resolver=None,
    ):
        self.interrupt_calls.append(
            {
                "sessionId": session.session_id,
                "contextId": session.context_id,
                "taskId": session.task_id,
                "message": message,
                "imageIds": list(image_ids),
                "fileRefs": list(file_refs),
            }
        )
        self.permission_resolvers.append(permission_resolver)
        return self.interrupt_result


class _DelayedPipelineActionRunner(_RecordingPipelineActionRunner):
    def __init__(self, *, select_result: Any | None = None) -> None:
        super().__init__(select_result=select_result)
        self.select_started = asyncio.Event()
        self.select_release = asyncio.Event()
        self.select_entries = 0
        self.interrupt_started = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self.interrupt_entries = 0

    async def select_candidate(
        self,
        session,
        selection,
        *,
        model_selection=None,
        event_sink=None,
        permission_resolver=None,
        envelope_observer=None,
    ):
        self.select_entries += 1
        self.select_started.set()
        await self.select_release.wait()
        return await super().select_candidate(
            session,
            selection,
            model_selection=model_selection,
            event_sink=event_sink,
            permission_resolver=permission_resolver,
        )

    async def interrupt(
        self,
        session,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection=None,
        event_sink=None,
        permission_resolver=None,
    ):
        self.interrupt_entries += 1
        self.interrupt_started.set()
        await self.interrupt_release.wait()
        return await super().interrupt(
            session,
            message,
            image_ids,
            file_refs,
            model_selection=model_selection,
            event_sink=event_sink,
            permission_resolver=permission_resolver,
        )


def test_pipeline_state_route_requires_context_or_task(monkeypatch, tmp_path) -> None:
    app, _config_dir = _config_app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/pipeline/state")

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "contextId or taskId is required"}}


def test_pipeline_state_route_rejects_invalid_after_sequence(monkeypatch, tmp_path) -> None:
    app, _config_dir = _config_app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/pipeline/state", params={"contextId": "ctx-1", "afterSequence": "²"})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "afterSequence must be a non-negative integer"}}


def test_pipeline_state_route_rejects_invalid_context_id_before_recovery(monkeypatch, tmp_path) -> None:
    app, _config_dir = _config_app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/pipeline/state", params={"contextId": "../ctx"})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "contextId is invalid"}}


def test_pipeline_state_route_rejects_invalid_task_id_before_recovery(monkeypatch, tmp_path) -> None:
    app, _config_dir = _config_app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/pipeline/state", params={"taskId": "bad/id"})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "taskId is invalid"}}


@pytest.mark.asyncio
async def test_pipeline_state_from_query_propagates_unexpected_recovery_errors() -> None:
    from iac_code.web.pipeline import pipeline_state_from_query

    with pytest.raises(RuntimeError, match="boom"):
        await pipeline_state_from_query({"contextId": "ctx-1"}, recovery_service=_RuntimeErrorRecovery())


def test_pipeline_state_route_returns_generic_500_for_unexpected_recovery_errors(monkeypatch, tmp_path) -> None:
    app, _config_dir = _config_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "iac_code.web.pipeline.create_a2a_pipeline_recovery_service",
        lambda: _RuntimeErrorRecovery(),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/pipeline/state", params={"contextId": "ctx-1"})

    assert response.status_code == 500
    assert response.json() == {"error": {"message": "internal server error"}}
    assert "/private/secret/path" not in response.text


def test_pipeline_state_route_does_not_fabricate_state_from_query_params(monkeypatch, tmp_path) -> None:
    app, config_dir = _config_app(monkeypatch, tmp_path)
    A2APersistenceStore(config_dir / "a2a").save_context(
        A2AContextSnapshot(context_id="ctx-empty", session_id="session-empty", cwd=str(tmp_path / "project"))
    )

    with TestClient(app) as client:
        response = client.get("/api/pipeline/state", params={"contextId": "ctx-empty"})

    assert response.status_code == 404
    assert response.json() == {"error": {"message": "pipeline state not found"}}


def test_pipeline_state_route_replays_events_after_sequence_and_preserves_snapshot_sections(
    monkeypatch,
    tmp_path,
) -> None:
    app, config_dir = _config_app(monkeypatch, tmp_path)
    _write_pipeline_state(config_dir, tmp_path / "project")

    with TestClient(app) as client:
        response = client.get("/api/pipeline/state", params={"contextId": "ctx-1", "afterSequence": "1"})

    assert response.status_code == 200
    data = response.json()
    snapshot = data["snapshot"]
    assert snapshot["contextId"] == "ctx-1"
    assert snapshot["taskId"] == "task-1"
    assert snapshot["lastSequence"] == 1
    assert snapshot["display"]["messages"] == [{"eventId": "msg-1", "text": "first message"}]
    assert snapshot["control"]["inputHistory"] == [{"eventId": "input-1", "message": "need input"}]
    assert snapshot["cleanup"]["status"] == "in_progress"
    assert snapshot["cleanup"]["resources"][0]["resourceId"] == "stack-1"
    assert [event["eventId"] for event in data["events"]] == ["evt-2"]


def test_pipeline_state_route_resolves_state_by_task_id(monkeypatch, tmp_path) -> None:
    app, config_dir = _config_app(monkeypatch, tmp_path)
    _write_pipeline_state(config_dir, tmp_path / "project")

    with TestClient(app) as client:
        response = client.get("/api/pipeline/state", params={"taskId": "task-1"})

    assert response.status_code == 200
    assert response.json()["snapshot"]["contextId"] == "ctx-1"


def test_pipeline_message_route_starts_pipeline_action_runner_and_persists_identity(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tf").write_text("resource test\n", encoding="utf-8")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/messages",
            json={"text": "create an ECS stack", "imageIds": [], "fileRefs": ["main.tf"]},
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["mode"] == "pipeline"
    assert payload["contextId"].startswith("ctx-")
    assert payload["taskId"].startswith("task-")
    assert runner.start_calls == [
        {
            "sessionId": session.session_id,
            "contextId": payload["contextId"],
            "taskId": payload["taskId"],
            "message": "create an ECS stack",
            "imageIds": [],
            "fileRefs": ["main.tf"],
        }
    ]
    # Issue 6: the route wires a session-bound permission resolver so interactive tool
    # prompts surface in the browser instead of being auto-denied.
    assert callable(runner.permission_resolvers[0])

    reloaded = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project").create_session(
        session_id=session.session_id
    )
    assert reloaded.mode == "pipeline"
    assert reloaded.context_id == payload["contextId"]
    assert reloaded.task_id == payload["taskId"]


@pytest.mark.asyncio
async def test_pipeline_turn_drains_queued_follow_up_as_an_independent_turn(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class BlockingRunner(_RecordingPipelineActionRunner):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.start_result.terminal_outcome = None

        async def start(self, session, message, image_ids, file_refs, **kwargs):
            if not self.start_calls:
                self.first_started.set()
                await self.release_first.wait()
            return await super().start(session, message, image_ids, file_refs, **kwargs)

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = BlockingRunner()
    session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "first"})
        await asyncio.wait_for(runner.first_started.wait(), timeout=1)
        queued = await client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": "follow up"})
        runner.release_first.set()

        async def queued_turn_finished() -> None:
            while len(runner.start_calls) < 2 or session.active_turn_task is not None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(queued_turn_finished(), timeout=1)

    assert first.status_code == 202
    assert queued.status_code == 200
    assert [call["message"] for call in runner.start_calls] == ["first", "follow up"]
    assert session.queued_inputs == []
    removed = [event for event in session.events.replay_after(0) if event["type"] == "queued-input.removed"]
    assert len(removed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("next_mode", ["normal", "pipeline"])
async def test_failed_queued_turn_start_releases_reservation_and_restores_input(
    tmp_path, monkeypatch, next_mode
) -> None:
    from iac_code.web import pipeline_actions
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class BlockingRunner(_RecordingPipelineActionRunner):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.first_completed = False
            self.start_result.terminal_outcome = None

        async def start(self, session, message, image_ids, file_refs, **kwargs):
            self.first_started.set()
            await self.release_first.wait()
            result = await super().start(session, message, image_ids, file_refs, **kwargs)
            self.first_completed = True
            return result

    async def load_snapshot(**_kwargs):
        if next_mode == "normal" and runner.first_completed:
            return {
                "normalHandoff": {
                    "action": "switch_to_normal",
                    "targetMode": "normal",
                    "summary": "handoff complete",
                }
            }
        return {}

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = BlockingRunner()
    session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    monkeypatch.setattr(pipeline_actions, "load_pipeline_snapshot", load_snapshot)

    original_attach = manager.attach_pipeline_identity
    attach_calls = 0

    def attach_pipeline_identity(*args, **kwargs):
        nonlocal attach_calls
        attach_calls += 1
        if next_mode == "pipeline" and attach_calls > 1:
            raise RuntimeError("pipeline pre-start failed")
        return original_attach(*args, **kwargs)

    monkeypatch.setattr(manager, "attach_pipeline_identity", attach_pipeline_identity)

    def runtime_factory(_session):
        raise RuntimeError("normal pre-start failed")

    app = create_app(
        session_manager=manager,
        runtime_factory=runtime_factory,
        pipeline_action_runner_factory=lambda: runner,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "first"})
        await asyncio.wait_for(runner.first_started.wait(), timeout=1)
        queued = await client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": "follow up"})
        runner.release_first.set()

        async def continuation_finished() -> None:
            while session.status != "idle" or session.queued_inputs != ["follow up"]:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0)

        await asyncio.wait_for(continuation_finished(), timeout=1)

    assert first.status_code == 202
    assert queued.status_code == 200
    assert session.mode == next_mode
    assert session.queued_inputs == ["follow up"]
    assert session.active_turn_task is None
    assert session.turn_admission_lock.locked() is False


@pytest.mark.asyncio
async def test_pipeline_message_rechecks_archive_after_handoff_snapshot_load(tmp_path, monkeypatch) -> None:
    from iac_code.web import pipeline_actions
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def load_snapshot(**_kwargs):
        snapshot_started.set()
        await release_snapshot.wait()
        return {
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "summary": "stale handoff context",
            }
        }

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    monkeypatch.setattr(pipeline_actions, "load_pipeline_snapshot", load_snapshot)
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        message_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "late"})
        )
        await asyncio.wait_for(snapshot_started.wait(), timeout=1)
        archived = await client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
        release_snapshot.set()
        message = await asyncio.wait_for(message_task, timeout=1)

    assert archived.status_code == 200
    assert message.status_code == 409
    assert message.json()["error"]["code"] == "session_archived"
    assert runner.start_calls == []
    assert session.mode == "pipeline"
    assert all(
        message.get_text() != "stale handoff context" for message in manager.load_resume_messages(session.session_id)
    )


@pytest.mark.asyncio
async def test_pipeline_handoff_cannot_mutate_recreated_session(tmp_path, monkeypatch) -> None:
    from iac_code.web import pipeline_actions
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def load_snapshot(**_kwargs):
        snapshot_started.set()
        await release_snapshot.wait()
        return {
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "summary": "stale handoff context",
            }
        }

    cwd = tmp_path / "project"
    projects_dir = tmp_path / "projects"
    manager = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    runner = _RecordingPipelineActionRunner()
    old_session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    monkeypatch.setattr(pipeline_actions, "load_pipeline_snapshot", load_snapshot)
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        message_task = asyncio.create_task(
            client.post(f"/api/sessions/{old_session.session_id}/messages", json={"text": "late"})
        )
        await asyncio.wait_for(snapshot_started.wait(), timeout=1)
        deleted = await client.delete(f"/api/sessions/{old_session.session_id}")
        fresh_session = manager.create_session(mode="normal", session_id=old_session.session_id)
        manager.rename_session(fresh_session, "fresh-session")
        release_snapshot.set()
        message = await asyncio.wait_for(message_task, timeout=1)

    assert deleted.status_code == 200
    assert message.status_code == 404
    assert message.json() == {"error": {"message": "session not found"}}
    assert runner.start_calls == []
    assert manager.get_session(fresh_session.web_session_id) is fresh_session
    assert fresh_session.mode == "normal"
    assert fresh_session.title == "fresh-session"
    assert all(
        message.get_text() != "stale handoff context"
        for message in manager.load_resume_messages(fresh_session.session_id)
    )

    reloaded_manager = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    reloaded_session = reloaded_manager.create_session(cwd=cwd, session_id=fresh_session.session_id)
    assert reloaded_session.mode == "normal"
    assert reloaded_session.title == "fresh-session"
    assert all(
        message.get_text() != "stale handoff context"
        for message in reloaded_manager.load_resume_messages(reloaded_session.session_id)
    )


def test_pipeline_message_route_publishes_user_message_bubble(tmp_path) -> None:
    import time

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tf").write_text("resource test\n", encoding="utf-8")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/messages",
            json={"text": "create an ECS stack", "imageIds": [], "fileRefs": ["main.tf"]},
        )
        assert response.status_code == 202

        # 后台流水线回合异步运行,轮询直到本轮结束(turn.done)再断言事件序列。
        deadline = time.monotonic() + 5
        events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            events = session.events.replay_after(0)
            if any(event["type"] == "turn.done" for event in events):
                break
            time.sleep(0.02)

    types = [event["type"] for event in events]
    assert "user.message" in types
    # 用户气泡必须先于 pipeline.web_turn.started 出现,复刻普通回合的实时渲染顺序。
    assert types.index("user.message") < types.index("pipeline.event")
    user_event = next(event for event in events if event["type"] == "user.message")
    assert user_event["payload"]["text"] == "create an ECS stack"
    assert user_event["payload"]["source"] == "pipeline"
    assert user_event["payload"]["fileRefs"] == ["main.tf"]
    assert user_event["payload"]["imageIds"] == []


def test_pipeline_message_route_sets_session_title_from_first_prompt(tmp_path) -> None:
    import time

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    assert session.title == "(empty)"
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/messages",
            json={"text": "帮我搭一条完整的售卖流水线", "imageIds": [], "fileRefs": []},
        )
        assert response.status_code == 202

        deadline = time.monotonic() + 5
        events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            events = session.events.replay_after(0)
            if any(event["type"] == "turn.done" for event in events):
                break
            time.sleep(0.02)

    # 首个流水线回合应把 prompt 文本设为会话标题,并广播 session.updated 让侧栏立即刷新。
    assert session.title == "帮我搭一条完整的售卖流水线"
    updated_events = [event for event in events if event["type"] == "session.updated"]
    assert any(event["payload"].get("title") == "帮我搭一条完整的售卖流水线" for event in updated_events)


def test_handoff_injects_pipeline_context_into_normal_chat_resume(monkeypatch, tmp_path) -> None:
    # 流水线交接给普通对话时,须把引擎生成的交接摘要(normalHandoff.summary)落入 web 会话 JSONL,
    # 否则进入普通对话后 LLM 读不到流水线上下文、答「什么都没创建」。这里模拟已交接的快照,POST
    # 一条普通输入触发重启兜底的模式翻转,断言:模式翻转为 normal 且交接摘要进入 resume 上下文。
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(mode="pipeline", pipeline_name="selling", session_id="session-1")
    session.context_id = "ctx-1"
    session.task_id = "task-1"
    manager.persist_web_metadata(session)

    summary = "[Pipeline Handoff Context]\nPipeline: selling\nOutcome: completed\n已创建 VPC、VSwitch 与 ECS。"

    async def fake_load_pipeline_snapshot(*, context_id, task_id):
        assert context_id == "ctx-1"
        assert task_id == "task-1"
        return {
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "outcome": "completed",
                "summary": summary,
            }
        }

    monkeypatch.setattr(
        "iac_code.web.pipeline_actions.load_pipeline_snapshot",
        fake_load_pipeline_snapshot,
    )
    # 本合成环境没有真实 cleanup ledger,清理门禁会以「unreadable」拦截普通回合;它与本次修复
    # (交接上下文注入)正交,置为不拦截,让普通回合真正跑到 runtime。
    monkeypatch.setattr("iac_code.web.cleanup.cleanup_blocks_normal_chat", lambda _status: False)

    seen: list[WebTurnRequest] = []

    class RecordingRuntime:
        async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
            seen.append(request)
            return {"accepted": True, "turnId": request.turn_id or "turn-x"}

    app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingRuntime())

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/messages",
            json={"text": "你刚才创建了什么", "imageIds": [], "fileRefs": []},
        )
        assert response.status_code in {200, 202}

    # 交接后模式翻转为 normal,普通输入走普通 runtime(而非被流水线路径吞掉)。
    assert session.mode == "normal"
    assert len(seen) == 1
    assert seen[0].text == "你刚才创建了什么"

    # 关键断言:交接摘要进入 resume 上下文(喂给 LLM),这样普通对话才知道流水线创建了什么。
    resume = manager.load_resume_messages(session.session_id, cwd=str(project))
    assert any(msg.role == "user" and msg.get_text() == summary for msg in resume)

    # 但交接摘要不得渲染成用户气泡。
    transcript = manager.load_visible_transcript(session.session_id, cwd=str(project))
    import json as _json

    assert "[Pipeline Handoff Context]" not in _json.dumps(transcript, ensure_ascii=False)


def test_candidate_selection_route_calls_pipeline_action_runner_with_encoded_selection(tmp_path) -> None:
    from iac_code.pipeline.engine.ui_contract import encode_selected_candidate
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            "/api/pipeline/candidates/select",
            json={
                "sessionId": session.session_id,
                "candidateName": "Plan A",
                "candidateIndex": 0,
                "parameterOverrides": {"InstanceType": "ecs.g7.large"},
            },
        )

    expected_input = encode_selected_candidate("Plan A", 0, {"InstanceType": "ecs.g7.large"})
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "action": "candidate_selected"}
    assert runner.select_calls == [
        {
            "sessionId": session.session_id,
            "contextId": "ctx-1",
            "taskId": "task-1",
            "encodedInput": expected_input,
            "candidateName": "Plan A",
            "candidateIndex": 0,
            "parameterOverrides": {"InstanceType": "ecs.g7.large"},
        }
    ]
    assert session.queued_inputs == []
    events = session.events.replay_after(0)
    # 选择候选续跑流水线后必须补发 turn.done,清前端「运行中」态、解开 prompt 排队(Issue 4)。
    # 无真实快照 → maybe_switch_session_to_normal 判否,不发 session.updated。
    assert [event["type"] for event in events] == ["pipeline.event", "turn.done"]
    assert events[0]["payload"] == {
        "kind": "candidate.selected",
        "queued": False,
        "contextId": "ctx-1",
        "taskId": "task-1",
        "mode": "pipeline",
    }
    assert events[1]["payload"] == {
        "mode": "pipeline",
        "interrupted": False,
        "canceled": False,
        "failed": False,
        "contextId": "ctx-1",
        "taskId": "task-1",
    }


def test_successful_candidate_selection_marks_unwatched_session_unread(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            "/api/pipeline/candidates/select",
            json={"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0},
        )

    assert response.status_code == 202
    assert session.unread is True


@pytest.mark.asyncio
async def test_successful_candidate_selection_stays_read_with_active_event_subscriber(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    stream = session.events.stream_after(0)
    pending_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                "/api/pipeline/candidates/select",
                json={"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0},
            )
        await asyncio.wait_for(pending_event, timeout=1)
    finally:
        await stream.aclose()

    assert response.status_code == 202
    assert session.unread is False


def test_candidate_selection_route_uses_one_immutable_model_selection(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebModelSelection
    from iac_code.web.session_manager import WebSessionManager

    frozen_selection = WebModelSelection(
        provider="dashscope",
        model="glm-5.2",
        effort="high",
        provider_api_key="snapshot-key",
        provider_base_url="https://snapshot.invalid/v1",
        provider_config_frozen=True,
        provider_config_override={"thinkingEnabled": True, "thinkingBudget": 2048},
    )
    monkeypatch.setattr(
        "iac_code.web.runtime.model_selection_for_session",
        lambda session: frozen_selection,
    )
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            "/api/pipeline/candidates/select",
            json={"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0},
        )

    assert response.status_code == 202
    assert runner.candidate_model_selections == [frozen_selection]


@pytest.mark.asyncio
async def test_concurrent_candidate_selection_returns_busy_and_runs_one_action(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _DelayedPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    body = {"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first = asyncio.create_task(client.post("/api/pipeline/candidates/select", json=body))
        await asyncio.wait_for(runner.select_started.wait(), timeout=1)
        second = asyncio.create_task(client.post("/api/pipeline/candidates/select", json=body))
        await asyncio.sleep(0.05)
        runner.select_release.set()
        responses = await asyncio.gather(first, second)

    assert sorted(response.status_code for response in responses) == [202, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json() == {"accepted": False, "reason": "turn already running"}
    assert runner.select_entries == 1
    assert len(runner.select_calls) == 1


@pytest.mark.asyncio
async def test_stop_cancels_candidate_selection_before_runner_is_released(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _DelayedPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    permission_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    manager.add_permission_request(session, {"toolName": "bash"}, future=permission_future)
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    candidate_body = {"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        candidate_task = asyncio.create_task(client.post("/api/pipeline/candidates/select", json=candidate_body))
        await asyncio.wait_for(runner.select_started.wait(), timeout=1)
        stop_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/interrupt", json={"message": ""})
        )
        await asyncio.sleep(0.05)
        stop_completed_before_release = stop_task.done()
        runner.select_release.set()
        candidate_response, stop_response = await asyncio.gather(candidate_task, stop_task)

    assert stop_completed_before_release is True
    assert stop_response.status_code == 200
    assert stop_response.json() == {"accepted": True}
    assert candidate_response.status_code == 409
    assert candidate_response.json() == {
        "accepted": False,
        "reason": "turn canceled",
        "canceled": True,
        "interrupted": True,
    }
    assert permission_future.result() is False
    assert session.pending_permissions == {}
    assert session.active_turn_task is None
    done_events = [event for event in session.events.replay_after(0) if event["type"] == "turn.done"]
    assert done_events[-1]["payload"]["interrupted"] is True
    assert done_events[-1]["payload"]["canceled"] is True


@pytest.mark.asyncio
async def test_candidate_selection_drains_follow_up_queued_during_action_after_handoff(tmp_path, monkeypatch) -> None:
    import iac_code.web.pipeline_actions as pipeline_actions
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _DelayedPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    started: list[str] = []

    class Runtime:
        async def start_turn(self, request):
            started.append(request.text)
            return {"accepted": True, "turnId": request.turn_id, "inputConsumed": True}

    async def load_snapshot(**_kwargs):
        return {
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "summary": "handoff complete",
            }
        }

    monkeypatch.setattr(pipeline_actions, "load_pipeline_snapshot", load_snapshot)
    app = create_app(
        session_manager=manager,
        pipeline_action_runner_factory=lambda: runner,
        runtime_factory=lambda _session: Runtime(),
    )
    body = {"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        candidate_task = asyncio.create_task(client.post("/api/pipeline/candidates/select", json=body))
        await asyncio.wait_for(runner.select_started.wait(), timeout=1)
        queued_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": "follow-up"})
        )
        await asyncio.sleep(0)
        runner.select_release.set()
        candidate_response, queued_response = await asyncio.gather(candidate_task, queued_task)
        active = session.active_turn_task
        if isinstance(active, asyncio.Task):
            await asyncio.wait_for(active, timeout=1)

    assert candidate_response.status_code == 202
    assert queued_response.status_code == 200
    assert queued_response.json()["accepted"] is True
    assert session.mode == "normal"
    assert session.queued_inputs == []
    assert started == ["follow-up"]


@pytest.mark.asyncio
async def test_failed_candidate_selection_leaves_concurrent_follow_up_queued(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    result = SimpleNamespace(
        accepted=False,
        status_code=409,
        response={"accepted": False, "reason": "candidate rejected"},
        events=[],
    )
    runner = _DelayedPipelineActionRunner(select_result=result)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    body = {"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        candidate_task = asyncio.create_task(client.post("/api/pipeline/candidates/select", json=body))
        await asyncio.wait_for(runner.select_started.wait(), timeout=1)
        queued_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": "keep queued"})
        )
        await asyncio.sleep(0)
        runner.select_release.set()
        candidate_response, queued_response = await asyncio.gather(candidate_task, queued_task)

    assert candidate_response.status_code == 409
    assert queued_response.status_code == 200
    assert session.queued_inputs == ["keep queued"]


@pytest.mark.asyncio
async def test_pipeline_message_returns_busy_while_candidate_selection_is_running(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _DelayedPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    candidate_body = {"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        candidate_task = asyncio.create_task(client.post("/api/pipeline/candidates/select", json=candidate_body))
        await asyncio.wait_for(runner.select_started.wait(), timeout=1)
        message_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "must stay busy"})
        )
        await asyncio.sleep(0.05)
        completed_before_release = message_task.done()
        runner.select_release.set()
        candidate_response, message_response = await asyncio.gather(candidate_task, message_task)

    assert completed_before_release is True
    assert candidate_response.status_code == 202
    assert message_response.status_code == 409
    assert message_response.json() == {"accepted": False, "reason": "turn already running"}
    assert runner.start_calls == []


def test_pipeline_action_event_publisher_honors_web_event_type_for_snapshots(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner(
        select_result=SimpleNamespace(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": "candidate_selected"},
            events=[
                {"kind": "candidate.selected"},
                {
                    "webEventType": "pipeline.snapshot",
                    "snapshot": {"contextId": "ctx-1", "taskId": "task-1", "lastSequence": 7},
                },
            ],
        )
    )
    session = manager.create_session(mode="pipeline", context_id="ctx-1", task_id="task-1", session_id="session-1")
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            "/api/pipeline/candidates/select",
            json={"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0},
        )

    assert response.status_code == 202
    events = session.events.replay_after(0)
    # 动作事件之后补发 turn.done(Issue 4);无真实快照 → 不发 session.updated。
    assert [event["type"] for event in events] == ["pipeline.event", "pipeline.snapshot", "turn.done"]
    assert events[1]["payload"]["snapshot"]["lastSequence"] == 7


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"sessionId": "session-1", "candidateIndex": -1}, "candidateIndex must be a non-negative integer"),
        ({"sessionId": "session-1", "candidateIndex": True}, "candidateIndex must be a non-negative integer"),
        (
            {"sessionId": "session-1", "candidateName": "Plan A", "parameterOverrides": []},
            "parameterOverrides must be an object",
        ),
        ({"sessionId": "session-1"}, "candidateName or candidateIndex is required"),
    ],
)
def test_candidate_selection_route_validates_request_body(tmp_path, body: dict, message: str) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    manager.create_session(mode="pipeline", session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/pipeline/candidates/select", json=body)

    assert response.status_code == 400
    assert response.json() == {"error": {"message": message}}


def test_candidate_selection_route_rejects_non_pipeline_session_without_pipeline_metadata(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(mode="normal", session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            "/api/pipeline/candidates/select",
            json={"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0},
        )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "pipeline_not_active", "message": "session is not a pipeline session"}}


def test_normal_session_with_pipeline_metadata_rejects_candidate_selection(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(
        mode="normal",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            "/api/pipeline/candidates/select",
            json={"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pipeline_not_active"
    assert response.json()["error"]["message"] == "session is not a pipeline session"


@pytest.mark.parametrize("endpoint", ["/api/pipeline/candidates/select", "/api/sessions/session-1/interrupt"])
def test_pipeline_action_routes_require_context_and_task_metadata(tmp_path, endpoint: str) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    manager.create_session(mode="pipeline", session_id="session-1")
    app = create_app(session_manager=manager)
    body = {"sessionId": "session-1", "candidateName": "Plan A", "candidateIndex": 0}
    if endpoint.endswith("/interrupt"):
        body = {"message": "please reconsider"}

    with TestClient(app) as client:
        response = client.post(endpoint, json=body)

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "pipeline contextId and taskId are required"}}


def test_candidate_selection_route_returns_pipeline_action_errors_without_queueing(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner(
        select_result=SimpleNamespace(
            accepted=False,
            status_code=409,
            response={"accepted": False, "error": {"message": "pipeline is not waiting for candidate input"}},
            events=[],
        )
    )
    session = manager.create_session(mode="pipeline", context_id="ctx-1", task_id="task-1", session_id="session-1")
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            "/api/pipeline/candidates/select",
            json={"sessionId": session.session_id, "candidateName": "Plan A", "candidateIndex": 0},
        )

    assert response.status_code == 409
    assert response.json() == {
        "accepted": False,
        "error": {"message": "pipeline is not waiting for candidate input"},
    }
    assert session.queued_inputs == []
    # 拒绝(409)仍补发 turn.done(failed=True)以清运行态,与 SSE 路径一致;错误正文由 409 响应
    # 直接返回给 fetch 调用方,不再另发 error 事件(避免钉在消息栈底部)。不入队。
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["turn.done"]
    assert events[0]["payload"]["failed"] is True
    assert events[0]["payload"]["mode"] == "pipeline"


def test_web_app_manages_fallback_pipeline_runner_cleanup_loop(monkeypatch, tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.pipeline_actions import A2APipelineActionRunner
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = A2APipelineActionRunner()
    assert runner._uses_web_global_defaults is True
    assert runner._task_store._cleanup_task is None

    app = create_app(
        session_manager=WebSessionManager(projects_dir=tmp_path / "projects"),
        pipeline_action_runner_factory=lambda: runner,
    )
    with TestClient(app):
        assert runner._task_store._cleanup_task is not None

    assert runner._task_store._cleanup_task is None


def test_web_fallback_owner_exposes_raw_thinking_for_loopback_sink(monkeypatch, tmp_path) -> None:
    # The Web console only consumes the pre-remote-redaction (loopback) envelope, so the
    # fallback owner must enable RAW_THINKING; otherwise publish() drops every thinking_delta
    # and pipeline mode never shows 正在思考/思考完成 (remote clients use a separate owner).
    from iac_code.a2a.exposure import A2AExposureType, normalize_a2a_exposure_types
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = A2APipelineActionRunner()
    assert runner._uses_web_global_defaults is True
    exposure = normalize_a2a_exposure_types(runner._owner.thinking_exposure_types)
    assert A2AExposureType.RAW_THINKING in exposure
    assert A2AExposureType.TOOL_TRACE in exposure


@pytest.mark.asyncio
async def test_forwarding_queue_uses_local_pipeline_envelope_for_web_sink() -> None:
    from iac_code.web.pipeline_actions import _ForwardingEventQueue

    batches: list[list[dict[str, Any]]] = []

    async def sink(events: list[dict[str, Any]]) -> None:
        batches.append(events)

    queue = _ForwardingEventQueue(sink)
    raw_path = "/Users/alice/project/template.yml"
    await queue.enqueue_local_pipeline_envelope(
        {
            "schemaVersion": "1.0",
            "eventId": "evt-tool",
            "sequence": 1,
            "eventType": "tool_started",
            "scope": "pipeline",
            "pipelineRunId": "ctx-1",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "status": "working",
            "data": {
                "toolName": "read_file",
                "toolUseId": "tool-1",
                "input": {"api_key": "***", "path": raw_path},
            },
        }
    )

    rendered = json.dumps(batches)
    assert raw_path in rendered
    assert "sk-test-secret" not in rendered


@pytest.mark.asyncio
async def test_pipeline_action_runner_returns_live_a2a_events_and_reducer_snapshot(monkeypatch, tmp_path) -> None:
    from iac_code.web.pipeline import parse_candidate_selection_body
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(project)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    class LiveStatusExecutor:
        def __init__(self, *, task_store, **_kwargs) -> None:
            self.task_store = task_store

        async def execute(self, *, event_queue, task, task_id: str, context_id: str, **_kwargs) -> None:
            message = Message(
                message_id="working-message",
                task_id=task_id,
                context_id=context_id,
                role=Role.ROLE_AGENT,
                parts=[],
            )
            status = TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_WORKING), message=message)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status)
            )
            task.state = "working"
            self.task_store.mirror_task(task)

    async def fake_snapshot(*, context_id: str | None, task_id: str | None):
        return {
            "contextId": context_id,
            "taskId": task_id,
            "lastSequence": 9,
            "display": {"messages": []},
            "control": {"inputHistory": []},
        }

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor", LiveStatusExecutor)
    monkeypatch.setattr("iac_code.web.pipeline_actions.load_pipeline_snapshot", fake_snapshot)
    session = SimpleNamespace(session_id="session-1", context_id="ctx-1", task_id="task-1", cwd=str(project))
    selection = parse_candidate_selection_body(
        {"sessionId": "session-1", "candidateName": "Plan A", "candidateIndex": 0}
    )

    result = await A2APipelineActionRunner().select_candidate(session, selection)

    assert result.accepted is True
    assert [event.get("kind") for event in result.events] == [
        "candidate.selected",
        "a2a.task.status",
        None,
    ]
    assert result.events[1]["taskId"] == "task-1"
    assert result.events[1]["contextId"] == "ctx-1"
    assert result.events[1]["state"] == "TASK_STATE_WORKING"
    assert result.events[2]["webEventType"] == "pipeline.snapshot"
    assert result.events[2]["snapshot"]["lastSequence"] == 9


@pytest.mark.asyncio
async def test_pipeline_action_runner_uses_session_provider_model_and_effort(monkeypatch, tmp_path) -> None:
    from iac_code.a2a.metrics import NoOpA2AMetrics
    from iac_code.a2a.persistence import A2APersistenceStore
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    captured: dict[str, object] = {}

    class CapturingExecutor:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def execute(self, **_kwargs) -> None:
            return None

    async def fake_snapshot(*, context_id=None, task_id=None):
        return None

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor", CapturingExecutor)
    monkeypatch.setattr("iac_code.web.pipeline_actions.load_pipeline_snapshot", fake_snapshot)
    runner = A2APipelineActionRunner.__new__(A2APipelineActionRunner)
    runner._owner = SimpleNamespace(
        model="global-model",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        thinking_exposure_types=(),
        auto_approve_permissions=False,
    )
    runner._task_store = task_store
    session = SimpleNamespace(
        session_id="session-1",
        context_id="ctx-1",
        task_id="task-1",
        cwd=str(project),
        permission_mode="bypass_permissions",
        pipeline_name="selling",
        provider="custom-provider",
        model="session-model",
        effort="high",
    )

    result = await runner.start(session, "deploy", [], [])

    assert result.accepted is True
    assert captured["model"] == "session-model"
    assert captured["provider_key_override"] == "custom-provider"
    assert captured["effort_override"] == "high"


@pytest.mark.asyncio
async def test_local_web_pipeline_runner_refreshes_global_selection_for_each_action(monkeypatch, tmp_path) -> None:
    from iac_code.a2a.metrics import NoOpA2AMetrics
    from iac_code.a2a.persistence import A2APersistenceStore
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    selections = {
        "model": "model-at-server-start",
        "provider": "provider-at-server-start",
        "effort": "low",
    }
    captured: list[dict[str, object]] = []

    class CapturingExecutor:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

        async def execute(self, **_kwargs) -> None:
            return None

    async def fake_snapshot(*, context_id=None, task_id=None):
        return None

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor", CapturingExecutor)
    monkeypatch.setattr("iac_code.web.pipeline_actions.load_pipeline_snapshot", fake_snapshot)
    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: selections["model"])
    monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: selections["provider"])
    monkeypatch.setattr("iac_code.config.load_saved_effort", lambda: selections["effort"])
    runner = A2APipelineActionRunner.__new__(A2APipelineActionRunner)
    runner._owner = SimpleNamespace(
        model="model-at-server-start",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        thinking_exposure_types=(),
        auto_approve_permissions=False,
    )
    runner._task_store = task_store
    runner._uses_web_global_defaults = True
    session = SimpleNamespace(
        session_id="session-1",
        context_id="ctx-1",
        task_id="task-1",
        cwd=str(project),
        permission_mode="bypass_permissions",
        pipeline_name="selling",
        provider=None,
        model=None,
        effort=None,
    )

    await runner.start(session, "first", [], [])
    selections.update(model="model-after-settings-change", provider="provider-after-settings-change", effort="high")
    await runner.start(session, "second", [], [])

    assert [item["model"] for item in captured] == ["model-at-server-start", "model-after-settings-change"]
    assert [item["provider_key_override"] for item in captured] == [
        "provider-at-server-start",
        "provider-after-settings-change",
    ]
    assert [item["effort_override"] for item in captured] == ["low", "high"]


@pytest.mark.asyncio
async def test_default_pipeline_action_runner_uses_registered_shared_task_store_for_live_interrupt(
    monkeypatch,
    tmp_path,
) -> None:
    from iac_code.a2a.metrics import NoOpA2AMetrics
    from iac_code.a2a.runtime_registry import A2ARuntimeOwner, register_runtime_owner
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.pipeline.engine.interrupt import InterruptVerdict
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    handled_inputs: list[str] = []

    class FakePipeline:
        sidecar_status = "running"

        async def pause_agent_loops(self) -> None:
            return None

        async def resume_agent_loops(self) -> None:
            return None

        async def handle_user_interrupt(self, user_input):
            handled_inputs.append(getattr(user_input, "display_text", user_input))
            return InterruptVerdict(action="continue", reason="carry on")

    class FakeSnapshotStore:
        def __init__(self) -> None:
            self.snapshot: dict[str, Any] | None = None

        def load(self):
            return self.snapshot

        def save(self, snapshot) -> None:
            self.snapshot = snapshot

    class FakeJournal:
        def read_all_repairing_tail(self):
            return []

    class FakePublisher:
        def __init__(self) -> None:
            self.snapshot_store = FakeSnapshotStore()
            self.journal = FakeJournal()
            self.interrupts: list[dict[str, Any]] = []

        async def publish_interrupt_received(self, *, prompt: str) -> None:
            self.interrupts.append({"event": "received", "prompt": prompt})

        async def publish_interrupt(self, *, prompt: str, verdict: Any, parent_rollback=None, include_received=True):
            self.interrupts.append(
                {
                    "event": "classified",
                    "prompt": prompt,
                    "action": verdict.action,
                    "parentRollback": parent_rollback,
                    "includeReceived": include_received,
                }
            )

    runtime = SimpleNamespace(pipeline=FakePipeline(), publisher=FakePublisher())
    context = await task_store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(project),
        runtime_factory=lambda _session_id: runtime,
    )
    task = await task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.active_task = asyncio.current_task()
    context.active_task_id = "task-1"
    task_store.mirror_task(task)
    task_store.mirror_context(context)
    registration = register_runtime_owner(
        A2ARuntimeOwner(
            task_store=task_store,
            model="qwen3.6-plus",
            metrics=NoOpA2AMetrics(),
            persistence_root=config_dir / "a2a",
        )
    )
    session = SimpleNamespace(session_id="session-1", context_id="ctx-1", task_id="task-1", cwd=str(project))

    try:
        result = await A2APipelineActionRunner().interrupt(session, "please pause", [], [])
    finally:
        registration.unregister()

    assert result.accepted is True
    assert result.status_code == 202
    assert handled_inputs == ["please pause"]
    assert runtime.publisher.interrupts[-1]["action"] == "continue"


@pytest.mark.asyncio
async def test_pipeline_action_runner_rejects_executor_failed_status_without_success_events(
    monkeypatch,
    tmp_path,
) -> None:
    from iac_code.a2a.events import make_text_part
    from iac_code.web.pipeline import parse_candidate_selection_body
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(project)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    class FailedStatusExecutor:
        def __init__(self, *, task_store, **_kwargs) -> None:
            self.task_store = task_store

        async def execute(self, *, event_queue, task, task_id: str, context_id: str, **_kwargs) -> None:
            message = Message(
                message_id="failure-message",
                task_id=task_id,
                context_id=context_id,
                role=Role.ROLE_AGENT,
                parts=[make_text_part("Pipeline failed at /Users/alice/.iac-code/settings.yml")],
            )
            status = TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_FAILED), message=message)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status)
            )
            task.state = "failed"
            self.task_store.mirror_task(task)

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor", FailedStatusExecutor)
    session = SimpleNamespace(session_id="session-1", context_id="ctx-1", task_id="task-1", cwd=str(project))
    selection = parse_candidate_selection_body(
        {"sessionId": "session-1", "candidateName": "Plan A", "candidateIndex": 0}
    )

    result = await A2APipelineActionRunner().select_candidate(session, selection)

    assert result.accepted is False
    assert result.status_code == 409
    assert result.events == []
    assert result.response["accepted"] is False
    # 终态标记为 failed:上层据此不再发底部 error 事件(改由主转录彩色结局行承载)。
    assert result.terminal_outcome == "failed"
    message = result.response["error"]["message"]
    assert "Pipeline failed" in message
    assert "/Users/alice" not in message


def test_pipeline_interrupt_route_rejects_persisted_task_without_live_active_pipeline(monkeypatch, tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.pipeline_actions import A2APipelineActionRunner, PipelineActionResult
    from iac_code.web.session_manager import WebSessionManager

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    persistence.save_context(
        A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(project), active_task_id="task-1")
    )
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))

    async def unexpected_execute(self, session, pipeline_input, *, action, events):  # noqa: ANN001
        return PipelineActionResult(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": action},
            events=events,
        )

    monkeypatch.setattr(A2APipelineActionRunner, "_execute", unexpected_execute)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(mode="pipeline", context_id="ctx-1", task_id="task-1", session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/interrupt", json={"message": "pause"})

    assert response.status_code == 409
    assert response.json() == {"accepted": False, "error": {"message": "pipeline is not active"}}
    assert session.events.replay_after(0) == []


@pytest.mark.asyncio
async def test_concurrent_pipeline_interrupt_returns_busy_and_runs_one_action(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _DelayedPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    body = {"message": "please reconsider"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first = asyncio.create_task(client.post(f"/api/sessions/{session.session_id}/interrupt", json=body))
        await asyncio.wait_for(runner.interrupt_started.wait(), timeout=1)
        second = asyncio.create_task(client.post(f"/api/sessions/{session.session_id}/interrupt", json=body))
        await asyncio.sleep(0.05)
        runner.interrupt_release.set()
        responses = await asyncio.gather(first, second)

    assert sorted(response.status_code for response in responses) == [202, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json() == {"accepted": False, "reason": "turn already running"}
    assert runner.interrupt_entries == 1
    assert len(runner.interrupt_calls) == 1


@pytest.mark.asyncio
async def test_pipeline_interrupt_with_message_reaches_active_pipeline_turn(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = _RecordingPipelineActionRunner()
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    release = asyncio.Event()
    active_turn = asyncio.create_task(release.wait())
    session.active_turn_task = active_turn
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                f"/api/sessions/{session.session_id}/interrupt",
                json={"message": "please reconsider"},
            )
    finally:
        release.set()
        await active_turn

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "action": "interrupt"}
    assert len(runner.interrupt_calls) == 1


@pytest.mark.parametrize(
    ("judge_outcome", "action"),
    [
        ("continue", "continue"),
        ("add_context", "supplement"),
        ("interrupt_and_reconsider", "hard_interrupt"),
        ("pause_for_manual_handling", "pause"),
    ],
)
def test_pipeline_interrupt_route_calls_runner_and_publishes_judge_outcomes(
    tmp_path,
    monkeypatch,
    judge_outcome: str,
    action: str,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *_args, **_kwargs: True)
    runner = _RecordingPipelineActionRunner(
        interrupt_result=SimpleNamespace(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": action, "judgeOutcome": judge_outcome},
            events=[
                {
                    "kind": "pipeline.interrupt.judged",
                    "judgeOutcome": judge_outcome,
                    "action": action,
                    "reason": "reviewed by judge",
                }
            ],
        )
    )
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    Path(session.cwd).mkdir(parents=True, exist_ok=True)
    (Path(session.cwd) / "main.yaml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    store_cached_image(
        "image-1",
        b"\x89PNG\r\n\x1a\npng-data",
        media_type="image/png",
        cwd=session.cwd,
        session_id=session.session_id,
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/interrupt",
            json={"message": "please reconsider", "imageIds": ["image-1"], "fileRefs": ["main.yaml"]},
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "action": action, "judgeOutcome": judge_outcome}
    assert runner.interrupt_calls == [
        {
            "sessionId": "session-1",
            "contextId": "ctx-1",
            "taskId": "task-1",
            "message": "please reconsider",
            "imageIds": ["image-1"],
            "fileRefs": ["main.yaml"],
        }
    ]
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["pipeline.event"]
    assert events[0]["payload"] == {
        "kind": "pipeline.interrupt.judged",
        "pipelineInterrupt": True,
        "mode": "pipeline",
        "contextId": "ctx-1",
        "taskId": "task-1",
        "judgeOutcome": judge_outcome,
        "action": action,
        "reason": "reviewed by judge",
        "message": "please reconsider",
        "imageIds": ["image-1"],
        "fileRefs": ["main.yaml"],
    }


def test_pipeline_interrupt_rejects_images_for_text_only_model(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "iac_code.services.capabilities.multimodal.is_model_multimodal",
        lambda *_args, **_kwargs: False,
    )
    runner = _RecordingPipelineActionRunner()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    session.provider = "text-provider"
    session.model = "text-model"
    store_cached_image(
        "image-1",
        b"\x89PNG\r\n\x1a\npng-data",
        media_type="image/png",
        cwd=session.cwd,
        session_id=session.session_id,
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/{}/interrupt".format(session.web_session_id),
            json={"message": "reconsider", "imageIds": ["image-1"]},
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "Current model text-model does not support image input."}}
    assert runner.interrupt_calls == []


def test_pipeline_interrupt_uses_partner_selection_for_capability_and_runtime(tmp_path, monkeypatch) -> None:
    from iac_code.services.qwenpaw_source import QwenPawConfig
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image
    from iac_code.web.session_manager import WebSessionManager

    partner = QwenPawConfig(
        model="partner-vision-model",
        provider_key="dashscope",
        api_key="fake-partner-key",
        base_url="https://partner.invalid/v1",
    )
    monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: None)
    monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
    monkeypatch.setattr("iac_code.services.qwenpaw_source.load_from_qwenpaw", lambda: partner)
    capability_checks = []

    def supports_images(model, *, provider_key=None, base_url=None, api_key=None, **_kwargs):
        capability_checks.append((provider_key, model, base_url, api_key))
        return (provider_key, model, base_url, api_key) == (
            partner.provider_key,
            partner.model,
            partner.base_url,
            partner.api_key,
        )

    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", supports_images)
    selections = []

    class Runner(_RecordingPipelineActionRunner):
        async def interrupt(
            self,
            session,
            message,
            image_ids,
            file_refs,
            *,
            model_selection=None,
            event_sink=None,
            permission_resolver=None,
        ):
            selections.append(model_selection)
            return await super().interrupt(
                session,
                message,
                image_ids,
                file_refs,
                model_selection=model_selection,
                event_sink=event_sink,
                permission_resolver=permission_resolver,
            )

    runner = Runner()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    store_cached_image(
        "image-1",
        b"\x89PNG\r\n\x1a\npng-data",
        media_type="image/png",
        cwd=session.cwd,
        session_id=session.session_id,
    )
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/{}/interrupt".format(session.web_session_id),
            json={"message": "reconsider", "imageIds": ["image-1"]},
        )

    assert response.status_code == 202
    assert capability_checks == [
        (partner.provider_key, partner.model, partner.base_url, partner.api_key),
    ]
    assert len(selections) == 1
    assert selections[0].provider == partner.provider_key
    assert selections[0].model == partner.model
    assert selections[0].provider_base_url == partner.base_url
    assert selections[0].provider_api_key == partner.api_key
    assert selections[0].provider_config_frozen is True


def test_pipeline_interrupt_empty_message_cancels_running_turn(tmp_path) -> None:
    """纯停止(空消息)必须能取消正在运行的流水线回合,而不是返回 turn_busy。

    回归:此前 pipeline 分支无条件走 reserve_pipeline_action,回合持锁时返回 None → 409,
    导致「取消」完全无效。修复后空消息 + 运行中回合应直接取消 active_turn_task。
    """
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def run() -> tuple[object, bool, list[str]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(
            mode="pipeline",
            context_id="ctx-1",
            task_id="task-1",
            session_id="session-1",
        )

        started = asyncio.Event()

        async def hold_turn() -> None:
            started.set()
            await asyncio.sleep(10)

        session.active_turn_task = asyncio.create_task(hold_turn())
        await started.wait()
        app = create_app(session_manager=manager)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    f"/api/sessions/{session.session_id}/interrupt",
                    json={"message": ""},
                )
            try:
                await session.active_turn_task
            except asyncio.CancelledError:
                pass
            return (
                response,
                session.active_turn_task.cancelled(),
                [event["type"] for event in session.events.replay_after(0)],
            )
        finally:
            if not session.active_turn_task.done():
                session.active_turn_task.cancel()

    response, cancelled, event_types = asyncio.run(run())

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert cancelled is True
    assert event_types == ["interrupt.accepted"]


def _auto_approve_runner(owner_flag: bool):
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    runner = A2APipelineActionRunner.__new__(A2APipelineActionRunner)
    runner._owner = SimpleNamespace(auto_approve_permissions=owner_flag)
    return runner


def test_resolve_auto_approve_maps_bypass_and_dont_ask_modes():
    from iac_code.types.permissions import PermissionMode

    runner = _auto_approve_runner(False)
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode=PermissionMode.BYPASS_PERMISSIONS)) is True
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode=PermissionMode.DONT_ASK)) is True
    # String form (session may store the raw value) is honoured too.
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode="bypass_permissions")) is True


def test_resolve_auto_approve_denies_interactive_modes():
    from iac_code.types.permissions import PermissionMode

    runner = _auto_approve_runner(False)
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode=PermissionMode.DEFAULT)) is False
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode=PermissionMode.ACCEPT_EDITS)) is False
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode=None)) is False
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode="")) is False


def test_resolve_auto_approve_owner_flag_overrides_mode():
    from iac_code.types.permissions import PermissionMode

    runner = _auto_approve_runner(True)
    # When the runtime owner already auto-approves, every session mode approves.
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode=PermissionMode.DEFAULT)) is True
    assert runner._resolve_auto_approve(SimpleNamespace(permission_mode=None)) is True


def _resolver_runner(monkeypatch, tmp_path, captured):
    """A runner wired to persistence with a capturing executor (Issue 6)."""
    from iac_code.a2a.metrics import NoOpA2AMetrics
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.web.pipeline_actions import A2APipelineActionRunner

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(project)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))

    class CapturingExecutor:
        def __init__(self, *, task_store, permission_resolver=None, auto_approve_permissions=False, **_kwargs) -> None:
            self.task_store = task_store
            captured["permission_resolver"] = permission_resolver
            captured["auto_approve_permissions"] = auto_approve_permissions

        async def execute(self, **_kwargs) -> None:
            return None

    async def fake_snapshot(*, context_id=None, task_id=None):
        return None

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor", CapturingExecutor)
    monkeypatch.setattr("iac_code.web.pipeline_actions.load_pipeline_snapshot", fake_snapshot)

    runner = A2APipelineActionRunner.__new__(A2APipelineActionRunner)
    runner._owner = SimpleNamespace(
        model="model",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        thinking_exposure_types=(),
        auto_approve_permissions=False,
    )
    runner._task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    session = SimpleNamespace(
        session_id="session-1",
        context_id="ctx-1",
        task_id="task-1",
        cwd=str(project),
        permission_mode="default",
        pipeline_name="selling",
    )
    return runner, session


@pytest.mark.asyncio
async def test_pipeline_execute_threads_web_resolver_when_interactive(monkeypatch, tmp_path):
    # Interactive session (default mode): the web permission resolver is threaded to the
    # executor and auto-approve stays off, so tool prompts surface instead of auto-denying.
    captured: dict[str, Any] = {}
    runner, session = _resolver_runner(monkeypatch, tmp_path, captured)

    async def resolver(event):
        return True

    result = await runner.start(session, "build me a vpc", [], [], permission_resolver=resolver)

    assert result.accepted is True
    assert captured["auto_approve_permissions"] is False
    assert captured["permission_resolver"] is resolver


@pytest.mark.asyncio
async def test_pipeline_execute_uses_silent_auto_approve_for_non_interactive(monkeypatch, tmp_path):
    # Non-interactive session (bypass_permissions): keep the silent auto-approve path —
    # resolver is dropped and auto-approve is on, so no permission UI is raised.
    captured: dict[str, Any] = {}
    runner, session = _resolver_runner(monkeypatch, tmp_path, captured)
    session.permission_mode = "bypass_permissions"

    async def resolver(event):
        return True

    result = await runner.start(session, "build me a vpc", [], [], permission_resolver=resolver)

    assert result.accepted is True
    assert captured["auto_approve_permissions"] is True
    assert captured["permission_resolver"] is None
