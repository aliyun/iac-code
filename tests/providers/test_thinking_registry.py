"""Tests for the centralized thinking registry."""

from __future__ import annotations

from iac_code.providers.thinking import (
    EffortLevel,
    ThinkingFamily,
    get_thinking_spec,
)


class TestGetThinkingSpec:
    def test_anthropic_claude_opus_7(self):
        spec = get_thinking_spec("anthropic", "claude-opus-4-7")
        assert spec.family is ThinkingFamily.ANTHROPIC
        assert spec.supports_effort is True
        assert spec.default_effort is EffortLevel.HIGH

    def test_openai_gpt55(self):
        spec = get_thinking_spec("openai", "gpt-5.5")
        assert spec.family is ThinkingFamily.OPENAI
        assert spec.allowed_efforts == (
            EffortLevel.LOW,
            EffortLevel.MEDIUM,
            EffortLevel.HIGH,
            EffortLevel.XHIGH,
        )
        assert spec.default_effort is EffortLevel.HIGH

    def test_deepseek_official_uses_openai_family_with_high_max(self):
        spec = get_thinking_spec("deepseek", "deepseek-v4-pro")
        assert spec.family is ThinkingFamily.OPENAI
        assert spec.allowed_efforts == (EffortLevel.HIGH, EffortLevel.MAX)
        assert spec.default_effort is EffortLevel.HIGH

    def test_dashscope_qwen_supports_thinking_no_effort(self):
        spec = get_thinking_spec("dashscope", "qwen3.6-plus")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == ()
        assert spec.supports_effort is False

    def test_dashscope_kimi(self):
        spec = get_thinking_spec("dashscope", "kimi-k2.6")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == ()

    def test_dashscope_glm(self):
        spec = get_thinking_spec("dashscope", "glm-5.1")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == ()

    def test_dashscope_deepseek_supports_high_max(self):
        spec = get_thinking_spec("dashscope", "deepseek-v4-pro")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == (EffortLevel.HIGH, EffortLevel.MAX)
        assert spec.default_effort is EffortLevel.HIGH

    def test_dashscope_qwen36_max_preview(self):
        spec = get_thinking_spec("dashscope", "qwen3.6-max-preview")
        assert spec.family is ThinkingFamily.DASHSCOPE

    def test_unknown_provider_returns_none(self):
        spec = get_thinking_spec("nonexistent", "anything")
        assert spec.family is ThinkingFamily.NONE
        assert spec.supports_effort is False

    def test_unknown_model_returns_none(self):
        spec = get_thinking_spec("openai", "no-such-model")
        assert spec.family is ThinkingFamily.NONE

    def test_default_thinking_budget_is_none_for_all_current_models(self):
        for provider_key in (
            "anthropic",
            "openai",
            "deepseek",
            "dashscope",
            "dashscope_token_plan",
        ):
            from iac_code.providers.thinking import MODEL_THINKING

            for model, spec in MODEL_THINKING[provider_key].items():
                assert spec.default_thinking_budget is None, (provider_key, model)

    def test_token_plan_qwen36_plus(self):
        spec = get_thinking_spec("dashscope_token_plan", "qwen3.6-plus")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == ()
        assert spec.supports_effort is False

    def test_token_plan_deepseek_v32(self):
        spec = get_thinking_spec("dashscope_token_plan", "deepseek-v3.2")
        assert spec.family is ThinkingFamily.DASHSCOPE

    def test_token_plan_glm5(self):
        spec = get_thinking_spec("dashscope_token_plan", "glm-5")
        assert spec.family is ThinkingFamily.DASHSCOPE

    def test_token_plan_minimax_m25(self):
        spec = get_thinking_spec("dashscope_token_plan", "MiniMax-M2.5")
        assert spec.family is ThinkingFamily.DASHSCOPE

    def test_same_model_different_provider_different_spec(self):
        official = get_thinking_spec("deepseek", "deepseek-v4-pro")
        dashscope_hosted = get_thinking_spec("dashscope", "deepseek-v4-pro")
        assert official.family is ThinkingFamily.OPENAI
        assert dashscope_hosted.family is ThinkingFamily.DASHSCOPE
