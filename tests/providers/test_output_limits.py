"""Offline SDK contracts: optional protocols use the server's output limit."""

from importlib import import_module

import pytest

from iac_code.providers.base import Message
from iac_code.providers.registry import PROVIDER_REGISTRY
from iac_code.providers.responses_provider import DashScopeResponsesProvider, ResponsesProvider
from tests.providers._fakes import FakeOpenAIClient, ns
from tests.providers._responses_fakes import FakeResponsesClient, response, terminal

_OUTPUT_LIMIT_FIELDS = {"max_tokens", "max_completion_tokens", "max_output_tokens"}
_CHAT_PROVIDERS = [
    (descriptor.provider_class, descriptor.key, descriptor.default_model)
    for descriptor in PROVIDER_REGISTRY.values()
    if descriptor.provider_class.rsplit(".", 1)[-1] not in {"AnthropicProvider", "MiniMaxProvider"}
] + [
    ("iac_code.providers.qwen_provider.QwenProvider", "dashscope", "qwen3.8-max"),
    ("iac_code.providers.openai_provider.OpenAIProvider", "openai_compatible", "custom-model"),
    ("iac_code.providers.openrouter_provider.OpenRouterProvider", "openrouter", "z-ai/glm-4.5-air:free"),
    ("iac_code.providers.openrouter_provider.OpenRouterProvider", "openrouter", "vendor/custom-model:free"),
    ("iac_code.providers.dashscope_provider.DashScopeProvider", "dashscope", "glm-5.2"),
    ("iac_code.providers.dashscope_provider.DashScopeProvider", "dashscope", "kimi-k2.7-code"),
]


@pytest.mark.asyncio
class TestOptionalOutputLimits:
    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize("thinking_enabled", [False, True])
    @pytest.mark.parametrize("class_path,provider_key,model", _CHAT_PROVIDERS)
    async def test_chat_request_uses_server_output_limit(
        self, monkeypatch, operation, thinking_enabled, class_path, provider_key, model
    ):
        client = FakeOpenAIClient(
            stream_chunks=[ns(usage=None, choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))])],
            create_response=ns(
                id="cmpl_1", choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))], usage=None
            ),
        )
        monkeypatch.setattr("iac_code.providers.openai_provider.AsyncOpenAI", lambda **kwargs: client)
        monkeypatch.setattr("iac_code.providers.openrouter_provider.AsyncOpenAI", lambda **kwargs: client)
        module_name, class_name = class_path.rsplit(".", 1)
        provider = getattr(import_module(module_name), class_name)(
            model=model,
            provider_key=provider_key,
            client=client,
            thinking_enabled=thinking_enabled,
            thinking_budget=2048,
        )

        if operation == "stream":
            _ = [event async for event in provider.stream([Message.user("hello")], "system")]
        else:
            await provider.complete([Message.user("hello")], "system")

        assert len(client.chat.completions.calls) == 1
        assert _OUTPUT_LIMIT_FIELDS.isdisjoint(client.chat.completions.calls[0])

    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize("thinking_enabled", [False, True])
    @pytest.mark.parametrize("provider_cls", [ResponsesProvider, DashScopeResponsesProvider])
    @pytest.mark.parametrize("model", ["gpt-5.5", "glm-5.3", "qwen3.8-max", "custom-model"])
    async def test_responses_request_uses_server_output_limit(
        self, operation, thinking_enabled, provider_cls, model
    ):
        data = response()
        client = FakeResponsesClient([terminal(data)] if operation == "stream" else data)
        provider = provider_cls(model=model, client=client, thinking_enabled=thinking_enabled)

        if operation == "stream":
            _ = [event async for event in provider.stream([Message.user("hello")], "system")]
        else:
            await provider.complete([Message.user("hello")], "system")

        assert len(client.calls) == 1
        assert _OUTPUT_LIMIT_FIELDS.isdisjoint(client.calls[0])
