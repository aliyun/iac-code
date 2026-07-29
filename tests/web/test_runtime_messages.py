import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from starlette.testclient import TestClient

VALID_PNG = b"\x89PNG\r\n\x1a\npng-data"
VALID_JPEG = b"\xff\xd8\xff\xe0jpeg-data"


def _event_types(events: list[dict[str, object]]) -> list[str]:
    return [str(event["type"]) for event in events]


def _error_message(response) -> str:
    error = response.json()["error"]
    return error["message"] if isinstance(error, dict) else error


def test_fake_stream_runtime_emits_user_and_assistant_turn_events(tmp_path) -> None:
    from iac_code.web.runtime import FakeStreamRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = FakeStreamRuntime(session, assistant_text="Hello from fake runtime")

        result = await runtime.start_turn(
            WebTurnRequest(
                text="Generate a VPC",
                image_ids=["image-1"],
                file_refs=["template.yaml"],
            )
        )

        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert isinstance(result["turnId"], str)
    assert _event_types(events) == [
        "user.message",
        "assistant.message.start",
        "assistant.text.delta",
        "assistant.message.end",
        "turn.done",
    ]

    turn_id = result["turnId"]
    user_payload = events[0]["payload"]
    assert user_payload == {
        "turnId": turn_id,
        "text": "Generate a VPC",
        "imageIds": ["image-1"],
        "fileRefs": ["template.yaml"],
        "source": "composer",
    }
    assert events[1]["payload"]["turnId"] == turn_id
    assert events[1]["payload"]["messageId"]
    assert events[2]["payload"] == {
        "turnId": turn_id,
        "messageId": events[1]["payload"]["messageId"],
        "delta": "Hello from fake runtime",
    }
    assert events[3]["payload"] == {
        "turnId": turn_id,
        "messageId": events[1]["payload"]["messageId"],
        "finishReason": "stop",
    }
    assert events[4]["payload"] == {
        "turnId": turn_id,
        "interrupted": False,
        "canceled": False,
    }


def test_fake_stream_runtime_rejects_when_turn_lock_is_held(tmp_path) -> None:
    from iac_code.web.runtime import FakeStreamRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_locked_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = FakeStreamRuntime(session, assistant_text="not emitted")

        await session.turn_lock.acquire()
        try:
            result = await runtime.start_turn(WebTurnRequest(text="hello", image_ids=[], file_refs=[]))
        finally:
            session.turn_lock.release()

        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_locked_turn())

    assert result == {"accepted": False, "reason": "turn already running"}
    assert events == []


def test_web_runtime_persists_normal_turn_messages_with_live_event_ids(tmp_path, monkeypatch) -> None:
    from iac_code.agent.message import Message
    from iac_code.types.stream_events import MessageEndEvent, MessageStartEvent, TextDeltaEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
        session = manager.create_session(session_id="stable-live-message-ids")

        class FakeAgentLoop:
            def __init__(self) -> None:
                self._session_storage = manager.storage
                self.context_messages: list[Message] = []

            async def run_streaming(self, user_input, queued_input_provider=None):
                user_message = Message(role="user", content=user_input)
                self.context_messages.append(user_message)
                self._session_storage.append(
                    session.cwd,
                    session.session_id,
                    Message(role="user", content=user_input),
                )
                yield MessageStartEvent(message_id="assistant-live-1")
                yield TextDeltaEvent(text="hello")
                yield MessageEndEvent(stop_reason="stop", usage=Usage())
                assistant_message = Message(role="assistant", content="hello", elapsed_seconds=5.0)
                self.context_messages.append(assistant_message)
                self._session_storage.append(
                    session.cwd,
                    session.session_id,
                    Message(role="assistant", content="hello"),
                )
                # AgentLoop.stamp_last_turn_elapsed() performs this whole-session
                # save for turns lasting >=1s. The context objects do not contain
                # the live IDs stamped onto the append-only storage copies.
                self._session_storage.save(
                    session.cwd,
                    session.session_id,
                    self.context_messages,
                    preserve_cleanup_prompts=True,
                )

        class FakeAgentRuntime:
            agent_loop = FakeAgentLoop()

        monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
        monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")
        result = await WebSessionRuntime(session, manager=manager).start_turn(
            WebTurnRequest(text="hi", image_ids=[], file_refs=[])
        )
        events = session.events.replay_after(0)
        transcript = manager.load_visible_transcript(session.session_id, cwd=session.cwd)["messages"]
        return result, events, transcript

    result, events, transcript = asyncio.run(run_turn())

    turn_id = str(result["turnId"])
    live_assistant_id = next(
        str(event["payload"]["messageId"]) for event in events if event["type"] == "assistant.message.start"
    )
    assert [message["messageId"] for message in transcript] == ["user-{}".format(turn_id), live_assistant_id]


def test_web_session_runtime_exposes_active_loop_during_turn_and_clears_after(tmp_path, monkeypatch) -> None:
    # 引导/立即插队端点依赖 turn 期间的 active_agent_loop/active_turn_id；此测试确认
    # start_turn 在 run_streaming 期间已把二者暴露到 session，且 turn 结束后清空，
    # 避免旧 turn 的残留指针让 steer 注入到已结束的 loop。
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    captured: dict[str, object] = {}

    async def run_turn() -> tuple[dict[str, object], object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        class FakeAgentLoop:
            async def run_streaming(self, _user_input, queued_input_provider=None):
                captured["loop_is_self"] = session.active_agent_loop is self
                captured["turn_id"] = session.active_turn_id
                captured["task_set"] = session.active_turn_task is not None
                yield MessageEndEvent(stop_reason="stop", usage=Usage())

        class FakeAgentRuntime:
            agent_loop = FakeAgentLoop()

        monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
        monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

        result = await runtime.start_turn(WebTurnRequest(text="hi", image_ids=[], file_refs=[]))
        return result, session

    result, session = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert captured["loop_is_self"] is True
    assert captured["turn_id"] == result["turnId"]
    assert captured["task_set"] is True
    # turn 结束后必须清空。
    assert session.active_agent_loop is None
    assert session.active_turn_id is None
    assert session.active_turn_task is None


def test_web_session_runtime_clears_unread_at_turn_start(tmp_path, monkeypatch) -> None:
    # 回归:上一轮无人观看结束后 unread=True;真实 start_turn 在本轮开跑时须清未读,否则
    # 「运行中」与未读并存,侧栏列表快照会把未读圆点画在正在运行的会话上(用户报「运行中显示未读」)。
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    captured: dict[str, object] = {}

    async def run_turn() -> None:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        # 模拟上一轮无人观看结束后的状态。
        session.unread = True
        runtime = WebSessionRuntime(session, manager=manager)

        class FakeAgentLoop:
            async def run_streaming(self, _user_input, queued_input_provider=None):
                # 本轮进行中(active_turn_task 已设)时,未读必须已被清除。
                captured["unread_during_turn"] = session.unread
                yield MessageEndEvent(stop_reason="stop", usage=Usage())

        class FakeAgentRuntime:
            agent_loop = FakeAgentLoop()

        monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
        monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

        await runtime.start_turn(WebTurnRequest(text="hi", image_ids=[], file_refs=[]))

    asyncio.run(run_turn())

    assert captured["unread_during_turn"] is False


def test_web_session_runtime_emits_user_bubble_for_each_queued_submission(tmp_path, monkeypatch) -> None:
    # agent 在轮次进行中消费排队输入时,只发 queued-input.submitted(移除 chip)而不发 user.message,
    # 会让这些消息在实时视图里“无气泡”——用户连发 4 条只看到 1 个气泡,数不清到底跑了几次
    # (刷新后却因存储 append 显示为 stored-N,live/reload 不一致)。这里断言每条被消费的排队输入
    # 都产生一个带唯一 messageId 的 user.message 气泡,且仍发 queued-input.submitted 清理 chip。
    from iac_code.types.stream_events import MessageEndEvent, QueuedInputSubmittedEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield QueuedInputSubmittedEvent(text="测试一下sleep 15命令", message_id="queued-live-1")
            yield QueuedInputSubmittedEvent(text="测试一下sleep 15命令", message_id="queued-live-2")
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="测试一下sleep 15命令", image_ids=[], file_refs=[]))
        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is True
    turn_id = result["turnId"]

    user_messages = [event for event in events if event["type"] == "user.message"]
    # 首条 prompt + 2 条被消费的排队输入 = 3 个用户气泡。
    assert len(user_messages) == 3
    assert [event["payload"]["text"] for event in user_messages] == [
        "测试一下sleep 15命令",
        "测试一下sleep 15命令",
        "测试一下sleep 15命令",
    ]

    # 被消费的排队输入必须带显式且互不相同的 messageId,否则同 turnId 会折叠成一个气泡
    # (messageIdFromEvent 在缺省时返回 user-<turnId>,首条 prompt 已占该 id)。
    queued_bubbles = user_messages[1:]
    queued_ids = [event["payload"]["messageId"] for event in queued_bubbles]
    assert all(isinstance(message_id, str) and message_id for message_id in queued_ids)
    assert len(set(queued_ids)) == len(queued_ids)
    initial_default_id = "user-{}".format(turn_id)
    assert all(message_id != initial_default_id for message_id in queued_ids)
    assert queued_ids == ["queued-live-1", "queued-live-2"]

    # 仍要发 queued-input.submitted 以移除输入框下方的排队 chip。
    submitted = [event for event in events if event["type"] == "queued-input.submitted"]
    assert len(submitted) == 2


