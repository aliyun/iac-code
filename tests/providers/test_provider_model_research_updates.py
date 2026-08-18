"""Tests for updates described by scripts/provider-update-guide.zh-CN.md."""

from __future__ import annotations

from iac_code.providers.kimi_provider import KimiProvider
from iac_code.providers.minimax_provider import MiniMaxProvider
from iac_code.providers.registry import PROVIDER_REGISTRY, ModelEntry
from iac_code.providers.thinking import EffortLevel, ThinkingFamily, get_thinking_spec
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
        "qwen3.8-max",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.7-flash",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "qwen-plus",
        "qwen-flash",
        "deepseek-v4-pro",
        "deepseek-v4-pro-0813",
        "deepseek-v4-flash-0731",
        "deepseek-v4-flash",
        "kimi/kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "kimi-k2.5",
        "glm-5.2-fast-preview",
        "glm-5.2",
        "glm-5.1",
        "MiniMax/MiniMax-M3",
        "MiniMax-M2.5",
    ):
        assert model_id in models

    # Soon-offline models are still callable until removed upstream.
    for model_id in ("qwen3.6-max-preview", "qwen3-max", "qwen3-coder-plus", "qwen3-coder-next", "qwq-plus"):
        assert model_id in models
        assert not _model_entry("dashscope", model_id).is_default

    assert PROVIDER_REGISTRY["dashscope"].default_model == "qwen3.8-max"
    assert not _model_entry("dashscope", "glm-5.2-fast-preview").support_multimodal
    assert "glm-5.2-fast-preview" not in _model_ids("dashscope_token_plan")
    assert _model_entry("dashscope", "qwen3.8-max").support_multimodal
    assert not _model_entry("dashscope", "qwen3.7-max").support_multimodal
    for model_id in (
        "qwen3.7-plus",
        "qwen3.7-flash",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "kimi-k2.7-code",
        "MiniMax/MiniMax-M3",
    ):
        assert _model_entry("dashscope", model_id).support_multimodal
    assert not _model_entry("dashscope", "deepseek-v4-pro-0813").support_multimodal
    # The public adapter can only send local attachments as data URLs, but
    # Moonshot-hosted K3 on DashScope accepts public image URLs only.
    assert not _model_entry("dashscope", "kimi/kimi-k3").support_multimodal


