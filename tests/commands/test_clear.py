"""Tests for the clear command."""

from unittest.mock import MagicMock

import pytest

from iac_code.commands.clear import clear_command


@pytest.mark.asyncio
async def test_clear_resets_store_messages():
    store = MagicMock()
    context = MagicMock(store=store)
    # No console → skip screen clear path
    context.console = None
    await clear_command(context=context)
    store.set_state.assert_called_with(messages=[])


@pytest.mark.asyncio
async def test_clear_no_context_no_store():
    # Should not raise
    result = await clear_command()
    assert result == ""


@pytest.mark.asyncio
async def test_clear_uses_kwargs_store():
    store = MagicMock()
    result = await clear_command(store=store)
    assert result == ""
    store.set_state.assert_called_with(messages=[])


@pytest.mark.asyncio
async def test_clear_resets_agent_loop():
    agent_loop = MagicMock()
    repl = MagicMock(_agent_loop=agent_loop)
    context = MagicMock(repl=repl)
    context.console = None

    await clear_command(context=context)

    agent_loop.reset.assert_called_once()


@pytest.mark.asyncio
async def test_clear_with_console_writes_ansi_and_banner(monkeypatch):
    store = MagicMock()
    state = MagicMock(model="claude-sonnet-4-6", cwd="/tmp")
    store.get_state.return_value = state

    console = MagicMock()
    console.file = MagicMock()
    context = MagicMock(store=store, console=console)

    calls = []

    def fake_banner(model, cwd, *, session_id=None, session_name=None):
        calls.append((model, cwd, session_id, session_name))
        return "BANNER"

    monkeypatch.setattr("iac_code.ui.banner.render_welcome_banner", fake_banner)
    result = await clear_command(context=context)
    assert result == ""
    # ANSI escape written
    console.file.write.assert_called()
    console.print.assert_called_with("BANNER")
    assert calls == [("claude-sonnet-4-6", "/tmp", None, None)]


@pytest.mark.asyncio
async def test_clear_banner_preserves_repl_session_identity(monkeypatch):
    store = MagicMock()
    state = MagicMock(model="claude-sonnet-4-6", cwd="/tmp")
    store.get_state.return_value = state

    console = MagicMock()
    console.file = MagicMock()
    repl = MagicMock(_session_id="session-123", _session_name="deploy-prod")
    context = MagicMock(store=store, console=console, repl=repl)

    calls = []

    def fake_banner(model, cwd, *, session_id=None, session_name=None):
        calls.append((model, cwd, session_id, session_name))
        return "BANNER"

    monkeypatch.setattr("iac_code.ui.banner.render_welcome_banner", fake_banner)

    result = await clear_command(context=context)

    assert result == ""
    console.print.assert_called_with("BANNER")
    assert calls == [("claude-sonnet-4-6", "/tmp", "session-123", "deploy-prod")]
