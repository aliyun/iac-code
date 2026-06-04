import json

import pytest

from iac_code.providers.base import Message, ToolDefinition
from iac_code.providers.openai_provider import OpenAIProvider
from tests.providers._fakes import FakeOpenAIClient, ns


class TestOpenAIProvider:
    def test_get_model_name(self):
        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        assert p.get_model_name() == "gpt-4.1"

    def test_convert_messages_user(self):
        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        msgs = [Message.user("Hello")]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "user"
        assert api[0]["content"] == "Hello"

    def test_convert_tools(self):
        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        tools = [ToolDefinition(name="bash", description="Run", input_schema={"type": "object"})]
        api = p._convert_tools(tools)
        assert api[0]["type"] == "function"
        assert api[0]["function"]["name"] == "bash"
        assert api[0]["function"]["description"] == "Run"
        assert api[0]["function"]["parameters"] == {"type": "object"}

    def test_convert_tool_use_message(self):
        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        msgs = [Message.assistant_tool_use(tool_use_id="t1", name="bash", input={"cmd": "ls"})]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "assistant"
        assert api[0]["tool_calls"][0]["id"] == "t1"
        assert api[0]["tool_calls"][0]["type"] == "function"
        assert api[0]["tool_calls"][0]["function"]["name"] == "bash"
        assert json.loads(api[0]["tool_calls"][0]["function"]["arguments"]) == {"cmd": "ls"}

    def test_convert_tool_result(self):
        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        msgs = [Message.tool_result(tool_use_id="t1", content="output", is_error=False)]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "tool"
        assert api[0]["tool_call_id"] == "t1"
        assert api[0]["content"] == "output"

    def test_convert_assistant_text_message(self):
        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        msgs = [Message.assistant_text("Hello world")]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "assistant"
        assert api[0]["content"] == "Hello world"

    def test_convert_assistant_with_thinking_block(self):
        from iac_code.providers.base import ContentBlock

        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        blocks = [
            ContentBlock(type="thinking", text="my reasoning"),
            ContentBlock(type="text", text="hello"),
        ]
        api = p._convert_content_blocks("assistant", blocks)
        assert api[0]["role"] == "assistant"
        assert api[0]["content"] == "hello"
        assert api[0]["reasoning_content"] == "my reasoning"

    def test_convert_multiple_messages(self):
        p = OpenAIProvider(model="gpt-4.1", api_key="test")
        msgs = [
            Message.user("Hi"),
            Message.assistant_text("Hello!"),
            Message.user("How are you?"),
        ]
        api = p._convert_messages(msgs)
        assert len(api) == 3
        assert api[0]["role"] == "user"
        assert api[1]["role"] == "assistant"
        assert api[2]["role"] == "user"