def test_web_session_runtime_rejects_overlapping_start_without_queueing(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_overlapping_turns() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)
        original_publish = session.events.publish
        first_turn_started = asyncio.Event()
        release_first_turn = asyncio.Event()

        async def publish_with_barrier(event_type: str, payload: dict[str, object]) -> dict[str, object]:
            event = await original_publish(event_type, payload)
            if event_type == "user.message":
                first_turn_started.set()
                await release_first_turn.wait()
            return event

        monkeypatch.setattr(session.events, "publish", publish_with_barrier)

        first_task = asyncio.create_task(runtime.start_turn(WebTurnRequest(text="first", image_ids=[], file_refs=[])))
        await first_turn_started.wait()
        try:
            second_result = await asyncio.wait_for(
                runtime.start_turn(WebTurnRequest(text="second", image_ids=[], file_refs=[])),
                timeout=1,
            )
        finally:
            release_first_turn.set()
        first_result = await first_task

        return [first_result, second_result], session.events.replay_after(0)

    results, events = asyncio.run(run_overlapping_turns())

    accepted_results = [result for result in results if result.get("accepted") is True]
    rejected_results = [result for result in results if result.get("accepted") is False]
    assert len(accepted_results) == 1
    assert rejected_results == [{"accepted": False, "reason": "turn already running"}]
    assert _event_types(events) == ["user.message", "assistant.message.end", "turn.done"]
    assert events[0]["payload"]["text"] == "first"
    done_payload = dict(events[-1]["payload"])
    elapsed_ms = done_payload.pop("elapsedMs")
    assert isinstance(elapsed_ms, int) and elapsed_ms >= 0
    assert done_payload == {
        "turnId": accepted_results[0]["turnId"],
        "interrupted": False,
        "canceled": False,
        "usage": {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheCreationInputTokens": 0,
            "cacheReadInputTokens": 0,
            "totalTokens": 0,
        },
    }


def test_web_session_runtime_publishes_turn_done_when_agent_loop_fails_after_user_message(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            raise RuntimeError("provider exploded")
            yield

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[]))
        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is False
    assert _event_types(events) == ["user.message", "error", "turn.done"]
    assert events[2]["payload"] == {
        "turnId": result["turnId"],
        "interrupted": False,
        "canceled": False,
        "failed": True,
        "usage": {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheCreationInputTokens": 0,
            "cacheReadInputTokens": 0,
            "totalTokens": 0,
        },
    }


def test_web_session_runtime_uses_agent_factory_and_translates_stream_events(tmp_path, monkeypatch) -> None:
    from iac_code.agent.message import Message, create_recalled_memory_message
    from iac_code.services.agent_factory import AgentFactoryOptions
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        PermissionRequestEvent,
        TextDeltaEvent,
        ToolInputDeltaEvent,
        ToolResultEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object]] = []

        async def run_streaming(self, user_input, queued_input_provider=None):
            self.calls.append((user_input, queued_input_provider))
            yield MessageStartEvent(message_id="message-1")
            yield TextDeltaEvent(text="hello")
            yield ToolUseStartEvent(tool_use_id="tool-1", name="ros_stack")
            yield ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='{"StackName":')
            yield ToolUseEndEvent(tool_use_id="tool-1", name="ros_stack", input={"StackName": "demo"})
            yield ToolResultEvent(tool_use_id="tool-1", tool_name="ros_stack", result="created")
            yield PermissionRequestEvent(tool_name="bash", tool_input={"cmd": "echo hi"}, tool_use_id="tool-2")
            yield MessageEndEvent(stop_reason="stop", usage=Usage(input_tokens=3, output_tokens=5))

    class FakeAgentRuntime:
        def __init__(self) -> None:
            self.agent_loop = FakeAgentLoop()

    fake_runtime = FakeAgentRuntime()
    create_agent_runtime = Mock(return_value=fake_runtime)
    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_agent_runtime)
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]], str, str]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        hidden_memory = create_recalled_memory_message(
            "# Recalled Memory\nhidden but needed by agent",
            ["memory.md"],
        )
        manager.storage.append(session.cwd, session.session_id, Message(role="user", content="prior visible prompt"))
        manager.storage.append(session.cwd, session.session_id, hidden_memory)
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="Generate a VPC", image_ids=[], file_refs=[]))

        return result, session.events.replay_after(0), session.cwd, hidden_memory.content

    result, events, session_cwd, hidden_memory_content = asyncio.run(run_turn())

    assert result["accepted"] is True
    create_agent_runtime.assert_called_once()
    options = create_agent_runtime.call_args.args[0]
    assert isinstance(options, AgentFactoryOptions)
    assert options.model == "fake-model"
    assert options.session_id == "session-1"
    assert options.cwd == session_cwd
    assert [message.content for message in options.resume_messages] == [
        "prior visible prompt",
        hidden_memory_content,
    ]
    assert events[0]["type"] == "user.message"
    event_types = _event_types(events)
    assert "assistant.text.delta" in event_types
    assert "tool.started" in event_types
    assert "tool.input.delta" in event_types
    assert "tool.finished" in event_types
    assert "tool.result" in event_types
    assert "permission.request" in event_types
    assert events[-1]["type"] == "turn.done"
    done_payload = dict(events[-1]["payload"])
    elapsed_ms = done_payload.pop("elapsedMs")
    assert isinstance(elapsed_ms, int) and elapsed_ms >= 0
    assert done_payload == {
        "turnId": result["turnId"],
        "interrupted": False,
        "canceled": False,
        "usage": {
            "inputTokens": 3,
            "outputTokens": 5,
            "cacheCreationInputTokens": 0,
            "cacheReadInputTokens": 0,
            "totalTokens": 8,
        },
    }
    assert fake_runtime.agent_loop.calls[0][0] == "Generate a VPC"
    # 不再在轮内自动排空队列(排队消息改为逐条、各自独立成 turn),故不传 queued_input_provider。
    assert fake_runtime.agent_loop.calls[0][1] is None


