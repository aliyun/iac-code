import asyncio
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient


def _run_frontend_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    events_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/events.js"
    blocking_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/blocking.js"
    script = tmp_path / "blocking-reducer-test.mjs"
    script_source = (
        source.strip()
        .replace("__EVENTS_MODULE__", json.dumps(events_js.as_uri()))
        .replace("__BLOCKING_MODULE__", json.dumps(blocking_js.as_uri()))
    )
    script.write_text(script_source, encoding="utf-8")

    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_reducer_script(tmp_path: Path, source: str) -> dict[str, object]:
    return _run_frontend_script(tmp_path, source)


async def _wait_for_event(session, event_type: str) -> dict[str, object]:
    # Runtime creation can take longer than one second on a loaded Windows CI
    # worker. The assertion is about the event, not initialization latency.
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        for event in session.events.replay_after(0):
            if event["type"] == event_type:
                return event
        await asyncio.sleep(0.01)
    raise AssertionError(f"{event_type} event was not published")


def _choice_ids(payload: dict[str, object]) -> list[str]:
    choices = payload.get("choices")
    assert isinstance(choices, list)
    return [str(choice.get("id")) for choice in choices if isinstance(choice, dict)]


@pytest.mark.parametrize("event_kind", ["permission", "question", "draft", "queued"])
def test_manager_append_paths_wake_live_sse_stream(tmp_path, event_kind: str) -> None:
    from iac_code.web.session_manager import WebSessionManager

    async def receive_live_event() -> dict[str, object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session()
        stream = session.events.stream_after(0)
        pending_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)

        if event_kind == "permission":
            manager.add_permission_request(session.session_id, {"action": "shell"})
            expected_type = "permission.request"
        elif event_kind == "question":
            manager.add_question_request(session.session_id, {"question": "Proceed?"})
            expected_type = "question.request"
        elif event_kind == "draft":
            manager.classify_queued_input(session.session_id, " /status")
            expected_type = "draft.updated"
        else:
            manager.classify_queued_input(session.session_id, "continue")
            expected_type = "queued-input.accepted"

        event = await asyncio.wait_for(pending_event, timeout=1)
        await stream.aclose()
        assert event["type"] == expected_type
        return event

    received = asyncio.run(receive_live_event())

    assert received["sequence"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "expected_allowed"), [("allow_once", True), ("reject_once", False)])
async def test_permission_answer_resolves_agent_loop_future(
    tmp_path,
    monkeypatch,
    choice: str,
    expected_allowed: bool,
) -> None:
    from iac_code.types.permissions import PermissionResult, PermissionRuleValue
    from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, Usage
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    observed_answers: list[bool] = []

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            future = asyncio.get_running_loop().create_future()
            yield PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "echo hi"},
                tool_use_id="tool-1",
                response_future=future,
                permission_result=PermissionResult(
                    behavior="ask",
                    message="Allow bash?",
                    suggestions=[PermissionRuleValue(tool_name="bash", rule_content="echo:*")],
                ),
            )
            observed_answers.append(await future)
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-1")
    # 本用例断言本轮完整事件序列以 turn.done 收尾;关闭新会话首轮的异步 LLM 标题副作用(session.updated),
    # 避免尾随的 session.updated 污染事件序列断言。
    session.pending_llm_title = False
    runtime = WebSessionRuntime(session, manager=manager)
    app = create_app(session_manager=manager)

    turn_task = asyncio.create_task(runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[])))
    permission_event = await _wait_for_event(session, "permission.request")
    request_id = str(permission_event["payload"]["requestId"])
    payload = permission_event["payload"]["payload"]

    assert payload["requestId"] == request_id
    assert payload["sessionId"] == session.session_id
    assert payload["toolName"] == "bash"
    assert payload["toolUseId"] == "tool-1"
    assert payload["message"] == "Allow bash?"
    assert payload["suggestions"] == [{"toolName": "bash", "ruleContent": "echo:*"}]
    assert _choice_ids(payload) == ["allow_once", "always_allow", "reject_once", "always_deny"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/permissions/{request_id}/answer",
            json={"sessionId": session.session_id, "choice": choice},
        )

    assert response.status_code == 200
    result = await asyncio.wait_for(turn_task, timeout=1)
    assert result["accepted"] is True
    assert observed_answers == [expected_allowed]
    assert request_id not in session.pending_permissions
    assert session.events.replay_after(0)[-1]["type"] == "turn.done"


@pytest.mark.asyncio
async def test_question_answer_resolves_agent_loop_future(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import AskUserQuestionEvent, MessageEndEvent, Usage
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    observed_answers: list[dict[str, str] | None] = []

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            future = asyncio.get_running_loop().create_future()
            yield AskUserQuestionEvent(
                tool_use_id="ask-1",
                question="Pick a zone",
                options=[{"id": "cn-hangzhou-a", "label": "Zone A"}],
                allow_free_text=True,
                free_text_prompt="Optional detail",
                response_future=future,
            )
            observed_answers.append(await future)
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-1")
    runtime = WebSessionRuntime(session, manager=manager)
    app = create_app(session_manager=manager)

    turn_task = asyncio.create_task(runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[])))
    question_event = await _wait_for_event(session, "question.request")
    request_id = str(question_event["payload"]["requestId"])
    answer = {
        "sessionId": session.session_id,
        "selected_id": "cn-hangzhou-a",
        "selected_label": "Zone A",
        "free_text": "please continue",
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/questions/{request_id}/answer", json=answer)

    assert response.status_code == 200
    result = await asyncio.wait_for(turn_task, timeout=1)
    assert result["accepted"] is True
    assert observed_answers == [
        {
            "selected_id": "cn-hangzhou-a",
            "selected_label": "Zone A",
            "free_text": "please continue",
        }
    ]
    assert request_id not in session.pending_questions


@pytest.mark.asyncio
async def test_turn_bumps_session_updated_at(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-1")
    session.updated_at = "2000-01-01T00:00:00Z"
    runtime = WebSessionRuntime(session, manager=manager)

    result = await asyncio.wait_for(
        runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[])),
        timeout=1,
    )

    assert result["accepted"] is True
    # 轮次开始即把「上一次操作」刷新为当前,侧边栏相对时间才不会永远显示距创建多久。
    assert session.updated_at != "2000-01-01T00:00:00Z"


