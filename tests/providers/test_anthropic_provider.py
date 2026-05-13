import pytest

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import Message, ToolDefinition
from tests.providers._fakes import FakeAnthropicClient, ns


class TestAnthropicProvider:
    def test_get_model_name(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        assert p.get_model_name() == "claude-sonnet-4-6"

    def test_convert_messages_user(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        msgs = [Message.user("Hello")]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "user"
        assert api[0]["content"] == "Hello"

    def test_convert_messages_tool_result(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        msgs = [Message.tool_result(tool_use_id="t1", content="output", is_error=False)]
        api = p._convert_messages(msgs)
        assert api[0]["content"][0]["type"] == "tool_result"
        assert api[0]["content"][0]["tool_use_id"] == "t1"

    def test_convert_tools(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        tools = [ToolDefinition(name="bash", description="Run", input_schema={"type": "object"})]
        api = p._convert_tools(tools)
        assert api[0]["name"] == "bash"
        assert api[0]["input_schema"]["type"] == "object"

    def test_convert_messages_assistant_tool_use(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        msgs = [Message.assistant_tool_use(tool_use_id="t1", name="bash", input={"command": "ls"})]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "assistant"
        assert api[0]["content"][0]["type"] == "tool_use"
        assert api[0]["content"][0]["id"] == "t1"

    def test_convert_thinking_block(self):
        from iac_code.providers.base import ContentBlock

        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        block = ContentBlock(type="thinking", text="deep thought")
        d = p._convert_content_block(block)
        assert d == {"type": "thinking", "thinking": "deep thought"}

    def test_convert_unknown_block_type(self):
        from iac_code.providers.base import ContentBlock

        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        block = ContentBlock(type="custom_kind")
        d = p._convert_content_block(block)
        assert d == {"type": "custom_kind"}

    def test_convert_tool_result_with_error(self):
        from iac_code.providers.base import ContentBlock

        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        block = ContentBlock(type="tool_result", tool_use_id="t1", content="boom", is_error=True)
        d = p._convert_content_block(block)
        assert d["is_error"] is True
        assert d["content"] == "boom"


class TestAnthropicBuildThinkingKwargs:
    def test_high_returns_thinking_block_and_bumps_max(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="high")
        kwargs = p._build_thinking_kwargs()
        assert kwargs == {"thinking": {"type": "enabled", "budget_tokens": 16384}}
        assert p._adjust_max_tokens(8192) >= 16384 + 4096

    def test_max_uses_64k_budget(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="max")
        assert p._build_thinking_kwargs()["thinking"]["budget_tokens"] == 64000
        assert p._adjust_max_tokens(8192) >= 64000 + 4096

    def test_auto_returns_empty(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="auto")
        assert p._build_thinking_kwargs() == {}
        assert p._adjust_max_tokens(8192) == 8192

    def test_no_effort_returns_empty(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort=None)
        assert p._build_thinking_kwargs() == {}
        assert p._adjust_max_tokens(8192) == 8192


@pytest.mark.asyncio
class TestAnthropicStream:
    async def test_text_only_response(self):
        events = [
            ns(type="message_start", message=ns(id="msg_1")),
            ns(type="content_block_start", content_block=ns(type="text")),
            ns(type="content_block_delta", delta=ns(type="text_delta", text="Hello ")),
            ns(type="content_block_delta", delta=ns(type="text_delta", text="world")),
            ns(type="content_block_stop"),
        ]
        final = ns(
            usage=ns(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            stop_reason="end_turn",
        )
        client = FakeAnthropicClient(stream_events=events, stream_final=final)
        provider = AnthropicProvider(model="claude-sonnet-4-6", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("hi")], system="sys")]

        types = [e.type for e in out]
        assert types == ["message_start", "text_delta", "text_delta", "message_end"]
        assert out[0].message_id == "msg_1"
        assert out[1].text == "Hello "
        assert out[2].text == "world"
        assert out[-1].stop_reason == "end_turn"
        assert out[-1].usage.input_tokens == 10
        assert out[-1].usage.output_tokens == 5

    async def test_stream_kwargs_includes_system_and_tools(self):
        events = []
        final = ns(
            usage=ns(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="end_turn",
        )
        client = FakeAnthropicClient(stream_events=events, stream_final=final)
        provider = AnthropicProvider(model="claude-sonnet-4-6", client=client)
        tools = [ToolDefinition(name="bash", description="run", input_schema={"type": "object"})]

        _ = [e async for e in provider.stream(messages=[Message.user("hi")], system="SYS", tools=tools)]

        call = client.messages.stream_calls[0]
        assert call["model"] == "claude-sonnet-4-6"
        assert call["system"] == "SYS"
        assert call["tools"][0]["name"] == "bash"

    async def test_tool_use_block_yields_events(self):
        events = [
            ns(type="message_start", message=ns(id="msg_2")),
            ns(
                type="content_block_start",
                content_block=ns(type="tool_use", id="toolu_1", name="bash"),
            ),
            ns(
                type="content_block_delta",
                delta=ns(type="input_json_delta", partial_json='{"cmd":'),
            ),
            ns(
                type="content_block_delta",
                delta=ns(type="input_json_delta", partial_json='"ls"}'),
            ),
            ns(type="content_block_stop"),
        ]
        final = ns(
            usage=ns(input_tokens=8, output_tokens=4, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="tool_use",
        )
        client = FakeAnthropicClient(stream_events=events, stream_final=final)
        provider = AnthropicProvider(model="claude-sonnet-4-6", client=client)

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
        assert out[1].tool_use_id == "toolu_1"
        assert out[1].name == "bash"
        end = out[-2]
        assert end.tool_use_id == "toolu_1"
        assert end.input == {"cmd": "ls"}

    async def test_thinking_delta_yields_thinking_event(self):
        events = [
            ns(type="message_start", message=ns(id="msg_3")),
            ns(type="content_block_start", content_block=ns(type="thinking")),
            ns(
                type="content_block_delta",
                delta=ns(type="thinking_delta", thinking="reasoning..."),
            ),
            ns(type="content_block_stop"),
        ]
        final = ns(
            usage=ns(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="end_turn",
        )
        client = FakeAnthropicClient(stream_events=events, stream_final=final)
        provider = AnthropicProvider(model="claude-sonnet-4-6", client=client)

        out = [e async for e in provider.stream(messages=[Message.user("?")], system="")]

        types = [e.type for e in out]
        assert "thinking_delta" in types
        ev = next(e for e in out if e.type == "thinking_delta")
        assert ev.text == "reasoning..."


@pytest.mark.asyncio
class TestAnthropicComplete:
    async def test_text_only_response(self):
        response = ns(
            id="msg_c1",
            content=[ns(type="text", text="Hello world")],
            usage=ns(input_tokens=3, output_tokens=2, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="end_turn",
        )
        client = FakeAnthropicClient(create_response=response)
        provider = AnthropicProvider(model="claude-sonnet-4-6", client=client)

        result = await provider.complete(messages=[Message.user("hi")], system="sys")

        assert result.message_id == "msg_c1"
        assert result.text == "Hello world"
        assert result.tool_uses == []
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 3
        assert result.usage.output_tokens == 2

    async def test_tool_use_response(self):
        response = ns(
            id="msg_c2",
            content=[
                ns(type="text", text="calling tool"),
                ns(type="tool_use", id="toolu_9", name="bash", input={"cmd": "ls"}),
            ],
            usage=ns(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="tool_use",
        )
        client = FakeAnthropicClient(create_response=response)
        provider = AnthropicProvider(model="claude-sonnet-4-6", client=client)

        result = await provider.complete(messages=[Message.user("run")], system="")

        assert result.text == "calling tool"
        assert result.tool_uses == [{"id": "toolu_9", "name": "bash", "input": {"cmd": "ls"}}]
        assert result.stop_reason == "tool_use"
