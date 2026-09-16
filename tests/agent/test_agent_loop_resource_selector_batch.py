import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.providers.base import ToolDefinition
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


class RecordingTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def input_schema(self) -> dict:
        return {"type": "object"}

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        self.calls.append(tool_input)
        return ToolResult(content="ok")

    async def check_permissions(self, input: dict, context: dict | None = None) -> PermissionResult:
        return PermissionResult(behavior="allow")


class BatchProvider:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def get_model_name(self) -> str:
        return "fake"

    async def stream(
        self,
        messages,
        system,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ):
        yield MessageStartEvent(message_id="batch")
        for index, name in enumerate(self.names):
            tool_use_id = "tool-{}".format(index)
            yield ToolUseStartEvent(tool_use_id=tool_use_id, name=name)
            yield ToolUseEndEvent(tool_use_id=tool_use_id, name=name, input={"index": index})
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


async def run_batch(names: list[str]) -> tuple[dict[str, RecordingTool], list]:
    tools = {name: RecordingTool(name) for name in names}
    registry = ToolRegistry()
    for tool in tools.values():
        registry.register(tool)
    loop = AgentLoop(
        provider_manager=BatchProvider(names),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
    )
    return tools, [event async for event in loop.run_streaming("go")]


@pytest.mark.asyncio
async def test_select_cloud_resource_rejects_the_entire_mixed_batch_before_execution() -> None:
    tools, events = await run_batch(["select_cloud_resource", "read_file"])

    assert tools["select_cloud_resource"].calls == []
    assert tools["read_file"].calls == []
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(results) == 2
    assert all(event.is_error for event in results)
    assert all("must be called alone" in event.result for event in results)


@pytest.mark.asyncio
async def test_resolve_cloud_resource_selector_remains_compatible_with_a_mixed_batch() -> None:
    tools, events = await run_batch(["resolve_cloud_resource_selector", "read_file"])

    assert tools["resolve_cloud_resource_selector"].calls == [{"index": 0}]
    assert tools["read_file"].calls == [{"index": 1}]
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(results) == 2
    assert all(not event.is_error for event in results)
