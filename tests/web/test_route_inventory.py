from __future__ import annotations

import json
from collections.abc import Callable

from starlette.testclient import TestClient

SECRET_STRINGS = (
    "sk-test-secret",
    "ALIYUN_SECRET",
    "SECRET_ACCESS_KEY",
)

# 本地单用户工作台有意回填已保存的云 AccessKeySecret 供页面查看/编辑(与模型面板
# savedApiKey 约定一致),因此该路由的响应允许出现刚保存的密钥。其正向回传由下方
# 专门断言覆盖,OAuth 令牌等仍不得出现。
SECRET_ROUNDTRIP_ROUTES = {"PUT /api/cloud/aliyun"}


def _assert_json_response(response, route: str) -> None:
    assert response.status_code < 500, route
    assert response.status_code != 501, route
    assert response.headers["content-type"].startswith("application/json"), route
    if route in SECRET_ROUNDTRIP_ROUTES:
        return
    for secret in SECRET_STRINGS:
        assert secret not in response.text, route


def _assert_static_response(response, content_type: str, route: str) -> None:
    assert response.status_code == 200, route
    assert content_type in response.headers["content-type"], route
    assert response.content, route


def test_p0_p1_route_inventory_has_no_501_secret_echoes_or_non_json_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_PROVIDER", raising=False)
    monkeypatch.delenv("IAC_CODE_MODEL", raising=False)
    monkeypatch.delenv("IAC_CODE_BASE_URL", raising=False)
    monkeypatch.delenv("IAC_CODE_API_KEY", raising=False)

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(
        session_id="route-inventory",
        mode="normal",
    )
    pipeline_session = manager.create_session(
        session_id="route-inventory-pipeline",
        mode="pipeline",
        pipeline_name="deploy",
        context_id="ctx-1",
        task_id="task-1",
        allow_user_escapes={"command": True},
    )
    app = create_app(session_manager=manager)

    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post("/api/sessions", json={"cwd": str(project), "sessionId": "created-session"})
        created_session_id = created.json()["sessionId"] if created.status_code == 201 else session.session_id
        routes: list[tuple[str, Callable[[], object]]] = [
            ("GET /health", lambda: client.get("/health")),
            ("POST /api/sessions", lambda: created),
            ("GET /api/sessions", lambda: client.get("/api/sessions")),
            ("GET /api/sessions/archived", lambda: client.get("/api/sessions/archived")),
            ("DELETE /api/sessions/archived", lambda: client.delete("/api/sessions/archived")),
            ("GET /api/sessions/{sessionId}", lambda: client.get(f"/api/sessions/{created_session_id}")),
            (
                "PATCH /api/sessions/{sessionId}",
                lambda: client.patch(
                    f"/api/sessions/{session.session_id}",
                    json={"name": "route inventory", "debugEnabled": True},
                ),
            ),
            (
                "DELETE /api/sessions/{sessionId}",
                lambda: client.delete(f"/api/sessions/{created_session_id}"),
            ),
            (
                "PUT /api/sessions/{sessionId}/permission-mode",
                lambda: client.put(
                    f"/api/sessions/{session.session_id}/permission-mode",
                    json={"mode": "accept_edits"},
                ),
            ),
            (
                "GET /api/sessions/{sessionId}/messages",
                lambda: client.get(f"/api/sessions/{session.session_id}/messages"),
            ),
            (
                "GET /api/sessions/{sessionId}/outputs",
                lambda: client.get(f"/api/sessions/{session.session_id}/outputs"),
            ),
            (
                "GET /api/sessions/{sessionId}/outputs/file",
                lambda: client.get(f"/api/sessions/{session.session_id}/outputs/file?path=x.json"),
            ),
            (
                "POST /api/sessions/{sessionId}/messages",
                lambda: client.post(f"/api/sessions/{session.session_id}/messages", json={}),
            ),
            (
                "GET /api/sessions/{sessionId}/events",
                lambda: client.get(f"/api/sessions/{session.session_id}/events?afterSequence=not-an-int"),
            ),
            (
                "POST /api/sessions/{sessionId}/commands",
                lambda: client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/status"}),
            ),
            (
                "POST /api/sessions/{sessionId}/queued-inputs",
                lambda: client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": "/status"}),
            ),
            (
                "POST /api/sessions/{sessionId}/interrupt",
                lambda: client.post(f"/api/sessions/{session.session_id}/interrupt", json={"message": ""}),
            ),
            (
                "GET /api/sessions/{sessionId}/status",
                lambda: client.get(f"/api/sessions/{session.session_id}/status"),
            ),
            (
                "GET /api/sessions/{sessionId}/prompt",
                lambda: client.get(f"/api/sessions/{session.session_id}/prompt"),
            ),
            (
                "POST /api/sessions/{sessionId}/compact",
                lambda: client.post(f"/api/sessions/{session.session_id}/compact"),
            ),
            (
                "GET /api/sessions/{sessionId}/debug",
                lambda: client.get(f"/api/sessions/{session.session_id}/debug"),
            ),
            (
                "POST /api/permissions/{requestId}/answer",
                lambda: client.post(
                    "/api/permissions/missing-permission/answer",
                    json={"sessionId": session.session_id, "choice": "allow_once"},
                ),
            ),
            (
                "POST /api/questions/{requestId}/answer",
                lambda: client.post(
                    "/api/questions/missing-question/answer",
                    json={
                        "sessionId": session.session_id,
                        "selected_id": "yes",
                        "selected_label": "Yes",
                        "free_text": "",
                    },
                ),
            ),
            ("GET /api/commands", lambda: client.get("/api/commands")),
            (
                "GET /api/suggestions",
                lambda: client.get(f"/api/suggestions?sessionId={session.web_session_id}&kind=command&q=sta"),
            ),
            ("GET /api/providers", lambda: client.get("/api/providers")),
            (
                "PUT /api/providers/active",
                lambda: client.put(
                    "/api/providers/active",
                    json={
                        "provider": "openai",
                        "model": "gpt-5.5",
                        "effort": "high",
                        "apiBase": "https://llm.example.test/v1",
                        "apiKey": "sk-test-secret",
                    },
                ),
            ),
            ("GET /api/cloud/aliyun", lambda: client.get("/api/cloud/aliyun")),
            (
                "PUT /api/cloud/aliyun",
                lambda: client.put(
                    "/api/cloud/aliyun",
                    json={
                        "mode": "AK",
                        "region": "cn-shanghai",
                        "accessKeyId": "LTAI-fake",
                        "accessKeySecret": "ALIYUN_SECRET",
                    },
                ),
            ),
            ("GET /api/memory", lambda: client.get(f"/api/memory?sessionId={session.web_session_id}")),
            ("GET /api/memory/projects", lambda: client.get("/api/memory/projects")),
            (
                "PUT /api/memory/project",
                lambda: client.put(
                    "/api/memory/project",
                    json={"sessionId": session.web_session_id, "content": "project memory"},
                ),
            ),
            (
                "PUT /api/memory/user",
                lambda: client.put(
                    "/api/memory/user",
                    json={"sessionId": session.web_session_id, "content": "user memory"},
                ),
            ),
            ("PUT /api/memory/auto", lambda: client.put("/api/memory/auto", json={"enabled": True})),
            ("GET /api/memory/legacy", lambda: client.get("/api/memory/legacy?q=none")),
            ("DELETE /api/memory/legacy/{memoryId}", lambda: client.delete("/api/memory/legacy/missing-memory")),
            ("GET /api/skills", lambda: client.get(f"/api/skills?sessionId={session.web_session_id}")),
            (
                "PUT /api/skills/disabled",
                lambda: client.put(
                    "/api/skills/disabled",
                    json={"sessionId": session.web_session_id, "disabled": []},
                ),
            ),
            (
                "POST /api/images",
                lambda: client.post(
                    "/api/images",
                    json={"sessionId": session.web_session_id, "mediaType": "image/png", "data": "not-base64"},
                ),
            ),
            (
                "POST /api/sessions/{sessionId}/images",
                lambda: client.post(
                    f"/api/sessions/{session.session_id}/images",
                    json={"mediaType": "image/png", "data": "not-base64"},
                ),
            ),
            (
                "GET /api/images/{imageId}",
                lambda: client.get(f"/api/images/missing-image?sessionId={session.web_session_id}"),
            ),
            ("GET /api/files/search", lambda: client.get(f"/api/files/search?sessionId={session.web_session_id}&q=py")),
            (
                "GET /api/files/quick-open",
                lambda: client.get(f"/api/files/quick-open?sessionId={session.web_session_id}&q=py"),
            ),
            (
                "GET /api/history/search",
                lambda: client.get(f"/api/history/search?sessionId={session.web_session_id}&q=deploy"),
            ),
            (
                "GET /api/transcript/{turnId}",
                lambda: client.get(f"/api/transcript/missing-turn?sessionId={session.web_session_id}"),
            ),
            ("GET /api/pipeline/state", lambda: client.get("/api/pipeline/state")),
            (
                "POST /api/pipeline/candidates/select",
                lambda: client.post(
                    "/api/pipeline/candidates/select",
                    json={"sessionId": pipeline_session.session_id, "candidateName": "fake"},
                ),
            ),
            (
                "GET /api/sessions/{sessionId}/cleanup",
                lambda: client.get(f"/api/sessions/{session.session_id}/cleanup"),
            ),
            (
                "POST /api/projects/archive-sessions",
                lambda: client.post("/api/projects/archive-sessions", json={"cwd": "/nonexistent/project"}),
            ),
            (
                "GET /api/settings/appearance",
                lambda: client.get("/api/settings/appearance"),
            ),
            (
                "PUT /api/settings/appearance",
                lambda: client.put("/api/settings/appearance", json={"theme": "graphite"}),
            ),
        ]

        static_routes = [
            ("GET /", client.get("/"), "text/html"),
            ("GET /static/styles.css", client.get("/static/styles.css"), "text/css"),
            ("GET /static/js/app.js", client.get("/static/js/app.js"), "javascript"),
        ]
        responses = [(route, call()) for route, call in routes]

    for route, response, content_type in static_routes:
        _assert_static_response(response, content_type, route)
    for route, response in responses:
        _assert_json_response(response, route)
        assert response.status_code != 404 or route in {
            "POST /api/permissions/{requestId}/answer",
            "POST /api/questions/{requestId}/answer",
            "DELETE /api/memory/legacy/{memoryId}",
            "GET /api/images/{imageId}",
            "GET /api/transcript/{turnId}",
            "POST /api/pipeline/candidates/select",
            "GET /api/sessions/{sessionId}/outputs/file",
        }, json.dumps({"route": route, "status": response.status_code, "body": response.text})

    # 显式验证云凭证保存路由的有意回填:AccessKeySecret 原样回传供本地查看,OAuth 令牌不回传。
    cloud_put = next(resp for route, resp in responses if route == "PUT /api/cloud/aliyun")
    assert cloud_put.json()["accessKeySecret"] == "ALIYUN_SECRET"
    assert "oauthAccessToken" not in cloud_put.json()
