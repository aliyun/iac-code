"""Tests for DeepSeek provider — thinking mode + OpenAI-compat round-trip."""

import pytest

from iac_code.providers.base import ContentBlock, Message
from iac_code.providers.deepseek_provider import (
    DEEPSEEK_BASE_URL,
    DeepSeekProvider,
)
from iac_code.providers.openai_provider import OpenAIProvider
from tests.providers._fakes import FakeOpenAIClient, ns


class TestDeepSeekProvider:
    def test_inherits_openai_provider(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        assert isinstance(p, OpenAIProvider)

    def test_uses_deepseek_base_url(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        assert str(p._client.base_url).rstrip("/") == DEEPSEEK_BASE_URL.rstrip("/")

    def test_supports_stream_options(self):
        assert DeepSeekProvider.supports_stream_options is True

    def test_effort_request_kwargs_none_without_effort(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        assert p._effort_request_kwargs() == {}

    def test_effort_request_kwargs_high(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test", effort="high")
        assert p._effort_request_kwargs() == {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def test_effort_request_kwargs_max(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test", effort="max")
        assert p._effort_request_kwargs() == {
            "reasoning_effort": "max",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def test_effort_request_kwargs_low_falls_back_to_default_high(self):
        # DeepSeek allowed_efforts = (HIGH, MAX), default = HIGH.
        # An out-of-range value falls back to default rather than dropping.
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test", effort="low")
        assert p._effort_request_kwargs() == {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def test_effort_request_kwargs_xhigh_falls_back_to_default_high(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test", effort="xhigh")
        assert p._effort_request_kwargs() == {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }


class TestReasoningContentRoundTrip:
    def test_assistant_message_echoes_reasoning_content(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        blocks = [
            ContentBlock(type="thinking", text="inner thoughts"),
            ContentBlock(type="text", text="final answer"),
            ContentBlock(type="tool_use", tool_use_id="t1", name="fn", input={"x": 1}),
        ]
        out = p._convert_content_blocks("assistant", blocks)
        assert len(out) == 1
        assert out[0]["role"] == "assistant"
        assert out[0]["content"] == "final answer"
        assert out[0]["reasoning_content"] == "inner thoughts"
        assert out[0]["tool_calls"][0]["function"]["name"] == "fn"

    def test_thinking_only_assistant_message(self):
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        blocks = [ContentBlock(type="thinking", text="just thinking")]
        out = p._convert_content_blocks("assistant", blocks)
        assert len(out) == 1
        assert out[0]["content"] is None
        assert out[0]["reasoning_content"] == "just thinking"


@pytest.mark.asyncio
class TestDeepSeekStream:
    async def test_reasoning_content_emits_thinking_delta(self):
        chunks = [
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(content=None, tool_calls=None, reasoning_content="step 1 "),
                    )
                ],
            ),
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(content=None, tool_calls=None, reasoning_content="step 2"),
                    )
                ],
            ),
            ns(
                usage=None,
                choices=[ns(finish_reason=None, delta=ns(content="done", tool_calls=None))],
            ),
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        p._client = client

        out = [e async for e in p.stream(messages=[Message.user("hi")], system="")]
        types = [e.type for e in out]
        assert "thinking_delta" in types
        thinking = [e for e in out if e.type == "thinking_delta"]
        assert "".join(e.text for e in thinking) == "step 1 step 2"
        text = [e for e in out if e.type == "text_delta"]
        assert "".join(e.text for e in text) == "done"

    async def test_effort_high_injects_reasoning_effort(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test", effort="high")
        p._client = client

        _ = [e async for e in p.stream(messages=[Message.user("hi")], system="")]
        call_kwargs = client.chat.completions.calls[0]
        assert call_kwargs.get("reasoning_effort") == "high"
        assert call_kwargs.get("extra_body") == {"thinking": {"type": "enabled"}}

    async def test_no_effort_does_not_inject_reasoning_effort(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        p._client = client

        _ = [e async for e in p.stream(messages=[Message.user("hi")], system="")]
        call_kwargs = client.chat.completions.calls[0]
        assert "reasoning_effort" not in call_kwargs


@pytest.mark.asyncio
class TestDeepSeekComplete:
    async def test_reasoning_content_captured(self):
        response = ns(
            id="cmpl_1",
            choices=[
                ns(
                    finish_reason="stop",
                    message=ns(content="answer", tool_calls=None, reasoning_content="my cot"),
                )
            ],
            usage=ns(prompt_tokens=1, completion_tokens=1),
        )
        client = FakeOpenAIClient(create_response=response)
        p = DeepSeekProvider(model="deepseek-v4-pro", api_key="test")
        p._client = client

        result = await p.complete(messages=[Message.user("hi")], system="")
        assert result.text == "answer"
        assert result.thinking == "my cot"


class TestProviderDefinitions:
    def test_deepseek_listed_in_providers(self):
        from iac_code.commands.auth import PROVIDERS

        names = [p["name"] for p in PROVIDERS]
        assert "DeepSeek" in names
        deepseek = next(p for p in PROVIDERS if p["name"] == "DeepSeek")
        assert deepseek["key_name"] == "deepseek"
        assert deepseek["api_base"] == DEEPSEEK_BASE_URL
        assert set(deepseek["models"]) == {"deepseek-v4-pro", "deepseek-v4-flash"}

    def test_deepseek_model_capabilities(self):
        from iac_code.providers.thinking import EffortLevel, get_thinking_spec

        for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
            spec = get_thinking_spec("deepseek", model)
            assert spec.supports_effort is True
            assert spec.effort_range == (EffortLevel.HIGH, EffortLevel.MAX)
            assert spec.default_effort == EffortLevel.HIGH
