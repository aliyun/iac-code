import asyncio
import builtins
import json
import threading
from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
)
from a2a.utils.errors import TaskNotCancelableError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict
from starlette.testclient import TestClient

from iac_code import __version__
from iac_code.a2a.app import (
    A2AAuthMiddleware,
    _A2AIdleShutdownController,
    _serve_async_transport,
    _supported_interfaces,
    create_app,
    resolve_api_key,
    resolve_basic_credentials,
    resolve_token,
    run_server,
)
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_executor import recoverable_task_id_from_sidecar
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.mcp.errors import MCPNeedsAuthError
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    PermissionWaitPolicy,
    build_permission_checkpoint,
)
from iac_code.services.session_backup import (
    BackupReason,
    SessionBackupBlocked,
    SessionBackupNotReadyError,
    SessionBackupService,
)
from iac_code.services.session_backup_state import NORMAL_HANDOFF_PROOF_KEY, BackupPublicationProof
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import TextDeltaEvent, ToolResultEvent

from .fakes import FakeAgentLoop, FakeRuntime


def test_resolve_token_prefers_cli_value(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_HTTP_TOKEN", "env-token")
    assert resolve_token("cli-token") == "cli-token"


def test_resolve_token_uses_environment(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_HTTP_TOKEN", "env-token")
    assert resolve_token(None) == "env-token"


def test_resolve_basic_credentials_uses_cli_values(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_BASIC_USERNAME", "env-user")
    monkeypatch.setenv("IACCODE_A2A_BASIC_PASSWORD", "env-pass")

    assert resolve_basic_credentials("cli-user", "cli-pass") == ("cli-user", "cli-pass")


def test_resolve_basic_credentials_uses_environment(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_BASIC_USERNAME", "env-user")
    monkeypatch.setenv("IACCODE_A2A_BASIC_PASSWORD", "env-pass")

    assert resolve_basic_credentials(None, None) == ("env-user", "env-pass")


def test_resolve_basic_credentials_requires_pair(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_BASIC_USERNAME", "env-user")
    monkeypatch.delenv("IACCODE_A2A_BASIC_PASSWORD", raising=False)

    assert resolve_basic_credentials(None, None) is None


def test_resolve_api_key_prefers_cli_value(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_API_KEY", "env-key")

    assert resolve_api_key("cli-key") == "cli-key"


@pytest.mark.asyncio
async def test_idle_shutdown_waits_for_active_work() -> None:
    class Store:
        active = True

        async def has_active_work(self) -> bool:
            return self.active

    store = Store()
    shutdown = asyncio.Event()
    controller = _A2AIdleShutdownController(0.05, shutdown.set)
    controller.touch()
    monitor = asyncio.create_task(controller.monitor(store))

    await asyncio.sleep(0.12)
    assert not shutdown.is_set()
    store.active = False
    await asyncio.wait_for(shutdown.wait(), timeout=0.5)
    await monitor


def test_resolve_api_key_uses_environment(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_API_KEY", "env-key")

    assert resolve_api_key(None) == "env-key"


def test_health_route() -> None:
    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "version": __version__, "mode": "normal"}


def test_readiness_route_is_authenticated_and_returns_non_secret_status(monkeypatch) -> None:
    expected = {
        "schemaVersion": 1,
        "llm": {"ready": True, "provider": "openai", "model": "gpt-5.6", "missing": []},
        "cloud": {"ready": False, "provider": "aliyun", "missing": ["credentials"]},
    }
    monkeypatch.setattr("iac_code.a2a.app.configuration_readiness", lambda *, model: expected)
    app = create_app(host="127.0.0.1", port=41242, token="runtime-token", model="gpt-5.6")

    with TestClient(app) as client:
        unauthorized = client.get("/iac-code/readiness")
        response = client.get("/iac-code/readiness", headers={"Authorization": "Bearer runtime-token"})

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == expected


def test_ensure_session_restored_is_authenticated_idempotent_and_restores_backup(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    backup_root = tmp_path / "backup"
    workspace_root = tmp_path / "workspace"
    cwd = workspace_root / "session"
    cwd.mkdir(parents=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(workspace_root))

    session_id = "session-restore"
    backup_storage = SessionStorage(projects_dir=backup_root / "projects")
    backup_session_dir = backup_storage.session_dir(str(cwd), session_id)
    write_session_metadata(
        backup_session_dir,
        SessionMetadata(session_id=session_id, cwd=str(cwd), layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    SessionBackupService(session_storage=backup_storage).initialize_session(str(cwd), session_id)
    pipeline_dir = backup_session_dir / "a2a" / "pipeline"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "a2a-events.jsonl").write_text('{"sequence":1}\n', encoding="utf-8")
    (pipeline_dir / "a2a-snapshot.json").write_text('{"lastSequence":1}\n', encoding="utf-8")

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token="runtime-token",
        model="qwen3.6-plus",
        persistence_dir=config_dir / "a2a",
    )
    payload = {"cwd": str(cwd), "sessionId": session_id}
    headers = {"Authorization": "Bearer runtime-token"}
    with TestClient(app) as client:
        unauthorized = client.post("/iac-code/session/ensure-restored", json=payload)
        restored = client.post("/iac-code/session/ensure-restored", json=payload, headers=headers)
        current = client.post("/iac-code/session/ensure-restored", json=payload, headers=headers)
        missing = client.post(
            "/iac-code/session/ensure-restored",
            json={"cwd": str(cwd), "sessionId": "missing-session"},
            headers=headers,
        )

    restored_pipeline_dir = SessionStorage().session_dir(str(cwd), session_id) / "a2a" / "pipeline"
    assert unauthorized.status_code == 401
    assert restored.status_code == 200
    assert restored.json() == {"status": "restored"}
    assert current.status_code == 200
    assert current.json() == {"status": "current"}
    assert missing.status_code == 404
    assert missing.json() == {"status": "not_found"}
    assert (restored_pipeline_dir / "a2a-events.jsonl").read_text(encoding="utf-8") == '{"sequence":1}\n'
    assert (restored_pipeline_dir / "a2a-snapshot.json").read_text(encoding="utf-8") == '{"lastSequence":1}\n'


def test_ensure_session_restored_for_task_returns_retryable_generation_error(monkeypatch, tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    cwd = workspace_root / "session"
    cwd.mkdir(parents=True)
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(workspace_root))
    calls: list[tuple[str, str, str | None]] = []

    async def not_ready(self, *, cwd, session_id, task_id=None):
        del self
        calls.append((cwd, session_id, task_id))
        raise SessionBackupNotReadyError(
            minimum_generation=2,
            local_generation=1,
            shared_generation=1,
        )

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2AExecutor.ensure_session_restored", not_ready)
    app = create_app(host="127.0.0.1", port=41242, token="runtime-token", model="qwen3.6-plus")

    with TestClient(app) as client:
        response = client.post(
            "/iac-code/session/ensure-restored",
            json={"cwd": str(cwd), "sessionId": "session-1", "taskId": "task-1"},
            headers={"Authorization": "Bearer runtime-token"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "SESSION_BACKUP_NOT_READY",
            "message": "Session backup is still synchronizing. Retry after 3 seconds.",
            "retryable": True,
        }
    }
    assert calls == [(str(cwd), "session-1", "task-1")]


@pytest.mark.asyncio
async def test_a2a_task_store_writes_session_snapshots_and_global_indexes(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(cwd),
        runtime_factory=lambda _session_id: FakeRuntime(),
    )
    task = await store.get_or_create_task(task_id="task-1", context_id=ctx.context_id)
    task.state = "working"
    store.mirror_task(task)

    session_dir = SessionStorage().session_dir(str(cwd), ctx.session_id)
    session_task = json.loads((session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
    session_context = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))

    assert session_task["task_id"] == "task-1"
    assert session_task["context_id"] == "ctx-1"
    assert session_context["context_id"] == "ctx-1"
    assert session_context["session_id"] == ctx.session_id
    assert (config_dir / "a2a" / "tasks" / "task-1.json").exists()
    assert (config_dir / "a2a" / "contexts" / "ctx-1.json").exists()


def test_run_server_reports_aligned_missing_uvicorn_hint(monkeypatch, tmp_path) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("No module named 'uvicorn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr("iac_code.a2a.app.create_app", lambda **kwargs: object())

    with pytest.raises(
        RuntimeError,
        match=r"A2A server dependencies are missing\. Install with: pip install 'iac-code\[a2a\]'",
    ):
        run_server(
            host="127.0.0.1",
            port=41242,
            token=None,
            model="qwen3.6-plus",
            basic_username=None,
            basic_password=None,
            api_key=None,
            api_key_header="X-API-Key",
            persistence_dir=tmp_path / "a2a",
        )


def test_pipeline_state_endpoint_requires_context_id(tmp_path) -> None:
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a",
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state")

    assert response.status_code == 400
    assert response.json() == {"error": "contextId or taskId is required"}


def test_pipeline_state_endpoint_returns_404_for_missing_context(tmp_path) -> None:
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a",
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=missing")

    assert response.status_code == 404
    assert response.json() == {"error": "A2A context not found"}


def test_pipeline_state_endpoint_rejects_unicode_digit_after_sequence(tmp_path) -> None:
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a",
    )

    with TestClient(app) as client:
        response = client.get(
            "/iac-code/pipeline/state",
            params={"contextId": "missing", "afterSequence": "²"},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "afterSequence must be a non-negative integer"}


def test_pipeline_state_endpoint_rejects_overlong_after_sequence(tmp_path) -> None:
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a",
    )

    with TestClient(app) as client:
        response = client.get(
            "/iac-code/pipeline/state",
            params={"contextId": "missing", "afterSequence": "9" * 5000},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "afterSequence must be a non-negative integer"}


def test_pipeline_state_endpoint_rejects_after_sequence_above_max_length(tmp_path) -> None:
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a",
    )

    with TestClient(app) as client:
        response = client.get(
            "/iac-code/pipeline/state",
            params={"contextId": "missing", "afterSequence": "9" * 21},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "afterSequence must be a non-negative integer"}


def test_pipeline_state_endpoint_returns_recovery_state(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(_pipeline_event(1, "evt-1"))
    journal.append(_pipeline_event(2, "evt-2"))
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([_pipeline_event(1, "evt-1")]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-1&afterSequence=1")

    assert response.status_code == 200
    data = response.json()
    assert data["snapshot"]["lastSequence"] == 1
    assert [event["eventId"] for event in data["events"]] == ["evt-2"]


@pytest.mark.parametrize(("safe_mode", "expected_path"), [("1", "[PATH]"), ("0", "raw")])
def test_pipeline_state_endpoint_projects_recovery_state_at_a2a_boundary(
    monkeypatch, tmp_path, safe_mode, expected_path
) -> None:
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", safe_mode)
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    raw_path = str(tmp_path / "private" / "result.json")
    event["data"] = {"path": raw_path, "password": "real-secret"}
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-1&afterSequence=0")

    data = response.json()
    assert data["events"][0]["data"]["password"] == "real-secret"
    if expected_path == "[PATH]":
        assert data["events"][0]["data"]["path"] == "[PATH]"
    else:
        assert data["events"][0]["data"]["path"] == raw_path


@pytest.mark.parametrize(("safe_mode", "expected_path"), [("1", "[PATH]"), ("0", "raw")])
def test_pipeline_state_endpoint_projects_value_error_without_changing_schema(
    monkeypatch, tmp_path, safe_mode, expected_path
) -> None:
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", safe_mode)
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    raw_path = str(tmp_path / "private" / "snapshot.json")

    async def fail_get_state(self, **kwargs):
        raise ValueError(f"recovery failed token=real-secret at {raw_path}")

    monkeypatch.setattr("iac_code.a2a.pipeline_recovery.A2APipelineRecoveryService.get_state", fail_get_state)
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-1")

    assert response.status_code == 404
    assert set(response.json()) == {"error"}
    assert "real-secret" in response.json()["error"]
    if expected_path == "[PATH]":
        assert "[PATH]" in response.json()["error"]
        assert raw_path not in response.json()["error"]
    else:
        assert raw_path in response.json()["error"]


def test_pipeline_state_endpoint_repairs_pending_backup_snapshot_after_committed_ack(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="canceled"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    started = _pipeline_event(1, "evt-started")
    pending_terminal = {
        **_pipeline_event(2, "evt-pending-terminal"),
        "eventType": "pipeline_canceled",
        "status": "canceled",
        "visibility": "pending_backup",
        "data": {"source": "executor"},
    }
    pending_handoff = {
        **_pipeline_event(3, "evt-pending-handoff"),
        "eventType": "pipeline_handoff_ready",
        "status": "canceled",
        "visibility": "pending_backup",
        "data": {
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": "[Pipeline Handoff Context]\nOutcome: canceled",
        },
    }
    committed_terminal = {
        **_pipeline_event(4, "evt-committed-terminal"),
        "eventType": "pipeline_canceled",
        "status": "canceled",
        "visibility": "committed",
        "data": {"source": "executor"},
    }
    committed_handoff = {
        **_pipeline_event(5, "evt-committed-handoff"),
        "eventType": "pipeline_handoff_ready",
        "status": "canceled",
        "visibility": "committed",
        "data": {
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": "[Pipeline Handoff Context]\nOutcome: canceled",
        },
    }
    terminal_ack = {
        **_pipeline_event(6, "evt-terminal-ack"),
        "eventType": "backup_committed",
        "data": {
            "committedEventId": committed_terminal["eventId"],
            "committedEventType": "pipeline_canceled",
            "committedSequence": committed_terminal["sequence"],
        },
    }
    terminal_ack.pop("status", None)
    handoff_ack = {
        **_pipeline_event(7, "evt-handoff-ack"),
        "eventType": "backup_committed",
        "data": {
            "committedEventId": committed_handoff["eventId"],
            "committedEventType": "pipeline_handoff_ready",
            "committedSequence": committed_handoff["sequence"],
        },
    }
    handoff_ack.pop("status", None)
    A2APipelineJournal(pipeline_dir).append_many(
        [
            started,
            pending_terminal,
            pending_handoff,
            committed_terminal,
            committed_handoff,
            terminal_ack,
            handoff_ack,
        ],
        durable=True,
    )
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([started, pending_terminal, pending_handoff, terminal_ack, handoff_ack]))
    assert snapshot_store.load()["normalHandoff"] is None
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?taskId=task-1")

    assert response.status_code == 200
    snapshot = response.json()["snapshot"]
    assert snapshot["status"] == "canceled"
    assert snapshot["pendingTerminal"] is None
    assert snapshot["pendingNormalHandoff"] is None
    assert snapshot["normalHandoff"]["summary"] == "[Pipeline Handoff Context]\nOutcome: canceled"
    assert snapshot_store.load()["normalHandoff"]["outcome"] == "canceled"


def test_pipeline_state_endpoint_resolves_recovery_state_from_task_id(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(_pipeline_event(1, "evt-1"))
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([_pipeline_event(1, "evt-1")]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?taskId=task-1")

    assert response.status_code == 200
    assert response.json()["snapshot"]["contextId"] == "ctx-1"


def test_pipeline_state_endpoint_allows_task_id_for_matching_authenticated_owner(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed", owner="bearer"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token="secret",
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get(
            "/iac-code/pipeline/state?taskId=task-1",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    assert response.json()["snapshot"]["taskId"] == "task-1"


def test_pipeline_state_endpoint_hides_task_id_from_wrong_authenticated_owner(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed", owner="bearer"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        basic_username="alice",
        basic_password="pass",
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get(
            "/iac-code/pipeline/state?taskId=task-1",
            headers={"Authorization": "Basic " + b64encode(b"alice:pass").decode("ascii")},
        )

    assert response.status_code == 404
    assert response.json() == {"error": "A2A pipeline state not found"}


def test_pipeline_state_endpoint_hides_context_only_state_when_owner_cannot_be_verified(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token="secret",
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get(
            "/iac-code/pipeline/state?contextId=ctx-1",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 404
    assert response.json() == {"error": "A2A pipeline state not found"}


def test_pipeline_state_endpoint_binds_context_only_owner_check_to_context_id(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed", owner="bearer"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-2", session_id="session-2", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-2") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    event["contextId"] = "ctx-2"
    event["pipelineRunId"] = "ctx-2"
    event["taskId"] = "task-1"
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token="secret",
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get(
            "/iac-code/pipeline/state?contextId=ctx-2",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 404
    assert response.json() == {"error": "A2A pipeline state not found"}


def test_pipeline_state_endpoint_returns_404_when_task_id_state_belongs_to_different_task(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_task(A2ATaskSnapshot(task_id="task-2", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?taskId=task-2")

    assert response.status_code == 404
    assert response.json() == {"error": "A2A pipeline state not found"}


def test_pipeline_state_endpoint_returns_404_for_context_task_mismatch(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-2", session_id="session-2", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-2") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    event["taskId"] = "task-2"
    event["contextId"] = "ctx-2"
    event["pipelineRunId"] = "ctx-2"
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-2&taskId=task-1")

    assert response.status_code == 404
    assert response.json() == {"error": "A2A task/context mismatch"}


def test_pipeline_state_endpoint_returns_404_for_context_without_pipeline_state(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-empty", session_id="session-empty", cwd=str(tmp_path)))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-empty")

    assert response.status_code == 404
    assert response.json() == {"error": "A2A pipeline state not found"}


def test_pipeline_state_endpoint_sanitizes_non_finite_floats(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _pipeline_cost_event(1, "evt-1", float("nan"))
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-1&afterSequence=0")

    assert response.status_code == 200
    data = response.json()
    assert data["snapshot"]["display"]["candidateDetails"][0]["totalMonthlyCost"] is None
    assert data["events"][0]["data"]["totalMonthlyCost"] is None


def _pipeline_event(sequence: int, event_id: str) -> dict:
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


def _sse_json_events(body: str) -> list[dict]:
    events: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line.removeprefix("data: ")))
    return events


def _pipeline_pending_ask_event() -> dict:
    event = _pipeline_event(1, "evt-ask")
    event["eventType"] = "input_required"
    event["scope"] = "step"
    event["status"] = "input_required"
    event["step"] = {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1}
    event["data"] = {"kind": "ask_user_question", "toolUseId": "ask-1"}
    event["input"] = {
        "inputId": "ask-ask-1",
        "kind": "ask_user_question",
        "toolUseId": "ask-1",
        "question": "请选择部署目标",
        "options": [{"id": "nginx", "label": "Nginx 网站"}],
        "allowFreeText": True,
    }
    return event


def _pipeline_cost_event(sequence: int, event_id: str, total_monthly_cost: float) -> dict:
    event = _pipeline_event(sequence, event_id)
    event["eventType"] = "candidate_detail_shown"
    event["scope"] = "candidate"
    event["candidate"] = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    event["data"] = {
        "detailId": "detail-1",
        "summary": "single ecs",
        "totalMonthlyCost": total_monthly_cost,
    }
    return event


def test_agent_card_route() -> None:
    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "iac-code"
    assert data["url"] == "http://127.0.0.1:41242/"
    assert data["preferredTransport"] == "JSONRPC"
    assert data["protocolVersion"] == "1.0"
    assert data["supportedInterfaces"][0]["protocolVersion"] == "1.0"


def test_agent_card_route_sets_cache_headers_and_supports_revalidation() -> None:
    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.get("/.well-known/agent-card.json")
    etag = response.headers["etag"]

    assert response.headers["cache-control"] == "public, max-age=60"
    assert etag.startswith('"sha256-')
    assert response.headers["last-modified"]

    revalidated = client.get("/.well-known/agent-card.json", headers={"If-None-Match": etag})

    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag


@pytest.mark.parametrize(
    ("app_kwargs", "headers", "expected_status"),
    [
        ({"token": "secret"}, {"Authorization": "Bearer wrong"}, 401),
        (
            {"token": None, "basic_username": "iac", "basic_password": "secret"},
            {"Authorization": f"Basic {b64encode(b'iac:secret').decode()}"},
            200,
        ),
        (
            {"token": None, "basic_username": "iac", "basic_password": "secret"},
            {"Authorization": f"Basic {b64encode(b'iac:wrong').decode()}"},
            401,
        ),
        ({"token": None, "api_key": "secret-key"}, {"X-API-Key": "secret-key"}, 200),
        ({"token": None, "api_key": "secret-key"}, {"X-API-Key": "wrong"}, 401),
    ],
)
def test_agent_card_auth_schemes(app_kwargs, headers, expected_status) -> None:
    app = create_app(host="127.0.0.1", port=41242, model="qwen3.6-plus", **app_kwargs)
    client = TestClient(app)

    response = client.get("/.well-known/agent-card.json", headers=headers)

    assert response.status_code == expected_status


def test_basic_auth_rejects_empty_decoded_username_or_password() -> None:
    middleware = A2AAuthMiddleware(
        app=None,
        token=None,
        basic_username="",
        basic_password="secret",
        api_key=None,
        api_key_header="X-API-Key",
    )
    empty_username = b64encode(b":secret").decode()

    assert middleware._valid_basic_auth(f"Basic {empty_username}") is False


def test_api_key_auth_with_custom_header() -> None:
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        api_key="secret-key",
        api_key_header="X-Custom-Key",
    )
    client = TestClient(app)

    accepted = client.get("/.well-known/agent-card.json", headers={"X-Custom-Key": "secret-key"})
    assert accepted.status_code == 200

    rejected_default = client.get("/.well-known/agent-card.json", headers={"X-API-Key": "secret-key"})
    assert rejected_default.status_code == 401

    rejected_wrong = client.get("/.well-known/agent-card.json", headers={"X-Custom-Key": "wrong"})
    assert rejected_wrong.status_code == 401


def test_supported_interfaces_preserves_explicit_zero_grpc_port() -> None:
    interfaces = _supported_interfaces(
        transport="grpc",
        host="127.0.0.1",
        port=41242,
        socket_path=None,
        ws_path="/a2a",
        grpc_host=None,
        grpc_port=0,
        redis_url=None,
        request_stream="requests",
        response_stream="responses",
        consumer_group="iac-code",
    )

    assert interfaces == [{"url": "grpc://127.0.0.1:0", "protocolBinding": "grpc", "protocolVersion": "1.0"}]


def test_supported_interfaces_advertises_grpc_jsonrpc_compatibility_binding() -> None:
    interfaces = _supported_interfaces(
        transport="grpc-jsonrpc",
        host="127.0.0.1",
        port=41242,
        socket_path=None,
        ws_path="/a2a",
        grpc_host=None,
        grpc_port=0,
        redis_url=None,
        request_stream="requests",
        response_stream="responses",
        consumer_group="iac-code",
    )

    assert interfaces == [
        {"url": "grpc-jsonrpc://127.0.0.1:0", "protocolBinding": "grpc-jsonrpc", "protocolVersion": "1.0"}
    ]


def test_supported_interfaces_advertises_jsonrpc_and_rest_for_http_transport() -> None:
    interfaces = _supported_interfaces(
        transport="http",
        host="127.0.0.1",
        port=41242,
        socket_path=None,
        ws_path="/a2a",
        grpc_host=None,
        grpc_port=None,
        redis_url=None,
        request_stream="requests",
        response_stream="responses",
        consumer_group="iac-code",
    )

    assert interfaces == [
        {"url": "http://127.0.0.1:41242/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": "http://127.0.0.1:41242", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
    ]


def test_auth_allows_any_configured_scheme() -> None:
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token="bearer-secret",
        model="qwen3.6-plus",
        api_key="api-secret",
    )
    client = TestClient(app)

    response = client.get("/.well-known/agent-card.json", headers={"X-API-Key": "api-secret"})

    assert response.status_code == 200


def test_send_message_through_sdk_route(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hello from route")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        },
    )

    data = response.json()
    assert "error" not in data
    assert data["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert loop.prompts == ["hello"]


@pytest.mark.parametrize("version_header", ["0.3", "0.3.0", "1.0", None])
def test_send_message_through_v03_route(monkeypatch, tmp_path, version_header: str | None) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hello from v03 route")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    headers = {"A2A-Version": version_header} if version_header else {}
    response = client.post(
        "/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello v03"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        },
    )

    data = response.json()
    assert "error" not in data
    assert data["result"]["status"]["state"] == "input-required"
    assert loop.prompts == ["hello v03"]


def test_streaming_v03_method_with_v10_header_returns_sse(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hello from mixed streaming route")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    with client.stream(
        "POST",
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/stream",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello mixed"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text"]},
            },
        },
    ) as response:
        body = response.read().decode()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "hello from mixed streaming route" in body
    assert loop.prompts == ["hello mixed"]


def test_streaming_v03_active_sidecar_mismatch_preserves_recoverable_error_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-owner", context_id="ctx-1", state="working"))
    persistence.save_task(A2ATaskSnapshot(task_id="task-new", context_id="ctx-1", state="input-required"))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), session_id)

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    owner_event = _pipeline_event(1, "evt-owner")
    owner_event["taskId"] = "task-owner"
    A2APipelineJournal(pipeline_dir).append(owner_event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([owner_event]))

    class RunningPipeline:
        pipeline_name = "selling"
        sidecar_status = "running"
        handoff_enabled = False

        def __init__(self) -> None:
            self.session = SimpleNamespace(
                session_dir=SessionStorage().session_dir(str(tmp_path), session_id) / "pipeline"
            )

        async def run(self, prompt: str):  # pragma: no cover - regression asserts this is not reached
            yield TextDeltaEvent(text=f"unexpected {prompt}")

        def clear_sidecar(self) -> None:  # pragma: no cover - regression asserts this is not reached
            raise AssertionError("active sidecar should not be cleared")

    fake_runtime = SimpleNamespace(provider_manager=object(), tool_registry=object())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: fake_runtime)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: RunningPipeline())

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": "msg-new",
                        "taskId": "task-new",
                        "contextId": "ctx-1",
                        "role": "user",
                        "parts": [{"kind": "text", "text": "new request"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ) as response:
            body = response.read().decode()

    events = _sse_json_events(body)
    assert response.status_code == 200
    assert events
    error = events[-1]["error"]
    assert error["code"] == -32602
    assert error["data"] == {
        "recoverableTaskId": "task-owner",
        "contextId": "ctx-1",
        "sidecarStatus": "running",
    }


def test_pipeline_streaming_starts_with_task_before_status_update(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class StreamingPipeline:
        pipeline_name = "selling"
        sidecar_status = None

        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.session = SimpleNamespace(session_dir=tmp_path / "pipeline-sidecar")
            self.handoff_enabled = False

        async def run(self, prompt: str):
            self.prompts.append(prompt)
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_STARTED,
                step_id=None,
                timestamp=1717821600.0,
                data={"total_steps": 1, "step_names": ["intent_parsing"]},
            )
            yield TextDeltaEvent(text="pipeline streaming output")

        def should_switch_to_normal(self, data: dict) -> bool:  # noqa: ARG002
            return False

    fake_pipeline = StreamingPipeline()
    fake_runtime = SimpleNamespace(provider_manager=object(), tool_registry=object())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: fake_runtime)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "选择一个已有vpc，创建一个vswitch"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ) as response:
            body = response.read().decode()

    assert response.status_code == 200
    assert "Agent should enqueue Task before TaskStatusUpdateEvent event" not in body
    assert "pipeline streaming output" in body
    assert fake_pipeline.prompts == ["选择一个已有vpc，创建一个vswitch"]


def test_pipeline_streaming_workspace_error_returns_request_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(allowed))

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "选择一个已有vpc，创建一个vswitch"}],
                        "metadata": {"iac_code": {"cwd": str(outside)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ) as response:
            body = response.read().decode()

    assert response.status_code == 200
    assert "Agent should enqueue Task before TaskStatusUpdateEvent event" not in body
    data = response.json()
    assert data["error"]["code"] == -32602
    assert data["error"]["message"] == "Invalid A2A workspace metadata."
    assert data["error"]["data"][0]["reason"] == "INVALID_PARAMS"


def test_follow_up_message_through_sdk_route_updates_existing_task(monkeypatch, tmp_path) -> None:
    class EchoAgentLoop:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run_streaming(self, prompt: str):
            self.prompts.append(prompt)
            yield TextDeltaEvent(text=f"turn-{len(self.prompts)}:{prompt}")

    loop = EchoAgentLoop()
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    with TestClient(app) as client:
        first = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )
        first_data = first.json()
        task = first_data["result"]["task"]

        second = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-2",
                        "taskId": task["id"],
                        "contextId": task["contextId"],
                        "role": "ROLE_USER",
                        "parts": [{"text": "follow up"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )

        second_data = second.json()
    assert "error" not in second_data
    assert loop.prompts == ["hello", "follow up"]
    assert "turn-2:follow up" in json.dumps(second_data)


def test_follow_up_message_with_context_id_continues_after_failed_task(monkeypatch, tmp_path) -> None:
    class FailThenEchoAgentLoop:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run_streaming(self, prompt: str):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                raise RuntimeError("first task failed")
                yield TextDeltaEvent(text="never")
            yield TextDeltaEvent(text="recovered")

    loop = FailThenEchoAgentLoop()
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    with TestClient(app) as client:
        first = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "fail first"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )
        first_data = first.json()
        failed_task = first_data["result"]["task"]
        assert failed_task["status"]["state"] == "TASK_STATE_FAILED"

        second = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-2",
                        "contextId": failed_task["contextId"],
                        "role": "ROLE_USER",
                        "parts": [{"text": "continue"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )
        second_data = second.json()

    assert "error" not in second_data
    recovered_task = second_data["result"]["task"]
    assert recovered_task["id"] != failed_task["id"]
    assert recovered_task["contextId"] == failed_task["contextId"]
    assert recovered_task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "recovered" in json.dumps(recovered_task)
    assert loop.prompts == ["fail first", "continue"]


def test_get_task_applies_history_length_without_mutating_stored_history(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="history chunk")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    with TestClient(app) as client:
        sent = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ).json()
        task_id = sent["result"]["task"]["id"]

        trimmed = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "GetTask",
                "params": {"id": task_id, "historyLength": 0},
            },
        ).json()
        full = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "3",
                "method": "GetTask",
                "params": {"id": task_id},
            },
        ).json()

    assert "history" not in trimmed["result"]
    assert full["result"]["history"]


def test_send_message_applies_history_length_to_returned_task(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="history chunk")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"], "historyLength": 0},
            },
        },
    )

    assert "history" not in response.json()["result"]["task"]


