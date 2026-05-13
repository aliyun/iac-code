"""Tests for the model command."""

from unittest.mock import MagicMock

import pytest

from iac_code.commands.model import (
    _get_active_provider,
    _get_active_provider_models,
    model_command,
)


@pytest.fixture
def fake_provider():
    return {
        "name": "Anthropic",
        "display_name": "Anthropic",
        "key_name": "anthropic",
        "api_base": None,
        "models": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "default_model": "claude-sonnet-4-6",
    }


class TestGetActiveProvider:
    def test_returns_none_when_no_key(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: None)
        assert _get_active_provider() is None

    def test_returns_matching_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        provider = _get_active_provider()
        assert provider is not None
        assert provider["key_name"] == "anthropic"

    def test_returns_none_when_key_not_in_providers(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "unknown")
        assert _get_active_provider() is None


class TestGetActiveProviderModels:
    def test_returns_models_when_active(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        models = _get_active_provider_models()
        assert "claude-sonnet-4-6" in models

    def test_returns_empty_when_no_active(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: None)
        assert _get_active_provider_models() == []


@pytest.mark.asyncio
class TestModelCommand:
    async def test_explicit_args_switches_model(self, monkeypatch):
        calls = []
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.commands.model.save_active_provider_config",
            lambda p, m: calls.append((p["key_name"], m)),
        )
        store = MagicMock()
        context = MagicMock(store=store)

        result = await model_command(context=context, args=["claude-opus-4-6"])

        assert "claude-opus-4-6" in result
        assert calls == [("anthropic", "claude-opus-4-6")]
        store.set_state.assert_called_with(model="claude-opus-4-6")

    async def test_no_context_no_console_returns_current(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        store = MagicMock()
        store.get_state.return_value = MagicMock(model="claude-sonnet-4-6")
        result = await model_command(store=store)
        assert "claude-sonnet-4-6" in result

    async def test_no_configured_providers(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_configured_providers", lambda: [])
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: None)
        store = MagicMock()
        store.get_state.return_value = MagicMock(model="")
        context = MagicMock(store=store)
        # console must be truthy to enter interactive branch
        context.console = MagicMock()
        result = await model_command(context=context)
        assert "no configured" in result.lower() or "auth" in result.lower()

    async def test_interactive_back_keeps_model(self, monkeypatch):
        from iac_code.commands.auth import _BACK

        monkeypatch.setattr("iac_code.commands.model.get_configured_providers", lambda: ["anthropic"])
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.commands.model.select_model_interactive",
            lambda models, current_model, provider_display_name: _BACK,
        )

        store = MagicMock()
        store.get_state.return_value = MagicMock(model="claude-sonnet-4-6")
        context = MagicMock(store=store)
        context.console = MagicMock()

        result = await model_command(context=context)
        assert "kept" in result.lower() or "claude-sonnet-4-6" in result

    async def test_interactive_selects_new_model(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_configured_providers", lambda: ["anthropic"])
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.commands.model.select_model_interactive",
            lambda models, current_model, provider_display_name: "claude-opus-4-6",
        )
        saved = []
        monkeypatch.setattr(
            "iac_code.commands.model.save_active_provider_config",
            lambda p, m: saved.append((p["key_name"], m)),
        )

        store = MagicMock()
        store.get_state.return_value = MagicMock(model="claude-sonnet-4-6")
        context = MagicMock(store=store)
        context.console = MagicMock()

        result = await model_command(context=context)
        assert "claude-opus-4-6" in result
        assert saved == [("anthropic", "claude-opus-4-6")]
