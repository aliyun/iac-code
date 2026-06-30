"""AgentTool — spawns sub-agents with tool filtering and progress tracking."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from iac_code.agent.agent_types import filter_tools, get_agent_definition, get_builtin_agents
from iac_code.i18n import _, ngettext
from iac_code.services.permissions.audit import emit_auto_permission_audit
from iac_code.tools.base import Tool, ToolContext, ToolResult


@dataclass
class AgentProgress:
    """Tracks sub-agent execution progress."""

    tool_use_count: int = 0
    token_count: int = 0
    last_activity: str = ""
    summary: str = ""


def _format_base_exception(error: BaseException) -> str:
    detail = str(error)
    error_type = type(error).__name__
    if not detail:
        return error_type
    return "{error_type}: {detail}".format(error_type=error_type, detail=detail)


async def run_sub_agent(
    *,
    prompt: str,
    agent_type: str = "general-purpose",
    cwd: str | None = None,
    parent_provider_manager: Any = None,
    parent_tool_registry: Any = None,
    parent_system_prompt: str = "",
    event_queue: asyncio.Queue | None = None,
    permission_context: Any = None,
) -> tuple[str, AgentProgress]:
    """Run a sub-agent and return (final_text, progress)."""
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.agent.system_prompt import build_system_prompt
    from iac_code.types.stream_events import (
        PermissionRequestEvent,
        TextDeltaEvent,
        ToolResultEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )

    defn = get_agent_definition(agent_type)
    if defn is None:
        raise ValueError(_("Unknown agent type: {agent_type}").format(agent_type=agent_type))

    sub_registry = filter_tools(parent_tool_registry, defn) if parent_tool_registry else parent_tool_registry
    system_prompt = parent_system_prompt or build_system_prompt(cwd=cwd)

    sub_loop = AgentLoop(
        provider_manager=parent_provider_manager,
        system_prompt=system_prompt,
        tool_registry=sub_registry or parent_tool_registry,
        max_turns=defn.max_turns,
        permission_context=permission_context,
    )

    progress = AgentProgress(summary=_("Running {agent_type} agent").format(agent_type=agent_type))
    text_chunks: list[str] = []
    # Track tool inputs: tool_use_id -> (name, input)
    pending_tool_inputs: dict[str, tuple[str, dict]] = {}

    async for event in sub_loop.run_streaming(prompt):
        if isinstance(event, PermissionRequestEvent):
            if event.response_future is not None and not event.response_future.done():
                emit_auto_permission_audit(
                    event,
                    decision="deny",
                    scope="auto_deny",
                    source="agent_tool_auto_deny",
                )
                event.response_future.set_result(False)
            continue
        if isinstance(event, TextDeltaEvent):
            text_chunks.append(event.text)
        elif isinstance(event, ToolUseStartEvent):
            pending_tool_inputs[event.tool_use_id] = (event.name, {})
        elif isinstance(event, ToolUseEndEvent):
            if event.tool_use_id in pending_tool_inputs:
                name = pending_tool_inputs[event.tool_use_id][0]
                pending_tool_inputs[event.tool_use_id] = (name, event.input)
            if event_queue:
                tool_input = event.input
                tool_name = pending_tool_inputs.get(event.tool_use_id, ("", {}))[0]
                await event_queue.put(
                    {
                        "child_tool_name": tool_name,
                        "child_tool_input": tool_input,
                        "is_done": False,
                    }
                )
        elif isinstance(event, ToolResultEvent):
            progress.tool_use_count += 1
            progress.last_activity = event.tool_name
            tool_input = pending_tool_inputs.pop(event.tool_use_id, ("", {}))[1]
            if event_queue:
                await event_queue.put(
                    {
                        "child_tool_name": event.tool_name,
                        "child_tool_input": tool_input,
                        "is_done": True,
                        "is_error": event.is_error,
                    }
                )

    progress.token_count = sub_loop.context_manager.get_total_tokens()
    final_text = "".join(text_chunks)

    words = final_text.split()
    if len(words) > 500:
        final_text = " ".join(words[:500]) + "\n\n[... truncated to 500 words]"

    return final_text, progress


class AgentTool(Tool):
    """Tool that spawns sub-agents for complex tasks."""

    def __init__(
        self,
        task_manager: Any = None,
        notification_queue: Any = None,
        provider_manager: Any = None,
        tool_registry: Any = None,
        system_prompt: str = "",
        permission_context: Any = None,
    ):
        self._task_manager = task_manager
        self._notification_queue = notification_queue
        self._provider_manager = provider_manager
        self._tool_registry = tool_registry
        self._system_prompt = system_prompt
        self._permission_context = permission_context

    def set_system_prompt(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt

    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        agents = get_builtin_agents()
        agent_list = "\n".join(
            "  - {agent_type}: {when_to_use}".format(agent_type=agent.agent_type, when_to_use=agent.when_to_use)
            for agent in agents
        )
        return _("Launch a sub-agent to handle complex tasks.\n\nAvailable agent types:\n{agent_list}").format(
            agent_list=agent_list
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        agent_types = [a.agent_type for a in get_builtin_agents()]
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": _("The task for the sub-agent to perform."),
                },
                "description": {
                    "type": "string",
                    "description": _("Short (3-5 word) description of the task."),
                },
                "subagent_type": {
                    "type": "string",
                    "enum": agent_types,
                    "description": _("The type of specialized agent to use."),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": _("Run agent in background, parent continues."),
                },
            },
            "required": ["prompt", "description"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        prompt = tool_input["prompt"]
        agent_type = tool_input.get("subagent_type", tool_input.get("agent_type", "general-purpose"))
        run_in_background = tool_input.get("run_in_background", False)
        event_queue = context.event_queue

        defn = get_agent_definition(agent_type)
        if defn is None:
            return ToolResult.error(_("Unknown agent type: '{agent_type}'").format(agent_type=agent_type))

        if run_in_background and self._task_manager:
            task_id = self._task_manager.register(
                description=tool_input.get("description", _("Sub-agent task")),
                agent_type=agent_type,
            )
            background_task = asyncio.create_task(self._run_background(task_id, prompt, agent_type, context))
            self._task_manager.attach_task(task_id, background_task)
            background_task.add_done_callback(self._consume_background_task_exception)
            if event_queue is not None:
                await event_queue.put(None)
            return ToolResult.success(
                _("Background agent launched (task_id: {task_id}, type: {agent_type})").format(
                    task_id=task_id,
                    agent_type=agent_type,
                )
            )

        try:
            result_text, progress = await run_sub_agent(
                prompt=prompt,
                agent_type=agent_type,
                cwd=context.cwd,
                parent_provider_manager=self._provider_manager,
                parent_tool_registry=self._tool_registry,
                parent_system_prompt=self._system_prompt,
                event_queue=event_queue,
                permission_context=self._permission_context,
            )
            if event_queue is not None:
                await event_queue.put(None)
            return ToolResult.success(
                f"{result_text}\n\n[Agent stats: {progress.tool_use_count} tool calls, {progress.token_count} tokens]"
            )
        except Exception as e:
            if event_queue is not None:
                await event_queue.put(None)
            return ToolResult.error(_("Sub-agent failed: {error}").format(error=e))

    @staticmethod
    def _consume_background_task_exception(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        with suppress(asyncio.CancelledError):
            task.exception()

    async def _run_background(
        self,
        task_id: str,
        prompt: str,
        agent_type: str,
        context: ToolContext,
    ) -> None:
        try:
            result_text, progress = await run_sub_agent(
                prompt=prompt,
                agent_type=agent_type,
                cwd=context.cwd,
                parent_provider_manager=self._provider_manager,
                parent_tool_registry=self._tool_registry,
                parent_system_prompt=self._system_prompt,
                permission_context=self._permission_context,
            )
            self._task_manager.complete(task_id, result=result_text)
            self._task_manager.update_progress(
                task_id,
                tool_use_count=progress.tool_use_count,
                token_count=progress.token_count,
            )
            if self._notification_queue:
                self._notification_queue.enqueue(
                    task_id=task_id,
                    message=ngettext(
                        "Agent completed: {tool_count} tool call",
                        "Agent completed: {tool_count} tool calls",
                        progress.tool_use_count,
                    ).format(tool_count=progress.tool_use_count),
                )
        except asyncio.CancelledError:
            self._task_manager.stop(task_id)
            if self._notification_queue:
                self._notification_queue.enqueue(
                    task_id=task_id,
                    message=_("Agent stopped"),
                )
            raise
        except Exception as e:
            error = str(e) or type(e).__name__
            self._task_manager.fail(task_id, error=error)
            if self._notification_queue:
                self._notification_queue.enqueue(
                    task_id=task_id,
                    message=_("Agent failed: {error}").format(error=error),
                )
        except BaseException as e:
            error = _format_base_exception(e)
            self._task_manager.fail(task_id, error=error)
            if self._notification_queue:
                self._notification_queue.enqueue(
                    task_id=task_id,
                    message=_("Agent failed: {error}").format(error=error),
                )
            raise

    def is_read_only(self, input: dict | None = None) -> bool:
        return False

    def needs_event_queue(self) -> bool:
        return True

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return True

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        return input.get("description", "")

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if is_error:
            return _("Agent error: {error}").format(error=output[:200])
        if verbose:
            return output
        # Extract stats from the end of the output
        import re

        match = re.search(r"\[Agent stats: (\d+) tool calls, (\d+) tokens\]", output)
        if match:
            tool_count = match.group(1)
            token_count = int(match.group(2))
            token_display = f"{token_count / 1000:.1f}k" if token_count >= 1000 else str(token_count)
            return _("Done ({tool_count} tool uses · {token_display} tokens)").format(
                tool_count=tool_count,
                token_display=token_display,
            )
        return None  # Let renderer handle as default

    def user_facing_name(self, input: dict | None = None) -> str:
        if input:
            agent_type = input.get("subagent_type", input.get("agent_type", "general-purpose"))
            return {
                "explore": _("Explore"),
                "plan": _("Plan"),
                "general-purpose": _("Agent"),
            }.get(agent_type, _("Agent"))
        return _("Agent")

    def get_activity_description(self, input: dict | None = None) -> str | None:
        if input is None:
            return None
        return _("Running agent: {description}").format(description=input.get("description", _("sub-agent")))