def test_send_message_accepts_data_part_as_json_prompt(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"data": {"template": "value"}, "mediaType": "application/json"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        },
    )

    data = response.json()
    assert "error" not in data
    assert loop.prompts == ['{"template":"value"}']


def test_send_message_accepts_file_url_part_from_workspace(monkeypatch, tmp_path) -> None:
    source = tmp_path / "template.yaml"
    source.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"url": source.as_uri(), "mediaType": "text/plain"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        },
    )

    assert "error" not in response.json()
    assert loop.prompts == ["ROSTemplateFormatVersion: '2015-09-01'\n"]


def test_send_message_stores_standard_artifact_update_in_task(monkeypatch, tmp_path) -> None:
    result = {"artifact": {"filename": "result.txt", "mediaType": "text/plain", "content": "hello artifact"}}
    loop = FakeAgentLoop(
        [
            TextDeltaEvent(text="done"),
            ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        artifact_dir=tmp_path / "artifacts",
    )
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        },
    )

    task = response.json()["result"]["task"]
    artifact = task["artifacts"][0]
    assert artifact["name"] == "result.txt"
    assert artifact["parts"][0]["url"].startswith("iac-code-artifact://")
    assert artifact["parts"][0]["mediaType"] == "text/plain"
    assert "file://" not in str(artifact)
    assert str(tmp_path) not in str(artifact)
    assert (tmp_path / "artifacts" / artifact["artifactId"] / "result.txt").read_text(encoding="utf-8") == (
        "hello artifact"
    )


