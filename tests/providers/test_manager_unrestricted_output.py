"""Legacy settings must not reintroduce client-side output budgets."""

import pytest

from iac_code.providers.base import Message
from iac_code.providers.manager import create_provider
from tests.providers._fakes import FakeOpenAIClient, ns
from tests.providers._responses_fakes import FakeResponsesClient, response, terminal


@pytest.mark.asyncio
class TestManagerUnrestrictedOutput:
    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
    async def test_legacy_model_and_provider_limits_do_not_reach_sdk(self, monkeypatch, operation, api_mode):
        client = FakeOpenAIClient(
            stream_chunks=[ns(usage=None, choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))])],
            create_response=ns(
                id="cmpl_1", choices=[ns(finish_reason="stop", message=ns(content="ok", tool_calls=None))], usage=None
            ),
        )
        if api_mode == "responses":
            client = FakeResponsesClient([terminal(response())] if operation == "stream" else response())
        monkeypatch.setattr("iac_code.providers.openai_provider.AsyncOpenAI", lambda **kwargs: client)
        provider = create_provider(
            "deepseek-v4.1-flash-1017",
            credentials={"dashscope": "fake-key"},
            provider_key_override="dashscope",
            provider_config_override={
                "maxCompletionTokens": 512,
                "models": {"deepseek-v4.1-flash-1017": {"maxCompletionTokens": 8192, "apiMode": api_mode}},
            },
        )

        if operation == "stream":
            _ = [event async for event in provider.stream([Message.user("hello")], "system")]
        else:
            await provider.complete([Message.user("hello")], "system")

        request = client.responses.calls[0] if api_mode == "responses" else client.chat.completions.calls[0]
        assert "max_tokens" not in request
        assert "max_completion_tokens" not in request
        assert "max_output_tokens" not in request
