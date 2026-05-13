"""Tests for DashScope provider — OpenAI-compatible endpoint."""

from iac_code.providers.base import Message, ToolDefinition
from iac_code.providers.dashscope_provider import (
    DASHSCOPE_BASE_URL,
    DashScopeProvider,
)
from iac_code.providers.openai_provider import OpenAIProvider


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
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True}}

    def test_qwen_with_effort_still_only_enable_thinking(self):
        # Bailian Qwen does not honor effort — provider ignores it gracefully.
        p = DashScopeProvider(model="qwen3.6-plus", api_key="k", effort="high")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True}}

    def test_kimi(self):
        p = DashScopeProvider(model="kimi-k2.6", api_key="k")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True}}

    def test_glm(self):
        p = DashScopeProvider(model="glm-5.1", api_key="k")
        assert p._build_thinking_kwargs() == {"extra_body": {"enable_thinking": True}}

    def test_bailian_deepseek_does_not_emit_reasoning_effort(self):
        # Bailian-hosted DeepSeek uses the BAILIAN wire format, not OpenAI's.
        p = DashScopeProvider(model="deepseek-v4-pro", api_key="k", effort="high")
        kwargs = p._build_thinking_kwargs()
        assert kwargs == {"extra_body": {"enable_thinking": True}}
        assert "reasoning_effort" not in kwargs

    def test_unknown_model_returns_empty(self):
        p = DashScopeProvider(model="not-real", api_key="k")
        assert p._build_thinking_kwargs() == {}

    def test_effort_request_kwargs_delegates(self):
        p = DashScopeProvider(model="qwen3.6-plus", api_key="k")
        assert p._effort_request_kwargs() == p._build_thinking_kwargs()


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
