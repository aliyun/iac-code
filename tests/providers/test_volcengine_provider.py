"""Ark Chat keeps encrypted reasoning private and replays it in tool loops."""

import json

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message as AgentMessage
from iac_code.agent.message import ThinkingBlock, ToolUseBlock
from iac_code.providers.base import ContentBlock, Message
from iac_code.providers.request_logging import build_provider_request_policy_log
from iac_code.providers.thinking import get_thinking_spec, resolve_thinking_active
from iac_code.providers.volcengine_provider import VolcengineProvider
from iac_code.services.telemetry.content_serializer import serialize_input_messages
from iac_code.services.token_counter import TokenCounter
from iac_code.types.stream_events import ThinkingDeltaEvent
from iac_code.ui.stream_accumulator import StreamAccumulator
from tests.providers._fakes import FakeOpenAIClient, ns

MODEL = "doubao-seed-2-1-pro-260915"
BASE = "https://ark.cn-beijing.volces.com/api/v3"


@pytest.mark.parametrize("key,model", [("volcengine_cn", MODEL), ("volcengine_cn_codingplan", "doubao-seed-2.1-pro")])
def test_seed_effort_and_disable_stay_on_ark_wire_format(key, model):
    p = VolcengineProvider(model=model, api_key="fake", provider_key=key, effort="max")
    assert p._build_thinking_kwargs() == {"reasoning_effort": "high"}
    p._thinking_enabled = False
    assert p._build_thinking_kwargs() == {"extra_body": {"thinking": {"type": "disabled"}}}
    assert p._token_limit_kwargs(4096) == {"max_completion_tokens": 4096}


@pytest.mark.parametrize(
    "model,alias,wire,active",
    [
        (MODEL, "minimal", "none", False),
        (MODEL, "none", "none", False),
        (MODEL, "xhigh", "high", True),
        ("deepseek-v4-pro-ga-260813", "minimal", "none", False),
        ("deepseek-v4-pro-ga-260813", "medium", "low", True),
        ("deepseek-v4-1-flash-261017", "minimal", "low", True),
        ("deepseek-v4-1-flash-261017", "medium", "high", True),
        ("deepseek-v4-1-flash-261017", "ultra", "max", True),
        ("glm-5-3-flash-260828", "none", "low", True),
        ("glm-5-3-flash-260828", "medium", "high", True),
        ("kimi-k3-1-pro-261024", "medium", "high", True),
        ("kimi-k3-1-pro-261024", "xhigh", "max", True),
    ],
)
def test_effort_aliases_follow_each_models_mapping_and_toggle_state(model, alias, wire, active):
    p = VolcengineProvider(model=model, api_key="fake", effort=alias)
    assert p._build_thinking_kwargs() == {"reasoning_effort": wire}
    assert resolve_thinking_active("volcengine_cn", model, None, alias) is active
    assert alias not in get_thinking_spec("volcengine_cn", model).effort_values or alias == "none"


def _saved_tool_turn(metadata):
    return AgentMessage(
        role="assistant",
        content=[
            ThinkingBlock(thinking="summary", provider_metadata=metadata),
            ToolUseBlock(id="call-1", name="read_file", input={"path": "main.tf"}),
        ],
    )


@pytest.mark.asyncio
async def test_non_streaming_encrypted_reasoning_survives_session_roundtrip_and_tool_result():
    raw = ns(
        content=None,
        reasoning_content="summary",
        encrypted_content="opaque-payload",
        tool_calls=[
            ns(id="call-1", function=ns(name="read_file", arguments='{"path":"main.tf"}')),
        ],
    )
    client = FakeOpenAIClient(
        base_url=BASE,
        create_response=ns(
            id="response-1",
            choices=[ns(message=raw, finish_reason="tool_calls")],
            usage=None,
        ),
    )
    p = VolcengineProvider(model=MODEL, client=client, base_url=BASE)
    response = await p.complete([Message.user("read template")], "system")
    assert response.text == ""
    assert response.thinking == "summary"
    assert response.tool_uses[0]["id"] == "call-1"
    metadata = response.thinking_blocks[0]["provider_metadata"]
    saved = _saved_tool_turn(metadata)
    restored = AgentMessage.model_validate_json(saved.model_dump_json())
    provider_message = AgentLoop._provider_message_from_api(restored.to_api_format())
    await p.complete([provider_message, Message.tool_result(tool_use_id="call-1", content="template")], "system")
    history = client.chat.completions.calls[-1]["messages"]
    assert history[1]["encrypted_content"] == "opaque-payload"
    assert history[1]["reasoning_content"] == "summary"
    assert history[2]["tool_call_id"] == "call-1"
    assert "opaque-payload" not in serialize_input_messages([provider_message])
    assert "opaque-payload" not in json.dumps(
        build_provider_request_policy_log(
            "volcengine_cn",
            MODEL,
            "chat.completions.create",
            client.chat.completions.calls[-1],
        )
    )
    counter = TokenCounter(model=MODEL)
    assert counter.count_message(saved.to_api_format()) > counter.count_message(_saved_tool_turn({}).to_api_format())


def test_stream_encrypted_fragments_are_request_local_and_never_rendered():
    p = VolcengineProvider(model=MODEL, api_key="fake", base_url=BASE)
    adapter = p._create_stream_response_adapter(None)
    acc = StreamAccumulator()
    latest = None
    # Identical neighboring fragments must not be deduplicated as whole metadata.
    for fragment in ("opaque-", "same", "same"):
        for event in adapter.feed(ns(encrypted_content=fragment), None):
            assert isinstance(event, ThinkingDeltaEvent)
            assert event.is_metadata_only
            assert acc.process(event) == "none"
            latest = event.provider_metadata
    assert latest["encrypted_content"] == "opaque-samesame"
    assert acc.thinking_buffer == ""
    assert acc._thinking_start_time is None
    second = p._create_stream_response_adapter(None)
    events = second.feed(ns(encrypted_content="fresh"), None)
    assert events[0].provider_metadata["encrypted_content"] == "fresh"


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "doubao-seed-2-1-lite-260915"},
        {"base_url": "https://other.example/v3"},
        {"provider_key": "volcengine_cn_codingplan"},
    ],
)
def test_encrypted_reasoning_is_not_replayed_across_identity_changes(changed):
    source = VolcengineProvider(model=MODEL, api_key="fake", base_url=BASE)
    metadata = source._message_provider_metadata(ns(encrypted_content="opaque"))
    target = VolcengineProvider(**{"model": MODEL, "api_key": "fake", "base_url": BASE, **changed})
    wire = target._convert_content_blocks(
        "assistant", [ContentBlock(type="thinking", text="summary", provider_metadata=metadata)]
    )
    assert "encrypted_content" not in wire[0]
