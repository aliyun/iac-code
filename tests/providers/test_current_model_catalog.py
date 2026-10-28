"""Offline contracts for the official catalogs checked on 2026-10-28."""

from pathlib import Path

import pytest

from iac_code.config import _infer_provider_key_from_model
from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import ContentBlock, Message, ToolDefinition
from iac_code.providers.dashscope_provider import DashScopeProvider
from iac_code.providers.gemini_provider import GeminiProvider
from iac_code.providers.kimi_provider import KimiProvider
from iac_code.providers.minimax_provider import MiniMaxProvider
from iac_code.providers.registry import PROVIDER_REGISTRY
from iac_code.providers.thinking import get_thinking_spec
from iac_code.services.context_manager import get_context_window_config
from iac_code.services.telemetry.constants import KNOWN_MODELS
from iac_code.types.stream_events import ThinkingDeltaEvent
from tests.providers._fakes import FakeAnthropicClient, ns


def test_audit_covers_each_registry_provider_once():
    guide = Path(__file__).parents[2] / "scripts/provider-update-guide.zh-CN.md"
    audit = guide.read_text(encoding="utf-8").split("### 2026-10-28 全量复核", 1)[1]
    rows = [line.split("|")[1].strip() for line in audit.splitlines() if line.startswith("| ")]
    assert set(PROVIDER_REGISTRY) <= set(rows)
    for key in PROVIDER_REGISTRY:
        assert rows.count(key) == 1


def test_every_selectable_model_is_recognized_by_telemetry():
    assert {m.id for provider in PROVIDER_REGISTRY.values() for m in provider.models} <= KNOWN_MODELS


@pytest.mark.parametrize(
    "key,model",
    [
        ("openai", "gpt-6.1-sol"),
        ("openai", "gpt-6.1-terra"),
        ("anthropic", "claude-opus-5-5"),
        ("anthropic", "claude-sonnet-5-5"),
        ("anthropic", "claude-haiku-5-5"),
        ("gemini", "gemini-4-argon"),
        ("deepseek", "deepseek-flash"),
        ("kimi_cn", "kimi-k3.1-pro"),
        ("kimi_intl", "kimi-k3.1-pro-highspeed"),
        ("kimi_code", "k3.1-pro"),
        ("kimi_code", "k3.1-pro-highspeed"),
        ("minimax_cn", "MiniMax-M3.1-Flash"),
        ("minimax_intl", "MiniMax-M3.1-Flash"),
        ("dashscope", "deepseek-v4.1-flash-1017"),
        ("dashscope", "vanchin/deepseek-v4.1-flash"),
        ("dashscope", "MiniMax/MiniMax-M3.1-Flash"),
        ("dashscope", "ZHIPU/GLM-5.3-FlashX"),
        ("dashscope", "xiaomi/mimo-v2.6-pro-ultraspeed"),
        ("dashscope", "stepfun/step-5-preview"),
        ("volcengine_cn", "doubao-seed-evolving"),
    ],
)
def test_current_multimodal_models_have_runtime_capacity_and_telemetry(key, model):
    entry = next(m for m in PROVIDER_REGISTRY[key].models if m.id == model)
    assert entry.support_multimodal
    assert get_context_window_config(model).context_window >= 1_000_000
    assert model in KNOWN_MODELS
    assert get_thinking_spec(key, model).family.value != "none"


def test_subscription_catalogs_do_not_inherit_unsupported_standard_models():
    for key in ("zhipu_cn", "zhipu_intl"):
        assert "glm-5.3-flashx" in PROVIDER_REGISTRY[key].model_ids
    for key in ("zhipu_cn_codingplan", "zhipu_intl_codingplan"):
        assert "glm-5.3-flashx" not in PROVIDER_REGISTRY[key].model_ids
        assert get_thinking_spec(key, "glm-5.3-flashx").family.value == "none"
    assert set(PROVIDER_REGISTRY["aliyun_codingplan"].model_ids) == {
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.5-plus",
        "glm-5",
        "MiniMax-M2.5",
        "kimi-k2.5",
    }
    assert "qwen3-max-2026-01-23" in PROVIDER_REGISTRY["aliyun_codingplan_intl"].model_ids
    assert "qwen3-coder-plus" in PROVIDER_REGISTRY["aliyun_codingplan_intl"].model_ids
    for key in ("minimax_cn", "minimax_intl"):
        assert PROVIDER_REGISTRY[key].model_ids == ["MiniMax-M3.1-Flash", "MiniMax-M3"]
    token_plan = PROVIDER_REGISTRY["dashscope_token_plan"].model_ids
    assert "deepseek-v4.1-flash-1017" in token_plan
    assert "glm-5.3" in token_plan
    assert not any("/" in model for model in token_plan)
    assert "gemini-3.7-flash" not in PROVIDER_REGISTRY["gemini"].model_ids
    assert "gemini-3.5-flash" not in PROVIDER_REGISTRY["gemini"].model_ids
    assert "o4-mini" not in PROVIDER_REGISTRY["openai"].model_ids


