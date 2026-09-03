"""Tests for DashScope provider — OpenAI-compatible endpoint."""

import pytest

from iac_code.agent.system_prompt import DYNAMIC_BOUNDARY
from iac_code.providers.base import ContentBlock, Message, ToolDefinition
from iac_code.providers.dashscope_provider import (
    _EXPLICIT_CACHE_MODEL_PREFIXES,
    _PRESERVE_THINKING_MODEL_PREFIXES,
    DASHSCOPE_BASE_URL,
    DashScopeProvider,
)
from iac_code.providers.openai_provider import OpenAIProvider
from tests.providers._fakes import FakeOpenAIClient, ns


class TestDashScopeProvider:
    def test_get_model_name(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="test")
        assert p.get_model_name() == "qwen3.6-plus"

    def test_inherits_openai_provider(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="test")
        assert isinstance(p, OpenAIProvider)

    def test_uses_dashscope_base_url(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="test")
        assert str(p._client.base_url).rstrip("/") == DASHSCOPE_BASE_URL.rstrip("/")

    def test_message_conversion_inherited(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="test")
        msgs = [Message.user("Hello")]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "user"
        assert api[0]["content"] == "Hello"

    def test_thinking_only_assistant_uses_required_string_content(self):
        p = DashScopeProvider(model="deepseek-v4-flash-0731", api_key="test")

        api = p._convert_content_blocks("assistant", [ContentBlock(type="thinking", text="still reasoning")])

        assert api == [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "still reasoning",
            }
        ]

    def test_tool_conversion_inherited(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="test")
        tools = [
            ToolDefinition(
                name="bash",
                description="Run",
                input_schema={"type": "object"},
            )
        ]
        api = p._convert_tools(tools)
        assert api[0]["type"] == "function"
        assert api[0]["function"]["name"] == "bash"


class TestDashScopeBaseUrl:
    def test_default_base_url_is_dashscope(self):
        from iac_code.providers.dashscope_provider import DASHSCOPE_BASE_URL, DashScopeProvider

        p = DashScopeProvider(model="qwen3.6-plus", api_key="test")
        assert p._base_url == DASHSCOPE_BASE_URL
        assert DASHSCOPE_BASE_URL.startswith("https://dashscope.aliyuncs.com/")

    def test_supports_stream_options_true(self):
        from iac_code.providers.dashscope_provider import DashScopeProvider

        assert DashScopeProvider.supports_stream_options is True


