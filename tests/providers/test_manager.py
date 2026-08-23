import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
from anthropic import APIConnectionError as AnthropicAPIConnectionError
from anthropic import APITimeoutError as AnthropicAPITimeoutError
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import APITimeoutError as OpenAIAPITimeoutError

from iac_code.providers.base import Message, NonStreamingResponse
from iac_code.providers.manager import (
    _PROVIDER_MODEL_FALLBACK_MAP,
    MODEL_FALLBACK_MAP,
    ProviderManager,
    _detect_provider_name,
    _is_bailian_compatible_endpoint,
    _telemetry_provider_name,
    create_provider,
)
from iac_code.providers.registry import PROVIDER_REGISTRY
from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.services.telemetry.names import Events, GenAiAttr, IacCodeAttr, Metrics, PipelineAttr, Spans
from iac_code.services.telemetry.scope import use_span_attributes
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolUseStartEvent,
    Usage,
)

STREAM_IDLE_TEST_TIMEOUT = 0.2


async def _collect_stream_events(stream):
    return [event async for event in stream]


class TestCreateProvider:
    @pytest.mark.parametrize(
        "base_url",
        [
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope-intl.aliyuncs.com/apps/anthropic",
            "https://dashscope-us.aliyuncs.com/compatible-mode/v1/chat/completions",
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://llm-testworkspace000000.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
            "https://workspace-1.ap-southeast-1.maas.aliyuncs.com/apps/anthropic",
            "https://llm-tokyo.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1",
            "https://llm-frankfurt.eu-central-1.maas.aliyuncs.com/apps/anthropic/v1/messages",
            "https://llm-virginia.us-east-1.maas.aliyuncs.com/compatible-mode/v1",
            "https://trial.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://trial.ap-southeast-1.maas.aliyuncs.com/apps/anthropic",
            "https://coding.dashscope.aliyuncs.com/v1",
            "https://coding.dashscope.aliyuncs.com/apps/anthropic",
            "https://coding-intl.dashscope.aliyuncs.com/v1/chat/completions",
            "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic/v1/messages",
            "https://LLM-TESTWORKSPACE000000.CN-BEIJING.MAAS.ALIYUNCS.COM:443/apps/anthropic?version=1",
        ],
    )
    def test_bailian_compatible_endpoint_detection(self, base_url):
        assert _is_bailian_compatible_endpoint(base_url) is True

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://token-plan.cn-beijing.maas.aliyuncs.com:8443/compatible-mode/v1",
            "https://token-plan.cn-beijing.maas.aliyuncs.com.example/compatible-mode/v1",
            "https://example.com/token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://example.com/compatible-mode/v1?target=token-plan.cn-beijing.maas.aliyuncs.com",
            "https://llm-example.cn-hangzhou.maas.aliyuncs.com/compatible-mode/v1",
            "https://-invalid.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://llm-example.cn-beijing.maas.aliyuncs.com/v1",
            "https://llm-example.cn-beijing.maas.aliyuncs.com/api/v1",
            "https://dashscope-us.aliyuncs.com/api/v1",
            "https://dashscope-eu.aliyuncs.com/compatible-mode/v1",
            "https://coding.dashscope.aliyuncs.com/compatible-mode/v1",
            "https://coding.dashscope.aliyuncs.com.example/v1",
            "https://example.cn-beijing.pai-eas.aliyuncs.com/apps/anthropic",
        ],
    )
    def test_bailian_compatible_endpoint_detection_rejects_non_official_urls(self, base_url):
        assert _is_bailian_compatible_endpoint(base_url) is False

    @pytest.mark.parametrize("provider_key", PROVIDER_REGISTRY)
    def test_saved_api_base_overrides_registry_default_for_every_provider(self, provider_key):
        descriptor = PROVIDER_REGISTRY[provider_key]
        model = descriptor.default_model or "custom-model"
        custom_base_url = "https://saved.example/v1"

        provider = create_provider(
            model,
            credentials={provider_key: "fake-key"},
            provider_key_override=provider_key,
            provider_config_override={"apiBase": custom_base_url},
        )

        assert str(provider._client.base_url).rstrip("/") == custom_base_url

    @pytest.mark.parametrize("provider_key", PROVIDER_REGISTRY)
    def test_explicit_base_url_overrides_saved_and_registry_urls_for_every_provider(self, provider_key):
        descriptor = PROVIDER_REGISTRY[provider_key]
        model = descriptor.default_model or "custom-model"
        explicit_base_url = "https://explicit.example/v1"

        provider = create_provider(
            model,
            credentials={provider_key: "fake-key"},
            provider_key_override=provider_key,
            base_url=explicit_base_url,
            provider_config_override={"apiBase": "https://saved.example/v1"},
        )

        assert str(provider._client.base_url).rstrip("/") == explicit_base_url

    def test_anthropic(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        p = create_provider("claude-sonnet-4-6", credentials={"anthropic": "key"})
        assert p.get_model_name() == "claude-sonnet-4-6"

    def test_openai(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        p = create_provider("gpt-4.1", credentials={"openai": "key"})
        assert p.get_model_name() == "gpt-4.1"

    def test_dashscope(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        p = create_provider("qwen3.6-plus", credentials={"dashscope": "key"})
        assert p.get_model_name() == "qwen3.6-plus"
        assert getattr(p, "_effort", None) is None

    def test_dashscope_loads_effort_from_settings(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {"effort": "max"})
        p = create_provider("deepseek-v4-pro", credentials={"dashscope": "key"})
        assert getattr(p, "_effort", None) == "max"

    def test_model_config_overrides_provider_request_policy(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {
                "effort": "high",
                "thinkingEnabled": False,
                "thinkingBudget": 4096,
                "maxCompletionTokens": 12288,
                "models": {
                    "glm-5.2": {
                        "effort": "low",
                        "thinkingEnabled": True,
                        "thinkingBudget": 2048,
                        "maxCompletionTokens": 10000,
                    }
                },
            },
        )

        p = create_provider("glm-5.2", credentials={"dashscope": "key"})

        assert getattr(p, "_effort", None) == "low"
        assert getattr(p, "_thinking_enabled", None) is True
        assert getattr(p, "_thinking_budget", None) == 2048
        assert getattr(p, "_max_completion_tokens", None) == 10000

    def test_provider_config_override_prevents_runtime_settings_reread(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {
                "effort": "low",
                "thinkingEnabled": False,
                "thinkingBudget": 1024,
                "maxCompletionTokens": 2048,
            },
        )

        p = create_provider(
            "glm-5.2",
            credentials={"dashscope": "snapshot-key"},
            provider_config_override={
                "effort": "high",
                "thinkingEnabled": True,
                "thinkingBudget": 4096,
                "maxCompletionTokens": 12000,
            },
        )

        assert getattr(p, "_effort", None) == "high"
        assert getattr(p, "_thinking_enabled", None) is True
        assert getattr(p, "_thinking_budget", None) == 4096
        assert getattr(p, "_max_completion_tokens", None) == 12000

    def test_request_policy_override_wins_over_settings(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {
                "effort": "low",
                "thinkingBudget": 4096,
                "maxCompletionTokens": 12288,
            },
        )

        p = create_provider(
            "glm-5.2",
            credentials={"dashscope": "key"},
            request_policy_override=ProviderRequestPolicy(thinking_enabled=False, effort="high", thinking_budget=2048),
        )

        assert getattr(p, "_thinking_enabled", None) is False
        assert getattr(p, "_effort", None) == "high"
        assert getattr(p, "_thinking_budget", None) == 2048
        assert getattr(p, "_max_completion_tokens", None) == 12288

    def test_session_start_settings_include_performance_parameters(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})

        manager = ProviderManager(
            model="glm-5.2",
            credentials={"dashscope": "key"},
            stream_idle_timeout=12.5,
            request_policy_override=ProviderRequestPolicy(
                thinking_enabled=False,
                effort="high",
                thinking_budget=2048,
            ),
            provider_config_override={
                "effort": "low",
                "thinkingBudget": 4096,
                "maxCompletionTokens": 10000,
            },
        )

        assert manager.session_start_settings() == {
            "provider": "dashscope",
            "provider_display": "Alibaba Cloud Bailian",
            "model": "glm-5.2",
            "effort": "high",
            "thinking_enabled": False,
            "thinking_budget": 2048,
            "max_completion_tokens": 10000,
            "stream_idle_timeout": 12.5,
            "thinking_phase_timeout": 300.0,
            "endpoint_origin": "https://dashscope.aliyuncs.com",
            "endpoint_custom": False,
        }

    def test_session_start_settings_sanitize_custom_endpoint(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})

        manager = ProviderManager(
            model="glm-5.2",
            credentials={"dashscope": "key"},
            provider_config_override={
                "apiBase": "https://user:pass@llm.example.com:9443/v1?api_key=secret",
            },
        )

        settings = manager.session_start_settings()
        assert settings["endpoint_origin"] == "https://llm.example.com:9443"
        assert settings["endpoint_custom"] is True
        assert "user:pass" not in str(settings)
        assert "api_key=secret" not in str(settings)

    def test_claude_46_request_budget_reaches_manual_thinking_wire_format(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})

        p = create_provider(
            "claude-sonnet-4-6",
            credentials={"anthropic": "key"},
            request_policy_override=ProviderRequestPolicy(thinking_budget=2048),
        )

        assert p._build_thinking_kwargs() == {"thinking": {"type": "enabled", "budget_tokens": 2048}}

    def test_openai_compatible_qwen_request_policy_disabled_uses_model_thinking_wire_format(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai_compatible")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})

        p = create_provider(
            "qwen3.6-plus",
            credentials={"openai_compatible": "key"},
            request_policy_override=ProviderRequestPolicy(thinking_enabled=False),
        )

        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}

    def test_provider_request_policy_used_when_model_config_absent(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {
                "thinkingBudget": 2048,
                "maxCompletionTokens": 10000,
            },
        )

        p = create_provider("kimi-k2.7-code", credentials={"dashscope": "key"})

        assert getattr(p, "_thinking_budget", None) == 2048
        assert getattr(p, "_max_completion_tokens", None) == 10000

    def test_invalid_model_request_policy_config_falls_back_to_provider_config(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {
                "thinkingBudget": 2048,
                "maxCompletionTokens": 10000,
                "models": {
                    "glm-5.2": {
                        "thinkingBudget": "bad",
                        "maxCompletionTokens": 0,
                    }
                },
            },
        )

        p = create_provider("glm-5.2", credentials={"dashscope": "key"})

        assert getattr(p, "_thinking_budget", None) == 2048
        assert getattr(p, "_max_completion_tokens", None) == 10000

    def test_float_request_policy_config_is_rejected_not_truncated(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {
                "thinkingBudget": 2048.9,
                "maxCompletionTokens": "10000.5",
            },
        )

        p = create_provider("glm-5.2", credentials={"dashscope": "key"})

        assert getattr(p, "_thinking_budget", None) is None
        assert getattr(p, "_max_completion_tokens", None) is None

    def test_anthropic_honors_model_thinking_budget_and_max_completion_tokens(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {
                "thinkingBudget": 2048,
                "maxCompletionTokens": 10000,
                "models": {"claude-sonnet-4-6": {"thinkingBudget": 3072}},
            },
        )

        p = create_provider("claude-sonnet-4-6", credentials={"anthropic": "key"})

        assert p.get_model_name() == "claude-sonnet-4-6"
        assert p._build_thinking_kwargs() == {"thinking": {"type": "enabled", "budget_tokens": 3072}}
        assert getattr(p, "_max_output_tokens", None) == 10000

    def test_effort_override_takes_precedence_over_settings(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {"effort": "high"})
        p = create_provider("qwen3.7-max", credentials={"dashscope": "key"}, effort_override="none")
        assert getattr(p, "_effort", None) is None
        assert getattr(p, "_thinking_enabled", None) is False
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}

    def test_anthropic_legacy_none_disables_thinking_without_high_effort(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {"effort": "high"})

        p = create_provider("claude-sonnet-4-6", credentials={"anthropic": "key"}, effort_override="none")

        assert getattr(p, "_effort", None) is None
        assert p._build_thinking_kwargs() == {"thinking": {"type": "disabled"}}

    @pytest.mark.parametrize(
        "provider_config, request_policy",
        [
            ({"effort": "none"}, None),
            ({}, ProviderRequestPolicy(effort="none")),
        ],
    )
    def test_anthropic_legacy_none_from_final_policy_disables_thinking(
        self, monkeypatch, provider_config, request_policy
    ):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: provider_config)

        p = create_provider(
            "claude-sonnet-4-6",
            credentials={"anthropic": "key"},
            request_policy_override=request_policy,
        )

        assert getattr(p, "_effort", None) is None
        assert getattr(p, "_thinking_enabled", None) is False
        assert p._build_thinking_kwargs() == {"thinking": {"type": "disabled"}}

    def test_supported_none_effort_is_not_reinterpreted_as_legacy_disable(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {"effort": "none"})

        p = create_provider("gpt-5.6", credentials={"openai": "key"})

        assert getattr(p, "_effort", None) == "none"
        assert getattr(p, "_thinking_enabled", None) is None
        assert p._build_thinking_kwargs() == {"reasoning_effort": "none"}

    def test_unknown_raises(self, monkeypatch):
        """Unknown model with no saved provider config raises ValueError."""
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        with pytest.raises(ValueError, match="Cannot determine provider"):
            create_provider("unknown-model", credentials={})

    def test_openai_compatible(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai_compatible")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {"apiBase": "https://my.llm.local/v1"},
        )
        p = create_provider("any-model", credentials={"openai_compatible": "sk-x"})
        assert p.get_model_name() == "any-model"
        assert p._base_url == "https://my.llm.local/v1"

    def test_openai_compatible_bailian_llm_endpoint_changes_only_telemetry_attribution(self, monkeypatch):
        from iac_code.providers.openai_provider import OpenAIProvider

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai_compatible")
        base_url = "https://llm-testworkspace000000.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

        p = create_provider(
            "any-model",
            credentials={"openai_compatible": "sk-x"},
            provider_config_override={"apiBase": base_url},
        )

        assert type(p) is OpenAIProvider
        assert p._PROVIDER_KEY == "openai_compatible"
        assert p._logical_provider_key == "openai_compatible"
        assert _telemetry_provider_name(p) == "dashscope"

    def test_anthropic_compatible_bailian_endpoint_changes_only_telemetry_attribution(self, monkeypatch):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic_compatible")
        base_url = "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic"

        p = create_provider(
            "any-model",
            credentials={"anthropic_compatible": "sk-x"},
            provider_config_override={"apiBase": base_url},
        )

        assert type(p) is AnthropicProvider
        assert p._PROVIDER_KEY == "anthropic_compatible"
        assert p._logical_provider_key == "anthropic_compatible"
        assert _telemetry_provider_name(p) == "dashscope"

    def test_openai_compatible_dashscope_base_uses_dashscope_default_thinking_wire_format(self, monkeypatch):
        from iac_code.providers.dashscope_provider import DashScopeProvider

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai_compatible")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {"apiBase": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        )

        p = create_provider("glm-5.2", credentials={"openai_compatible": "sk-x"})

        assert isinstance(p, DashScopeProvider)
        assert getattr(p, "_PROVIDER_KEY", None) == "dashscope"
        assert getattr(p, "_logical_provider_key", None) == "openai_compatible"
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True}}

    def test_openai_compatible_dashscope_base_uses_dashscope_thinking_wire_format(self, monkeypatch):
        from iac_code.providers.dashscope_provider import DashScopeProvider

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai_compatible")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {"apiBase": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        )

        p = create_provider(
            "glm-5.2",
            credentials={"openai_compatible": "sk-x"},
            request_policy_override=ProviderRequestPolicy(thinking_enabled=False, effort="low", thinking_budget=2048),
        )

        assert isinstance(p, DashScopeProvider)
        assert getattr(p, "_PROVIDER_KEY", None) == "dashscope"
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}

    def test_dashscope_token_plan(self, monkeypatch):
        from iac_code.providers.dashscope_provider import (
            DASHSCOPE_TOKEN_PLAN_BASE_URL,
            DashScopeProvider,
        )

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope_token_plan")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        p = create_provider("qwen3.6-plus", credentials={"dashscope_token_plan": "tp-key"})
        assert isinstance(p, DashScopeProvider)
        assert p.get_model_name() == "qwen3.6-plus"
        assert p._base_url == DASHSCOPE_TOKEN_PLAN_BASE_URL
        assert p._PROVIDER_KEY == "dashscope_token_plan"
        assert getattr(p, "_effort", None) is None

    def test_dashscope_token_plan_uses_token_plan_credential_slot(self, monkeypatch):
        # The dashscope (regular) credential must NOT leak into the token plan
        # provider — only credentials["dashscope_token_plan"] is consumed.
        from iac_code.providers.dashscope_provider import DashScopeProvider

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope_token_plan")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        p = create_provider(
            "qwen3.6-plus",
            credentials={"dashscope": "regular-key", "dashscope_token_plan": "tp-key"},
        )
        assert isinstance(p, DashScopeProvider)
        assert p._client.api_key == "tp-key"


