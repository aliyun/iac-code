from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from iac_code.web.session_manager import WebSessionManager

VALID_PNG = b"\x89PNG\r\n\x1a\npng-data"


def _manager(tmp_path):
    return WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")


def _dispatcher(tmp_path):
    from iac_code.web.commands import WebCommandDispatcher

    manager = _manager(tmp_path)
    return manager, WebCommandDispatcher(manager)


def _web_metadata_path(manager: WebSessionManager, cwd: str, session_id: str):
    return manager.storage.session_dir(cwd, session_id) / "web-session.json"


def test_status_command_returns_local_session_status(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(session_id="session-1")
    manager.add_permission_request(
        session,
        {
            "action": "shell",
            "command": "echo accessKeySecret=super-secret-value",
            "apiKey": "sk-unsafe12345678",
        },
    )

    response = dispatcher.dispatch(session.session_id, "/status")

    assert response["accepted"] is True
    assert response["command"] == "status"
    assert response["status"]["sessionId"] == session.session_id
    assert response["status"]["mode"] == "normal"
    assert response["status"]["pendingPermissionCount"] == 1
    assert response["status"]["pendingPermissions"][0]["payload"]["apiKey"] == "sk-unsafe12345678"
    assert "super-secret-value" in str(response)
    assert "sk-unsafe" in str(response)


def test_mcp_command_returns_server_listing(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(session_id="session-mcp")

    response = dispatcher.dispatch(session.session_id, "/mcp")

    assert response["accepted"] is True
    assert response["command"] == "mcp"
    assert isinstance(response["mcp"]["servers"], list)
    assert isinstance(response["mcp"]["warnings"], list)


def test_shell_escape_is_local_and_does_not_enter_agent_context(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    response = dispatcher.dispatch(session.session_id, "!echo hi")

    assert response == {
        "accepted": True,
        "command": "shell_escape",
        "local": True,
        "entersAgentContext": False,
        "shell": "echo hi",
    }


def test_pipeline_mode_allows_shell_escape_when_pipeline_policy_opts_in(tmp_path) -> None:
    from iac_code.pipeline.engine.step_spec import AllowUserEscapes

    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(mode="pipeline", allow_user_escapes=AllowUserEscapes(shell=True))

    response = dispatcher.dispatch(session.session_id, "!echo hi")

    assert response == {
        "accepted": True,
        "command": "shell_escape",
        "local": True,
        "entersAgentContext": False,
        "shell": "echo hi",
    }


def test_session_create_route_accepts_pipeline_escape_policy_for_shell_commands(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)

    class FakeShellRunner:
        async def run(self, web_session, command: str):
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "fake-shell",
                    "toolUseId": "fake-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "fake-shell",
                    "toolUseId": "fake-shell",
                    "command": command,
                    "exitCode": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "local": True,
                    "entersAgentContext": False,
                },
            )

    app = create_app(session_manager=manager, shell_runner_factory=lambda: FakeShellRunner())

    with TestClient(app) as client:
        created = client.post(
            "/api/sessions",
            json={
                "sessionId": "pipeline-shell",
                "mode": "pipeline",
                "allowUserEscapes": {"shell": True},
            },
        )
        command = client.post("/api/sessions/pipeline-shell/commands", json={"command": "!echo hi"})

    assert created.status_code == 201
    assert created.json()["allowUserEscapes"] == {"skill": False, "command": False, "shell": True}
    assert command.status_code == 200
    assert command.json()["command"] == "shell_escape"


def test_skill_escape_enters_agent_context(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    response = dispatcher.dispatch(session.session_id, "$skill arg")

    assert response == {
        "accepted": True,
        "command": "skill",
        "skill": "skill arg",
        "entersAgentContext": True,
    }


@pytest.mark.parametrize(
    ("command_text", "error_code", "message"),
    [
        ("!", "empty_shell_escape", "Usage: !<command>"),
        ("!   ", "empty_shell_escape", "Usage: !<command>"),
        ("$", "empty_skill_command", "Usage: $<skill> [args]"),
        ("$   ", "empty_skill_command", "Usage: $<skill> [args]"),
    ],
)
def test_empty_user_escape_commands_are_rejected_in_normal_mode(
    tmp_path, command_text: str, error_code: str, message: str
) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    response = dispatcher.dispatch(session.session_id, command_text)

    assert response == {
        "accepted": False,
        "error": {
            "code": error_code,
            "message": message,
        },
    }


def test_pipeline_mode_blocks_model_but_allows_status_and_prompt(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(mode="pipeline")

    blocked = dispatcher.dispatch(session.session_id, "/model gpt-test")
    status = dispatcher.dispatch(session.session_id, "/status")
    prompt = dispatcher.dispatch(session.session_id, "/prompt")

    assert blocked == {
        "accepted": False,
        "command": "model",
        "error": {
            "code": "command_not_allowed_in_pipeline",
            "message": "/model is not available in pipeline mode",
        },
    }
    assert status["accepted"] is True
    assert status["command"] == "status"
    assert prompt == {
        "accepted": True,
        "command": "prompt",
        "action": "show_prompt_snapshot",
    }


@pytest.mark.parametrize(
    "command_text",
    [
        "/quit",
        "/q",
        "/?",
    ],
)
def test_pipeline_mode_blocks_non_safe_terminal_aliases(tmp_path, command_text: str) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(mode="pipeline")

    response = dispatcher.dispatch(session.session_id, command_text)

    command = command_text.removeprefix("/")
    assert response == {
        "accepted": False,
        "command": command,
        "error": {
            "code": "command_not_allowed_in_pipeline",
            "message": f"/{command} is not available in pipeline mode",
        },
    }


@pytest.mark.parametrize(
    "command_text",
    [
        "$iac_aliyun",
    ],
)
def test_pipeline_mode_blocks_user_escape_shortcuts_by_default(tmp_path, command_text: str) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(mode="pipeline")

    response = dispatcher.dispatch(session.session_id, command_text)

    assert response == {
        "accepted": False,
        "error": {
            "code": "user_escape_not_allowed_in_pipeline",
            "message": "user escape commands are not available in pipeline mode",
        },
    }


def test_pipeline_mode_blocks_shell_escape_with_pipeline_command_error(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(mode="pipeline")

    response = dispatcher.dispatch(session.session_id, "!echo hi")

    assert response == {
        "accepted": False,
        "error": {
            "code": "command_not_allowed_in_pipeline",
            "message": "shell escape commands are not available in pipeline mode",
        },
    }


@pytest.mark.parametrize(
    ("command_text", "expected"),
    [
        ("/compact", {"accepted": True, "command": "compact", "action": "compact_session"}),
        ("/resume", {"accepted": True, "command": "resume", "action": "resume"}),
        (
            "/prompt",
            {
                "accepted": True,
                "command": "prompt",
                "action": "show_prompt_snapshot",
            },
        ),
        ("/model", {"accepted": True, "command": "model", "action": "open_model_selector"}),
        ("/effort high", {"accepted": True, "command": "effort", "action": "open_effort_selector"}),
        ("/auth", {"accepted": True, "command": "auth", "action": "open_settings", "panel": "provider"}),
        ("/login", {"accepted": True, "command": "login", "action": "open_settings", "panel": "provider"}),
        ("/memory", {"accepted": True, "command": "memory", "action": "open_panel", "panel": "memory"}),
        ("/memory-folder", {"accepted": True, "command": "memory-folder", "action": "open_panel", "panel": "memory"}),
        ("/skills", {"accepted": True, "command": "skills", "action": "open_panel", "panel": "skills"}),
        ("/exit", {"accepted": True, "command": "exit", "action": "close_session_runtime"}),
        ("/quit", {"accepted": True, "command": "quit", "action": "close_session_runtime"}),
        ("/q", {"accepted": True, "command": "q", "action": "close_session_runtime"}),
    ],
)
def test_explicit_commands_return_accepted_payloads(tmp_path, command_text: str, expected: dict[str, object]) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    assert dispatcher.dispatch(session.session_id, command_text) == expected


def test_help_commands_return_command_metadata(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    help_response = dispatcher.dispatch(session.session_id, "/help")
    short_help_response = dispatcher.dispatch(session.session_id, "/?")

    assert help_response["accepted"] is True
    assert help_response["command"] == "help"
    assert help_response["commands"][0]["name"] == "/status"
    assert {command["name"] for command in help_response["commands"]} >= {"/status", "/prompt", "/skills"}
    assert short_help_response == help_response


def test_rename_returns_requested_title_and_updates_session(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(session_id="rename-me")

    response = dispatcher.dispatch(session.session_id, "/rename production-stack")

    assert response == {
        "accepted": True,
        "command": "rename",
        "sessionId": session.session_id,
        "title": "production-stack",
        "action": "rename_session",
    }
    assert session.title == "production-stack"
    assert session.events.replay_after(0)[0]["type"] == "session.updated"
    assert session.events.replay_after(0)[0]["payload"]["title"] == "production-stack"
    assert manager.storage.read_metadata(session.cwd, session.session_id).name == "production-stack"
    assert [entry.title for entry in manager.list_sessions()] == ["production-stack"]


def test_rename_preserves_existing_git_branch_metadata(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(session_id="rename-branch")
    manager.storage.rename_session(session.cwd, session.session_id, "old-name", git_branch="main")

    response = dispatcher.dispatch(session.session_id, "/rename production-stack")

    metadata = manager.storage.read_metadata(session.cwd, session.session_id)
    assert response["accepted"] is True
    assert response["title"] == "production-stack"
    assert metadata is not None
    assert metadata.name == "production-stack"
    assert metadata.git_branch == "main"


def test_same_session_id_across_projects_are_distinct_and_rename_by_web_session_id(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    first_cwd = str(tmp_path / "project-a")
    second_cwd = str(tmp_path / "project-b")
    first = manager.create_session(cwd=first_cwd, session_id="same-id")
    second = manager.create_session(cwd=second_cwd, session_id="same-id")
    manager.storage.rename_session(first_cwd, "same-id", "first-name", git_branch=None)
    manager.storage.rename_session(second_cwd, "same-id", "second-name", git_branch=None)

    listed = sorted(manager.list_sessions(), key=lambda item: item.cwd)

    assert first is not second
    assert first.web_session_id != second.web_session_id
    assert [session["sessionId"] for session in [first.to_dict(), second.to_dict()]] == ["same-id", "same-id"]
    assert [session["webSessionId"] for session in [first.to_dict(), second.to_dict()]] == [
        first.web_session_id,
        second.web_session_id,
    ]
    assert [(session.cwd, session.title) for session in listed] == [
        (first_cwd, "first-name"),
        (second_cwd, "second-name"),
    ]

    first_response = dispatcher.dispatch(first.web_session_id, "/rename first-renamed")
    second_response = dispatcher.dispatch(second.web_session_id, "/rename second-renamed")

    assert first_response["accepted"] is True
    assert first_response["title"] == "first-renamed"
    assert second_response["accepted"] is True
    assert second_response["title"] == "second-renamed"
    assert manager.storage.read_metadata(first_cwd, "same-id").name == "first-renamed"
    assert manager.storage.read_metadata(second_cwd, "same-id").name == "second-renamed"
    assert first.title == "first-renamed"
    assert second.title == "second-renamed"


def test_rename_with_ambiguous_bare_session_id_does_not_mutate_duplicate_projects(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    first_cwd = str(tmp_path / "project-a")
    second_cwd = str(tmp_path / "project-b")
    manager.create_session(cwd=first_cwd, session_id="same-id")
    manager.create_session(cwd=second_cwd, session_id="same-id")
    manager.storage.rename_session(first_cwd, "same-id", "first-name", git_branch=None)
    manager.storage.rename_session(second_cwd, "same-id", "second-name", git_branch=None)

    with pytest.raises(ValueError, match="session not found"):
        dispatcher.dispatch("same-id", "/rename ambiguous")

    assert manager.storage.read_metadata(first_cwd, "same-id").name == "first-name"
    assert manager.storage.read_metadata(second_cwd, "same-id").name == "second-name"


def test_rename_rejects_invalid_name_without_mutating_session(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session(session_id="rename-invalid")
    original_title = session.title

    response = dispatcher.dispatch(session.session_id, "/rename Production stack")

    assert response["accepted"] is False
    assert response["command"] == "rename"
    assert response["error"]["code"] == "invalid_session_name"
    assert "Session name must match" in response["error"]["message"]
    assert session.title == original_title
    assert manager.storage.read_metadata(session.cwd, session.session_id).name is None
    assert session.events.replay_after(0) == []


def test_rename_rejects_duplicate_name_without_mutating_session(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    first = manager.create_session(session_id="rename-first")
    second = manager.create_session(session_id="rename-second")
    dispatcher.dispatch(first.session_id, "/rename shared-name")
    original_title = second.title

    response = dispatcher.dispatch(second.session_id, "/rename shared-name")

    assert response["accepted"] is False
    assert response["command"] == "rename"
    assert response["error"]["code"] == "invalid_session_name"
    assert "Session name already exists" in response["error"]["message"]
    assert second.title == original_title
    assert manager.storage.read_metadata(second.cwd, second.session_id).name is None
    assert second.events.replay_after(0) == []


def test_clear_command_emits_session_updated_cleared_event(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()
    session.draft = "stale"

    response = dispatcher.dispatch(session.session_id, "/clear")

    assert response == {"accepted": True, "command": "clear", "cleared": True}
    assert session.draft == ""
    assert session.events.replay_after(0)[0]["type"] == "session.updated"
    assert session.events.replay_after(0)[0]["payload"] == {"cleared": True}


def test_debug_on_off_updates_state_and_emits_events(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    enabled = dispatcher.dispatch(session.session_id, "/debug on")
    disabled = dispatcher.dispatch(session.session_id, "/debug off")

    assert enabled == {"accepted": True, "command": "debug", "enabled": True}
    assert disabled == {"accepted": True, "command": "debug", "enabled": False}
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["session.updated", "session.updated"]
    assert [event["payload"] for event in events] == [{"debugEnabled": True}, {"debugEnabled": False}]


def test_debug_status_and_no_arg_report_state_without_emitting_events(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    initial = dispatcher.dispatch(session.session_id, "/debug")
    enabled = dispatcher.dispatch(session.session_id, "/debug on")
    status = dispatcher.dispatch(session.session_id, "/debug status")

    assert initial == {"accepted": True, "command": "debug", "enabled": False}
    assert enabled == {"accepted": True, "command": "debug", "enabled": True}
    assert status == {"accepted": True, "command": "debug", "enabled": True}
    assert [event["payload"] for event in session.events.replay_after(0)] == [{"debugEnabled": True}]


def test_debug_rejects_invalid_arguments_without_emitting_events(tmp_path) -> None:
    manager, dispatcher = _dispatcher(tmp_path)
    session = manager.create_session()

    response = dispatcher.dispatch(session.session_id, "/debug maybe")

    assert response == {
        "accepted": False,
        "command": "debug",
        "error": {
            "code": "invalid_debug_argument",
            "message": "Usage: /debug [on|off|status]",
        },
    }
    assert session.debug_enabled is False
    assert session.events.replay_after(0) == []


def test_command_route_publishes_finished_event_and_validates_body(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        accepted = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/status"})
        missing_command = client.post(f"/api/sessions/{session.session_id}/commands", json={})
        non_object = client.post(f"/api/sessions/{session.session_id}/commands", json=["/status"])
        missing_session = client.post("/api/sessions/missing/commands", json={"command": "/status"})

    assert accepted.status_code == 200
    assert accepted.json()["accepted"] is True
    assert accepted.json()["command"] == "status"
    assert missing_command.status_code == 400
    assert missing_command.json() == {"error": {"message": "command is required"}}
    assert non_object.status_code == 400
    assert non_object.json() == {"error": {"message": "request body must be a JSON object"}}
    assert missing_session.status_code == 404
    assert missing_session.json() == {"error": {"message": "session not found"}}
    events = session.events.replay_after(0)
    assert events[-1]["type"] == "command.finished"
    assert events[-1]["payload"]["result"]["command"] == "status"


def test_command_route_returns_400_for_rejected_command(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(mode="pipeline")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/model"})

    assert response.status_code == 400
    assert response.json()["accepted"] is False
    assert response.json()["error"]["code"] == "command_not_allowed_in_pipeline"
    assert session.events.replay_after(0)[-1]["type"] == "command.finished"


def test_command_route_executes_prompt_and_compact_without_placeholder_payloads(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = _manager(tmp_path)
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        prompt = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/prompt"})
        compact = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/compact"})

    assert prompt.status_code == 200
    prompt_payload = prompt.json()
    assert prompt_payload["accepted"] is True
    assert prompt_payload["snapshot"]["available"] is True
    assert prompt_payload["snapshot"]["sections"]
    assert compact.status_code == 202
    assert compact.json()["command"] == "compact"
    assert compact.json()["available"] is True
    assert compact.json()["state"] == "empty"
    event_types = [event["type"] for event in session.events.replay_after(0)]
    assert "compaction.started" in event_types
    assert "compaction.finished" in event_types
    assert event_types[-1] == "command.finished"


def test_command_route_resume_exact_id_and_unique_prefix(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    current = manager.create_session(session_id="current")
    target = manager.create_session(session_id="target-session")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        exact = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume target-session"})
        prefix = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume target-"})

    assert exact.status_code == 200
    assert exact.json()["action"] == "reload_session"
    assert exact.json()["session"]["sessionId"] == target.session_id
    assert prefix.status_code == 200
    assert prefix.json()["action"] == "reload_session"
    assert prefix.json()["session"]["sessionId"] == target.session_id


def test_command_route_resume_without_argument_opens_resume_chooser(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    current = manager.create_session(session_id="current")
    manager.create_session(session_id="other")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is False
    assert payload["command"] == "resume"
    assert payload["action"] == "open_resume_chooser"
    assert {candidate["sessionId"] for candidate in payload["candidates"]} == {"current", "other"}


def test_command_route_resume_without_argument_does_not_materialize_candidates(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    current = manager.create_session(session_id="current")
    other_cwd = str(tmp_path / "project")
    manager.storage.save(other_cwd, "other", [])
    assert (other_cwd, "other") not in manager._sessions
    assert not _web_metadata_path(manager, other_cwd, "other").exists()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume"})

    assert response.status_code == 200
    payload = response.json()
    assert {candidate["sessionId"] for candidate in payload["candidates"]} == {"current", "other"}
    assert (other_cwd, "other") not in manager._sessions
    assert not _web_metadata_path(manager, other_cwd, "other").exists()


def test_command_route_resume_searches_current_project_before_global_matches(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    current_cwd = str(tmp_path / "project-current")
    other_cwd = str(tmp_path / "project-other")
    current = manager.create_session(cwd=current_cwd, session_id="current")
    target_current = manager.create_session(cwd=current_cwd, session_id="target-current")
    target_global = manager.create_session(cwd=other_cwd, session_id="target-global")
    manager.storage.rename_session(target_current.cwd, target_current.session_id, "shared-name", git_branch=None)
    manager.storage.rename_session(target_global.cwd, target_global.session_id, "shared-name", git_branch=None)
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        by_prefix = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume target-"})
        by_name = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume shared-name"})

    assert by_prefix.status_code == 200
    assert by_prefix.json()["session"]["sessionId"] == "target-current"
    assert by_prefix.json()["session"]["cwd"] == current_cwd
    assert by_name.status_code == 200
    assert by_name.json()["session"]["sessionId"] == "target-current"
    assert by_name.json()["session"]["cwd"] == current_cwd


def test_command_route_resume_ambiguous_name_returns_candidates(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    current = manager.create_session(session_id="current")
    first = manager.create_session(cwd=str(tmp_path / "project-a"), session_id="first")
    second = manager.create_session(cwd=str(tmp_path / "project-b"), session_id="second")
    manager.storage.rename_session(first.cwd, first.session_id, "shared-name", git_branch=None)
    manager.storage.rename_session(second.cwd, second.session_id, "shared-name", git_branch=None)
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume shared-name"})

    assert response.status_code == 409
    payload = response.json()
    assert payload["accepted"] is False
    assert payload["error"]["code"] == "resume_ambiguous"
    assert sorted(candidate["sessionId"] for candidate in payload["candidates"]) == ["first", "second"]


def test_command_route_resume_ambiguous_name_does_not_materialize_candidates(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    current = manager.create_session(session_id="current")
    first_cwd = str(tmp_path / "project-a")
    second_cwd = str(tmp_path / "project-b")
    manager.storage.save(first_cwd, "first", [])
    manager.storage.save(second_cwd, "second", [])
    manager.storage.rename_session(first_cwd, "first", "shared-name", git_branch=None)
    manager.storage.rename_session(second_cwd, "second", "shared-name", git_branch=None)
    assert (first_cwd, "first") not in manager._sessions
    assert (second_cwd, "second") not in manager._sessions
    assert not _web_metadata_path(manager, first_cwd, "first").exists()
    assert not _web_metadata_path(manager, second_cwd, "second").exists()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume shared-name"})

    assert response.status_code == 409
    payload = response.json()
    assert sorted(candidate["sessionId"] for candidate in payload["candidates"]) == ["first", "second"]
    assert (first_cwd, "first") not in manager._sessions
    assert (second_cwd, "second") not in manager._sessions
    assert not _web_metadata_path(manager, first_cwd, "first").exists()
    assert not _web_metadata_path(manager, second_cwd, "second").exists()


def test_command_route_resume_cross_project_returns_new_web_session(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    current = manager.create_session(cwd=str(tmp_path / "project-a"), session_id="current")
    target = manager.create_session(cwd=str(tmp_path / "project-b"), session_id="target")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{current.session_id}/commands", json={"command": "/resume target"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "open_session"
    assert payload["session"]["sessionId"] == target.session_id
    assert payload["session"]["cwd"] == target.cwd
    assert payload["session"]["webSessionId"] == target.web_session_id
    assert current.cwd != payload["session"]["cwd"]


def test_command_route_model_and_effort_without_args_return_workspace_controls(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = _manager(tmp_path)
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        model = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/model"})
        effort = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/effort"})

    assert model.status_code == 200
    assert model.json()["action"] == "open_model_selector"
    assert "providers" in model.json()
    assert effort.status_code == 200
    assert effort.json()["action"] == "open_effort_selector"
    assert "providers" in effort.json()


class FakePermissionShellTool:
    name = "bash"
    supports_blanket_allow = False

    def __init__(self, permission) -> None:
        self.permission = permission
        self.permission_calls: list[tuple[dict[str, object], object]] = []

    async def check_permissions(self, input: dict, context=None):
        self.permission_calls.append((dict(input), context))
        return self.permission

    def user_facing_name(self, input: dict | None = None) -> str:
        return "Bash"

    def is_read_only(self, input: dict | None = None) -> bool:
        return False

    def permission_audit_operation(self, input: dict | None = None) -> dict[str, object]:
        return {"is_read_only": False}


class FakeToolRegistry:
    def __init__(self, tool: FakePermissionShellTool | None) -> None:
        self.tool = tool

    def get(self, name: str):
        return self.tool if name == "bash" else None


class FakeToolExecutor:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    async def execute_batch(self, calls, context):
        self.calls.append((calls, context))
        return [self.result]


@pytest.mark.asyncio
async def test_shell_escape_runner_checks_permission_and_executes_via_tool_executor(tmp_path) -> None:
    from iac_code.tools.base import ToolResult
    from iac_code.types.permissions import PermissionResult, ToolPermissionContext
    from iac_code.web.shell import WebShellEscapeRunner

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-session")
    tool = FakePermissionShellTool(PermissionResult(behavior="allow"))
    executor = FakeToolExecutor(ToolResult.success("STDOUT:\nhi\nExit code: 0"))

    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        executor_factory=lambda _registry: executor,
        permission_context_factory=lambda web_session: ToolPermissionContext(cwd=web_session.cwd),
    )

    result = await runner.run(session, "echo hi")

    assert tool.permission_calls[0][0] == {"command": "echo hi"}
    assert tool.permission_calls[0][1].cwd == session.cwd
    assert len(executor.calls) == 1
    calls, context = executor.calls[0]
    assert context.cwd == session.cwd
    assert calls[0].id == "shell-escape"
    assert calls[0].name == "bash"
    assert calls[0].input == {"command": "echo hi"}
    assert result == {
        "shellUseId": result["shellUseId"],
        "toolUseId": result["toolUseId"],
        "command": "echo hi",
        "exitCode": 0,
        "stdout": "hi",
        "stderr": "",
        "local": True,
        "entersAgentContext": False,
    }
    assert result["shellUseId"] == result["toolUseId"]
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["local.shell.start", "local.shell.end"]
    assert events[0]["payload"] == {
        "shellUseId": result["shellUseId"],
        "toolUseId": result["toolUseId"],
        "command": "echo hi",
        "local": True,
        "entersAgentContext": False,
    }
    assert events[1]["payload"] == result
    assert manager.load_visible_messages(session.session_id, cwd=session.cwd) == []


@pytest.mark.asyncio
async def test_shell_escape_runner_waits_for_web_permission_rejection_without_executing(tmp_path) -> None:
    from iac_code.tools.base import ToolResult
    from iac_code.types.permissions import PermissionResult, ToolPermissionContext
    from iac_code.web.shell import WebShellEscapeRunner

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-permission")
    tool = FakePermissionShellTool(PermissionResult(behavior="ask", message="Allow Bash?"))
    executor = FakeToolExecutor(ToolResult.success("STDOUT:\nshould-not-run\nExit code: 0"))
    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        executor_factory=lambda _registry: executor,
        permission_context_factory=lambda web_session: ToolPermissionContext(cwd=web_session.cwd),
    )

    run_task = asyncio.create_task(runner.run(session, "touch denied"))
    deadline = asyncio.get_running_loop().time() + 1
    while not session.pending_permissions and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert len(session.pending_permissions) == 1
    request_id = next(iter(session.pending_permissions))
    permission_event = session.events.replay_after(0)[1]

    assert permission_event["type"] == "permission.request"
    assert permission_event["payload"]["payload"]["toolName"] == "bash"
    assert permission_event["payload"]["payload"]["toolUseId"] == "shell-escape"
    assert permission_event["payload"]["payload"]["toolInput"] == {"command": "touch denied"}
    assert permission_event["payload"]["payload"]["allowAlways"] is False
    assert [choice["id"] for choice in permission_event["payload"]["payload"]["choices"]] == [
        "allow_once",
        "reject_once",
        "always_deny",
    ]
    manager.resolve_permission(request_id, {"choice": "reject_once"}, session_id=session.session_id)

    result = await asyncio.wait_for(run_task, timeout=1)

    assert executor.calls == []
    assert result["exitCode"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == "Permission denied."
    assert [event["type"] for event in session.events.replay_after(0)] == [
        "local.shell.start",
        "permission.request",
        "permission.resolved",
        "local.shell.end",
    ]


@pytest.mark.asyncio
async def test_shell_escape_allow_fails_closed_when_permission_audit_fails(tmp_path, monkeypatch) -> None:
    from iac_code.tools.base import ToolResult
    from iac_code.types.permissions import PermissionAuditMetadata, PermissionResult, ToolPermissionContext
    from iac_code.web.shell import WebShellEscapeRunner

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-audit-fail-closed")
    tool = FakePermissionShellTool(
        PermissionResult(
            behavior="ask",
            message="Allow Bash?",
            audit=PermissionAuditMetadata(scope="once", source="permission_pipeline", is_read_only=False),
        )
    )
    executor = FakeToolExecutor(ToolResult.success("STDOUT:\nshould-not-run\nExit code: 0"))
    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        executor_factory=lambda _registry: executor,
        permission_context_factory=lambda web_session: ToolPermissionContext(cwd=web_session.cwd),
    )
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_boundary_audit",
        lambda *_args, **_kwargs: False,
    )

    run_task = asyncio.create_task(runner.run(session, "touch denied"))
    deadline = asyncio.get_running_loop().time() + 1
    while not session.pending_permissions and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    request_id = next(iter(session.pending_permissions))
    manager.resolve_permission(request_id, {"choice": "allow_once"}, session_id=session.session_id)

    result = await asyncio.wait_for(run_task, timeout=1)

    assert executor.calls == []
    assert result["exitCode"] == 1
    assert result["stderr"] == "Permission denied."


@pytest.mark.asyncio
async def test_shell_escape_always_deny_applies_session_rule_without_executing_later_commands(tmp_path) -> None:
    from iac_code.tools.base import ToolResult
    from iac_code.tools.bash.permissions import bash_tool_has_permission
    from iac_code.types.permissions import ToolPermissionContext
    from iac_code.web.shell import WebShellEscapeRunner

    class RuleAwareShellTool(FakePermissionShellTool):
        def __init__(self) -> None:
            super().__init__(None)

        async def check_permissions(self, input: dict, context=None):
            self.permission_calls.append((dict(input), context))
            assert isinstance(context, ToolPermissionContext)
            return await bash_tool_has_permission(str(input["command"]), context)

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-deny-rule")
    tool = RuleAwareShellTool()
    executor = FakeToolExecutor(ToolResult.success("STDOUT:\nshould-not-run\nExit code: 0"))
    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        executor_factory=lambda _registry: executor,
    )

    first_task = asyncio.create_task(runner.run(session, "curl https://example.com"))
    deadline = asyncio.get_running_loop().time() + 1
    while not session.pending_permissions and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert len(session.pending_permissions) == 1
    request_id = next(iter(session.pending_permissions))
    manager.resolve_permission(request_id, {"choice": "always_deny"}, session_id=session.session_id)

    first = await asyncio.wait_for(first_task, timeout=1)
    second = await runner.run(session, "curl https://example.com/status")

    assert first["exitCode"] == 1
    assert second["exitCode"] == 1
    assert executor.calls == []
    assert [event["type"] for event in session.events.replay_after(0)].count("permission.request") == 1


@pytest.mark.asyncio
async def test_shell_escape_runner_missing_bash_tool_publishes_terminal_event(tmp_path) -> None:
    from iac_code.web.shell import WebShellEscapeRunner

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-missing-tool")
    runner = WebShellEscapeRunner(manager, tool_registry=FakeToolRegistry(None))

    result = await runner.run(session, "echo hi")

    assert result["exitCode"] == 127
    assert result["stderr"] == "Shell command support is unavailable."
    assert result["shellUseId"] == result["toolUseId"]
    assert [event["type"] for event in session.events.replay_after(0)] == ["local.shell.start", "local.shell.end"]


@pytest.mark.asyncio
async def test_shell_escape_runner_publishes_end_when_cancelled_while_waiting_for_permission(tmp_path) -> None:
    from iac_code.tools.base import ToolResult
    from iac_code.types.permissions import PermissionResult, ToolPermissionContext
    from iac_code.web.shell import WebShellEscapeRunner

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-cancel")
    tool = FakePermissionShellTool(PermissionResult(behavior="ask", message="Allow Bash?"))
    executor = FakeToolExecutor(ToolResult.success("STDOUT:\nshould-not-run\nExit code: 0"))
    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        executor_factory=lambda _registry: executor,
        permission_context_factory=lambda web_session: ToolPermissionContext(cwd=web_session.cwd),
    )

    run_task = asyncio.create_task(runner.run(session, "touch maybe"))
    deadline = asyncio.get_running_loop().time() + 1
    while not session.pending_permissions and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert len(session.pending_permissions) == 1

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert executor.calls == []
    assert session.pending_permissions == {}
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == [
        "local.shell.start",
        "permission.request",
        "permission.resolved",
        "local.shell.end",
    ]
    assert events[2]["payload"]["answer"] == {
        "choice": "canceled",
        "canceled": True,
    }
    assert events[-1]["payload"] == {
        "shellUseId": events[0]["payload"]["shellUseId"],
        "toolUseId": events[0]["payload"]["toolUseId"],
        "command": "touch maybe",
        "exitCode": 130,
        "stdout": "",
        "stderr": "Shell command canceled.",
        "local": True,
        "entersAgentContext": False,
    }


@pytest.mark.asyncio
async def test_shell_escape_runner_turns_permission_setup_error_into_terminal_event(tmp_path) -> None:
    from iac_code.types.permissions import PermissionResult
    from iac_code.web.shell import WebShellEscapeRunner

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-error")
    tool = FakePermissionShellTool(PermissionResult(behavior="allow"))
    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        permission_context_factory=lambda _web_session: (_ for _ in ()).throw(
            RuntimeError("permission boom api_key=sk-shellsecret12345678")
        ),
    )

    result = await runner.run(session, "echo hi")

    assert result["exitCode"] == 1
    assert result["stdout"] == ""
    assert "sk-shellsecret" in result["stderr"]
    assert [event["type"] for event in session.events.replay_after(0)] == [
        "local.shell.start",
        "local.shell.end",
    ]


@pytest.mark.asyncio
async def test_shell_escape_runner_preserves_local_executor_exception(tmp_path) -> None:
    from iac_code.types.permissions import PermissionResult, ToolPermissionContext
    from iac_code.web.shell import WebShellEscapeRunner

    class RaisingExecutor:
        async def execute_batch(self, _calls, _context):
            raise RuntimeError("executor failed api_key=sk-executorsecret12345678")

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-executor-error")
    tool = FakePermissionShellTool(PermissionResult(behavior="allow"))
    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        executor_factory=lambda _registry: RaisingExecutor(),
        permission_context_factory=lambda web_session: ToolPermissionContext(cwd=web_session.cwd),
    )

    result = await runner.run(session, "echo hi")

    assert result["exitCode"] == 1
    assert "sk-executorsecret" in result["stderr"]
    assert [event["type"] for event in session.events.replay_after(0)] == ["local.shell.start", "local.shell.end"]


@pytest.mark.asyncio
async def test_shell_escape_always_allow_applies_session_rule_for_later_commands(tmp_path) -> None:
    from iac_code.tools.base import ToolResult
    from iac_code.tools.bash.permissions import bash_tool_has_permission
    from iac_code.types.permissions import ToolPermissionContext
    from iac_code.web.shell import WebShellEscapeRunner

    class RuleAwareShellTool(FakePermissionShellTool):
        def __init__(self) -> None:
            super().__init__(None)

        async def check_permissions(self, input: dict, context=None):
            self.permission_calls.append((dict(input), context))
            assert isinstance(context, ToolPermissionContext)
            return await bash_tool_has_permission(str(input["command"]), context)

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-rule")
    tool = RuleAwareShellTool()
    executor = FakeToolExecutor(ToolResult.success("STDOUT:\nok\nExit code: 0"))
    runner = WebShellEscapeRunner(
        manager,
        tool_registry=FakeToolRegistry(tool),
        executor_factory=lambda _registry: executor,
    )

    first_task = asyncio.create_task(runner.run(session, "curl https://example.com"))
    deadline = asyncio.get_running_loop().time() + 1
    while not session.pending_permissions and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert len(session.pending_permissions) == 1
    request_id = next(iter(session.pending_permissions))
    permission_payload = session.events.replay_after(0)[1]["payload"]["payload"]
    assert [choice["id"] for choice in permission_payload["choices"]] == [
        "allow_once",
        "always_allow",
        "reject_once",
        "always_deny",
    ]

    manager.resolve_permission(request_id, {"choice": "always_allow"}, session_id=session.session_id)
    await asyncio.wait_for(first_task, timeout=1)

    await runner.run(session, "curl https://example.com/status")

    assert len(executor.calls) == 2
    assert [event["type"] for event in session.events.replay_after(0)].count("permission.request") == 1


@pytest.mark.asyncio
async def test_command_route_runs_shell_escape_before_finished_event(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route")

    class FakeShellRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def run(self, web_session, command: str):
            self.calls.append((web_session.session_id, command))
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "route-shell",
                    "toolUseId": "route-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "command": command,
                    "exitCode": 0,
                    "stdout": "hi",
                    "stderr": "",
                    "local": True,
                    "entersAgentContext": False,
                },
            )

    shell_runner = FakeShellRunner()
    app = create_app(session_manager=manager, shell_runner_factory=lambda: shell_runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo hi"})

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "command": "shell_escape",
        "local": True,
        "entersAgentContext": False,
        "shell": "echo hi",
    }
    assert shell_runner.calls == [(session.session_id, "echo hi")]
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["local.shell.start", "local.shell.end", "command.finished"]
    assert events[-1]["payload"]["command"] == "!echo hi"
    assert events[-1]["payload"]["result"]["command"] == "shell_escape"
    assert manager.load_visible_messages(session.session_id, cwd=session.cwd) == []


@pytest.mark.asyncio
async def test_running_shell_command_blocks_archive_and_delete(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-lifecycle")

    class BlockingShellRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, _session, _command: str):
            self.started.set()
            await self.release.wait()

    shell_runner = BlockingShellRunner()
    app = create_app(session_manager=manager, shell_runner_factory=lambda: shell_runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        command_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo hi"})
        )
        await asyncio.wait_for(shell_runner.started.wait(), timeout=1)
        archived = await client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
        deleted = await client.delete(f"/api/sessions/{session.session_id}")
        shell_runner.release.set()
        command = await asyncio.wait_for(command_task, timeout=1)

    assert archived.status_code == 409
    assert archived.json()["error"]["code"] == "session_busy"
    assert deleted.status_code == 409
    assert manager.get_session(session.session_id) is session
    assert command.status_code == 200


@pytest.mark.asyncio
async def test_shell_command_stays_active_through_finished_event_publication(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-finished-lifecycle")
    finished_started = asyncio.Event()
    release_finished = asyncio.Event()
    original_publish = session.events.publish

    class ImmediateShellRunner:
        async def run(self, _session, _command: str):
            return None

    async def blocking_publish(event_type: str, payload: dict):
        if event_type == "command.finished":
            finished_started.set()
            await release_finished.wait()
        return await original_publish(event_type, payload)

    monkeypatch.setattr(session.events, "publish", blocking_publish)
    app = create_app(session_manager=manager, shell_runner_factory=lambda: ImmediateShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        command_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo hi"})
        )
        await asyncio.wait_for(finished_started.wait(), timeout=1)
        active_while_finishing = any(not task.done() for task in session.active_local_tasks)
        archived = await client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
        deleted = await client.delete(f"/api/sessions/{session.session_id}")
        release_finished.set()
        command = await asyncio.wait_for(command_task, timeout=1)

    assert active_while_finishing is True
    assert archived.status_code == 409
    assert archived.json()["error"]["code"] == "session_busy"
    assert deleted.status_code == 409
    assert manager.get_session(session.session_id) is session
    assert command.status_code == 200


@pytest.mark.asyncio
async def test_command_route_publishes_finished_when_shell_runner_raises(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-error")

    class RaisingShellRunner:
        async def run(self, web_session, command: str):
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "raising-shell",
                    "toolUseId": "raising-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            raise RuntimeError("runner boom")

    app = create_app(session_manager=manager, shell_runner_factory=lambda: RaisingShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo hi"})

    assert response.status_code == 500
    assert response.json()["accepted"] is False
    assert response.json()["error"]["code"] == "shell_escape_failed"
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["local.shell.start", "local.shell.end", "command.finished"]
    assert events[1]["payload"]["shellUseId"] == events[0]["payload"]["shellUseId"]
    assert events[1]["payload"]["toolUseId"] == events[0]["payload"]["toolUseId"]
    assert events[1]["payload"]["stderr"] == "runner boom"
    assert events[-1]["payload"]["result"]["error"]["message"] == "runner boom"


@pytest.mark.asyncio
async def test_command_route_fallback_end_matches_concurrent_shell_escape_start(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-concurrent-error")
    first_started = asyncio.Event()
    second_finished = asyncio.Event()

    class ConcurrentShellRunner:
        async def run(self, web_session, command: str):
            if command == "first":
                await web_session.events.publish(
                    "local.shell.start",
                    {
                        "shellUseId": "first-shell",
                        "toolUseId": "first-shell",
                        "command": command,
                        "local": True,
                        "entersAgentContext": False,
                    },
                )
                first_started.set()
                await asyncio.wait_for(second_finished.wait(), timeout=1)
                raise RuntimeError("first boom")

            await asyncio.wait_for(first_started.wait(), timeout=1)
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "exitCode": 0,
                    "stdout": "second ok",
                    "stderr": "",
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            second_finished.set()

    app = create_app(session_manager=manager, shell_runner_factory=lambda: ConcurrentShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!first"})
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second_response = await client.post(
            f"/api/sessions/{session.session_id}/commands",
            json={"command": "!second"},
        )
        first_response = await asyncio.wait_for(first_task, timeout=1)

    assert first_response.status_code == 500
    assert second_response.status_code == 200
    shell_end_payloads = [
        event["payload"] for event in session.events.replay_after(0) if event["type"] == "local.shell.end"
    ]
    assert [payload["shellUseId"] for payload in shell_end_payloads] == ["second-shell", "first-shell"]
    assert shell_end_payloads[1]["toolUseId"] == "first-shell"
    assert shell_end_payloads[1]["stderr"] == "first boom"


@pytest.mark.asyncio
async def test_command_route_fallback_end_does_not_borrow_concurrent_shell_id_without_start(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-concurrent-no-start")
    first_entered = asyncio.Event()
    second_finished = asyncio.Event()

    class ConcurrentShellRunner:
        async def run(self, web_session, command: str):
            if command == "first":
                first_entered.set()
                await asyncio.wait_for(second_finished.wait(), timeout=1)
                raise RuntimeError("first boom")

            await asyncio.wait_for(first_entered.wait(), timeout=1)
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "exitCode": 0,
                    "stdout": "second ok",
                    "stderr": "",
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            second_finished.set()

    app = create_app(session_manager=manager, shell_runner_factory=lambda: ConcurrentShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!first"})
        )
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        second_response = await client.post(
            f"/api/sessions/{session.session_id}/commands",
            json={"command": "!second"},
        )
        first_response = await asyncio.wait_for(first_task, timeout=1)

    assert first_response.status_code == 500
    assert second_response.status_code == 200
    shell_end_payloads = [
        event["payload"] for event in session.events.replay_after(0) if event["type"] == "local.shell.end"
    ]
    assert [payload["command"] for payload in shell_end_payloads] == ["second", "first"]
    assert shell_end_payloads[1]["stderr"] == "first boom"
    assert shell_end_payloads[1]["shellUseId"] != "second-shell"
    assert shell_end_payloads[1]["toolUseId"] == shell_end_payloads[1]["shellUseId"]


@pytest.mark.asyncio
async def test_command_route_fallback_end_does_not_borrow_running_concurrent_shell_id(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-concurrent-running")
    first_entered = asyncio.Event()
    second_started = asyncio.Event()
    allow_second_finish = asyncio.Event()

    class ConcurrentShellRunner:
        async def run(self, web_session, command: str):
            if command == "first":
                first_entered.set()
                await asyncio.wait_for(second_started.wait(), timeout=1)
                raise RuntimeError("first boom")

            await asyncio.wait_for(first_entered.wait(), timeout=1)
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            second_started.set()
            await asyncio.wait_for(allow_second_finish.wait(), timeout=1)
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "exitCode": 0,
                    "stdout": "second ok",
                    "stderr": "",
                    "local": True,
                    "entersAgentContext": False,
                },
            )

    app = create_app(session_manager=manager, shell_runner_factory=lambda: ConcurrentShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!first"})
        )
        second_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!second"})
        )
        first_response = await asyncio.wait_for(first_task, timeout=1)
        allow_second_finish.set()
        second_response = await asyncio.wait_for(second_task, timeout=1)

    assert first_response.status_code == 500
    assert second_response.status_code == 200
    shell_end_payloads = [
        event["payload"] for event in session.events.replay_after(0) if event["type"] == "local.shell.end"
    ]
    assert [payload["command"] for payload in shell_end_payloads] == ["first", "second"]
    assert shell_end_payloads[0]["shellUseId"] != "second-shell"
    assert shell_end_payloads[0]["toolUseId"] == shell_end_payloads[0]["shellUseId"]
    assert shell_end_payloads[1]["shellUseId"] == "second-shell"
    assert shell_end_payloads[1]["toolUseId"] == "second-shell"


@pytest.mark.asyncio
async def test_command_route_fallback_end_matches_same_command_concurrent_start(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-same-command")
    first_started = asyncio.Event()
    second_finished = asyncio.Event()

    class SameCommandShellRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, web_session, command: str):
            self.calls += 1
            if self.calls == 1:
                await web_session.events.publish(
                    "local.shell.start",
                    {
                        "shellUseId": "first-shell",
                        "toolUseId": "first-shell",
                        "command": command,
                        "local": True,
                        "entersAgentContext": False,
                    },
                )
                first_started.set()
                await asyncio.wait_for(second_finished.wait(), timeout=1)
                raise RuntimeError("first boom")

            await asyncio.wait_for(first_started.wait(), timeout=1)
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "exitCode": 0,
                    "stdout": "second ok",
                    "stderr": "",
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            second_finished.set()

    runner = SameCommandShellRunner()
    app = create_app(session_manager=manager, shell_runner_factory=lambda: runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!same"})
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second_response = await client.post(
            f"/api/sessions/{session.session_id}/commands",
            json={"command": "!same"},
        )
        first_response = await asyncio.wait_for(first_task, timeout=1)

    assert first_response.status_code == 500
    assert second_response.status_code == 200
    shell_end_payloads = [
        event["payload"] for event in session.events.replay_after(0) if event["type"] == "local.shell.end"
    ]
    assert [payload["shellUseId"] for payload in shell_end_payloads] == ["second-shell", "first-shell"]
    assert shell_end_payloads[1]["stderr"] == "first boom"


@pytest.mark.asyncio
async def test_command_route_fallback_end_does_not_borrow_running_same_command_shell_id(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-same-command-running")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    allow_second_finish = asyncio.Event()
    coordination_timeout = 5

    class SameCommandShellRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, web_session, command: str):
            self.calls += 1
            if self.calls == 1:
                await web_session.events.publish(
                    "local.shell.start",
                    {
                        "shellUseId": "first-shell",
                        "toolUseId": "first-shell",
                        "command": command,
                        "local": True,
                        "entersAgentContext": False,
                    },
                )
                first_started.set()
                await asyncio.wait_for(second_started.wait(), timeout=coordination_timeout)
                raise RuntimeError("first boom")

            await asyncio.wait_for(first_started.wait(), timeout=coordination_timeout)
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            second_started.set()
            await asyncio.wait_for(allow_second_finish.wait(), timeout=coordination_timeout)
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "second-shell",
                    "toolUseId": "second-shell",
                    "command": command,
                    "exitCode": 0,
                    "stdout": "second ok",
                    "stderr": "",
                    "local": True,
                    "entersAgentContext": False,
                },
            )

    runner = SameCommandShellRunner()
    app = create_app(session_manager=manager, shell_runner_factory=lambda: runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!same"})
        )
        await asyncio.wait_for(first_started.wait(), timeout=coordination_timeout)
        second_task = asyncio.create_task(
            client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!same"})
        )
        first_response = await asyncio.wait_for(first_task, timeout=coordination_timeout)
        allow_second_finish.set()
        second_response = await asyncio.wait_for(second_task, timeout=coordination_timeout)

    assert first_response.status_code == 500
    assert second_response.status_code == 200
    shell_end_payloads = [
        event["payload"] for event in session.events.replay_after(0) if event["type"] == "local.shell.end"
    ]
    assert [payload["shellUseId"] for payload in shell_end_payloads] == ["first-shell", "second-shell"]
    assert shell_end_payloads[0]["stderr"] == "first boom"
    assert shell_end_payloads[1]["stderr"] == ""


@pytest.mark.asyncio
async def test_command_route_publishes_finished_when_shell_runner_is_cancelled(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-cancel")

    class CancellingShellRunner:
        async def run(self, web_session, command: str):
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "cancel-shell",
                    "toolUseId": "cancel-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "cancel-shell",
                    "toolUseId": "cancel-shell",
                    "command": command,
                    "exitCode": 130,
                    "stdout": "",
                    "stderr": "Shell command canceled.",
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            raise asyncio.CancelledError()

    app = create_app(session_manager=manager, shell_runner_factory=lambda: CancellingShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo hi"})

    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["local.shell.start", "local.shell.end", "command.finished"]
    assert events[-1]["payload"]["result"]["error"]["code"] == "shell_escape_canceled"


@pytest.mark.asyncio
async def test_command_route_does_not_duplicate_shell_end_when_runner_raises_after_terminal_event(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-terminal-then-error")

    class TerminalThenRaisingShellRunner:
        async def run(self, web_session, command: str):
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "terminal-shell",
                    "toolUseId": "terminal-shell",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "shellUseId": "terminal-shell",
                    "toolUseId": "terminal-shell",
                    "command": command,
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "already ended",
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            raise RuntimeError("runner boom")

    app = create_app(session_manager=manager, shell_runner_factory=lambda: TerminalThenRaisingShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo hi"})

    assert response.status_code == 500
    events = session.events.replay_after(0)
    assert [event["type"] for event in events].count("local.shell.end") == 1
    assert [event["type"] for event in events] == ["local.shell.start", "local.shell.end", "command.finished"]


@pytest.mark.asyncio
async def test_command_route_does_not_duplicate_mixed_legacy_shell_end_when_runner_raises(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-legacy-end-then-error")

    class LegacyEndThenRaisingShellRunner:
        async def run(self, web_session, command: str):
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "modern-start",
                    "toolUseId": "modern-start",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "command": command,
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "legacy end",
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            raise RuntimeError("runner boom")

    app = create_app(session_manager=manager, shell_runner_factory=lambda: LegacyEndThenRaisingShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo hi"})

    assert response.status_code == 500
    events = session.events.replay_after(0)
    assert [event["type"] for event in events].count("local.shell.end") == 1
    assert [event["type"] for event in events] == ["local.shell.start", "local.shell.end", "command.finished"]
    assert events[1]["payload"]["stderr"] == "legacy end"


@pytest.mark.asyncio
async def test_command_route_matches_raw_shell_command_when_runner_raises_after_legacy_end(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="shell-route-redacted-legacy-end")

    class LegacyEndThenRaisingShellRunner:
        async def run(self, web_session, command: str):
            await web_session.events.publish(
                "local.shell.start",
                {
                    "shellUseId": "modern-start",
                    "toolUseId": "modern-start",
                    "command": command,
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            await web_session.events.publish(
                "local.shell.end",
                {
                    "command": command,
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "legacy end",
                    "local": True,
                    "entersAgentContext": False,
                },
            )
            raise RuntimeError("runner boom")

    app = create_app(session_manager=manager, shell_runner_factory=lambda: LegacyEndThenRaisingShellRunner())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{session.session_id}/commands",
            json={"command": "!echo API_KEY=abcd1234"},
        )

    assert response.status_code == 500
    events = session.events.replay_after(0)
    assert [event["type"] for event in events].count("local.shell.end") == 1
    assert [event["type"] for event in events] == ["local.shell.start", "local.shell.end", "command.finished"]
    assert events[0]["payload"]["command"] == "echo API_KEY=abcd1234"
    assert events[1]["payload"]["command"] == "echo API_KEY=abcd1234"
    assert events[1]["payload"]["stderr"] == "legacy end"


def test_commands_and_status_routes(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="status-route")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        commands = client.get("/api/commands")
        status = client.get(f"/api/sessions/{session.session_id}/status")
        missing = client.get("/api/sessions/missing/status")

    assert commands.status_code == 200
    assert {command["name"] for command in commands.json()["commands"]} >= {"/status", "/prompt", "/model"}
    assert status.status_code == 200
    assert status.json()["sessionId"] == session.session_id
    assert status.json()["mode"] == "normal"
    assert missing.status_code == 404
    assert missing.json() == {"error": {"message": "session not found"}}


def test_session_snapshot_exposes_latest_event_sequence(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="sequence-route")
    session.events.append("user.message", {"turnId": "turn-1", "text": "hello"})
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}")

    assert response.status_code == 200
    assert response.json()["latestSequence"] == 1
    # 普通会话空闲(无进行中轮次):存储转录即完整历史,replaySequence 回到 latest 不回放,
    # 避免重载时已完成轮次被回放而重复渲染(见 compute_replay_sequence)。
    assert response.json()["replaySequence"] == 1
    assert response.json()["hasBufferedEvents"] is True


def test_suggestions_route_serves_commands_files_and_shell_history(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    project = tmp_path / "project"
    project.mkdir()
    (project / "main.yaml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zsh_history").write_text(": 1700000000:0;git status --short\n: 1700000001:0;pwd\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    # os.path.expanduser("~") ignores HOME on Windows and reads USERPROFILE, so
    # point that at the temp home too or the shell-history lookup escapes tmp_path.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(session_id="suggestions-route")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        command_response = client.get("/api/suggestions", params={"kind": "command", "q": "status"})
        file_response = client.get(
            "/api/suggestions",
            params={"kind": "file", "q": "main", "sessionId": session.session_id},
        )
        shell_response = client.get("/api/suggestions", params={"kind": "shell", "q": "git"})

    assert command_response.status_code == 200
    assert command_response.json()["suggestions"][0]["value"] == "/status"
    assert file_response.status_code == 200
    assert {"label": "main.yaml", "value": "@main.yaml", "kind": "file"} in file_response.json()["suggestions"]
    assert shell_response.status_code == 200
    assert {"label": "git status --short", "value": "!git status --short", "kind": "shell"} in shell_response.json()[
        "suggestions"
    ]


def test_suggestions_route_serves_project_skills_from_management_state(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "project"
    skill_dir = project / "skills" / "web-demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: web-demo\ndescription: Web demo skill\n---\nUse this skill.\n",
        encoding="utf-8",
    )
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(session_id="skill-suggestions-route")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(
            "/api/suggestions",
            params={"kind": "skill", "q": "web", "sessionId": session.session_id},
        )

    assert response.status_code == 200
    assert {
        "label": "web-demo Web demo skill",
        "value": "$web-demo",
        "kind": "skill",
        "origin": "project",
    } in response.json()["suggestions"]


@pytest.mark.asyncio
async def test_command_route_starts_turn_for_dollar_skill(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "project"
    skill_dir = project / "skills" / "web-demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: web-demo\ndescription: Web demo skill\n---\nUse this skill.\n",
        encoding="utf-8",
    )
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(session_id="skill-command-route")
    started = asyncio.Event()

    class RecordingRuntime:
        def __init__(self) -> None:
            self.requests = []

        async def start_turn(self, request):
            self.requests.append(request)
            started.set()
            return {"accepted": True, "turnId": request.turn_id}

    runtime = RecordingRuntime()
    app = create_app(session_manager=manager, runtime_factory=lambda _session: runtime)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{session.session_id}/commands",
            json={"command": "$web-demo arg-one"},
        )
        await asyncio.wait_for(started.wait(), timeout=1)

    assert response.status_code == 202
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["command"] == "skill"
    assert payload["skill"] == "web-demo"
    assert runtime.requests[0].source == "skill"
    assert runtime.requests[0].turn_id == payload["turnId"]
    assert runtime.requests[0].text.startswith("<skill-name>web-demo</skill-name>")
    assert "Use this skill." in runtime.requests[0].text
    assert "ARGUMENTS: arg-one" in runtime.requests[0].text
    assert [event["type"] for event in session.events.replay_after(0)] == ["command.finished"]


@pytest.mark.asyncio
async def test_command_route_starts_turn_for_slash_skill_and_lists_it_in_slash_suggestions(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "project"
    skill_dir = project / "skills" / "web-demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: web-demo\ndescription: Web demo skill\n---\nSlash skill body.\n",
        encoding="utf-8",
    )
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(session_id="slash-skill-command-route")
    started = asyncio.Event()

    class RecordingRuntime:
        def __init__(self) -> None:
            self.requests = []

        async def start_turn(self, request):
            self.requests.append(request)
            started.set()
            return {"accepted": True, "turnId": request.turn_id}

    runtime = RecordingRuntime()
    app = create_app(session_manager=manager, runtime_factory=lambda _session: runtime)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        suggestions = await client.get(
            "/api/suggestions",
            params={"kind": "command", "q": "web", "sessionId": session.session_id},
        )
        response = await client.post(
            f"/api/sessions/{session.session_id}/commands",
            json={"command": "/web-demo"},
        )
        await asyncio.wait_for(started.wait(), timeout=1)

    assert suggestions.status_code == 200
    assert {
        "label": "web-demo Web demo skill",
        "value": "/web-demo",
        "kind": "command",
        "origin": "project",
    } in suggestions.json()["suggestions"]
    assert response.status_code == 202
    assert runtime.requests[0].source == "skill"
    assert "Slash skill body." in runtime.requests[0].text


def test_command_route_rejects_unknown_dollar_skill_without_fake_acceptance(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="unknown-skill-command-route")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "$missing"})

    assert response.status_code == 400
    payload = response.json()
    assert payload["accepted"] is False
    assert payload["error"]["code"] == "unknown_skill"


def test_image_upload_route_stores_session_cached_image(tmp_path, monkeypatch) -> None:
    import base64

    from iac_code.web.app import create_app
    from iac_code.web.images import load_cached_image

    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="image-route")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/images",
            json={
                "mediaType": "image/png",
                "data": base64.b64encode(VALID_PNG).decode("ascii"),
            },
        )

    assert response.status_code == 201
    payload = response.json()
    image = load_cached_image(payload["imageId"], cwd=session.cwd, session_id=session.session_id)
    assert image.media_type == "image/png"
    assert image.data == VALID_PNG


def test_image_upload_route_rejects_text_only_model_without_storing_image(tmp_path, monkeypatch) -> None:
    import base64

    from iac_code.web.app import create_app

    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: "text-only-model")
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: False)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="image-route-text-only")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/images",
            json={
                "mediaType": "image/png",
                "data": base64.b64encode(VALID_PNG).decode("ascii"),
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "Current model text-only-model does not support image input."}}
