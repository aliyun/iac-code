"""Regression cases found by reviewing the 2026-10 provider update."""

import pytest

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import ContentBlock, Message
from iac_code.providers.manager import create_provider
from iac_code.providers.thinking import get_thinking_spec, resolve_thinking_active
from iac_code.types.stream_events import ThinkingDeltaEvent
from tests.providers._fakes import FakeAnthropicClient, ns


@pytest.mark.parametrize(
    "key,model,effort,expected",
    [
        ("volcengine_cn", "kimi-k3-1-pro-261024", "medium", "low"),
        ("volcengine_cn_codingplan", "kimi-k3.1-pro", "medium", "low"),
        ("volcengine_cn", "glm-5-3-flash-260828", "none", "low"),
        ("volcengine_cn_codingplan", "glm-5.3-flash", "none", "low"),
    ],
)
def test_ark_effort_alias_survives_provider_configuration(key, model, effort, expected):
    provider = create_provider(
        model,
        {key: "fake"},
        provider_key_override=key,
        provider_config_override={"models": {model: {"effort": effort}}},
    )
    assert provider._build_thinking_kwargs() == {"reasoning_effort": expected}


@pytest.mark.parametrize("enabled", [None, True])
def test_terra_none_effort_matches_web_thinking_state(enabled):
    provider = create_provider(
        "gpt-6.1-terra",
        {"openai": "fake"},
        provider_key_override="openai",
        provider_config_override={"effort": "none", "thinkingEnabled": enabled},
    )
    assert provider._responses_effort() == "none"
    assert resolve_thinking_active("openai", "gpt-6.1-terra", enabled, "none") is False


@pytest.mark.parametrize("key,model", [("kimi_cn", "kimi-k3.1-pro"), ("kimi_code", "k3.1-pro")])
def test_always_on_kimi_keeps_effort_and_completion_limit_when_disable_is_unsupported(key, model):
    provider = create_provider(
        model,
        {key: "fake"},
        provider_key_override=key,
        provider_config_override={"effort": "low", "thinkingEnabled": False},
    )
    assert provider._build_thinking_kwargs() == {"reasoning_effort": "low"}
    assert provider._token_limit_kwargs(4096) == {"max_completion_tokens": 4096}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_claude_drops_signed_thinking_when_its_own_assistant_turn_is_edited(streaming):
    response = ns(
        id="message-1",
        content=[
            ns(type="thinking", thinking="", signature="signed"),
            ns(type="text", text="original response"),
            ns(type="tool_use", id="call-1", name="read_file", input={"path": "main.tf"}),
        ],
        stop_reason="tool_use",
        usage=ns(input_tokens=10, output_tokens=20),
    )
    client = FakeAnthropicClient(
        create_response=response,
        stream_final=response,
        stream_events=[
            ns(type="content_block_start", index=0, content_block=ns(type="thinking", thinking="", signature="")),
            ns(type="content_block_delta", index=0, delta=ns(type="signature_delta", signature="signed")),
            ns(type="content_block_start", index=1, content_block=ns(type="text", text="")),
            ns(type="content_block_delta", index=1, delta=ns(type="text_delta", text="original response")),
            ns(type="content_block_start", index=2, content_block=response.content[2]),
            ns(
                type="content_block_delta",
                index=2,
                delta=ns(type="input_json_delta", partial_json='{"path":"main.tf"}'),
            ),
            ns(type="content_block_stop", index=2),
        ],
    )
    provider = AnthropicProvider(model="claude-opus-5-5", client=client)
    user = Message.user("inspect template")
    if streaming:
        metadata = {}
        async for event in provider.stream([user], "system"):
            if isinstance(event, ThinkingDeltaEvent) and event.provider_metadata:
                metadata.update(event.provider_metadata)
    else:
        result = await provider.complete([user], "system")
        metadata = result.thinking_blocks[0]["provider_metadata"]
    blocks = [
        ContentBlock(type="thinking", text="", provider_metadata=metadata),
        ContentBlock(type="text", text="original response"),
        ContentBlock(type="tool_use", tool_use_id="call-1", name="read_file", input={"path": "main.tf"}),
    ]
    history = [user, Message(role="assistant", content=blocks)]
    normal = provider._build_kwargs(history, "system", None, 8192)
    assert normal["messages"][1]["content"][0]["signature"] == "signed"
    # Moving the signed block past text changes the prefix before that block.
    history[1] = Message(role="assistant", content=[blocks[1], blocks[0], blocks[2]])
    changed = provider._build_kwargs(history, "system", None, 8192)
    assert [block["type"] for block in changed["messages"][1]["content"]] == ["text", "tool_use"]


@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    "effort,expected", [("minimal", "low"), ("medium", "high"), ("xhigh", "high"), ("ultra", "max")]
)
def test_deepseek_documented_effort_aliases_reach_both_api_surfaces(api_mode, effort, expected):
    provider = create_provider(
        "deepseek-flash",
        {"deepseek": "fake"},
        provider_key_override="deepseek",
        provider_config_override={"models": {"deepseek-flash": {"apiMode": api_mode, "effort": effort}}},
    )
    if api_mode == "responses":
        request = provider._build_responses_kwargs([Message.user("test")], "system", None, 8192, streaming=False)
        assert request["reasoning"] == {"effort": expected}
    else:
        assert provider._build_thinking_kwargs() == {
            "reasoning_effort": expected,
            "extra_body": {"thinking": {"type": "enabled"}},
        }