def test_permission_answer_from_wrong_session_returns_404_and_preserves_pending(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    owner = manager.create_session(session_id="owner-session")
    other = manager.create_session(session_id="other-session")
    request_id = manager.add_permission_request(
        owner,
        {
            "toolName": "bash",
            "toolUseId": "tool-1",
            "choices": [{"id": "allow_once", "label": "Allow once"}],
        },
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            f"/api/permissions/{request_id}/answer",
            json={"sessionId": other.session_id, "choice": "allow_once"},
        )
        assert request_id in owner.pending_permissions
        assert owner.events.replay_after(0)[-1]["type"] == "permission.request"

    assert response.status_code == 404


def test_permission_answer_rejects_choice_that_was_not_offered_without_consuming(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    request_id = manager.add_permission_request(
        session,
        {
            "toolName": "read_file",
            "toolUseId": "tool-1",
            "choices": [{"id": "allow_once", "label": "Allow once"}, {"id": "reject_once", "label": "Reject once"}],
        },
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            f"/api/permissions/{request_id}/answer",
            json={"sessionId": session.session_id, "choice": "always_allow"},
        )
        assert request_id in session.pending_permissions
        assert [event["type"] for event in session.events.replay_after(0)] == ["permission.request"]

    assert response.status_code == 400


def test_permission_payload_without_suggestions_offers_tool_level_always_actions(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-tool-rule")
    request_id = manager.add_permission_request(
        session,
        {
            "toolName": "bash",
            "toolUseId": "tool-1",
            "allowAlways": True,
        },
    )

    payload = session.pending_permissions[request_id].payload

    assert _choice_ids(payload) == ["allow_once", "always_allow", "reject_once", "always_deny"]


def test_tool_level_always_permission_choice_applies_blanket_session_rule(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-tool-rule-apply")
    request_id = manager.add_permission_request(
        session,
        {
            "toolName": "bash",
            "toolUseId": "tool-1",
            "allowAlways": True,
        },
    )

    manager.resolve_permission(request_id, {"choice": "always_deny"}, session_id=session.session_id)

    assert session.permission_context is not None
    assert "bash" in session.permission_context.deny_rules["session"]


def test_allow_permission_fails_closed_when_boundary_audit_fails(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import PermissionRequestEvent
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-audit-fail-closed")
    future = asyncio.new_event_loop().create_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"command": "touch denied"},
        tool_use_id="tool-audit-1",
        response_future=future,
        audit_context={"session_id": session.session_id, "cwd": session.cwd},
    )
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_boundary_audit",
        lambda *_args, **_kwargs: False,
    )

    request_id = manager.add_permission_request(
        session,
        {"toolName": "bash", "toolUseId": "tool-audit-1", "allowAlways": True},
        future=future,
        audit_event=event,
    )
    manager.resolve_permission(request_id, {"choice": "always_allow"}, session_id=session.session_id)

    assert future.result() is False
    assert session.permission_context is None or "bash" not in session.permission_context.allow_rules["session"]


def test_always_permission_choice_preserves_loaded_permission_context(tmp_path, monkeypatch) -> None:
    from iac_code.types.permissions import PermissionRuleValue, ToolPermissionContext
    from iac_code.web.session_manager import WebSessionManager

    loaded_context = ToolPermissionContext(
        cwd=str(tmp_path / "project"),
        deny_rules={"project": ["bash(rm:*)"]},
        trusted_read_directories=["/trusted/project"],
    )

    monkeypatch.setattr(
        "iac_code.services.permissions.loader.load_permission_context",
        lambda _cwd: loaded_context,
    )

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-rule-context")
    request_id = manager.add_permission_request(
        session,
        {
            "toolName": "bash",
            "toolUseId": "tool-1",
            "suggestions": [PermissionRuleValue(tool_name="bash", rule_content="echo:*")],
        },
    )

    manager.resolve_permission(request_id, {"choice": "always_allow"}, session_id=session.session_id)

    assert session.permission_context is not None
    assert session.permission_context.deny_rules["project"] == ["bash(rm:*)"]
    assert "/trusted/project" in session.permission_context.trusted_read_directories
    assert "bash(echo:*)" in session.permission_context.allow_rules["session"]


def test_permission_resolution_wakes_live_sse_stream(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def receive_resolution() -> dict[str, object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session()
        request_id = manager.add_permission_request(
            session,
            {
                "toolName": "bash",
                "toolUseId": "tool-1",
            },
        )
        app = create_app(session_manager=manager)
        stream = session.events.stream_after(1)
        pending_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                f"/api/permissions/{request_id}/answer",
                json={"sessionId": session.session_id, "choice": "allow_once"},
            )

        event = await asyncio.wait_for(pending_event, timeout=1)
        await stream.aclose()
        assert response.status_code == 200
        return event

    received = asyncio.run(receive_resolution())

    assert received["type"] == "permission.resolved"


def test_permission_request_survives_session_detail_and_can_answer(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    request_id = manager.add_permission_request(
        session,
        {
            "action": "shell",
            "command": "ls",
        },
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        detail_response = client.get(f"/api/sessions/{session.session_id}")
        answer_response = client.post(
            f"/api/permissions/{request_id}/answer",
            json={"sessionId": session.session_id, "choice": "allow_once"},
        )

    assert detail_response.status_code == 200
    assert detail_response.json()["pendingPermissionCount"] == 1
    assert request_id not in session.pending_permissions
    assert answer_response.status_code == 200
    assert answer_response.json() == {"requestId": request_id, "resolved": True}
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["permission.request", "permission.resolved"]
    assert events[0]["payload"] == {
        "requestId": request_id,
        "payload": {
            "action": "shell",
            "command": "ls",
            "requestId": request_id,
            "sessionId": session.session_id,
            "message": "Allow shell?",
            "suggestions": [],
            "choices": [
                {"id": "allow_once", "label": "Allow once"},
                {"id": "reject_once", "label": "Deny once"},
                {"id": "always_deny", "label": "Always deny this tool"},
            ],
        },
    }
    assert events[1]["payload"] == {
        "requestId": request_id,
        "answer": {"choice": "allow_once"},
    }


def test_interrupt_route_clears_pending_blocking_requests(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def run_interrupt() -> tuple[asyncio.Future[bool], asyncio.Future[dict[str, str] | None], list[str]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(session_id="session-1")
        permission_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        question_future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
        manager.add_permission_request(session, {"toolName": "bash"}, future=permission_future)
        manager.add_question_request(session, {"question": "Proceed?"}, future=question_future)
        app = create_app(session_manager=manager)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                "/api/sessions/session-1/interrupt",
                json={"message": "stop"},
            )

        assert response.status_code == 200
        assert response.json() == {"accepted": True}
        assert session.pending_permissions == {}
        assert session.pending_questions == {}
        return permission_future, question_future, [event["type"] for event in session.events.replay_after(0)]

    permission_future, question_future, event_types = asyncio.run(run_interrupt())

    assert permission_future.result() is False
    assert question_future.result() is None
    assert event_types == [
        "permission.request",
        "question.request",
        "permission.resolved",
        "question.resolved",
        "interrupt.accepted",
    ]


def test_session_detail_includes_pending_blocking_payloads(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    permission_id = manager.add_permission_request(
        session,
        {
            "action": "shell",
            "command": "echo ok",
            "apiKey": "sk-unsafe12345678",
        },
    )
    question_id = manager.add_question_request(
        session,
        {
            "question": "Choose",
            "options": [{"id": "yes", "label": "Yes"}],
        },
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["pendingPermissionCount"] == 1
    assert body["pendingQuestionCount"] == 1
    assert body["pendingPermissions"] == [
        {
            "requestId": permission_id,
            "payload": {
                "action": "shell",
                "command": "echo ok",
                "apiKey": "sk-unsafe12345678",
                "requestId": permission_id,
                "sessionId": session.session_id,
                "message": "Allow shell?",
                "suggestions": [],
                "choices": [
                    {"id": "allow_once", "label": "Allow once"},
                    {"id": "reject_once", "label": "Deny once"},
                    {"id": "always_deny", "label": "Always deny this tool"},
                ],
            },
        }
    ]
    assert body["pendingQuestions"] == [
        {
            "requestId": question_id,
            "payload": {
                "question": "Choose",
                "options": [{"id": "yes", "label": "Yes"}],
                "requestId": question_id,
                "sessionId": session.session_id,
            },
        }
    ]


@pytest.mark.parametrize("body", [{}, {"choice": 3}, {"answer": 3}])
def test_malformed_permission_answer_does_not_consume_pending_request(tmp_path, body: dict[str, object]) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    request_id = manager.add_permission_request(session, {"action": "shell"})
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/permissions/{request_id}/answer", json=body)
        assert request_id in session.pending_permissions
        assert session.to_dict()["pendingPermissionCount"] == 1
        assert [event["type"] for event in session.events.replay_after(0)] == ["permission.request"]

    assert response.status_code == 400


def test_permission_answer_accepts_choice_and_preserves_full_body(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    request_id = manager.add_permission_request(session.session_id, {"action": "shell"})
    app = create_app(session_manager=manager)
    answer = {"sessionId": session.session_id, "choice": "reject_once"}

    with TestClient(app) as client:
        response = client.post(f"/api/permissions/{request_id}/answer", json=answer)

    assert response.status_code == 200
    assert request_id not in session.pending_permissions
    assert session.events.replay_after(0)[1]["payload"] == {
        "requestId": request_id,
        "answer": {"choice": "reject_once"},
    }


def test_missing_permission_answer_returns_json_404_and_resolved_false(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            "/api/permissions/missing/answer",
            json={"sessionId": "missing", "choice": "reject_once"},
        )

    assert response.status_code == 404
    assert response.json() == {"requestId": "missing", "resolved": False}


def test_question_request_answer_preserves_payload_shape(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    request_id = manager.add_question_request(
        session,
        {
            "question": "Choose mode",
            "options": [{"id": "safe", "label": "Safe"}],
            "allowFreeText": True,
        },
    )
    app = create_app(session_manager=manager)
    answer = {"selected_id": "safe", "selected_label": "Safe", "free_text": "please continue"}

    with TestClient(app) as client:
        detail_response = client.get(f"/api/sessions/{session.session_id}")
        answer_response = client.post(
            f"/api/questions/{request_id}/answer",
            json={**answer, "sessionId": session.session_id},
        )

    assert detail_response.status_code == 200
    assert detail_response.json()["pendingQuestionCount"] == 1
    assert request_id not in session.pending_questions
    assert answer_response.status_code == 200
    assert answer_response.json() == {"requestId": request_id, "resolved": True}
    assert session.events.replay_after(0)[1]["payload"] == {"requestId": request_id, "answer": answer}


@pytest.mark.asyncio
async def test_question_answer_rejects_unoffered_selected_id_without_consuming(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    request_id = manager.add_question_request(
        session,
        {
            "question": "Choose mode",
            "options": [{"id": "safe", "label": "Safe"}],
        },
        future=future,
    )
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/questions/{request_id}/answer",
            json={
                "sessionId": session.session_id,
                "selected_id": "dangerous",
                "selected_label": "Dangerous",
                "free_text": "",
            },
        )

    assert response.status_code == 400
    assert request_id in session.pending_questions
    assert not future.done()
    assert [event["type"] for event in session.events.replay_after(0)] == ["question.request"]


@pytest.mark.asyncio
async def test_question_answer_rejects_free_text_with_selected_option_when_not_allowed(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    request_id = manager.add_question_request(
        session,
        {
            "question": "Choose mode",
            "options": [{"id": "safe", "label": "Safe"}],
        },
        future=future,
    )
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/questions/{request_id}/answer",
            json={
                "sessionId": session.session_id,
                "selected_id": "safe",
                "selected_label": "Safe",
                "free_text": "unexpected extra text",
            },
        )

    assert response.status_code == 400
    assert request_id in session.pending_questions
    assert not future.done()
    assert [event["type"] for event in session.events.replay_after(0)] == ["question.request"]


@pytest.mark.asyncio
async def test_question_answer_derives_selected_label_from_pending_option(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    request_id = manager.add_question_request(
        session,
        {
            "question": "Choose mode",
            "options": [{"id": "safe", "label": "Safe"}],
        },
        future=future,
    )
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/questions/{request_id}/answer",
            json={
                "sessionId": session.session_id,
                "selected_id": "safe",
                "selected_label": "Forged label",
                "free_text": "",
            },
        )

    assert response.status_code == 200
    assert future.result() == {
        "selected_id": "safe",
        "selected_label": "Safe",
        "free_text": "",
    }
    assert session.events.replay_after(0)[1]["payload"] == {
        "requestId": request_id,
        "answer": {
            "selected_id": "safe",
            "selected_label": "Safe",
            "free_text": "",
        },
    }


@pytest.mark.asyncio
async def test_question_answer_accepts_free_text_when_enabled_without_selected_id(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    request_id = manager.add_question_request(
        session,
        {
            "question": "Describe custom zone",
            "allow_free_text": True,
            "options": [{"id": "safe", "label": "Safe"}],
        },
        future=future,
    )
    app = create_app(session_manager=manager)
    answer = {
        "sessionId": session.session_id,
        "selected_id": "",
        "selected_label": "",
        "free_text": "custom answer",
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/questions/{request_id}/answer", json=answer)

    assert response.status_code == 200
    assert request_id not in session.pending_questions
    assert future.result() == {
        "selected_id": "",
        "selected_label": "",
        "free_text": "custom answer",
    }


@pytest.mark.asyncio
async def test_question_answer_rejects_selected_label_with_free_text_fallback(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    request_id = manager.add_question_request(
        session,
        {
            "question": "Describe custom zone",
            "allow_free_text": True,
            "options": [{"id": "safe", "label": "Safe"}],
        },
        future=future,
    )
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/questions/{request_id}/answer",
            json={
                "sessionId": session.session_id,
                "selected_id": "",
                "selected_label": "Forged label",
                "free_text": "custom answer",
            },
        )

    assert response.status_code == 400
    assert request_id in session.pending_questions
    assert not future.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "answer"),
    [
        (
            {"question": "Choose mode", "options": [{"id": "safe", "label": "Safe"}]},
            {"selected_id": "", "selected_label": "", "free_text": "custom answer"},
        ),
        (
            {"question": "Choose mode", "allowFreeText": False, "options": [{"id": "safe", "label": "Safe"}]},
            {"selected_id": "", "selected_label": "", "free_text": "custom answer"},
        ),
        (
            {"question": "Choose mode", "allowFreeText": True, "options": [{"id": "safe", "label": "Safe"}]},
            {"selected_id": "", "selected_label": "", "free_text": ""},
        ),
    ],
)
async def test_question_answer_rejects_invalid_free_text_fallback_without_consuming(
    tmp_path,
    payload: dict[str, object],
    answer: dict[str, str],
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    request_id = manager.add_question_request(session, payload, future=future)
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/questions/{request_id}/answer",
            json={**answer, "sessionId": session.session_id},
        )

    assert response.status_code == 400
    assert request_id in session.pending_questions
    assert not future.done()
    assert [event["type"] for event in session.events.replay_after(0)] == ["question.request"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"selected_label": "Safe", "free_text": ""},
        {"selected_id": "safe", "free_text": ""},
        {"selected_id": "safe", "selected_label": "Safe"},
        {"selected_id": 1, "selected_label": "Safe", "free_text": ""},
        {"selected_id": "safe", "selected_label": 1, "free_text": ""},
        {"selected_id": "safe", "selected_label": "Safe", "free_text": None},
    ],
)
def test_malformed_question_answer_does_not_consume_pending_request(tmp_path, body: dict[str, object]) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    request_id = manager.add_question_request(session.session_id, {"question": "Choose"})
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/questions/{request_id}/answer", json=body)
        assert request_id in session.pending_questions
        assert session.to_dict()["pendingQuestionCount"] == 1
        assert [event["type"] for event in session.events.replay_after(0)] == ["question.request"]

    assert response.status_code == 400


@pytest.mark.parametrize("text", ["/status", "$x", "!ls", "", "   ", " /status", " $skill", " !ls"])
def test_queued_non_submittable_input_becomes_draft(tmp_path, text: str) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": text})

    assert response.status_code == 200
    assert response.json() == {"accepted": False, "draft": text}
    assert session.draft == text
    event = session.events.replay_after(0)[0]
    assert event["type"] == "draft.updated"
    assert event["payload"] == {
        "draft": text,
        "reason": "not_submittable_mid_turn",
    }


def test_queued_normal_text_is_accepted_without_overwriting_current_draft(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    session.draft = "/status"
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": "continue"})

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "draft": "/status"}
    assert session.draft == "/status"
    event = session.events.replay_after(0)[0]
    assert event["type"] == "queued-input.accepted"
    assert event["payload"] == {"text": "continue", "draft": "/status"}


def test_queued_input_missing_session_returns_json_404(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions/missing/queued-inputs", json={"text": "hello"})

    assert response.status_code == 404
    assert response.json() == {"error": {"message": "session not found"}}


def test_interrupt_route_returns_accepted_and_replays_mode(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(mode="normal")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/interrupt", json={"message": "stop please"})

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    event = session.events.replay_after(0)[0]
    assert event["type"] == "interrupt.accepted"
    assert event["payload"] == {
        "message": "stop please",
        "mode": "normal",
        "imageIds": [],
        "fileRefs": [],
    }


def test_interrupt_route_rejects_normal_attachments_with_draft_payload(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-1")
    Path(session.cwd).mkdir(parents=True, exist_ok=True)
    (Path(session.cwd) / "main.yaml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    store_cached_image(
        "image-1",
        b"\x89PNG\r\n\x1a\npng-data",
        media_type="image/png",
        cwd=session.cwd,
        session_id=session.session_id,
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        rejected = client.post(
            f"/api/sessions/{session.session_id}/interrupt",
            json={"message": "use this image", "imageIds": ["image-1"], "fileRefs": ["main.yaml"]},
        )

    assert rejected.status_code == 400
    assert rejected.json() == {
        "accepted": False,
        "error": {
            "code": "interrupt_attachments_not_supported",
            "message": "normal-mode interrupts do not support attachments",
        },
        "draft": {
            "message": "use this image",
            "imageIds": ["image-1"],
            "fileRefs": ["main.yaml"],
        },
    }
    assert session.events.replay_after(0) == []


def test_interrupt_route_queues_non_empty_message_for_active_turn(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def run_interrupt() -> tuple[object, list[str], list[str]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(session_id="session-1")

        async def hold_turn() -> None:
            await asyncio.sleep(10)

        session.active_turn_task = asyncio.create_task(hold_turn())
        app = create_app(session_manager=manager)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    f"/api/sessions/{session.session_id}/interrupt",
                    json={"message": "add this context"},
                )
            return response, session.queued_inputs, [event["type"] for event in session.events.replay_after(0)]
        finally:
            session.active_turn_task.cancel()

    response, queued_inputs, event_types = asyncio.run(run_interrupt())

    assert response.status_code == 200
    assert queued_inputs == ["add this context"]
    assert event_types == ["queued-input.accepted", "interrupt.accepted"]


def test_frontend_reducer_tracks_permission_question_draft_and_interrupt(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "permission.request",
              sequence: 1,
              payload: {
                requestId: "permission-1",
                payload: { action: "shell" },
              },
            });
            state = reduceEvent(state, {
              type: "question.request",
              sequence: 2,
              payload: {
                requestId: "question-1",
                payload: { question: "Proceed?" },
              },
            });
            state = reduceEvent(state, {
              type: "draft.updated",
              sequence: 3,
              payload: { draft: "/status", reason: "not_submittable_mid_turn" },
            });
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 4,
              payload: { text: "continue", draft: "/status" },
            });
            state = reduceEvent(state, {
              type: "interrupt.accepted",
              sequence: 5,
              payload: { message: "stop", mode: "pipeline" },
            });
            state = reduceEvent(state, {
              type: "permission.resolved",
              sequence: 6,
              payload: { requestId: "permission-1", answer: "allow" },
            });
            state = reduceEvent(state, {
              type: "question.resolved",
              sequence: 7,
              payload: {
                requestId: "question-1",
                answer: { selected_id: "yes", selected_label: "Yes", free_text: "" },
              },
            });

            console.log(JSON.stringify({
              permissions: state.permissions,
              questions: state.questions,
              resolvedPermissions: state.resolvedPermissions,
              resolvedQuestions: state.resolvedQuestions,
              draft: state.draft,
              draftReason: state.draftReason,
              queuedInputs: state.queuedInputs,
              lastInterrupt: state.lastInterrupt,
            }));
            """
        ),
    )

    assert output == {
        "permissions": {},
        "questions": {},
        "resolvedPermissions": {
            "permission-1": {
                "requestId": "permission-1",
                "answer": "allow",
            },
        },
        "resolvedQuestions": {
            "question-1": {
                "requestId": "question-1",
                "answer": {
                    "selected_id": "yes",
                    "selected_label": "Yes",
                    "free_text": "",
                },
            },
        },
        "draft": "/status",
        "draftReason": "not_submittable_mid_turn",
        "queuedInputs": [{"text": "continue", "draft": "/status"}],
        "lastInterrupt": {"message": "stop", "mode": "pipeline"},
    }


def test_frontend_reducer_submitted_queued_input_is_removed(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "queued-input.accepted",
              sequence: 1,
              payload: { text: "第一条", draft: "" },
            });
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 2,
              payload: { text: "第二条", draft: "" },
            });
            // agent 消费第一条并把它变成正式一轮；提交时文本会被 strip，
            // 因此用带空白的文本验证按 trim 匹配也能命中。
            state = reduceEvent(state, {
              type: "queued-input.submitted",
              sequence: 3,
              payload: { text: "  第一条  " },
            });
            const afterFirst = state.queuedInputs.map((item) => ({ ...item }));
            state = reduceEvent(state, {
              type: "queued-input.submitted",
              sequence: 4,
              payload: { text: "第二条" },
            });

            console.log(JSON.stringify({
              afterFirst,
              afterSecond: state.queuedInputs,
            }));
            """
        ),
    )

    assert output == {
        "afterFirst": [{"text": "第二条", "draft": ""}],
        "afterSecond": [],
    }


def test_frontend_reducer_restored_queued_input_returns_to_original_index(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "queued-input.accepted",
              sequence: 1,
              payload: { text: "B", draft: "" },
            });
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 2,
              payload: { text: "A", draft: "", restored: true, index: 0 },
            });

            console.log(JSON.stringify(state.queuedInputs));
            """
        ),
    )

    assert output == [
        {"text": "A", "draft": ""},
        {"text": "B", "draft": ""},
    ]


def test_frontend_reducer_removed_and_updated_queued_inputs(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "queued-input.accepted",
              sequence: 1,
              payload: { text: "第一条", draft: "" },
            });
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 2,
              payload: { text: "第二条", draft: "" },
            });
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 3,
              payload: { text: "第三条", draft: "" },
            });
            // 编辑第二条。
            state = reduceEvent(state, {
              type: "queued-input.updated",
              sequence: 4,
              payload: { index: 1, text: "第二条改" },
            });
            const afterUpdate = state.queuedInputs.map((item) => ({ ...item }));
            // 删除第一条。
            state = reduceEvent(state, {
              type: "queued-input.removed",
              sequence: 5,
              payload: { index: 0 },
            });
            const afterRemove = state.queuedInputs.map((item) => ({ ...item }));
            // 越界删除是 no-op。
            state = reduceEvent(state, {
              type: "queued-input.removed",
              sequence: 6,
              payload: { index: 9 },
            });

            console.log(JSON.stringify({
              afterUpdate,
              afterRemove,
              afterOutOfRange: state.queuedInputs,
            }));
            """
        ),
    )

    assert output == {
        "afterUpdate": [
            {"text": "第一条", "draft": ""},
            {"text": "第二条改", "draft": ""},
            {"text": "第三条", "draft": ""},
        ],
        "afterRemove": [
            {"text": "第二条改", "draft": ""},
            {"text": "第三条", "draft": ""},
        ],
        "afterOutOfRange": [
            {"text": "第二条改", "draft": ""},
            {"text": "第三条", "draft": ""},
        ],
    }


def test_frontend_reducer_skips_queued_events_already_in_snapshot_seed(tmp_path) -> None:
    # 回归:loadSession 会用会话快照(latestSequence 时的状态)把“排队中”列表种下,
    # 同时把 lastSequence 设为 replaySequence(缓冲 floor - 1);随后 SSE 从 floor 回放,
    # 会把已计入种子的 queued-input.accepted 再投递一次。accepted 用非幂等 push,若不拦截
    # 就会出现重复行(3 条变 6 条)。此处用 queuedInputsSeedSequence 高水位复现并断言不重复。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            // 模拟 loadSession:快照高水位 latestSequence=3,已种入 3 条排队;
            // lastSequence 回退到 replaySequence(floor-1=0)以便回放缓冲。
            let state = {
              messages: {},
              queuedInputs: [
                { text: "第一条", draft: "" },
                { text: "第二条", draft: "" },
                { text: "第三条", draft: "" },
              ],
              queuedInputsSeedSequence: 3,
              lastSequence: 0,
            };

            // SSE 从 floor 回放:这三条 accepted(序号 <= 种子高水位)必须被跳过。
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 1,
              payload: { text: "第一条", draft: "" },
            });
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 2,
              payload: { text: "第二条", draft: "" },
            });
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 3,
              payload: { text: "第三条", draft: "" },
            });
            const afterReplay = state.queuedInputs.map((item) => ({ ...item }));

            // 高水位之后的真实新增仍要正常入队。
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 4,
              payload: { text: "第四条", draft: "" },
            });
            const afterNew = state.queuedInputs.map((item) => ({ ...item }));

            console.log(JSON.stringify({ afterReplay, afterNew }));
            """
        ),
    )

    assert output == {
        "afterReplay": [
            {"text": "第一条", "draft": ""},
            {"text": "第二条", "draft": ""},
            {"text": "第三条", "draft": ""},
        ],
        "afterNew": [
            {"text": "第一条", "draft": ""},
            {"text": "第二条", "draft": ""},
            {"text": "第三条", "draft": ""},
            {"text": "第四条", "draft": ""},
        ],
    }


def test_frontend_reducer_steer_makes_distinct_user_bubble_and_drops_chip(tmp_path) -> None:
    # 引导(steer)会以带显式唯一 messageId 的 user.message 立即注入用户气泡，
    # 且发 queued-input.removed 移除排队条。此处验证:
    #   1) steer 气泡与首条 prompt(user-<turnId>)是两条不同消息，不会互相覆盖；
    #   2) 被 steer 的排队条从 queuedInputs 消失。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            // 首条 prompt：无显式 messageId，reducer 归到 user-T1。
            let state = reduceEvent({}, {
              type: "user.message",
              sequence: 1,
              payload: { turnId: "T1", text: "原始问题" },
            });
            // 入队一条，稍后被引导。
            state = reduceEvent(state, {
              type: "queued-input.accepted",
              sequence: 2,
              payload: { text: "插队消息", draft: "" },
            });
            // 引导：带唯一 messageId 的 user.message + queued-input.removed。
            state = reduceEvent(state, {
              type: "user.message",
              sequence: 3,
              payload: {
                messageId: "user-T1-steer-abcd1234",
                turnId: "T1",
                text: "插队消息",
                source: "steer",
              },
            });
            state = reduceEvent(state, {
              type: "queued-input.removed",
              sequence: 4,
              payload: { index: 0 },
            });

            const messages = Object.values(state.messages)
              .filter((m) => m.role === "user")
              .map((m) => ({ id: m.messageId, text: m.text }));

            console.log(JSON.stringify({
              userMessageCount: messages.length,
              hasOriginal: messages.some((m) => m.id === "user-T1" && m.text === "原始问题"),
              hasSteer: messages.some((m) => m.id === "user-T1-steer-abcd1234" && m.text === "插队消息"),
              queuedInputs: state.queuedInputs,
            }));
            """
        ),
    )

    assert output == {
        "userMessageCount": 2,
        "hasOriginal": True,
        "hasSteer": True,
        "queuedInputs": [],
    }


