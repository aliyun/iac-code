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
async def test_clear_with_console_writes_ansi_and_banner(monkeypatch):
    store = MagicMock()
    state = MagicMock(model="claude-sonnet-4-6", cwd="/tmp")
    store.get_state.return_value = state

    console = MagicMock()
    console.file = MagicMock()
    context = MagicMock(store=store, console=console)

    calls = []

    def fake_banner(model, cwd):
        calls.append((model, cwd))
        return "BANNER"

    monkeypatch.setattr("iac_code.ui.banner.render_welcome_banner", fake_banner)
    result = await clear_command(context=context)
    assert result == ""
    # ANSI escape written
    console.file.write.assert_called()
    console.print.assert_called_with("BANNER")
    assert calls == [("claude-sonnet-4-6", "/tmp")]