class TestProviderManager:
    def test_get_fallback(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-opus-4-7", credentials={})
        assert m._get_fallback_model() == "claude-haiku-4-5-20251001"

    def test_fable_fallback_uses_approved_opus_target(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        manager = ProviderManager(model="claude-fable-5", credentials={})

        assert manager._get_fallback_model() == "claude-opus-4-8"

    def test_opus5_falls_back_to_opus48_for_errors_and_refusals(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        manager = ProviderManager(model="claude-opus-5", credentials={})

        assert manager._get_fallback_model() == "claude-opus-4-8"
        assert manager._get_refusal_fallback_model("claude-opus-5", "anthropic") == "claude-opus-4-8"

    def test_no_fallback_cheapest(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-haiku-4-5-20251001", credentials={})
        assert m._get_fallback_model() is None

    def test_deferred_init_when_no_active_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={})
        assert m._provider is None

    def test_ensure_provider_raises_when_still_unconfigured(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={})
        with pytest.raises(ValueError, match="Cannot determine provider"):
            m._ensure_provider()

    def test_ensure_provider_lazy_success(self, monkeypatch):
        # First call: no provider configured, model name not auto-mappable
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={"anthropic": "k"})
        assert m._provider is None
        # Second call: user configured provider via /auth
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        provider = m._ensure_provider()
        assert provider.get_model_name() == "custom-model"

    def test_unknown_model_no_fallback(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="some-model-without-fallback", credentials={})
        assert m._get_fallback_model() is None

    @pytest.mark.parametrize("provider_key", ["azure_openai", "openai_compatible", "ollama"])
    def test_custom_model_catalogs_do_not_use_public_model_fallbacks(self, provider_key):
        manager = ProviderManager.__new__(ProviderManager)
        manager._model = "gpt-5.6-sol"

        assert manager._get_fallback_model(provider_key=provider_key) is None

    def test_provider_key_and_display_use_runtime_override(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        m = ProviderManager(
            model="qwen3.6-plus",
            credentials={"dashscope_token_plan": "tp-key"},
            provider_key_override="dashscope_token_plan",
        )

        assert m.get_provider_key() == "dashscope_token_plan"
        assert m.get_provider_display() == "Alibaba Cloud Bailian Token Plan"

    def test_effort_override_is_passed_to_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {"effort": "high"})
        m = ProviderManager(
            model="qwen3.7-max",
            credentials={"dashscope": "key"},
            effort_override="none",
        )

        assert m._provider is not None
        assert getattr(m._provider, "_effort", None) is None
        assert getattr(m._provider, "_thinking_enabled", None) is False

    def test_reconfigure_swaps_model_and_credentials(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "old"})
        original_provider = m._provider
        assert original_provider is not None

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        m.reconfigure("gpt-5.5", {"openai": "new"})

        assert m.get_model_name() == "gpt-5.5"
        assert m._credentials == {"openai": "new"}
        # Underlying provider was rebuilt — different instance from before.
        assert m._provider is not None
        assert m._provider is not original_provider
        assert m._provider.get_model_name() == "gpt-5.5"

    def test_reconfigure_recovers_from_unconfigured(self, monkeypatch):
        # Start with no active provider — manager defers provider init.
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={})
        assert m._provider is None

        # User runs /auth — reconfigure should now build the provider.
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m.reconfigure("claude-sonnet-4-6", {"anthropic": "k"})
        assert m._provider is not None

    def test_reconfigure_stays_lazy_when_no_provider_configured(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        assert m._provider is not None

        # Reconfigure with no active provider key → underlying provider drops
        # to None and stays None until the user configures something.
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m.reconfigure("some-model", {})
        assert m._provider is None
        assert m.get_model_name() == "some-model"

    def test_check_qwenpaw_config_change_reconfigures_by_default(self, monkeypatch):
        """CLI/REPL hot-reload: a QwenPaw active_model change is picked up mid-session."""
        from iac_code.services.qwenpaw_source import QwenPawConfig

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config._get_env_overrides",
            lambda: {"provider_key": None, "model": None, "api_base": None, "api_key": None},
        )
        monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr(
            "iac_code.services.qwenpaw_source.load_from_qwenpaw",
            lambda: QwenPawConfig(
                model="qwen-max",
                provider_key="dashscope",
                api_key="fake-qwenpaw-key",
                base_url="https://qwenpaw.invalid/v1",
            ),
        )

        m = ProviderManager(
            model="qwen3.7-plus",
            credentials={"dashscope": "k"},
            provider_key_override="dashscope",
        )
        m._check_qwenpaw_config_change()

        # Default behaviour: QwenPaw's active model wins (hot-reload).
        assert m.get_model_name() == "qwen-max"
        assert m._base_url_override == "https://qwenpaw.invalid/v1"

    def test_ignore_llm_source_keeps_session_provider(self, monkeypatch):
        """A session-level provider override must survive the per-request QwenPaw check.

        Web bug: the user activates QwenPaw (global ``llm_source=qwenpaw``), it fails, then
        switches the session to a regular provider. ``agent_factory`` builds the manager with the
        session's provider/model, but ``stream()`` calls ``_check_qwenpaw_config_change`` on every
        request, which reconfigured back to QwenPaw's (broken) endpoint — so switching provider
        appeared to do nothing. With ``ignore_llm_source`` set the session's choice stays put.
        """
        from iac_code.services.qwenpaw_source import QwenPawConfig

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.config._get_env_overrides",
            lambda: {"provider_key": None, "model": None, "api_base": None, "api_key": None},
        )
        monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr(
            "iac_code.services.qwenpaw_source.load_from_qwenpaw",
            lambda: QwenPawConfig(
                model="qwen-max",
                provider_key="dashscope",
                api_key="fake-qwenpaw-key",
                base_url="https://qwenpaw.invalid/v1",
            ),
        )

        m = ProviderManager(
            model="qwen3.7-plus",
            credentials={"dashscope": "k"},
            provider_key_override="dashscope",
            ignore_llm_source=True,
        )
        m._check_qwenpaw_config_change()

        # Session choice is immune to the global QwenPaw hot-reload.
        assert m.get_model_name() == "qwen3.7-plus"
        assert m._base_url_override is None

    def test_failure_telemetry_uses_public_error_summary(self, monkeypatch):
        telemetry_events = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr("iac_code.providers.manager.add_metric", lambda *args, **kwargs: None)

        ProviderManager._emit_failure_telemetry(
            "dashscope",
            "qwen3.6-plus",
            0.0,
            RuntimeError("Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml"),
        )

        _, attrs = next(item for item in telemetry_events if item[0] == Events.API_REQUEST_FAILED)
        assert attrs["error_id"]
        assert "sk-live" not in attrs["error_message"]
        assert "/Users/alice" not in attrs["error_message"]
        assert "[REDACTED]" in attrs["error_message"]


