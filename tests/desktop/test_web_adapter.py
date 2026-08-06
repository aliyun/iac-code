from __future__ import annotations

import asyncio
import base64
import os
import threading
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from iac_code.desktop.runtime import DesktopInstallContext
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


class _PipelineRunner:
    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _runner_factory() -> _PipelineRunner:
    return _PipelineRunner()


def _install_context(tmp_path: Path, *, degraded: tuple[str, ...] = ()) -> DesktopInstallContext:
    runtime_dir = tmp_path / "runtime"
    host_state_dir = tmp_path / "host-state"
    install_lock_dir = tmp_path / "install-lock"
    for path in (runtime_dir, host_state_dir, install_lock_dir):
        path.mkdir()
    return DesktopInstallContext(
        install_id="com-alibabacloud-iac-code-stable",
        runtime_dir=runtime_dir,
        host_state_dir=host_state_dir,
        install_lock_dir=install_lock_dir,
        degraded_prerequisites=degraded,
    )


def _desktop_app(tmp_path: Path, *, degraded: tuple[str, ...] = ()):
    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    return create_app(
        session_manager=manager,
        desktop_runtime=True,
        default_project_cwd=project,
        distribution_channel="appimage",
        update_mode="tauri",
        desktop_install_context=_install_context(tmp_path, degraded=degraded),
        pipeline_action_runner_factory=_runner_factory,
    )


def _route_paths(app) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def test_default_web_routes_and_bootstrap_remain_unchanged() -> None:
    app = create_app(pipeline_action_runner_factory=_runner_factory)

    assert "/api/server/restart" in _route_paths(app)
    assert "/api/update/status" in _route_paths(app)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "window.__IAC_DESKTOP__" not in response.text


def test_desktop_injects_runtime_and_removes_web_self_update_routes(tmp_path: Path) -> None:
    app = _desktop_app(tmp_path)
    paths = _route_paths(app)

    assert "/api/server/restart" not in paths
    assert "/api/update/status" not in paths
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert 'window.__IAC_DESKTOP__ = {"runtime": "desktop"' in response.text
    assert '"nativeUpdater": true' in response.text
    assert '"distributionChannel": "appimage"' in response.text
    assert "/api/desktop/diagnostics" in paths


def test_desktop_requires_explicit_valid_session_cwd(tmp_path: Path) -> None:
    app = _desktop_app(tmp_path)
    with TestClient(app) as client:
        missing = client.post("/api/sessions", json={})
        invalid = client.post("/api/sessions", json={"cwd": str(tmp_path / "missing")})
        valid = client.post("/api/sessions", json={"cwd": str(tmp_path / "project")})

    assert missing.status_code == 400
    assert invalid.status_code == 400
    assert valid.status_code == 201


