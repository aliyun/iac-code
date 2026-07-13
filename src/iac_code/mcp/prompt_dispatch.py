"""Shared dispatch helpers for MCP prompt slash commands."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from iac_code.agent.message import ContentBlock
from iac_code.commands.registry import PromptCommand
from iac_code.skills.processor import ProcessedSkillResult, process_prompt_command


def is_mcp_prompt_file_path(file_path: str) -> bool:
    """Return True when a skill file path represents an MCP server prompt."""
    if not file_path.startswith("mcp://"):
        return False
    parts = file_path.removeprefix("mcp://").split("/", 2)
    return len(parts) == 3 and bool(parts[0]) and parts[1] == "prompt" and bool(parts[2])


def is_mcp_prompt_command(command: Any) -> bool:
    """Return True when command is a PromptCommand backed by an MCP prompt."""
    if not isinstance(command, PromptCommand):
        return False
    skill = command.skill
    file_path = str(getattr(skill, "file_path", "") if skill is not None else "")
    return is_mcp_prompt_file_path(file_path)


def lookup_mcp_prompt_command(
    commands: Any,
    prompt: str | list[ContentBlock],
) -> tuple[PromptCommand, str] | None:
    """Find the MCP PromptCommand addressed by a slash prompt, if any."""
    if not isinstance(prompt, str):
        return None
    stripped = prompt.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    if not parts or not parts[0]:
        return None
    command = _lookup_command(commands, parts[0])
    if not is_mcp_prompt_command(command):
        return None
    args = parts[1] if len(parts) > 1 else ""
    return command, args


async def mcp_prompt_command_stream(
    *,
    agent_loop: Any,
    commands: Any,
    prompt: str | list[ContentBlock],
    session_id: str = "",
) -> AsyncIterator[Any] | None:
    """Expand an MCP prompt slash command and return the stream to consume."""
    match = lookup_mcp_prompt_command(commands, prompt)
    if match is None:
        return None
    command, args = match
    result = await process_prompt_command(command, args, session_id=session_id)
    return stream_processed_mcp_prompt(agent_loop, result)


def stream_processed_mcp_prompt(agent_loop: Any, result: ProcessedSkillResult) -> AsyncIterator[Any]:
    """Apply a processed MCP prompt result to an AgentLoop and return its stream."""
    if result.is_fork:
        return agent_loop.run_streaming(result.prompt_content)

    injected = _inject_processed_messages(agent_loop, result.new_messages)
    if result.context_modifier:
        apply_context_modifier = getattr(agent_loop, "_apply_context_modifier", None)
        if callable(apply_context_modifier):
            apply_context_modifier(result.context_modifier)

    continue_streaming = getattr(agent_loop, "continue_streaming", None)
    if injected and callable(continue_streaming):
        return continue_streaming()
    return agent_loop.run_streaming(result.prompt_content)


def _lookup_command(commands: Any, name: str) -> Any:
    get = getattr(commands, "get", None)
    if callable(get):
        return get(name) or get(name.lower())

    if isinstance(commands, Iterable):
        lower_name = name.lower()
        for command in commands:
            command_names = [getattr(command, "name", "")]
            command_names.extend(getattr(command, "aliases", []) or [])
            if name in command_names or lower_name in [str(value).lower() for value in command_names]:
                return command
    return None


def _inject_processed_messages(agent_loop: Any, messages: list[dict[str, Any]]) -> bool:
    inject_user_message = getattr(agent_loop, "inject_user_message", None)
    if callable(inject_user_message):
        injected = False
        for message in messages:
            if message.get("role", "user") != "user":
                continue
            inject_user_message(message.get("content", ""))
            injected = True
        if injected:
            return True

    context_manager = getattr(agent_loop, "context_manager", None)
    add_raw_message = getattr(context_manager, "add_raw_message", None)
    if not callable(add_raw_message):
        return False
    injected = False
    for message in messages:
        add_raw_message(message)
        injected = True
    return injected