class TestDashScopeBuildThinkingKwargs:
    def test_qwen_returns_enable_thinking(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="k")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True, "preserve_thinking": True}}

    def test_enabled_false_returns_disable_thinking(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="k", thinking_enabled=False)
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}

    def test_qwen_with_effort_still_only_enable_thinking(self):
        # Bailian Qwen does not honor effort — provider ignores it gracefully.
        p = DashScopeProvider(model="qwen3.6-plus", api_key="k", effort="high")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True, "preserve_thinking": True}}

    def test_qwen_with_effort_none_disables_thinking(self):
        p = DashScopeProvider(model="qwen3.7-max", api_key="k", effort="none")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}

    def test_kimi(self):
        p = DashScopeProvider(model="kimi-k2.6", api_key="k")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True, "preserve_thinking": True}}

    def test_kimi_k3_preserves_thinking_without_enable_flag(self):
        p = DashScopeProvider(model="kimi/kimi-k3", api_key="k")
        assert p._build_thinking_kwargs() == {"extra_body": {"preserve_thinking": True}}

    def test_bailian_hosted_kimi_k3_keeps_always_on_thinking(self):
        p = DashScopeProvider(model="kimi-k3", api_key="k", thinking_enabled=False)
        assert p._build_thinking_kwargs() == {
            "extra_body": {"enable_thinking": True, "preserve_thinking": True}
        }

    def test_qwen38_open_model_supports_thinking_budget(self):
        p = DashScopeProvider(model="qwen3.8-2.4t-a95b", api_key="k", thinking_budget=2048)
        assert p._build_thinking_kwargs() == {
            "extra_body": {"enable_thinking": True, "thinking_budget": 2048}
        }

    def test_stepfun_uses_its_documented_effort_values(self):
        p = DashScopeProvider(model="stepfun/step-3.7-flash", api_key="k", effort="medium")
        assert p._build_thinking_kwargs() == {
            "extra_body": {"enable_thinking": True},
            "reasoning_effort": "medium",
        }

    def test_qwen38_uses_always_on_thinking_without_enable_flag(self):
        p = DashScopeProvider(
            model="qwen3.8-max-preview",
            api_key="k",
            provider_key="dashscope_token_plan",
        )
        assert p._build_thinking_kwargs() == {"extra_body": {"preserve_thinking": True}}

    def test_qwen38_formal_uses_hybrid_thinking_and_preserves_reasoning(self):
        p = DashScopeProvider(model="qwen3.8-max", api_key="k", effort="low")
        assert p._build_thinking_kwargs() == {
            "extra_body": {"enable_thinking": True, "preserve_thinking": True},
            "reasoning_effort": "low",
        }

    def test_qwen38_formal_can_disable_thinking(self):
        p = DashScopeProvider(model="qwen3.8-max", api_key="k", thinking_enabled=False)
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": False}}

    @pytest.mark.parametrize("prefix", _PRESERVE_THINKING_MODEL_PREFIXES)
    def test_documented_models_support_preserve_thinking(self, prefix):
        p = DashScopeProvider(model=prefix, api_key="k")
        assert p._supports_preserve_thinking()

    def test_qwen36_flash_preserves_thinking_for_tool_loops(self):
        p = DashScopeProvider(model="qwen3.6-flash", api_key="k")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True, "preserve_thinking": True}}

    def test_glm(self):
        p = DashScopeProvider(model="glm-5.1", api_key="k")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True}}

    def test_zhipu_glm53_uses_always_on_hosted_protocol(self):
        p = DashScopeProvider(model="ZHIPU/GLM-5.3", api_key="k", effort="high")
        assert p._build_thinking_kwargs() == {
            "extra_body": {"enable_thinking": True},
            "reasoning_effort": "high",
        }

    def test_zhipu_glm53_disable_request_degrades_to_low_effort(self):
        p = DashScopeProvider(model="ZHIPU/GLM-5.3", api_key="k", thinking_enabled=False)
        assert p._build_thinking_kwargs() == {
            "extra_body": {"enable_thinking": True},
            "reasoning_effort": "low",
        }

    @pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-0731"])
    def test_bailian_deepseek_emits_enable_thinking_and_reasoning_effort(self, model):
        p = DashScopeProvider(model=model, api_key="k", effort="xhigh")
        kwargs = p._build_thinking_kwargs()
        assert kwargs == {
            "extra_body": {"enable_thinking": True},
            "reasoning_effort": "xhigh",
        }

    def test_unknown_model_returns_empty(self):
        p = DashScopeProvider(model="not-real", api_key="k")
        assert p._build_thinking_kwargs() == {}

    def test_effort_request_kwargs_delegates(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="k")
        assert p._effort_request_kwargs() == p._build_thinking_kwargs()


