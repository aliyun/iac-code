"""Tests for auth.py storage functions (pure data I/O)."""

import pytest
import yaml

from iac_code.commands.auth import (
    _load_existing_api_base,
    _load_existing_key,
    _load_existing_model,
    get_configured_providers,
    save_active_provider_config,
    save_llm_key,
)


@pytest.fixture(autouse=True)
def iac_home(tmp_path, monkeypatch):
    """Redirect ~/.iac-code to tmp_path for each test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestSaveLlmKey:
    def test_creates_credentials_file(self, tmp_path):
        save_llm_key("anthropic", "sk-123")
        creds_path = tmp_path / ".iac-code" / ".credentials.yml"
        assert creds_path.exists()
        data = yaml.safe_load(creds_path.read_text())
        assert data == {"anthropic": "sk-123"}

    def test_adds_to_existing_file(self):
        save_llm_key("anthropic", "sk-123")
        save_llm_key("openai", "sk-456")
        keys = get_configured_providers()
        assert set(keys) >= {"anthropic", "openai"}

    def test_overwrites_same_provider(self):
        save_llm_key("anthropic", "v1")
        save_llm_key("anthropic", "v2")
        assert _load_existing_key("anthropic") == "v2"


class TestSaveActiveProviderConfig:
    def test_persists_name_model_api_base(self, tmp_path):
        provider = {
            "name": "Anthropic",
            "key_name": "anthropic",
            "api_base": "https://api.example.com",
        }
        save_active_provider_config(provider, "claude-sonnet-4-6")
        settings = yaml.safe_load((tmp_path / ".iac-code" / "settings.yml").read_text())
        assert settings["activeProvider"] == "anthropic"
        assert settings["providers"]["anthropic"]["model"] == "claude-sonnet-4-6"
        assert settings["providers"]["anthropic"]["name"] == "Anthropic"
        assert settings["providers"]["anthropic"]["apiBase"] == "https://api.example.com"

    def test_effort_override(self):
        provider = {"name": "OpenAI", "key_name": "openai", "api_base": None}
        save_active_provider_config(provider, "gpt-5.4", effort="high")
        from iac_code.config import get_provider_config

        assert get_provider_config("openai").get("effort") == "high"

    def test_api_base_override(self):
        provider = {"name": "X", "key_name": "custom", "api_base": None}
        save_active_provider_config(provider, "m", api_base="https://override/")
        from iac_code.config import get_provider_config

        assert get_provider_config("custom").get("apiBase") == "https://override/"

    def test_preserves_existing_entry_keys(self):
        provider = {"name": "A", "key_name": "anthropic", "api_base": None}
        save_active_provider_config(provider, "m1", effort="low")
        save_active_provider_config(provider, "m2")  # no effort → should keep "low"
        from iac_code.config import get_provider_config

        cfg = get_provider_config("anthropic")
        assert cfg.get("model") == "m2"
        assert cfg.get("effort") == "low"


class TestLoaders:
    def test_load_existing_key_returns_none_if_missing(self):
        assert _load_existing_key("nonexistent") is None

    def test_load_existing_key_returns_value(self):
        save_llm_key("anthropic", "sk-xxx")
        assert _load_existing_key("anthropic") == "sk-xxx"

    def test_load_existing_api_base_none_when_missing(self):
        assert _load_existing_api_base("nope") is None

    def test_load_existing_api_base_returns_value(self):
        provider = {"name": "X", "key_name": "custom", "api_base": "https://api/"}
        save_active_provider_config(provider, "m1")
        assert _load_existing_api_base("custom") == "https://api/"

    def test_load_existing_model_none_when_missing(self):
        assert _load_existing_model("nope") is None

    def test_load_existing_model_returns_value(self):
        provider = {"name": "Y", "key_name": "foo", "api_base": None}
        save_active_provider_config(provider, "m-foo")
        assert _load_existing_model("foo") == "m-foo"


class TestGetConfiguredProviders:
    def test_empty_returns_empty_list(self):
        assert get_configured_providers() == []

    def test_returns_saved_providers(self):
        save_llm_key("anthropic", "k1")
        save_llm_key("openai", "k2")
        assert set(get_configured_providers()) == {"anthropic", "openai"}

    def test_returns_empty_on_error(self, monkeypatch):
        def boom():
            raise RuntimeError("bad")

        monkeypatch.setattr("iac_code.commands.auth.get_credentials_path", boom)
        assert get_configured_providers() == []


class TestLegacyBailianMigration:
    """Pre-existing .credentials.yml uses the legacy ``bailian`` slot.
    /auth must surface it as ``dashscope``, and writing replaces it cleanly.
    """

    def _write_legacy_creds(self, tmp_path, value: str) -> None:
        creds_dir = tmp_path / ".iac-code"
        creds_dir.mkdir(exist_ok=True)
        (creds_dir / ".credentials.yml").write_text(f"bailian: {value}\n")

    def test_load_existing_key_dashscope_falls_back_to_bailian(self, tmp_path):
        self._write_legacy_creds(tmp_path, "legacy-key")
        assert _load_existing_key("dashscope") == "legacy-key"

    def test_get_configured_providers_normalizes_legacy_to_canonical(self, tmp_path):
        self._write_legacy_creds(tmp_path, "legacy-key")
        assert get_configured_providers() == ["dashscope"]

    def test_save_llm_key_drops_legacy_bailian(self, tmp_path):
        self._write_legacy_creds(tmp_path, "legacy-key")
        save_llm_key("dashscope", "new-key")
        data = yaml.safe_load((tmp_path / ".iac-code" / ".credentials.yml").read_text())
        assert data == {"dashscope": "new-key"}

    def test_save_active_provider_config_drops_legacy_providers_bailian(self, tmp_path):
        settings_path = tmp_path / ".iac-code" / "settings.yml"
        settings_path.parent.mkdir(exist_ok=True)
        settings_path.write_text("activeProvider: bailian\nproviders:\n  bailian:\n    model: qwen3.5-plus\n")
        provider = {"name": "DashScope", "key_name": "dashscope", "api_base": "https://x/v1"}
        save_active_provider_config(provider, "qwen3.6-plus")
        settings = yaml.safe_load(settings_path.read_text())
        assert settings["activeProvider"] == "dashscope"
        assert "bailian" not in settings["providers"]
        assert settings["providers"]["dashscope"]["model"] == "qwen3.6-plus"
