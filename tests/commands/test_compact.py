"""Tests for the compact command."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.agent.agent_loop import CompactResult
from iac_code.commands.compact import compact_command


@pytest.mark.asyncio
async def test_compact_requires_context():
    result = await compact_command()
    assert "context" in result.lower()


@pytest.mark.asyncio
async def test_compact_requires_repl():
    context = MagicMock(spec=["repl"])
    context.repl = None
    result = await compact_command(context=context)
    assert "repl" in result.lower()


@pytest.mark.asyncio
async def test_compact_no_agent_loop():
    context = MagicMock()
    context.repl = MagicMock(_agent_loop=None)
    result = await compact_command(context=context)
    assert "agent loop" in result.lower()


@pytest.mark.asyncio
async def test_compact_empty_conversation():
    agent_loop = MagicMock()
    agent_loop.compact = AsyncMock(return_value=CompactResult(status="empty"))
    context = MagicMock()
    context.repl = MagicMock(_agent_loop=agent_loop)
    result = await compact_command(context=context)
    assert "empty" in result.lower()


@pytest.mark.asyncio
async def test_compact_too_short_mentions_preserve_window():
    agent_loop = MagicMock()
    agent_loop.compact = AsyncMock(return_value=CompactResult(status="too_short", preserve_recent_turns=3))
    context = MagicMock()
    context.repl = MagicMock(_agent_loop=agent_loop)
    result = await compact_command(context=context)
    assert "too short" in result.lower()
    assert "3" in result


@pytest.mark.asyncio
async def test_compact_failed():
    agent_loop = MagicMock()
    agent_loop.compact = AsyncMock(return_value=CompactResult(status="failed"))
    context = MagicMock()
    context.repl = MagicMock(_agent_loop=agent_loop)
    result = await compact_command(context=context)
    assert "failed" in result.lower()


@pytest.mark.asyncio
async def test_compact_success():
    agent_loop = MagicMock()
    agent_loop.compact = AsyncMock(
        return_value=CompactResult(status="success", original_tokens=10000, compacted_tokens=3000)
    )
    agent_loop.get_context_usage = MagicMock(return_value={"usage_percent": 42.5})
    context = MagicMock()
    context.repl = MagicMock(_agent_loop=agent_loop)
    result = await compact_command(context=context)
    assert "10000" in result
    assert "3000" in result
    assert "70" in result  # ~70% reduction
