"""Tools for the model to read and write persistent memories."""

from __future__ import annotations

from typing import Any

from iac_code.memory.memory_manager import MEMORY_TYPES, MemoryManager
from iac_code.tools.base import Tool, ToolContext, ToolResult


class ReadMemoryTool(Tool):
    def __init__(self, memory_manager: MemoryManager):
        self._manager = memory_manager

    @property
    def name(self) -> str:
        return "read_memory"

    @property
    def description(self) -> str:
        return "Read persistent memories. Omit name to list all, or provide name to read specific memory."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Memory name to read. Omit to list all.",
                }
            },
        }

    async def execute(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        name = tool_input.get("name")
        if name:
            mem = self._manager.load(name)
            if mem is None:
                return ToolResult.error(f"Memory '{name}' not found.")
            return ToolResult.success(f"[{mem.get('type', '')}] {mem.get('description', '')}\n\n{mem['content']}")
        else:
            index = self._manager.get_index_content()
            return ToolResult.success(index or "No memories saved yet.")

    def is_read_only(self, input: dict | None = None) -> bool:
        return True


class WriteMemoryTool(Tool):
    def __init__(self, memory_manager: MemoryManager):
        self._manager = memory_manager

    @property
    def name(self) -> str:
        return "write_memory"

    @property
    def description(self) -> str:
        types = ", ".join(sorted(MEMORY_TYPES))
        return (
            "Save a persistent memory. Use when the user explicitly asks you to remember or preserve information. "
            "Choose a concise, stable name, an appropriate type, a short description, and the useful content to keep. "
            f"Types: {types}."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string"},
                "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
                "description": {"type": "string"},
            },
            "required": ["name", "content", "memory_type", "description"],
        }

    async def execute(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            self._manager.save(
                name=tool_input["name"],
                content=tool_input["content"],
                memory_type=tool_input["memory_type"],
                description=tool_input["description"],
            )
            return ToolResult.success(f"Memory '{tool_input['name']}' saved.")
        except Exception as e:
            return ToolResult.error(str(e))

    def is_read_only(self, input: dict | None = None) -> bool:
        return False
