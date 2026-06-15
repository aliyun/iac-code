"""Helpers for rebuilding pipeline step state from AgentLoop transcripts."""

from __future__ import annotations

import json
from typing import Any

from iac_code.agent.message import Message, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine.types import StepResult, StepStatus


def _tool_uses_by_id(messages: list[Message]) -> dict[str, ToolUseBlock]:
    tool_uses: dict[str, ToolUseBlock] = {}
    for message in messages:
        if message.role != "assistant" or isinstance(message.content, str):
            continue
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                tool_uses[block.id] = block
    return tool_uses


def _successful_tool_result_ids(messages: list[Message]) -> list[str]:
    successful: list[str] = []
    for message in messages:
        if message.role != "user" or isinstance(message.content, str):
            continue
        for block in message.content:
            if isinstance(block, ToolResultBlock) and not block.is_error:
                successful.append(block.tool_use_id)
    return successful


def last_successful_tool_input(messages: list[Message], tool_name: str) -> dict[str, Any] | None:
    tool_uses = _tool_uses_by_id(messages)
    for tool_use_id in reversed(_successful_tool_result_ids(messages)):
        tool_use = tool_uses.get(tool_use_id)
        if tool_use is not None and tool_use.name == tool_name:
            return tool_use.input
    return None


def reconstruct_step_result(messages: list[Message], step_id: str) -> StepResult | None:
    tool_input = last_successful_tool_input(messages, "complete_step")
    if tool_input is None:
        return None
    conclusion = tool_input.get("conclusion", {})
    rollback = tool_input.get("rollback_request")
    rollback_tuple = None
    if isinstance(rollback, dict) and rollback.get("target_step") and rollback.get("reason"):
        rollback_tuple = (str(rollback["target_step"]), str(rollback["reason"]))
    return StepResult(
        step_id=step_id,
        status=StepStatus.COMPLETED,
        conclusion=conclusion if isinstance(conclusion, dict) else {},
        rollback_request=rollback_tuple,
    )


def reconstruct_completion_guard_state(messages: list[Message]) -> dict[str, Any]:
    tool_uses = _tool_uses_by_id(messages)
    successful_tools: set[str] = set()
    tool_results: dict[str, Any] = {}
    for message in messages:
        if message.role != "user" or isinstance(message.content, str):
            continue
        for block in message.content:
            if not isinstance(block, ToolResultBlock) or block.is_error:
                continue
            tool_use = tool_uses.get(block.tool_use_id)
            if tool_use is None:
                continue
            if tool_use.name != "ask_user_question":
                continue
            successful_tools.add("ask_user_question")
            try:
                tool_results["ask_user_question"] = json.loads(block.content)
            except json.JSONDecodeError:
                tool_results["ask_user_question"] = {
                    "selected_id": "",
                    "selected_label": "",
                    "free_text": block.content,
                }
    return {"successful_tools": successful_tools, "tool_results": tool_results}
