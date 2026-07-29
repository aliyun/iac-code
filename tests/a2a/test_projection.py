from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from iac_code.a2a.app import A2AProjectionMiddleware
from iac_code.a2a.projection import (
    a2a_identities_from_data,
    a2a_identity_from_data,
    a2a_safe_mode_enabled,
    build_a2a_public_path_roots,
    project_a2a_data,
    resolve_a2a_public_path_roots_for_data,
)


def test_a2a_safe_mode_uses_shared_truthy_values(monkeypatch) -> None:
    for value in ("1", " true ", "YES", "On"):
        monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", value)
        assert a2a_safe_mode_enabled() is True
    for value in ("", "0", "false", "enabled", "no"):
        monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", value)
        assert a2a_safe_mode_enabled() is False


def test_a2a_roots_always_include_application_root(tmp_path, monkeypatch) -> None:
    application_root = tmp_path / "installed-iac-code"
    monkeypatch.setattr(
        "iac_code.tools.path_safety.get_iac_code_application_root",
        lambda: application_root,
    )

    roots = build_a2a_public_path_roots(cwd=str(tmp_path / "workspace"))

    assert any(root["path"] == str(application_root) for root in roots)


def test_project_a2a_data_safe_mode_off_returns_unredacted_deep_copy() -> None:
    canonical = {"password": "secret-value", "path": "/server-root/private/result.json"}

    projected = project_a2a_data(
        canonical,
        public_path_roots=[{"path": "/server-root", "label": "."}],
        safe_mode=False,
    )

    assert projected == canonical
    assert projected is not canonical
    projected["path"] = "changed"
    assert canonical["path"] == "/server-root/private/result.json"


def test_project_a2a_data_safe_mode_on_is_path_only() -> None:
    canonical = {
        "password": "secret-value",
        "server": "/server-root/private/result.json",
        "cloud": "/home/cloud-user/bootstrap.sh",
    }

    projected = project_a2a_data(
        canonical,
        public_path_roots=[{"path": "/server-root", "label": "."}],
        safe_mode=True,
    )

    assert projected == {
        "password": "secret-value",
        "server": "[PATH]",
        "cloud": "/home/cloud-user/bootstrap.sh",
    }
    assert canonical["server"] == "/server-root/private/result.json"


def test_project_a2a_data_mapping_key_collisions_do_not_drop_values() -> None:
    canonical = {
        "/server-root/a": "first",
        "[PATH]": "existing",
        "/server-root/b": "second",
        "[PATH#2]": "existing-second",
    }

    projected = project_a2a_data(
        canonical,
        public_path_roots=[{"path": "/server-root", "label": "."}],
        safe_mode=True,
    )

    assert projected == {
        "[PATH#3]": "first",
        "[PATH]": "existing",
        "[PATH#4]": "second",
        "[PATH#2]": "existing-second",
    }
    assert canonical == {
        "/server-root/a": "first",
        "[PATH]": "existing",
        "/server-root/b": "second",
        "[PATH#2]": "existing-second",
    }


def test_a2a_identity_ignores_jsonrpc_request_id_and_finds_nested_task() -> None:
    assert a2a_identity_from_data(
        {"jsonrpc": "2.0", "id": "request-17", "result": {"id": "task-1", "contextId": "context-1"}}
    ) == ("task-1", "context-1")


def test_a2a_identity_does_not_treat_bare_jsonrpc_id_as_task_id() -> None:
    assert a2a_identity_from_data({"jsonrpc": "2.0", "id": "request-17", "error": {"code": -32603}}) == (
        None,
        None,
    )


def test_a2a_identity_uses_task_scoped_jsonrpc_params_id_not_request_id() -> None:
    request = {"jsonrpc": "2.0", "id": "request-17", "method": "GetTask", "params": {"id": "task-1"}}

    assert a2a_identity_from_data(request) == ("task-1", None)