def test_send_message_stores_binary_artifact_update_in_task(monkeypatch, tmp_path) -> None:
    result = {
        "artifact": {
            "filename": "diagram.png",
            "mediaType": "image/png",
            "bytes": "iVBORw0KGgppbWFnZQ==",
        }
    }
    loop = FakeAgentLoop(
        [
            TextDeltaEvent(text="done"),
            ToolResultEvent(tool_use_id="tool-1", tool_name="draw", result=result, is_error=False),
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        artifact_dir=tmp_path / "artifacts",
    )
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain", "image/png"]},
            },
        },
    )

    task = response.json()["result"]["task"]
    artifact = task["artifacts"][0]
    assert artifact["name"] == "diagram.png"
    assert artifact["parts"][0]["url"].startswith("iac-code-artifact://")
    assert artifact["parts"][0]["mediaType"] == "image/png"
    assert "file://" not in str(artifact)
    assert str(tmp_path) not in str(artifact)
    artifact_path = tmp_path / "artifacts" / artifact["artifactId"] / "diagram.png"
    assert artifact_path.read_bytes() == b"\x89PNG\r\n\x1a\nimage"


def test_required_a2a_extension_must_be_requested(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="unused")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        agent_extensions=[
            {"uri": "urn:iac-code:test-required", "description": "test required extension", "required": True}
        ],
    )
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        },
    )

    data = response.json()
    assert "result" not in data
    assert data["error"]["message"] == "Required A2A extensions were not requested: urn:iac-code:test-required"
    assert loop.prompts == []