@pytest.mark.asyncio
class TestDashScopeThinkingBudgetRequestPolicy:
    async def test_qwen38_stream_uses_token_plan_always_on_payload(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(
            model="qwen3.8-max-preview",
            api_key="k",
            provider_key="dashscope_token_plan",
            thinking_enabled=True,
        )
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="")]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["extra_body"] == {"preserve_thinking": True}
        assert call_kwargs["reasoning_effort"] == "xhigh"
        assert "enable_thinking" not in call_kwargs["extra_body"]

    async def test_qwen38_complete_default_omits_enable_thinking(self):
        response = ns(
            id="cmpl_qwen38",
            choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))],
            usage=ns(prompt_tokens=1, completion_tokens=1),
        )
        client = FakeOpenAIClient(create_response=response)
        provider = DashScopeProvider(
            model="qwen3.8-max-preview",
            api_key="k",
            provider_key="dashscope_token_plan",
        )
        provider._client = client

        await provider.complete(messages=[Message.user("hi")], system="")

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["extra_body"] == {"preserve_thinking": True}
        assert "reasoning_effort" not in call_kwargs
        assert "enable_thinking" not in call_kwargs["extra_body"]

    @pytest.mark.parametrize("model", ["glm-5.2", "glm-5.2-fast-preview"])
    async def test_glm52_models_use_total_output_limit_without_qwen_thinking_budget(self, model):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model=model, api_key="k")
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["max_completion_tokens"] == 8192
        assert "max_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {"enable_thinking": True}
        assert "reasoning_effort" not in call_kwargs

    async def test_glm52_enabled_false_disables_budget_and_max_completion_tokens(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model="glm-5.2", api_key="k", effort="high", thinking_enabled=False)
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["max_tokens"] == 8192
        assert "max_completion_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {"enable_thinking": False}
        assert "reasoning_effort" not in call_kwargs

    async def test_kimi_k27_code_defaults_to_bounded_thinking_budget_and_max_completion_tokens(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model="kimi-k2.7-code", api_key="k")
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["max_completion_tokens"] == 16384
        assert "max_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {
            "enable_thinking": True,
            "preserve_thinking": True,
            "thinking_budget": 8192,
        }
        assert "reasoning_effort" not in call_kwargs

    async def test_qwen_request_policy_keeps_existing_max_tokens_behavior(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model="qwen3.7-max", api_key="k")
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["max_tokens"] == 8192
        assert "max_completion_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {"enable_thinking": True, "preserve_thinking": True}

    async def test_token_plan_glm52_uses_same_budget_free_request_policy(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model="glm-5.2", api_key="k", provider_key="dashscope_token_plan")
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["max_completion_tokens"] == 8192
        assert "max_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {"enable_thinking": True}

    @pytest.mark.parametrize("model", ["glm-5.2", "glm-5.2-fast-preview"])
    async def test_glm52_models_use_user_configured_reasoning_effort(self, model):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model=model, api_key="k", effort="low")
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["reasoning_effort"] == "low"
        assert call_kwargs["extra_body"] == {"enable_thinking": True}

    @pytest.mark.parametrize(
        ("provider_key", "model"),
        [
            ("dashscope", "glm-5.1"),
            ("dashscope_token_plan", "glm-5.1"),
            ("dashscope_token_plan", "glm-5"),
        ],
    )
    async def test_glm51_family_stream_uses_user_configured_reasoning_effort(self, provider_key, model):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model=model, api_key="k", provider_key=provider_key, effort="xhigh")
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="")]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["reasoning_effort"] == "xhigh"
        assert call_kwargs["extra_body"]["enable_thinking"] is True

    @pytest.mark.parametrize("model", ["glm-5.1", "glm-5"])
    async def test_token_plan_glm51_family_complete_uses_user_configured_reasoning_effort(self, model):
        response = ns(
            id="cmpl_glm51",
            choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))],
            usage=ns(prompt_tokens=1, completion_tokens=1),
        )
        client = FakeOpenAIClient(create_response=response)
        provider = DashScopeProvider(
            model=model,
            api_key="k",
            provider_key="dashscope_token_plan",
            effort="high",
        )
        provider._client = client

        await provider.complete(messages=[Message.user("hi")], system="")

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["reasoning_effort"] == "high"
        assert call_kwargs["extra_body"]["enable_thinking"] is True

    async def test_kimi_k27_code_ignores_reasoning_effort(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model="kimi-k2.7-code", api_key="k", effort="high")
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert "reasoning_effort" not in call_kwargs

    async def test_complete_uses_same_glm52_request_policy(self):
        response = ns(
            id="cmpl_1",
            choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))],
            usage=ns(prompt_tokens=1, completion_tokens=1),
        )
        client = FakeOpenAIClient(create_response=response)
        provider = DashScopeProvider(model="glm-5.2", api_key="k")
        provider._client = client

        await provider.complete(messages=[Message.user("hi")], system="", max_tokens=8192)

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["max_completion_tokens"] == 8192
        assert "max_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {"enable_thinking": True}

    async def test_glm52_ignores_unsupported_thinking_budget_but_uses_output_limit(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(
            model="glm-5.2",
            api_key="k",
            thinking_budget=2048,
            max_completion_tokens=10000,
        )
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        # glm-5.2 由 effort 驱动思考，不支持独立思考预算；显式输出上限不叠加 2048。
        assert call_kwargs["max_completion_tokens"] == 10000
        assert "max_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {"enable_thinking": True}

    async def test_float_request_policy_values_are_rejected_not_truncated(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(
            model="glm-5.2",
            api_key="k",
            thinking_budget=2048.9,
            max_completion_tokens=10000.5,
        )
        provider._client = client

        _ = [event async for event in provider.stream(messages=[Message.user("hi")], system="", max_tokens=8192)]

        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs["max_completion_tokens"] == 8192
        assert call_kwargs["extra_body"] == {"enable_thinking": True}


class TestDashScopeTokenPlanBaseUrl:
    def test_token_plan_base_url_constant(self):
        from iac_code.providers.dashscope_provider import DASHSCOPE_TOKEN_PLAN_BASE_URL

        assert DASHSCOPE_TOKEN_PLAN_BASE_URL == ("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")

    def test_uses_custom_base_url_when_provided(self):
        from iac_code.providers.dashscope_provider import (
            DASHSCOPE_TOKEN_PLAN_BASE_URL,
            DashScopeProvider,
        )

        p = DashScopeProvider(
            model="qwen3.6-plus",
            api_key="k",
            base_url=DASHSCOPE_TOKEN_PLAN_BASE_URL,
        )
        assert p._base_url == DASHSCOPE_TOKEN_PLAN_BASE_URL
        assert str(p._client.base_url).rstrip("/") == DASHSCOPE_TOKEN_PLAN_BASE_URL.rstrip("/")

    def test_default_base_url_unchanged(self):
        from iac_code.providers.dashscope_provider import DASHSCOPE_BASE_URL, DashScopeProvider

        p = DashScopeProvider(model="qwen3.6-plus", api_key="k")
        assert p._base_url == DASHSCOPE_BASE_URL


class TestDashScopeProviderKeyInjection:
    def test_default_provider_key_is_dashscope(self):
        from iac_code.providers.dashscope_provider import DashScopeProvider

        p = DashScopeProvider(model="qwen3.6-plus", api_key="k")
        assert p._PROVIDER_KEY == "dashscope"

    def test_provider_key_can_be_overridden(self):
        from iac_code.providers.dashscope_provider import DashScopeProvider

        p = DashScopeProvider(
            model="qwen3.6-plus",
            api_key="k",
            provider_key="dashscope_token_plan",
        )
        assert p._PROVIDER_KEY == "dashscope_token_plan"


class TestDashScopeExplicitCache:
    """Tests for DashScope explicit context cache (cache_control markers)."""

    @pytest.mark.parametrize("prefix", _EXPLICIT_CACHE_MODEL_PREFIXES)
    def test_supported_model_prefixes(self, prefix):
        p = DashScopeProvider(model=prefix, api_key="k")
        assert p._supports_explicit_cache()

    @pytest.mark.parametrize("model", ["qwen3.7-max", "qwen3.7-plus"])
    def test_qwen37_models_support_explicit_cache(self, model):
        p = DashScopeProvider(model=model, api_key="k")
        assert p._supports_explicit_cache()

    @pytest.mark.parametrize("model", ["kimi-k2.6", "kimi/kimi-k3"])
    def test_unsupported_model_returns_false(self, model):
        p = DashScopeProvider(model=model, api_key="k")
        assert not p._supports_explicit_cache()

    def test_unknown_model_returns_false(self):
        p = DashScopeProvider(model="some-random-model", api_key="k")
        assert not p._supports_explicit_cache()

    def test_build_api_messages_with_cache_control(self):
        """Supported model: system message uses array content with cache_control."""
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        system = f"STATIC\n\n{DYNAMIC_BOUNDARY}\n\nDYNAMIC"
        msgs = [Message.user("hello")]
        api = p._build_api_messages(msgs, system)

        sys_msg = api[0]
        assert sys_msg["role"] == "system"
        assert isinstance(sys_msg["content"], list)
        assert len(sys_msg["content"]) == 2
        assert sys_msg["content"][0]["text"] == "STATIC"
        assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert sys_msg["content"][1]["text"] == "DYNAMIC"
        assert "cache_control" not in sys_msg["content"][1]

    def test_build_api_messages_without_dynamic_part(self):
        """No DYNAMIC_BOUNDARY → entire prompt cached as one block."""
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        api = p._build_api_messages([Message.user("hi")], "ALL STATIC")

        sys_msg = api[0]
        assert isinstance(sys_msg["content"], list)
        assert len(sys_msg["content"]) == 1
        assert sys_msg["content"][0]["text"] == "ALL STATIC"
        assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_build_api_messages_unsupported_model_plain_string(self):
        """Unsupported model: system message stays as plain string."""
        p = DashScopeProvider(model="deepseek-v4-pro", api_key="k")
        api = p._build_api_messages([Message.user("hi")], "sys prompt")

        sys_msg = api[0]
        assert sys_msg["role"] == "system"
        assert sys_msg["content"] == "sys prompt"

    def test_build_api_messages_empty_system(self):
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        api = p._build_api_messages([Message.user("hi")], "")
        assert api[0]["role"] == "user"

    def test_no_explicit_cache_policy_leaves_messages_plain(self):
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        system = f"STATIC\n\n{DYNAMIC_BOUNDARY}\n\nDYNAMIC"
        api = p._build_api_messages([Message.user("hi")], system, cache_policy="no_explicit_cache")

        assert api[0] == {"role": "system", "content": system}
        assert api[1] == {"role": "user", "content": "hi"}

    def test_last_user_message_gets_cache_control(self):
        """Supported model: last user message is wrapped with cache_control."""
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        msgs = [Message.user("first"), Message.assistant_text("reply"), Message.user("second")]
        api = p._build_api_messages(msgs, "sys")

        last_user = api[-1]
        assert last_user["role"] == "user"
        assert isinstance(last_user["content"], list)
        assert last_user["content"][0]["text"] == "second"
        assert last_user["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_last_user_message_dynamic_boundary_caches_only_static_prefix(self):
        """Supported model: user message dynamic boundary keeps changing retry text outside the cache marker."""
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        msg = Message.user(f"FACT BUNDLE\n\n{DYNAMIC_BOUNDARY}\n\nATTEMPT INSTRUCTION")
        api = p._build_api_messages([msg], "sys")

        last_user = api[-1]
        assert last_user["role"] == "user"
        assert last_user["content"] == [
            {"type": "text", "text": "FACT BUNDLE", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "ATTEMPT INSTRUCTION"},
        ]

    def test_non_last_user_message_with_dynamic_boundary_gets_cache_control(self):
        """Append-style retries keep first-turn facts cacheable even after later user turns are appended."""
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        msgs = [
            Message.user(f"FACT BUNDLE\n\n{DYNAMIC_BOUNDARY}\n\nATTEMPT 1"),
            Message.assistant_text('{"node_labels":[]}'),
            Message.user("ATTEMPT 2"),
        ]
        api = p._build_api_messages(msgs, "sys")

        first_user = api[1]
        last_user = api[-1]
        assert first_user["role"] == "user"
        assert first_user["content"] == [
            {"type": "text", "text": "FACT BUNDLE", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "ATTEMPT 1"},
        ]
        assert last_user["content"] == [{"type": "text", "text": "ATTEMPT 2", "cache_control": {"type": "ephemeral"}}]

    def test_first_user_not_tagged_when_multiple(self):
        """Only the *last* user message gets cache_control, not earlier ones."""
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        msgs = [Message.user("first"), Message.assistant_text("reply"), Message.user("second")]
        api = p._build_api_messages(msgs, "sys")

        first_user = api[1]
        assert first_user["role"] == "user"
        assert isinstance(first_user["content"], str)

    def test_unsupported_model_no_user_cache_control(self):
        """Unsupported model: user messages stay as plain strings."""
        p = DashScopeProvider(model="deepseek-v4-pro", api_key="k")
        msgs = [Message.user("hello")]
        api = p._build_api_messages(msgs, "sys")

        user_msg = api[-1]
        assert user_msg["content"] == "hello"

    def test_recalled_memory_reminder_does_not_steal_user_cache_control(self):
        """Provider-only recalled memory should not become the cache prefix marker."""
        p = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        msgs = [
            Message.user("actual user question"),
            Message.user(
                "<system-reminder>\n"
                "Relevant persistent memories recalled for this conversation:\n\n"
                "# Recalled Memory\n"
                "Prefer ROS YAML.\n"
                "</system-reminder>"
            ),
        ]

        api = p._build_api_messages(msgs, "sys")

        actual_user = api[1]
        reminder = api[2]
        assert actual_user["content"][0]["text"] == "actual user question"
        assert actual_user["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert reminder["content"] == (
            "<system-reminder>\n"
            "Relevant persistent memories recalled for this conversation:\n\n"
            "# Recalled Memory\n"
            "Prefer ROS YAML.\n"
            "</system-reminder>"
        )


@pytest.mark.asyncio
class TestDashScopeCacheMetrics:
    """Tests that DashScope streaming path reads cache metrics from response."""

    async def test_stream_captures_cache_metrics(self):
        chunks = [
            ns(
                usage=None,
                choices=[ns(finish_reason=None, delta=ns(content="hi", tool_calls=None))],
            ),
            ns(
                usage=ns(
                    prompt_tokens=1000,
                    completion_tokens=50,
                    prompt_tokens_details=ns(cached_tokens=800, cache_creation_input_tokens=0),
                ),
                choices=[ns(finish_reason="stop", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model="qwen3.5-plus", api_key="k")
        provider._client = client

        events = [e async for e in provider.stream(messages=[Message.user("test")], system="sys")]
        end = events[-1]
        assert end.type == "message_end"
        assert end.usage.cache_read_input_tokens == 800
        assert end.usage.cache_creation_input_tokens == 0
        assert end.usage.input_tokens == 1000
        assert end.usage.total_input_tokens == 1000
        assert end.usage.standard_input_tokens == 200
        assert end.usage.total_tokens == 1850
        assert end.usage.normalized_total_tokens == 1050
        assert end.usage.cache_hit_rate == 0.8
        assert end.usage.usage_reported is True

    async def test_stream_without_cache_details(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=100, completion_tokens=10),
                choices=[ns(finish_reason="stop", delta=ns(content="x", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = DashScopeProvider(model="deepseek-v4-pro", api_key="k")
        provider._client = client

        events = [e async for e in provider.stream(messages=[Message.user("test")], system="sys")]
        end = events[-1]
        assert end.usage.cache_read_input_tokens == 0
        assert end.usage.cache_creation_input_tokens == 0