@pytest.mark.asyncio
class TestProviderManagerStreaming:
    @pytest.fixture(autouse=True)
    def _active_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")

    async def test_stream_success(self):
        mock_provider = AsyncMock()

        async def fake_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="hello")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        mock_provider.get_model_name.return_value = "test"
        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        types = [e.type for e in events]
        assert "message_start" in types and "text_delta" in types and "message_end" in types

    async def test_stream_records_normalized_token_usage_on_all_signals(self):
        mock_provider = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="answer")
            yield MessageEndEvent(
                stop_reason="end_turn",
                usage=Usage(
                    input_tokens=30,
                    output_tokens=20,
                    cache_read_input_tokens=60,
                    cache_creation_input_tokens=10,
                    input_tokens_include_cache=False,
                    reported=True,
                ),
            )

        mock_provider.stream = fake_stream
        span = MagicMock()
        telemetry_events = []
        metrics = []
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider
        scope = {
            IacCodeAttr.MODE: "pipeline",
            PipelineAttr.NAME: "selling",
            PipelineAttr.RUN_ID: "run-high-cardinality",
            PipelineAttr.STEP_ID: "intent_parsing",
        }

        with (
            use_span_attributes(scope),
            patch("iac_code.providers.manager.start_detached_span", return_value=span),
            patch("iac_code.providers.manager.get_session_id", return_value="iac_sess_1"),
            patch(
                "iac_code.providers.manager.log_event",
                side_effect=lambda name, attrs: telemetry_events.append((name, attrs)),
            ),
            patch(
                "iac_code.providers.manager.add_metric",
                side_effect=lambda name, value, attrs: metrics.append((name, value, attrs)),
            ),
        ):
            await _collect_stream_events(manager.stream(messages=[Message.user("hi")], system="sys"))

        span_attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
        assert span_attrs[GenAiAttr.USAGE_REPORTED] is True
        assert span_attrs[GenAiAttr.USAGE_INPUT_TOKENS] == 30
        assert span_attrs[GenAiAttr.USAGE_TOTAL_INPUT_TOKENS] == 100
        assert span_attrs[GenAiAttr.USAGE_STANDARD_INPUT_TOKENS] == 30
        assert span_attrs[GenAiAttr.USAGE_OUTPUT_TOKENS] == 20
        assert span_attrs[GenAiAttr.USAGE_TOTAL_TOKENS] == 120
        assert span_attrs[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS] == 60
        assert span_attrs[GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS] == 10
        assert span_attrs[GenAiAttr.USAGE_CACHE_HIT_RATE] == 0.6

        success = next(attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED)
        assert success["usage_reported"] is True
        assert success["input_tokens"] == 30
        assert success["total_input_tokens"] == 100
        assert success["standard_input_tokens"] == 30
        assert success["output_tokens"] == 20
        assert success["total_tokens"] == 120
        assert success["cache_read_tokens"] == 60
        assert success["cache_create_tokens"] == 10
        assert success["cache_hit_rate"] == 0.6
        assert success[PipelineAttr.RUN_ID] == "run-high-cardinality"

        token_metrics = {(attrs["type"], value): attrs for name, value, attrs in metrics if name == Metrics.TOKEN_USAGE}
        assert set(token_metrics) == {
            ("input", 30),
            ("output", 20),
            ("cache_read", 60),
            ("cache_create", 10),
        }
        assert all(attrs[IacCodeAttr.MODE] == "pipeline" for attrs in token_metrics.values())
        assert all(attrs[PipelineAttr.STEP_ID] == "intent_parsing" for attrs in token_metrics.values())
        assert all(PipelineAttr.RUN_ID not in attrs for attrs in token_metrics.values())
        total_metric = next((value, attrs) for name, value, attrs in metrics if name == Metrics.TOKEN_TOTAL)
        assert total_metric[0] == 120
        assert total_metric[1][IacCodeAttr.MODE] == "pipeline"

    async def test_stream_attributes_bailian_anthropic_endpoint_to_dashscope_on_all_signals(self):
        class AnthropicCompatibleProvider:
            _base_url = "https://llm-testworkspace000000.cn-beijing.maas.aliyuncs.com/apps/anthropic"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="m1")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=3, output_tokens=2))

        provider = AnthropicCompatibleProvider()
        span = MagicMock()
        telemetry_events = []
        metrics = []
        manager = ProviderManager(model="any-model", credentials={"anthropic": "k"})
        manager._provider = provider

        with (
            patch("iac_code.providers.manager.start_detached_span", return_value=span) as start_span,
            patch(
                "iac_code.providers.manager.log_event",
                side_effect=lambda name, attrs: telemetry_events.append((name, attrs)),
            ),
            patch(
                "iac_code.providers.manager.add_metric",
                side_effect=lambda name, value, attrs: metrics.append((name, value, attrs)),
            ),
        ):
            await _collect_stream_events(manager.stream(messages=[Message.user("hi")], system="sys"))

        assert start_span.call_args.args[1][GenAiAttr.PROVIDER_NAME] == "dashscope"
        provider_events = [
            attrs["provider"]
            for name, attrs in telemetry_events
            if name in {Events.API_REQUEST_STARTED, Events.API_REQUEST_SUCCEEDED}
        ]
        assert provider_events == ["dashscope", "dashscope"]
        token_metrics = [attrs["provider"] for name, _value, attrs in metrics if name == Metrics.TOKEN_TOTAL]
        assert token_metrics == ["dashscope"]
        assert provider._base_url.endswith("/apps/anthropic")

    async def test_stream_records_zero_cache_breakdown_on_span(self):
        mock_provider = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield MessageStartEvent(message_id="m1")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=2))

        mock_provider.stream = fake_stream
        span = MagicMock()
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider

        with (
            patch("iac_code.providers.manager.start_detached_span", return_value=span),
            patch("iac_code.providers.manager.get_session_id", return_value="iac_sess_1"),
        ):
            await _collect_stream_events(manager.stream(messages=[Message.user("hi")], system="sys"))

        span_attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
        assert span_attrs[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS] == 0
        assert span_attrs[GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS] == 0
        assert span_attrs[GenAiAttr.USAGE_CACHE_HIT_RATE] == 0.0

    async def test_stream_marks_missing_usage_without_emitting_zero_token_metrics(self):
        mock_provider = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield MessageStartEvent(message_id="m1")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        span = MagicMock()
        telemetry_events = []
        metrics = []
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider

        with (
            patch("iac_code.providers.manager.start_detached_span", return_value=span),
            patch("iac_code.providers.manager.get_session_id", return_value="iac_sess_1"),
            patch(
                "iac_code.providers.manager.log_event",
                side_effect=lambda name, attrs: telemetry_events.append((name, attrs)),
            ),
            patch(
                "iac_code.providers.manager.add_metric",
                side_effect=lambda name, value, attrs: metrics.append((name, value, attrs)),
            ),
        ):
            await _collect_stream_events(manager.stream(messages=[Message.user("hi")], system="sys"))

        span_attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
        assert span_attrs[GenAiAttr.USAGE_REPORTED] is False
        assert GenAiAttr.USAGE_INPUT_TOKENS not in span_attrs
        assert GenAiAttr.USAGE_CACHE_HIT_RATE not in span_attrs
        success = next(attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED)
        assert success["usage_reported"] is False
        assert "total_tokens" not in success
        usage_report_metrics = [
            (value, attrs) for name, value, attrs in metrics if name == Metrics.TOKEN_USAGE_REPORT_COUNT
        ]
        assert len(usage_report_metrics) == 1
        assert usage_report_metrics[0][0] == 1
        assert usage_report_metrics[0][1]["reported"] is False
        assert not any(name == Metrics.TOKEN_USAGE for name, _value, _attrs in metrics)
        assert not any(name == Metrics.TOKEN_TOTAL for name, _value, _attrs in metrics)

    async def test_stream_distinguishes_reported_zero_usage_from_missing_usage(self):
        mock_provider = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield MessageStartEvent(message_id="m1")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage(reported=True))

        mock_provider.stream = fake_stream
        metrics = []
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider

        with (
            patch("iac_code.providers.manager.start_detached_span", return_value=MagicMock()),
            patch("iac_code.providers.manager.get_session_id", return_value="iac_sess_1"),
            patch(
                "iac_code.providers.manager.add_metric",
                side_effect=lambda name, value, attrs: metrics.append((name, value, attrs)),
            ),
        ):
            await _collect_stream_events(manager.stream(messages=[Message.user("hi")], system="sys"))

        usage_report_metrics = [
            (value, attrs) for name, value, attrs in metrics if name == Metrics.TOKEN_USAGE_REPORT_COUNT
        ]
        assert len(usage_report_metrics) == 1
        assert usage_report_metrics[0][0] == 1
        assert usage_report_metrics[0][1]["reported"] is True
        assert not any(name == Metrics.TOKEN_USAGE for name, _value, _attrs in metrics)
        assert not any(name == Metrics.TOKEN_TOTAL for name, _value, _attrs in metrics)

    async def test_stream_records_first_non_empty_thinking_delta_and_pipeline_scope(self):
        mock_provider = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield MessageStartEvent(message_id="m1")
            yield ThinkingDeltaEvent(text="", provider_metadata={"signature": "opaque"})
            yield ThinkingDeltaEvent(text="reasoning")
            yield TextDeltaEvent(text="answer")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        span = MagicMock()
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider
        scope = {
            IacCodeAttr.MODE: "pipeline",
            PipelineAttr.NAME: "selling",
            PipelineAttr.STEP_ID: "intent_parsing",
        }

        with (
            use_span_attributes(scope),
            patch("iac_code.providers.manager.start_detached_span", return_value=span) as start_span,
            patch("iac_code.providers.manager.get_session_id", return_value="iac_sess_1"),
            patch("iac_code.providers.manager.log_event") as log_event,
        ):
            events = [event async for event in manager.stream(messages=[Message.user("hi")], system="sys")]

        assert [event.type for event in events] == [
            "message_start",
            "thinking_delta",
            "thinking_delta",
            "text_delta",
            "message_end",
        ]
        assert start_span.call_args.args[0] == f"{Spans.LLM_CHAT} claude-sonnet-4-6"
        initial_attrs = start_span.call_args.args[1]
        assert initial_attrs[GenAiAttr.SESSION_ID] == "iac_sess_1"
        assert initial_attrs[GenAiAttr.CONVERSATION_ID] == "iac_sess_1"
        assert initial_attrs[IacCodeAttr.MODE] == "pipeline"
        assert initial_attrs[PipelineAttr.NAME] == "selling"
        assert initial_attrs[PipelineAttr.STEP_ID] == "intent_parsing"
        ttft_calls = [
            call for call in span.set_attribute.call_args_list if call.args[0] == GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN
        ]
        assert len(ttft_calls) == 1
        assert ttft_calls[0].args[1] >= 0
        first_token_calls = [
            call for call in log_event.call_args_list if call.args[0] == Events.API_RESPONSE_FIRST_TOKEN
        ]
        assert len(first_token_calls) == 1
        assert first_token_calls[0].args[1] == {
            "provider": "asyncmock",
            "model": "claude-sonnet-4-6",
            GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN: ttft_calls[0].args[1],
            "first_token_source": "thinking_delta",
            **scope,
        }

    async def test_fable_accepted_stream_preserves_event_order(self, monkeypatch):
        expected = [
            MessageStartEvent(message_id="fable-accepted"),
            TextDeltaEvent(text="accepted response"),
            MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=2, output_tokens=3)),
        ]

        class FableProvider:
            _PROVIDER_KEY = "anthropic"
            _logical_provider_key = "anthropic"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                for event in expected:
                    yield event

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise AssertionError("accepted Fable stream must not use fallback completion")

        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *args, **kwargs: FableProvider())
        manager = ProviderManager(model="claude-fable-5", credentials={"anthropic": "k"})

        events = await _collect_stream_events(manager.stream(messages=[Message.user("hi")], system="sys"))

        assert events == expected
        assert manager.get_model_name() == "claude-fable-5"

    async def test_fable_stream_refusal_discards_partial_output_and_falls_back_to_opus(self, monkeypatch):
        class FableProvider:
            _PROVIDER_KEY = "anthropic"
            _logical_provider_key = "anthropic"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="fable-refusal")
                yield TextDeltaEvent(text="incomplete refusal text")
                yield MessageEndEvent(stop_reason="refusal", usage=Usage(input_tokens=3, output_tokens=4))

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise AssertionError("stream refusal must fall back without retrying Fable")

        class OpusProvider:
            _PROVIDER_KEY = "anthropic"
            _logical_provider_key = "anthropic"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="opus-follow-up")
                yield TextDeltaEvent(text="continued with opus")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=2, output_tokens=3))

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="opus-fallback",
                    text="complete answer",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=5, output_tokens=6),
                )

        created_models: list[str] = []

        def fake_create_provider(model, credentials, **kwargs):
            created_models.append(model)
            return FableProvider() if model == "claude-fable-5" else OpusProvider()

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        telemetry_events = []
        metrics = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )
        manager = ProviderManager(model="claude-fable-5", credentials={"anthropic": "k"})

        events = await _collect_stream_events(manager.stream(messages=[Message.user("hi")], system="sys"))
        follow_up_events = await _collect_stream_events(
            manager.stream(messages=[Message.user("continue")], system="sys")
        )

        assert [event.type for event in events] == ["message_start", "text_delta", "message_end"]
        assert events[0].message_id == "opus-fallback"
        assert events[1].text == "complete answer"
        assert all(getattr(event, "text", None) != "incomplete refusal text" for event in events)
        assert events[-1].stop_reason == "end_turn"
        assert follow_up_events[1].text == "continued with opus"
        assert manager.get_model_name() == "claude-opus-4-8"
        assert created_models == ["claude-fable-5", "claude-opus-4-8"]
        usage_events = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED]
        assert [attrs["status"] for attrs in usage_events] == ["refusal", "ok", "ok"]
        assert [value for name, value, _attrs in metrics if name == Metrics.TOKEN_TOTAL] == [7, 11, 5]

    async def test_stream_fallback_tombstone(self, monkeypatch):
        mock_provider = AsyncMock()
        metrics = []
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="partial")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="m2",
                text="complete",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=10, output_tokens=20),
            )
        )
        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        types = [e.type for e in events]
        assert "tombstone" in types and "text_delta" in types and "message_end" in types
        request_statuses = [attrs["status"] for name, _value, attrs in metrics if name == Metrics.API_REQUEST_COUNT]
        assert request_statuses == ["error", "ok"]

    async def test_stream_completion_fallback_reuses_same_telemetry_sidecar_for_each_attempt(self, monkeypatch):
        mock_provider = AsyncMock()
        captured_sidecars = []

        async def failing_stream(*args, **kwargs):
            del args, kwargs
            yield MessageStartEvent(message_id="m1")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="m2",
                text="complete",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(),
            )
        )

        def capture(_attrs, _messages, _system, _tools, telemetry_messages=None):
            captured_sidecars.append(telemetry_messages)

        monkeypatch.setattr("iac_code.providers.manager._capture_request_content", capture)
        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        sidecar = [object()]

        events = [
            event
            async for event in mgr.stream(
                messages=[Message.user("hi")],
                system="sys",
                telemetry_messages=sidecar,
            )
        ]

        assert any(event.type == "message_end" for event in events)
        assert captured_sidecars == [sidecar, sidecar]

    async def test_stream_fallback_tombstone_identifies_orphaned_tools(self):
        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            yield ToolUseStartEvent(tool_use_id="tool-1", name="bash")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="m2",
                text="complete",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(),
            )
        )
        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider

        events = [event async for event in mgr.stream(messages=[Message.user("hi")], system="sys")]

        tombstone = next(event for event in events if event.type == "tombstone")
        assert tombstone.message_id == "m1"
        assert tombstone.affected_tool_use_ids == ["tool-1"]

    async def test_fallback_complete_also_fails_yields_error_event(self):
        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(side_effect=ValueError("irrecoverable"))

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        # Shrink retry window so test is fast
        mgr._retry_config.max_retries = 0

        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        types = [e.type for e in events]
        assert "tombstone" in types
        assert "error" in types
        err = next(e for e in events if e.type == "error")
        assert err.error.startswith("ValueError:")
        assert "irrecoverable" in err.error
        assert err.error_id

    async def test_fallback_complete_error_event_redacts_public_error(self):
        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(
            side_effect=RuntimeError("Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml")
        )

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        mgr._retry_config.max_retries = 0

        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        err = next(e for e in events if e.type == "error")
        assert "sk-live" not in err.error
        assert "/Users/alice" not in err.error
        assert err.error.startswith("RuntimeError:")
        assert err.error_id

    async def test_fallback_error_event_preserves_original_exception_type_via_retry_wrapper(self):
        class RateLimitError(Exception):
            status_code = 429

        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(side_effect=RateLimitError("slow down"))

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        mgr._retry_config.max_retries = 0

        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        err = next(e for e in events if e.type == "error")
        # RetryableError wraps RateLimitError; both names should appear in the diagnostic
        assert "RetryableError" in err.error
        assert "RateLimitError" in err.error
        assert "slow down" in err.error
        assert err.error_id

    async def test_stream_idle_timeout_recovers_with_non_streaming_fallback(self):
        class HangingStreamProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                await asyncio.sleep(999)
                yield MessageEndEvent(stop_reason="never", usage=Usage())

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="fallback-after-timeout",
                    text="recovered",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=3, output_tokens=4),
                )

        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            stream_idle_timeout=STREAM_IDLE_TEST_TIMEOUT,
        )
        mgr._provider = HangingStreamProvider()

        events = await asyncio.wait_for(
            _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys")),
            timeout=1.0,
        )

        assert [event.type for event in events] == ["message_start", "text_delta", "message_end"]
        assert events[0].message_id == "fallback-after-timeout"
        assert events[1].text == "recovered"

    async def test_stream_idle_timeout_after_partial_message_yields_tombstone_then_fallback(self):
        class HangingAfterStartProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="partial-message")
                await asyncio.sleep(999)
                yield MessageEndEvent(stop_reason="never", usage=Usage())

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="fallback-after-partial-timeout",
                    text="recovered",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=3, output_tokens=4),
                )

        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            stream_idle_timeout=STREAM_IDLE_TEST_TIMEOUT,
        )
        mgr._provider = HangingAfterStartProvider()

        events = await asyncio.wait_for(
            _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys")),
            timeout=1.0,
        )

        assert [event.type for event in events] == [
            "message_start",
            "tombstone",
            "message_start",
            "text_delta",
            "message_end",
        ]
        assert events[0].message_id == "partial-message"
        assert events[1].message_id == "partial-message"
        assert events[2].message_id == "fallback-after-partial-timeout"
        assert events[3].text == "recovered"

    async def test_stream_idle_timeout_logs_disambiguating_diagnostic(self):
        # The idle watchdog fires as a bare asyncio.TimeoutError (empty message), so the
        # generic handler alone logs an uninformative line. A dedicated warning records
        # message_started + first_token_received, which distinguishes "nothing arrived"
        # (upstream-queue/connection stall) from "response opened then went silent"
        # (mid-stream/slow generation) — the exact question when a parallel pipeline
        # candidate appears to idle.
        from loguru import logger

        class HangImmediatelyProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                await asyncio.sleep(999)
                yield MessageEndEvent(stop_reason="never", usage=Usage())

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="fb",
                    text="recovered",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=1, output_tokens=1),
                )

        class HangAfterStartProvider(HangImmediatelyProvider):
            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="partial")
                await asyncio.sleep(999)
                yield MessageEndEvent(stop_reason="never", usage=Usage())

        async def _capture_idle_warning(provider) -> str:
            records: list[str] = []
            handler_id = logger.add(records.append, level="WARNING", format="{message}")
            try:
                mgr = ProviderManager(
                    model="claude-sonnet-4-6",
                    credentials={"anthropic": "k"},
                    stream_idle_timeout=STREAM_IDLE_TEST_TIMEOUT,
                )
                mgr._provider = provider
                await asyncio.wait_for(
                    _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys")),
                    timeout=1.0,
                )
            finally:
                logger.remove(handler_id)
            idle_lines = [line for line in records if "Provider stream idle timeout" in str(line)]
            assert idle_lines, records
            return str(idle_lines[0])

        nothing_arrived = await _capture_idle_warning(HangImmediatelyProvider())
        assert "message_started=False" in nothing_arrived
        assert "first_token_received=False" in nothing_arrived

        opened_then_silent = await _capture_idle_warning(HangAfterStartProvider())
        assert "message_started=True" in opened_then_silent
        assert "first_token_received=False" in opened_then_silent

    async def test_stream_cancelled_error_propagates_without_fallback(self):
        class CancellingStreamProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="partial-before-cancel")
                raise asyncio.CancelledError()

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise AssertionError("cancellation must not call non-streaming fallback")

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = CancellingStreamProvider()
        telemetry_events = []
        metrics = []

        with (
            patch(
                "iac_code.providers.manager.log_event",
                side_effect=lambda name, attrs: telemetry_events.append((name, attrs)),
            ),
            patch(
                "iac_code.providers.manager.add_metric",
                side_effect=lambda name, value, attrs: metrics.append((name, value, attrs)),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys"))

        assert len([attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_STARTED]) == 1
        failures = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
        assert len(failures) == 1
        assert failures[0]["status"] == "cancelled"
        assert failures[0]["error_type"] == "CancelledError"
        request_statuses = [attrs["status"] for name, _value, attrs in metrics if name == Metrics.API_REQUEST_COUNT]
        assert request_statuses == ["cancelled"]
        assert len([value for name, value, _attrs in metrics if name == Metrics.API_REQUEST_DURATION]) == 1

    async def test_stream_fallback_records_fallback_response_model_without_mutating_state(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig
        from iac_code.services.telemetry.names import Events, GenAiAttr
        from iac_code.services.telemetry.sanitize import sanitize_model_name

        class Status503Error(Exception):
            status_code = 503

        class PrimaryProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="primary-stream")
                raise ConnectionError("stream died")

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise Status503Error("primary complete outage")

        class FallbackProvider:
            def get_model_name(self) -> str:
                return "claude-haiku-4-5-20251001"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                raise AssertionError("stream fallback should use non-streaming complete")

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="fallback-response",
                    text="fallback text",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=5, output_tokens=6),
                )

        class RecordingSpan:
            def __init__(self, name, attributes):
                self.name = name
                self.attributes = dict(attributes or {})

            def set_attribute(self, key, value):
                self.attributes[key] = value

            def end(self):
                return None

        class RecordingSpanContext:
            def __init__(self, span):
                self.span = span

            def __enter__(self):
                return self.span

            def __exit__(self, exc_type, exc, tb):
                return None

        spans = []
        telemetry_events = []

        monkeypatch.setattr(
            "iac_code.providers.manager.create_provider",
            lambda model, credentials, *, base_url=None, provider_key_override=None, effort_override=None: (
                FallbackProvider() if model == "claude-haiku-4-5-20251001" else PrimaryProvider()
            ),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.start_detached_span",
            lambda name, attrs=None, *, parent_context=None: spans.append(RecordingSpan(name, attrs)) or spans[-1],
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.start_span",
            lambda name, attrs=None: RecordingSpanContext(spans.append(RecordingSpan(name, attrs)) or spans[-1]),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )

        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        events = await _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys"))

        success_event = next(attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED)
        failure_events = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
        assert [event.type for event in events] == [
            "message_start",
            "tombstone",
            "message_start",
            "text_delta",
            "message_end",
        ]
        assert len(spans) == 3
        assert [span.name for span in spans] == [
            f"{Spans.LLM_CHAT} claude-sonnet-4-6",
            f"{Spans.LLM_CHAT} claude-sonnet-4-6",
            f"{Spans.LLM_CHAT} claude-haiku-4-5-20251001",
        ]
        assert spans[2].attributes[GenAiAttr.REQUEST_MODEL] == "claude-haiku-4-5-20251001"
        assert GenAiAttr.RESPONSE_MODEL not in spans[0].attributes
        assert GenAiAttr.RESPONSE_MODEL not in spans[1].attributes
        assert spans[2].attributes[GenAiAttr.RESPONSE_MODEL] == "claude-haiku-4-5-20251001"
        assert spans[2].attributes[GenAiAttr.USAGE_TOTAL_TOKENS] == 11
        assert [attrs["model"] for attrs in failure_events] == [
            sanitize_model_name("claude-sonnet-4-6"),
            sanitize_model_name("claude-sonnet-4-6"),
        ]
        assert success_event["provider"] == "fallback"
        assert success_event["model"] == sanitize_model_name("claude-haiku-4-5-20251001")
        assert mgr.get_model_name() == "claude-sonnet-4-6"

    async def test_qwenpaw_config_error_yields_error_event_instead_of_system_exit(self, monkeypatch):
        from iac_code.services.qwenpaw_source import QwenPawError

        monkeypatch.setattr(
            "iac_code.config._get_env_overrides",
            lambda: {"api_key": None, "model": None, "base_url": None, "provider_key": None},
        )
        monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr(
            "iac_code.services.qwenpaw_source.load_from_qwenpaw",
            lambda: (_ for _ in ()).throw(QwenPawError("bad qwenpaw config")),
        )

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})

        events = await _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys"))

        assert len(events) == 1
        assert events[0].type == "error"
        assert "bad qwenpaw config" in events[0].error
        assert events[0].is_retryable is False
        assert events[0].error_id


