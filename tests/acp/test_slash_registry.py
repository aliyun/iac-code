"""Tests for ACP slash command registry — targeting 95%+ coverage."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from iac_code.acp.slash_registry import ACPSlashRegistry


@pytest.fixture
def registry() -> ACPSlashRegistry:
    return ACPSlashRegistry()


# ---------------------------------------------------------------------------
# is_slash_command
# ---------------------------------------------------------------------------


class TestIsSlashCommand:
    def test_slash_command_recognized(self, registry: ACPSlashRegistry) -> None:
        assert registry.is_slash_command("/compact") is True
        assert registry.is_slash_command("/debug on") is True

    def test_not_slash_command(self, registry: ACPSlashRegistry) -> None:
        assert registry.is_slash_command("hello") is False
        assert registry.is_slash_command("") is False

    def test_double_slash_not_command(self, registry: ACPSlashRegistry) -> None:
        assert registry.is_slash_command("//comment") is False

    def test_single_slash_not_command(self, registry: ACPSlashRegistry) -> None:
        assert registry.is_slash_command("/") is False

    def test_leading_whitespace(self, registry: ACPSlashRegistry) -> None:
        assert registry.is_slash_command("  /compact") is True


# ---------------------------------------------------------------------------
# execute — unsupported command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_command_returns_rejection(registry: ACPSlashRegistry) -> None:
    result = await registry.execute("/help", agent_loop=None)
    assert "not supported" in result or "不支持" in result
    assert "/clear" in result or "clear" in result
    assert "/memory-folder" not in result


@pytest.mark.asyncio
async def test_empty_command_name_unsupported(registry: ACPSlashRegistry) -> None:
    # Edge case: just "/" with no name after it
    result = await registry.execute("/ ", agent_loop=None)
    assert "not supported" in result or "不支持" in result


# ---------------------------------------------------------------------------
# execute — /compact
# ---------------------------------------------------------------------------


@dataclass
class CompactResult:
    status: str = "success"
    original_tokens: int = 1000
    compacted_tokens: int = 300
    preserve_recent_turns: int = 3


@pytest.mark.asyncio
async def test_compact_success(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()
    agent_loop.compact = MagicMock(return_value=CompactResult(status="success"))
    agent_loop.get_context_usage = MagicMock(return_value={"usage_percent": 42.5})

    # Make compact a coroutine
    async def _compact():
        return CompactResult(status="success")

    agent_loop.compact = _compact
    result = await registry.execute("/compact", agent_loop=agent_loop)
    assert "1000" in result
    assert "300" in result
    assert "70%" in result


@pytest.mark.asyncio
async def test_compact_empty(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()

    async def _compact():
        return CompactResult(status="empty")

    agent_loop.compact = _compact
    result = await registry.execute("/compact", agent_loop=agent_loop)
    assert "empty" in result.lower() or "空" in result


@pytest.mark.asyncio
async def test_compact_too_short(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()

    async def _compact():
        return CompactResult(status="too_short", preserve_recent_turns=5)

    agent_loop.compact = _compact
    result = await registry.execute("/compact", agent_loop=agent_loop)
    assert "5" in result


@pytest.mark.asyncio
async def test_compact_failed_status(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()

    async def _compact():
        return CompactResult(status="failed")

    agent_loop.compact = _compact
    result = await registry.execute("/compact", agent_loop=agent_loop)
    assert "failed" in result.lower() or "失败" in result


@pytest.mark.asyncio
async def test_compact_exception(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()

    async def _compact():
        raise RuntimeError("network error")

    agent_loop.compact = _compact
    result = await registry.execute("/compact", agent_loop=agent_loop)
    assert "network error" in result


# ---------------------------------------------------------------------------
# execute — /clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_success(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()
    result = await registry.execute("/clear", agent_loop=agent_loop)
    agent_loop.reset.assert_called_once()
    assert "clear" in result.lower() or "清" in result


@pytest.mark.asyncio
async def test_clear_exception(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()
    agent_loop.reset.side_effect = RuntimeError("db error")
    result = await registry.execute("/clear", agent_loop=agent_loop)
    assert "db error" in result


# ---------------------------------------------------------------------------
# execute — /debug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_status_on(registry: ACPSlashRegistry) -> None:
    with (
        patch("iac_code.utils.log.is_debug_enabled", return_value=True),
        patch("iac_code.utils.log.current_log_file", return_value="/tmp/debug.log"),
    ):
        result = await registry.execute("/debug status", agent_loop=None)
    assert "/tmp/debug.log" in result


@pytest.mark.asyncio
async def test_debug_status_off(registry: ACPSlashRegistry) -> None:
    with patch("iac_code.utils.log.is_debug_enabled", return_value=False):
        result = await registry.execute("/debug", agent_loop=None)
    assert "off" in result.lower() or "关" in result


@pytest.mark.asyncio
async def test_debug_on(registry: ACPSlashRegistry) -> None:
    with patch("iac_code.utils.log.enable_debug_at_runtime", return_value="/tmp/acp.log"):
        result = await registry.execute("/debug on", agent_loop=None)
    assert "/tmp/acp.log" in result


@pytest.mark.asyncio
async def test_debug_off(registry: ACPSlashRegistry) -> None:
    with patch("iac_code.utils.log.disable_debug_at_runtime"):
        result = await registry.execute("/debug off", agent_loop=None)
    assert "disabled" in result.lower() or "关" in result


@pytest.mark.asyncio
async def test_debug_invalid_arg(registry: ACPSlashRegistry) -> None:
    result = await registry.execute("/debug foo", agent_loop=None)
    assert "usage" in result.lower() or "/debug" in result.lower()


# ---------------------------------------------------------------------------
# execute — /memory-folder
# ---------------------------------------------------------------------------


class _MemoryManager:
    def __init__(self):
        self.memories = {
            "user-role": {"name": "user-role", "type": "user", "description": "Role", "content": "Senior engineer"},
            "feedback-testing": {
                "name": "feedback-testing",
                "type": "feedback",
                "description": "Testing",
                "content": "Prefer integration tests",
            },
        }
        self.deleted: list[str] = []

    def list_memories(self):
        return list(self.memories.values())

    def load(self, name):
        if name == "../escape":
            raise ValueError("Invalid memory name: '../escape'")
        return self.memories.get(name)

    def delete(self, name):
        self.deleted.append(name)
        self.memories.pop(name, None)

    def search(self, query):
        query = query.casefold()
        return [
            memory
            for memory in self.memories.values()
            if query
            in "\n".join(str(memory.get(field, "")) for field in ("name", "description", "type", "content")).casefold()
        ]


@pytest.mark.asyncio
async def test_memory_without_manager_returns_unavailable(registry: ACPSlashRegistry) -> None:
    result = await registry.execute("/memory-folder", agent_loop=None)
    assert result == "Memory manager is unavailable."


@pytest.mark.asyncio
async def test_memory_list_view_search_delete(registry: ACPSlashRegistry) -> None:
    memory_manager = _MemoryManager()

    listed = await registry.execute("/memory-folder", agent_loop=None, memory_manager=memory_manager)
    viewed = await registry.execute("/memory-folder user-role", agent_loop=None, memory_manager=memory_manager)
    searched = await registry.execute(
        "/memory-folder search integration", agent_loop=None, memory_manager=memory_manager
    )
    deleted = await registry.execute("/memory-folder delete user-role", agent_loop=None, memory_manager=memory_manager)

    assert "Saved memories:" in listed
    assert viewed == "[user] Role\n\nSenior engineer"
    assert searched == "Matching memories:\n  - feedback-testing - Testing"
    assert deleted == "Memory 'user-role' deleted."
    assert memory_manager.deleted == ["user-role"]


@pytest.mark.asyncio
async def test_memory_help_missing_invalid_name_and_unknown_usage(registry: ACPSlashRegistry) -> None:
    memory_manager = _MemoryManager()

    helped = await registry.execute("/memory-folder help", agent_loop=None, memory_manager=memory_manager)
    missing = await registry.execute("/memory-folder missing", agent_loop=None, memory_manager=memory_manager)
    invalid = await registry.execute("/memory-folder ../escape", agent_loop=None, memory_manager=memory_manager)
    unknown = await registry.execute("/memory-folder remove user-role", agent_loop=None, memory_manager=memory_manager)

    assert helped == "Usage: /memory-folder [<name>|search <query>|delete <name>|help]"
    assert missing == "Memory 'missing' not found."
    assert invalid == "Invalid memory name: '../escape'"
    assert unknown == "Usage: /memory-folder [<name>|search <query>|delete <name>|help]"


# ---------------------------------------------------------------------------
# execute — /rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_success_calls_storage_with_session_context(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()
    agent_loop._cwd = "/project"
    agent_loop._session_id = "session-1"
    agent_loop._current_git_branch = "main"

    storage = MagicMock()
    storage.rename_session.return_value = "renamed"

    with patch("iac_code.acp.slash_registry.SessionStorage", return_value=storage):
        result = await registry.execute("/rename deploy-prod", agent_loop=agent_loop)

    storage.rename_session.assert_called_once_with(
        "/project",
        "session-1",
        "deploy-prod",
        git_branch="main",
    )
    assert result == "Renamed session to deploy-prod"


@pytest.mark.asyncio
async def test_rename_requires_name(registry: ACPSlashRegistry) -> None:
    result = await registry.execute("/rename", agent_loop=MagicMock())

    assert result == "Usage: /rename <name>"


@pytest.mark.asyncio
async def test_rename_rejects_multi_token_name(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()

    result = await registry.execute("/rename deploy prod", agent_loop=agent_loop)

    assert result == "Usage: /rename <name>"


@pytest.mark.asyncio
async def test_rename_value_error_returns_message(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()
    agent_loop._cwd = "/project"
    agent_loop._session_id = "session-1"
    agent_loop._current_git_branch = None

    storage = MagicMock()
    storage.rename_session.side_effect = ValueError("Session name already exists in this project: deploy-prod")

    with patch("iac_code.acp.slash_registry.SessionStorage", return_value=storage):
        result = await registry.execute("/rename deploy-prod", agent_loop=agent_loop)

    assert result == "Session name already exists in this project: deploy-prod"


@pytest.mark.asyncio
async def test_rename_unchanged_message(registry: ACPSlashRegistry) -> None:
    agent_loop = MagicMock()
    agent_loop._cwd = "/project"
    agent_loop._session_id = "session-1"
    agent_loop._current_git_branch = None

    storage = MagicMock()
    storage.rename_session.return_value = "unchanged"

    with patch("iac_code.acp.slash_registry.SessionStorage", return_value=storage):
        result = await registry.execute("/rename deploy-prod", agent_loop=agent_loop)

    assert result == "Session is already named deploy-prod"
