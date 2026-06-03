from __future__ import annotations

import pytest

from iac_code.commands.memory import execute_memory_command, memory_command
from iac_code.memory.memory_manager import MemoryManager


@pytest.fixture
def manager(tmp_path):
    mgr = MemoryManager(memory_dir=str(tmp_path))
    mgr.save("user-role", "Senior cloud engineer", memory_type="user", description="Role")
    mgr.save("feedback-testing", "Prefer integration tests", memory_type="feedback", description="Testing")
    return mgr


class _Context:
    def __init__(self, manager):
        self.repl = type("Repl", (), {"_memory_manager": manager})()


def test_execute_memory_command_lists_memories(manager):
    output = execute_memory_command(manager, [])
    assert "Saved memories:" in output
    assert "feedback-testing - Testing" in output
    assert "user-role - Role" in output


def test_execute_memory_command_lists_empty(tmp_path):
    output = execute_memory_command(MemoryManager(memory_dir=str(tmp_path)), [])
    assert output == "No memories saved yet."


def test_execute_memory_command_views_memory(manager):
    output = execute_memory_command(manager, ["user-role"])
    assert output == "[user] Role\n\nSenior cloud engineer"


def test_execute_memory_command_missing_memory(manager):
    output = execute_memory_command(manager, ["missing"])
    assert output == "Memory 'missing' not found."


def test_execute_memory_command_searches_memories(manager):
    output = execute_memory_command(manager, ["search", "integration"])
    assert output == "Matching memories:\n  - feedback-testing - Testing"


def test_execute_memory_command_search_no_matches(manager):
    output = execute_memory_command(manager, ["search", "nope"])
    assert output == "No matching memories."


def test_execute_memory_command_search_without_query_shows_help(manager):
    output = execute_memory_command(manager, ["search"])
    assert "Usage: /memory" in output


def test_execute_memory_command_deletes_memory(manager):
    output = execute_memory_command(manager, ["delete", "user-role"])
    assert output == "Memory 'user-role' deleted."
    assert manager.load("user-role") is None


def test_execute_memory_command_delete_missing(manager):
    output = execute_memory_command(manager, ["delete", "missing"])
    assert output == "Memory 'missing' not found."


def test_execute_memory_command_invalid_name(manager):
    output = execute_memory_command(manager, ["../escape"])
    assert "Invalid memory name" in output


def test_execute_memory_command_help_and_unknown_multi_token(manager):
    assert "Usage: /memory" in execute_memory_command(manager, ["help"])
    assert "Usage: /memory" in execute_memory_command(manager, ["remove", "user-role"])


@pytest.mark.asyncio
async def test_memory_command_uses_repl_memory_manager(manager):
    output = await memory_command(context=_Context(manager), args=["user-role"])
    assert output == "[user] Role\n\nSenior cloud engineer"


@pytest.mark.asyncio
async def test_memory_command_missing_context_manager():
    output = await memory_command(context=object(), args=[])
    assert output == "Memory manager is unavailable."
