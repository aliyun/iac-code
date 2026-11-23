"""Exercise actual tool execution and model-visible history with large outputs."""

import json
import re
from dataclasses import asdict

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.bash import BashTool
from iac_code.tools.grep import GrepTool
from iac_code.tools.read_file import ReadFileTool
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


class LargeTool(Tool):
    name = "large_output"
    description = "Return a large document."
    input_schema = {"type": "object"}
    original = "intro\n" + "padding\n" * 4_000 + "needle required\n"

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        return ToolResult.success(self.original)

    def is_read_only(self, input=None):
        return True


class Provider:
    def __init__(self, read_with_bash=False):
        self.messages = []
        self.read_with_bash = read_with_bash

    def get_model_name(self):
        return "fake"

    async def stream(self, messages, system, tools=None):
        self.messages.append(list(messages))
        step = len(self.messages)
        reference = "tool-result://large-1.txt"
        if step > 1:
            for message in messages:
                if not isinstance(message.content, list):
                    continue
                for block in message.content:
                    if getattr(block, "tool_use_id", None) == "large-1" and isinstance(block.content, str):
                        match = re.search(r"tool-result://[^\s]+", block.content)
                        if match:
                            reference = match[0]
        yield MessageStartEvent(message_id=str(step))
        calls = {
            1: [("large-1", "large_output", {}), ("large-2", "large_output", {})],
            2: [
                (
                    "search",
                    "grep",
                    {
                        "path": reference,
                        "pattern": "needle",
                        "output_mode": "positions",
                    },
                )
            ],
            3: [
                (
                    "read",
                    "read_file",
                    {
                        "path": reference,
                        "start_line": 4002,
                        "end_line": 4002,
                    },
                )
            ],
        }.get(step, [])
        if step == 3 and self.read_with_bash:
            calls = [("read", "bash", {"command": "tail -n 1 " + reference})]
        for call_id, name, arguments in calls:
            yield ToolUseStartEvent(tool_use_id=call_id, name=name)
            yield ToolUseEndEvent(tool_use_id=call_id, name=name, input=arguments)
        if not calls:
            yield TextDeltaEvent(text="The parameter is required.")
        yield MessageEndEvent(stop_reason="tool_use" if calls else "end_turn", usage=Usage())


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline_mode", [False, True])
@pytest.mark.parametrize("read_with_bash", [False, True])
async def test_loop_saves_searches_reads_and_answers_without_large_prompt(tmp_path, pipeline_mode, read_with_bash):
    registry = ToolRegistry()
    for tool in (LargeTool(), GrepTool(), ReadFileTool(), BashTool()):
        registry.register(tool)
    provider = Provider(read_with_bash=read_with_bash)
    root = tmp_path / "tool-results"
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="system",
        tool_registry=registry,
        cwd=str(tmp_path),
        result_storage_dir=root,
        pipeline_mode=pipeline_mode,
    )
    events = [event async for event in loop.run_streaming("Find the parameter requirement.")]
    assert len(provider.messages) == 4
    assert (root / "large-1.txt").read_text(encoding="utf-8") == LargeTool.original
    assert (root / "large-2.txt").read_text(encoding="utf-8") == LargeTool.original
    contexts = [json.dumps([asdict(message) for message in messages]) for messages in provider.messages]
    assert all("padding" not in prompt for prompt in contexts)
    assert "tool-result://local/" in contexts[1] and "/large-1.txt" in contexts[1]
    assert "character" in contexts[2]
    assert "needle required" in contexts[3]
    assert any(isinstance(event, TextDeltaEvent) and event.text == "The parameter is required." for event in events)