def test_a2a_identities_collect_every_task_in_list_response() -> None:
    response = {
        "result": {
            "tasks": [
                {"id": "task-1", "contextId": "context-1"},
                {"id": "task-2", "contextId": "context-2"},
            ]
        }
    }

    assert a2a_identities_from_data(response) == [
        ("task-1", "context-1"),
        ("task-2", "context-2"),
    ]


class _ProjectionTaskStore:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    async def get_task_record(self, task_id: str):
        assert task_id == "task-1"
        return SimpleNamespace(context_id="context-1")

    async def get_context_record(self, context_id: str):
        assert context_id == "context-1"
        return SimpleNamespace(cwd=self.cwd, session_id="session-1")


class _ProjectionTaskStoreWithRuntimeRoots(_ProjectionTaskStore):
    def __init__(self, cwd: str, trusted_root: str) -> None:
        super().__init__(cwd)
        self.trusted_root = trusted_root

    async def get_context_runtime_path_directories(self, context_id: str):
        assert context_id == "context-1"
        return [], [self.trusted_root], []


class _MultiProjectionTaskStore:
    def __init__(self, contexts: dict[str, tuple[str, str, str]]) -> None:
        self.contexts = contexts

    async def get_task_record(self, task_id: str):
        return SimpleNamespace(context_id=self.contexts[task_id][0])

    async def get_context_record(self, context_id: str):
        cwd, session_id = next(
            (cwd, session_id)
            for stored_context_id, cwd, session_id in self.contexts.values()
            if stored_context_id == context_id
        )
        return SimpleNamespace(cwd=cwd, session_id=session_id)


def test_http_projection_middleware_replays_request_and_applies_path_only_policy(tmp_path, monkeypatch) -> None:
    server_path = str(tmp_path / "private" / "result.json")

    async def echo(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"taskId": body["taskId"], "path": server_path, "password": body["password"]})

    app = Starlette(routes=[Route("/", echo, methods=["POST"])])
    app.add_middleware(A2AProjectionMiddleware, task_store=_ProjectionTaskStore(str(tmp_path)))

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")
    with TestClient(app) as client:
        safe = client.post("/", json={"taskId": "task-1", "password": "real-secret"}).json()
    assert safe == {"taskId": "task-1", "path": "[PATH]", "password": "real-secret"}

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "false")
    with TestClient(app) as client:
        raw = client.post("/", json={"taskId": "task-1", "password": "real-secret"}).json()
    assert raw == {"taskId": "task-1", "path": server_path, "password": "real-secret"}


def test_http_projection_uses_new_request_cwd_before_task_context_exists(tmp_path, monkeypatch) -> None:
    server_path = str(tmp_path / "private" / "result.json")

    async def fail(_request: Request) -> JSONResponse:
        return JSONResponse({"error": f"failed at {server_path}", "password": "real-secret"}, status_code=500)

    app = Starlette(routes=[Route("/", fail, methods=["POST"])])
    app.add_middleware(A2AProjectionMiddleware, task_store=SimpleNamespace())
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")

    with TestClient(app) as client:
        response = client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": "request-1",
                "params": {"message": {"metadata": {"iac_code": {"cwd": str(tmp_path)}}}},
            },
        )

    assert response.json() == {"error": "failed at [PATH]", "password": "real-secret"}


def test_http_jsonrpc_task_scoped_error_uses_params_task_workspace(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "task-workspace"
    server_path = str(workspace / "private" / "result.json")
    store = _MultiProjectionTaskStore(
        {"task-1": ("context-1", str(workspace), "session-1")}
    )

    async def fail(_request: Request) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": "request-1", "error": {"message": server_path}})

    app = Starlette(routes=[Route("/", fail, methods=["POST"])])
    app.add_middleware(A2AProjectionMiddleware, task_store=store)
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")

    with TestClient(app) as client:
        response = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": "request-1", "method": "GetTask", "params": {"id": "task-1"}},
        )

    assert response.json()["error"]["message"] == "[PATH]"