def test_desktop_arguments_cannot_leak_into_default_web(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    try:
        create_app(default_project_cwd=project, pipeline_action_runner_factory=_runner_factory)
    except ValueError as exc:
        assert "desktop_runtime=True" in str(exc)
    else:  # pragma: no cover - explicit assertion gives a clearer failure
        raise AssertionError("Desktop-only argument was accepted by the default Web app")


def test_desktop_diagnostics_is_read_only_and_not_mounted_for_web(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    app = _desktop_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/desktop/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"] == "desktop"
    assert payload["paths"]["runtime"] == str(tmp_path / "runtime")
    assert set(payload["tools"]) == {"git", "terraform", "node", "npm", "npx", "infraguard"}
    assert "credentials" not in response.text.lower()
    assert not config_dir.exists()

    web_app = create_app(pipeline_action_runner_factory=_runner_factory)
    assert "/api/desktop/diagnostics" not in _route_paths(web_app)


def test_desktop_git_bash_startup_probe_and_explicit_install_are_desktop_only(tmp_path: Path, monkeypatch) -> None:
    install_calls: list[str] = []

    def install_git_bash() -> dict[str, str | None]:
        install_calls.append("install")
        return {"status": "available", "path": r"C:\Program Files\Git\bin\bash.exe"}

    monkeypatch.setattr(
        "iac_code.desktop.git_bash.inspect_git_bash",
        lambda: {"status": "unavailable", "path": None},
    )
    monkeypatch.setattr(
        "iac_code.desktop.git_bash.install_git_bash_for_desktop",
        install_git_bash,
    )
    app = _desktop_app(tmp_path)

    with TestClient(app) as client:
        probe = client.get("/api/desktop/git-bash")
        assert install_calls == []
        installed = client.post("/api/desktop/git-bash/install")

    assert probe.status_code == 200
    assert probe.json() == {"status": "unavailable", "path": None}
    assert install_calls == ["install"]
    assert installed.status_code == 200
    assert installed.json()["status"] == "available"
    assert app.state.desktop_controller.close_state()["activeWorkCount"] == 0

    web_paths = _route_paths(create_app(pipeline_action_runner_factory=_runner_factory))
    assert "/api/desktop/git-bash" not in web_paths
    assert "/api/desktop/git-bash/install" not in web_paths


def test_desktop_image_store_transaction_runs_off_the_web_event_loop(tmp_path: Path, monkeypatch) -> None:
    from iac_code.services.capabilities import multimodal
    from iac_code.web import images

    handler_threads: list[int] = []
    store_threads: list[int] = []
    original_store = images.store_cached_image

    def supports_images(*args, **kwargs):
        handler_threads.append(threading.get_ident())
        return True

    def store_cached_image(*args, **kwargs):
        store_threads.append(threading.get_ident())
        return original_store(*args, **kwargs)

    monkeypatch.setattr(multimodal, "is_model_multimodal", supports_images)
    monkeypatch.setattr(images, "store_cached_image", store_cached_image)
    app = _desktop_app(tmp_path)
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"cwd": str(tmp_path / "project")}).json()["webSessionId"]
        response = client.post(
            "/api/images",
            json={
                "sessionId": session_id,
                "mediaType": "image/png",
                "data": base64.b64encode(b"\x89PNG\r\n\x1a\npng-data").decode("ascii"),
            },
        )

    assert response.status_code == 201
    assert len(handler_threads) == len(store_threads) == 1
    assert store_threads[0] != handler_threads[0]


@pytest.mark.asyncio
async def test_desktop_settings_transactions_preserve_concurrent_updates(tmp_path: Path, monkeypatch) -> None:
    from iac_code.web import settings as web_settings

    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(web_settings, "set_language", lambda _language: None)
    original_load = web_settings._load_yaml
    load_count = 0
    load_count_lock = threading.Lock()
    second_load_started = threading.Event()

    def synchronized_load(path: Path) -> dict:
        nonlocal load_count
        settings = original_load(path)
        with load_count_lock:
            load_count += 1
            current_load = load_count
        if current_load == 1:
            second_load_started.wait(timeout=0.5)
        elif current_load == 2:
            second_load_started.set()
        return settings

    monkeypatch.setattr(web_settings, "_load_yaml", synchronized_load)
    app = _desktop_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        appearance, language = await asyncio.gather(
            client.put("/api/settings/appearance", json={"theme": "midnight"}),
            client.put("/api/settings/ui-language", json={"language": "zh"}),
        )

    assert appearance.status_code == 200
    assert language.status_code == 200
    settings = original_load(config_dir / "settings.yml")
    assert settings["appearance"]["theme"] == "midnight"
    assert settings["ui"]["language"] == "zh"


def test_desktop_tool_paths_round_trip_and_validate_executable_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    binary_dir = tmp_path / "tools"
    binary_dir.mkdir()
    git = binary_dir / "git"
    git.write_text("desktop-test", encoding="utf-8")
    wrong = binary_dir / "not-git"
    wrong.write_text("desktop-test", encoding="utf-8")
    app = _desktop_app(tmp_path)

    with TestClient(app) as client:
        saved = client.put(
            "/api/desktop/tool-paths",
            json={"toolPaths": {"git": str(git)}, "searchPaths": [str(binary_dir), str(binary_dir)]},
        )
        loaded = client.get("/api/desktop/tool-paths")
        invalid = client.put(
            "/api/desktop/tool-paths",
            json={"toolPaths": {"git": str(wrong)}, "searchPaths": []},
        )

    expected = {"toolPaths": {"git": str(git.resolve())}, "searchPaths": [str(binary_dir.resolve())]}
    assert saved.status_code == 200
    assert saved.json() == expected
    assert loaded.json() == expected
    assert invalid.status_code == 400
    assert "filename" in invalid.text
    assert "/api/desktop/tool-paths" not in _route_paths(create_app(pipeline_action_runner_factory=_runner_factory))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink basename contract")