def test_requested_required_a2a_extension_allows_message(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        agent_extensions=[
            {"uri": "urn:iac-code:test-required", "description": "test required extension", "required": True}
        ],
    )
    client = TestClient(app)

    response = client.post(
        "/",
        headers={"A2A-Version": "1.0", "A2A-Extensions": "urn:iac-code:test-required"},
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        },
    )

    data = response.json()
    assert "error" not in data
    assert loop.prompts == ["hello"]


def test_push_notification_config_methods_round_trip(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="done")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "state",
        push_notifications=True,
    )
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()
        sent = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ).json()
        task_id = sent["result"]["task"]["id"]
        created = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "CreateTaskPushNotificationConfig",
                "params": {
                    "taskId": task_id,
                    "id": "cfg-1",
                    "url": "https://callback.example/a2a",
                    "token": "token-1",
                    "authentication": {"scheme": "bearer", "credentials": "secret"},
                },
            },
        ).json()
        listed = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "3",
                "method": "ListTaskPushNotificationConfigs",
                "params": {"taskId": task_id, "pageSize": 1},
            },
        ).json()
        fetched = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "4",
                "method": "GetTaskPushNotificationConfig",
                "params": {"taskId": task_id, "id": "cfg-1"},
            },
        ).json()
        deleted = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "5",
                "method": "DeleteTaskPushNotificationConfig",
                "params": {"taskId": task_id, "id": "cfg-1"},
            },
        ).json()

    assert card["capabilities"]["pushNotifications"] is True
    assert created["result"]["id"] == "cfg-1"
    assert created["result"]["authentication"]["scheme"] == "bearer"
    assert listed["result"]["configs"][0]["id"] == "cfg-1"
    assert fetched["result"]["url"] == "https://callback.example/a2a"
    assert deleted["result"] is None