def test_frontend_reducer_interrupt_clears_pending_blocking_requests(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "permission.request",
              sequence: 1,
              payload: {
                requestId: "permission-1",
                payload: { action: "shell" },
              },
            });
            state = reduceEvent(state, {
              type: "question.request",
              sequence: 2,
              payload: {
                requestId: "question-1",
                payload: { question: "Proceed?" },
              },
            });
            state = reduceEvent(state, {
              type: "interrupt.accepted",
              sequence: 3,
              payload: { message: "stop", mode: "pipeline" },
            });

            console.log(JSON.stringify({
              permissions: state.permissions,
              questions: state.questions,
              lastInterrupt: state.lastInterrupt,
            }));
            """
        ),
    )

    assert output == {
        "permissions": {},
        "questions": {},
        "lastInterrupt": {"message": "stop", "mode": "pipeline"},
    }


def test_frontend_reducer_interrupt_does_not_finish_active_turn_before_turn_done(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "user.message",
              sequence: 1,
              payload: { turnId: "turn-1", text: "hello", imageIds: [], fileRefs: [] },
            });
            state = reduceEvent(state, {
              type: "interrupt.accepted",
              sequence: 2,
              payload: { message: "", mode: "normal" },
            });
            const afterInterrupt = state.currentTurnActive;
            state = reduceEvent(state, {
              type: "turn.done",
              sequence: 3,
              payload: { turnId: "turn-1", interrupted: true, canceled: true },
            });

            console.log(JSON.stringify({
              afterInterrupt,
              afterDone: state.currentTurnActive,
              lastTurn: state.lastTurn,
            }));
            """
        ),
    )

    assert output == {
        "afterInterrupt": True,
        "afterDone": False,
        "lastTurn": {
            "turnId": "turn-1",
            "interrupted": True,
            "canceled": True,
            "elapsedMs": None,
            "usage": None,
        },
    }


