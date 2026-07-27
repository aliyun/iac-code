"""售卖流水线「审查步骤」开关的 web 设置层 + HTTP API 测试。"""

import pytest
from starlette.testclient import TestClient

from iac_code.web import settings
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    yield


def test_settings_wrapper_defaults_false():
    assert settings.selling_review_step_settings() == {"enabled": False}


def test_settings_wrapper_roundtrip():
    assert settings.save_selling_review_step(True) == {"enabled": True}
    assert settings.selling_review_step_settings() == {"enabled": True}
    assert settings.save_selling_review_step(False) == {"enabled": False}
    assert settings.selling_review_step_settings() == {"enabled": False}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "proj"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    return TestClient(create_app(session_manager=manager))


def test_get_review_step_defaults(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/settings/pipeline-review-step")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_put_review_step_roundtrip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put("/api/settings/pipeline-review-step", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}

    resp2 = client.get("/api/settings/pipeline-review-step")
    assert resp2.json() == {"enabled": True}


def test_put_review_step_rejects_non_bool(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put("/api/settings/pipeline-review-step", json={"enabled": "yes"})
    assert resp.status_code == 400
