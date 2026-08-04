import pytest
from starlette.testclient import TestClient

from iac_code.web import settings
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    yield


def test_telemetry_settings_default_false():
    assert settings.telemetry_settings() == {"shareContent": False}


def test_save_telemetry_settings_roundtrip():
    assert settings.save_telemetry_settings(True) == {"shareContent": True}
    assert settings.telemetry_settings() == {"shareContent": True}

    assert settings.save_telemetry_settings(False) == {"shareContent": False}
    assert settings.telemetry_settings() == {"shareContent": False}


def test_save_telemetry_settings_preserves_other_keys():
    from iac_code.config import _load_yaml, _save_yaml, get_settings_path

    path = get_settings_path()
    data = _load_yaml(path)
    data.setdefault("telemetry", {})["someOtherKey"] = "keep-me"
    _save_yaml(path, data)

    settings.save_telemetry_settings(True)

    reloaded = _load_yaml(get_settings_path())
    assert reloaded["telemetry"]["someOtherKey"] == "keep-me"
    assert reloaded["telemetry"]["shareContent"] is True


def test_telemetry_settings_api_roundtrip(tmp_path, monkeypatch):
    # 隔离进程级运行时覆盖:PUT 会按 shareContent 值调用 set_content_capture_optin,断言按值调用。
    calls: list[bool] = []
    monkeypatch.setattr(
        "iac_code.services.telemetry.config.set_content_capture_optin",
        lambda enabled: calls.append(enabled),
    )

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        assert client.get("/api/settings/telemetry").json() == {"shareContent": False}

        put = client.put("/api/settings/telemetry", json={"shareContent": True})
        assert put.status_code == 200
        assert put.json() == {"shareContent": True}
        assert calls[-1] is True

        assert client.get("/api/settings/telemetry").json() == {"shareContent": True}

        put_off = client.put("/api/settings/telemetry", json={"shareContent": False})
        assert put_off.status_code == 200
        assert put_off.json()["shareContent"] is False
        assert calls[-1] is False


def test_telemetry_settings_api_rejects_non_bool(tmp_path):
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        # 缺字段 / 非布尔都应被 required_bool 拒绝(400),不落库。
        missing = client.put("/api/settings/telemetry", json={})
        assert missing.status_code == 400
        assert client.put("/api/settings/telemetry", json={"shareContent": "yes"}).status_code == 400
        assert settings.telemetry_settings() == {"shareContent": False}