def test_dashscope_token_plan_uses_exact_supported_chat_models() -> None:
    models = _model_ids("dashscope_token_plan")

    for model_id in (
        "qwen3.8-max",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "deepseek-v4-pro",
        "deepseek-v4-pro-0813",
        "deepseek-v4-flash-0731",
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
    # qwen3.8-max-preview ended its preview and went offline; the legacy ID is
    # now routed to qwen3.8-max server-side, so it leaves the selectable list.
    assert "qwen3.8-max-preview" not in models
    assert PROVIDER_REGISTRY["dashscope_token_plan"].default_model == "qwen3.8-max"
    assert _model_entry("dashscope_token_plan", "qwen3.8-max").support_multimodal
    assert not _model_entry("dashscope_token_plan", "qwen3.7-max").support_multimodal
    assert not _model_entry("dashscope_token_plan", "deepseek-v4-pro-0813").support_multimodal
    for model_id in ("qwen3.7-plus", "qwen3.6-plus", "qwen3.6-flash", "kimi-k2.7-code", "kimi-k2.5", "kimi-k2.6"):
        assert _model_entry("dashscope_token_plan", model_id).support_multimodal


def test_openai_azure_anthropic_and_gemini_models_are_updated() -> None:
    for model_id in (
        "gpt-5.6-sol",
        "gpt-5.6",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.3-codex",
        "gpt-5.2",
    ):
        assert model_id in _model_ids("openai")
        assert get_thinking_spec("openai", model_id).family is ThinkingFamily.OPENAI

    for responses_only_model in ("gpt-5.5-pro", "gpt-5.4-pro", "gpt-5.2-pro"):
        assert responses_only_model not in _model_ids("openai")
        assert get_thinking_spec("openai", responses_only_model).family is ThinkingFamily.NONE

    assert PROVIDER_REGISTRY["openai"].default_model == "gpt-5.6-sol"

    assert _model_ids("azure_openai") == []
    assert PROVIDER_REGISTRY["azure_openai"].default_model == ""

    assert PROVIDER_REGISTRY["anthropic"].default_model == "claude-fable-5"
    assert get_thinking_spec("anthropic", "claude-fable-5").family is ThinkingFamily.ANTHROPIC_ADAPTIVE
    assert "claude-opus-5" in _model_ids("anthropic")
    assert _model_entry("anthropic", "claude-opus-5").support_multimodal
    assert get_thinking_spec("anthropic", "claude-opus-5").family is ThinkingFamily.ANTHROPIC_ADAPTIVE
    assert get_thinking_spec("anthropic", "claude-opus-4-8").family is ThinkingFamily.ANTHROPIC_ADAPTIVE
    assert get_thinking_spec("anthropic", "claude-sonnet-5").family is ThinkingFamily.ANTHROPIC_ADAPTIVE

    for model_id in (
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro-preview-customtools",
    ):
        assert model_id in _model_ids("gemini")
        assert _model_entry("gemini", model_id).support_multimodal
        assert get_thinking_spec("gemini", model_id).family is ThinkingFamily.GEMINI
    assert PROVIDER_REGISTRY["gemini"].default_model == "gemini-3.7-flash"
    assert "gemini-3.1-flash-lite-preview" not in _model_ids("gemini")
    assert "gemini-2.0-flash" not in _model_ids("gemini")
    assert get_thinking_spec("gemini", "gemini-2.5-flash-lite").family is ThinkingFamily.GEMINI


def test_direct_kimi_minimax_and_zhipu_models_are_updated() -> None:
    for provider_key in ("kimi_cn", "kimi_intl"):
        assert PROVIDER_REGISTRY[provider_key].default_model == "kimi-k3"
        assert "kimi-k3" in _model_ids(provider_key)
        assert "kimi-k2.7-code" in _model_ids(provider_key)
        assert "kimi-k2.7-code-highspeed" in _model_ids(provider_key)
        assert _model_entry(provider_key, "kimi-k3").support_multimodal
        assert _model_entry(provider_key, "kimi-k2.7-code").support_multimodal
        assert get_thinking_spec(provider_key, "kimi-k3").family is ThinkingFamily.KIMI
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
        # GLM-5.3 is Coding-Plan-only for now; the standard model API is not
        # live yet, so it must not appear on the direct endpoints.
        assert "glm-5.3" not in _model_ids(provider_key)
        assert get_thinking_spec(provider_key, "glm-5.3").family is ThinkingFamily.NONE

    for provider_key in ("zhipu_cn_codingplan", "zhipu_intl_codingplan"):
        assert "glm-5.3" in _model_ids(provider_key)
        assert "glm-5.2" in _model_ids(provider_key)
        assert PROVIDER_REGISTRY[provider_key].default_model == "glm-5.3"
        assert get_thinking_spec(provider_key, "glm-5.3").family is ThinkingFamily.ZHIPU
        assert get_thinking_spec(provider_key, "glm-5.2").family is ThinkingFamily.ZHIPU


def test_provider_specific_thinking_wire_formats_do_not_use_openai_or_anthropic_effort_fields() -> None:
    kimi = KimiProvider(model="kimi-k2.6", api_key="k", effort="high")
    assert kimi._build_thinking_kwargs() == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert KimiProvider(model="kimi-k2.7-code", api_key="k", effort="high")._build_thinking_kwargs() == {}
    assert KimiProvider(model="kimi-k2.7-code-highspeed", api_key="k", effort="high")._build_thinking_kwargs() == {}
    assert KimiProvider(model="kimi-k2.7-code", api_key="k", thinking_enabled=False)._build_thinking_kwargs() == {}
    assert (
        KimiProvider(model="kimi-k2.7-code-highspeed", api_key="k", thinking_enabled=False)._build_thinking_kwargs()
        == {}
    )
    assert KimiProvider(model="kimi-k3", api_key="k", effort="high")._build_thinking_kwargs() == {
        "reasoning_effort": "high"
    }

    zhipu = ZhiPuProvider(model="glm-5.1", api_key="k", effort="high")
    assert zhipu._build_thinking_kwargs() == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert ZhiPuProvider(model="glm-5.2", api_key="k", effort="high")._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }
    assert ZhiPuProvider(model="glm-5.2", api_key="k", effort="max")._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "max",
    }
    # GLM-5.3 (Coding Plan) is always-on: it accepts low/high/max and rejects
    # thinking.type=disabled, so a disabled toggle degrades to enabled + low.
    assert ZhiPuProvider(
        model="glm-5.3", api_key="k", provider_key="zhipu_cn_codingplan", effort="max"
    )._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "max",
    }
    assert ZhiPuProvider(
        model="glm-5.3", api_key="k", provider_key="zhipu_cn_codingplan", thinking_enabled=False
    )._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "low",
    }
    assert ZhiPuProvider(
        model="glm-5.3", api_key="k", provider_key="zhipu_intl_codingplan"
    )._build_thinking_kwargs() == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert ZhiPuProvider(model="glm-5.2", api_key="k")._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "enabled"}}
    }
    assert ZhiPuProvider(model="glm-5.2", api_key="k", thinking_enabled=False)._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }

    minimax = MiniMaxProvider(model="MiniMax-M3", api_key="k", effort="high")
    assert minimax._build_thinking_kwargs() == {"thinking": {"type": "adaptive"}}

    for kwargs in (kimi._build_thinking_kwargs(), zhipu._build_thinking_kwargs(), minimax._build_thinking_kwargs()):
        assert "reasoning_effort" not in kwargs
        assert kwargs.get("thinking", {}).get("budget_tokens") is None


