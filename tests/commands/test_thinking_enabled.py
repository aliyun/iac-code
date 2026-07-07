"""Tests for the /thinking_enabled command."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_thinking_enabled_no_active_provider(monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command

    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_active_provider_key", lambda: None)

    result = await thinking_enabled_command(context=None, args=[])

    assert "/auth" in result


@pytest.mark.asyncio
async def test_thinking_enabled_no_model(monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command

    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_active_provider_key", lambda: "openai")
    store = MagicMock()
    store.get_state.return_value = SimpleNamespace(model="")

    result = await thinking_enabled_command(context=None, args=[], store=store)

    assert "/model" in result


@pytest.mark.asyncio
async def test_thinking_enabled_off_persists_and_updates_state(monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command

    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_active_provider_key", lambda: "openai")
    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_provider_config", lambda key: {})
    calls = []
    monkeypatch.setattr(
        "iac_code.commands.thinking_enabled.save_active_provider_config",
        lambda provider, model, thinking_enabled=None: calls.append((provider["key_name"], model, thinking_enabled)),
    )
    store = MagicMock()
    store.get_state.return_value = SimpleNamespace(model="gpt-5.5")

    result = await thinking_enabled_command(context=None, args=["off"], store=store)

    assert "disabled" in result
    assert calls == [("openai", "gpt-5.5", False)]
    store.set_state.assert_called_once_with(thinking_enabled=False)


@pytest.mark.asyncio
async def test_thinking_enabled_on_persists_and_updates_state(monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command

    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_active_provider_key", lambda: "openai")
    monkeypatch.setattr(
        "iac_code.commands.thinking_enabled.get_provider_config",
        lambda key: {"thinkingEnabled": False},
    )
    calls = []
    monkeypatch.setattr(
        "iac_code.commands.thinking_enabled.save_active_provider_config",
        lambda provider, model, thinking_enabled=None: calls.append((provider["key_name"], model, thinking_enabled)),
    )
    store = MagicMock()
    store.get_state.return_value = SimpleNamespace(model="gpt-5.5")

    result = await thinking_enabled_command(context=None, args=["on"], store=store)

    assert "enabled" in result
    assert calls == [("openai", "gpt-5.5", True)]
    store.set_state.assert_called_once_with(thinking_enabled=True)


@pytest.mark.asyncio
async def test_thinking_enabled_rejects_invalid_value(monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command

    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_active_provider_key", lambda: "openai")
    store = MagicMock()
    store.get_state.return_value = SimpleNamespace(model="gpt-5.5")

    result = await thinking_enabled_command(context=None, args=["maybe"], store=store)

    assert "Invalid thinking_enabled" in result
    store.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_thinking_enabled_shows_current_value(monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command

    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_active_provider_key", lambda: "openai")
    monkeypatch.setattr(
        "iac_code.commands.thinking_enabled.get_provider_config",
        lambda key: {"thinkingEnabled": False},
    )
    store = MagicMock()
    store.get_state.return_value = SimpleNamespace(model="gpt-5.5")

    result = await thinking_enabled_command(context=None, args=[], store=store)

    assert "disabled" in result


@pytest.mark.asyncio
async def test_thinking_enabled_interactive_selects_value(monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command

    monkeypatch.setattr("iac_code.commands.thinking_enabled.get_active_provider_key", lambda: "openai")
    monkeypatch.setattr(
        "iac_code.commands.thinking_enabled.get_provider_config",
        lambda key: {"thinkingEnabled": True},
    )
    selected_options = []

    def fake_select(title, options, default_index=0):
        selected_options.extend(options)
        return 1

    monkeypatch.setattr("iac_code.commands.thinking_enabled._select", fake_select)

    calls = []
    monkeypatch.setattr(
        "iac_code.commands.thinking_enabled.save_active_provider_config",
        lambda provider, model, thinking_enabled=None: calls.append((provider["key_name"], model, thinking_enabled)),
    )
    store = MagicMock()
    store.get_state.return_value = SimpleNamespace(model="gpt-5.5")
    context = SimpleNamespace(console=object(), store=store)

    result = await thinking_enabled_command(context=context, args=[])

    assert selected_options == ["on", "off"]
    assert "disabled" in result
    assert calls == [("openai", "gpt-5.5", False)]
    store.set_state.assert_called_once_with(thinking_enabled=False)
