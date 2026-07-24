from __future__ import annotations

import os
import stat

import pytest
from starlette.testclient import TestClient

from iac_code.web.session_manager import WebSessionManager


def _app(monkeypatch, tmp_path, project):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)

    from iac_code.web.app import create_app

    return create_app(session_manager=manager), manager


def _write_project_skill(project, name: str = "deploy", *, description: str = "Deploy fake stack") -> None:
    skill_dir = project / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "allowed_tools:\n"
        "  - shell\n"
        "model: qwen-test\n"
        "---\n\n"
        "Use fake project-only instructions.\n",
        encoding="utf-8",
    )


def test_get_memory_uses_session_cwd_and_returns_only_instruction_and_legacy_summaries(monkeypatch, tmp_path) -> None:
    default_project = tmp_path / "default"
    session_project = tmp_path / "session"
    other_project = tmp_path / "other"
    default_project.mkdir()
    session_project.mkdir()
    other_project.mkdir()
    (default_project / "AGENTS.md").write_text("default project instruction\n", encoding="utf-8")
    (session_project / "AGENTS.md").write_text("session project instruction\n", encoding="utf-8")
    (other_project / "AGENTS.md").write_text("other project secret\n", encoding="utf-8")
    app, manager = _app(monkeypatch, tmp_path, default_project)
    session = manager.create_session(cwd=str(session_project), session_id="session-1")

    from iac_code.config import get_config_dir
    from iac_code.memory.memory_manager import MemoryManager

    (get_config_dir() / "AGENTS.md").write_text("user instruction\n", encoding="utf-8")
    MemoryManager(str(get_config_dir() / "memory")).save(
        "project-hint",
        "legacy full content should not be returned",
        "project",
        "short legacy summary",
    )

    with TestClient(app) as client:
        response = client.get(f"/api/memory?sessionId={session.session_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["project"]["content"] == "session project instruction"
    assert data["user"]["content"] == "user instruction"
    assert data["autoMemoryEnabled"] is True
    assert data["legacy"] == [
        {
            "memoryId": "project-hint",
            "name": "project-hint",
            "description": "short legacy summary",
            "type": "project",
            "summary": "short legacy summary",
            "scope": "global",
        }
    ]
    assert "other project secret" not in response.text
    assert "legacy full content" not in response.text


def test_put_memory_project_and_user_persist_content_and_auto_setting(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    app, manager = _app(monkeypatch, tmp_path, project)
    session = manager.create_session(cwd=str(project), session_id="session-1")

    with TestClient(app) as client:
        project_response = client.put(
            "/api/memory/project",
            json={"sessionId": session.session_id, "content": "project rules\n"},
        )
        user_response = client.put("/api/memory/user", json={"content": "user rules\n"})
        auto_response = client.put("/api/memory/auto", json={"enabled": False})
        get_response = client.get("/api/memory")

    assert project_response.status_code == 200
    assert project_response.json()["content"] == "project rules\n"
    assert project_response.json()["updated"] is True
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "project rules\n"
    assert user_response.status_code == 200
    assert user_response.json()["content"] == "user rules\n"
    assert user_response.json()["updated"] is True

    from iac_code.config import get_config_dir

    user_path = get_config_dir() / "AGENTS.md"
    assert user_path.read_text(encoding="utf-8") == "user rules\n"
    if os.name != "nt":
        assert stat.S_IMODE(user_path.stat().st_mode) == 0o600
    assert auto_response.status_code == 200
    assert auto_response.json() == {"autoMemoryEnabled": False}
    assert get_response.status_code == 200
    assert get_response.json()["autoMemoryEnabled"] is False


@pytest.mark.skipif(os.name == "nt", reason="Symlink behavior varies on Windows")
def test_put_memory_user_rejects_symlinked_instruction_without_overwriting_target(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    app, _manager = _app(monkeypatch, tmp_path, project)

    from iac_code.config import get_config_dir

    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-user-agents.md"
    outside.write_text("original user rules\n", encoding="utf-8")
    (config_dir / "AGENTS.md").symlink_to(outside)

    with TestClient(app) as client:
        response = client.put("/api/memory/user", json={"content": "new user rules\n"})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "user memory path is invalid"}}
    assert outside.read_text(encoding="utf-8") == "original user rules\n"


def test_get_memory_projects_lists_launch_dir_and_session_projects_deduped(monkeypatch, tmp_path) -> None:
    launch_dir = tmp_path / "launch"
    session_project = tmp_path / "session"
    launch_dir.mkdir()
    session_project.mkdir()
    app, manager = _app(monkeypatch, tmp_path, launch_dir)
    manager.create_session(cwd=str(session_project), session_id="session-1")
    # A second session in the launch dir must not create a duplicate entry.
    manager.create_session(cwd=str(launch_dir), session_id="session-2")

    with TestClient(app) as client:
        response = client.get("/api/memory/projects")

    assert response.status_code == 200
    projects = response.json()["projects"]
    cwds = [item["cwd"] for item in projects]
    assert cwds.count(str(launch_dir.resolve())) == 1
    assert str(session_project.resolve()) in cwds
    current = [item for item in projects if item["current"]]
    assert len(current) == 1
    assert current[0]["cwd"] == str(launch_dir.resolve())


def test_memory_projects_skips_storage_only_slug_entries(monkeypatch, tmp_path) -> None:
    # A project storage folder with no listable sessions surfaces with cwd set to its
    # slug name (not an absolute path); it must not become a selectable memory target,
    # nor a phantom duplicate of the launch dir.
    from iac_code.web.memory import memory_projects, resolve_project_cwd

    launch_dir = tmp_path / "launch"
    real_project = tmp_path / "real"
    launch_dir.mkdir()
    real_project.mkdir()
    entries = [
        {"cwd": str(real_project), "label": "real"},
        {"cwd": "-Users-ehzyo-open-repo-iac-code3--worktrees-feature-web", "label": "feature-web"},
    ]

    projects = memory_projects(launch_dir, entries)
    cwds = [item["cwd"] for item in projects]

    assert cwds == [str(launch_dir.resolve()), str(real_project.resolve())]
    assert resolve_project_cwd("-Users-ehzyo-open-repo-iac-code3--worktrees-feature-web", launch_dir, entries) is None


def test_get_memory_accepts_known_cwd_and_rejects_unknown(monkeypatch, tmp_path) -> None:
    launch_dir = tmp_path / "launch"
    session_project = tmp_path / "session"
    outside = tmp_path / "outside"
    launch_dir.mkdir()
    session_project.mkdir()
    outside.mkdir()
    (session_project / "AGENTS.md").write_text("session project instruction\n", encoding="utf-8")
    (outside / "AGENTS.md").write_text("outside secret\n", encoding="utf-8")
    app, manager = _app(monkeypatch, tmp_path, launch_dir)
    manager.create_session(cwd=str(session_project), session_id="session-1")

    with TestClient(app) as client:
        known = client.get(f"/api/memory?cwd={session_project}")
        unknown = client.get(f"/api/memory?cwd={outside}")

    assert known.status_code == 200
    assert known.json()["project"]["content"] == "session project instruction"
    assert unknown.status_code == 404
    assert unknown.json() == {"error": {"message": "project not found"}}
    assert "outside secret" not in unknown.text


def test_put_memory_project_with_cwd_writes_selected_project_and_rejects_unknown(monkeypatch, tmp_path) -> None:
    launch_dir = tmp_path / "launch"
    session_project = tmp_path / "session"
    outside = tmp_path / "outside"
    launch_dir.mkdir()
    session_project.mkdir()
    outside.mkdir()
    app, manager = _app(monkeypatch, tmp_path, launch_dir)
    manager.create_session(cwd=str(session_project), session_id="session-1")

    with TestClient(app) as client:
        known = client.put(
            "/api/memory/project",
            json={"cwd": str(session_project), "content": "selected project rules\n"},
        )
        rejected = client.put(
            "/api/memory/project",
            json={"cwd": str(outside), "content": "should not be written\n"},
        )

    assert known.status_code == 200
    assert known.json()["path"] == str(session_project / "AGENTS.md")
    assert (session_project / "AGENTS.md").read_text(encoding="utf-8") == "selected project rules\n"
    assert rejected.status_code == 404
    assert rejected.json() == {"error": {"message": "project not found"}}
    assert not (outside / "AGENTS.md").exists()


@pytest.mark.skipif(os.name == "nt", reason="Symlink behavior varies on Windows")
def test_save_project_instruction_canonicalizes_symlinked_project_cwd(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    physical_project = tmp_path / "physical-project"
    physical_project.mkdir()
    linked_project = tmp_path / "linked-project"
    linked_project.symlink_to(physical_project, target_is_directory=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    from iac_code.web.memory import save_project_instruction

    response = save_project_instruction(linked_project, "project rules\n")

    assert response["path"] == str(physical_project / "AGENTS.md")
    assert (physical_project / "AGENTS.md").read_text(encoding="utf-8") == "project rules\n"


def test_memory_routes_reject_malformed_bodies_with_json(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    app, _manager = _app(monkeypatch, tmp_path, project)

    with TestClient(app) as client:
        malformed = client.put("/api/memory/project", content="{")
        wrong_content = client.put("/api/memory/user", json={"content": 123})
        wrong_auto = client.put("/api/memory/auto", json={"enabled": "yes"})

    assert malformed.status_code == 400
    assert malformed.json() == {"error": {"message": "malformed JSON request body"}}
    assert wrong_content.status_code == 400
    assert wrong_content.json() == {"error": {"message": "content must be a string"}}
    assert wrong_auto.status_code == 400
    assert wrong_auto.json() == {"error": {"message": "enabled must be a boolean"}}


def test_legacy_memory_search_and_delete_skip_unsafe_files(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    app, _manager = _app(monkeypatch, tmp_path, project)

    from iac_code.config import get_config_dir
    from iac_code.memory.memory_manager import MemoryManager

    memory_dir = get_config_dir() / "memory"
    manager = MemoryManager(str(memory_dir))
    manager.save("alpha", "alpha full text", "user", "alpha summary")
    manager.save("beta", "beta full text", "reference", "beta summary")
    unsafe_target = tmp_path / "unsafe.md"
    unsafe_target.write_text("---\nname: unsafe\ndescription: unsafe\ntype: user\n---\n\nunsafe", encoding="utf-8")
    (memory_dir / "unsafe.md").symlink_to(unsafe_target)

    with TestClient(app) as client:
        search_response = client.get("/api/memory/legacy?q=alpha")
        delete_response = client.delete("/api/memory/legacy/alpha")
        missing_response = client.delete("/api/memory/legacy/missing")

    assert search_response.status_code == 200
    assert search_response.json()["memories"] == [
        {
            "memoryId": "alpha",
            "name": "alpha",
            "description": "alpha summary",
            "type": "user",
            "summary": "alpha summary",
            "scope": "global",
        }
    ]
    assert "unsafe" not in search_response.text
    assert "full text" not in search_response.text
    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True, "memoryId": "alpha"}
    assert missing_response.status_code == 404
    assert missing_response.json() == {"deleted": False, "memoryId": "missing"}


def test_legacy_summaries_merge_project_and_global_scopes(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    from iac_code.config import get_config_dir
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.project_memory import get_project_memory_dir
    from iac_code.web.memory import legacy_memory_summaries

    MemoryManager(str(get_config_dir() / "memory")).save("g1", "global body", "user", "global summary")
    MemoryManager(str(get_project_memory_dir(str(project)))).save("p1", "project body", "project", "project summary")

    summaries = legacy_memory_summaries("", project)

    # Project-scoped entries come first, then global; each carries its own scope.
    assert [(item["memoryId"], item["scope"]) for item in summaries] == [("p1", "project"), ("g1", "global")]


def test_legacy_summaries_without_project_dir_return_only_global_and_create_nothing(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    from iac_code.config import get_config_dir
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.project_memory import get_project_memory_dir
    from iac_code.web.memory import legacy_memory_summaries

    MemoryManager(str(get_config_dir() / "memory")).save("g1", "global body", "user", "global summary")

    summaries = legacy_memory_summaries("", project)

    assert [(item["memoryId"], item["scope"]) for item in summaries] == [("g1", "global")]
    # Browsing a project with no memories must not leave an empty storage dir behind.
    assert not get_project_memory_dir(str(project)).exists()


def test_delete_legacy_memory_targets_only_the_requested_scope(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    from iac_code.config import get_config_dir
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.project_memory import get_project_memory_dir
    from iac_code.web.memory import delete_legacy_memory

    global_manager = MemoryManager(str(get_config_dir() / "memory"))
    project_manager = MemoryManager(str(get_project_memory_dir(str(project))))
    global_manager.save("shared", "global body", "user", "global summary")
    project_manager.save("shared", "project body", "project", "project summary")

    # Deleting the project scope leaves the identically-named global memory intact.
    assert delete_legacy_memory("shared", project, "project") is True
    assert project_manager.load("shared") is None
    assert global_manager.load("shared") is not None

    # And the global scope ignores the project dir.
    assert delete_legacy_memory("shared", project, "global") is True
    assert global_manager.load("shared") is None


def test_get_legacy_memory_with_cwd_includes_project_scope_and_rejects_unknown(monkeypatch, tmp_path) -> None:
    launch_dir = tmp_path / "launch"
    session_project = tmp_path / "session"
    outside = tmp_path / "outside"
    launch_dir.mkdir()
    session_project.mkdir()
    outside.mkdir()
    app, manager = _app(monkeypatch, tmp_path, launch_dir)
    manager.create_session(cwd=str(session_project), session_id="session-1")

    from iac_code.config import get_config_dir
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.project_memory import get_project_memory_dir

    MemoryManager(str(get_config_dir() / "memory")).save("g1", "global body", "user", "global summary")
    MemoryManager(str(get_project_memory_dir(str(session_project)))).save(
        "p1", "project body", "project", "project summary"
    )

    with TestClient(app) as client:
        known = client.get(f"/api/memory/legacy?cwd={session_project}")
        unknown = client.get(f"/api/memory/legacy?cwd={outside}")

    assert known.status_code == 200
    scopes = {item["memoryId"]: item["scope"] for item in known.json()["memories"]}
    assert scopes == {"p1": "project", "g1": "global"}
    assert unknown.status_code == 404
    assert unknown.json() == {"error": {"message": "project not found"}}


def test_delete_legacy_memory_route_scopes_to_project_dir(monkeypatch, tmp_path) -> None:
    launch_dir = tmp_path / "launch"
    session_project = tmp_path / "session"
    launch_dir.mkdir()
    session_project.mkdir()
    app, manager = _app(monkeypatch, tmp_path, launch_dir)
    manager.create_session(cwd=str(session_project), session_id="session-1")

    from iac_code.config import get_config_dir
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.project_memory import get_project_memory_dir

    global_manager = MemoryManager(str(get_config_dir() / "memory"))
    project_manager = MemoryManager(str(get_project_memory_dir(str(session_project))))
    global_manager.save("shared", "global body", "user", "global summary")
    project_manager.save("shared", "project body", "project", "project summary")

    with TestClient(app) as client:
        deleted = client.delete(f"/api/memory/legacy/shared?scope=project&cwd={session_project}")

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "memoryId": "shared"}
    assert project_manager.load("shared") is None
    assert global_manager.load("shared") is not None


def test_get_skills_lists_bundled_and_session_project_skills(monkeypatch, tmp_path) -> None:
    default_project = tmp_path / "default"
    session_project = tmp_path / "session"
    default_project.mkdir()
    session_project.mkdir()
    _write_project_skill(session_project, "deploy")
    app, manager = _app(monkeypatch, tmp_path, default_project)
    session = manager.create_session(cwd=str(session_project), session_id="session-1")

    with TestClient(app) as client:
        response = client.get(f"/api/skills?sessionId={session.session_id}")

    assert response.status_code == 200
    skills = response.json()["skills"]
    deploy = next(skill for skill in skills if skill["name"] == "deploy")
    assert deploy["description"] == "Deploy fake stack"
    assert deploy["source"] == "project"
    assert deploy["path"].endswith("/skills/deploy")
    assert deploy["enabled"] is True
    assert deploy["locked"] is False
    assert deploy["commandAvailable"] is True
    assert deploy["modelInvocable"] is True
    assert isinstance(deploy["contentLength"], int)
    assert deploy["contentLength"] > 0
    bundled = [skill for skill in skills if skill["source"] == "bundled"]
    assert bundled
    assert all(skill["locked"] for skill in bundled)
    iac_aliyun = next(skill for skill in skills if skill["name"] == "iac-aliyun")
    assert iac_aliyun["commandAvailable"] is False
    assert iac_aliyun["modelInvocable"] is True


def test_get_skills_accepts_known_cwd_and_rejects_unknown(monkeypatch, tmp_path) -> None:
    default_project = tmp_path / "default"
    session_project = tmp_path / "session"
    outside = tmp_path / "outside"
    default_project.mkdir()
    session_project.mkdir()
    outside.mkdir()
    _write_project_skill(session_project, "deploy")
    _write_project_skill(outside, "outside-skill", description="should not leak")
    app, manager = _app(monkeypatch, tmp_path, default_project)
    manager.create_session(cwd=str(session_project), session_id="session-1")

    with TestClient(app) as client:
        known = client.get(f"/api/skills?cwd={session_project}")
        unknown = client.get(f"/api/skills?cwd={outside}")

    assert known.status_code == 200
    names = {skill["name"] for skill in known.json()["skills"]}
    assert "deploy" in names
    assert "outside-skill" not in names
    assert unknown.status_code == 404
    assert unknown.json() == {"error": {"message": "project not found"}}
    assert "outside-skill" not in unknown.text


def test_put_disabled_skills_with_cwd_uses_selected_project(monkeypatch, tmp_path) -> None:
    default_project = tmp_path / "default"
    session_project = tmp_path / "session"
    outside = tmp_path / "outside"
    default_project.mkdir()
    session_project.mkdir()
    outside.mkdir()
    _write_project_skill(session_project, "deploy")
    app, manager = _app(monkeypatch, tmp_path, default_project)
    manager.create_session(cwd=str(session_project), session_id="session-1")

    with TestClient(app) as client:
        known = client.put(
            "/api/skills/disabled",
            json={"cwd": str(session_project), "disabled": ["deploy"]},
        )
        rejected = client.put(
            "/api/skills/disabled",
            json={"cwd": str(outside), "disabled": ["deploy"]},
        )

    assert known.status_code == 200
    deploy = next(skill for skill in known.json()["skills"] if skill["name"] == "deploy")
    assert deploy["enabled"] is False
    assert rejected.status_code == 404
    assert rejected.json() == {"error": {"message": "project not found"}}


def test_put_disabled_skills_persists_only_unlocked_skills_and_updates_suggestions(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_project_skill(project, "deploy")
    app, _manager = _app(monkeypatch, tmp_path, project)

    with TestClient(app) as client:
        before = client.get("/api/suggestions?kind=skill&q=dep")
        response = client.put("/api/skills/disabled", json={"disabled": ["deploy", "iac-aliyun"]})
        after = client.get("/api/suggestions?kind=skill&q=dep")
        bundled_after = client.get("/api/suggestions?kind=skill&q=iac")

    assert before.status_code == 200
    assert before.json()["suggestions"]
    assert response.status_code == 200
    deploy = next(skill for skill in response.json()["skills"] if skill["name"] == "deploy")
    assert deploy["enabled"] is False
    iac_aliyun = next(skill for skill in response.json()["skills"] if skill["name"] == "iac-aliyun")
    assert iac_aliyun["locked"] is True
    assert iac_aliyun["enabled"] is True
    assert after.status_code == 200
    assert after.json()["suggestions"] == []
    assert bundled_after.status_code == 200
    assert bundled_after.json()["suggestions"]


@pytest.mark.skipif(os.name == "nt", reason="Symlink behavior varies on Windows")
def test_put_disabled_skills_rejects_symlinked_settings_without_overwriting_target(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_project_skill(project, "deploy")
    app, _manager = _app(monkeypatch, tmp_path, project)

    from iac_code.config import get_config_dir

    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-settings.yml"
    outside.write_text("activeProvider: openai\n", encoding="utf-8")
    (config_dir / "settings.yml").symlink_to(outside)

    with TestClient(app) as client:
        response = client.put("/api/skills/disabled", json={"disabled": ["deploy"]})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "settings path is invalid"}}
    assert outside.read_text(encoding="utf-8") == "activeProvider: openai\n"


def test_put_disabled_skills_uses_session_cwd_for_returned_skill_list(monkeypatch, tmp_path) -> None:
    default_project = tmp_path / "default"
    session_project = tmp_path / "session"
    default_project.mkdir()
    session_project.mkdir()
    _write_project_skill(session_project, "deploy")
    app, manager = _app(monkeypatch, tmp_path, default_project)
    session = manager.create_session(cwd=str(session_project), session_id="session-1")

    with TestClient(app) as client:
        response = client.put(
            "/api/skills/disabled",
            json={"sessionId": session.session_id, "disabled": ["deploy"]},
        )

    assert response.status_code == 200
    deploy = next(skill for skill in response.json()["skills"] if skill["name"] == "deploy")
    assert deploy["enabled"] is False


def test_put_disabled_skills_rejects_bad_body(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    app, _manager = _app(monkeypatch, tmp_path, project)

    with TestClient(app) as client:
        response = client.put("/api/skills/disabled", json={"disabled": [123]})
        missing = client.put("/api/skills/disabled", json={})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "disabled must be a list of strings"}}
    assert missing.status_code == 400
    assert missing.json() == {"error": {"message": "disabled is required"}}