def test_volcengine_plan_uses_plan_aliases_and_standard_uses_versioned_ids():
    assert PROVIDER_REGISTRY["volcengine_cn_codingplan"].model_ids == [
        "doubao-seed-evolving",
        "doubao-seed-2.1-pro",
        "doubao-seed-2.1-lite",
        "doubao-seed-2.0-mini",
        "glm-5.3",
        "glm-5.3-flash",
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash-economy",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "kimi-k2.8-preview",
        "kimi-k3",
        "kimi-k3.1-pro",
    ]
    assert "doubao-seed-2-1-pro-260915" in PROVIDER_REGISTRY["volcengine_cn"].model_ids
    assert "doubao-seed-2.1-pro" not in PROVIDER_REGISTRY["volcengine_cn"].model_ids
    assert "doubao-seed-2-0-code-preview-260215" not in PROVIDER_REGISTRY["volcengine_cn"].model_ids


@pytest.mark.parametrize(
    "model,thinking_type", [("claude-sonnet-5-5", "between_tools"), ("claude-haiku-5-5", "disabled")]
)
def test_claude55_disable_uses_model_specific_wire_format(model, thinking_type):
    p = AnthropicProvider(model=model, api_key="fake", thinking_enabled=False, effort="high")
    assert p._build_thinking_kwargs() == {"thinking": {"type": thinking_type}, "output_config": {"effort": "high"}}
    p._effort = "max"
    with pytest.raises(ValueError, match="cannot be disabled"):
        p._build_thinking_kwargs()


def test_opus55_never_sends_a_disabled_or_manual_budget_request():
    p = AnthropicProvider(
        model="claude-opus-5-5",
        api_key="fake",
        thinking_enabled=False,
        effort="max",
        thinking_budget=1024,
    )
    assert p._build_thinking_kwargs() == {"output_config": {"effort": "max"}}
    assert get_thinking_spec("anthropic", "claude-opus-5-5").default_effort_value == "medium"
    assert get_thinking_spec("anthropic", "claude-haiku-5-5").default_effort_value == "medium"


@pytest.mark.parametrize("effort,expected", [(None, None), ("low", "low"), ("max", "max"), ("none", "max")])
def test_minimax31_effort_stays_in_each_endpoints_protocol(effort, expected):
    direct = MiniMaxProvider(model="MiniMax-M3.1-Flash", api_key="fake", effort=effort, thinking_enabled=False)
    hosted = DashScopeProvider(
        model="MiniMax/MiniMax-M3.1-Flash", api_key="fake", effort=effort, thinking_enabled=False
    )
    direct_kwargs = direct._build_thinking_kwargs()
    hosted_kwargs = hosted._build_thinking_kwargs()
    assert direct_kwargs["thinking"] == {"type": "adaptive"}
    assert direct_kwargs.get("output_config") == ({"effort": expected} if expected else None)
    assert hosted_kwargs == ({"reasoning_effort": expected} if expected else {})


@pytest.mark.parametrize(
    "key,model,default",
    [
        ("kimi_cn", "kimi-k3.1-pro", "max"),
        ("kimi_intl", "kimi-k3.1-pro-highspeed", "max"),
        ("kimi_code", "k3.1-pro", "high"),
    ],
)
def test_kimi31_never_disables_thinking_and_keeps_endpoint_defaults(key, model, default):
    p = KimiProvider(model=model, api_key="fake", provider_key=key, effort="high")
    assert p._build_thinking_kwargs() == {"reasoning_effort": "high"}
    assert get_thinking_spec(key, model).default_effort_value == default
    p._thinking_enabled = False
    assert p._build_thinking_kwargs() == {"reasoning_effort": "high"}


@pytest.mark.parametrize("model", ["stepfun/step-3.7-flash", "stepfun/step-5-preview"])
def test_step_thinking_uses_only_the_documented_effort_parameter(model):
    p = DashScopeProvider(model=model, api_key="fake", effort="medium")
    assert p._build_thinking_kwargs() == {"reasoning_effort": "medium"}
    p._thinking_enabled = False
    assert p._build_thinking_kwargs() == {}


