import asyncio
import builtins
import json
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
from starlette.testclient import TestClient

from iac_code.a2a.app import (
    A2AAuthMiddleware,
    _serve_async_transport,
    _supported_interfaces,
    create_app,
    resolve_api_key,
    resolve_basic_credentials,
    resolve_token,
    run_server,
)
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
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


def test_resolve_api_key_uses_environment(monkeypatch) -> None:
    monkeypatch.setenv("IACCODE_A2A_API_KEY", "env-key")

    assert resolve_api_key(None) == "env-key"


def test_health_route() -> None:
    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


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
        snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
        assert snapshot["status"] == "canceled"
        assert snapshot["normalHandoff"]["action"] == "switch_to_normal"
        assert snapshot["normalHandoff"]["targetMode"] == "normal"
        assert snapshot["normalHandoff"]["outcome"] == "canceled"
        assert "Outcome: canceled" in snapshot["normalHandoff"]["summary"]
        events = A2APipelineJournal(pipeline_dir).read_all_repairing_tail()
        assert [event["eventType"] for event in events[-2:]] == ["pipeline_canceled", "pipeline_handoff_ready"]
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

    class ControlledLoop:
        async def run_streaming(self, _prompt: str):
            yield TextDeltaEvent(text="first")
            await release.wait()
            yield TextDeltaEvent(text="second")

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
            remaining_events = []

            async def collect_remaining_events() -> None:
                async for event in stream:
                    remaining_events.append(event)

            await asyncio.wait_for(collect_remaining_events(), timeout=1)

        final_task = await components.handler.on_get_task(GetTaskRequest(id=result.id), call_context)
        assert final_task.status.state != TaskState.TASK_STATE_FAILED
        assert "second" in json.dumps([event.__class__.__name__ + str(event) for event in remaining_events])
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
