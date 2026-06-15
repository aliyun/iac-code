"""Tests for AgentLoop.inject_user_message and current_turn_text."""

from collections import deque
from unittest.mock import MagicMock

import pytest

from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    PermissionRequestEvent,
    TombstoneEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


@pytest.fixture
def mock_provider_manager():
    pm = MagicMock()
    pm.get_model_name.return_value = "test-model"
    return pm


@pytest.fixture
def agent_loop(mock_provider_manager):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.tools.base import ToolRegistry

    registry = ToolRegistry()
    loop = AgentLoop(
        provider_manager=mock_provider_manager,
        system_prompt="test",
        tool_registry=registry,
        max_turns=5,
    )
    return loop


class TestInjectUserMessage:
    def test_inject_appends_to_queue(self, agent_loop):
        assert len(agent_loop._pending_injections) == 0
        agent_loop.inject_user_message("补充信息")
        assert agent_loop._pending_injections == deque(["补充信息"])

    def test_inject_queues_multiple(self, agent_loop):
        agent_loop.inject_user_message("first")
        agent_loop.inject_user_message("second")
        assert agent_loop._pending_injections == deque(["first", "second"])


class TestCurrentTurnText:
    def test_initial_value_empty(self, agent_loop):
        assert agent_loop.current_turn_text == ""


class TestCheckedInjection:
    def test_try_inject_rejects_when_loop_not_accepting(self, agent_loop):
        assert agent_loop.can_accept_injected_user_message is False

        accepted = agent_loop.try_inject_user_message("too late")

        assert accepted is False
        assert agent_loop._pending_injections == deque()

    def test_try_inject_appends_when_loop_accepting(self, agent_loop):
        agent_loop._accepting_injected_user_messages = True

        accepted = agent_loop.try_inject_user_message("补充信息")

        assert accepted is True
        assert agent_loop._pending_injections == deque(["补充信息"])

    @pytest.mark.asyncio
    async def test_message_end_without_tools_is_not_accepting_when_observed(self):
        class NoToolProvider:
            def get_model_name(self) -> str:
                return "test-model"

            async def stream(self, messages, system, tools=None):
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        from iac_code.agent.agent_loop import AgentLoop

        loop = AgentLoop(
            provider_manager=NoToolProvider(),
            system_prompt="test",
            tool_registry=ToolRegistry(),
            max_turns=5,
        )

        stream = loop._run_streaming_inner("hello")
        try:
            event = await anext(stream)

            assert isinstance(event, MessageEndEvent)
            assert loop.can_accept_injected_user_message is False
            assert loop.try_inject_user_message("too late") is False
        finally:
            await stream.aclose()

    @pytest.mark.asyncio
    async def test_final_turn_with_complete_tool_call_never_accepts_injection(self):
        class FinalTurnToolProvider:
            def get_model_name(self) -> str:
                return "test-model"

            async def stream(self, messages, system, tools=None):
                yield ToolUseStartEvent(tool_use_id="tool1", name="quick_tool")
                yield ToolUseEndEvent(tool_use_id="tool1", name="quick_tool", input={})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

        from iac_code.agent.agent_loop import AgentLoop

        registry = ToolRegistry()
        loop = AgentLoop(
            provider_manager=FinalTurnToolProvider(),
            system_prompt="test",
            tool_registry=registry,
            max_turns=1,
        )

        stream = loop._run_streaming_inner("use tool")
        try:
            assert isinstance(await anext(stream), ToolUseStartEvent)
            assert isinstance(await anext(stream), ToolUseEndEvent)
            event = await anext(stream)

            assert isinstance(event, MessageEndEvent)
            assert loop.can_accept_injected_user_message is False
            assert loop.try_inject_user_message("too late") is False
            assert loop._pending_injections == deque()
        finally:
            await stream.aclose()

    @pytest.mark.asyncio
    async def test_retracted_tool_call_never_accepts_injection_while_streaming(self):
        class RetractedToolProvider:
            def get_model_name(self) -> str:
                return "test-model"

            async def stream(self, messages, system, tools=None):
                yield ToolUseStartEvent(tool_use_id="tool1", name="quick_tool")
                yield ToolUseEndEvent(tool_use_id="tool1", name="quick_tool", input={})
                yield TombstoneEvent(message_id="m1")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        from iac_code.agent.agent_loop import AgentLoop

        loop = AgentLoop(
            provider_manager=RetractedToolProvider(),
            system_prompt="test",
            tool_registry=ToolRegistry(),
            max_turns=2,
        )

        stream = loop._run_streaming_inner("use tool")
        try:
            assert isinstance(await anext(stream), ToolUseStartEvent)
            event = await anext(stream)

            assert isinstance(event, ToolUseEndEvent)
            assert loop.can_accept_injected_user_message is False
            assert loop.try_inject_user_message("too early") is False
            assert isinstance(await anext(stream), TombstoneEvent)
            assert loop.can_accept_injected_user_message is False
            assert isinstance(await anext(stream), MessageEndEvent)
            assert loop.can_accept_injected_user_message is False
        finally:
            await stream.aclose()

    @pytest.mark.asyncio
    async def test_complete_tool_call_accepts_after_provider_stream_before_permission(self):
        class AskTool(Tool):
            @property
            def name(self) -> str:
                return "ask_tool"

            @property
            def description(self) -> str:
                return "Ask before running."

            @property
            def input_schema(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
                return ToolResult.success("ok")

            async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
                return PermissionResult(behavior="ask", message="Allow?")

        class ToolProvider:
            def get_model_name(self) -> str:
                return "test-model"

            async def stream(self, messages, system, tools=None):
                self.messages_seen.append(messages)
                if len(self.messages_seen) == 1:
                    yield ToolUseStartEvent(tool_use_id="tool1", name="ask_tool")
                    yield ToolUseEndEvent(tool_use_id="tool1", name="ask_tool", input={})
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                else:
                    yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        from iac_code.agent.agent_loop import AgentLoop

        provider = ToolProvider()
        provider.messages_seen = []
        registry = ToolRegistry()
        registry.register(AskTool())
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
        )

        stream = loop._run_streaming_inner("use tool")
        try:
            assert isinstance(await anext(stream), ToolUseStartEvent)
            assert isinstance(await anext(stream), ToolUseEndEvent)
            assert isinstance(await anext(stream), MessageEndEvent)
            event = await anext(stream)

            assert isinstance(event, PermissionRequestEvent)
            assert loop.can_accept_injected_user_message is True
            assert loop.try_inject_user_message("补充：使用更小规格") is True
            assert event.response_future is not None
            event.response_future.set_result(False)

            while len(provider.messages_seen) < 2:
                await anext(stream)

            user_messages = [msg.content for msg in provider.messages_seen[-1] if msg.role == "user"]
            assert "补充：使用更小规格" in user_messages
        finally:
            await stream.aclose()