def test_desktop_tool_path_preserves_valid_symlink_basename(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    binary_dir = tmp_path / "tools"
    binary_dir.mkdir()
    target = binary_dir / "npm-cli.js"
    target.write_text("desktop-test", encoding="utf-8")
    npm = binary_dir / "npm"
    npm.symlink_to(target.name)
    app = _desktop_app(tmp_path)

    with TestClient(app) as client:
        response = client.put(
            "/api/desktop/tool-paths",
            json={"toolPaths": {"npm": str(npm)}, "searchPaths": []},
        )

    assert response.status_code == 200
    assert response.json()["toolPaths"]["npm"] == str(npm.absolute())
    assert Path(response.json()["toolPaths"]["npm"]).name == "npm"


def test_desktop_probe_timeout_payload_remains_http_200(tmp_path: Path, monkeypatch) -> None:
    from iac_code.desktop import probe_worker

    async def timed_out(kind, config, current_project, *, timeout):
        return probe_worker._timeout_result(kind, config, current_project)

    monkeypatch.setattr(probe_worker, "run_desktop_probe", timed_out)
    app = _desktop_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/desktop/diagnostics")

    assert response.status_code == 200
    assert response.json()["tools"]["git"]["status"] == "timeout"


def test_desktop_aliyun_oauth_route_injects_controlled_browser_opener(tmp_path: Path, monkeypatch) -> None:
    from iac_code.desktop.external_env import open_desktop_browser
    from iac_code.web import settings

    observed: list[object] = []

    def login(data, *, browser_opener=None):
        observed.append(browser_opener)
        return {"configured": True, "mode": "OAuth", "region": data.get("region")}

    monkeypatch.setattr(settings, "login_aliyun_oauth", login)
    app = _desktop_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/cloud/aliyun/oauth-login",
            json={"site": "CN", "region": "cn-hangzhou"},
        )

    assert response.status_code == 200
    assert observed == [open_desktop_browser]


def test_desktop_quiescing_rejects_new_sessions_and_external_queue_input(tmp_path: Path) -> None:
    app = _desktop_app(tmp_path)
    project = str(tmp_path / "project")
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"cwd": project})
        session_id = created.json()["webSessionId"]
        app.state.desktop_controller.prepare_close()

        new_session = client.post("/api/sessions", json={"cwd": project})
        queued = client.post(
            f"/api/sessions/{session_id}/queued-inputs",
            json={"text": "must not be accepted after close admission"},
        )

    assert new_session.status_code == 409
    assert queued.status_code == 409
    assert app.state.desktop_controller.close_state()["activeWorkCount"] == 0


def test_desktop_quiescing_allows_empty_interrupt_but_rejects_new_message(tmp_path: Path) -> None:
    app = _desktop_app(tmp_path)
    project = str(tmp_path / "project")
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"cwd": project}).json()["webSessionId"]
        app.state.desktop_controller.prepare_close()

        message = client.post(f"/api/sessions/{session_id}/interrupt", json={"message": "new work"})
        stop = client.post(f"/api/sessions/{session_id}/interrupt", json={"message": ""})

    assert message.status_code == 409
    assert stop.status_code == 200
    assert stop.json() == {"accepted": True}


def test_desktop_repair_query_uses_force_repair_without_changing_web_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from iac_code.web import pipeline_prerequisites

    observed: list[tuple[DesktopInstallContext | None, bool]] = []

    async def fake_stream(context=None, *, force_repair=False, desktop_cancel_event=None):
        observed.append((context, force_repair))
        yield {"phase": "result", "status": "ok", "satisfied": True}

    monkeypatch.setattr(pipeline_prerequisites, "stream_install_review_step_prerequisite", fake_stream)
    app = _desktop_app(tmp_path, degraded=("infraguard",))
    with TestClient(app) as client:
        response = client.post("/api/settings/pipeline-review-step/install?repair=1")
        bootstrap = client.get("/")

    assert response.status_code == 200
    assert observed and observed[0][0] is not None
    assert observed[0][1] is True
    assert '"degradedPrerequisites": []' in bootstrap.text
