"""Tests for new Phase 2 provider classes."""

from __future__ import annotations


class TestNewProviderImports:
    """Verify all new provider classes can be imported and instantiated."""

    def test_kimi_provider(self):
        from iac_code.providers.kimi_provider import KimiProvider

        p = KimiProvider(model="kimi-k2.6", api_key="test")
        assert p.get_model_name() == "kimi-k2.6"
        assert p._PROVIDER_KEY == "kimi_cn"

    def test_zhipu_provider(self):
        from iac_code.providers.zhipu_provider import ZhiPuProvider

        p = ZhiPuProvider(model="glm-5.1", api_key="test")
        assert p.get_model_name() == "glm-5.1"
        assert p._PROVIDER_KEY == "zhipu_cn"

    def test_volcengine_provider(self):
        from iac_code.providers.volcengine_provider import VolcengineProvider

        p = VolcengineProvider(model="doubao-seed", api_key="test")
        assert p.get_model_name() == "doubao-seed"
        assert p._PROVIDER_KEY == "volcengine_cn"

    def test_siliconflow_provider(self):
        from iac_code.providers.siliconflow_provider import SiliconFlowProvider

        p = SiliconFlowProvider(model="test-model", api_key="test")
        assert p.get_model_name() == "test-model"
        assert p._PROVIDER_KEY == "siliconflow_cn"

    def test_ollama_provider_default_api_key(self):
        from iac_code.providers.ollama_provider import OllamaProvider

        p = OllamaProvider(model="llama3")
        assert p.get_model_name() == "llama3"
        assert p._PROVIDER_KEY == "ollama"

    def test_lmstudio_provider_default_api_key(self):
        from iac_code.providers.lmstudio_provider import LMStudioProvider

        p = LMStudioProvider(model="local-model")
        assert p.get_model_name() == "local-model"
        assert p._PROVIDER_KEY == "lmstudio"

    def test_openrouter_provider(self):
        from iac_code.providers.openrouter_provider import OpenRouterProvider

        p = OpenRouterProvider(model="test", api_key="test")
        assert p.get_model_name() == "test"
        assert p._PROVIDER_KEY == "openrouter"

    def test_azure_openai_provider(self):
        from iac_code.providers.azure_openai_provider import AzureOpenAIProvider

        p = AzureOpenAIProvider(model="gpt-5", api_key="test")
        assert p.get_model_name() == "gpt-5"
        assert p._PROVIDER_KEY == "azure_openai"

    def test_modelscope_provider(self):
        from iac_code.providers.modelscope_provider import ModelScopeProvider

        p = ModelScopeProvider(model="Qwen/Qwen3.5", api_key="test")
        assert p.get_model_name() == "Qwen/Qwen3.5"
        assert p._PROVIDER_KEY == "modelscope"

    def test_minimax_provider(self):
        from iac_code.providers.minimax_provider import MiniMaxProvider

        p = MiniMaxProvider(model="MiniMax-M2.5", api_key="test")
        assert p.get_model_name() == "MiniMax-M2.5"
        assert p._PROVIDER_KEY == "minimax_cn"

    def test_gemini_provider(self):
        from iac_code.providers.gemini_provider import GeminiProvider

        p = GeminiProvider(model="gemini-2.5-pro", api_key="test")
        assert p.get_model_name() == "gemini-2.5-pro"
        assert p._PROVIDER_KEY == "gemini"


class TestProviderKeyOverride:
    """Verify provider_key parameter is respected."""

    def test_kimi_intl_key(self):
        from iac_code.providers.kimi_provider import KimiProvider

        p = KimiProvider(model="kimi-k2.6", api_key="test", provider_key="kimi_intl")
        assert p._PROVIDER_KEY == "kimi_intl"

    def test_zhipu_intl_key(self):
        from iac_code.providers.zhipu_provider import ZhiPuProvider

        p = ZhiPuProvider(model="glm-5", api_key="test", provider_key="zhipu_intl")
        assert p._PROVIDER_KEY == "zhipu_intl"


class TestRegistryDrivenCreateProvider:
    def test_creates_kimi_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "kimi_cn")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        from iac_code.providers.manager import create_provider

        p = create_provider("kimi-k2.6", credentials={"kimi_cn": "test-key"})
        assert p.get_model_name() == "kimi-k2.6"

    def test_creates_gemini_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "gemini")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        from iac_code.providers.manager import create_provider

        p = create_provider("gemini-2.5-pro", credentials={"gemini": "test-key"})
        assert p.get_model_name() == "gemini-2.5-pro"

    def test_creates_ollama_without_api_key(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "ollama")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        from iac_code.providers.manager import create_provider

        p = create_provider("llama3", credentials={})
        assert p.get_model_name() == "llama3"

    def test_provider_key_override(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        from iac_code.providers.manager import create_provider

        p = create_provider(
            "glm-5",
            credentials={"zhipu_intl": "test-key"},
            provider_key_override="zhipu_intl",
        )
        assert p._PROVIDER_KEY == "zhipu_intl"