def test_frontend_reducer_computes_turn_elapsed_from_event_timestamps(tmp_path) -> None:
    # turn.done has no elapsedMs of its own, so the reducer derives per-turn elapsed from the
    # createdAt timestamps of user.message (start) and turn.done (end). Powers the "已处理 <时间>"
    # collapsed-turn header.
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "user.message",
              sequence: 1,
              createdAt: "2026-07-02T10:00:00.000Z",
              payload: { turnId: "turn-1", text: "hello", imageIds: [], fileRefs: [] },
            });
            state = reduceEvent(state, {
              type: "turn.done",
              sequence: 2,
              createdAt: "2026-07-02T10:00:08.000Z",
              payload: { turnId: "turn-1", interrupted: false, canceled: false },
            });

            console.log(JSON.stringify({
              elapsedMs: state.turns["turn-1"].elapsedMs,
              done: state.turns["turn-1"].done,
              lastElapsed: state.lastTurn.elapsedMs,
            }));
            """
        ),
    )

    assert output == {"elapsedMs": 8000, "done": True, "lastElapsed": 8000}


def test_frontend_blocking_components_send_canonical_answer_payloads(tmp_path) -> None:
    output = _run_frontend_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPermissionRequest, renderQuestionRequest } from __BLOCKING_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.type = "";
              }
              append(...children) {
                this.children.push(...children);
              }
              addEventListener(type, handler) {
                this.listeners[type] = handler;
              }
              click() {
                this.listeners.click?.({ target: this });
              }
            }

            function collectButtons(node, buttons = []) {
              if (node.tagName === "BUTTON") {
                buttons.push(node);
              }
              for (const child of node.children || []) {
                collectButtons(child, buttons);
              }
              return buttons;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const permissionCalls = [];
            const permissionPanel = renderPermissionRequest(
              {
                requestId: "permission-1",
                payload: {
                  sessionId: "session-1",
                  choices: [
                    { id: "allow_once", label: "Allow once" },
                    { id: "reject_once", label: "Reject once" },
                  ],
                },
              },
              {
                onPermissionAnswer(...args) {
                  permissionCalls.push(args);
                },
              },
            );
            const permissionButtons = collectButtons(permissionPanel).filter(
              (button) => button.dataset && button.dataset.choiceId,
            );
            const optionLabel = (row) =>
              (row.children || [])
                .find((child) => (child.className || "").includes("blocking-option-label"))
                ?.textContent || "";
            permissionButtons[0].click();
            permissionButtons[1].click();

            const questionCalls = [];
            const questionPanel = renderQuestionRequest(
              {
                requestId: "question-1",
                payload: {
                  sessionId: "session-1",
                  options: [{ id: "safe", label: "Safe" }],
                },
              },
              {
                onQuestionAnswer(...args) {
                  questionCalls.push(args);
                },
              },
            );
            collectButtons(questionPanel)[0].click();

            console.log(JSON.stringify({
              permissionButtonLabels: permissionButtons.map(optionLabel),
              permissionCalls,
              questionCalls,
            }));
            """
        ),
    )

    assert output == {
        "permissionButtonLabels": ["Allow once", "Reject once"],
        "permissionCalls": [
            ["permission-1", {"sessionId": "session-1", "choice": "allow_once"}],
            ["permission-1", {"sessionId": "session-1", "choice": "reject_once"}],
        ],
        "questionCalls": [
            [
                "question-1",
                {
                    "sessionId": "session-1",
                    "selected_id": "safe",
                    "selected_label": "Safe",
                    "free_text": "",
                },
            ],
        ],
    }


