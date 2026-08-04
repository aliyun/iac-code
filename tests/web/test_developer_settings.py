import pytest
from starlette.testclient import TestClient

from iac_code.web import settings
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    yield


def test_developer_settings_default_false():
    assert settings.developer_settings() == {"mode": False, "highlightFailedTools": False, "debug": False}


def test_save_developer_settings_roundtrip():
    result = settings.save_developer_settings(True, False, False)
    assert result == {"mode": True, "highlightFailedTools": False, "debug": False}
    assert settings.developer_settings() == {"mode": True, "highlightFailedTools": False, "debug": False}

    result2 = settings.save_developer_settings(False, True, True)
    assert result2 == {"mode": False, "highlightFailedTools": True, "debug": True}
    assert settings.developer_settings() == {"mode": False, "highlightFailedTools": True, "debug": True}


def test_save_developer_settings_preserves_other_keys():
    from iac_code.config import _load_yaml, _save_yaml, get_settings_path

    path = get_settings_path()
    data = _load_yaml(path)
    data.setdefault("developer", {})["someOtherKey"] = "keep-me"
    _save_yaml(path, data)

    settings.save_developer_settings(True, True, True)

    reloaded = _load_yaml(get_settings_path())
    assert reloaded["developer"]["someOtherKey"] == "keep-me"
    assert reloaded["developer"]["mode"] is True
    assert reloaded["developer"]["highlightFailedTools"] is True
    assert reloaded["developer"]["debug"] is True


def test_developer_settings_api_roundtrip(tmp_path, monkeypatch):
    # 隔离全局日志副作用:PUT 会按 debug 值切换进程级日志,测试里 stub 掉即可断言按值调用。
    calls: list[str] = []
    monkeypatch.setattr(
        "iac_code.utils.log.enable_debug_at_runtime",
        lambda session_id="web", **_kwargs: calls.append("enable"),
    )
    monkeypatch.setattr("iac_code.utils.log.disable_debug_at_runtime", lambda: calls.append("disable"))

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        assert client.get("/api/settings/developer").json() == {
            "mode": False,
            "highlightFailedTools": False,
            "debug": False,
        }

        put = client.put(
            "/api/settings/developer",
            json={"mode": True, "highlightFailedTools": True, "debug": True},
        )
        assert put.status_code == 200
        assert put.json() == {"mode": True, "highlightFailedTools": True, "debug": True}
        assert calls[-1] == "enable"

        assert client.get("/api/settings/developer").json() == {
            "mode": True,
            "highlightFailedTools": True,
            "debug": True,
        }

        put_off = client.put(
            "/api/settings/developer",
            json={"mode": True, "highlightFailedTools": True, "debug": False},
        )
        assert put_off.status_code == 200
        assert put_off.json()["debug"] is False
        assert calls[-1] == "disable"


def test_developer_settings_api_enables_debug_with_web_dedup_optin(tmp_path, monkeypatch):
    """Turning debug on via the Web endpoint must opt into the INFO-handler dedup path.

    Only the Web toggle passes replace_startup_info_handler=True; CLI/REPL/A2A/ACP keep
    the shared default. This guards that the endpoint wiring stays on the Web-only path.
    """
    captured: list[dict] = []
    monkeypatch.setattr(
        "iac_code.utils.log.enable_debug_at_runtime",
        lambda session_id="web", **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr("iac_code.utils.log.disable_debug_at_runtime", lambda: None)

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        put = client.put(
            "/api/settings/developer",
            json={"mode": False, "highlightFailedTools": False, "debug": True},
        )
        assert put.status_code == 200

    assert captured == [{"replace_startup_info_handler": True}]


def test_web_debug_toggle_writes_info_once_and_returns_to_info(tmp_path, monkeypatch):
    """Web debug path: INFO written once while on, DEBUG dropped after off, no stacking.

    Exercises the real log helper on the Web opt-in path (replace_startup_info_handler=True),
    mirroring what the /api/settings/developer toggle triggers. Repeated toggling must not
    stack same-file handlers or duplicate INFO.
    """
    from loguru import logger

    from iac_code.utils.log import disable_debug_at_runtime, enable_debug_at_runtime, setup_logging

    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    monkeypatch.delenv("DEBUG", raising=False)
    logger.remove()

    setup_logging(session_id="web", debug=False)

    # Debug on (Web opt-in): INFO must appear exactly once, not duplicated by the
    # leftover startup INFO handler.
    enable_debug_at_runtime("web", replace_startup_info_handler=True)
    logger.info("on-info")
    logger.complete()
    content = (tmp_path / "logs" / "web.log").read_text(encoding="utf-8")
    assert content.count("on-info") == 1

    # Repeated toggling must not stack handlers or duplicate INFO.
    for _ in range(3):
        disable_debug_at_runtime()
        enable_debug_at_runtime("web", replace_startup_info_handler=True)
    disable_debug_at_runtime()

    logger.debug("off-debug")
    logger.info("off-info")
    logger.complete()
    content = (tmp_path / "logs" / "web.log").read_text(encoding="utf-8")
    assert "off-debug" not in content
    assert content.count("off-info") == 1


def test_developer_settings_api_rejects_non_bool(tmp_path):
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        # 缺字段 / 非布尔都应被 required_bool 拒绝(400),不落库。
        missing = client.put("/api/settings/developer", json={"mode": True, "highlightFailedTools": True})
        assert missing.status_code == 400
        assert (
            client.put(
                "/api/settings/developer",
                json={"mode": "yes", "highlightFailedTools": False, "debug": False},
            ).status_code
            == 400
        )
        assert settings.developer_settings() == {"mode": False, "highlightFailedTools": False, "debug": False}