def test_argon_minimal_alias_remains_low_instead_of_becoming_default_medium():
    provider = create_provider(
        "gemini-4-argon",
        {"gemini": "fake"},
        provider_key_override="gemini",
        provider_config_override={"effort": "minimal"},
    )
    assert provider._build_thinking_kwargs() == {"reasoning_effort": "low"}


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_deepseek_responses_can_disable_thinking_without_adding_none_to_chat_efforts(model):
    assert get_thinking_spec("deepseek", model).effort_values == ("low", "high", "max")
    provider = create_provider(
        model,
        {"deepseek": "fake"},
        provider_key_override="deepseek",
        provider_config_override={"models": {model: {"apiMode": "responses", "thinkingEnabled": False}}},
    )
    request = provider._build_responses_kwargs([Message.user("test")], "system", None, 8192, streaming=False)
    assert request["reasoning"] == {"effort": "none"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
async def test_claude_keeps_valid_signatures_across_three_serialized_tool_turns(model):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.agent.message import Message as AgentMessage
    from iac_code.agent.message import TextBlock, ThinkingBlock, ToolUseBlock

    client = FakeAnthropicClient()
    provider = AnthropicProvider(model=model, client=client)
    history = [Message.user("inspect template")]
    for turn in range(3):
        client.messages._create_response = ns(
            id=f"message-{turn}",
            content=[
                ns(type="thinking", thinking="", signature=f"signed-{turn}"),
                ns(type="text", text=f"turn {turn}"),
                ns(type="tool_use", id=f"call-{turn}", name="read_file", input={"path": f"{turn}.tf"}),
            ],
            stop_reason="tool_use",
            usage=ns(input_tokens=10, output_tokens=20),
        )
        response = await provider.complete(history, "system")
        wire_assistants = [
            message for message in client.messages.create_calls[-1]["messages"] if message["role"] == "assistant"
        ]
        assert [message["content"][0]["signature"] for message in wire_assistants] == [
            f"signed-{previous}" for previous in range(turn)
        ]
        saved = AgentMessage(
            role="assistant",
            content=[
                ThinkingBlock(thinking="", provider_metadata=response.thinking_blocks[0]["provider_metadata"]),
                TextBlock(text=response.text),
                ToolUseBlock(id=f"call-{turn}", name="read_file", input={"path": f"{turn}.tf"}),
            ],
        )
        restored = AgentMessage.model_validate_json(saved.model_dump_json())
        history.extend(
            [
                AgentLoop._provider_message_from_api(restored.to_api_format()),
                Message.tool_result(tool_use_id=f"call-{turn}", content=f"result {turn}"),
            ]
        )
    wire = provider._build_kwargs(history, "system", None, 8192)
    assert (
        sum(
            block["type"] == "thinking"
            for message in wire["messages"]
            for block in message["content"]
            if isinstance(block, dict)
        )
        == 3
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_deepseek_responses_plain_reasoning_survives_two_tool_turns_with_real_sdk(streaming):
    import json

    import httpx
    from openai import AsyncOpenAI

    from iac_code.providers.base import ToolDefinition
    from iac_code.providers.responses_provider import ResponsesProvider
    from iac_code.types.stream_events import ToolUseEndEvent
    from tests.providers._responses_fakes import call, message, response, terminal

    tools = [
        ToolDefinition("lookup", "Look up a value", {"type": "object", "properties": {"value": {"type": "integer"}}})
    ]
    raw_reasoning = {
        "type": "reasoning",
        "id": "rs1",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "inspect"}],
    }
    first = response([raw_reasoning, call()])
    second = response([message("done")])
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        data = first if len(requests) == 1 else second
        if not streaming:
            return httpx.Response(200, json=data)
        events = [{"type": "response.reasoning_text.delta", "delta": "inspect"}] if len(requests) == 1 else []
        events.append(terminal(data))
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        async with AsyncOpenAI(api_key="fake", base_url="https://api.deepseek.com", http_client=http_client) as sdk:
            provider = ResponsesProvider(model="deepseek-flash", provider_key="deepseek", client=sdk)
            history = [Message.user("inspect template")]
            if streaming:
                events = [event async for event in provider.stream(history, "system", tools)]
                assert "".join(event.text for event in events if isinstance(event, ThinkingDeltaEvent)) == "inspect"
                assert any(isinstance(event, ToolUseEndEvent) for event in events)
                result = events[-1]
            else:
                result = await provider.complete(history, "system", tools)
                assert result.thinking == "inspect"
            metadata = json.loads(json.dumps(result.provider_metadata))
            history.extend(
                [
                    Message(role="assistant", metadata=metadata),
                    Message.tool_result(tool_use_id="call1", content="found"),
                ]
            )
            if streaming:
                events = [event async for event in provider.stream(history, "system", tools)]
                assert events[-1].stop_reason == "end_turn"
            else:
                result = await provider.complete(history, "system", tools)
                assert result.text == "done"
    assert [item for item in requests[1]["input"] if item.get("type") == "reasoning"] == [raw_reasoning]