def test_frontend_question_component_submits_free_text_answers(tmp_path) -> None:
    output = _run_frontend_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderQuestionRequest } from __BLOCKING_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.type = "";
                this.value = "";
                this.rows = 0;
                this.placeholder = "";
              }
              append(...children) {
                this.children.push(...children);
              }
              addEventListener(type, handler) {
                this.listeners[type] = handler;
              }
            }

            function findTag(node, tagName) {
              if (node.tagName === tagName) {
                return node;
              }
              for (const child of node.children || []) {
                const found = findTag(child, tagName);
                if (found) {
                  return found;
                }
              }
              return null;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const calls = [];
            const panel = renderQuestionRequest(
              {
                requestId: "question-1",
                payload: {
                  sessionId: "session-1",
                  question: "Describe custom zone",
                  allowFreeText: true,
                  freeTextPrompt: "Custom zone",
                },
              },
              {
                onQuestionAnswer(...args) {
                  calls.push(args);
                },
              },
            );
            const textarea = findTag(panel, "TEXTAREA");
            const form = findTag(panel, "FORM");
            textarea.value = "custom answer";
            form.listeners.submit({ preventDefault() {} });

            console.log(JSON.stringify({
              placeholder: textarea.placeholder,
              calls,
            }));
            """
        ),
    )

    assert output == {
        "placeholder": "Custom zone",
        "calls": [
            [
                "question-1",
                {
                    "sessionId": "session-1",
                    "selected_id": "",
                    "selected_label": "",
                    "free_text": "custom answer",
                },
            ],
        ],
    }


def test_frontend_question_component_combines_option_and_free_text(tmp_path) -> None:
    output = _run_frontend_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderQuestionRequest } from __BLOCKING_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.type = "";
                this.value = "";
                this.rows = 0;
                this.placeholder = "";
              }
              append(...children) {
                this.children.push(...children);
              }
              addEventListener(type, handler) {
                this.listeners[type] = handler;
              }
              click() {
                this.listeners.click?.({ target: this });
              }
            }

            function findTag(node, tagName) {
              if (node.tagName === tagName) {
                return node;
              }
              for (const child of node.children || []) {
                const found = findTag(child, tagName);
                if (found) {
                  return found;
                }
              }
              return null;
            }

            function collectButtons(node, buttons = []) {
              if (node.tagName === "BUTTON") {
                buttons.push(node);
              }
              for (const child of node.children || []) {
                collectButtons(child, buttons);
              }
              return buttons;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const calls = [];
            const panel = renderQuestionRequest(
              {
                requestId: "question-1",
                payload: {
                  sessionId: "session-1",
                  question: "Choose mode",
                  allowFreeText: true,
                  options: [{ id: "safe", label: "Safe" }],
                },
              },
              {
                onQuestionAnswer(...args) {
                  calls.push(args);
                },
              },
            );
            findTag(panel, "TEXTAREA").value = "extra context";
            const buttons = collectButtons(panel);
            buttons[0].click();
            const callsAfterOptionClick = calls.length;
            findTag(panel, "FORM").listeners.submit({ preventDefault() {} });

            console.log(JSON.stringify({ callsAfterOptionClick, selectedClass: buttons[0].className, calls }));
            """
        ),
    )

    assert output == {
        "callsAfterOptionClick": 0,
        "selectedClass": "blocking-option-row is-selected",
        "calls": [
            [
                "question-1",
                {
                    "sessionId": "session-1",
                    "selected_id": "safe",
                    "selected_label": "Safe",
                    "free_text": "extra context",
                },
            ],
        ],
    }


