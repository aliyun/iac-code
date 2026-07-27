from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.services.session_storage import SessionStorage


def _cleanup_module():
    try:
        from iac_code.web import cleanup
    except ModuleNotFoundError as exc:
        pytest.fail(f"iac_code.web.cleanup module is missing: {exc}")
    return cleanup


def _event(sequence: int, event_id: str) -> dict:
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "sequence": sequence,
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }


def _write_cleanup_snapshot(config_dir: Path, project: Path, session_id: str) -> None:
    persistence = A2APersistenceStore(config_dir / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(project)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed"))
    pipeline_dir = SessionStorage().session_dir(str(project), session_id) / "pipeline"
    event = _event(1, "evt-1")
    A2APipelineJournal(pipeline_dir).append(event)
    snapshot = reduce_pipeline_events([event])
    snapshot["cleanup"] = {
        "status": "started",
        "resourceCount": 1,
        "resources": [{"resourceId": "stack-1", "cleanupStatus": "DELETE_STARTED"}],
        "history": [{"eventId": "cleanup-1", "status": "started"}],
    }
    A2APipelineSnapshotStore(pipeline_dir).save(snapshot)


class _RuntimeErrorRecovery:
    async def get_state(self, **_kwargs) -> dict:
        raise RuntimeError("boom at /private/secret/path")


@pytest.mark.parametrize("status", ["pending", "running", "failed", "unreadable", "started", "in_progress"])
def test_cleanup_blocks_normal_chat_for_blocking_states(status: str) -> None:
    cleanup = _cleanup_module()

    assert cleanup.cleanup_blocks_normal_chat(status) is True


@pytest.mark.parametrize("status", ["completed", "none", "skipped", None])
def test_cleanup_does_not_block_normal_chat_for_terminal_or_absent_states(status: str | None) -> None:
    cleanup = _cleanup_module()

    assert cleanup.cleanup_blocks_normal_chat(status) is False


def test_cleanup_status_summary_normalizes_snapshot_names_and_preserves_details() -> None:
    cleanup = _cleanup_module()
    snapshot_cleanup = {
        "status": "started",
        "resourceCount": 1,
        "resources": [{"resourceId": "stack-1"}],
        "history": [{"eventId": "cleanup-1"}],
    }

    summary = cleanup.cleanup_status_summary(snapshot_cleanup)

    assert summary["status"] == "running"
    assert summary["rawStatus"] == "started"
    assert summary["blocksNormalChat"] is True
    assert summary["resourceCount"] == 1
    assert summary["resources"] == [{"resourceId": "stack-1"}]
    assert summary["history"] == [{"eventId": "cleanup-1"}]


@pytest.mark.asyncio
async def test_session_cleanup_summary_propagates_unexpected_recovery_errors() -> None:
    cleanup = _cleanup_module()
    session = SimpleNamespace(session_id="session-1", context_id="ctx-1", task_id=None)

    with pytest.raises(RuntimeError, match="boom"):
        await cleanup.session_cleanup_summary(session, recovery_service=_RuntimeErrorRecovery())


def test_session_cleanup_route_returns_latest_pipeline_cleanup_summary(monkeypatch, tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    manager = WebSessionManager(cwd=project)
    session = manager.create_session(
        cwd=str(project),
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
        session_id="session-1",
    )
    _write_cleanup_snapshot(config_dir, project, session.session_id)
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/cleanup")

    assert response.status_code == 200
    data = response.json()
    assert data["sessionId"] == session.session_id
    assert data["contextId"] == "ctx-1"
    assert data["taskId"] == "task-1"
    assert data["status"] == "running"
    assert data["rawStatus"] == "started"
    assert data["blocksNormalChat"] is True
    assert data["resourceCount"] == 1
    assert data["resources"][0]["resourceId"] == "stack-1"


def test_session_cleanup_route_reports_sanitized_unreadable_for_malformed_session_pipeline_ids(
    monkeypatch,
    tmp_path,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = WebSessionManager(cwd=project)
    session = manager.create_session(
        cwd=str(project),
        mode="pipeline",
        context_id="../ctx",
        task_id="task-1",
        session_id="session-1",
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/cleanup")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "unreadable"
    assert data["blocksNormalChat"] is True
    assert data["contextId"] is None
    assert data["taskId"] is None
    assert "../ctx" not in response.text


def test_session_cleanup_route_reports_unreadable_for_recovery_errors(monkeypatch, tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    manager = WebSessionManager(cwd=project)
    session = manager.create_session(
        cwd=str(project),
        mode="pipeline",
        context_id="ctx-missing",
        task_id="task-missing",
        session_id="session-1",
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/cleanup")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "unreadable"
    assert data["blocksNormalChat"] is True
    assert "ctx-missing" not in data.get("message", "")