def test_web_session_runtime_registers_agent_loop_permission_and_question_requests(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import AskUserQuestionEvent, MessageEndEvent, PermissionRequestEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield PermissionRequestEvent(tool_name="bash", tool_input={"cmd": "echo hi"}, tool_use_id="tool-1")
            yield AskUserQuestionEvent(
                tool_use_id="ask-1",
                question="Pick a zone",
                options=[{"id": "cn-hangzhou-a", "label": "Zone A"}],
                allow_free_text=False,
                free_text_prompt="",
            )
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]], dict[str, object], dict[str, object]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[]))

        return result, session.events.replay_after(0), session.pending_permissions, session.pending_questions

    result, events, pending_permissions, pending_questions = asyncio.run(run_turn())

    assert result["accepted"] is True
    event_types = _event_types(events)
    assert event_types.count("permission.request") == 1
    assert event_types.count("permission.resolved") == 1
    assert event_types.count("question.request") == 1
    assert event_types.count("question.resolved") == 1
    assert "debug.stream_event" not in event_types

    permission_event = next(event for event in events if event["type"] == "permission.request")
    question_event = next(event for event in events if event["type"] == "question.request")
    permission_request_id = permission_event["payload"]["requestId"]
    question_request_id = question_event["payload"]["requestId"]

    assert pending_permissions == {}
    assert pending_questions == {}
    assert permission_event["payload"] == {
        "requestId": permission_request_id,
        "payload": {
            "turnId": result["turnId"],
            "requestId": permission_request_id,
            "sessionId": "session-1",
            "toolName": "bash",
            "toolUseId": "tool-1",
            "toolInput": {"cmd": "echo hi"},
            "message": "Allow bash?",
            "suggestions": [],
            "allowAlways": False,
            "choices": [
                {"id": "allow_once", "label": "Allow once"},
                {"id": "reject_once", "label": "Deny once"},
                {"id": "always_deny", "label": "Always deny this tool"},
            ],
        },
    }
    assert question_event["payload"] == {
        "requestId": question_request_id,
        "payload": {
            "turnId": result["turnId"],
            "requestId": question_request_id,
            "sessionId": "session-1",
            "toolUseId": "ask-1",
            "question": "Pick a zone",
            "options": [{"id": "cn-hangzhou-a", "label": "Zone A"}],
            "allowFreeText": False,
            "freeTextPrompt": "",
        },
    }


def test_web_session_runtime_exposes_session_permission_context_to_agent_loop(tmp_path, monkeypatch) -> None:
    from iac_code.types.permissions import PermissionResult, PermissionRuleValue, ToolPermissionContext
    from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    observed_contexts: list[ToolPermissionContext | None] = []

    class FakeAgentLoop:
        def __init__(self) -> None:
            self._permission_context = ToolPermissionContext(cwd=str(tmp_path / "project"))
            self._permission_context_getter = None

        async def run_streaming(self, _user_input, queued_input_provider=None):
            future = asyncio.get_running_loop().create_future()
            yield PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "echo hi"},
                tool_use_id="tool-1",
                response_future=future,
                permission_result=PermissionResult(
                    behavior="ask",
                    suggestions=[PermissionRuleValue(tool_name="bash", rule_content="echo:*")],
                ),
            )
            await future
            observed_contexts.append(self._permission_context_getter())
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        def __init__(self) -> None:
            self.agent_loop = FakeAgentLoop()
            self.tool_registry = Mock()
            self.tool_registry.get.return_value = None

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)
        turn_task = asyncio.create_task(runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[])))
        deadline = asyncio.get_running_loop().time() + 1
        while not session.pending_permissions and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert len(session.pending_permissions) == 1
        request_id = next(iter(session.pending_permissions))
        manager.resolve_permission(request_id, {"choice": "always_allow"}, session_id=session.session_id)
        result = await asyncio.wait_for(turn_task, timeout=1)
        return result, session

    result, session = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert observed_contexts == [session.permission_context]
    assert session.permission_context is not None
    assert "bash(echo:*)" in session.permission_context.allow_rules["session"]


def test_web_session_runtime_exposes_session_permission_context_getter_to_agent_tool(tmp_path, monkeypatch) -> None:
    from iac_code.agent.agent_tool import AgentTool
    from iac_code.types.permissions import ToolPermissionContext
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    observed_contexts: list[ToolPermissionContext | None] = []

    class FakeAgentLoop:
        def __init__(self) -> None:
            self._permission_context = ToolPermissionContext(cwd=str(tmp_path / "project"))

        async def run_streaming(self, _user_input, queued_input_provider=None):
            observed_contexts.append(agent_tool._permission_context_getter())
            return
            yield

    agent_tool = AgentTool(permission_context=ToolPermissionContext(cwd="stale"))

    class FakeRegistry:
        def get(self, name: str):
            return agent_tool if name == "agent" else None

    class FakeAgentRuntime:
        def __init__(self) -> None:
            self.agent_loop = FakeAgentLoop()
            self.tool_registry = FakeRegistry()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)
        result = await runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[]))
        return result, session

    result, session = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert observed_contexts == [session.permission_context]


def test_web_session_runtime_offers_tool_level_always_allow_for_blanket_tool(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class BlanketTool:
        supports_blanket_allow = True

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "echo hi"},
                tool_use_id="tool-1",
            )
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeRegistry:
        def get(self, name: str):
            return BlanketTool() if name == "bash" else None

    class FakeAgentRuntime:
        def __init__(self) -> None:
            self.agent_loop = FakeAgentLoop()
            self.tool_registry = FakeRegistry()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)
        result = await runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[]))
        return result, session

    result, session = asyncio.run(run_turn())
    permission_event = next(event for event in session.events.replay_after(0) if event["type"] == "permission.request")
    payload = permission_event["payload"]["payload"]

    assert result["accepted"] is True
    assert payload["allowAlways"] is True
    assert [choice["id"] for choice in payload["choices"]] == [
        "allow_once",
        "always_allow",
        "reject_once",
        "always_deny",
    ]


