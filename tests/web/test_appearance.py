import pytest
from starlette.testclient import TestClient

from iac_code.config import _load_yaml, _save_yaml, get_settings_path
from iac_code.web import settings
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    yield


def test_appearance_theme_defaults_graphite():
    assert settings.get_appearance_theme() == "graphite"


def test_save_appearance_theme_roundtrip():
    assert settings.save_appearance_theme("midnight") == {"theme": "midnight"}
    assert settings.get_appearance_theme() == "midnight"


def test_save_appearance_theme_rejects_unknown():
    with pytest.raises(ValueError):
        settings.save_appearance_theme("bogus")


def test_get_appearance_theme_ignores_invalid_stored():
    path = get_settings_path()
    data = _load_yaml(path)
    data["appearance"] = {"theme": "nope"}
    _save_yaml(path, data)
    assert settings.get_appearance_theme() == "graphite"


def test_save_appearance_preserves_other_keys():
    path = get_settings_path()
    data = _load_yaml(path)
    data.setdefault("appearance", {})["someOtherKey"] = "keep-me"
    _save_yaml(path, data)
    settings.save_appearance_theme("sepia")
    reloaded = _load_yaml(get_settings_path())
    assert reloaded["appearance"]["someOtherKey"] == "keep-me"
    assert reloaded["appearance"]["theme"] == "sepia"


def _client(tmp_path):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    return TestClient(create_app(session_manager=manager))


def test_get_appearance_defaults(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/settings/appearance")
    assert resp.status_code == 200
    assert resp.json() == {"theme": "graphite"}


def test_put_appearance_roundtrip(tmp_path):
    client = _client(tmp_path)
    resp = client.put("/api/settings/appearance", json={"theme": "evergreen"})
    assert resp.status_code == 200
    assert resp.json() == {"theme": "evergreen"}
    assert client.get("/api/settings/appearance").json() == {"theme": "evergreen"}


def test_put_appearance_rejects_unknown(tmp_path):
    client = _client(tmp_path)
    resp = client.put("/api/settings/appearance", json={"theme": "bogus"})
    assert resp.status_code == 400


def test_index_default_theme_graphite(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.web.app.resolve_ui_language", lambda override: "en")
    client = _client(tmp_path)
    html = client.get("/").text
    assert '<html lang="en" data-theme="graphite">' in html


def test_index_injects_saved_theme(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.web.app.resolve_ui_language", lambda override: "en")
    client = _client(tmp_path)
    client.put("/api/settings/appearance", json={"theme": "midnight"})
    html = client.get("/").text
    assert '<html lang="en" data-theme="midnight">' in html
    assert '<html lang="zh-CN">' not in html
