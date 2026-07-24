import pytest

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import Message, ToolDefinition
from tests.providers._fakes import FakeAnthropicClient, ns


class TestAnthropicProvider:
    def test_created_client_preserves_sdk_retry_default(self, monkeypatch):
        calls = []

        class FakeAsyncAnthropic:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.base_url = kwargs.get("base_url") or "https://fake.anthropic.local"

        monkeypatch.setattr("iac_code.providers.anthropic_provider.anthropic.AsyncAnthropic", FakeAsyncAnthropic)

        AnthropicProvider(model="claude-sonnet-4-6", api_key="test")

        assert "max_retries" not in calls[0]

    def test_get_model_name(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        assert p.get_model_name() == "claude-sonnet-4-6"

    def test_convert_messages_user(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        msgs = [Message.user("Hello")]
        api = p._convert_messages(msgs)
        assert api[0]["role"] == "user"
        assert api[0]["content"] == "Hello"

    def test_convert_messages_merges_consecutive_user_messages(self):
        from iac_code.agent.message import create_recalled_memory_message

        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        recalled_memory = create_recalled_memory_message("# Recalled Memory\nUse YAML", ["ros.md"])
        msgs = [
            Message.user("real user prompt"),
            Message(role="user", content=recalled_memory.content),
        ]

        api = p._convert_messages(msgs)

        assert len(api) == 1
        assert api[0]["role"] == "user"
        assert api[0]["content"].startswith("real user prompt\n\n")
        assert "Relevant persistent memories" in api[0]["content"]

    def test_convert_messages_merges_consecutive_mixed_content_messages(self):
        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        msgs = [
            Message.user("tool result follows"),
            Message.tool_result(tool_use_id="t1", content="done", is_error=False),
            Message.assistant_text("first answer"),
            Message.assistant_text("second answer"),
        ]

        api = p._convert_messages(msgs)

        assert [message["role"] for message in api] == ["user", "assistant"]
        assert api[0]["content"] == [
            {"type": "text", "text": "tool result follows"},
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
        ]
        assert api[1]["content"] == [
            {"type": "text", "text": "first answer"},
            {"type": "text", "text": "second answer"},
        ]

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
        assert d is None

    def test_convert_thinking_and_redacted_blocks_echo_anthropic_metadata(self):
        from iac_code.providers.base import ContentBlock

        p = AnthropicProvider(model="claude-sonnet-5", api_key="test")
        thinking = ContentBlock(
            type="thinking",
            text="deep thought",
            provider_metadata=p._provider_metadata(signature="signed-thinking"),
        )
        redacted = ContentBlock(
            type="redacted_thinking",
            data="encrypted-thinking",
            provider_metadata=p._provider_metadata(data="encrypted-thinking"),
        )

        assert p._convert_content_block(thinking) == {
            "type": "thinking",
            "thinking": "deep thought",
            "signature": "signed-thinking",
        }
        assert p._convert_content_block(redacted) == {
            "type": "redacted_thinking",
            "data": "encrypted-thinking",
        }

    def test_convert_thinking_does_not_echo_foreign_provider_metadata(self):
        from iac_code.providers.base import ContentBlock

        p = AnthropicProvider(model="claude-sonnet-5", api_key="test")
        block = ContentBlock(
            type="thinking",
            text="deep thought",
            provider_metadata={"provider": "other", "signature": "not-an-anthropic-signature"},
        )

        assert p._convert_content_block(block) is None

    def test_anthropic_metadata_is_not_sent_to_minimax(self):
        from iac_code.providers.base import ContentBlock
        from iac_code.providers.minimax_provider import MiniMaxProvider

        provider = MiniMaxProvider(model="MiniMax-M3", api_key="test")
        blocks = [
            ContentBlock(
                type="thinking",
                text="deep thought",
                provider_metadata={"provider": "anthropic", "signature": "signed-thinking"},
            ),
            ContentBlock(
                type="redacted_thinking",
                data="encrypted-thinking",
                provider_metadata={"provider": "anthropic", "data": "encrypted-thinking"},
            ),
        ]

        assert provider._convert_message_content(blocks) == []

    def test_anthropic_compatible_metadata_is_scoped_to_endpoint(self):
        from iac_code.providers.base import ContentBlock

        source = AnthropicProvider(
            model="custom-model",
            api_key="test",
            base_url="https://first.example/v1",
            provider_key="anthropic_compatible",
        )
        target = AnthropicProvider(
            model="custom-model",
            api_key="test",
            base_url="https://second.example/v1",
            provider_key="anthropic_compatible",
        )
        block = ContentBlock(
            type="thinking",
            text="deep thought",
            provider_metadata=source._provider_metadata(signature="signed-thinking"),
        )

        assert target._convert_content_block(block) is None

    def test_anthropic_metadata_is_scoped_to_wire_model(self):
        from iac_code.providers.base import ContentBlock

        source = AnthropicProvider(model="claude-fable-5", api_key="test")
        target = AnthropicProvider(model="claude-sonnet-5", api_key="test")
        thinking = ContentBlock(
            type="thinking",
            text="deep thought",
            provider_metadata=source._provider_metadata(signature="signed-thinking"),
        )
        redacted = ContentBlock(
            type="redacted_thinking",
            data="encrypted-thinking",
            provider_metadata=source._provider_metadata(data="encrypted-thinking"),
        )

        assert target._convert_message_content([thinking, redacted]) == []

    def test_anthropic_model_alias_accepts_metadata_from_same_wire_model(self):
        from iac_code.providers.base import ContentBlock

        source = AnthropicProvider(model="claude-sonnet-4-6-1m", api_key="test")
        target = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        block = ContentBlock(
            type="thinking",
            text="deep thought",
            provider_metadata=source._provider_metadata(signature="signed-thinking"),
        )

        assert target._convert_content_block(block) == {
            "type": "thinking",
            "thinking": "deep thought",
            "signature": "signed-thinking",
        }

    def test_convert_messages_drops_empty_assistant_after_foreign_thinking_is_filtered(self):
        from iac_code.providers.base import ContentBlock, Message

        provider = AnthropicProvider(model="claude-sonnet-5", api_key="test")
        messages = [
            Message.user("before"),
            Message(
                role="assistant",
                content=[
                    ContentBlock(
                        type="thinking",
                        text="old reasoning",
                        provider_metadata={"provider": "other", "signature": "foreign"},
                    )
                ],
            ),
            Message.user("after"),
        ]

        assert provider._convert_messages(messages) == [{"role": "user", "content": "before\n\nafter"}]

    def test_metadata_uses_sdk_endpoint_from_environment(self, monkeypatch):
        from iac_code.providers.base import ContentBlock

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://first.example/v1")
        source = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")
        block = ContentBlock(
            type="thinking",
            text="deep thought",
            provider_metadata=source._provider_metadata(signature="signed-thinking"),
        )

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://second.example/v1")
        target = AnthropicProvider(model="claude-sonnet-4-6", api_key="test")

        assert source._metadata_endpoint_id != target._metadata_endpoint_id
        assert target._convert_content_block(block) is None

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
    def test_high_returns_adaptive_thinking_and_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="high")
        kwargs = p._build_thinking_kwargs()
        assert kwargs == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
        assert p._adjust_max_tokens(8192) == 8192

    def test_max_uses_adaptive_output_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="max")
        assert p._build_thinking_kwargs() == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}
        assert p._adjust_max_tokens(8192) == 8192

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

    def test_enabled_true_uses_default_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", thinking_enabled=True)
        assert p._build_thinking_kwargs() == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
        assert p._adjust_max_tokens(8192) == 8192

    def test_enabled_false_disables_configurable_adaptive_thinking(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(
            model="claude-opus-4-7",
            api_key="k",
            effort="high",
            thinking_budget=2048,
            thinking_enabled=False,
        )
        assert p._build_thinking_kwargs() == {
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "high"},
        }
        assert p._adjust_max_tokens(8192) == 8192

    def test_disabled_thinking_ignores_invalid_manual_budget(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(
            model="claude-sonnet-4-6",
            api_key="k",
            thinking_budget=1023,
            thinking_enabled=False,
        )

        assert p._build_thinking_kwargs() == {"thinking": {"type": "disabled"}}
        assert p._adjust_max_tokens(8192) == 8192

    def test_explicit_thinking_budget_is_ignored_without_manual_budget_support(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", thinking_budget=2048)

        assert p._build_thinking_kwargs() == {}
        assert p._adjust_max_tokens(8192) == 8192

    def test_claude_46_explicit_thinking_budget_uses_manual_mode(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="k", thinking_budget=2048)

        assert p._build_thinking_kwargs() == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
        assert p._adjust_max_tokens(8192) == 8192

    def test_claude_46_manual_budget_preserves_explicit_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(
            model="claude-sonnet-4-6",
            api_key="k",
            thinking_budget=2048,
            effort="low",
        )

        assert p._build_thinking_kwargs() == {
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "output_config": {"effort": "low"},
        }

    def test_claude_46_rejects_manual_thinking_budget_below_minimum(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="k", thinking_budget=1023)

        with pytest.raises(ValueError, match="at least 1024"):
            p._build_thinking_kwargs()

    def test_legacy_haiku_uses_budget_tokens(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-haiku-4-5-20251001", api_key="k", effort="high")

        assert p._build_thinking_kwargs() == {"thinking": {"type": "enabled", "budget_tokens": 16384}}
        assert p._adjust_max_tokens(8192) >= 16384 + 4096

    def test_legacy_haiku_rejects_manual_thinking_budget_below_minimum(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-haiku-4-5-20251001", api_key="k", thinking_budget=1023)

        with pytest.raises(ValueError, match="at least 1024"):
            p._build_thinking_kwargs()

    def test_always_on_adaptive_models_do_not_send_thinking_switch(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-fable-5", api_key="k", effort="high")

        assert p._build_thinking_kwargs() == {"output_config": {"effort": "high"}}
        assert AnthropicProvider(
            model="claude-fable-5",
            api_key="k",
            effort="high",
            thinking_enabled=False,
        )._build_thinking_kwargs() == {"output_config": {"effort": "high"}}


class TestAnthropicMaxOutputTokens:
    def test_configured_cap_overrides_request_default_without_thinking(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(
            model="claude-opus-4-7",
            api_key="k",
            effort="auto",
            max_completion_tokens=32000,
        )
        # 无思考预算时,配置的输出上限直接覆盖调用方默认(8192)。
        assert p._adjust_max_tokens(8192) == 32000
        kwargs = p._build_kwargs([Message.user("hi")], "sys", None, 8192)
        assert kwargs["max_tokens"] == 32000

    def test_configured_cap_is_respected_for_adaptive_thinking(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        # Adaptive thinking has no separate manual budget in the current capability table.
        p = AnthropicProvider(
            model="claude-opus-4-7",
            api_key="k",
            effort="high",  # 16384 budget
            max_completion_tokens=1000,
        )
        assert p._adjust_max_tokens(8192) == 1000

    def test_blank_config_falls_back_to_request_default(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="auto")
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

    async def test_cache_tokens_are_included_in_normalized_input_total(self):
        final = ns(
            usage=ns(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=20,
                cache_read_input_tokens=70,
            ),
            stop_reason="end_turn",
        )
        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            client=FakeAnthropicClient(stream_events=[], stream_final=final),
        )

        out = [e async for e in provider.stream(messages=[Message.user("hi")], system="sys")]
        usage = out[-1].usage

        assert usage.input_tokens == 10
        assert usage.total_input_tokens == 100
        assert usage.standard_input_tokens == 10
        assert usage.output_tokens == 5
        assert usage.cache_creation_input_tokens == 20
        assert usage.cache_read_input_tokens == 70
        assert usage.total_tokens == 105
        assert usage.normalized_total_tokens == 105
        assert usage.cache_hit_rate == 0.7
        assert usage.usage_reported is True

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

    async def test_thinking_signature_and_redacted_block_are_preserved(self):
        events = [
            ns(type="message_start", message=ns(id="msg_signed")),
            ns(type="content_block_start", index=0, content_block=ns(type="thinking", thinking="", signature="")),
            ns(type="content_block_delta", index=0, delta=ns(type="thinking_delta", thinking="reasoning")),
            ns(type="content_block_delta", index=0, delta=ns(type="signature_delta", signature="signed-")),
            ns(type="content_block_delta", index=0, delta=ns(type="signature_delta", signature="thinking")),
            ns(type="content_block_stop", index=0),
            ns(
                type="content_block_start",
                index=1,
                content_block=ns(type="redacted_thinking", data="encrypted-thinking"),
            ),
            ns(type="content_block_stop", index=1),
        ]
        final = ns(
            usage=ns(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="end_turn",
        )
        provider = AnthropicProvider(
            model="claude-sonnet-5",
            client=FakeAnthropicClient(stream_events=events, stream_final=final),
        )

        out = [e async for e in provider.stream(messages=[Message.user("?")], system="")]
        thinking_events = [event for event in out if event.type == "thinking_delta"]

        assert [(event.block_index, event.block_type) for event in thinking_events] == [
            (0, "thinking"),
            (0, "thinking"),
            (0, "thinking"),
            (1, "redacted_thinking"),
        ]
        assert thinking_events[1].provider_metadata == {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "signature": "signed-",
        }
        assert thinking_events[-1].provider_metadata == {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "data": "encrypted-thinking",
        }


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

    async def test_cache_tokens_are_included_in_normalized_input_total(self):
        response = ns(
            id="msg_cached",
            content=[ns(type="text", text="cached")],
            usage=ns(
                input_tokens=5,
                output_tokens=2,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=45,
            ),
            stop_reason="end_turn",
        )
        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            client=FakeAnthropicClient(create_response=response),
        )

        result = await provider.complete(messages=[Message.user("hi")], system="sys")

        assert result.usage.input_tokens == 5
        assert result.usage.total_input_tokens == 50
        assert result.usage.standard_input_tokens == 5
        assert result.usage.total_tokens == 52
        assert result.usage.normalized_total_tokens == 52
        assert result.usage.cache_hit_rate == 0.9
        assert result.usage.usage_reported is True

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

    async def test_thinking_signature_and_redacted_block_are_preserved(self):
        response = ns(
            id="msg_signed",
            content=[
                ns(type="thinking", thinking="reasoning", signature="signed-thinking"),
                ns(type="redacted_thinking", data="encrypted-thinking"),
                ns(type="text", text="answer"),
            ],
            usage=ns(input_tokens=2, output_tokens=3, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="end_turn",
        )
        provider = AnthropicProvider(
            model="claude-sonnet-5",
            client=FakeAnthropicClient(create_response=response),
        )

        result = await provider.complete(messages=[Message.user("?")], system="")

        assert result.thinking == "reasoning"
        assert result.thinking_blocks == [
            {
                "type": "thinking",
                "text": "reasoning",
                "provider_metadata": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "signature": "signed-thinking",
                },
            },
            {
                "type": "redacted_thinking",
                "provider_metadata": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "data": "encrypted-thinking",
                },
            },
        ]