def test_argon_exposes_only_canonical_efforts_and_synchronous_output_limit():
    spec = get_thinking_spec("gemini", "gemini-4-argon")
    assert spec.effort_values == ("low", "medium", "high")
    p = GeminiProvider(model="gemini-4-argon", api_key="fake", effort="high")
    assert p._build_thinking_kwargs() == {"reasoning_effort": "high"}
    config = get_context_window_config("gemini-4-argon")
    assert (config.context_window, config.max_output_tokens) == (1_048_576, 65_536)


@pytest.mark.parametrize(
    "model,key",
    [
        ("k3.1-pro", "kimi_code"),
        ("deepseek-flash", "deepseek"),
        ("deepseek-v4.1-flash-1017", "dashscope"),
        ("kimi/kimi-k3.1-pro", "dashscope"),
        ("kimi-k3-1-pro-261024", "volcengine_cn"),
        ("deepseek-v4-1-flash-261017", "volcengine_cn"),
    ],
)
def test_new_model_names_resolve_to_their_own_endpoint(model, key):
    assert _infer_provider_key_from_model(model) == key


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
async def test_claude_bound_thinking_survives_tool_loop_but_not_prefix_changes(model):
    client = FakeAnthropicClient(
        create_response=ns(
            id="message-1",
            content=[
                ns(type="thinking", thinking="", signature="opaque-signed-block"),
                ns(type="tool_use", id="call-1", name="read_file", input={}),
            ],
            stop_reason="tool_use",
            usage=ns(input_tokens=10, output_tokens=20),
        )
    )
    p = AnthropicProvider(model=model, client=client)
    user = Message.user("inspect main.tf")
    tools = [ToolDefinition(name="read_file", description="Read", input_schema={"type": "object"})]
    response = await p.complete([user], "system", tools)
    block = response.thinking_blocks[0]
    assistant = Message(
        role="assistant",
        content=[
            ContentBlock(type="thinking", text=block["text"], provider_metadata=block["provider_metadata"]),
            ContentBlock(type="tool_use", tool_use_id="call-1", name="read_file", input={}),
        ],
    )
    history = [user, assistant, Message.tool_result(tool_use_id="call-1", content="template")]
    normal = p._build_kwargs(history, "system", tools, 8192)
    assert normal["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "opaque-signed-block",
    }
    for messages, system, next_tools in (
        (history, "new system", tools),
        ([Message.user("compacted conversation"), *history[1:]], "system", tools),
        (history, "system", [ToolDefinition(name="different_tool", description="Other", input_schema={})]),
    ):
        changed = p._build_kwargs(messages, system, next_tools, 8192)
        assert not any(
            b.get("type") == "thinking"
            for msg in changed["messages"]
            if isinstance(msg["content"], list)
            for b in msg["content"]
        )
        assert any(b.get("type") == "tool_use" for b in changed["messages"][1]["content"])


@pytest.mark.asyncio
async def test_claude_stream_binds_signed_and_redacted_blocks_to_request_prefix():
    client = FakeAnthropicClient(
        stream_events=[
            ns(type="content_block_start", index=0, content_block=ns(type="thinking", thinking="", signature="")),
            ns(type="content_block_delta", index=0, delta=ns(type="signature_delta", signature="signed")),
            ns(type="content_block_start", index=1, content_block=ns(type="redacted_thinking", data="opaque")),
            ns(type="content_block_start", index=2, content_block=ns(type="text", text="")),
            ns(type="content_block_delta", index=2, delta=ns(type="text_delta", text="done")),
        ],
        stream_final=ns(
            content=[
                ns(type="thinking", thinking="", signature="signed"),
                ns(type="redacted_thinking", data="opaque"),
                ns(type="text", text="done"),
            ],
            stop_reason="end_turn",
            usage=ns(input_tokens=10, output_tokens=20),
        ),
    )
    p = AnthropicProvider(model="claude-opus-5-5", client=client)
    user = Message.user("inspect template")
    events = [event async for event in p.stream([user], "system") if isinstance(event, ThinkingDeltaEvent)]
    metadata = {}
    for event in events:
        metadata.setdefault(event.block_index, {}).update(event.provider_metadata or {})
    assert metadata[0]["prefix"] == metadata[1]["prefix"]
    history = [
        user,
        Message(
            role="assistant",
            content=[
                ContentBlock(type="thinking", text="", provider_metadata=metadata[0]),
                ContentBlock(type="redacted_thinking", data="opaque", provider_metadata=metadata[1]),
                ContentBlock(type="text", text="done"),
            ],
        ),
    ]
    normal = p._build_kwargs(history, "system", None, 8192)
    assert [block["type"] for block in normal["messages"][1]["content"]] == ["thinking", "redacted_thinking", "text"]
    changed = p._build_kwargs(history, "updated system", None, 8192)
    assert changed["messages"][1]["content"] == [{"type": "text", "text": "done"}]
