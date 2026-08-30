import pytest
from starlette.testclient import TestClient

from iac_code.config import _load_yaml, _save_yaml, get_settings_path
from iac_code.web import settings
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    # The stored pipeline default now falls back to the process-wide env choice;
    # keep the ambient environment out of these assertions.
    monkeypatch.delenv("IAC_CODE_PIPELINE_NAME", raising=False)
    yield


def test_session_defaults_fall_back():
    assert settings.get_session_defaults() == {
        "permissionMode": "default",
        "mode": "normal",
        "pipelineName": "selling",
    }


def test_save_session_defaults_roundtrip():
    saved = settings.save_session_defaults("accept_edits", "pipeline", "selling")
    assert saved == {"permissionMode": "accept_edits", "mode": "pipeline", "pipelineName": "selling"}
    assert settings.get_session_defaults() == {
        "permissionMode": "accept_edits",
        "mode": "pipeline",
        "pipelineName": "selling",
    }


def test_save_session_defaults_pipeline_name_defaults_when_blank():
    saved = settings.save_session_defaults("default", "normal", "   ")
    assert saved["pipelineName"] == "selling"


def test_save_session_defaults_rejects_unknown_permission():
    with pytest.raises(ValueError):
        settings.save_session_defaults("bogus", "normal")


def test_save_session_defaults_rejects_unknown_mode():
    with pytest.raises(ValueError):
        settings.save_session_defaults("default", "bogus")


def test_get_session_defaults_ignores_invalid_stored():
    path = get_settings_path()
    data = _load_yaml(path)
    data["sessionDefaults"] = {"permissionMode": "nope", "mode": "nope", "pipelineName": 7}
    _save_yaml(path, data)
    assert settings.get_session_defaults() == {
        "permissionMode": "default",
        "mode": "normal",
        "pipelineName": "selling",
    }


def test_get_session_defaults_falls_back_to_the_env_pipeline(monkeypatch):
    """用 IAC_CODE_PIPELINE_NAME 启动 Web 时,浏览器新会话草稿必须默认到同一条流水线。"""
    monkeypatch.setenv("IAC_CODE_PIPELINE_NAME", "selling_solution_first")
    assert settings.get_session_defaults()["pipelineName"] == "selling_solution_first"


def test_get_session_defaults_prefers_saved_pipeline_over_env(monkeypatch):
    """用户在设置里明确选过流水线时,该选择优先于进程 env。"""
    settings.save_session_defaults("default", "pipeline", "selling")
    monkeypatch.setenv("IAC_CODE_PIPELINE_NAME", "selling_solution_first")
    assert settings.get_session_defaults()["pipelineName"] == "selling"


def test_get_session_defaults_ignores_blank_env_pipeline(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_NAME", "   ")
    assert settings.get_session_defaults()["pipelineName"] == "selling"


def test_index_injects_env_pipeline_default(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_NAME", "selling_solution_first")
    client = _client(tmp_path)
    html = client.get("/").text
    assert 'data-default-pipeline-name="selling_solution_first"' in html


def test_save_session_defaults_preserves_other_keys():
    path = get_settings_path()
    data = _load_yaml(path)
    data.setdefault("sessionDefaults", {})["someOtherKey"] = "keep-me"
    _save_yaml(path, data)
    settings.save_session_defaults("dont_ask", "normal")
    reloaded = _load_yaml(get_settings_path())
    assert reloaded["sessionDefaults"]["someOtherKey"] == "keep-me"
    assert reloaded["sessionDefaults"]["permissionMode"] == "dont_ask"


def _client(tmp_path):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    return TestClient(create_app(session_manager=manager))


def test_get_session_defaults_endpoint_defaults(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/settings/session-defaults")
    assert resp.status_code == 200
    assert resp.json() == {"permissionMode": "default", "mode": "normal", "pipelineName": "selling"}


def test_put_session_defaults_roundtrip(tmp_path):
    client = _client(tmp_path)
    resp = client.put(
        "/api/settings/session-defaults",
        json={"permissionMode": "bypass_permissions", "mode": "pipeline"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "permissionMode": "bypass_permissions",
        "mode": "pipeline",
        "pipelineName": "selling",
    }
    assert client.get("/api/settings/session-defaults").json() == {
        "permissionMode": "bypass_permissions",
        "mode": "pipeline",
        "pipelineName": "selling",
    }


def test_put_session_defaults_rejects_unknown(tmp_path):
    client = _client(tmp_path)
    resp = client.put(
        "/api/settings/session-defaults",
        json={"permissionMode": "bogus", "mode": "normal"},
    )
    assert resp.status_code == 400


def test_index_injects_default_session_attributes(tmp_path):
    client = _client(tmp_path)
    html = client.get("/").text
    assert 'data-default-permission-mode="default"' in html
    assert 'data-default-mode="normal"' in html
    assert 'data-default-pipeline-name="selling"' in html


def test_index_injects_saved_session_defaults(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/settings/session-defaults",
        json={"permissionMode": "accept_edits", "mode": "pipeline"},
    )
    html = client.get("/").text
    assert 'data-default-permission-mode="accept_edits"' in html
    assert 'data-default-mode="pipeline"' in html
