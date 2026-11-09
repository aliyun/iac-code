import json

import anthropic
import httpx
import pytest

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import Message, ToolDefinition
from iac_code.providers.request_headers import use_provider_request_headers
from tests.providers._fakes import FakeAnthropicClient, ns


class AnthropicSDKTransport:
    def __init__(self, capacity=196608, stop_reason="end_turn"):
        self.capacity = capacity
        self.stop_reason = stop_reason
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "type": "model",
                    "display_name": "Test model",
                    "created_at": "2026-01-01T00:00:00Z",
                    "max_tokens": self.capacity,
                },
            )
        response = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": json.loads(request.content)["model"],
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": self.stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
        if not json.loads(request.content).get("stream"):
            return httpx.Response(200, json=response)
        events = [
            ("message_start", {"message": {**response, "content": [], "stop_reason": None}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "done"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": self.stop_reason}, "usage": {"output_tokens": 1}}),
            ("message_stop", {}),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n" for kind, data in events),
        )


@pytest.mark.asyncio
class TestAnthropicModelCapacity:
    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize(
        "server_reason,internal_reason",
        [
            ("model_context_window_exceeded", "max_tokens"),
            ("max_tokens", "max_tokens"),
            ("end_turn", "end_turn"),
        ],
    )
    async def test_context_window_stop_reason_uses_existing_truncation_contract(
        self, operation, server_reason, internal_reason
    ):
        transport = AnthropicSDKTransport(stop_reason=server_reason)
        async with anthropic.AsyncAnthropic(
            api_key="fake", auth_token="fake", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        ) as client:
            provider = AnthropicProvider(model="claude-sonnet-4-6", client=client)
            if operation == "stream":
                events = [event async for event in provider.stream([Message.user("hello")], "system")]
                assert events[-1].stop_reason == internal_reason
            else:
                response = await provider.complete([Message.user("hello")], "system")
                assert response.stop_reason == internal_reason

    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize(
        "model,capacity",
        [
            ("claude-fable-5-1", 128000),
            ("claude-opus-5-5", 128000),
            ("claude-sonnet-5-5", 128000),
            ("claude-haiku-5-5", 128000),
            ("claude-fable-5", 128000),
            ("claude-opus-5", 128000),
            ("claude-opus-4-8", 128000),
            ("claude-sonnet-5", 128000),
            ("claude-opus-4-7", 128000),
            ("claude-opus-4-6", 128000),
            ("claude-sonnet-4-6", 128000),
            ("claude-sonnet-4-6-1m", 128000),
            ("claude-haiku-4-5-20251001", 64000),
        ],
    )
    async def test_registered_claude_uses_full_model_capacity(self, operation, model, capacity):
        transport = AnthropicSDKTransport()
        async with anthropic.AsyncAnthropic(
            api_key="fake", auth_token="fake", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        ) as client:
            provider = AnthropicProvider(model=model, client=client)
            if operation == "stream":
                _ = [event async for event in provider.stream([Message.user("hello")], "system")]
            else:
                assert (await provider.complete([Message.user("hello")], "system")).text == "done"

        assert len(transport.requests) == 1
        assert json.loads(transport.requests[0].content)["max_tokens"] == capacity

    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize(
        "provider_key,model,metadata_url",
        [
            ("anthropic", "custom-model", "https://custom.example/api/v1/models/custom-model"),
            ("anthropic_compatible", "custom-model", "https://custom.example/api/v1/models/custom-model"),
            ("anthropic_compatible", "claude-sonnet-4-6", "https://custom.example/api/v1/models/claude-sonnet-4-6"),
            ("minimax_cn", "MiniMax-M2.5", "https://custom.example/api/v1/models/MiniMax-M2.5"),
        ],
    )
    async def test_unknown_model_queries_its_endpoint_and_caches_capacity(
        self, operation, provider_key, model, metadata_url
    ):
        transport = AnthropicSDKTransport()
        async with anthropic.AsyncAnthropic(
            api_key="fake", auth_token="fake",
            base_url="https://custom.example/api",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        ) as client:
            provider = AnthropicProvider(model=model, provider_key=provider_key, client=client)
            for _ in range(2):
                if operation == "stream":
                    _ = [event async for event in provider.stream([Message.user("hello")], "system")]
                else:
                    await provider.complete([Message.user("hello")], "system")

        assert [request.method for request in transport.requests] == ["GET", "POST", "POST"]
        assert transport.requests[0].url == metadata_url
        assert all(json.loads(request.content)["max_tokens"] == 196608 for request in transport.requests[1:])

    @pytest.mark.parametrize("operation", ["stream", "complete"])
    async def test_model_metadata_and_messages_forward_request_headers(self, operation):
        transport = AnthropicSDKTransport()
        async with anthropic.AsyncAnthropic(
            api_key="fake", auth_token="fake",
            base_url="https://custom.example/api",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        ) as client:
            provider = AnthropicProvider(model="custom-model", provider_key="anthropic_compatible", client=client)
            for authorization in ["Bearer request-token-1", "Bearer request-token-2"]:
                with use_provider_request_headers({"Authorization": authorization, "X-A2A-Session": "session-1"}):
                    if operation == "stream":
                        _ = [event async for event in provider.stream([Message.user("hello")], "system")]
                    else:
                        await provider.complete([Message.user("hello")], "system")

        assert [request.method for request in transport.requests] == ["GET", "POST", "POST"]
        assert [
            (request.headers.get("Authorization"), request.headers.get("X-A2A-Session"))
            for request in transport.requests
        ] == [
            ("Bearer request-token-1", "session-1"),
            ("Bearer request-token-1", "session-1"),
            ("Bearer request-token-2", "session-1"),
        ]

    @pytest.mark.parametrize("capacity", [None, 0, -1, True, "64000", 64000.5])
    async def test_unknown_model_rejects_invalid_capacity_without_sending_message(self, capacity):
        transport = AnthropicSDKTransport(capacity)
        async with anthropic.AsyncAnthropic(
            api_key="fake", auth_token="fake", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        ) as client:
            provider = AnthropicProvider(model="custom-model", client=client)
            with pytest.raises(ValueError, match="max_tokens"):
                await provider.complete([Message.user("hello")], "system")
        assert [request.method for request in transport.requests] == ["GET"]

    @pytest.mark.parametrize("provider_key", ["minimax_cn", "minimax_intl"])
    @pytest.mark.parametrize(
        "model",
        [
            "MiniMax-M3",
            "MiniMax-M3.1-Flash",
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-M2.5",
            "MiniMax-M2.5-highspeed",
            "MiniMax-M2.1",
            "MiniMax-M2",
            "custom-minimax-model",
        ],
    )
    @pytest.mark.parametrize("operation", ["stream", "complete"])
    @pytest.mark.parametrize("thinking_enabled", [False, True])
    async def test_minimax_omits_optional_output_limit_without_model_lookup(
        self, provider_key, model, operation, thinking_enabled
    ):
        from iac_code.providers.minimax_provider import MiniMaxProvider

        transport = AnthropicSDKTransport()
        async with anthropic.AsyncAnthropic(
            api_key="fake", auth_token="fake", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        ) as client:
            provider = MiniMaxProvider(
                model=model, provider_key=provider_key, client=client, thinking_enabled=thinking_enabled
            )
            if operation == "stream":
                _ = [event async for event in provider.stream([Message.user("hello")], "system")]
            else:
                await provider.complete([Message.user("hello")], "system")
        assert [request.method for request in transport.requests] == ["POST"]
        assert {"max_tokens", "max_completion_tokens", "max_output_tokens"}.isdisjoint(
            json.loads(transport.requests[0].content)
        )

    @pytest.mark.parametrize("budget", [128000, 128001])
    async def test_thinking_budget_must_leave_room_within_model_capacity(self, budget):
        client = FakeAnthropicClient()
        provider = AnthropicProvider(model="claude-sonnet-4-6", client=client, thinking_budget=budget)
        with pytest.raises(ValueError, match="thinking_budget.*model"):
            await provider.complete([Message.user("hello")], "system")
        assert client.messages.create_calls == []

    @pytest.mark.parametrize("budget", [1024, 127999])
    async def test_valid_thinking_budget_does_not_resize_model_capacity(self, budget):
        transport = AnthropicSDKTransport()
        async with anthropic.AsyncAnthropic(
            api_key="fake", auth_token="fake", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        ) as client:
            provider = AnthropicProvider(model="claude-sonnet-4-6", client=client, thinking_budget=budget)
            await provider.complete([Message.user("hello")], "system")
        payload = json.loads(transport.requests[0].content)
        assert payload["max_tokens"] == 128000
        assert payload["thinking"] == {"type": "enabled", "budget_tokens": budget}


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
    def test_opus5_defaults_to_server_managed_thinking(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-5", api_key="k")
        assert p._build_thinking_kwargs() == {}

    def test_opus5_can_disable_thinking_at_high_or_lower_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(
            model="claude-opus-5",
            api_key="k",
            effort="high",
            thinking_enabled=False,
        )
        assert p._build_thinking_kwargs() == {
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "high"},
        }

    @pytest.mark.parametrize("effort", ["xhigh", "max"])
    def test_opus5_rejects_disabling_thinking_at_highest_efforts(self, effort):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(
            model="claude-opus-5",
            api_key="k",
            effort=effort,
            thinking_enabled=False,
        )
        with pytest.raises(ValueError, match="cannot be disabled.*use high or lower"):
            p._build_thinking_kwargs()

    def test_high_returns_adaptive_thinking_and_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="high")
        kwargs = p._build_thinking_kwargs()
        assert kwargs == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}

    def test_max_uses_adaptive_output_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="max")
        assert p._build_thinking_kwargs() == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}

    def test_auto_returns_empty(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort="auto")
        assert p._build_thinking_kwargs() == {}

    def test_no_effort_returns_empty(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", effort=None)
        assert p._build_thinking_kwargs() == {}

    def test_enabled_true_uses_default_effort(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", thinking_enabled=True)
        assert p._build_thinking_kwargs() == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}

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

    def test_disabled_thinking_ignores_invalid_manual_budget(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(
            model="claude-sonnet-4-6",
            api_key="k",
            thinking_budget=1023,
            thinking_enabled=False,
        )

        assert p._build_thinking_kwargs() == {"thinking": {"type": "disabled"}}

    def test_explicit_thinking_budget_is_ignored_without_manual_budget_support(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-opus-4-7", api_key="k", thinking_budget=2048)

        assert p._build_thinking_kwargs() == {}

    def test_claude_46_explicit_thinking_budget_uses_manual_mode(self):
        from iac_code.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="claude-sonnet-4-6", api_key="k", thinking_budget=2048)

        assert p._build_thinking_kwargs() == {"thinking": {"type": "enabled", "budget_tokens": 2048}}

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
        assert out[-1].stop_reason == "tool_use"

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