def test_web_session_runtime_applies_tool_level_always_allow_for_blanket_tool(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class BlanketTool:
        supports_blanket_allow = True

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            future = asyncio.get_running_loop().create_future()
            yield PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "echo hi"},
                tool_use_id="tool-1",
                response_future=future,
            )
            await future
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeRegistry:
        def get(self, name: str):
            return BlanketTool() if name == "bash" else None

    class FakeAgentRuntime:
        def __init__(self) -> None:
            self.agent_loop = FakeAgentLoop()
            self.tool_registry = FakeRegistry()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[object, str]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)
        turn_task = asyncio.create_task(runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[])))
        deadline = asyncio.get_running_loop().time() + 1
        while not session.pending_permissions and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        permission_event = next(
            event for event in session.events.replay_after(0) if event["type"] == "permission.request"
        )
        request_id = str(permission_event["payload"]["requestId"])
        manager.resolve_permission(request_id, {"choice": "always_allow"}, session_id=session.session_id)
        await asyncio.wait_for(turn_task, timeout=1)
        return session, request_id

    session, request_id = asyncio.run(run_turn())

    assert request_id not in session.pending_permissions
    assert session.permission_context is not None
    assert "bash" in session.permission_context.allow_rules["session"]


def test_web_session_runtime_omits_always_allow_when_tool_disables_blanket_allow(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class NoBlanketTool:
        supports_blanket_allow = False

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield PermissionRequestEvent(tool_name="bash", tool_input={"cmd": "echo hi"}, tool_use_id="tool-1")
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeRegistry:
        def get(self, name: str):
            return NoBlanketTool() if name == "bash" else None

    class FakeAgentRuntime:
        def __init__(self) -> None:
            self.agent_loop = FakeAgentLoop()
            self.tool_registry = FakeRegistry()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> dict[str, object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)
        await runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[]))
        permission_event = next(
            event for event in session.events.replay_after(0) if event["type"] == "permission.request"
        )
        return permission_event["payload"]["payload"]

    payload = asyncio.run(run_turn())

    assert payload["allowAlways"] is False
    assert [choice["id"] for choice in payload["choices"]] == [
        "allow_once",
        "reject_once",
        "always_deny",
    ]


def test_web_session_runtime_registers_wrapped_permission_and_question_requests(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import (
        AskUserQuestionEvent,
        MessageEndEvent,
        PermissionRequestEvent,
        SubPipelineStreamEvent,
        Usage,
    )
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield SubPipelineStreamEvent(
                sub_pipeline_id="candidate-a",
                candidate_index=2,
                inner=PermissionRequestEvent(
                    tool_name="bash",
                    tool_input={"cmd": "echo from candidate"},
                    tool_use_id="tool-1",
                ),
            )
            yield SubPipelineStreamEvent(
                sub_pipeline_id="candidate-b",
                candidate_index=3,
                inner=AskUserQuestionEvent(
                    tool_use_id="ask-1",
                    question="Pick a zone",
                    options=[{"id": "cn-hangzhou-a", "label": "Zone A"}],
                    allow_free_text=False,
                    free_text_prompt="",
                ),
            )
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]], dict[str, object], dict[str, object]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[]))

        return result, session.events.replay_after(0), session.pending_permissions, session.pending_questions

    result, events, pending_permissions, pending_questions = asyncio.run(run_turn())

    assert result["accepted"] is True
    event_types = _event_types(events)
    assert event_types.count("permission.request") == 1
    assert event_types.count("permission.resolved") == 1
    assert event_types.count("question.request") == 1
    assert event_types.count("question.resolved") == 1
    assert "debug.stream_event" not in event_types

    permission_event = next(event for event in events if event["type"] == "permission.request")
    question_event = next(event for event in events if event["type"] == "question.request")
    permission_request_id = permission_event["payload"]["requestId"]
    question_request_id = question_event["payload"]["requestId"]

    assert pending_permissions == {}
    assert pending_questions == {}
    assert permission_event["payload"] == {
        "requestId": permission_request_id,
        "payload": {
            "turnId": result["turnId"],
            "requestId": permission_request_id,
            "sessionId": "session-1",
            "toolName": "bash",
            "toolUseId": "tool-1",
            "toolInput": {"cmd": "echo from candidate"},
            "message": "Allow bash?",
            "suggestions": [],
            "allowAlways": False,
            "choices": [
                {"id": "allow_once", "label": "Allow once"},
                {"id": "reject_once", "label": "Deny once"},
                {"id": "always_deny", "label": "Always deny this tool"},
            ],
            "subPipelineId": "candidate-a",
            "candidateIndex": 2,
        },
    }
    assert question_event["payload"] == {
        "requestId": question_request_id,
        "payload": {
            "turnId": result["turnId"],
            "requestId": question_request_id,
            "sessionId": "session-1",
            "toolUseId": "ask-1",
            "question": "Pick a zone",
            "options": [{"id": "cn-hangzhou-a", "label": "Zone A"}],
            "allowFreeText": False,
            "freeTextPrompt": "",
            "subPipelineId": "candidate-b",
            "candidateIndex": 3,
        },
    }


def test_web_session_runtime_accumulates_usage_across_message_end_events(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield MessageEndEvent(
                stop_reason="tool_use",
                usage=Usage(
                    input_tokens=1,
                    output_tokens=2,
                    cache_creation_input_tokens=3,
                    cache_read_input_tokens=4,
                ),
            )
            yield MessageEndEvent(
                stop_reason="stop",
                usage=Usage(
                    input_tokens=5,
                    output_tokens=6,
                    cache_creation_input_tokens=7,
                    cache_read_input_tokens=8,
                ),
            )

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="Deploy", image_ids=[], file_refs=[]))

        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert _event_types(events) == ["user.message", "assistant.message.end", "assistant.message.end", "turn.done"]
    done_payload = dict(events[-1]["payload"])
    assert isinstance(done_payload.pop("elapsedMs"), int)
    assert done_payload == {
        "turnId": result["turnId"],
        "interrupted": False,
        "canceled": False,
        "usage": {
            "inputTokens": 6,
            "outputTokens": 8,
            "cacheCreationInputTokens": 10,
            "cacheReadInputTokens": 12,
            "totalTokens": 36,
        },
    }