def test_provider_specific_thinking_enabled_false_disables_native_wire_formats() -> None:
    assert KimiProvider(model="kimi-k2.6", api_key="k", thinking_enabled=False)._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert ZhiPuProvider(model="glm-5.1", api_key="k", thinking_enabled=False)._build_thinking_kwargs() == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert MiniMaxProvider(model="MiniMax-M3", api_key="k", thinking_enabled=False)._build_thinking_kwargs() == {
        "thinking": {"type": "disabled"}
    }


def test_gemini_thinking_rules_match_each_model_generation() -> None:
    from iac_code.providers.gemini_provider import GeminiProvider

    assert GeminiProvider(model="gemini-2.5-pro", api_key="k", thinking_enabled=True)._build_thinking_kwargs() == {
        "reasoning_effort": "high"
    }
    assert (
        GeminiProvider(
            model="gemini-2.5-pro",
            api_key="k",
            effort="high",
            thinking_enabled=False,
        )._build_thinking_kwargs()
        == {}
    )
    assert GeminiProvider(model="gemini-2.5-flash", api_key="k", thinking_enabled=False)._build_thinking_kwargs() == {
        "reasoning_effort": "none"
    }
    assert GeminiProvider(model="gemini-2.5-flash", api_key="k", effort="minimal")._build_thinking_kwargs() == {
        "reasoning_effort": "minimal"
    }
    assert GeminiProvider(
        model="gemini-3.1-flash-lite",
        api_key="k",
        thinking_enabled=True,
    )._build_thinking_kwargs() == {"reasoning_effort": "minimal"}
    assert GeminiProvider(model="gemini-3.6-flash", api_key="k", effort="low")._build_thinking_kwargs() == {
        "reasoning_effort": "low"
    }
    assert GeminiProvider(model="gemini-3.5-flash-lite", api_key="k", effort="low")._build_thinking_kwargs() == {
        "reasoning_effort": "low"
    }
    # Gemini 3.7 Flash documents low/medium/high; minimal returns an error
    # server-side, so the adapter must fall back to the default instead.
    assert GeminiProvider(model="gemini-3.7-flash", api_key="k", effort="low")._build_thinking_kwargs() == {
        "reasoning_effort": "low"
    }
    assert GeminiProvider(model="gemini-3.7-flash", api_key="k", effort="minimal")._build_thinking_kwargs() == {
        "reasoning_effort": "medium"
    }
    assert GeminiProvider(model="gemini-3.7-flash", api_key="k", thinking_enabled=False)._build_thinking_kwargs() == {}

    flash_lite = get_thinking_spec("gemini", "gemini-3.1-flash-lite")
    assert flash_lite.default_effort is EffortLevel.MINIMAL
    assert flash_lite.supports_disable is False

    gemini_37 = get_thinking_spec("gemini", "gemini-3.7-flash")
    assert gemini_37.allowed_efforts == (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.HIGH)
    assert gemini_37.default_effort is EffortLevel.MEDIUM
    assert gemini_37.supports_disable is False


