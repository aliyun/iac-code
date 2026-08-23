"""Tests for the centralized thinking registry."""

from __future__ import annotations

from iac_code.providers.thinking import (
    EffortLevel,
    ThinkingFamily,
    get_thinking_spec,
    resolve_thinking_active,
)


class TestGetThinkingSpec:
    def test_anthropic_claude_opus_7(self):
        spec = get_thinking_spec("anthropic", "claude-opus-4-7")
        assert spec.family is ThinkingFamily.ANTHROPIC_ADAPTIVE
        assert spec.supports_effort is True
        assert spec.default_effort is EffortLevel.HIGH

    def test_anthropic_opus5_defaults_on_and_limits_disable_efforts(self):
        spec = get_thinking_spec("anthropic", "claude-opus-5")
        assert spec.family is ThinkingFamily.ANTHROPIC_ADAPTIVE
        assert spec.allowed_efforts == (
            EffortLevel.LOW,
            EffortLevel.MEDIUM,
            EffortLevel.HIGH,
            EffortLevel.XHIGH,
            EffortLevel.MAX,
            EffortLevel.AUTO,
        )
        assert spec.default_effort is EffortLevel.HIGH
        assert spec.thinking_enabled_by_default is True
        assert spec.disable_forbidden_efforts == (EffortLevel.XHIGH, EffortLevel.MAX)

    def test_anthropic_haiku_supports_manual_thinking_budget(self):
        spec = get_thinking_spec("anthropic", "claude-haiku-4-5-20251001")
        assert spec.family is ThinkingFamily.ANTHROPIC
        assert spec.supports_thinking_budget is True

    def test_openai_gpt55(self):
        spec = get_thinking_spec("openai", "gpt-5.5")
        assert spec.family is ThinkingFamily.OPENAI
        assert spec.allowed_efforts == (
            EffortLevel.NONE,
            EffortLevel.LOW,
            EffortLevel.MEDIUM,
            EffortLevel.HIGH,
            EffortLevel.XHIGH,
        )
        assert spec.default_effort is EffortLevel.MEDIUM

    def test_openai_codex_and_o_series_have_generation_specific_efforts(self):
        codex = get_thinking_spec("openai", "gpt-5.3-codex")
        codex_52 = get_thinking_spec("openai", "gpt-5.2-codex")
        o3 = get_thinking_spec("openai", "o3")
        o4_mini = get_thinking_spec("openai", "o4-mini")

        assert codex.allowed_efforts == (
            EffortLevel.LOW,
            EffortLevel.MEDIUM,
            EffortLevel.HIGH,
            EffortLevel.XHIGH,
        )
        assert codex_52.allowed_efforts == codex.allowed_efforts
        assert o3.allowed_efforts == (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.HIGH)
        assert o4_mini.allowed_efforts == o3.allowed_efforts
        assert EffortLevel.NONE not in o3.allowed_efforts

    def test_deepseek_official_uses_openai_family_with_low_high_max(self):
        spec = get_thinking_spec("deepseek", "deepseek-v4-pro")
        assert spec.family is ThinkingFamily.OPENAI
        assert spec.allowed_efforts == (EffortLevel.LOW, EffortLevel.HIGH, EffortLevel.MAX)
        assert spec.default_effort is EffortLevel.HIGH
        assert spec.thinking_enabled_by_default is True

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
        assert spec.allowed_efforts == (
            EffortLevel.NONE,
            EffortLevel.MINIMAL,
            EffortLevel.LOW,
            EffortLevel.MEDIUM,
            EffortLevel.HIGH,
            EffortLevel.XHIGH,
        )
        assert spec.uses_reasoning_effort_param is True

    def test_dashscope_glm52_models_use_effort_and_total_output_limit_without_thinking_budget(self):
        for model in ("glm-5.2", "glm-5.2-fast-preview"):
            spec = get_thinking_spec("dashscope", model)
            assert spec.family is ThinkingFamily.DASHSCOPE
            assert spec.allowed_efforts == (
                EffortLevel.NONE,
                EffortLevel.MINIMAL,
                EffortLevel.LOW,
                EffortLevel.MEDIUM,
                EffortLevel.HIGH,
                EffortLevel.XHIGH,
                EffortLevel.MAX,
            )
            assert spec.default_thinking_budget is None
            assert spec.supports_thinking_budget is False
            assert spec.use_max_completion_tokens is True
            assert spec.uses_reasoning_effort_param is True

    def test_qwen38_formal_and_preview_have_distinct_thinking_modes(self):
        for provider_key in ("dashscope", "dashscope_token_plan"):
            formal = get_thinking_spec(provider_key, "qwen3.8-max")
            assert formal.allowed_efforts == (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.XHIGH)
            assert formal.default_effort is EffortLevel.XHIGH
            assert formal.supports_disable is True
            assert formal.thinking_enabled_by_default is True

        preview = get_thinking_spec("dashscope_token_plan", "qwen3.8-max-preview")
        assert preview.allowed_efforts == (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.XHIGH)
        assert preview.default_effort is EffortLevel.XHIGH
        assert preview.supports_disable is False
        assert preview.thinking_enabled_by_default is True

    def test_anthropic_46_excludes_xhigh_but_keeps_max(self):
        spec = get_thinking_spec("anthropic", "claude-sonnet-4-6")
        assert EffortLevel.XHIGH not in spec.allowed_efforts
        assert EffortLevel.MAX in spec.allowed_efforts

    def test_gemini_efforts_and_defaults_are_per_model(self):
        latest_flash = get_thinking_spec("gemini", "gemini-3.6-flash")
        flash = get_thinking_spec("gemini", "gemini-3.5-flash")
        latest_lite = get_thinking_spec("gemini", "gemini-3.5-flash-lite")
        pro = get_thinking_spec("gemini", "gemini-3.1-pro-preview")
        lite = get_thinking_spec("gemini", "gemini-2.5-flash-lite")

        assert latest_flash.default_effort is EffortLevel.MEDIUM
        assert latest_lite.default_effort is EffortLevel.MINIMAL
        assert latest_flash.allowed_efforts == (
            EffortLevel.MINIMAL,
            EffortLevel.LOW,
            EffortLevel.MEDIUM,
            EffortLevel.HIGH,
        )
        assert latest_lite.allowed_efforts == latest_flash.allowed_efforts
        assert EffortLevel.MINIMAL in flash.allowed_efforts
        assert flash.default_effort is EffortLevel.MEDIUM
        assert pro.default_effort is EffortLevel.HIGH
        assert pro.supports_disable is False
        assert EffortLevel.MINIMAL in lite.allowed_efforts
        assert lite.default_effort is EffortLevel.NONE
        assert lite.supports_disable is True

    def test_gemini_37_flash_excludes_minimal_effort(self):
        spec = get_thinking_spec("gemini", "gemini-3.7-flash")
        assert spec.family is ThinkingFamily.GEMINI
        assert spec.allowed_efforts == (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.HIGH)
        assert EffortLevel.MINIMAL not in spec.allowed_efforts
        assert spec.default_effort is EffortLevel.MEDIUM
        assert spec.supports_disable is False

    def test_direct_glm52_supports_high_and_max_effort(self):
        for provider_key in ("zhipu_cn", "zhipu_intl", "zhipu_cn_codingplan", "zhipu_intl_codingplan"):
            spec = get_thinking_spec(provider_key, "glm-5.2")
            assert spec.allowed_efforts == (EffortLevel.HIGH, EffortLevel.MAX)
            assert spec.default_effort is EffortLevel.MAX
            assert spec.uses_reasoning_effort_param is True

    def test_dashscope_kimi_k27_code_has_bounded_default_request_policy(self):
        spec = get_thinking_spec("dashscope", "kimi-k2.7-code")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == ()
        assert spec.default_thinking_budget == 8192
        assert spec.supports_thinking_budget is True
        assert spec.use_max_completion_tokens is True
        assert spec.uses_reasoning_effort_param is False

    def test_token_plan_glm52_and_kimi_k27_code_use_model_specific_request_policies(self):
        from iac_code.providers.thinking import MODEL_THINKING

        glm = get_thinking_spec("dashscope_token_plan", "glm-5.2")
        kimi = get_thinking_spec("dashscope_token_plan", "kimi-k2.7-code")

        assert "glm-5.2" in MODEL_THINKING["dashscope_token_plan"]
        assert "kimi-k2.7-code" in MODEL_THINKING["dashscope_token_plan"]
        assert glm.family is ThinkingFamily.DASHSCOPE
        assert glm.default_thinking_budget is None
        assert glm.supports_thinking_budget is False
        assert glm.use_max_completion_tokens is True
        assert glm.uses_reasoning_effort_param is True

        assert kimi.family is ThinkingFamily.DASHSCOPE
        assert kimi.default_thinking_budget == 8192
        assert kimi.supports_thinking_budget is True
        assert kimi.use_max_completion_tokens is True
        assert kimi.uses_reasoning_effort_param is False

    def test_qwen37_max_has_bounded_thinking_budget_on_both_endpoints(self):
        # 长尾治理契约:qwen3.7-max 的思考阶段必须有硬预算上限,否则服务端可能
        # 产出 ~12 分钟级 thinking(实测 p99 728,878ms)。两个 endpoint 必须一致,
        # 只治一个入口等于没治。
        for provider_key in ("dashscope", "dashscope_token_plan"):
            spec = get_thinking_spec(provider_key, "qwen3.7-max")
            assert spec.family is ThinkingFamily.DASHSCOPE, provider_key
            assert spec.supports_thinking_budget is True, provider_key
            assert spec.default_thinking_budget == 8192, provider_key
            # 预算走 extra_body.thinking_budget,不改 max_tokens 语义。
            assert spec.use_max_completion_tokens is False, provider_key
            assert spec.allowed_efforts == (), provider_key

    def test_qwen37_siblings_keep_unbounded_thinking(self):
        # 收敛范围校验:只有 qwen3.7-max 被声明有界,同代其他模型不受影响。
        for model in ("qwen3.7-plus", "qwen3.7-flash"):
            spec = get_thinking_spec("dashscope", model)
            assert spec.supports_thinking_budget is False, model
            assert spec.default_thinking_budget is None, model

    def test_thinking_budget_capability_false_for_effort_families(self):
        # UI gating contract: the 思考预算 field must NOT appear for effort-driven
        # families (Anthropic/OpenAI/Gemini). Lock the negative side so a registry
        # edit can't silently start exposing the budget knob where it has no effect.
        for provider_key, model in (
            ("anthropic", "claude-opus-4-7"),
            ("openai", "gpt-5.5"),
            ("gemini", "gemini-3.5-flash"),
        ):
            spec = get_thinking_spec(provider_key, model)
            assert spec.supports_thinking_budget is False, (provider_key, model)
            assert spec.use_max_completion_tokens is False, (provider_key, model)
            assert spec.default_thinking_budget is None, (provider_key, model)

    def test_dashscope_deepseek_supports_documented_efforts(self):
        for model in ("deepseek-v4-pro", "deepseek-v4-pro-0813", "deepseek-v4-flash", "deepseek-v4-flash-0731"):
            spec = get_thinking_spec("dashscope", model)
            assert spec.family is ThinkingFamily.DASHSCOPE
            assert spec.allowed_efforts == (
                EffortLevel.LOW,
                EffortLevel.MEDIUM,
                EffortLevel.HIGH,
                EffortLevel.XHIGH,
                EffortLevel.MAX,
            )
            assert spec.default_effort is EffortLevel.HIGH
            assert spec.uses_reasoning_effort_param is True

    def test_token_plan_deepseek_0731_is_registered(self):
        spec = get_thinking_spec("dashscope_token_plan", "deepseek-v4-flash-0731")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == (EffortLevel.LOW, EffortLevel.HIGH, EffortLevel.MAX)
        assert spec.default_effort is EffortLevel.HIGH

    def test_token_plan_deepseek_pro_0813_is_registered(self):
        spec = get_thinking_spec("dashscope_token_plan", "deepseek-v4-pro-0813")
        assert spec.family is ThinkingFamily.DASHSCOPE
        assert spec.allowed_efforts == (EffortLevel.LOW, EffortLevel.HIGH, EffortLevel.MAX)
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

    def test_default_thinking_budget_is_none_for_models_without_bounded_policy(self):
        for provider_key in (
            "anthropic",
            "openai",
            "deepseek",
            "dashscope",
            "dashscope_token_plan",
        ):
            from iac_code.providers.thinking import MODEL_THINKING

            for model, spec in MODEL_THINKING[provider_key].items():
                if (provider_key, model) in {
                    ("dashscope", "kimi-k2.7-code"),
                    ("dashscope_token_plan", "kimi-k2.7-code"),
                    ("dashscope", "qwen3.7-max"),
                    ("dashscope_token_plan", "qwen3.7-max"),
                }:
                    continue
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
        assert spec.allowed_efforts[-1] is EffortLevel.XHIGH
        assert EffortLevel.MAX not in spec.allowed_efforts
        assert spec.uses_reasoning_effort_param is True

    def test_zhipu_intl_coding_plan_follows_nested_thinking_fallback(self):
        spec = get_thinking_spec("zhipu_intl_codingplan", "glm-5.1")
        assert spec.family is ThinkingFamily.ZHIPU

    def test_glm53_coding_plan_is_always_on_with_low_high_max(self):
        for provider_key in ("zhipu_cn_codingplan", "zhipu_intl_codingplan"):
            spec = get_thinking_spec(provider_key, "glm-5.3")
            assert spec.family is ThinkingFamily.ZHIPU
            assert spec.allowed_efforts == (EffortLevel.LOW, EffortLevel.HIGH, EffortLevel.MAX)
            assert spec.default_effort is EffortLevel.MAX
            assert spec.uses_reasoning_effort_param is True
            assert spec.supports_disable is False
            assert spec.thinking_enabled_by_default is True
        # The standard ZhiPu model API is not live for glm-5.3 yet.
        assert get_thinking_spec("zhipu_cn", "glm-5.3").family is ThinkingFamily.NONE
        assert get_thinking_spec("zhipu_intl", "glm-5.3").family is ThinkingFamily.NONE

    def test_token_plan_minimax_m25(self):
        spec = get_thinking_spec("dashscope_token_plan", "MiniMax-M2.5")
        assert spec.family is ThinkingFamily.DASHSCOPE

    def test_same_model_different_provider_different_spec(self):
        official = get_thinking_spec("deepseek", "deepseek-v4-pro")
        dashscope_hosted = get_thinking_spec("dashscope", "deepseek-v4-pro")
        assert official.family is ThinkingFamily.OPENAI
        assert dashscope_hosted.family is ThinkingFamily.DASHSCOPE