def test_web_session_runtime_builds_image_blocks_from_real_temp_cache_and_file_reference_text(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.agent.message import ImageBlock, TextBlock
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.images import store_cached_image
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    captured_inputs: list[object] = []

    class FakeAgentLoop:
        async def run_streaming(self, user_input, queued_input_provider=None):
            captured_inputs.append(user_input)
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        store_cached_image(
            "image-1",
            VALID_PNG,
            media_type="image/png",
            cwd=session.cwd,
            session_id=session.session_id,
        )

        runtime = WebSessionRuntime(session, manager=manager)
        result = await runtime.start_turn(
            WebTurnRequest(text="Review this", image_ids=["image-1"], file_refs=["templates/main.yaml"])
        )
        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert _event_types(events) == ["user.message", "assistant.message.end", "turn.done"]
    assert len(captured_inputs) == 1
    user_input = captured_inputs[0]
    assert isinstance(user_input, list)
    assert len(user_input) == 2
    text_block, image_block = user_input
    assert isinstance(text_block, TextBlock)
    assert text_block.text == "Review this\n\nReferenced files:\n- templates/main.yaml"
    assert isinstance(image_block, ImageBlock)
    assert image_block.media_type == "image/png"
    assert image_block.data == "iVBORw0KGgpwbmctZGF0YQ=="


def test_web_image_cache_scopes_same_session_and_image_id_by_cwd(tmp_path, monkeypatch) -> None:
    from iac_code.web.images import load_cached_image, store_cached_image

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    cwd_a = str(tmp_path / "project-a")
    cwd_b = str(tmp_path / "project-b")

    store_cached_image("image-1", VALID_PNG, media_type="image/png", cwd=cwd_a, session_id="session-1")
    store_cached_image("image-1", VALID_JPEG, media_type="image/jpeg", cwd=cwd_b, session_id="session-1")

    image_a = load_cached_image("image-1", cwd=cwd_a, session_id="session-1")
    image_b = load_cached_image("image-1", cwd=cwd_b, session_id="session-1")

    assert image_a.media_type == "image/png"
    assert image_a.data == VALID_PNG
    assert image_b.media_type == "image/jpeg"
    assert image_b.data == VALID_JPEG


def test_store_cached_image_writes_all_bytes_when_os_write_is_partial(tmp_path, monkeypatch) -> None:
    from iac_code.web import images

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    original_write = images.os.write
    writes: list[bytes] = []

    def partial_write(fd: int, data: bytes) -> int:
        chunk = bytes(data[: max(1, len(data) // 2)])
        writes.append(chunk)
        return original_write(fd, chunk)

    monkeypatch.setattr(images.os, "write", partial_write)

    images.store_cached_image(
        "image-1",
        VALID_PNG,
        media_type="image/png",
        cwd=str(tmp_path),
        session_id="session-1",
    )

    assert images.load_cached_image("image-1", cwd=str(tmp_path), session_id="session-1").data == VALID_PNG
    assert len(writes) > 1


def test_web_image_cache_validates_image_ids(tmp_path, monkeypatch) -> None:
    from iac_code.web.images import load_cached_image, store_cached_image

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="image id is invalid"):
        store_cached_image("../escape", b"bytes", media_type="image/png", cwd=str(tmp_path), session_id="session-1")

    with pytest.raises(ValueError, match="image id is invalid"):
        load_cached_image("../escape", cwd=str(tmp_path), session_id="session-1")


def test_web_image_cache_validates_magic_bytes_and_media_type(tmp_path, monkeypatch) -> None:
    from iac_code.web.images import store_cached_image

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="supported image"):
        store_cached_image(
            "image-1",
            b"not an image",
            media_type="image/png",
            cwd=str(tmp_path),
            session_id="session-1",
        )

    with pytest.raises(ValueError, match="does not match media type"):
        store_cached_image("image-2", VALID_PNG, media_type="image/jpeg", cwd=str(tmp_path), session_id="session-1")


def test_web_session_runtime_publishes_error_when_input_conversion_fails(tmp_path, monkeypatch) -> None:
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    create_agent_runtime = Mock()
    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_agent_runtime)
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="", image_ids=[], file_refs=["../secret.yaml"]))
        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is False
    assert result["reason"] == "runtime error"
    assert isinstance(result["turnId"], str)
    assert _event_types(events) == ["error", "turn.done"]
    assert events[0]["payload"] == {
        "turnId": result["turnId"],
        "message": "file reference escapes the workspace",
        "retryable": False,
    }
    assert events[1]["payload"]["failed"] is True
    create_agent_runtime.assert_not_called()


def test_normal_interrupt_with_attachments_preserves_draft_instead_of_dropping_refs(tmp_path) -> None:
    import asyncio

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-1")
    loop = asyncio.new_event_loop()
    task = loop.create_task(asyncio.sleep(60))
    session.active_turn_task = task
    app = create_app(session_manager=manager)

    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/sessions/{session.session_id}/interrupt",
                json={"message": "use this", "imageIds": ["img-1"], "fileRefs": ["template.yaml"]},
            )
    finally:
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()

    assert response.status_code == 400
    assert response.json() == {
        "accepted": False,
        "error": {
            "code": "interrupt_attachments_not_supported",
            "message": "normal-mode interrupts do not support attachments",
        },
        "draft": {
            "message": "use this",
            "imageIds": ["img-1"],
            "fileRefs": ["template.yaml"],
        },
    }
    assert session.events.replay_after(0) == []