class TestOpenAIBuildThinkingKwargs:
    def test_medium_returns_reasoning_effort_and_extra_body(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="medium")
        assert p._build_thinking_kwargs() == {
            "reasoning_effort": "medium",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def test_xhigh_returns_extras(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="xhigh")
        assert p._build_thinking_kwargs() == {
            "reasoning_effort": "xhigh",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def test_no_effort_returns_empty(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort=None)
        assert p._build_thinking_kwargs() == {}

    def test_auto_returns_empty(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="auto")
        assert p._build_thinking_kwargs() == {}

    def test_unknown_effort_falls_back_to_default(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="ultra")
        assert p._build_thinking_kwargs() == {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def test_unknown_model_returns_empty(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="some-unknown-model", api_key="k", effort="high")
        assert p._build_thinking_kwargs() == {}

    def test_effort_request_kwargs_delegates_to_build_thinking_kwargs(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="high")
        assert p._effort_request_kwargs() == p._build_thinking_kwargs()


@pytest.mark.asyncio
class TestOpenAIStream:
    async def test_text_chunks_and_usage(self):
        chunks = [
            ns(
                usage=None,
                choices=[ns(finish_reason=None, delta=ns(content="Hello ", tool_calls=None))],
            ),
            ns(
                usage=None,
                choices=[ns(finish_reason=None, delta=ns(content="world", tool_calls=None))],
            ),
            ns(
                usage=ns(prompt_tokens=3, completion_tokens=2),
                choices=[ns(finish_reason="stop", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("hi")], system="sys")]

        types = [e.type for e in out]
        assert types == ["message_start", "text_delta", "text_delta", "message_end"]
        assert out[1].text == "Hello "
        assert out[2].text == "world"
        assert out[-1].stop_reason == "end_turn"
        assert out[-1].usage.input_tokens == 3
        assert out[-1].usage.output_tokens == 2

    async def test_tool_call_accumulation(self):
        chunks = [
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(
                            content=None,
                            tool_calls=[
                                ns(
                                    index=0,
                                    id="call_1",
                                    function=ns(name="bash", arguments='{"cmd":'),
                                )
                            ],
                        ),
                    )
                ],
            ),
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(
                            content=None,
                            tool_calls=[
                                ns(
                                    index=0,
                                    id=None,
                                    function=ns(name=None, arguments='"ls"}'),
                                )
                            ],
                        ),
                    )
                ],
            ),
            ns(
                usage=ns(prompt_tokens=5, completion_tokens=3),
                choices=[ns(finish_reason="tool_calls", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("run")], system="")]

        types = [e.type for e in out]
        assert types == [
            "message_start",
            "tool_use_start",
            "tool_input_delta",
            "tool_input_delta",
            "tool_use_end",
            "message_end",
        ]
        assert out[1].tool_use_id == "call_1"
        assert out[1].name == "bash"
        end_tool = out[-2]
        assert end_tool.tool_use_id == "call_1"
        assert end_tool.input == {"cmd": "ls"}
        assert out[-1].stop_reason == "tool_use"

    async def test_finish_reason_length_maps_to_max_tokens(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="length", delta=ns(content="x", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("x")], system="")]

        assert out[-1].stop_reason == "max_tokens"

    async def test_reasoning_content_delta_emits_thinking_event(self):
        chunks = [
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(content=None, tool_calls=None, reasoning_content="cot "),
                    )
                ],
            ),
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(content="answer", tool_calls=None, reasoning_content=None),
                    )
                ],
            ),
            ns(
                usage=ns(prompt_tokens=1, completion_tokens=1),
                choices=[ns(finish_reason="stop", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = OpenAIProvider(model="gpt-x", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("hi")], system="")]
        types = [e.type for e in out]
        assert types.count("thinking_delta") == 1
        thinking = next(e for e in out if e.type == "thinking_delta")
        assert thinking.text == "cot "

    async def test_empty_response_raises_runtime_error(self):
        client = FakeOpenAIClient(stream_chunks=[], base_url="https://api.example.com")
        provider = OpenAIProvider(
            model="gpt-4.1",
            base_url="https://api.example.com",
            client=client,
        )

        gen = provider.stream(messages=[Message.user("hi")], system="")
        with pytest.raises(RuntimeError, match="API returned no data"):
            async for _ev in gen:
                pass


@pytest.mark.asyncio
class TestOpenAIComplete:
    async def test_text_response(self):
        response = ns(
            id="cmpl_1",
            choices=[
                ns(
                    finish_reason="stop",
                    message=ns(content="hello", tool_calls=None),
                )
            ],
            usage=ns(prompt_tokens=2, completion_tokens=1),
        )
        client = FakeOpenAIClient(create_response=response)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        result = await provider.complete(messages=[Message.user("hi")], system="sys")

        assert result.message_id == "cmpl_1"
        assert result.text == "hello"
        assert result.tool_uses == []
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 2
        assert result.usage.output_tokens == 1

    async def test_tool_calls_response(self):
        response = ns(
            id="cmpl_2",
            choices=[
                ns(
                    finish_reason="tool_calls",
                    message=ns(
                        content=None,
                        tool_calls=[
                            ns(
                                id="call_x",
                                function=ns(name="bash", arguments='{"cmd":"ls"}'),
                            )
                        ],
                    ),
                )
            ],
            usage=ns(prompt_tokens=3, completion_tokens=2),
        )
        client = FakeOpenAIClient(create_response=response)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        result = await provider.complete(messages=[Message.user("x")], system="")

        assert result.stop_reason == "tool_use"
        assert result.text == ""
        assert result.tool_uses == [{"id": "call_x", "name": "bash", "input": {"cmd": "ls"}}]

    async def test_finish_reason_length(self):
        response = ns(
            id="cmpl_3",
            choices=[ns(finish_reason="length", message=ns(content="x", tool_calls=None))],
            usage=ns(prompt_tokens=1, completion_tokens=1),
        )
        client = FakeOpenAIClient(create_response=response)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        result = await provider.complete(messages=[Message.user("x")], system="")
        assert result.stop_reason == "max_tokens"

    async def test_invalid_response_raises_runtime_error(self):
        # response has no "choices" attribute — triggers base_url hint path
        response = ns(id="x")
        client = FakeOpenAIClient(create_response=response, base_url="https://api.example.com")
        provider = OpenAIProvider(
            model="gpt-4.1",
            base_url="https://api.example.com",
            client=client,
        )

        with pytest.raises(RuntimeError, match="invalid response"):
            await provider.complete(messages=[Message.user("x")], system="")

    async def test_empty_choices_raises_runtime_error(self):
        response = ns(id="x", choices=[], usage=None)
        client = FakeOpenAIClient(create_response=response, base_url="https://api.example.com")
        provider = OpenAIProvider(
            model="gpt-4.1",
            base_url="https://api.example.com",
            client=client,
        )

        with pytest.raises(RuntimeError, match="invalid response.*choices"):
            await provider.complete(messages=[Message.user("x")], system="")


@pytest.mark.asyncio
class TestOpenAICacheMetrics:
    """Tests for prompt_tokens_details (cache metrics) parsing."""

    async def test_stream_reads_cached_tokens(self):
        chunks = [
            ns(
                usage=ns(
                    prompt_tokens=500,
                    completion_tokens=20,
                    prompt_tokens_details=ns(cached_tokens=300, cache_creation_input_tokens=100),
                ),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("hi")], system="sys")]
        end = out[-1]
        assert end.usage.cache_read_input_tokens == 300
        assert end.usage.cache_creation_input_tokens == 100

    async def test_stream_without_details_defaults_to_zero(self):
        chunks = [
            ns(
                usage=ns(prompt_tokens=100, completion_tokens=10),
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("hi")], system="sys")]
        end = out[-1]
        assert end.usage.cache_read_input_tokens == 0
        assert end.usage.cache_creation_input_tokens == 0

    async def test_complete_reads_cached_tokens(self):
        response = ns(
            id="cmpl_cache",
            choices=[ns(finish_reason="stop", message=ns(content="hi", tool_calls=None))],
            usage=ns(
                prompt_tokens=500,
                completion_tokens=20,
                prompt_tokens_details=ns(cached_tokens=400, cache_creation_input_tokens=0),
            ),
        )
        client = FakeOpenAIClient(create_response=response)
        provider = OpenAIProvider(model="gpt-4.1", client=client)

        result = await provider.complete(messages=[Message.user("hi")], system="sys")
        assert result.usage.cache_read_input_tokens == 400
        assert result.usage.cache_creation_input_tokens == 0
