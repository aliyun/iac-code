"""Model-side task tools for managing background agents."""

from __future__ import annotations

from typing import Any

from iac_code.tasks.task_state import TaskManager
from iac_code.tools.base import Tool, ToolContext, ToolResult


class TaskListTool(Tool):
    def __init__(self, task_manager: TaskManager):
        self._manager = task_manager

    @property
    def name(self) -> str:
        return "task_list"

    @property
    def description(self) -> str:
        return "List all background tasks with their status."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = self._manager.list_all()
        if not tasks:
            return ToolResult.success("No background tasks.")
        lines = []
        for t in tasks:
            lines.append(f"- [{t.id}] {t.status.value} | [{t.agent_type}] {t.description}")
            if t.result:
                lines.append(f"  Result: {t.result[:200]}")
            if t.error:
                lines.append(f"  Error: {t.error[:200]}")
        return ToolResult.success("\n".join(lines))

    def is_read_only(self, input: dict | None = None) -> bool:
        return True


class TaskGetTool(Tool):
    def __init__(self, task_manager: TaskManager):
        self._manager = task_manager

    @property
    def name(self) -> str:
        return "task_get"

    @property
    def description(self) -> str:
        return "Get details of a specific background task by ID."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        task = self._manager.get(tool_input["task_id"])
        if not task:
            return ToolResult.error(f"Task '{tool_input['task_id']}' not found.")
        lines = [
            f"ID: {task.id}",
            f"Description: {task.description}",
            f"Status: {task.status.value}",
            f"Agent type: {task.agent_type}",
            f"Tool uses: {task.tool_use_count}",
            f"Tokens: {task.token_count}",
        ]
        if task.result:
            lines.append(f"Result: {task.result}")
        if task.error:
            lines.append(f"Error: {task.error}")
        return ToolResult.success("\n".join(lines))

    def is_read_only(self, input: dict | None = None) -> bool:
        return True


class TaskStopTool(Tool):
    def __init__(self, task_manager: TaskManager):
        self._manager = task_manager

    @property
    def name(self) -> str:
        return "task_stop"

    @property
    def description(self) -> str:
        return "Stop a running background task."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        task = self._manager.get(tool_input["task_id"])
        if not task:
            return ToolResult.error(f"Task '{tool_input['task_id']}' not found.")
        self._manager.stop(tool_input["task_id"])
        return ToolResult.success(f"Task '{tool_input['task_id']}' stopped.")

    def is_read_only(self, input: dict | None = None) -> bool:
        return False