def test_frontend_permission_panel_shows_shell_command_and_suggested_rules(tmp_path) -> None:
    output = _run_frontend_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPermissionRequest } from __BLOCKING_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.type = "";
              }
              append(...children) {
                this.children.push(...children);
              }
              addEventListener(type, handler) {
                this.listeners[type] = handler;
              }
            }

            function textOf(node) {
              return `${node.textContent || ""} ${(node.children || []).map(textOf).join(" ")}`.trim();
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const panel = renderPermissionRequest({
              requestId: "permission-1",
              payload: {
                sessionId: "session-1",
                toolName: "bash",
                toolUseId: "shell-escape",
                message: "Allow Bash?",
                toolInput: { command: "curl https://example.com" },
                suggestions: [{ toolName: "bash", ruleContent: "curl:*" }],
                choices: [
                  { id: "allow_once", label: "Allow once" },
                  { id: "always_allow", label: "Always allow curl:*" },
                  { id: "reject_once", label: "Reject once" },
                  { id: "always_deny", label: "Always deny curl:*" },
                ],
              },
            });

            console.log(JSON.stringify({
              text: textOf(panel),
              className: panel.className,
            }));
            """
        ),
    )

    assert "curl https://example.com" in output["text"]
    assert "curl:*" in output["text"]
    assert "blocking-panel-permission" in output["className"]
