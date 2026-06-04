"""Tests for the model command."""

from unittest.mock import MagicMock

import pytest
import yaml

from iac_code.commands.model import (
    _get_active_provider,
    _get_active_provider_models,
    model_command,
)
from iac_code.config import get_provider_config, get_settings_path


@pytest.mark.asyncio
class TestModelLocked:
    async def test_model_locked_when_qwenpaw(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "qwenpaw")
        store = MagicMock()
        context = MagicMock(store=store)
        result = await model_command(context=context)
        assert "QwenPaw" in result
        assert "/auth" in result

    async def test_model_locked_when_env(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "env")
        store = MagicMock()
        context = MagicMock(store=store)
        result = await model_command(context=context)
        assert "env" in result
        assert "/auth" in result

    async def test_model_locked_with_args(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "qwenpaw")
        store = MagicMock()
        context = MagicMock(store=store)
        result = await model_command(context=context, args=["gpt-4"])
        assert "QwenPaw" in result
        assert "/auth" in result

    async def test_model_not_locked_when_local(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr("iac_code.commands.model.save_active_provider_config", lambda p, m, api_base=None: None)
        store = MagicMock()
        context = MagicMock(store=store)
        result = await model_command(context=context, args=["claude-opus-4-6"])
        assert "claude-opus-4-6" in result
        assert "locked" not in result.lower()


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


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("IAC_CODE_API_KEY", raising=False)
    monkeypatch.delenv("IAC_CODE_PROVIDER", raising=False)
    monkeypatch.delenv("IAC_CODE_MODEL", raising=False)
    monkeypatch.delenv("IAC_CODE_BASE_URL", raising=False)
    return config_dir


def _write_settings(data: dict) -> None:
    settings_path = get_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _write_credentials(config_dir, data: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.yml").write_text(yaml.safe_dump(data), encoding="utf-8")


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
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.commands.model.save_active_provider_config",
            lambda p, m, api_base=None: calls.append((p["key_name"], m)),
        )
        store = MagicMock()
        context = MagicMock(store=store)

        result = await model_command(context=context, args=["claude-opus-4-6"])

        assert "claude-opus-4-6" in result
        assert calls == [("anthropic", "claude-opus-4-6")]
        store.set_state.assert_called_with(model="claude-opus-4-6")

    async def test_explicit_args_logs_custom_base_url_from_active_provider_settings(self, monkeypatch):
        events = []
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.commands.model._load_yaml",
            lambda path: {
                "activeProvider": "anthropic",
                "providers": {
                    "anthropic": {
                        "apiBase": "https://proxy.example.com/v1",
                    }
                },
            },
        )
        monkeypatch.setattr("iac_code.commands.model.save_active_provider_config", lambda p, m, api_base=None: None)
        monkeypatch.setattr("iac_code.commands.model.log_event", lambda name, payload: events.append((name, payload)))
        store = MagicMock()
        context = MagicMock(store=store)

        result = await model_command(context=context, args=["claude-opus-4-6"])

        assert "claude-opus-4-6" in result
        assert events
        assert events[-1][1]["has_custom_base_url"] is True
        assert events[-1][1]["custom_base_url_host_kind"] == "other"

    async def test_explicit_args_does_not_log_default_base_url_as_custom(self, monkeypatch):
        events = []
        provider = {
            "name": "Custom",
            "display_name": "Custom",
            "key_name": "custom",
            "api_base": "https://default.example.com/v1",
            "models": ["custom-model"],
            "default_model": "custom-model",
        }
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "custom")
        monkeypatch.setattr("iac_code.commands.model._get_active_provider", lambda: provider)
        monkeypatch.setattr(
            "iac_code.commands.model._load_yaml",
            lambda path: {
                "activeProvider": "custom",
                "providers": {
                    "custom": {
                        "apiBase": "https://default.example.com/v1",
                    }
                },
            },
        )
        monkeypatch.setattr("iac_code.commands.model.save_active_provider_config", lambda p, m, api_base=None: None)
        monkeypatch.setattr("iac_code.commands.model.log_event", lambda name, payload: events.append((name, payload)))
        store = MagicMock()
        context = MagicMock(store=store)

        result = await model_command(context=context, args=["custom-model"])

        assert "custom-model" in result
        assert events
        assert events[-1][1]["has_custom_base_url"] is False
        assert events[-1][1]["custom_base_url_host_kind"] == ""

    async def test_explicit_args_preserves_custom_api_base_in_settings(self, isolated_config_dir, monkeypatch):
        custom_api_base = "https://proxy.example.com/compatible-mode/v1"
        _write_settings(
            {
                "activeProvider": "dashscope",
                "providers": {
                    "dashscope": {
                        "name": "DashScope",
                        "model": "qwen3.7-max",
                        "apiBase": custom_api_base,
                    }
                },
            }
        )
        monkeypatch.setattr("iac_code.commands.model.log_event", lambda name, payload: None)

        store = MagicMock()
        context = MagicMock(store=store)

        result = await model_command(context=context, args=["qwen3.6-plus"])

        provider_config = get_provider_config("dashscope")
        assert "qwen3.6-plus" in result
        assert provider_config["model"] == "qwen3.6-plus"
        assert provider_config["apiBase"] == custom_api_base
        store.set_state.assert_called_with(model="qwen3.6-plus")

    async def test_no_context_no_console_returns_current(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        store = MagicMock()
        store.get_state.return_value = MagicMock(model="claude-sonnet-4-6")
        result = await model_command(store=store)
        assert "claude-sonnet-4-6" in result

    async def test_no_configured_providers(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
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

        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
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
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.model.get_configured_providers", lambda: ["anthropic"])
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.commands.model.select_model_interactive",
            lambda models, current_model, provider_display_name: "claude-opus-4-6",
        )
        saved = []
        monkeypatch.setattr(
            "iac_code.commands.model.save_active_provider_config",
            lambda p, m, api_base=None: saved.append((p["key_name"], m)),
        )

        store = MagicMock()
        store.get_state.return_value = MagicMock(model="claude-sonnet-4-6")
        context = MagicMock(store=store)
        context.console = MagicMock()

        result = await model_command(context=context)
        assert "claude-opus-4-6" in result
        assert saved == [("anthropic", "claude-opus-4-6")]

    async def test_interactive_select_logs_custom_base_url_from_active_provider_settings(self, monkeypatch):
        events = []
        monkeypatch.setattr("iac_code.commands.model.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.model.get_configured_providers", lambda: ["anthropic"])
        monkeypatch.setattr("iac_code.commands.model.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.commands.model._load_yaml",
            lambda path: {
                "activeProvider": "anthropic",
                "providers": {
                    "anthropic": {
                        "apiBase": "https://proxy.example.com/v1",
                    }
                },
            },
        )
        monkeypatch.setattr(
            "iac_code.commands.model.select_model_interactive",
            lambda models, current_model, provider_display_name: "claude-opus-4-6",
        )
        monkeypatch.setattr("iac_code.commands.model.save_active_provider_config", lambda p, m, api_base=None: None)
        monkeypatch.setattr("iac_code.commands.model.log_event", lambda name, payload: events.append((name, payload)))

        store = MagicMock()
        store.get_state.return_value = MagicMock(model="claude-sonnet-4-6")
        context = MagicMock(store=store)
        context.console = MagicMock()

        result = await model_command(context=context)

        assert "claude-opus-4-6" in result
        assert events
        assert events[-1][1]["has_custom_base_url"] is True
        assert events[-1][1]["custom_base_url_host_kind"] == "other"

    async def test_interactive_select_preserves_custom_api_base_in_settings(self, isolated_config_dir, monkeypatch):
        custom_api_base = "https://proxy.example.com/compatible-mode/v1"
        _write_settings(
            {
                "activeProvider": "dashscope",
                "providers": {
                    "dashscope": {
                        "name": "DashScope",
                        "model": "qwen3.7-max",
                        "apiBase": custom_api_base,
                    }
                },
            }
        )
        _write_credentials(isolated_config_dir, {"dashscope": "fake-api-key"})
        monkeypatch.setattr(
            "iac_code.commands.model.select_model_interactive",
            lambda models, current_model, provider_display_name: "qwen3.6-plus",
        )
        monkeypatch.setattr("iac_code.commands.model.log_event", lambda name, payload: None)

        store = MagicMock()
        store.get_state.return_value = MagicMock(model="qwen3.7-max")
        context = MagicMock(store=store)
        context.console = MagicMock()

        result = await model_command(context=context)

        provider_config = get_provider_config("dashscope")
        assert "qwen3.6-plus" in result
        assert provider_config["model"] == "qwen3.6-plus"
        assert provider_config["apiBase"] == custom_api_base
        store.set_state.assert_called_with(model="qwen3.6-plus")