def test_push_notification_config_rejects_private_callback_url(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="done")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "state",
        push_notifications=True,
    )
    with TestClient(app) as client:
        sent = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ).json()
        task_id = sent["result"]["task"]["id"]
        rejected = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "CreateTaskPushNotificationConfig",
                "params": {"taskId": task_id, "id": "cfg-1", "url": "http://127.0.0.1:9999/a2a"},
            },
        ).json()

    assert "result" not in rejected
    assert "private" in rejected["error"]["message"]


def test_get_extended_agent_card_returns_private_card() -> None:
    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    public_card = client.get("/.well-known/agent-card.json").json()
    extended = client.post(
        "/",
        headers={"A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": "1", "method": "GetExtendedAgentCard", "params": {}},
    ).json()

    assert public_card["capabilities"]["extendedAgentCard"] is True
    assert extended["result"]["skills"][-1]["id"] == "iac_code_runtime_details"


def test_cancel_non_running_task_returns_standard_jsonrpc_error(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="done")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    with TestClient(app) as client:
        sent = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ).json()
        task_id = sent["result"]["task"]["id"]

        canceled = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={"jsonrpc": "2.0", "id": "2", "method": "CancelTask", "params": {"id": task_id}},
        ).json()

    assert "result" not in canceled
    assert canceled["error"]["message"] == "Task cannot be canceled"


def test_persisted_task_get_and_cancel_work_with_bearer_auth_after_restart(tmp_path: Path) -> None:
    persistence_dir = tmp_path / "a2a"
    A2APersistenceStore(persistence_dir).save_task(
        A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working", owner="bearer")
    )
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token="secret",
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        get_result = client.post(
            "/",
            headers={"A2A-Version": "1.0", "Authorization": "Bearer secret"},
            json={"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": "task-1"}},
        ).json()
        cancel_result = client.post(
            "/",
            headers={"A2A-Version": "1.0", "Authorization": "Bearer secret"},
            json={"jsonrpc": "2.0", "id": "2", "method": "CancelTask", "params": {"id": "task-1"}},
        ).json()

    assert get_result["result"]["id"] == "task-1"
    assert get_result["result"]["contextId"] == "ctx-1"
    assert "result" not in cancel_result
    assert cancel_result["error"]["message"] == "Task cannot be canceled"


def test_send_message_routes_context_only_pending_pipeline_input_after_restart(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), session_id)

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    class WaitingAskPipeline:
        pipeline_name = "selling"
        sidecar_status = "waiting_input"

        def __init__(self) -> None:
            self.ask_answers: list[dict[str, str]] = []
            self.run_prompts: list[str] = []
            self.resume_prompts: list[str] = []
            self.clear_sidecar_calls = 0

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="fresh pipeline")

        async def resume(self, prompt: str):
            self.resume_prompts.append(prompt)
            yield TextDeltaEvent(text="resumed pipeline")

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            yield TextDeltaEvent(text="nginx selected")

        def clear_sidecar(self) -> None:
            self.clear_sidecar_calls += 1

    fake_pipeline = WaitingAskPipeline()
    fake_runtime = SimpleNamespace(provider_manager=object(), tool_registry=object())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: fake_runtime)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-answer",
                        "contextId": "ctx-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Nginx 网站"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )

    data = response.json()
    assert "error" not in data
    assert data["result"]["task"]["id"] == "task-1"
    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.run_prompts == []
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    assert "nginx selected" in json.dumps(data, ensure_ascii=False)


def test_send_message_routes_context_only_pending_pipeline_input_from_legacy_sidecar_after_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    class WaitingAskPipeline:
        pipeline_name = "selling"
        sidecar_status = "waiting_input"

        def __init__(self) -> None:
            self.ask_answers: list[dict[str, str]] = []
            self.run_prompts: list[str] = []
            self.clear_sidecar_calls = 0

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="fresh pipeline")

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            yield TextDeltaEvent(text="nginx selected from legacy")

        def clear_sidecar(self) -> None:
            self.clear_sidecar_calls += 1

    fake_pipeline = WaitingAskPipeline()
    fake_runtime = SimpleNamespace(provider_manager=object(), tool_registry=object())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: fake_runtime)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-answer",
                        "contextId": "ctx-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Nginx 网站"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )

    data = response.json()
    assert "error" not in data
    assert data["result"]["task"]["id"] == "task-1"
    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.run_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    assert "nginx selected from legacy" in json.dumps(data, ensure_ascii=False)


