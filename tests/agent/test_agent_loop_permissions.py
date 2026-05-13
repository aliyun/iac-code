import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.providers.base import ToolDefinition
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


class WriteTool(Tool):
    def __init__(self) -> None:
        self.executed = False

    @property
    def name(self) -> str:
        return "write_test"

    @property
    def description(self) -> str:
        return "Write test content."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        self.executed = True
        return ToolResult.success(f"wrote {tool_input['value']}")

    async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
        return PermissionResult(behavior="ask", message="Allow write?")


class FakeProviderManager:
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
        yield TextDeltaEvent(text="I will write.")
        yield ToolUseStartEvent(tool_use_id="tool1", name="write_test")
        yield ToolUseEndEvent(tool_use_id="tool1", input={"value": "ok"})
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


@pytest.mark.asyncio
async def test_agent_loop_emits_permission_request_before_write_tool() -> None:
    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        provider_manager=FakeProviderManager(),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )

    events = []
    async for event in loop.run_streaming("write"):
        events.append(event)
        if isinstance(event, PermissionRequestEvent):
            assert event.response_future is not None
            event.response_future.set_result(False)

    assert any(isinstance(event, PermissionRequestEvent) for event in events)
    assert any(
        isinstance(event, ToolResultEvent) and event.is_error and event.result == "Permission denied."
        for event in events
    )
    assert tool.executed is False