class TestResolveThinkingActive:
    def test_dashscope_unset_defaults_on(self):
        # 用户报告场景:qwen3.7-max 未配置 thinkingEnabled → 本回合仍思考。
        assert resolve_thinking_active("dashscope", "qwen3.7-max", None) is True

    def test_kimi_and_zhipu_unset_default_on(self):
        assert resolve_thinking_active("kimi_cn", "kimi-k2.6", None) is True
        assert resolve_thinking_active("zhipu_cn", "glm-5.2", None) is True

    def test_glm53_cannot_be_turned_off(self):
        assert resolve_thinking_active("zhipu_cn_codingplan", "glm-5.3", None) is True
        assert resolve_thinking_active("zhipu_cn_codingplan", "glm-5.3", False) is True
        assert resolve_thinking_active("zhipu_intl_codingplan", "glm-5.3", False) is True

    def test_reasoning_families_unset_default_off(self):
        # reasoning-effort / budget 家族:未配置时不下发思考指令 → 视为关。
        assert resolve_thinking_active("openai", "gpt-5.5", None) is False
        assert resolve_thinking_active("anthropic", "claude-opus-4-8", None) is False
        assert resolve_thinking_active("gemini", "gemini-3.5-flash", None) is False

    def test_anthropic_default_on_and_disable_constraints_are_reflected(self):
        assert resolve_thinking_active("anthropic", "claude-fable-5", None) is True
        assert resolve_thinking_active("anthropic", "claude-opus-5", None) is True
        assert resolve_thinking_active("anthropic", "claude-opus-5", False, effort="high") is False
        assert resolve_thinking_active("anthropic", "claude-opus-5", False, effort="xhigh") is True
        assert resolve_thinking_active("anthropic", "claude-opus-5", False, effort="max") is True

    def test_explicit_true_forces_on_across_families(self):
        assert resolve_thinking_active("openai", "gpt-5.5", True) is True
        assert resolve_thinking_active("dashscope", "qwen3.7-max", True) is True

    def test_explicit_false_forces_off_across_families(self):
        assert resolve_thinking_active("dashscope", "qwen3.7-max", False) is False
        assert resolve_thinking_active("openai", "gpt-5.5", False) is False

    def test_none_family_never_thinks(self):
        # 未知模型 → NONE 家族,即使显式打开也无思考协议可用。
        assert resolve_thinking_active("dashscope", "unknown-model", None) is False
        assert resolve_thinking_active("dashscope", "unknown-model", True) is False

    def test_dashscope_disable_effort_overrides_config(self):
        # DashScope 的 disable-effort 令牌压过配置的“开”。
        assert resolve_thinking_active("dashscope", "qwen3.7-max", True, effort="off") is False
        assert resolve_thinking_active("dashscope", "qwen3.7-max", None, effort="none") is False
        # 常规 effort 不影响默认开。
        assert resolve_thinking_active("dashscope", "qwen3.7-max", None, effort="high") is True

    def test_empty_provider_or_model_is_off(self):
        assert resolve_thinking_active(None, None, None) is False
        assert resolve_thinking_active("", "", True) is False