def test_web_session_runtime_preserves_local_exception_messages(tmp_path, monkeypatch) -> None:
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    create_agent_runtime = Mock(side_effect=RuntimeError("provider failed api_key=sk-runtimesecret12345678"))
    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_agent_runtime)
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> tuple[dict[str, object], list[dict[str, object]]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        runtime = WebSessionRuntime(session, manager=manager)

        result = await runtime.start_turn(WebTurnRequest(text="hello", image_ids=[], file_refs=[]))
        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is False
    assert _event_types(events) == ["error", "turn.done"]
    assert "sk-runtimesecret" in events[0]["payload"]["message"]


def test_web_session_runtime_requires_injected_manager(tmp_path) -> None:
    from iac_code.web.runtime import WebSessionRuntime
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")

    with pytest.raises(TypeError):
        WebSessionRuntime(session)


def test_web_session_runtime_keeps_agent_loop_persistence_replayable(tmp_path, monkeypatch) -> None:
    from iac_code.agent.message import Message
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_turn() -> list[dict[str, object]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")

        class FakeAgentLoop:
            async def run_streaming(self, user_input, queued_input_provider=None):
                manager.storage.append(session.cwd, session.session_id, Message(role="user", content=user_input))
                manager.storage.append(session.cwd, session.session_id, Message(role="assistant", content="done"))
                yield MessageEndEvent(stop_reason="stop", usage=Usage())

        class FakeAgentRuntime:
            agent_loop = FakeAgentLoop()

        monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
        monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

        runtime = WebSessionRuntime(session, manager=manager)
        await runtime.start_turn(WebTurnRequest(text="persist me", image_ids=[], file_refs=[]))

        assert manager.storage.session_path(session.cwd, session.session_id).name == "session.jsonl"
        return manager.load_visible_messages(session.session_id, cwd=session.cwd)

    messages = asyncio.run(run_turn())

    assert [{"role": message["role"], "content": message["content"]} for message in messages] == [
        {"role": "user", "content": "persist me"},
        {"role": "assistant", "content": "done"},
    ]


def test_web_session_runtime_does_not_auto_drain_queued_inputs_mid_turn(tmp_path, monkeypatch) -> None:
    # 排队消息改为逐条、各自独立成 turn(由 app 层在本轮结束后顺序排空),因此
    # 运行时不再向 run_streaming 传 queued_input_provider,也不在轮内注入队列。
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    seen_providers: list[object] = []

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            seen_providers.append(queued_input_provider)
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> list[str]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        manager.classify_queued_input(session, "queued while busy")
        runtime = WebSessionRuntime(session, manager=manager)
        await runtime.start_turn(WebTurnRequest(text="first", image_ids=[], file_refs=[]))
        return list(session.queued_inputs)

    remaining_queue = asyncio.run(run_turn())

    # 未传 provider,且队列在本轮内保持不变(留待逐条独立处理)。
    assert seen_providers == [None]
    assert remaining_queue == ["queued while busy"]


def test_message_route_starts_injected_runtime_in_background(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image
    from iac_code.web.runtime import WebModelSelection, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_request() -> tuple[object, object, object]:
        project = tmp_path / "project"
        project.mkdir()
        (project / "template.yaml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
        session = manager.create_session(session_id="session-1")
        store_cached_image(
            "image-1",
            VALID_PNG,
            media_type="image/png",
            cwd=str(project),
            session_id=session.session_id,
        )
        started = asyncio.Event()
        release = asyncio.Event()

        class RecordingRuntime:
            def __init__(self) -> None:
                self.requests: list[WebTurnRequest] = []

            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                self.requests.append(request)
                started.set()
                await release.wait()
                return {"accepted": True, "turnId": request.turn_id or "turn-1"}

        runtime = RecordingRuntime()
        frozen_selection = WebModelSelection(provider=None, model="qwen3.7-max", effort=None)
        monkeypatch.setattr(
            "iac_code.web.runtime.model_selection_for_session",
            lambda _session: frozen_selection,
        )
        monkeypatch.setattr(
            "iac_code.services.capabilities.multimodal.is_model_multimodal",
            lambda *args, **kwargs: True,
        )
        app = create_app(session_manager=manager, runtime_factory=lambda received_session: runtime)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                "/api/sessions/session-1/messages",
                json={"text": "hello", "imageIds": ["image-1"], "fileRefs": ["template.yaml"]},
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            assert session.active_turn_task is not None
            assert not session.active_turn_task.done()
            release.set()
            await asyncio.wait_for(session.active_turn_task, timeout=1)
        return response, runtime, session

    response, runtime, session = asyncio.run(run_request())

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert isinstance(response.json()["turnId"], str)
    assert runtime.requests[0] == WebTurnRequest(
        text="hello",
        image_ids=["image-1"],
        file_refs=["template.yaml"],
        source="composer",
        turn_id=response.json()["turnId"],
        model_selection=WebModelSelection(provider=None, model="qwen3.7-max", effort=None),
    )
    assert session.active_turn_task is None or session.active_turn_task.done()


def test_message_route_returns_conflict_when_runtime_rejects_active_turn(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")

    async def hold_turn() -> None:
        await asyncio.sleep(10)

    async def run_request() -> object:
        session.active_turn_task = asyncio.create_task(hold_turn())
        try:
            app = create_app(session_manager=manager)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                return await client.post("/api/sessions/session-1/messages", json={"text": "hello"})
        finally:
            session.active_turn_task.cancel()

    response = asyncio.run(run_request())

    assert response.status_code == 409
    assert response.json() == {"accepted": False, "reason": "turn already running"}


def test_skill_command_and_message_share_turn_admission(tmp_path, monkeypatch) -> None:
    from iac_code.skills.processor import ProcessedSkillResult
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_requests() -> tuple[list[int], list[WebTurnRequest]]:
        project = tmp_path / "project"
        skill_dir = project / "skills" / "slowtest"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Slow test skill\n---\nRender a deterministic prompt.\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
        manager.create_session(session_id="session-1")
        renderer_entered = asyncio.Event()
        release_renderer = asyncio.Event()
        release_runtime = asyncio.Event()
        requests: list[WebTurnRequest] = []

        async def fake_process_prompt_command(command, args, *, session_id="", cwd=None):
            renderer_entered.set()
            await release_renderer.wait()
            return ProcessedSkillResult(
                prompt_content="rendered prompt",
                skill_name=command.name,
                new_messages=[{"role": "user", "content": "rendered prompt"}],
            )

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                await release_runtime.wait()
                return {"accepted": True, "turnId": request.turn_id or "turn-1"}

        monkeypatch.setattr("iac_code.skills.processor.process_prompt_command", fake_process_prompt_command)
        app = create_app(session_manager=manager, runtime_factory=lambda received_session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            skill_task = asyncio.create_task(
                client.post("/api/sessions/session-1/commands", json={"command": "/slowtest arg"})
            )
            await asyncio.wait_for(renderer_entered.wait(), timeout=1)
            message_task = asyncio.create_task(client.post("/api/sessions/session-1/messages", json={"text": "hello"}))
            await asyncio.sleep(0.05)
            release_renderer.set()
            responses = await asyncio.gather(skill_task, message_task)
            release_runtime.set()
            active = manager.get_session("session-1").active_turn_task
            if active is not None:
                await asyncio.wait_for(active, timeout=1)

        return sorted(response.status_code for response in responses), requests

    statuses, requests = asyncio.run(run_requests())

    assert statuses == [202, 409]
    assert len(requests) == 1


def test_empty_interrupt_cancels_reserved_skill_turn_before_runtime_starts(tmp_path, monkeypatch) -> None:
    from iac_code.skills.processor import ProcessedSkillResult
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_requests() -> tuple[object, object, list[WebTurnRequest]]:
        project = tmp_path / "project"
        skill_dir = project / "skills" / "slowtest"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Slow test skill\n---\nRender a deterministic prompt.\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
        manager.create_session(session_id="session-1")
        renderer_entered = asyncio.Event()
        release_renderer = asyncio.Event()
        requests: list[WebTurnRequest] = []

        async def fake_process_prompt_command(command, args, *, session_id="", cwd=None):
            renderer_entered.set()
            await release_renderer.wait()
            return ProcessedSkillResult(
                prompt_content="rendered prompt",
                skill_name=command.name,
                new_messages=[{"role": "user", "content": "rendered prompt"}],
            )

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                return {"accepted": True, "turnId": request.turn_id or "turn-1"}

        monkeypatch.setattr("iac_code.skills.processor.process_prompt_command", fake_process_prompt_command)
        app = create_app(session_manager=manager, runtime_factory=lambda received_session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            skill_task = asyncio.create_task(
                client.post("/api/sessions/session-1/commands", json={"command": "/slowtest arg"})
            )
            await asyncio.wait_for(renderer_entered.wait(), timeout=1)
            interrupt_response = await client.post("/api/sessions/session-1/interrupt", json={"message": ""})
            release_renderer.set()
            skill_response = await skill_task

        return interrupt_response, skill_response, requests

    interrupt_response, skill_response, requests = asyncio.run(run_requests())

    assert interrupt_response.status_code == 200
    assert interrupt_response.json() == {"accepted": True}
    assert skill_response.status_code == 409
    assert skill_response.json() == {
        "accepted": False,
        "command": "skill",
        "skill": "slowtest",
        "reason": "turn canceled",
        "canceled": True,
        "interrupted": True,
    }
    assert requests == []


def test_canceling_skill_command_request_releases_reserved_turn(tmp_path, monkeypatch) -> None:
    from iac_code.skills.processor import ProcessedSkillResult
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_requests() -> tuple[object, list[WebTurnRequest], bool, object]:
        project = tmp_path / "project"
        skill_dir = project / "skills" / "slowtest"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Slow test skill\n---\nRender a deterministic prompt.\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
        session = manager.create_session(session_id="session-1")
        renderer_entered = asyncio.Event()
        release_runtime = asyncio.Event()
        requests: list[WebTurnRequest] = []

        async def fake_process_prompt_command(command, args, *, session_id="", cwd=None):
            renderer_entered.set()
            await asyncio.sleep(60)
            return ProcessedSkillResult(
                prompt_content="rendered prompt",
                skill_name=command.name,
                new_messages=[{"role": "user", "content": "rendered prompt"}],
            )

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                release_runtime.set()
                return {"accepted": True, "turnId": request.turn_id or "turn-1"}

        monkeypatch.setattr("iac_code.skills.processor.process_prompt_command", fake_process_prompt_command)
        app = create_app(session_manager=manager, runtime_factory=lambda received_session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            skill_task = asyncio.create_task(
                client.post("/api/sessions/session-1/commands", json={"command": "/slowtest arg"})
            )
            await asyncio.wait_for(renderer_entered.wait(), timeout=1)
            skill_task.cancel()
            cancellation = await asyncio.gather(skill_task, return_exceptions=True)
            response = await asyncio.wait_for(
                client.post("/api/sessions/session-1/messages", json={"text": "hello"}),
                timeout=1,
            )
            await asyncio.wait_for(release_runtime.wait(), timeout=1)
            active = session.active_turn_task
            if active is not None:
                await asyncio.wait_for(active, timeout=1)

        return response, requests, session.turn_admission_lock.locked(), cancellation[0]

    response, requests, lock_held, cancellation = asyncio.run(run_requests())

    assert isinstance(cancellation, asyncio.CancelledError)
    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert [request.text for request in requests] == ["hello"]
    assert lock_held is False


def test_canceling_message_during_pre_start_cleanup_releases_reserved_turn(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_requests() -> tuple[object, list[WebTurnRequest], bool, object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        cleanup_entered = asyncio.Event()
        release_runtime = asyncio.Event()
        requests: list[WebTurnRequest] = []
        cleanup_calls = 0

        async def fake_session_cleanup_summary(_session):
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls > 1:
                return {"status": "clean"}
            cleanup_entered.set()
            await asyncio.sleep(60)
            return {"status": "clean"}

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                release_runtime.set()
                return {"accepted": True, "turnId": request.turn_id or "turn-1"}

        monkeypatch.setattr("iac_code.web.cleanup.session_cleanup_summary", fake_session_cleanup_summary)
        app = create_app(session_manager=manager, runtime_factory=lambda received_session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            message_task = asyncio.create_task(client.post("/api/sessions/session-1/messages", json={"text": "first"}))
            await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
            message_task.cancel()
            cancellation = await asyncio.gather(message_task, return_exceptions=True)
            response = await asyncio.wait_for(
                client.post("/api/sessions/session-1/messages", json={"text": "second"}),
                timeout=1,
            )
            await asyncio.wait_for(release_runtime.wait(), timeout=1)
            active = session.active_turn_task
            if active is not None:
                await asyncio.wait_for(active, timeout=1)

        return response, requests, session.turn_admission_lock.locked(), cancellation[0]

    response, requests, lock_held, cancellation = asyncio.run(run_requests())

    assert isinstance(cancellation, asyncio.CancelledError)
    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert [request.text for request in requests] == ["second"]
    assert lock_held is False


def test_message_route_returns_json_404_for_missing_session(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions/missing/messages", json={"text": "hello"})

    assert response.status_code == 404
    assert response.json() == {"error": {"message": "session not found"}}


def test_message_route_accepts_file_reference_only_message(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebModelSelection, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class RecordingRuntime:
        def __init__(self) -> None:
            self.requests: list[WebTurnRequest] = []

        async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
            self.requests.append(request)
            return {"accepted": True, "turnId": "turn-1"}

    project = tmp_path / "project"
    project.mkdir()
    (project / "template.yaml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    manager.create_session(session_id="session-1")
    runtime = RecordingRuntime()
    frozen_selection = WebModelSelection(provider=None, model="qwen3.7-max", effort=None)
    monkeypatch.setattr(
        "iac_code.web.runtime.model_selection_for_session",
        lambda _session: frozen_selection,
    )
    app = create_app(session_manager=manager, runtime_factory=lambda _session: runtime)

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json={"fileRefs": ["template.yaml"]})

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert runtime.requests == [
        WebTurnRequest(
            text="",
            image_ids=[],
            file_refs=["template.yaml"],
            source="composer",
            turn_id=response.json()["turnId"],
            model_selection=WebModelSelection(provider=None, model="qwen3.7-max", effort=None),
        )
    ]


def test_message_route_rejects_unavailable_file_ref_before_background_turn(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingRuntime:
        def __init__(self) -> None:
            self.called = False

        async def start_turn(self, _request) -> dict[str, object]:
            self.called = True
            return {"accepted": True, "turnId": "turn-1"}

    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    manager.create_session(session_id="session-1")
    runtime = RecordingRuntime()
    app = create_app(session_manager=manager, runtime_factory=lambda _session: runtime)

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json={"fileRefs": ["missing.yaml"]})

    assert response.status_code == 400
    assert _error_message(response) == "file reference is not available: missing.yaml"
    assert runtime.called is False


def test_message_route_rejects_missing_image_id_before_background_turn(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingRuntime:
        def __init__(self) -> None:
            self.called = False

        async def start_turn(self, _request) -> dict[str, object]:
            self.called = True
            return {"accepted": True, "turnId": "turn-1"}

    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.create_session(session_id="session-1")
    runtime = RecordingRuntime()
    app = create_app(session_manager=manager, runtime_factory=lambda _session: runtime)

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json={"text": "look", "imageIds": ["missing"]})

    assert response.status_code == 400
    assert _error_message(response) == "image is not available: missing"
    assert runtime.called is False


def test_message_route_rejects_whitespace_only_text_without_image(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json={"text": "   "})

    assert response.status_code == 400
    assert _error_message(response) == "message text, image, or file is required"


def test_message_route_accepts_whitespace_only_text_with_image(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image
    from iac_code.web.session_manager import WebSessionManager

    class AcceptingRuntime:
        async def start_turn(self, _request) -> dict[str, object]:
            return {"accepted": True, "turnId": "turn-1"}

    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    store_cached_image("image-1", VALID_PNG, media_type="image/png", cwd=session.cwd, session_id=session.session_id)
    app = create_app(session_manager=manager, runtime_factory=lambda _session: AcceptingRuntime())

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json={"text": "   ", "imageIds": ["image-1"]})

    assert response.status_code == 202
    assert response.json()["accepted"] is True


def test_message_route_rejects_image_ids_for_text_only_model(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: "text-only-model")
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: False)
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json={"text": "look", "imageIds": ["image-1"]})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "Current model text-only-model does not support image input."}}


def test_message_route_rejects_malformed_json(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/session-1/messages",
            content="{",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "malformed JSON request body"}}


def test_message_route_rejects_non_object_json(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json=[])

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "request body must be a JSON object"}}


def test_message_route_rejects_invalid_message_field_types(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        responses = [
            client.post("/api/sessions/session-1/messages", json={"text": 123}),
            client.post("/api/sessions/session-1/messages", json={"imageIds": "image-1"}),
            client.post("/api/sessions/session-1/messages", json={"imageIds": ["image-1", 2]}),
            client.post("/api/sessions/session-1/messages", json={"fileRefs": "template.yaml"}),
            client.post("/api/sessions/session-1/messages", json={"fileRefs": ["template.yaml", 2]}),
        ]

    assert [response.status_code for response in responses] == [400, 400, 400, 400, 400]
    assert [_error_message(response) for response in responses] == [
        "text must be a string",
        "imageIds must be a list of strings",
        "imageIds must be a list of strings",
        "fileRefs must be a list of strings",
        "fileRefs must be a list of strings",
    ]


def test_default_runtime_route_uses_agent_runtime_factory(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    completed = threading.Event()

    class FakeAgentLoop:
        async def run_streaming(self, user_input, queued_input_provider=None):
            assert user_input == "hello"
            # 排队消息改为逐条独立成 turn,不再在轮内批量注入,故不传 provider。
            assert queued_input_provider is None
            yield TextDeltaEvent(text="from agent")
            yield MessageEndEvent(stop_reason="stop", usage=Usage())
            completed.set()

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    create_agent_runtime = Mock(return_value=FakeAgentRuntime())
    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_agent_runtime)
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions/session-1/messages", json={"text": "hello"})
        assert completed.wait(timeout=1)

    assert response.status_code == 202
    result = response.json()
    assert result["accepted"] is True
    assert isinstance(result["turnId"], str)
    create_agent_runtime.assert_called_once()

    events = session.events.replay_after(0)
    assert _event_types(events) == ["user.message", "assistant.text.delta", "assistant.message.end", "turn.done"]
    assert events[0]["payload"] == {
        "turnId": result["turnId"],
        "text": "hello",
        "imageIds": [],
        "fileRefs": [],
        "source": "composer",
    }
    done_payload = dict(events[3]["payload"])
    assert isinstance(done_payload.pop("elapsedMs"), int)
    assert done_payload == {
        "turnId": result["turnId"],
        "interrupted": False,
        "canceled": False,
        "usage": {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheCreationInputTokens": 0,
            "cacheReadInputTokens": 0,
            "totalTokens": 0,
        },
    }


def test_web_session_runtime_stamps_turn_elapsed_when_turn_takes_time(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        def __init__(self) -> None:
            self.stamped: list[float] = []

        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

        def stamp_last_turn_elapsed(self, elapsed: float) -> None:
            self.stamped.append(elapsed)

    fake_loop = FakeAgentLoop()

    class FakeAgentRuntime:
        agent_loop = fake_loop

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")
    # 强制本轮 wall-clock 耗时为 5s，确保会持久化耗时。只替换 runtime 命名空间里的 time，
    # 不动全局 time 模块，避免影响 asyncio 事件循环。
    clock = iter([100.0, 105.0])
    monkeypatch.setattr("iac_code.web.runtime.time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def run_turn() -> list[dict[str, object]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-elapsed")
        runtime = WebSessionRuntime(session, manager=manager)
        await runtime.start_turn(WebTurnRequest(text="deploy", image_ids=[], file_refs=[]))
        return session.events.replay_after(0)

    events = asyncio.run(run_turn())

    assert fake_loop.stamped == [pytest.approx(5.0)]
    assert events[-1]["type"] == "turn.done"
    assert events[-1]["payload"]["elapsedMs"] == 5000


def test_web_session_runtime_skips_stamp_for_sub_second_turn(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        def __init__(self) -> None:
            self.stamped: list[float] = []

        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

        def stamp_last_turn_elapsed(self, elapsed: float) -> None:
            self.stamped.append(elapsed)

    fake_loop = FakeAgentLoop()

    class FakeAgentRuntime:
        agent_loop = fake_loop

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")
    clock = iter([100.0, 100.2])
    monkeypatch.setattr("iac_code.web.runtime.time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def run_turn() -> None:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-fast")
        runtime = WebSessionRuntime(session, manager=manager)
        await runtime.start_turn(WebTurnRequest(text="hi", image_ids=[], file_refs=[]))

    asyncio.run(run_turn())

    assert fake_loop.stamped == []


def test_web_session_runtime_emits_live_context_usage_on_message_end_and_turn_done(tmp_path, monkeypatch) -> None:
    # 回归:上下文进度圆环须在 turn 进行中就更新。每次 MessageEndEvent(每个模型往返)都应
    # 携带实时 contextUsage(取自活跃 agent_loop 的 context_manager),turn.done 再带一次结算值,
    # 前端据此在本轮期间实时刷新圆环,而不是只在会话加载/切换时才更新。
    from unittest.mock import Mock

    from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeContextManager:
        def get_usage(self) -> dict[str, object]:
            return {
                "total_tokens": 30533,
                "context_window": 131072,
                "usage_percent": 23.3,
                "message_count": 4,
            }

    class FakeAgentLoop:
        def __init__(self) -> None:
            self.context_manager = FakeContextManager()

        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield TextDeltaEvent(text="working")
            yield MessageEndEvent(stop_reason="stop", usage=Usage(input_tokens=3, output_tokens=5))

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> list[dict[str, object]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-ctx")
        runtime = WebSessionRuntime(session, manager=manager)
        await runtime.start_turn(WebTurnRequest(text="hi", image_ids=[], file_refs=[]))
        return session.events.replay_after(0)

    events = asyncio.run(run_turn())
    by_type = {str(event["type"]): dict(event["payload"]) for event in events}

    expected_usage = {
        "totalTokens": 30533,
        "contextWindow": 131072,
        "usagePercent": 23.3,
        "messageCount": 4,
    }
    assert by_type["assistant.message.end"]["contextUsage"] == expected_usage
    assert by_type["turn.done"]["contextUsage"] == expected_usage


def test_web_session_runtime_omits_context_usage_when_loop_has_no_context_manager(tmp_path, monkeypatch) -> None:
    # 兜底:agent_loop 没有 context_manager 时不应崩溃,也不应附带 contextUsage 字段。
    from unittest.mock import Mock

    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input, queued_input_provider=None):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", Mock(return_value=FakeAgentRuntime()))
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "fake-model")

    async def run_turn() -> list[dict[str, object]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-noctx")
        runtime = WebSessionRuntime(session, manager=manager)
        await runtime.start_turn(WebTurnRequest(text="hi", image_ids=[], file_refs=[]))
        return session.events.replay_after(0)

    events = asyncio.run(run_turn())
    by_type = {str(event["type"]): dict(event["payload"]) for event in events}
    assert "contextUsage" not in by_type["assistant.message.end"]
    assert "contextUsage" not in by_type["turn.done"]


def test_web_model_selection_freezes_ordinary_provider_configuration(tmp_path, monkeypatch) -> None:
    from iac_code.web.runtime import agent_factory_options_for_session, model_selection_for_session
    from iac_code.web.session_manager import WebSessionManager

    provider_config = {
        "model": "glm-5.2",
        "apiBase": "https://snapshot.invalid/v1",
        "effort": "high",
        "thinkingEnabled": True,
        "thinkingBudget": 4096,
        "maxCompletionTokens": 12000,
        "models": {"glm-5.2": {"thinkingBudget": 2048}},
    }
    monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "glm-5.2")
    monkeypatch.setattr("iac_code.config.load_saved_effort", lambda: "high")
    monkeypatch.setattr("iac_code.config.get_provider_config", lambda provider: provider_config)
    monkeypatch.setattr(
        "iac_code.config.load_credentials",
        lambda model=None: {"dashscope": "snapshot-key"},
    )
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="ordinary-provider-snapshot")

    selection = model_selection_for_session(session)
    provider_config["thinkingBudget"] = 1
    provider_config["models"]["glm-5.2"]["thinkingBudget"] = 1
    options = agent_factory_options_for_session(session, manager, model_selection=selection)

    assert selection.provider == "dashscope"
    assert selection.model == "glm-5.2"
    assert selection.provider_api_key == "snapshot-key"
    assert selection.provider_base_url == "https://snapshot.invalid/v1"
    assert selection.provider_config_frozen is True
    assert selection.provider_config_override == {
        "model": "glm-5.2",
        "apiBase": "https://snapshot.invalid/v1",
        "effort": "high",
        "thinkingEnabled": True,
        "thinkingBudget": 4096,
        "maxCompletionTokens": 12000,
        "models": {"glm-5.2": {"thinkingBudget": 2048}},
    }
    assert options.provider_config_override == selection.provider_config_override