def test_anthropic_effort_and_disable_rules_are_model_specific() -> None:
    from iac_code.providers.anthropic_provider import AnthropicProvider

    opus_5 = get_thinking_spec("anthropic", "claude-opus-5")
    assert opus_5.thinking_enabled_by_default is True
    assert opus_5.default_effort is EffortLevel.HIGH
    assert opus_5.disable_forbidden_efforts == (EffortLevel.XHIGH, EffortLevel.MAX)

    sonnet_5 = get_thinking_spec("anthropic", "claude-sonnet-5")
    assert sonnet_5.adaptive_always_on is False
    assert AnthropicProvider(
        model="claude-sonnet-5",
        api_key="k",
        thinking_enabled=False,
    )._build_thinking_kwargs() == {"thinking": {"type": "disabled"}}

    opus_46 = get_thinking_spec("anthropic", "claude-opus-4-6")
    assert EffortLevel.XHIGH not in opus_46.allowed_efforts
    assert EffortLevel.MAX in opus_46.allowed_efforts
    assert opus_46.supports_thinking_budget is True


def test_dashscope_new_model_protocols_are_not_flattened() -> None:
    from iac_code.providers.dashscope_provider import DashScopeProvider

    deepseek_0813 = get_thinking_spec("dashscope", "deepseek-v4-pro-0813")
    assert deepseek_0813.allowed_efforts == (
        EffortLevel.LOW,
        EffortLevel.MEDIUM,
        EffortLevel.HIGH,
        EffortLevel.XHIGH,
        EffortLevel.MAX,
    )
    assert deepseek_0813.default_effort is EffortLevel.HIGH
    assert deepseek_0813.uses_reasoning_effort_param is True
    assert DashScopeProvider(
        model="deepseek-v4-pro-0813",
        api_key="k",
        thinking_enabled=False,
    )._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}

    deepseek = get_thinking_spec("dashscope", "deepseek-v4-flash-0731")
    assert deepseek.allowed_efforts == (
        EffortLevel.LOW,
        EffortLevel.MEDIUM,
        EffortLevel.HIGH,
        EffortLevel.XHIGH,
        EffortLevel.MAX,
    )
    assert deepseek.default_effort is EffortLevel.HIGH
    assert deepseek.uses_reasoning_effort_param is True

    qwen = get_thinking_spec("dashscope", "qwen3.8-max")
    assert qwen.allowed_efforts == (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.XHIGH)
    assert qwen.default_effort is EffortLevel.XHIGH
    assert qwen.supports_disable is True
    assert DashScopeProvider(
        model="qwen3.8-max",
        api_key="k",
        thinking_enabled=False,
    )._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}
    assert DashScopeProvider(
        model="qwen3.8-max",
        api_key="k",
        thinking_enabled=True,
    )._build_thinking_kwargs() == {
        "extra_body": {"enable_thinking": True, "preserve_thinking": True},
        "reasoning_effort": "xhigh",
    }

    preview = get_thinking_spec("dashscope_token_plan", "qwen3.8-max-preview")
    assert preview.allowed_efforts == (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.XHIGH)
    assert preview.default_effort is EffortLevel.XHIGH
    assert preview.supports_disable is False
    assert DashScopeProvider(
        model="qwen3.8-max-preview",
        api_key="k",
        provider_key="dashscope_token_plan",
        thinking_enabled=False,
    )._build_thinking_kwargs() == {"extra_body": {"preserve_thinking": True}}
    assert DashScopeProvider(
        model="qwen3.8-max-preview",
        api_key="k",
        provider_key="dashscope_token_plan",
        thinking_enabled=True,
    )._build_thinking_kwargs() == {
        "extra_body": {"preserve_thinking": True},
        "reasoning_effort": "xhigh",
    }

    assert DashScopeProvider(
        model="kimi/kimi-k3",
        api_key="k",
        thinking_enabled=True,
    )._build_thinking_kwargs() == {
        "extra_body": {"preserve_thinking": True},
        "reasoning_effort": "max",
    }
    assert DashScopeProvider(
        model="MiniMax/MiniMax-M3",
        api_key="k",
        thinking_enabled=False,
    )._build_thinking_kwargs() == {"extra_body": {"thinking": {"type": "disabled"}}}