def test_send_message_rejects_context_only_pending_pipeline_input_for_wrong_owner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required", owner="bob"))

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    class UnexpectedPipeline:
        pipeline_name = "selling"
        sidecar_status = "waiting_input"

        async def run(self, prompt: str):
            yield TextDeltaEvent(text=f"unexpected fresh run: {prompt}")

    fake_runtime = SimpleNamespace(provider_manager=object(), tool_registry=object())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: fake_runtime)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: UnexpectedPipeline())

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token="secret",
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        result = client.post(
            "/",
            headers={"A2A-Version": "1.0", "Authorization": "Bearer secret"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-answer",
                        "contextId": "ctx-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Nginx 网站"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ).json()

    assert result["error"]["message"] == "Task task-1 not found"


@pytest.mark.asyncio
async def test_persisted_task_is_visible_to_get_and_cancel_after_restart(tmp_path: Path) -> None:
    persistence_dir = tmp_path / "a2a"
    A2APersistenceStore(persistence_dir).save_task(
        A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working")
    )
    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
    )
    call_context = ServerCallContext()

    try:
        task = await components.handler.on_get_task(GetTaskRequest(id="task-1"), call_context)

        assert isinstance(task, Task)
        assert task.id == "task-1"
        assert task.context_id == "ctx-1"
        assert task.status.state == TaskState.TASK_STATE_WORKING
        assert A2APersistenceStore(persistence_dir).load_task("task-1").state == "working"
        with pytest.raises(TaskNotCancelableError):
            await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), call_context)
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_input_required_pipeline_task_after_restart_marks_canceled(tmp_path: Path) -> None:
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), session_id)
    permission_store = PermissionWaitCheckpointStore(str(tmp_path), session_id)
    permission = permission_store.create(
        build_permission_checkpoint(
            session_id=session_id,
            task_id="task-1",
            context_id="ctx-1",
            input_id="permission-1",
            tool_use_id="tool-1",
            tool_name="aliyun_api",
            tool_input={"action": "CreateStack"},
            permission_class="pipeline",
            continuation_frame={
                "assistantMessageRef": "pipeline/transcripts/transcript-step-1/session.jsonl:0",
                "assistantMessageDigest": "a" * 64,
                "orderedToolUseIds": ["tool-1"],
                "currentIndex": 0,
                "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
            },
            policy=PermissionWaitPolicy(),
        )
    )

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
    )
    call_context = ServerCallContext()

    try:
        task = await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), call_context)

        assert isinstance(task, Task)
        assert task.status.state == TaskState.TASK_STATE_CANCELED
        assert persistence.load_task("task-1").state == "canceled"
        assert permission_store.load(permission["boundaryId"])["phase"] == "CANCELED"
        snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
        assert snapshot["status"] == "canceled"
        assert snapshot["normalHandoff"]["action"] == "switch_to_normal"
        assert snapshot["normalHandoff"]["targetMode"] == "normal"
        assert snapshot["normalHandoff"]["outcome"] == "canceled"
        assert "Outcome: canceled" in snapshot["normalHandoff"]["summary"]
        events = A2APipelineJournal(pipeline_dir).read_all_repairing_tail()
        assert [event["eventType"] for event in events[-4:]] == [
            "pipeline_canceled",
            "pipeline_handoff_ready",
            "backup_committed",
            "backup_committed",
        ]
        assert [event["data"]["committedEventType"] for event in events[-2:]] == [
            "pipeline_canceled",
            "pipeline_handoff_ready",
        ]
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_inactive_permission_wait_loses_to_claimed_decision(tmp_path: Path) -> None:
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), session_id)
    permission_store = PermissionWaitCheckpointStore(str(tmp_path), session_id)
    permission = permission_store.create(
        build_permission_checkpoint(
            session_id=session_id,
            task_id="task-1",
            context_id="ctx-1",
            input_id="permission-1",
            tool_use_id="tool-1",
            tool_name="aliyun_api",
            tool_input={"action": "CreateStack"},
            permission_class="normal",
            continuation_frame={
                "assistantMessageRef": "session.jsonl:0",
                "assistantMessageDigest": "a" * 64,
                "orderedToolUseIds": ["tool-1"],
                "currentIndex": 0,
                "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
            },
            policy=PermissionWaitPolicy(),
        )
    )
    permission_store.claim_decision(permission["boundaryId"], value="allow_once", source="user")
    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
    )

    try:
        task = await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), ServerCallContext())

        assert isinstance(task, Task)
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert persistence.load_task("task-1").state == "input-required"
        assert permission_store.load(permission["boundaryId"])["decision"]["status"] == "claimed"
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_input_required_normal_task_marks_canceled_and_allows_same_context_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    persistence_dir = tmp_path / "a2a"

    class NeedsAuthLoop:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run_streaming(self, prompt: str):
            self.prompts.append(prompt)
            if prompt == "first":
                yield TextDeltaEvent(text="old partial")
                raise MCPNeedsAuthError("MCP server 'remote' requires authentication")
            yield TextDeltaEvent(text="old runtime reused")

    class FreshLoop:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run_streaming(self, prompt: str):
            self.prompts.append(prompt)
            yield TextDeltaEvent(text="fresh retry")

    needs_auth_loop = NeedsAuthLoop()
    fresh_loop = FreshLoop()
    factory_sessions: list[str] = []

    def runtime_factory(options):
        factory_sessions.append(options.session_id)
        loop = needs_auth_loop if len(factory_sessions) == 1 else fresh_loop
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", runtime_factory)

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
    )
    call_context = ServerCallContext()

    try:
        first = await components.handler.on_message_send(
            SendMessageRequest(
                message=Message(
                    message_id="msg-first",
                    context_id="ctx-1",
                    role=Role.ROLE_USER,
                    parts=[Part(text="first")],
                    metadata={"iac_code": {"cwd": str(tmp_path)}},
                ),
                configuration=SendMessageConfiguration(accepted_output_modes=["text/plain"]),
            ),
            call_context,
        )
        assert isinstance(first, Task)
        assert first.status.state == TaskState.TASK_STATE_INPUT_REQUIRED

        canceled = await components.handler.on_cancel_task(CancelTaskRequest(id=first.id), call_context)

        assert isinstance(canceled, Task)
        assert canceled.status.state == TaskState.TASK_STATE_CANCELED
        assert A2APersistenceStore(persistence_dir).load_task(first.id).state == "canceled"

        second = await components.handler.on_message_send(
            SendMessageRequest(
                message=Message(
                    message_id="msg-second",
                    context_id="ctx-1",
                    role=Role.ROLE_USER,
                    parts=[Part(text="second")],
                    metadata={"iac_code": {"cwd": str(tmp_path)}},
                ),
                configuration=SendMessageConfiguration(accepted_output_modes=["text/plain"]),
            ),
            call_context,
        )

        assert isinstance(second, Task)
        assert second.id != first.id
        assert second.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert "fresh retry" in json.dumps(MessageToDict(second, preserving_proto_field_name=False))
        assert len(factory_sessions) == 2
        assert factory_sessions[0] == factory_sessions[1]
        assert needs_auth_loop.prompts == ["first"]
        assert fresh_loop.prompts == ["second"]
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_input_required_pipeline_task_backup_blocked_returns_input_required(tmp_path: Path) -> None:
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), session_id)

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    class BlockingBackupService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, BackupReason, bool]] = []
            self.thread_ids: list[int] = []
            self.publication_proofs: list[dict[str, BackupPublicationProof]] = []

        def backup_session(
            self,
            cwd: str,
            session_id: str,
            *,
            reason: BackupReason,
            critical: bool,
            publication_proofs: dict[str, BackupPublicationProof],
        ) -> None:
            self.calls.append((cwd, session_id, reason, critical))
            self.thread_ids.append(threading.get_ident())
            self.publication_proofs.append(publication_proofs)
            session_dir = SessionStorage().session_dir(cwd, session_id)
            task_snapshot = json.loads((session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
            context_snapshot = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
            pipeline_snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
            assert task_snapshot["state"] == "input-required"
            assert context_snapshot["session_id"] == session_id
            assert context_snapshot["active_task_id"] is None
            assert pipeline_snapshot is not None
            assert pipeline_snapshot["status"] == "waiting_input"
            assert pipeline_snapshot["pendingTerminal"]["eventType"] == "pipeline_canceled"
            assert pipeline_snapshot["normalHandoff"] is None
            assert pipeline_snapshot["pendingNormalHandoff"]["outcome"] == "canceled"
            raise SessionBackupBlocked("copy failed secret_token=tok-live at /tmp/iac-code/cancel")

    backup_service = BlockingBackupService()
    event_loop_thread_id = threading.get_ident()
    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
        backup_service=backup_service,
    )
    call_context = ServerCallContext()

    try:
        task = await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), call_context)

        assert isinstance(task, Task)
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert persistence.load_task("task-1").state == "input-required"
        assert backup_service.calls == [(str(tmp_path), session_id, BackupReason.TERMINAL, True)]
        assert backup_service.publication_proofs[0][NORMAL_HANDOFF_PROOF_KEY].event_type == "pipeline_handoff_ready"
        assert backup_service.thread_ids and backup_service.thread_ids[0] != event_loop_thread_id
        snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
        assert snapshot is not None
        assert snapshot["status"] == "waiting_input"
        assert snapshot["pendingInput"]["kind"] == "ask_user_question"
        assert snapshot["normalHandoff"] is None
        assert snapshot["pendingNormalHandoff"] is None
        assert snapshot["pendingTerminal"] is None
        assert (
            recoverable_task_id_from_sidecar(cwd=str(tmp_path), session_id=session_id, context_id="ctx-1") == "task-1"
        )
        assert (
            recoverable_task_id_from_sidecar(
                cwd=str(tmp_path),
                session_id=session_id,
                context_id="ctx-1",
                include_running=False,
            )
            == "task-1"
        )
        assert not await components.handler.agent_executor._should_route_pipeline_handoff_to_normal(
            context_id="ctx-1",
            cwd=str(tmp_path),
        )
        events = A2APipelineJournal(pipeline_dir).read_all_repairing_tail()
        assert events[-1]["eventType"] == "backup_blocked"
        assert events[-1]["status"] == "input_required"
        assert events[-1]["data"]["reason"] == "terminal"
        assert events[-1]["data"]["recoverable"] is True
        assert "tok-live" in events[-1]["data"]["error"]
        assert "/tmp/iac-code" in events[-1]["data"]["error"]
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_input_required_pipeline_task_backup_blocked_persist_failure_stays_input_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    class BlockingBackupService:
        def backup_session(
            self,
            cwd: str,
            session_id: str,
            *,
            reason: BackupReason,
            critical: bool,
            publication_proofs: dict[str, BackupPublicationProof],
        ) -> None:
            assert publication_proofs[NORMAL_HANDOFF_PROOF_KEY].event_type == "pipeline_handoff_ready"
            raise SessionBackupBlocked("copy failed secret_token=tok-live at /tmp/iac-code/cancel")

    original_append = A2APipelineJournal.append

    def fail_backup_blocked_append(self, event: dict, durable: bool = False):
        if event.get("eventType") == "backup_blocked":
            raise OSError("journal locked")
        return original_append(self, event, durable=durable)

    monkeypatch.setattr(A2APipelineJournal, "append", fail_backup_blocked_append)

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
        backup_service=BlockingBackupService(),
    )
    call_context = ServerCallContext()

    try:
        task = await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), call_context)

        assert isinstance(task, Task)
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert persistence.load_task("task-1").state == "input-required"
        events = A2APipelineJournal(pipeline_dir).read_all_repairing_tail()
        assert [event["eventType"] for event in events] == [
            "input_required",
            "pipeline_canceled",
            "pipeline_handoff_ready",
            "pipeline_canceled",
            "pipeline_handoff_ready",
            "input_required",
        ]
        assert [event.get("visibility") for event in events[1:5]] == [
            "pending_backup",
            "pending_backup",
            "committed",
            "committed",
        ]
        assert events[-1]["data"]["kind"] == "terminal_publication_unavailable"
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_stale_input_required_pipeline_task_reconciles_terminal_sidecar(tmp_path: Path) -> None:
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    canceled = _pipeline_event(2, "evt-canceled")
    canceled["eventType"] = "pipeline_canceled"
    canceled["status"] = "canceled"
    canceled["data"] = {"source": "a2a_cancel"}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(pending)
    journal.append(canceled)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending, canceled]))

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
    )
    call_context = ServerCallContext()

    try:
        task = await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), call_context)

        assert isinstance(task, Task)
        assert task.status.state == TaskState.TASK_STATE_CANCELED
        assert persistence.load_task("task-1").state == "canceled"
        assert A2APipelineSnapshotStore(pipeline_dir).load()["status"] == "canceled"
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_input_required_pipeline_task_after_restart_enqueues_push_update(tmp_path: Path) -> None:
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
        push_notifications=True,
    )
    call_context = ServerCallContext()

    try:
        await components.handler.on_create_task_push_notification_config(
            TaskPushNotificationConfig(
                task_id="task-1",
                id="cfg-1",
                url="https://callback.example/a2a",
            ),
            call_context,
        )

        await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), call_context)

        job = await components.push_queue.claim()
        assert job is not None
        assert job.task_id == "task-1"
        assert job.config_id == "cfg-1"
        assert job.payload["statusUpdate"]["taskId"] == "task-1"
        assert job.payload["statusUpdate"]["status"]["state"] == "TASK_STATE_CANCELED"
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_cancel_input_required_pipeline_task_after_restart_ignores_push_enqueue_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    persistence_dir = tmp_path / "a2a"
    session_id = "session-ctx-1"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    pipeline_dir = SessionStorage().session_dir(str(tmp_path), session_id) / "a2a" / "pipeline"
    pending = _pipeline_pending_ask_event()
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
        push_notifications=True,
    )
    call_context = ServerCallContext()

    async def fail_send_notification(*_args, **_kwargs) -> None:
        raise OSError("queue unavailable")

    try:
        components.handler._push_sender.send_notification = fail_send_notification  # type: ignore[union-attr, method-assign]

        with caplog.at_level("WARNING", logger="iac_code.a2a.transports.dispatcher"):
            task = await components.handler.on_cancel_task(CancelTaskRequest(id="task-1"), call_context)

        assert task.status.state == TaskState.TASK_STATE_CANCELED
        assert persistence.load_task("task-1").state == "canceled"
        snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
        assert snapshot["status"] == "canceled"
        assert "Failed to enqueue A2A push notification for terminal task task-1" in caplog.text
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_subscribe_to_inactive_task_returns_error_without_hanging(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="done")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    call_context = ServerCallContext()

    result = await components.handler.on_message_send(
        SendMessageRequest(
            message=Message(
                message_id="msg-1",
                role=Role.ROLE_USER,
                parts=[Part(text="hello")],
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            configuration=SendMessageConfiguration(accepted_output_modes=["text/plain"]),
        ),
        call_context,
    )
    assert isinstance(result, Task)

    stream = components.handler.on_subscribe_to_task(SubscribeToTaskRequest(id=result.id), call_context)
    with pytest.raises(TaskNotFoundError, match="not active"):
        await asyncio.wait_for(anext(stream), timeout=0.1)
    await components.aclose()


@pytest.mark.asyncio
async def test_subscribe_to_active_task_yields_initial_task_then_updates(monkeypatch, tmp_path) -> None:
    release = asyncio.Event()
    prompts: list[str] = []

    class ControlledLoop:
        async def run_streaming(self, prompt: str):
            prompts.append(prompt)
            yield TextDeltaEvent(text="first")
            await release.wait()
            yield TextDeltaEvent(text="second")

    runtime = FakeRuntime(agent_loop=ControlledLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    call_context = ServerCallContext()

    result = await components.handler.on_message_send(
        SendMessageRequest(
            message=Message(
                message_id="msg-1",
                role=Role.ROLE_USER,
                parts=[Part(text="hello")],
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            configuration=SendMessageConfiguration(accepted_output_modes=["text/plain"], return_immediately=True),
        ),
        call_context,
    )
    assert isinstance(result, Task)

    stream = components.handler.on_subscribe_to_task(SubscribeToTaskRequest(id=result.id), call_context)
    first_event = await asyncio.wait_for(anext(stream), timeout=1)
    release.set()
    remaining_events = []

    async def collect_remaining_events() -> None:
        async for event in stream:
            remaining_events.append(event)

    await asyncio.wait_for(collect_remaining_events(), timeout=1)

    assert isinstance(first_event, Task)
    assert first_event.id == result.id
    assert "second" in json.dumps([event.__class__.__name__ + str(event) for event in remaining_events])
    assert prompts == ["hello"]
    await components.aclose()


@pytest.mark.asyncio
async def test_active_task_push_enqueue_failure_does_not_fail_task(monkeypatch, tmp_path, caplog) -> None:
    release = asyncio.Event()
    loop_completed = asyncio.Event()
    enqueue_attempted = asyncio.Event()

    class ControlledLoop:
        async def run_streaming(self, _prompt: str):
            yield TextDeltaEvent(text="first")
            await release.wait()
            yield TextDeltaEvent(text="second")
            loop_completed.set()

    runtime = FakeRuntime(agent_loop=ControlledLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=tmp_path / "a2a",
        push_notifications=True,
    )
    call_context = ServerCallContext()

    async def fail_enqueue(_job) -> None:
        enqueue_attempted.set()
        raise OSError("queue unavailable")

    try:
        result = await components.handler.on_message_send(
            SendMessageRequest(
                message=Message(
                    message_id="msg-1",
                    role=Role.ROLE_USER,
                    parts=[Part(text="hello")],
                    metadata={"iac_code": {"cwd": str(tmp_path)}},
                ),
                configuration=SendMessageConfiguration(accepted_output_modes=["text/plain"], return_immediately=True),
            ),
            call_context,
        )
        assert isinstance(result, Task)
        await components.handler.on_create_task_push_notification_config(
            TaskPushNotificationConfig(
                task_id=result.id,
                id="cfg-1",
                url="https://callback.example/a2a",
            ),
            call_context,
        )
        components.push_queue.enqueue = fail_enqueue  # type: ignore[union-attr, method-assign]

        stream = components.handler.on_subscribe_to_task(SubscribeToTaskRequest(id=result.id), call_context)
        await asyncio.wait_for(anext(stream), timeout=1)
        with caplog.at_level("WARNING", logger="iac_code.a2a.push"):
            release.set()

            async def collect_remaining_events() -> None:
                async for _event in stream:
                    pass

            await asyncio.wait_for(collect_remaining_events(), timeout=1)
            await asyncio.wait_for(loop_completed.wait(), timeout=1)
            await asyncio.wait_for(enqueue_attempted.wait(), timeout=1)

        final_task = await components.handler.on_get_task(GetTaskRequest(id=result.id), call_context)
        assert final_task.status.state != TaskState.TASK_STATE_FAILED
        assert "Failed to enqueue A2A push notification for task" in caplog.text
    finally:
        await components.aclose()


def test_create_app_wires_stateful_server_primitives(monkeypatch, tmp_path) -> None:
    calls: dict[str, object] = {}

    class SpyTaskStore:
        def __init__(self, **kwargs) -> None:
            calls["task_store_kwargs"] = kwargs

        async def start_cleanup_loop(self) -> None:
            calls["cleanup_started"] = True

        async def stop_cleanup_loop(self) -> None:
            calls["cleanup_stopped"] = True

    class SpyExecutor:
        def __init__(self, **kwargs) -> None:
            calls["executor_kwargs"] = kwargs

    class SpyPushConfigStore:
        def __init__(self, **kwargs) -> None:
            calls["push_store_kwargs"] = kwargs

        async def resolve_headers_for_dispatch(self, task_id: str, config_id: str) -> dict[str, str]:
            return {}

    class SpyPushSender:
        def __init__(self, **kwargs) -> None:
            calls["push_sender_kwargs"] = kwargs

    class SpyPushQueue:
        def __init__(self, root, **kwargs) -> None:
            calls["push_queue_root"] = root
            calls["push_queue_kwargs"] = kwargs

    class SpyPushWorker:
        def __init__(self, **kwargs) -> None:
            calls["push_worker_kwargs"] = kwargs
            self.started = asyncio.Event()

        async def serve_forever(self) -> None:
            calls["push_worker_started"] = True
            self.started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            calls["push_worker_closed"] = True

    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.A2ATaskStore", SpyTaskStore)
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.IacCodeA2AExecutor", SpyExecutor)
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.A2APushConfigStore", SpyPushConfigStore)
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.A2APushSender", SpyPushSender)
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.LocalFileA2APushQueue", SpyPushQueue)
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.A2APushDeliveryWorker", SpyPushWorker)

    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        signing_secret="s" * 32,
        signing_key_id="local-key",
        push_notifications=True,
    )
    with TestClient(app) as client:
        response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    card = response.json()
    assert card["capabilities"]["pushNotifications"] is True
    assert card["signatures"][0]["protected"]
    persistence = calls["task_store_kwargs"]["persistence"]
    assert isinstance(persistence, A2APersistenceStore)
    assert persistence.root == tmp_path / "state"
    assert calls["push_store_kwargs"]["persistence"] is persistence
    assert calls["push_store_kwargs"]["secret_keyring"] is calls["push_queue_kwargs"]["secret_keyring"]
    assert calls["push_queue_root"] == persistence.root / "push_queue"
    assert calls["push_sender_kwargs"]["config_store"] is not None
    assert calls["push_sender_kwargs"]["queue"] is not None
    assert calls["push_worker_kwargs"]["queue"] is not None
    assert calls["push_worker_started"] is True
    assert calls["push_worker_closed"] is True
    executor_kwargs = calls["executor_kwargs"]
    assert executor_kwargs["task_store"] is not None
    assert executor_kwargs["artifact_store"].root == tmp_path / "artifacts"


@pytest.mark.asyncio
async def test_runtime_components_close_owned_redis_push_queue(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeRedisQueue:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True
            captured["queue_closed"] = True

    class FakeRedisModule:
        @staticmethod
        def from_url(url):
            captured["redis_url"] = url
            return object()

    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.RedisStreamsA2APushQueue", FakeRedisQueue)
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.require_redis_asyncio", lambda: FakeRedisModule)

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=tmp_path,
        push_notifications=True,
        push_queue="redis-streams",
        push_redis_url="redis://localhost:6379/0",
        push_stream="custom:push",
        push_retry_key="custom:push:retry",
        push_dead_stream="custom:push:dead",
        push_consumer_group="custom-workers",
        push_consumer_name="worker-a",
        push_lease_timeout_ms=120000,
    )

    assert captured["redis_url"] == "redis://localhost:6379/0"
    assert captured["stream"] == "custom:push"
    assert captured["retry_key"] == "custom:push:retry"
    assert captured["dead_stream"] == "custom:push:dead"
    assert captured["consumer_group"] == "custom-workers"
    assert captured["consumer_name"] == "worker-a"
    assert captured["lease_timeout_ms"] == 120000
    assert captured["secret_keyring"] is not None
    assert components.push_worker is not None
    assert components.push_queue is not None

    await components.aclose()

    assert captured["queue_closed"] is True


@pytest.mark.asyncio
async def test_async_transport_runner_starts_push_worker() -> None:
    calls: dict[str, bool] = {}

    class SpyTaskStore:
        async def start_cleanup_loop(self) -> None:
            calls["cleanup_started"] = True

    class SpyPushWorker:
        async def serve_forever(self) -> None:
            calls["push_started"] = True
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            calls["push_closed"] = True

    class SpyComponents:
        task_store = SpyTaskStore()
        push_worker = SpyPushWorker()

        async def aclose(self) -> None:
            await self.push_worker.aclose()
            calls["components_closed"] = True

    class SpyServer:
        async def serve(self) -> None:
            calls["server_served"] = True

        async def aclose(self) -> None:
            calls["server_closed"] = True

    await _serve_async_transport(SpyServer(), components=SpyComponents())

    assert calls == {
        "cleanup_started": True,
        "push_started": True,
        "server_served": True,
        "server_closed": True,
        "push_closed": True,
        "components_closed": True,
    }


def test_pipeline_state_endpoint_omits_server_only_seen_event_ids(tmp_path) -> None:
    """`seenEventIds` 是服务端去重台账，客户端恢复用不到，却能占到整份响应的六成。

    这份台账只在服务端拿磁盘上的快照做新鲜度判定（``_snapshot_seen_events_are_within_replay``），
    客户端要的增量锚点是 ``lastSequence`` / ``afterSequence``。因此响应里不再带它，
    磁盘快照仍然照旧保存，服务端判定不受影响。
    """

    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(_pipeline_event(1, "evt-1"))
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([_pipeline_event(1, "evt-1")]))
    stored = snapshot_store.load()
    assert stored is not None
    assert stored["seenEventIds"] == ["evt-1"]
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-1")

    assert response.status_code == 200
    snapshot = response.json()["snapshot"]
    assert "seenEventIds" not in snapshot
    # 恢复真正依赖的锚点与展示数据照旧
    assert snapshot["lastSequence"] == 1
    assert snapshot["contextId"] == "ctx-1"
    assert "display" in snapshot
    # 磁盘快照不受影响：服务端下次仍能用台账判定新鲜度
    reloaded = snapshot_store.load()
    assert reloaded is not None
    assert reloaded["seenEventIds"] == ["evt-1"]


def test_pipeline_state_endpoint_keeps_tool_results_for_debugging_clients(tmp_path) -> None:
    """`display.toolResults` 仍要返回：pipeline debugger 与恢复 e2e 脚本都在读它。"""

    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _pipeline_event(1, "evt-1")
    event["eventType"] = "tool_result"
    event["data"] = {"toolUseId": "call-1", "toolName": "read_file", "result": "content"}
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        response = client.get("/iac-code/pipeline/state?contextId=ctx-1")

    display = response.json()["snapshot"]["display"]
    assert [item["toolUseId"] for item in display["toolResults"]] == ["call-1"]


def test_pipeline_state_endpoint_drops_tool_results_only_when_lean_is_requested(tmp_path) -> None:
    """`?lean=1` 才裁 `display.toolResults`：控制台恢复不读它，调试工具默认仍要全量。

    真实会话里 47 条工具留档就有 330 KB，占裁掉 ``seenEventIds`` 之后的四分之三。
    恢复界面只用消息、图表与候选方案，所以 bridge 拉恢复时显式要求精简。
    """

    persistence_dir = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_dir)
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    started = _pipeline_event(1, "evt-1")
    tool_result = _pipeline_event(2, "evt-2")
    tool_result["eventType"] = "tool_result"
    tool_result["data"] = {"toolUseId": "call-1", "toolName": "read_file", "result": "content"}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(started)
    journal.append(tool_result)
    snapshot = reduce_pipeline_events([started, tool_result])
    snapshot["display"]["messages"].append({"eventId": "msg-1", "text": "first message"})
    A2APipelineSnapshotStore(pipeline_dir).save(snapshot)
    app = create_app(
        host="127.0.0.1",
        port=41242,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=persistence_dir,
    )

    with TestClient(app) as client:
        lean = client.get("/iac-code/pipeline/state?contextId=ctx-1&lean=1")
        explicitly_full = client.get("/iac-code/pipeline/state?contextId=ctx-1&lean=0")
        unrecognized = client.get("/iac-code/pipeline/state?contextId=ctx-1&lean=yes")

    lean_display = lean.json()["snapshot"]["display"]
    assert "toolResults" not in lean_display
    # 恢复界面真正要用的展示数据一个不少
    assert lean_display["messages"] == [{"eventId": "msg-1", "text": "first message"}]
    assert lean.json()["snapshot"]["lastSequence"] == 2
    # 显式关闭与认不出来的值都给全量：这个开关只影响体积，不该让客户端拿不到数据
    for response in (explicitly_full, unrecognized):
        display = response.json()["snapshot"]["display"]
        assert [item["toolUseId"] for item in display["toolResults"]] == ["call-1"]
