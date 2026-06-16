"""Tests for the Provider Registry."""

from __future__ import annotations

from iac_code.providers.registry import PROVIDER_REGISTRY, ModelEntry, ProviderDescriptor


class TestProviderRegistry:
    def test_registry_is_not_empty(self):
        assert len(PROVIDER_REGISTRY) > 0

    def test_all_descriptors_have_required_fields(self):
        for key, desc in PROVIDER_REGISTRY.items():
            assert desc.key == key
            assert desc.name
            assert desc.display_name
            assert desc.provider_class

    def test_known_providers_present(self):
        expected = [
            "dashscope",
            "openai",
            "anthropic",
            "deepseek",
            "gemini",
            "kimi_cn",
            "kimi_intl",
            "zhipu_cn",
            "minimax_cn",
            "volcengine_cn",
            "ollama",
            "lmstudio",
            "openrouter",
            "azure_openai",
            "modelscope",
            "openai_compatible",
        ]
        for key in expected:
            assert key in PROVIDER_REGISTRY, f"Missing provider: {key}"

    def test_default_model_returns_first_if_no_default(self):
        desc = ProviderDescriptor(
            key="test",
            name="Test",
            display_name="Test",
            provider_class="test.TestProvider",
            base_url=None,
            models=[ModelEntry("m1"), ModelEntry("m2")],
        )
        assert desc.default_model == "m1"

    def test_default_model_returns_marked_default(self):
        desc = ProviderDescriptor(
            key="test",
            name="Test",
            display_name="Test",
            provider_class="test.TestProvider",
            base_url=None,
            models=[ModelEntry("m1"), ModelEntry("m2", is_default=True)],
        )
        assert desc.default_model == "m2"

    def test_model_ids_property(self):
        desc = ProviderDescriptor(
            key="test",
            name="Test",
            display_name="Test",
            provider_class="test.TestProvider",
            base_url=None,
            models=[ModelEntry("m1"), ModelEntry("m2")],
        )
        assert desc.model_ids == ["m1", "m2"]

    def test_local_providers_no_api_key_required(self):
        for key in ("ollama", "lmstudio"):
            desc = PROVIDER_REGISTRY[key]
            assert desc.require_api_key is False
            assert desc.is_local is True

    def test_qwenpaw_provider_ids_populated(self):
        compatible_keys = {"openai_compatible", "anthropic_compatible"}
        for key, desc in PROVIDER_REGISTRY.items():
            if key not in compatible_keys:
                assert desc.qwenpaw_provider_ids, f"{key} missing qwenpaw_provider_ids"

    def test_dashscope_models_include_qwen(self):
        desc = PROVIDER_REGISTRY["dashscope"]
        assert any("qwen" in m.id for m in desc.models)

    def test_codingplan_providers_share_class_with_parent(self):
        assert PROVIDER_REGISTRY["aliyun_codingplan"].provider_class == PROVIDER_REGISTRY["dashscope"].provider_class
