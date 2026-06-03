"""Tests for the /rename command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.commands.rename import rename_command


@pytest.mark.asyncio
async def test_rename_inline_success() -> None:
    repl = MagicMock()
    repl.rename_current_session = MagicMock(return_value="renamed")
    context = MagicMock(repl=repl)

    result = await rename_command(context=context, args=["deploy-prod"])

    assert "deploy-prod" in result.message
    assert "renamed" in result.message.lower()
    assert result.is_error is False
    assert result.refresh_banner is True
    repl.rename_current_session.assert_called_once_with("deploy-prod")


@pytest.mark.asyncio
async def test_rename_multiple_args_returns_usage() -> None:
    repl = MagicMock()
    repl.rename_current_session = MagicMock()
    context = MagicMock(repl=repl)

    result = await rename_command(context=context, args=["deploy", "prod"])

    assert result.message == "Usage: /rename <name>"
    assert result.is_error is True
    assert result.refresh_banner is False
    repl.rename_current_session.assert_not_called()


@pytest.mark.asyncio
async def test_rename_invalid_name_returns_validation_message() -> None:
    repl = MagicMock()
    repl.rename_current_session = MagicMock()
    context = MagicMock(repl=repl)

    result = await rename_command(context=context, args=["bad name"])

    assert result.message == "Session name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"
    assert result.is_error is True
    assert result.refresh_banner is False
    repl.rename_current_session.assert_not_called()


@pytest.mark.asyncio
async def test_rename_duplicate_value_error_returns_message() -> None:
    repl = MagicMock()
    repl.rename_current_session = MagicMock(side_effect=ValueError("Session name already exists: deploy-prod"))
    context = MagicMock(repl=repl)

    result = await rename_command(context=context, args=["deploy-prod"])

    assert result.message == "Session name already exists: deploy-prod"
    assert result.is_error is True
    assert result.refresh_banner is False


@pytest.mark.asyncio
async def test_rename_unchanged_message() -> None:
    repl = MagicMock()
    repl.rename_current_session = MagicMock(return_value="unchanged")
    context = MagicMock(repl=repl)

    result = await rename_command(context=context, args=["deploy-prod"])

    assert "already" in result.message.lower()
    assert "deploy-prod" in result.message
    assert result.is_error is False
    assert result.refresh_banner is False


@pytest.mark.asyncio
async def test_rename_interactive_success() -> None:
    repl = MagicMock()
    repl.prompt_for_session_name = AsyncMock(return_value=" deploy-prod ")
    repl.rename_current_session = MagicMock(return_value="renamed")
    context = MagicMock(repl=repl)

    result = await rename_command(context=context, args=[])

    assert "deploy-prod" in result.message
    assert "renamed" in result.message.lower()
    assert result.is_error is False
    assert result.refresh_banner is True
    repl.prompt_for_session_name.assert_awaited_once()
    repl.rename_current_session.assert_called_once_with("deploy-prod")


@pytest.mark.asyncio
async def test_rename_interactive_cancelled() -> None:
    repl = MagicMock()
    repl.prompt_for_session_name = AsyncMock(return_value=None)
    repl.rename_current_session = MagicMock()
    context = MagicMock(repl=repl)

    result = await rename_command(context=context, args=[])

    assert "cancel" in result.message.lower()
    assert result.is_error is False
    assert result.refresh_banner is False
    repl.rename_current_session.assert_not_called()


@pytest.mark.asyncio
async def test_rename_no_interactive_context_returns_message() -> None:
    result = await rename_command(context=None, args=["deploy-prod"])

    assert "interactive" in result.message.lower()
    assert result.is_error is True
    assert result.refresh_banner is False
