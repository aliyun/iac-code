"""Offline SDK boundary contracts for GLM output limits."""

import pytest

from iac_code.providers.base import Message
from iac_code.providers.dashscope_provider import DashScopeProvider
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.openrouter_provider import OpenRouterProvider
from iac_code.providers.responses_provider import DashScopeResponsesProvider, ResponsesProvider
from iac_code.providers.zhipu_provider import ZhiPuProvider
from tests.providers._fakes import FakeOpenAIClient, ns
from tests.providers._responses_fakes import FakeResponsesClient, response, terminal


@pytest.mark.asyncio
class TestGlmChatOutputLimits:
    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize(
        "provider_cls,provider_key,model",
        [
            (DashScopeProvider, "dashscope", "glm-5.3"),
            (DashScopeProvider, "dashscope", "ZHIPU/GLM-5.3"),
            (DashScopeProvider, "dashscope", "zhipu/glm-5.3-flash"),
            (DashScopeProvider, "dashscope", "glm-5.2"),
            (DashScopeProvider, "dashscope_token_plan", "glm-5.2"),
            (DashScopeProvider, "aliyun_codingplan", "glm-5"),
            (ZhiPuProvider, "zhipu_cn", "glm-5.3"),
            (ZhiPuProvider, "zhipu_intl", "GLM-5.3-FLASH"),
            (ZhiPuProvider, "zhipu_cn_codingplan", "glm-5.2"),
            (ZhiPuProvider, "zhipu_intl_codingplan", "glm-4.7"),
            (ZhiPuProvider, "zhipu_cn", "glm-4v"),
            (ZhiPuProvider, "zhipu_cn", "glm-4.6v"),
            (OpenAIProvider, "openai_compatible", "GLM-4.1V-THINKING"),
            (OpenAIProvider, "openai_compatible", "glm-5.3"),
            (OpenRouterProvider, "openrouter", "z-ai/glm-5"),
        ],
    )
    async def test_default_glm_request_omits_output_limits(
        self, monkeypatch, operation, provider_cls, provider_key, model
    ):
        client = FakeOpenAIClient(
            stream_chunks=[ns(usage=None, choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))])],
            create_response=ns(
                id="cmpl_1", choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))], usage=None
            ),
        )
        monkeypatch.setattr("iac_code.providers.openai_provider.AsyncOpenAI", lambda **kwargs: client)
        provider = provider_cls(model=model, provider_key=provider_key, client=client)

        if operation == "stream":
            _ = [event async for event in provider.stream([Message.user("hello")], "system")]
        else:
            await provider.complete([Message.user("hello")], "system")

        request = client.chat.completions.calls[0]
        assert "max_tokens" not in request
        assert "max_completion_tokens" not in request

    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize("configured_limit", [None, 8192])
    @pytest.mark.parametrize(
        "model,is_glm",
        [
            ("z-ai/glm-4.5-air:free", True),
            ("~z-ai/glm-latest", True),
            ("~z-ai/glm-flash-latest", True),
            ("custom-glm-5:free", False),
            ("glm-custom:free", False),
        ],
    )
    async def test_openrouter_routes_preserve_explicit_limits(self, operation, configured_limit, model, is_glm):
        client = FakeOpenAIClient(
            stream_chunks=[ns(usage=None, choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))])],
            create_response=ns(
                id="cmpl_1", choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))], usage=None
            ),
        )
        provider = OpenRouterProvider(model=model, client=client, max_completion_tokens=configured_limit)

        if operation == "stream":
            _ = [event async for event in provider.stream([Message.user("hello")], "system")]
        else:
            await provider.complete([Message.user("hello")], "system")

        request = client.chat.completions.calls[0]
        if is_glm and configured_limit is None:
            assert "max_tokens" not in request
        else:
            assert request["max_tokens"] == 8192
        assert "max_completion_tokens" not in request

    @pytest.mark.parametrize("model", ["glm-5.3", "glm-5.2"])
    @pytest.mark.parametrize(
        "configured_limit,call_limit,want", [(None, 512, 512), (32768, 8192, 32768), (8192, 512, 8192)]
    )
    async def test_explicit_glm_limits_remain_effective(self, model, configured_limit, call_limit, want):
        client = FakeOpenAIClient(
            create_response=ns(
                id="cmpl_1", choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))], usage=None
            )
        )
        provider = DashScopeProvider(model=model, client=client, max_completion_tokens=configured_limit)

        await provider.complete([Message.user("hello")], "system", max_tokens=call_limit)

        request = client.chat.completions.calls[0]
        key = "max_completion_tokens" if model == "glm-5.2" else "max_tokens"
        assert request[key] == want

    @pytest.mark.parametrize("model,want", [("qwen3.6-plus", 8192), ("custom-glm-5.3", 8192), ("glm-custom", 8192)])
    async def test_other_models_keep_the_request_default(self, model, want):
        client = FakeOpenAIClient(
            create_response=ns(
                id="cmpl_1", choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))], usage=None
            )
        )
        provider = DashScopeProvider(model=model, client=client)

        await provider.complete([Message.user("hello")], "system")

        assert client.chat.completions.calls[0]["max_tokens"] == want

    async def test_small_limit_does_not_leak_into_next_request(self):
        client = FakeOpenAIClient(
            create_response=ns(
                id="cmpl_1", choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))], usage=None
            )
        )
        provider = DashScopeProvider(model="glm-5.3", client=client)

        await provider.complete([Message.user("first")], "system")
        await provider.complete([Message.user("short")], "system", max_tokens=512)
        await provider.complete([Message.user("next")], "system")

        first, short, next_request = client.chat.completions.calls
        assert "max_tokens" not in first
        assert short["max_tokens"] == 512
        assert "max_tokens" not in next_request


