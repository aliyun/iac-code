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
    assert settings.developer_settings() == {"mode": False, "highlightFailedTools": False}


def test_save_developer_settings_roundtrip():
    result = settings.save_developer_settings(True, False)
    assert result == {"mode": True, "highlightFailedTools": False}
    assert settings.developer_settings() == {"mode": True, "highlightFailedTools": False}

    result2 = settings.save_developer_settings(False, True)
    assert result2 == {"mode": False, "highlightFailedTools": True}
    assert settings.developer_settings() == {"mode": False, "highlightFailedTools": True}


def test_save_developer_settings_preserves_other_keys():
    from iac_code.config import _load_yaml, _save_yaml, get_settings_path

    path = get_settings_path()
    data = _load_yaml(path)
    data.setdefault("developer", {})["someOtherKey"] = "keep-me"
    _save_yaml(path, data)

    settings.save_developer_settings(True, True)

    reloaded = _load_yaml(get_settings_path())
    assert reloaded["developer"]["someOtherKey"] == "keep-me"
    assert reloaded["developer"]["mode"] is True
    assert reloaded["developer"]["highlightFailedTools"] is True


def test_developer_settings_api_roundtrip(tmp_path):
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        assert client.get("/api/settings/developer").json() == {"mode": False, "highlightFailedTools": False}

        put = client.put("/api/settings/developer", json={"mode": True, "highlightFailedTools": True})
        assert put.status_code == 200
        assert put.json() == {"mode": True, "highlightFailedTools": True}

        assert client.get("/api/settings/developer").json() == {"mode": True, "highlightFailedTools": True}


def test_developer_settings_api_rejects_non_bool(tmp_path):
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        # 缺字段 / 非布尔都应被 required_bool 拒绝(400),不落库。
        assert client.put("/api/settings/developer", json={"mode": True}).status_code == 400
        assert client.put(
            "/api/settings/developer", json={"mode": "yes", "highlightFailedTools": False}
        ).status_code == 400
        assert settings.developer_settings() == {"mode": False, "highlightFailedTools": False}
