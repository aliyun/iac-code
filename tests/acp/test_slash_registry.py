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
