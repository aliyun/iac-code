"""A tool call whose arguments failed to parse must not reach the tool.

Executing on ``{}`` makes the tool answer with its own schema error ("missing
required field ..."), which tells the model the opposite of the truth — it did
send that field — so the model retries the identical call and every round trip
costs a full generation. The parse failure has to come back as the tool result.
"""

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.providers.base import ToolDefinition
from iac_code.services.permission_wait import PermissionWaitPolicy, build_permission_checkpoint
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    PermissionWaitOutcome,
    PermissionWaitSuspended,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

INPUT_ERROR = "Tool arguments were not valid JSON, so this tool call was not executed (no arguments reached the tool)."


class RecordingTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return "complete_step"

    @property
    def description(self) -> str:
        return "Complete the current step."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"conclusion": {"type": "object"}},
            "required": ["conclusion"],
        }

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        self.calls.append(tool_input)
        return ToolResult.error("completion_input_schema_validation_failed: required ['conclusion']")

    async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
        return PermissionResult(behavior="ask", message="Allow?")


class _Provider:
    def __init__(self, *, input_error: str | None) -> None:
        self._input_error = input_error

    def get_model_name(self) -> str:
        return "fake"

    async def stream(
        self,
        messages,
        system,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ):
        yield MessageStartEvent(message_id="m1")
        yield ToolUseStartEvent(tool_use_id="tool1", name="complete_step")
        if self._input_error is None:
            yield ToolUseEndEvent(tool_use_id="tool1", name="complete_step", input={"conclusion": {}})
        else:
            yield ToolUseEndEvent(
                tool_use_id="tool1",
                name="complete_step",
                input={},
                input_error=self._input_error,
            )
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


async def _run(input_error: str | None) -> tuple[RecordingTool, list]:
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        provider_manager=_Provider(input_error=input_error),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )
    events = []
    async for event in loop.run_streaming("go"):
        events.append(event)
        if isinstance(event, PermissionRequestEvent) and event.response_future is not None:
            event.response_future.set_result(True)
    return tool, events


@pytest.mark.asyncio
async def test_unparseable_arguments_skip_execution_and_report_the_real_defect() -> None:
    tool, events = await _run(INPUT_ERROR)

    assert tool.calls == []
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(results) == 1
    assert results[0].is_error
    assert results[0].result == INPUT_ERROR
    # The parse failure is reported before any permission prompt: there are no
    # arguments to show the user, and nothing is going to run either way.
    assert not any(isinstance(event, PermissionRequestEvent) for event in events)


@pytest.mark.asyncio
async def test_parsed_arguments_still_execute_the_tool() -> None:
    tool, events = await _run(None)

    assert tool.calls == [{"conclusion": {}}]
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(results) == 1
    assert "completion_input_schema_validation_failed" in results[0].result


@pytest.mark.asyncio
async def test_unparseable_later_tool_remains_denied_across_permission_resume() -> None:
    class _MixedBatchProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="mixed")
            yield ToolUseStartEvent(tool_use_id="valid", name="complete_step")
            yield ToolUseEndEvent(tool_use_id="valid", name="complete_step", input={"conclusion": {}})
            yield ToolUseStartEvent(tool_use_id="invalid", name="complete_step")
            yield ToolUseEndEvent(
                tool_use_id="invalid",
                name="complete_step",
                input={},
                input_error=INPUT_ERROR,
            )
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    class _ContinueProvider:
        def get_model_name(self) -> str:
            return "fake"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="continued")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    live_loop = AgentLoop(
        provider_manager=_MixedBatchProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )
    permission_event = None
    with pytest.raises(PermissionWaitSuspended):
        async for event in live_loop.run_streaming("go"):
            if isinstance(event, PermissionRequestEvent):
                permission_event = event
                assert event.response_future is not None
                event.response_future.set_result(PermissionWaitOutcome.SUSPEND)

    assert permission_event is not None
    assert permission_event.continuation_frame is not None
    assert permission_event.continuation_frame["decisions"][1] == {
        "toolUseId": "invalid",
        "state": "deny",
        "source": "input_error",
        "deniedResult": INPUT_ERROR,
    }
    checkpoint = build_permission_checkpoint(
        session_id="session-1",
        task_id=None,
        context_id="context-1",
        input_id="input-1",
        tool_use_id=permission_event.tool_use_id,
        tool_name=permission_event.tool_name,
        tool_input=permission_event.tool_input,
        permission_class="normal",
        continuation_frame=permission_event.continuation_frame,
        policy=PermissionWaitPolicy(),
    )
    checkpoint["decision"] = {"status": "claimed", "value": "allow_once", "claimId": "claim-1"}
    resumed_loop = AgentLoop(
        provider_manager=_ContinueProvider(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        resume_messages=list(live_loop.context_manager.get_messages()),
    )
    events = [event async for event in resumed_loop.resume_permission_boundary(checkpoint)]

    assert tool.calls == [{"conclusion": {}}]
    assert not any(isinstance(event, PermissionRequestEvent) for event in events)
    invalid_results = [
        event for event in events if isinstance(event, ToolResultEvent) and event.tool_use_id == "invalid"
    ]
    assert len(invalid_results) == 1
    assert invalid_results[0].is_error
    assert invalid_results[0].result == INPUT_ERROR
