from __future__ import annotations

import pytest

from iac_code.memory.memory_tools import ReadMemoryTool, WriteMemoryTool
from iac_code.tools.base import ToolContext


class FakeMemoryManager:
    def __init__(self):
        self.saved = []
        self.memories = {}
        self.index_content = ""

    def load(self, name):
        return self.memories.get(name)

    def save(self, *, name, content, memory_type, description):
        self.saved.append(
            {
                "name": name,
                "content": content,
                "memory_type": memory_type,
                "description": description,
            }
        )

    def get_index_content(self):
        return self.index_content


@pytest.mark.asyncio
class TestReadMemoryTool:
    async def test_read_named_memory_returns_formatted_content(self):
        manager = FakeMemoryManager()
        manager.memories["role"] = {
            "type": "user",
            "description": "Role",
            "content": "Senior engineer",
        }
        tool = ReadMemoryTool(manager)

        result = await tool.execute(tool_input={"name": "role"}, context=ToolContext())

        assert result.is_error is False
        assert result.content == "[user] Role\n\nSenior engineer"

    async def test_read_missing_memory_returns_error(self):
        tool = ReadMemoryTool(FakeMemoryManager())

        result = await tool.execute(tool_input={"name": "missing"}, context=ToolContext())

        assert result.is_error is True
        assert "not found" in result.content

    async def test_read_without_name_returns_index_content(self):
        manager = FakeMemoryManager()
        manager.index_content = "memory index"
        tool = ReadMemoryTool(manager)

        result = await tool.execute(tool_input={}, context=ToolContext())

        assert result.is_error is False
        assert result.content == "memory index"

    async def test_read_without_name_returns_empty_message_when_index_empty(self):
        tool = ReadMemoryTool(FakeMemoryManager())

        result = await tool.execute(tool_input={}, context=ToolContext())

        assert result.is_error is False
        assert result.content == "No memories saved yet."

    async def test_is_read_only(self):
        assert ReadMemoryTool(FakeMemoryManager()).is_read_only() is True


@pytest.mark.asyncio
class TestWriteMemoryTool:
    async def test_write_memory_saves_and_returns_success(self):
        manager = FakeMemoryManager()
        tool = WriteMemoryTool(manager)

        result = await tool.execute(
            tool_input={
                "name": "role",
                "content": "Senior engineer",
                "memory_type": "user",
                "description": "Role",
            },
            context=ToolContext(),
        )

        assert result.is_error is False
        assert result.content == "Memory 'role' saved."
        assert manager.saved == [
            {
                "name": "role",
                "content": "Senior engineer",
                "memory_type": "user",
                "description": "Role",
            }
        ]

    async def test_write_memory_returns_error_when_save_fails(self):
        class FailingManager(FakeMemoryManager):
            def save(self, *, name, content, memory_type, description):
                raise RuntimeError("disk full")

        tool = WriteMemoryTool(FailingManager())

        result = await tool.execute(
            tool_input={
                "name": "role",
                "content": "Senior engineer",
                "memory_type": "user",
                "description": "Role",
            },
            context=ToolContext(),
        )

        assert result.is_error is True
        assert result.content == "disk full"

    async def test_is_read_only(self):
        assert WriteMemoryTool(FakeMemoryManager()).is_read_only() is False