@pytest.mark.asyncio
class TestGlmResponsesOutputLimits:
    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize("configured_limit", [None, 8192])
    @pytest.mark.parametrize(
        "model,is_glm",
        [
            ("z-ai/glm-4.5-air:free", True),
            ("~z-ai/glm-latest", True),
            ("~z-ai/glm-flash-latest", True),
            ("custom-glm-5:free", False),
            ("glm-custom:free", False),
        ],
    )
    async def test_openrouter_routes_preserve_explicit_limits(self, operation, configured_limit, model, is_glm):
        data = response()
        client = FakeResponsesClient([terminal(data)] if operation == "stream" else data)
        provider = ResponsesProvider(
            model=model, provider_key="openrouter", client=client, max_completion_tokens=configured_limit
        )

        if operation == "stream":
            _ = [event async for event in provider.stream([Message.user("hello")], "system")]
        else:
            await provider.complete([Message.user("hello")], "system")

        request = client.calls[0]
        if is_glm and configured_limit is None:
            assert "max_output_tokens" not in request
        else:
            assert request["max_output_tokens"] == 8192

    @pytest.mark.parametrize("model", ["glm-5.3", "gpt-6-sol"])
    @pytest.mark.parametrize("call_limit", [None, 0, -1, True, "invalid"])
    async def test_invalid_explicit_limits_still_fail_before_sdk(self, model, call_limit):
        client = FakeResponsesClient(response())
        provider = ResponsesProvider(model=model, client=client)

        with pytest.raises(ValueError, match="positive integer"):
            await provider.complete([Message.user("hello")], "system", max_tokens=call_limit)

        assert client.calls == []

    @pytest.mark.parametrize("provider_cls", [ResponsesProvider, DashScopeResponsesProvider])
    @pytest.mark.parametrize("operation", ["stream", "complete"])
    async def test_default_glm_request_omits_output_limit(self, provider_cls, operation):
        data = response()
        client = FakeResponsesClient([terminal(data)] if operation == "stream" else data)
        provider = provider_cls(model="glm-5.3", client=client)

        if operation == "stream":
            _ = [event async for event in provider.stream([Message.user("hello")], "system")]
        else:
            await provider.complete([Message.user("hello")], "system")

        assert "max_output_tokens" not in client.calls[0]

    @pytest.mark.parametrize("provider_cls", [ResponsesProvider, DashScopeResponsesProvider])
    @pytest.mark.parametrize(
        "configured_limit,call_limit,want", [(None, 512, 512), (32768, 8192, 32768), (8192, 512, 8192)]
    )
    async def test_explicit_glm_limits_remain_effective(self, provider_cls, configured_limit, call_limit, want):
        client = FakeResponsesClient(response())
        provider = provider_cls(model="glm-5.3", client=client, max_completion_tokens=configured_limit)

        await provider.complete([Message.user("hello")], "system", max_tokens=call_limit)

        assert client.calls[0]["max_output_tokens"] == want

    async def test_small_limit_does_not_leak_into_next_request(self):
        client = FakeResponsesClient(response(), response(), response())
        provider = DashScopeResponsesProvider(model="glm-5.3", client=client)

        await provider.complete([Message.user("first")], "system")
        await provider.complete([Message.user("short")], "system", max_tokens=512)
        await provider.complete([Message.user("next")], "system")

        first, short, next_request = client.calls
        assert "max_output_tokens" not in first
        assert short["max_output_tokens"] == 512
        assert "max_output_tokens" not in next_request
