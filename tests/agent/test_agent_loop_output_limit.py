"""Output truncation must continue safely or end with an explicit error."""

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import TextBlock, ThinkingBlock
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


class ScriptedProvider:
    def __init__(self, replies: list[list]) -> None:
        self.replies = replies
        self.messages: list[list] = []

    def get_model_name(self) -> str:
        return "fake"

    async def stream(self, messages, system, tools=None, max_tokens=8192):
        self.messages.append(list(messages))
        for event in self.replies[len(self.messages) - 1]:
            yield event


class RecordingTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return "record"

    @property
    def description(self) -> str:
        return "Record an input."

    @property
    def input_schema(self) -> dict:
        return {"type": "object"}

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        self.calls.append(tool_input)
        return ToolResult.success("recorded")


def reply(message_id: str, *, text: str = "", reason: str = "end_turn") -> list:
    events = [MessageStartEvent(message_id=message_id)]
    if text:
        events.append(TextDeltaEvent(text=text))
    events.append(MessageEndEvent(stop_reason=reason, usage=Usage()))
    return events


@pytest.mark.asyncio
async def test_text_truncation_continues_with_partial_text_and_no_incomplete_thinking() -> None:
    provider = ScriptedProvider(
        [
            [
                MessageStartEvent(message_id="first"),
                ThinkingDeltaEvent(text="unfinished"),
                TextDeltaEvent(text="first part"),
                MessageEndEvent(stop_reason="max_tokens", usage=Usage()),
            ],
            reply("second", text="second part"),
        ]
    )
    loop = AgentLoop(provider_manager=provider, system_prompt="system", tool_registry=ToolRegistry())

    events = [event async for event in loop.run_streaming("go")]

    assert len(provider.messages) == 2
    assert [message.role for message in provider.messages[1]] == ["user", "assistant", "user"]
    partial = loop.context_manager.get_messages()[1]
    assert partial.content == [TextBlock(text="first part")]
    assert not any(isinstance(block, ThinkingBlock) for block in partial.content)
    assert loop.context_manager.get_messages()[2].metadata["type"] == "output_limit_continuation"
    assert [event.stop_reason for event in events if isinstance(event, MessageEndEvent)] == ["max_tokens", "end_turn"]
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_truncated_tool_call_is_discarded_before_retry() -> None:
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = ScriptedProvider(
        [
            [
                MessageStartEvent(message_id="first"),
                ToolUseStartEvent(tool_use_id="call-1", name="record"),
                ToolUseEndEvent(tool_use_id="call-1", name="record", input={"value": 1}),
                MessageEndEvent(stop_reason="max_tokens", usage=Usage()),
            ],
            reply("second", text="done"),
        ]
    )
    loop = AgentLoop(provider_manager=provider, system_prompt="system", tool_registry=registry)

    events = [event async for event in loop.run_streaming("go")]

    assert len(provider.messages) == 2
    assert tool.calls == []
    assert not any(isinstance(event, ToolResultEvent) for event in events)
    assert any(isinstance(event, TombstoneEvent) and event.affected_tool_use_ids == ["call-1"] for event in events)
    assert [message.role for message in provider.messages[1]] == ["user"]


@pytest.mark.asyncio
async def test_repeated_truncation_stops_after_bounded_continuations() -> None:
    provider = ScriptedProvider([reply(str(index), text="part", reason="max_tokens") for index in range(4)])
    loop = AgentLoop(provider_manager=provider, system_prompt="system", tool_registry=ToolRegistry())

    events = [event async for event in loop.run_streaming("go")]

    assert len(provider.messages) == 4
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert "output token limit" in errors[0].i18n_message_id
    assert errors[0].is_retryable is False


@pytest.mark.asyncio
async def test_truncation_without_visible_text_reports_error_immediately() -> None:
    provider = ScriptedProvider(
        [
            [
                MessageStartEvent(message_id="first"),
                ThinkingDeltaEvent(text="unusable reasoning"),
                MessageEndEvent(stop_reason="max_tokens", usage=Usage()),
            ]
        ]
    )
    loop = AgentLoop(provider_manager=provider, system_prompt="system", tool_registry=ToolRegistry())

    events = [event async for event in loop.run_streaming("go")]

    assert len(provider.messages) == 1
    assert sum(isinstance(event, ErrorEvent) for event in events) == 1
    assert any(isinstance(event, TombstoneEvent) for event in events)


@pytest.mark.asyncio
async def test_repeated_truncated_tool_calls_never_execute() -> None:
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = ScriptedProvider(
        [
            [
                MessageStartEvent(message_id=str(index)),
                ToolUseStartEvent(tool_use_id=str(index), name="record"),
                ToolUseEndEvent(tool_use_id=str(index), name="record", input={"value": index}),
                MessageEndEvent(stop_reason="max_tokens", usage=Usage()),
            ]
            for index in range(4)
        ]
    )
    loop = AgentLoop(provider_manager=provider, system_prompt="system", tool_registry=registry)

    events = [event async for event in loop.run_streaming("go")]

    assert len(provider.messages) == 4
    assert tool.calls == []
    assert sum(isinstance(event, TombstoneEvent) for event in events) == 4
    assert sum(isinstance(event, ErrorEvent) for event in events) == 1
