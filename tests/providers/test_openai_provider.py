import json

import pytest

from iac_code.providers.base import Message, ToolDefinition
from iac_code.providers.openai_provider import OpenAIProvider
from tests.providers._fakes import FakeOpenAIClient, ns


class TestOpenAIProvider:
    def test_created_client_preserves_sdk_retry_default(self, monkeypatch):
        calls = []

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.base_url = kwargs.get("base_url") or "https://fake.openai.local"

        monkeypatch.setattr("iac_code.providers.openai_provider.AsyncOpenAI", FakeAsyncOpenAI)

        OpenAIProvider(model="gpt-4.1", api_key="test")

        assert "max_retries" not in calls[0]

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

    def test_gemini_tool_call_echoes_thought_signature(self):
        from iac_code.providers.base import ContentBlock
        from iac_code.providers.gemini_provider import GeminiProvider

        provider = GeminiProvider(model="gemini-3-flash-preview", api_key="test")
        provider_metadata = provider._gemini_provider_metadata({"google": {"thought_signature": "signed-thought"}})
        blocks = [
            ContentBlock(
                type="tool_use",
                tool_use_id="call_1",
                name="bash",
                input={"cmd": "ls"},
                provider_metadata=provider_metadata,
            )
        ]

        api = provider._convert_content_blocks("assistant", blocks)

        assert api[0]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "signed-thought"}}

    def test_gemini_message_echoes_non_tool_thought_signature(self):
        from iac_code.providers.base import ContentBlock
        from iac_code.providers.gemini_provider import GeminiProvider

        provider = GeminiProvider(model="gemini-3-flash-preview", api_key="test")
        provider_metadata = provider._gemini_provider_metadata({"google": {"thought_signature": "signed-text-thought"}})
        blocks = [
            ContentBlock(
                type="thinking",
                text="",
                provider_metadata=provider_metadata,
            ),
            ContentBlock(type="text", text="answer"),
        ]

        api = provider._convert_content_blocks("assistant", blocks)

        assert api == [
            {
                "role": "assistant",
                "content": "answer",
                "extra_content": {"google": {"thought_signature": "signed-text-thought"}},
            }
        ]

    def test_gemini_metadata_is_scoped_to_endpoint(self):
        from iac_code.providers.base import ContentBlock
        from iac_code.providers.gemini_provider import GeminiProvider

        source = GeminiProvider(
            model="gemini-3-flash-preview",
            api_key="test",
            base_url="https://first.example/v1",
        )
        target = GeminiProvider(
            model="gemini-3-flash-preview",
            api_key="test",
            base_url="https://second.example/v1",
        )
        blocks = [
            ContentBlock(
                type="tool_use",
                tool_use_id="call_1",
                name="bash",
                input={},
                provider_metadata=source._gemini_provider_metadata({"google": {"thought_signature": "signed-thought"}}),
            )
        ]

        api = target._convert_content_blocks("assistant", blocks)

        assert "extra_content" not in api[0]["tool_calls"][0]

    def test_gemini_metadata_is_scoped_to_model(self):
        from iac_code.providers.base import ContentBlock
        from iac_code.providers.gemini_provider import GeminiProvider

        source = GeminiProvider(model="gemini-3-flash-preview", api_key="test")
        target = GeminiProvider(model="gemini-3.1-pro-preview", api_key="test")
        provider_metadata = source._gemini_provider_metadata(
            {"google": {"thought_signature": "signed-for-source-model"}}
        )
        blocks = [
            ContentBlock(type="thinking", text="", provider_metadata=provider_metadata),
            ContentBlock(type="text", text="answer"),
            ContentBlock(
                type="tool_use",
                tool_use_id="call_1",
                name="bash",
                input={},
                provider_metadata=provider_metadata,
            ),
        ]

        api = target._convert_content_blocks("assistant", blocks)

        assert "extra_content" not in api[0]
        assert "extra_content" not in api[0]["tool_calls"][0]

    def test_non_gemini_provider_does_not_echo_gemini_metadata(self):
        from iac_code.providers.base import ContentBlock

        provider = OpenAIProvider(model="gpt-5.4", api_key="test")
        blocks = [
            ContentBlock(
                type="tool_use",
                tool_use_id="call_1",
                name="bash",
                input={},
                provider_metadata={
                    "provider": "gemini",
                    "extra_content": {"google": {"thought_signature": "signed-thought"}},
                },
            )
        ]

        api = provider._convert_content_blocks("assistant", blocks)

        assert "extra_content" not in api[0]["tool_calls"][0]

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
    def test_medium_returns_reasoning_effort(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="medium")
        assert p._build_thinking_kwargs() == {"reasoning_effort": "medium"}

    def test_xhigh_returns_extras(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="xhigh")
        assert p._build_thinking_kwargs() == {"reasoning_effort": "xhigh"}

    def test_no_effort_returns_empty(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort=None)
        assert p._build_thinking_kwargs() == {}

    def test_enabled_true_uses_default_effort(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", thinking_enabled=True)
        assert p._build_thinking_kwargs() == {"reasoning_effort": "medium"}

    def test_enabled_false_sends_none_when_model_supports_it(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="high", thinking_enabled=False)
        assert p._build_thinking_kwargs() == {"reasoning_effort": "none"}

        latest = OpenAIProvider(model="gpt-5.6", api_key="k", thinking_enabled=False)
        assert latest._build_thinking_kwargs() == {"reasoning_effort": "none"}

    def test_enabled_false_omits_effort_when_model_cannot_disable_reasoning(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="o3", api_key="k", effort="high", thinking_enabled=False)
        assert p._build_thinking_kwargs() == {}

    def test_auto_returns_empty(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="auto")
        assert p._build_thinking_kwargs() == {}

    def test_unknown_effort_falls_back_to_default(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="ultra")
        assert p._build_thinking_kwargs() == {"reasoning_effort": "medium"}

    def test_unknown_model_returns_empty(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="some-unknown-model", api_key="k", effort="high")
        assert p._build_thinking_kwargs() == {}

    def test_effort_request_kwargs_delegates_to_build_thinking_kwargs(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="high")
        assert p._effort_request_kwargs() == p._build_thinking_kwargs()


class TestOpenAIMaxOutputTokens:
    def test_configured_cap_overrides_default_thinking_disabled(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(
            model="gpt-5.5",
            api_key="k",
            thinking_enabled=False,
            max_completion_tokens=40000,
        )
        assert p._token_limit_kwargs(8192) == {"max_tokens": 40000}

    def test_configured_cap_overrides_default_for_non_max_completion_model(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        # gpt-5.5 uses the OpenAI family spec (use_max_completion_tokens=False).
        p = OpenAIProvider(model="gpt-5.5", api_key="k", effort="high", max_completion_tokens=40000)
        assert p._token_limit_kwargs(8192) == {"max_tokens": 40000}

    def test_blank_config_falls_back_to_request_default(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="gpt-5.5", api_key="k", thinking_enabled=False)
        assert p._token_limit_kwargs(8192) == {"max_tokens": 8192}

    def test_configured_cap_uses_new_model_capability_without_thinking_budget(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        # The current DashScope capability table does not expose a thinking budget for glm-5.2.
        p = OpenAIProvider(
            model="glm-5.2",
            api_key="k",
            provider_key="dashscope",
            thinking_enabled=True,
            max_completion_tokens=50000,
        )
        assert p._token_limit_kwargs(8192) == {"max_completion_tokens": 50000}

    def test_unsupported_explicit_thinking_budget_does_not_inflate_cap(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        # An explicit budget is ignored when the current model capability does not support it.
        p = OpenAIProvider(
            model="glm-5.2",
            api_key="k",
            provider_key="dashscope",
            thinking_enabled=True,
            thinking_budget=2000,
            max_completion_tokens=50000,
        )
        assert p._token_limit_kwargs(8192) == {"max_completion_tokens": 50000}

    def test_blank_cap_uses_request_default_for_max_completion_model(self):
        from iac_code.providers.openai_provider import OpenAIProvider

        # Without a configured cap or supported budget, preserve the caller's default.
        p = OpenAIProvider(
            model="glm-5.2",
            api_key="k",
            provider_key="dashscope",
            thinking_enabled=True,
        )
        assert p._token_limit_kwargs(8192) == {"max_completion_tokens": 8192}


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
        assert out[-1].usage.usage_reported is True
        assert client.chat.completions.calls[0]["stream_options"] == {"include_usage": True}

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

    async def test_tool_call_waits_for_late_id_before_emitting_events(self):
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
                                    id=None,
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
                                    id="call_late",
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
        provider = OpenAIProvider(model="gpt-4.1", client=FakeOpenAIClient(stream_chunks=chunks))

        out = [e async for e in provider.stream(messages=[Message.user("run")], system="")]

        tool_events = [event for event in out if event.type.startswith("tool_")]
        assert [event.type for event in tool_events] == [
            "tool_use_start",
            "tool_input_delta",
            "tool_use_end",
        ]
        assert {event.tool_use_id for event in tool_events} == {"call_late"}
        assert tool_events[-1].input == {"cmd": "ls"}

    @pytest.mark.parametrize(
        ("name_parts", "expected_name"),
        [
            (("get_", "weather"), "get_weather"),
            (("a", "abc"), "aabc"),
        ],
    )
    async def test_tool_call_accumulates_fragmented_function_name(self, name_parts, expected_name):
        chunks = [
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(
                            content=None,
                            tool_calls=[ns(index=0, id="call_1", function=ns(name=name_parts[0], arguments=None))],
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
                            tool_calls=[ns(index=0, id=None, function=ns(name=name_parts[1], arguments="{}"))],
                        ),
                    )
                ],
            ),
            ns(
                usage=ns(prompt_tokens=3, completion_tokens=2),
                choices=[ns(finish_reason="tool_calls", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        provider = OpenAIProvider(model="gpt-4.1", client=FakeOpenAIClient(stream_chunks=chunks))

        out = [event async for event in provider.stream(messages=[Message.user("run")], system="")]

        start = next(event for event in out if event.type == "tool_use_start")
        end = next(event for event in out if event.type == "tool_use_end")
        assert start.name == expected_name
        assert end.name == expected_name
        assert end.input == {}

    async def test_tool_call_does_not_start_while_name_and_arguments_are_both_fragmenting(self):
        chunks = [
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(
                            content=None,
                            tool_calls=[ns(index=0, id="call_1", function=ns(name="read_", arguments="{"))],
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
                            tool_calls=[ns(index=0, id=None, function=ns(name="file", arguments='"path":"main.py"}'))],
                        ),
                    )
                ],
            ),
            ns(
                usage=ns(prompt_tokens=3, completion_tokens=2),
                choices=[ns(finish_reason="tool_calls", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        provider = OpenAIProvider(model="gpt-4.1", client=FakeOpenAIClient(stream_chunks=chunks))

        out = [event async for event in provider.stream(messages=[Message.user("run")], system="")]

        start = next(event for event in out if event.type == "tool_use_start")
        end = next(event for event in out if event.type == "tool_use_end")
        assert start.name == "read_file"
        assert end.name == "read_file"
        assert end.input == {"path": "main.py"}

    async def test_parallel_tool_calls_start_in_api_index_order(self):
        chunks = [
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(
                            content=None,
                            tool_calls=[
                                ns(index=0, id="call_0", function=ns(name="write_a", arguments=None)),
                                ns(index=1, id="call_1", function=ns(name="write_b", arguments=None)),
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
                            tool_calls=[ns(index=1, id=None, function=ns(name=None, arguments="{}"))],
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
                            tool_calls=[ns(index=0, id=None, function=ns(name=None, arguments="{}"))],
                        ),
                    )
                ],
            ),
            ns(
                usage=ns(prompt_tokens=3, completion_tokens=2),
                choices=[ns(finish_reason="tool_calls", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        provider = OpenAIProvider(model="gpt-4.1", client=FakeOpenAIClient(stream_chunks=chunks))

        out = [event async for event in provider.stream(messages=[Message.user("run")], system="")]

        starts = [event for event in out if event.type == "tool_use_start"]
        ends = [event for event in out if event.type == "tool_use_end"]
        assert [event.tool_use_id for event in starts] == ["call_0", "call_1"]
        assert [event.tool_use_id for event in ends] == ["call_0", "call_1"]

    async def test_gemini_tool_call_preserves_thought_signature(self):
        from iac_code.providers.gemini_provider import GeminiProvider

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
                                    function=ns(name="bash", arguments='{"cmd":"ls"}'),
                                    extra_content={"google": {"thought_signature": "signed-thought"}},
                                )
                            ],
                        ),
                    )
                ],
            ),
            ns(
                usage=ns(prompt_tokens=3, completion_tokens=2),
                choices=[ns(finish_reason="tool_calls", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        provider = GeminiProvider(
            model="gemini-3-flash-preview",
            client=FakeOpenAIClient(stream_chunks=chunks),
        )

        out = [e async for e in provider.stream(messages=[Message.user("run")], system="")]
        start = next(event for event in out if event.type == "tool_use_start")
        end = next(event for event in out if event.type == "tool_use_end")
        expected = provider._gemini_provider_metadata({"google": {"thought_signature": "signed-thought"}})
        assert start.provider_metadata == expected
        assert end.provider_metadata == expected

    async def test_gemini_text_response_preserves_thought_signature(self):
        from iac_code.providers.gemini_provider import GeminiProvider

        chunks = [
            ns(
                usage=None,
                choices=[
                    ns(
                        finish_reason=None,
                        delta=ns(
                            content="answer",
                            tool_calls=None,
                            extra_content={"google": {"thought_signature": "signed-text-thought"}},
                        ),
                    )
                ],
            ),
            ns(
                usage=ns(prompt_tokens=3, completion_tokens=2),
                choices=[ns(finish_reason="stop", delta=ns(content=None, tool_calls=None))],
            ),
        ]
        provider = GeminiProvider(
            model="gemini-3-flash-preview",
            client=FakeOpenAIClient(stream_chunks=chunks),
        )

        out = [e async for e in provider.stream(messages=[Message.user("run")], system="")]

        metadata_event = next(event for event in out if event.type == "thinking_delta")
        assert metadata_event.text == ""
        assert metadata_event.provider_metadata == provider._gemini_provider_metadata(
            {"google": {"thought_signature": "signed-text-thought"}}
        )

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

    async def test_gemini_tool_call_preserves_thought_signature(self):
        from iac_code.providers.gemini_provider import GeminiProvider

        response = ns(
            id="cmpl_gemini",
            choices=[
                ns(
                    finish_reason="tool_calls",
                    message=ns(
                        content=None,
                        tool_calls=[
                            ns(
                                id="call_x",
                                function=ns(name="bash", arguments='{"cmd":"ls"}'),
                                extra_content={"google": {"thought_signature": "signed-thought"}},
                            )
                        ],
                    ),
                )
            ],
            usage=ns(prompt_tokens=3, completion_tokens=2),
        )
        provider = GeminiProvider(
            model="gemini-3-flash-preview",
            client=FakeOpenAIClient(create_response=response),
        )

        result = await provider.complete(messages=[Message.user("run")], system="")

        provider_metadata = provider._gemini_provider_metadata({"google": {"thought_signature": "signed-thought"}})
        assert result.tool_uses == [
            {
                "id": "call_x",
                "name": "bash",
                "input": {"cmd": "ls"},
                "provider_metadata": provider_metadata,
            }
        ]

    async def test_gemini_text_response_preserves_thought_signature(self):
        from iac_code.providers.gemini_provider import GeminiProvider

        response = ns(
            id="cmpl_gemini_text",
            choices=[
                ns(
                    finish_reason="stop",
                    message=ns(
                        content="answer",
                        tool_calls=None,
                        extra_content={"google": {"thought_signature": "signed-text-thought"}},
                    ),
                )
            ],
            usage=ns(prompt_tokens=3, completion_tokens=2),
        )
        provider = GeminiProvider(
            model="gemini-3-flash-preview",
            client=FakeOpenAIClient(create_response=response),
        )

        result = await provider.complete(messages=[Message.user("run")], system="")

        provider_metadata = provider._gemini_provider_metadata({"google": {"thought_signature": "signed-text-thought"}})
        assert result.thinking_blocks == [
            {
                "type": "thinking",
                "text": "",
                "provider_metadata": provider_metadata,
            }
        ]

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
        assert end.usage.input_tokens == 500
        assert end.usage.total_input_tokens == 500
        assert end.usage.standard_input_tokens == 100
        assert end.usage.cache_read_input_tokens == 300
        assert end.usage.cache_creation_input_tokens == 100
        assert end.usage.total_tokens == 920
        assert end.usage.normalized_total_tokens == 520
        assert end.usage.cache_hit_rate == 0.6
        assert end.usage.usage_reported is True

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

    async def test_unknown_compatible_endpoint_does_not_receive_stream_options(self):
        chunks = [
            ns(
                usage=None,
                choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
            ),
        ]
        client = FakeOpenAIClient(stream_chunks=chunks)
        provider = OpenAIProvider(model="custom", client=client, provider_key="openai_compatible")

        out = [e async for e in provider.stream(messages=[Message.user("hi")], system="sys")]

        assert out[-1].usage.usage_reported is False
        assert "stream_options" not in client.chat.completions.calls[0]

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
        assert result.usage.input_tokens == 500
        assert result.usage.total_input_tokens == 500
        assert result.usage.standard_input_tokens == 100
        assert result.usage.cache_read_input_tokens == 400
        assert result.usage.cache_creation_input_tokens == 0
        assert result.usage.total_tokens == 920
        assert result.usage.normalized_total_tokens == 520
        assert result.usage.cache_hit_rate == 0.8
        assert result.usage.usage_reported is True