@pytest.mark.asyncio
class TestProviderManagerCompleteRetry:
    @pytest.fixture(autouse=True)
    def _active_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")

    async def test_complete_records_chat_span_event_and_total_metric(self):
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="complete-response",
                text="ok",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=100, output_tokens=20, cache_read_input_tokens=60),
            )
        )
        span = MagicMock()
        telemetry_events = []
        metrics = []
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider

        with (
            use_span_attributes({IacCodeAttr.MODE: "normal"}),
            patch("iac_code.providers.manager.start_span", return_value=nullcontext(span)) as start_span,
            patch("iac_code.providers.manager.get_session_id", return_value="iac_sess_1"),
            patch(
                "iac_code.providers.manager.log_event",
                side_effect=lambda name, attrs: telemetry_events.append((name, attrs)),
            ),
            patch(
                "iac_code.providers.manager.add_metric",
                side_effect=lambda name, value, attrs: metrics.append((name, value, attrs)),
            ),
        ):
            response = await manager.complete(messages=[Message.user("hi")], system="sys")

        assert response.message_id == "complete-response"
        assert start_span.call_args.args[0] == f"{Spans.LLM_CHAT} claude-sonnet-4-6"
        assert start_span.call_args.args[1][IacCodeAttr.MODE] == "normal"
        span_attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
        assert span_attrs[GenAiAttr.USAGE_TOTAL_TOKENS] == 120
        success = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED]
        assert len(success) == 1
        assert success[0]["status"] == "ok"
        total_metrics = [(value, attrs) for name, value, attrs in metrics if name == Metrics.TOKEN_TOTAL]
        assert total_metrics == [
            (120, {"provider": "asyncmock", "model": "claude-sonnet-4-6", "iac_code.mode": "normal"})
        ]

    async def test_complete_attributes_bailian_openai_endpoint_to_dashscope_on_all_signals(self):
        mock_provider = AsyncMock()
        mock_provider._base_url = (
            "https://llm-testworkspace000000.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        )
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="complete-response",
                text="ok",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=4, output_tokens=3),
            )
        )
        span = MagicMock()
        telemetry_events = []
        metrics = []
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider

        with (
            patch("iac_code.providers.manager.start_span", return_value=nullcontext(span)) as start_span,
            patch(
                "iac_code.providers.manager.log_event",
                side_effect=lambda name, attrs: telemetry_events.append((name, attrs)),
            ),
            patch(
                "iac_code.providers.manager.add_metric",
                side_effect=lambda name, value, attrs: metrics.append((name, value, attrs)),
            ),
        ):
            await manager.complete(messages=[Message.user("hi")], system="sys")

        assert start_span.call_args.args[1][GenAiAttr.PROVIDER_NAME] == "dashscope"
        provider_events = [
            attrs["provider"]
            for name, attrs in telemetry_events
            if name in {Events.API_REQUEST_STARTED, Events.API_REQUEST_SUCCEEDED}
        ]
        assert provider_events == ["dashscope", "dashscope"]
        token_metrics = [attrs["provider"] for name, _value, attrs in metrics if name == Metrics.TOKEN_TOTAL]
        assert token_metrics == ["dashscope"]

    async def test_complete_succeeds_when_telemetry_start_event_and_metrics_fail(self):
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="complete-response",
                text="ok",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=2, output_tokens=1, reported=True),
            )
        )
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider

        with (
            patch("iac_code.providers.manager.start_span", side_effect=RuntimeError("span unavailable")),
            patch("iac_code.providers.manager.log_event", side_effect=RuntimeError("events unavailable")),
            patch("iac_code.providers.manager.add_metric", side_effect=RuntimeError("metrics unavailable")),
        ):
            response = await manager.complete(messages=[Message.user("hi")], system="sys")

        assert response.message_id == "complete-response"
        mock_provider.complete.assert_awaited_once()

    async def test_complete_succeeds_when_span_attribute_and_close_fail(self):
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="complete-response",
                text="ok",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=2, output_tokens=1, reported=True),
            )
        )
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider

        class FailingSpan:
            def set_attribute(self, _key, _value):
                raise RuntimeError("span attribute unavailable")

        class FailingSpanContext:
            def __enter__(self):
                return FailingSpan()

            def __exit__(self, *_args):
                raise RuntimeError("span close unavailable")

        with patch("iac_code.providers.manager.start_span", return_value=FailingSpanContext()):
            response = await manager.complete(messages=[Message.user("hi")], system="sys")

        assert response.message_id == "complete-response"
        mock_provider.complete.assert_awaited_once()

    async def test_complete_cancelled_error_records_terminal_telemetry_without_fallback(self, monkeypatch):
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(side_effect=asyncio.CancelledError())
        telemetry_events = []
        metrics = []
        manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        manager._provider = mock_provider
        fallback_factory = Mock(side_effect=AssertionError("cancellation must not create a fallback provider"))
        monkeypatch.setattr("iac_code.providers.manager.create_provider", fallback_factory)
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )

        with pytest.raises(asyncio.CancelledError):
            await manager.complete(messages=[Message.user("hi")], system="sys")

        mock_provider.complete.assert_awaited_once()
        fallback_factory.assert_not_called()
        assert len([attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_STARTED]) == 1
        failures = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
        assert len(failures) == 1
        assert failures[0]["status"] == "cancelled"
        assert failures[0]["error_type"] == "CancelledError"
        request_statuses = [attrs["status"] for name, _value, attrs in metrics if name == Metrics.API_REQUEST_COUNT]
        assert request_statuses == ["cancelled"]
        assert len([value for name, value, _attrs in metrics if name == Metrics.API_REQUEST_DURATION]) == 1

    async def test_retryable_status_429_retries_then_succeeds(self, monkeypatch):
        from iac_code.providers.base import NonStreamingResponse
        from iac_code.providers.retry import RetryConfig
        from iac_code.types.stream_events import Usage

        class RateLimitError(Exception):
            status_code = 429

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=[
                RateLimitError("slow down"),
                NonStreamingResponse(message_id="m", text="ok", tool_uses=[], stop_reason="end_turn", usage=Usage()),
            ]
        )
        telemetry_events = []
        metrics = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=2, base_delay=0.01, jitter_factor=0.0),
        )
        mgr._provider = mock_provider

        result = await mgr.complete(messages=[Message.user("hi")], system="")
        assert result.text == "ok"
        assert mock_provider.complete.call_count == 2
        request_statuses = [attrs["status"] for name, _value, attrs in metrics if name == Metrics.API_REQUEST_COUNT]
        assert request_statuses == ["error", "ok"]
        started_events = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_STARTED]
        assert [attrs["model"] for attrs in started_events] == ["claude-sonnet-4-6", "claude-sonnet-4-6"]

    async def test_retryable_failure_still_retries_when_failure_telemetry_fails(self):
        from iac_code.providers.retry import RetryConfig

        class RateLimitError(Exception):
            status_code = 429

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=[
                RateLimitError("slow down"),
                NonStreamingResponse(
                    message_id="m",
                    text="ok",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=1, output_tokens=1, reported=True),
                ),
            ]
        )
        manager = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=1, base_delay=0, jitter_factor=0),
        )
        manager._provider = mock_provider

        with (
            patch("iac_code.providers.manager.log_event", side_effect=RuntimeError("events unavailable")),
            patch("iac_code.providers.manager.add_metric", side_effect=RuntimeError("metrics unavailable")),
        ):
            response = await manager.complete(messages=[Message.user("hi")], system="sys")

        assert response.text == "ok"
        assert mock_provider.complete.await_count == 2

    async def test_any_5xx_status_retries_then_succeeds(self):
        from iac_code.providers.base import NonStreamingResponse
        from iac_code.providers.retry import RetryConfig
        from iac_code.types.stream_events import Usage

        class GatewayTimeoutError(Exception):
            status_code = 504

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=[
                GatewayTimeoutError("upstream timed out"),
                NonStreamingResponse(message_id="m", text="ok", tool_uses=[], stop_reason="end_turn", usage=Usage()),
            ]
        )
        manager = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=1, base_delay=0.01, jitter_factor=0.0),
        )
        manager._provider = mock_provider

        result = await manager.complete(messages=[Message.user("hi")], system="")

        assert result.text == "ok"
        assert mock_provider.complete.call_count == 2

    async def test_fable_non_streaming_refusal_falls_back_without_retrying_fable(self, monkeypatch):
        class FakeProvider:
            _PROVIDER_KEY = "anthropic"
            _logical_provider_key = "anthropic"

            def __init__(self, model: str):
                self.model = model
                self.complete_calls = 0

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                self.complete_calls += 1
                if self.model == "claude-fable-5":
                    return NonStreamingResponse(
                        message_id="fable-refusal",
                        text="incomplete refusal text",
                        tool_uses=[],
                        stop_reason="refusal",
                        usage=Usage(input_tokens=3, output_tokens=4),
                    )
                return NonStreamingResponse(
                    message_id="opus-fallback",
                    text="complete answer",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=5, output_tokens=6),
                )

        providers: dict[str, FakeProvider] = {}

        def fake_create_provider(model, credentials, **kwargs):
            provider = FakeProvider(model)
            providers[model] = provider
            return provider

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        telemetry_events = []
        metrics = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )
        manager = ProviderManager(model="claude-fable-5", credentials={"anthropic": "k"})

        response = await manager.complete(messages=[Message.user("hi")], system="")
        follow_up = await manager.complete(messages=[Message.user("continue")], system="")

        assert response.message_id == "opus-fallback"
        assert response.text == "complete answer"
        assert follow_up.message_id == "opus-fallback"
        assert providers["claude-fable-5"].complete_calls == 1
        assert providers["claude-opus-4-8"].complete_calls == 2
        assert manager.get_model_name() == "claude-opus-4-8"
        usage_events = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED]
        assert [attrs["status"] for attrs in usage_events] == ["refusal", "ok", "ok"]
        assert [value for name, value, _attrs in metrics if name == Metrics.TOKEN_TOTAL] == [7, 11, 11]

        manager.reset_conversation_state()

        assert manager.get_model_name() == "claude-fable-5"

    async def test_opus_refusal_does_not_continue_to_unapproved_target(self, monkeypatch):
        provider = AsyncMock()
        provider._PROVIDER_KEY = "anthropic"
        provider._logical_provider_key = "anthropic"
        provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="opus-refusal",
                text="",
                tool_uses=[],
                stop_reason="refusal",
                usage=Usage(),
            )
        )
        manager = ProviderManager(model="claude-opus-4-8", credentials={"anthropic": "k"})
        manager._provider = provider
        fallback_factory = Mock()
        monkeypatch.setattr("iac_code.providers.manager.create_provider", fallback_factory)

        with pytest.raises(RuntimeError, match="claude-opus-4-8.*refused"):
            await manager.complete(messages=[Message.user("hi")], system="")

        provider.complete.assert_awaited_once()
        fallback_factory.assert_not_called()

    async def test_fable_refusal_opus_transport_failure_does_not_degrade_to_sonnet(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class FakeProvider:
            _PROVIDER_KEY = "anthropic"
            _logical_provider_key = "anthropic"

            def __init__(self, model):
                self.model = model

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                if self.model == "claude-fable-5":
                    return NonStreamingResponse(
                        message_id="fable-refusal",
                        text="",
                        tool_uses=[],
                        stop_reason="refusal",
                        usage=Usage(),
                    )
                if self.model == "claude-opus-4-8":
                    raise Status503Error("temporary Opus outage")
                raise AssertionError(f"unapproved refusal fallback: {self.model}")

        created_models: list[str] = []

        def fake_create_provider(model, credentials, **kwargs):
            created_models.append(model)
            return FakeProvider(model)

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        telemetry_events = []
        metrics = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )
        manager = ProviderManager(
            model="claude-fable-5",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        with pytest.raises(RuntimeError, match="claude-fable-5.*refused"):
            await manager.complete(messages=[Message.user("hi")], system="")

        assert created_models == ["claude-fable-5", "claude-opus-4-8"]
        assert manager.get_model_name() == "claude-fable-5"
        success_events = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED]
        failure_events = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_FAILED]
        started_events = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_STARTED]
        assert [attrs["model"] for attrs in started_events] == ["claude-fable-5", "claude-opus-4-8"]
        assert [(attrs["model"], attrs["status"]) for attrs in success_events] == [("claude-fable-5", "refusal")]
        assert [(attrs["model"], attrs["error_type"]) for attrs in failure_events] == [
            ("claude-opus-4-8", "Status503Error")
        ]
        request_statuses = [attrs["status"] for name, _value, attrs in metrics if name == Metrics.API_REQUEST_COUNT]
        assert request_statuses == ["refusal", "error"]

    async def test_connection_error_is_retryable(self):
        from iac_code.providers.base import NonStreamingResponse
        from iac_code.providers.retry import RetryConfig
        from iac_code.types.stream_events import Usage

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=[
                ConnectionError("net"),
                NonStreamingResponse(message_id="m", text="ok", tool_uses=[], stop_reason="end_turn", usage=Usage()),
            ]
        )
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=2, base_delay=0.01, jitter_factor=0.0),
        )
        mgr._provider = mock_provider

        result = await mgr.complete(messages=[Message.user("hi")], system="")
        assert result.text == "ok"
        assert mock_provider.complete.call_count == 2

    @pytest.mark.parametrize(
        "error_type",
        [
            OpenAIAPIConnectionError,
            OpenAIAPITimeoutError,
            AnthropicAPIConnectionError,
            AnthropicAPITimeoutError,
        ],
    )
    async def test_sdk_transport_errors_are_retryable(self, error_type):
        from iac_code.providers.base import NonStreamingResponse
        from iac_code.providers.retry import RetryConfig
        from iac_code.types.stream_events import Usage

        request = httpx.Request("POST", "https://api.example.test/v1/messages")
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=[
                error_type(request=request),
                NonStreamingResponse(message_id="m", text="ok", tool_uses=[], stop_reason="end_turn", usage=Usage()),
            ]
        )
        manager = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=1, base_delay=0, jitter_factor=0),
        )
        manager._provider = mock_provider

        result = await manager.complete(messages=[Message.user("hi")], system="")

        assert result.text == "ok"
        assert mock_provider.complete.call_count == 2

    async def test_sdk_transport_error_triggers_model_fallback(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

        class PrimaryProvider:
            _PROVIDER_KEY = "openai"
            _logical_provider_key = "openai"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise OpenAIAPIConnectionError(request=request)

        fallback_provider = AsyncMock()
        fallback_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="fallback",
                text="fallback ok",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(),
            )
        )
        fallback_provider._PROVIDER_KEY = "openai"
        fallback_provider._logical_provider_key = "openai"
        monkeypatch.setattr("iac_code.providers.manager.create_provider", Mock(return_value=fallback_provider))
        manager = ProviderManager(
            model="gpt-5.6-sol",
            credentials={"openai": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )
        manager._provider = PrimaryProvider()

        result = await manager.complete(messages=[Message.user("hi")], system="")

        assert result.text == "fallback ok"
        fallback_provider.complete.assert_awaited_once()

    async def test_non_retryable_error_propagates(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(side_effect=ValueError("bad input"))
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=3, base_delay=0.01, jitter_factor=0.0),
        )
        mgr._provider = mock_provider
        fallback_factory = Mock()
        monkeypatch.setattr("iac_code.providers.manager.create_provider", fallback_factory)

        with pytest.raises(ValueError, match="bad input"):
            await mgr.complete(messages=[Message.user("hi")], system="")
        # ValueError has no status_code and isn't ConnectionError/TimeoutError/OSError,
        # so it should NOT be retried.
        assert mock_provider.complete.call_count == 1
        fallback_factory.assert_not_called()

    async def test_authentication_error_does_not_trigger_model_fallback(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        class Status401Error(Exception):
            status_code = 401

        class FakeProvider:
            _PROVIDER_KEY = "openai"
            _logical_provider_key = "openai"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise Status401Error("invalid api key")

        manager = ProviderManager(
            model="gpt-5.6-sol",
            credentials={"openai": "key"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )
        manager._provider = FakeProvider()
        fallback_factory = Mock()
        monkeypatch.setattr("iac_code.providers.manager.create_provider", fallback_factory)

        with pytest.raises(Status401Error, match="invalid api key"):
            await manager.complete(messages=[Message.user("hi")], system="")

        fallback_factory.assert_not_called()

    async def test_fallback_success_does_not_mutate_manager_state(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class FakeProvider:
            def __init__(self, model: str, *, fail: bool = False):
                self.model = model
                self.fail = fail

            def get_model_name(self) -> str:
                return self.model

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                if self.fail:
                    raise Status503Error("temporary outage")
                return NonStreamingResponse(
                    message_id="fallback-response",
                    text="fallback ok",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=1, output_tokens=2),
                )

        created_models: list[str] = []

        def fake_create_provider(
            model, credentials, *, base_url=None, provider_key_override=None, effort_override=None
        ):
            created_models.append(model)
            return FakeProvider(model, fail=model == "claude-sonnet-4-6")

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )
        original_provider = mgr._provider

        response = await mgr.complete(messages=[Message.user("hi")], system="")

        assert response.text == "fallback ok"
        assert created_models == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
        assert mgr.get_model_name() == "claude-sonnet-4-6"
        assert mgr._provider is original_provider

    async def test_fallback_preserves_runtime_provider_identity(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class FakeProvider:
            def __init__(self, model: str, provider_key: str, *, fail: bool):
                self.model = model
                self._PROVIDER_KEY = provider_key
                self._logical_provider_key = provider_key
                self.fail = fail

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                if self.fail:
                    raise Status503Error("temporary outage")
                return NonStreamingResponse(
                    message_id="fallback-response",
                    text="fallback ok",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(),
                )

        created: list[tuple[str, str | None]] = []

        def fake_create_provider(model, credentials, *, base_url=None, provider_key_override=None):
            created.append((model, provider_key_override))
            provider_key = provider_key_override or "dashscope_token_plan"
            return FakeProvider(model, provider_key, fail=model == "qwen3.8-max-preview")

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        manager = ProviderManager(
            model="qwen3.8-max-preview",
            credentials={"dashscope_token_plan": "key", "dashscope": ""},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        response = await manager.complete(messages=[Message.user("hi")], system="")

        assert response.text == "fallback ok"
        assert created == [
            ("qwen3.8-max-preview", None),
            ("qwen3.8-max", "dashscope_token_plan"),
        ]

    async def test_provider_specific_fallback_uses_wire_key_and_preserves_logical_provider(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class FakeProvider:
            _PROVIDER_KEY = "dashscope_token_plan"

            def __init__(self, logical_provider_key: str, *, fail: bool):
                self._logical_provider_key = logical_provider_key
                self.fail = fail

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                if self.fail:
                    raise Status503Error("temporary outage")
                return NonStreamingResponse(
                    message_id="fallback-response",
                    text="fallback ok",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(),
                )

        created: list[tuple[str, str | None]] = []

        def fake_create_provider(model, credentials, *, base_url=None, provider_key_override=None):
            created.append((model, provider_key_override))
            if provider_key_override == "openai_compatible":
                assert credentials["openai_compatible"] == "compat-key"
            return FakeProvider(
                provider_key_override or "openai_compatible",
                fail=model == "qwen3.6-plus",
            )

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai_compatible")
        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        manager = ProviderManager(
            model="qwen3.6-plus",
            credentials={"openai_compatible": "compat-key", "dashscope_token_plan": ""},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        response = await manager.complete(messages=[Message.user("hi")], system="")

        assert response.text == "fallback ok"
        assert created == [
            ("qwen3.6-plus", None),
            ("qwen3.6-flash", "openai_compatible"),
        ]

    async def test_fallback_walks_declared_chain_until_success(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class FakeProvider:
            _PROVIDER_KEY = "openai"
            _logical_provider_key = "openai"

            def __init__(self, model: str):
                self.model = model

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                if self.model != "gpt-5.6-luna":
                    raise Status503Error(f"{self.model} unavailable")
                return NonStreamingResponse(
                    message_id="luna-response",
                    text="luna ok",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(),
                )

        created_models: list[str] = []

        def fake_create_provider(model, credentials, *, base_url=None, provider_key_override=None):
            created_models.append(model)
            assert provider_key_override in {None, "openai"}
            return FakeProvider(model)

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        manager = ProviderManager(
            model="gpt-5.6-sol",
            credentials={"openai": "key"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        response = await manager.complete(messages=[Message.user("hi")], system="")

        assert response.text == "luna ok"
        assert created_models == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

    async def test_fallback_provider_creation_failure_preserves_original_error(self, monkeypatch):
        from iac_code.providers.retry import RetryableError, RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class PrimaryProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise Status503Error("primary temporary outage")

        def fake_create_provider(
            model, credentials, *, base_url=None, provider_key_override=None, effort_override=None
        ):
            if model == "claude-sonnet-4-6":
                return PrimaryProvider()
            raise RuntimeError("fallback provider unavailable")

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        with pytest.raises(RetryableError, match="primary temporary outage"):
            await mgr.complete(messages=[Message.user("hi")], system="")

    async def test_fallback_complete_failure_preserves_original_error(self, monkeypatch):
        from iac_code.providers.retry import RetryableError, RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class PrimaryProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise Status503Error("primary temporary outage")

        class FallbackProvider:
            def get_model_name(self) -> str:
                return "claude-haiku-4-5-20251001"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise RuntimeError("fallback complete failed")

        created_models: list[str] = []

        def fake_create_provider(
            model, credentials, *, base_url=None, provider_key_override=None, effort_override=None
        ):
            created_models.append(model)
            if model == "claude-sonnet-4-6":
                return PrimaryProvider()
            return FallbackProvider()

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        with pytest.raises(RetryableError, match="primary temporary outage") as exc_info:
            await mgr.complete(messages=[Message.user("hi")], system="")

        assert "fallback complete failed" not in str(exc_info.value)
        assert created_models == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


class TestModelPrefixAutoMapping:
    """_detect_provider_name falls back to model-name prefix heuristics."""

    @pytest.mark.parametrize(
        "model, expected_provider",
        [
            ("claude-sonnet-4-6", "anthropic"),
            ("claude-opus-4-7", "anthropic"),
            ("claude-haiku-4-5-20251001", "anthropic"),
            ("gpt-4o", "openai"),
            ("gpt-5.5", "openai"),
            ("o1-preview", "openai"),
            ("o3-mini", "openai"),
            ("qwen3.6-plus", "dashscope"),
            ("qwen3.8-max", "dashscope"),
            ("qwen-max", "dashscope"),
            ("deepseek-v4-pro", "deepseek"),
            ("deepseek-chat", "deepseek"),
        ],
    )
    def test_auto_maps_mainstream_models(self, monkeypatch, model, expected_provider):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        assert _detect_provider_name(model) == expected_provider

    def test_saved_config_takes_precedence_over_prefix(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        assert _detect_provider_name("claude-sonnet-4-6") == "openai"

    @pytest.mark.parametrize(
        "model, expected_provider",
        [
            ("qwen3.8-max-preview", "dashscope_token_plan"),
            ("glm-5.2-fast-preview", "dashscope"),
            ("kimi/kimi-k3", "dashscope"),
            ("MiniMax/MiniMax-M3", "dashscope"),
            ("deepseek-v4-pro-0813", "dashscope"),
            ("deepseek-v4-flash-0731", "dashscope"),
            ("glm-5.3", "zhipu_cn_codingplan"),
        ],
    )
    def test_exact_hosted_models_override_generic_prefixes(self, monkeypatch, model, expected_provider):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        assert _detect_provider_name(model) == expected_provider

    def test_unknown_model_still_raises(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        with pytest.raises(ValueError, match="Cannot determine provider"):
            _detect_provider_name("totally-unknown-model")

    def test_auto_mapped_model_without_api_key_raises(self, monkeypatch):
        """Model prefix resolves the provider, but empty credential raises ValueError."""
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        with pytest.raises(ValueError, match="No API key configured for provider"):
            create_provider("claude-sonnet-4-6", credentials={"anthropic": ""})


def test_qwen38_multimodal_fallback_preserves_image_support():
    # qwen3.8-max-preview ended its preview and left the selectable catalog,
    # but saved settings may still hold the ID; the fallback keeps routing
    # it to the multimodal formal model.
    source_model = "qwen3.8-max-preview"
    fallback_model = MODEL_FALLBACK_MAP[source_model]
    entries = {model.id: model for model in PROVIDER_REGISTRY["dashscope_token_plan"].models}

    assert fallback_model == "qwen3.8-max"
    assert source_model not in entries
    assert entries[fallback_model].support_multimodal is True


def test_static_provider_fallbacks_stay_within_each_model_catalog():
    for provider_key, descriptor in PROVIDER_REGISTRY.items():
        models = {model.id: model for model in descriptor.models}
        for source_model, fallback_model in MODEL_FALLBACK_MAP.items():
            if source_model in models:
                assert fallback_model in models, (
                    f"{provider_key} fallback {source_model} -> {fallback_model} leaves the provider catalog"
                )
                if models[source_model].support_multimodal:
                    assert models[fallback_model].support_multimodal, (
                        f"{provider_key} fallback {source_model} -> {fallback_model} loses image support"
                    )
    for provider_key, fallbacks in _PROVIDER_MODEL_FALLBACK_MAP.items():
        models = {model.id: model for model in PROVIDER_REGISTRY[provider_key].models}
        for source_model, fallback_model in fallbacks.items():
            assert source_model in models
            assert fallback_model in models
            if models[source_model].support_multimodal:
                assert models[fallback_model].support_multimodal, (
                    f"{provider_key} fallback {source_model} -> {fallback_model} loses image support"
                )


@pytest.mark.parametrize(
    "provider_key, expected_fallback",
    [
        ("dashscope", "qwen3.6-flash"),
        ("dashscope_token_plan", "qwen3.6-flash"),
        ("aliyun_codingplan", "qwen3.5-plus"),
        ("aliyun_codingplan_intl", "qwen3.5-plus"),
    ],
)
def test_qwen36_fallback_is_available_on_each_endpoint(provider_key, expected_fallback):
    assert _PROVIDER_MODEL_FALLBACK_MAP[provider_key]["qwen3.6-plus"] == expected_fallback
    assert expected_fallback in {model.id for model in PROVIDER_REGISTRY[provider_key].models}
    assert "qwen3.6-plus" not in MODEL_FALLBACK_MAP
