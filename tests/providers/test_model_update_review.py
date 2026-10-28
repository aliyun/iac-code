"""Regression cases found by reviewing the 2026-10 provider update."""

import pytest

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import ContentBlock, Message
from iac_code.providers.manager import create_provider
from iac_code.providers.thinking import resolve_thinking_active
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
