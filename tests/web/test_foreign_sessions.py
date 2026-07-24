from pathlib import Path

import pytest
from starlette.testclient import TestClient

from iac_code.web import settings
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    yield


def test_foreign_visibility_defaults_false():
    assert settings.is_foreign_pipeline_visible() is False
    assert settings.is_foreign_normal_visible() is False


def test_save_foreign_sessions_visibility_roundtrip():
    result = settings.save_foreign_sessions_visibility(True, False)
    assert result == {"showPipeline": True, "showNormal": False}
    assert settings.is_foreign_pipeline_visible() is True
    assert settings.is_foreign_normal_visible() is False

    result2 = settings.save_foreign_sessions_visibility(False, True)
    assert result2 == {"showPipeline": False, "showNormal": True}
    assert settings.is_foreign_pipeline_visible() is False
    assert settings.is_foreign_normal_visible() is True


def test_save_foreign_sessions_preserves_other_keys(monkeypatch, tmp_path):
    # 先写入一个同段无关键,确认读改写不丢它。
    from iac_code.config import _load_yaml, _save_yaml, get_settings_path

    path = get_settings_path()
    data = _load_yaml(path)
    data.setdefault("foreignSessions", {})["someOtherKey"] = "keep-me"
    _save_yaml(path, data)

    settings.save_foreign_sessions_visibility(True, True)

    reloaded = _load_yaml(get_settings_path())
    assert reloaded["foreignSessions"]["someOtherKey"] == "keep-me"
    assert reloaded["foreignSessions"]["showPipeline"] is True
    assert reloaded["foreignSessions"]["showNormal"] is True


def _seed_foreign(manager, *, mode):
    """用 manager 建会话后删掉 web-session.json,使其成为「外来」;pipeline 追加 display.jsonl。"""
    session = manager.create_session(cwd=str(Path.cwd()), mode=mode)
    session_dir = manager.storage.session_dir(session.cwd, session.session_id)
    (session_dir / "web-session.json").unlink()
    if mode == "pipeline":
        replay = session_dir / "pipeline"
        replay.mkdir(parents=True, exist_ok=True)
        (replay / "display.jsonl").write_text("{}\n", encoding="utf-8")
    return session.cwd, session.session_id


def _fresh_manager(projects_dir):
    return WebSessionManager(projects_dir=projects_dir)


def test_foreign_pipeline_marked_read_only(tmp_path):
    projects = tmp_path / "projects"
    seed_mgr = WebSessionManager(projects_dir=projects)
    cwd, sid = _seed_foreign(seed_mgr, mode="pipeline")

    mgr = _fresh_manager(projects)
    entry = next(e for e in mgr.index.list_for_cwd(cwd) if e.session_id == sid)
    session = mgr._from_entry(entry)
    assert session.read_only is True
    assert session.mode == "pipeline"
    assert session.to_dict()["readOnly"] is True
    # 有可点击兜底标题
    assert session.title and session.title != "(empty)"


def test_foreign_normal_not_read_only(tmp_path):
    projects = tmp_path / "projects"
    seed_mgr = WebSessionManager(projects_dir=projects)
    cwd, sid = _seed_foreign(seed_mgr, mode="normal")

    mgr = _fresh_manager(projects)
    entry = next(e for e in mgr.index.list_for_cwd(cwd) if e.session_id == sid)
    session = mgr._from_entry(entry)
    assert session.read_only is False
    assert session.to_dict()["readOnly"] is False
    assert session.title and session.title != "(empty)"


def test_web_pipeline_not_foreign_not_read_only(tmp_path):
    projects = tmp_path / "projects"
    mgr = WebSessionManager(projects_dir=projects)
    session = mgr.create_session(cwd=str(Path.cwd()), mode="pipeline")
    got = mgr.get_session(session.session_id)
    assert got.read_only is False
    assert got.to_dict()["readOnly"] is False


def _titles(sessions):
    return {s.title for s in sessions}


def _seed_all_four(projects_dir):
    """Seed two web sessions (renamed to stable titles) plus a foreign normal and
    foreign pipeline session, so visibility filtering can be asserted end to end."""
    mgr = WebSessionManager(projects_dir=projects_dir)
    wn = mgr.create_session(cwd=str(Path.cwd()), mode="normal")
    wp = mgr.create_session(cwd=str(Path.cwd()), mode="pipeline")
    # 给 web 会话可列出的标题(否则 (empty) 会被现有门槛挡掉,干扰断言)。
    mgr.rename_session(wn.session_id, "web-normal-title")
    mgr.rename_session(wp.session_id, "web-pipeline-title")
    _fn_cwd, fn_sid = _seed_foreign(mgr, mode="normal")
    _fp_cwd, fp_sid = _seed_foreign(mgr, mode="pipeline")
    return wn.session_id, wp.session_id, fn_sid, fp_sid


def test_list_hides_foreign_by_default(tmp_path):
    projects = tmp_path / "projects"
    _seed_all_four(projects)

    mgr = WebSessionManager(projects_dir=projects)
    sessions, _total = mgr.list_sessions_page(limit=100)
    # 仅两类 web 会话:全部非只读,含两个已命名标题,无外来兜底标题。
    assert all(not s.read_only for s in sessions)
    titles = _titles(sessions)
    assert "web-normal-title" in titles
    assert "web-pipeline-title" in titles
    assert not any(t.startswith("Pipeline · ") for t in titles)
    assert not any(t.startswith("Session · ") for t in titles)


