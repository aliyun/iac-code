"""Tests for provider model updates captured in provider-model-research.md."""

from __future__ import annotations

from iac_code.providers.kimi_provider import KimiProvider
from iac_code.providers.minimax_provider import MiniMaxProvider
from iac_code.providers.registry import PROVIDER_REGISTRY, ModelEntry
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec
from iac_code.providers.zhipu_provider import ZhiPuProvider


def _model_entry(provider_key: str, model_id: str) -> ModelEntry:
    for entry in PROVIDER_REGISTRY[provider_key].models:
        if entry.id == model_id:
            return entry
    raise AssertionError(f"{model_id} missing from {provider_key}")


def _model_ids(provider_key: str) -> list[str]:
    return PROVIDER_REGISTRY[provider_key].model_ids


def test_dashscope_models_match_researched_bailian_catalog() -> None:
    models = _model_ids("dashscope")

    for model_id in (
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "qwen-plus",
        "qwen-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "kimi-k2.5",
        "glm-5.2",
        "glm-5.1",
        "MiniMax-M2.5",
    ):
        assert model_id in models

    # Soon-offline models are still callable until removed upstream.
    for model_id in ("qwen3.6-max-preview", "qwen3-max", "qwen3-coder-plus", "qwen3-coder-next", "qwq-plus"):
        assert model_id in models
        assert not _model_entry("dashscope", model_id).is_default

    assert PROVIDER_REGISTRY["dashscope"].default_model == "qwen3.7-max"
    assert not _model_entry("dashscope", "qwen3.7-max").support_multimodal
    for model_id in (
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "kimi-k2.7-code",
    ):
        assert _model_entry("dashscope", model_id).support_multimodal


def test_dashscope_token_plan_uses_exact_supported_chat_models() -> None:
    models = _model_ids("dashscope_token_plan")

    for model_id in (
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v3.2",
        "glm-5.2",
        "glm-5.1",
        "glm-5",
        "MiniMax-M2.5",
        "kimi-k2.7-code",
        "kimi-k2.5",
        "kimi-k2.6",
    ):
        assert model_id in models

    assert "glm-5-turbo" not in models
    assert "MiniMax-M2.7" not in models
    assert PROVIDER_REGISTRY["dashscope_token_plan"].default_model == "qwen3.7-max"
    assert not _model_entry("dashscope_token_plan", "qwen3.7-max").support_multimodal
    for model_id in ("qwen3.7-plus", "qwen3.6-plus", "qwen3.6-flash", "kimi-k2.7-code", "kimi-k2.5", "kimi-k2.6"):
        assert _model_entry("dashscope_token_plan", model_id).support_multimodal


def test_openai_azure_anthropic_and_gemini_models_are_updated() -> None:
    for model_id in ("gpt-5.5-pro", "gpt-5.4-pro", "gpt-5.4-nano"):
        assert model_id in _model_ids("openai")
        assert get_thinking_spec("openai", model_id).family is ThinkingFamily.OPENAI

    expected_azure = {
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-5.2",
    }
    assert set(_model_ids("azure_openai")) == expected_azure
    assert PROVIDER_REGISTRY["azure_openai"].default_model == "gpt-5.5"
    assert get_thinking_spec("azure_openai", "gpt-5.4-pro").family is ThinkingFamily.OPENAI

    assert PROVIDER_REGISTRY["anthropic"].default_model == "claude-opus-4-8"
    assert get_thinking_spec("anthropic", "claude-opus-4-8").family is ThinkingFamily.ANTHROPIC

    assert "gemini-3.1-pro-preview-customtools" in _model_ids("gemini")
    assert get_thinking_spec("gemini", "gemini-2.5-flash-lite").family is ThinkingFamily.GEMINI


def test_direct_kimi_minimax_and_zhipu_models_are_updated() -> None:
    for provider_key in ("kimi_cn", "kimi_intl"):
        assert PROVIDER_REGISTRY[provider_key].default_model == "kimi-k2.7-code"
        assert "kimi-k2.7-code" in _model_ids(provider_key)
        assert _model_entry(provider_key, "kimi-k2.7-code").support_multimodal
        assert get_thinking_spec(provider_key, "kimi-k2.6").family is ThinkingFamily.KIMI

    for provider_key in ("minimax_cn", "minimax_intl"):
        assert PROVIDER_REGISTRY[provider_key].default_model == "MiniMax-M3"
        assert _model_entry(provider_key, "MiniMax-M3").support_multimodal
        assert "MiniMax-M2.1" not in _model_ids(provider_key)
        assert get_thinking_spec(provider_key, "MiniMax-M3").family is ThinkingFamily.MINIMAX

    for provider_key in ("zhipu_cn", "zhipu_intl"):
        assert "glm-5.2" in _model_ids(provider_key)
        assert PROVIDER_REGISTRY[provider_key].default_model == "glm-5.2"
        assert get_thinking_spec(provider_key, "glm-5.2").family is ThinkingFamily.ZHIPU
        assert get_thinking_spec(provider_key, "glm-5.1").family is ThinkingFamily.ZHIPU

    for provider_key in ("zhipu_cn_codingplan", "zhipu_intl_codingplan"):
        assert "glm-5.2" in _model_ids(provider_key)
        assert PROVIDER_REGISTRY[provider_key].default_model == "glm-5.2"
        assert get_thinking_spec(provider_key, "glm-5.2").family is ThinkingFamily.ZHIPU


def test_provider_specific_thinking_wire_formats_do_not_use_openai_or_anthropic_effort_fields() -> None:
    kimi = KimiProvider(model="kimi-k2.6", api_key="k", effort="high")
    assert kimi._build_thinking_kwargs() == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert KimiProvider(model="kimi-k2.7-code", api_key="k", effort="high")._build_thinking_kwargs() == {}

    zhipu = ZhiPuProvider(model="glm-5.1", api_key="k", effort="high")
    assert zhipu._build_thinking_kwargs() == {"extra_body": {"thinking": {"type": "enabled"}}}

    minimax = MiniMaxProvider(model="MiniMax-M3", api_key="k", effort="high")
    assert minimax._build_thinking_kwargs() == {"thinking": {"type": "adaptive"}}

    for kwargs in (kimi._build_thinking_kwargs(), zhipu._build_thinking_kwargs(), minimax._build_thinking_kwargs()):
        assert "reasoning_effort" not in kwargs
        assert kwargs.get("thinking", {}).get("budget_tokens") is None
