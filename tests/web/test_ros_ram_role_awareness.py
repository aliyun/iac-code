"""The Web HTTP turn rejects ROS stack roles before reaching Alibaba Cloud."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.tools.base import ToolRegistry
from iac_code.tools.cloud.aliyun.ros_stack import RosStack
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


@pytest.mark.asyncio
async def test_web_turn_rejects_ros_ram_role_before_cloud_client(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir()

    class AllowedRosStack(RosStack):
        async def check_permissions(self, input, context=None):
            return PermissionResult(behavior="allow")

    class ScriptedProvider:
        def get_model_name(self) -> str:
            return "offline-web-fixture"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="web-model-message")
            has_result = any(
                block.type == "tool_result"
                for message in messages
                if isinstance(message.content, list)
                for block in message.content
            )
            if has_result:
                yield TextDeltaEvent(text="request was blocked")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())
            else:
                yield ToolUseStartEvent(tool_use_id="blocked-call", name="ros_stack")
                yield ToolUseEndEvent(
                    tool_use_id="blocked-call",
                    name="ros_stack",
                    input={
                        "action": "DeleteStack",
                        "region_id": "cn-hangzhou",
                        "params": {"StackId": "fake-stack", "RamRoleName": "fake-role"},
                    },
                )
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    def make_runtime(_options):
        registry = ToolRegistry()
        registry.register(AllowedRosStack())
        return SimpleNamespace(
            tool_registry=registry,
            agent_loop=AgentLoop(
                provider_manager=ScriptedProvider(),
                system_prompt="offline Web test",
                tool_registry=registry,
                max_turns=3,
            ),
        )

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", make_runtime)
    monkeypatch.setattr("iac_code.web.runtime.load_saved_model", lambda: "offline-web-fixture")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    app = create_app(session_manager=manager)

    with patch.object(
        RosStack, "_get_client", side_effect=AssertionError("cloud client must not be created")
    ) as client:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            created = await http.post("/api/sessions", json={"cwd": str(project), "mode": "normal"})
            assert created.status_code == 201
            session_id = created.json()["sessionId"]
            session = manager.get_session(session_id)
            assert session is not None
            session.pending_llm_title = False
            sent = await http.post(f"/api/sessions/{session_id}/messages", json={"text": "delete fake stack"})
            assert sent.status_code == 202
            for _ in range(200):
                events = session.events.replay_after(0)
                if any(event["type"] == "turn.done" for event in events):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Web turn did not finish")

    results = [event["payload"] for event in events if event["type"] == "tool.result"]
    assert len(results) == 1
    assert results[0]["toolUseId"] == "blocked-call"
    assert results[0]["resultKind"] == "error"
    assert "RamRoleName" in results[0]["summary"]
    assert any(event["type"] == "assistant.text.delta" for event in events)
    client.assert_not_called()