def test_list_shows_foreign_pipeline_when_enabled(tmp_path):
    projects = tmp_path / "projects"
    _seed_all_four(projects)
    settings.save_foreign_sessions_visibility(True, False)

    mgr = WebSessionManager(projects_dir=projects)
    sessions, _ = mgr.list_sessions_page(limit=100)
    titles = _titles(sessions)
    assert any(t.startswith("Pipeline · ") for t in titles)  # 外来 pipeline 出现
    assert not any(t.startswith("Session · ") for t in titles)  # 外来普通仍隐藏


def test_list_shows_foreign_normal_when_enabled(tmp_path):
    projects = tmp_path / "projects"
    _seed_all_four(projects)
    settings.save_foreign_sessions_visibility(False, True)

    mgr = WebSessionManager(projects_dir=projects)
    sessions, _ = mgr.list_sessions_page(limit=100)
    titles = _titles(sessions)
    assert any(t.startswith("Session · ") for t in titles)  # 外来普通出现
    assert not any(t.startswith("Pipeline · ") for t in titles)  # 外来 pipeline 仍隐藏


def test_search_applies_same_predicate(tmp_path):
    projects = tmp_path / "projects"
    _seed_all_four(projects)

    mgr = WebSessionManager(projects_dir=projects)
    results, _total = mgr.search_sessions("")  # 关闭时外来不应出现
    titles = {r.get("title") for r in results}
    assert not any((t or "").startswith("Pipeline · ") for t in titles)
    assert not any((t or "").startswith("Session · ") for t in titles)
    # web 会话仍可搜索到。
    assert "web-normal-title" in titles
    assert "web-pipeline-title" in titles


def test_search_shows_foreign_pipeline_when_enabled(tmp_path):
    projects = tmp_path / "projects"
    _seed_all_four(projects)
    settings.save_foreign_sessions_visibility(True, False)

    mgr = WebSessionManager(projects_dir=projects)
    results, _total = mgr.search_sessions("")
    titles = {r.get("title") for r in results}
    assert any((t or "").startswith("Pipeline · ") for t in titles)
    assert not any((t or "").startswith("Session · ") for t in titles)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "proj"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    app = create_app(session_manager=manager)
    return TestClient(app)


def test_get_foreign_settings_defaults(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/settings/foreign-sessions")
    assert resp.status_code == 200
    assert resp.json() == {"showPipeline": False, "showNormal": False}


def test_put_foreign_settings_roundtrip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put(
        "/api/settings/foreign-sessions",
        json={"showPipeline": True, "showNormal": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"showPipeline": True, "showNormal": False}

    resp2 = client.get("/api/settings/foreign-sessions")
    assert resp2.json() == {"showPipeline": True, "showNormal": False}


def test_put_foreign_settings_rejects_non_bool(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put(
        "/api/settings/foreign-sessions",
        json={"showPipeline": "yes", "showNormal": False},
    )
    assert resp.status_code == 400


def test_is_session_read_only_matrix(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    projects = tmp_path / "projects"
    seed = WebSessionManager(projects_dir=projects)
    fp_cwd, fp_sid = _seed_foreign(seed, mode="pipeline")
    fn_cwd, fn_sid = _seed_foreign(seed, mode="normal")
    web = seed.create_session(cwd=str(Path.cwd()), mode="normal")

    mgr = WebSessionManager(projects_dir=projects)

    def sess(cwd, sid):
        entry = next(e for e in mgr.index.list_for_cwd(cwd) if e.session_id == sid)
        return mgr._from_entry(entry)

    # 外来 pipeline 恒只读
    settings.save_foreign_sessions_visibility(True, True)
    assert mgr.is_session_read_only(sess(fp_cwd, fp_sid)) is True
    # 外来普通:开关② 开 → 可写;关 → 只读
    assert mgr.is_session_read_only(sess(fn_cwd, fn_sid)) is False
    settings.save_foreign_sessions_visibility(True, False)
    assert mgr.is_session_read_only(sess(fn_cwd, fn_sid)) is True
    # web 会话恒可写
    assert mgr.is_session_read_only(mgr.get_session(web.session_id)) is False


def test_post_message_blocks_foreign_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "proj"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=str(project))
    # cwd 须与 manager 一致,故不用 _seed_foreign(它用 Path.cwd());手动构造外来 pipeline。
    session = manager.create_session(cwd=str(project), mode="pipeline")
    fp_sid = session.session_id
    session_dir = manager.storage.session_dir(session.cwd, session.session_id)
    (session_dir / "web-session.json").unlink()
    replay = session_dir / "pipeline"
    replay.mkdir(parents=True, exist_ok=True)
    (replay / "display.jsonl").write_text("{}\n", encoding="utf-8")

    client = TestClient(create_app(session_manager=manager))
    resp = client.post(f"/api/sessions/{fp_sid}/messages", json={"prompt": "hi"})
    assert resp.status_code == 409
    body = resp.json()
    # json_error 将 code 嵌套在 error 下:{"error": {"code": ..., "message": ...}}
    assert body["error"]["code"] == "foreign_read_only"