def test_http_rest_projection_uses_task_id_from_routed_path_parameter(tmp_path, monkeypatch) -> None:
    server_path = str(tmp_path / "private" / "result.json")

    async def fail(_request: Request) -> JSONResponse:
        return JSONResponse({"error": f"failed at {server_path}"}, status_code=500)

    app = Starlette(routes=[Route("/tasks/{id}", fail)])
    app.add_middleware(A2AProjectionMiddleware, task_store=_ProjectionTaskStore(str(tmp_path)))
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")

    with TestClient(app) as client:
        response = client.get("/tasks/task-1")

    assert response.json() == {"error": "failed at [PATH]"}


def test_http_projection_includes_current_runtime_trusted_directories(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    trusted = tmp_path / "shared"
    server_path = str(trusted / "private" / "result.json")

    async def result(_request: Request) -> JSONResponse:
        return JSONResponse({"taskId": "task-1", "path": server_path})

    app = Starlette(routes=[Route("/", result)])
    app.add_middleware(
        A2AProjectionMiddleware,
        task_store=_ProjectionTaskStoreWithRuntimeRoots(str(workspace), str(trusted)),
    )
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")

    with TestClient(app) as client:
        response = client.get("/")

    assert response.json() == {"taskId": "task-1", "path": "[PATH]"}


def test_http_projection_aggregates_roots_for_list_tasks_response(tmp_path, monkeypatch) -> None:
    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    store = _MultiProjectionTaskStore(
        {
            "task-1": ("context-1", str(workspace_one), "session-1"),
            "task-2": ("context-2", str(workspace_two), "session-2"),
        }
    )

    async def list_tasks(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "tasks": [
                    {
                        "id": "task-1",
                        "contextId": "context-1",
                        "path": str(workspace_one / "result.json"),
                    },
                    {
                        "id": "task-2",
                        "contextId": "context-2",
                        "path": str(workspace_two / "result.json"),
                    },
                ]
            }
        )

    app = Starlette(routes=[Route("/", list_tasks)])
    app.add_middleware(A2AProjectionMiddleware, task_store=store)
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")

    with TestClient(app) as client:
        response = client.get("/")

    assert [task["path"] for task in response.json()["tasks"]] == ["[PATH]", "[PATH]"]


@pytest.mark.asyncio
async def test_task_scoped_request_and_grpc_bare_id_resolve_task_workspace_roots(tmp_path) -> None:
    workspace = tmp_path / "task-workspace"
    store = _MultiProjectionTaskStore(
        {"task-1": ("context-1", str(workspace), "session-1")}
    )

    jsonrpc_roots = await resolve_a2a_public_path_roots_for_data(
        store,
        request_data={"jsonrpc": "2.0", "id": "request-1", "method": "GetTask", "params": {"id": "task-1"}},
    )
    grpc_roots = await resolve_a2a_public_path_roots_for_data(
        store,
        request_data={"id": "task-1"},
        request_bare_id_is_task_id=True,
    )

    assert any(root["path"] == str(workspace) for root in jsonrpc_roots)
    assert any(root["path"] == str(workspace) for root in grpc_roots)


def test_http_projection_middleware_projects_sse_frames_incrementally(tmp_path, monkeypatch) -> None:
    server_path = str(tmp_path / "private" / "result.json")
    payload = json.dumps({"taskId": "task-1", "path": server_path, "password": "real-secret"})
    split_at = len(payload) // 2

    async def stream(_request: Request) -> StreamingResponse:
        async def chunks():
            yield "event: message\ndata: " + payload[:split_at]
            yield payload[split_at:] + "\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/stream", stream)])
    app.add_middleware(A2AProjectionMiddleware, task_store=_ProjectionTaskStore(str(tmp_path)))
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "on")

    with TestClient(app) as client:
        response = client.get("/stream")

    data_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    assert json.loads(data_line.removeprefix("data: ")) == {
        "taskId": "task-1",
        "path": "[PATH]",
        "password": "real-secret",
    }
