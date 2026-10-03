from dataclasses import replace

import pytest

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.manager import _telemetry_provider_name, create_provider
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.qwen_provider import QwenProvider
from iac_code.providers.registry import PROVIDER_REGISTRY, ModelEntry
from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.providers.responses_provider import DashScopeResponsesProvider, ResponsesProvider
from iac_code.providers.thinking import MODEL_THINKING, EffortLevel, ThinkingFamily, ThinkingSpec, get_thinking_spec
from iac_code.services.context_manager import get_context_window_config
from tests.providers.test_openai_responses_provider import TOOLS


def create(key, model, config=None):
    return create_provider(model, {key: "fake"}, provider_key_override=key, provider_config_override=config or {})


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
def test_gpt6_default_protocol_and_capabilities(model):
    assert type(create("openai", model)) is ResponsesProvider
    assert get_context_window_config(model).context_window == 1_050_000
    assert ("none" in get_thinking_spec("openai", model).effort_values) == (model != "gpt-6-astra")


def test_only_explicit_qwen_model_switches_protocol():
    cfg = {"models": {"qwen3.8-max": {"apiMode": "responses"}}}
    assert type(create("dashscope", "qwen3.8-max")) is QwenProvider
    assert type(create("dashscope", "qwen3.8-max", cfg)) is DashScopeResponsesProvider
    assert type(create("dashscope", "qwen3.8-flash", cfg)) is QwenProvider
    assert type(create("openai", "gpt-5.6-sol")) is OpenAIProvider
    assert type(create("anthropic", "claude-opus-5")) is AnthropicProvider


@pytest.mark.parametrize("mode", ["auto", "invalid", None, {}, []])
def test_invalid_model_api_mode_rejected(mode):
    with pytest.raises(ValueError, match="apiMode"):
        create("openai", "gpt-5.6-sol", {"models": {"gpt-5.6-sol": {"apiMode": mode}}})


@pytest.mark.parametrize(
    "key,model",
    [
        ("anthropic", "claude-opus-5"),
        ("anthropic_compatible", "custom"),
        ("minimax_cn", "MiniMax-M3"),
    ],
)
def test_non_openai_transports_reject_responses(key, model):
    with pytest.raises(ValueError, match="OpenAI-style provider"):
        create(key, model, {"models": {model: {"apiMode": "responses"}}})


@pytest.mark.parametrize(
    "key,base,expected,telemetry_name",
    [
        ("azure_openai", "https://resource.openai.azure.com/openai/v1/", ResponsesProvider, "azureopenai"),
        ("openai_compatible", "http://127.0.0.1:8765/custom/v1", ResponsesProvider, "openai"),
        ("openai", "https://proxy.example.test/v1", ResponsesProvider, "openai"),
        ("deepseek", None, ResponsesProvider, "deepseek"),
        ("openrouter", None, ResponsesProvider, "openrouter"),
        ("ollama", None, ResponsesProvider, "ollama"),
        ("lmstudio", None, ResponsesProvider, "lmstudio"),
        ("dashscope", None, DashScopeResponsesProvider, "dashscope"),
        ("dashscope_token_plan", None, DashScopeResponsesProvider, "dashscope"),
        ("aliyun_codingplan", None, DashScopeResponsesProvider, "dashscope"),
        ("aliyun_codingplan_intl", None, DashScopeResponsesProvider, "dashscope"),
        (
            "openai_compatible",
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            DashScopeResponsesProvider,
            "dashscope",
        ),
    ],
)
def test_openai_transports_can_explicitly_select_responses_for_new_models(key, base, expected, telemetry_name):
    model = "future-model"
    cfg = {"models": {model: {"apiMode": "responses"}}}
    if base is not None:
        cfg["apiBase"] = base
    assert not isinstance(create(key, model, {"apiBase": base} if base else {}), ResponsesProvider)
    p = create(key, model, cfg)
    assert type(p) is expected
    assert p._logical_provider_key == key
    assert p._responses_identity["profile"] == expected.responses_profile
    assert p._responses_identity["provider"] == p._PROVIDER_KEY
    assert _telemetry_provider_name(p) == telemetry_name
    assert type(create(key, "other-model", cfg)) is not expected


def test_responses_reuses_openrouter_client_headers():
    p = create("openrouter", "future-model", {"models": {"future-model": {"apiMode": "responses"}}})
    assert p._client.default_headers["HTTP-Referer"] == "https://github.com/aliyun/iac-code"
    assert p._client.default_headers["X-Title"] == "iac-code"


@pytest.mark.parametrize("key,default_key", [("ollama", "ollama"), ("lmstudio", "lm-studio")])
def test_responses_reuses_local_provider_default_api_key(key, default_key):
    p = create_provider(
        "local-model",
        {},
        provider_key_override=key,
        provider_config_override={"models": {"local-model": {"apiMode": "responses"}}},
    )
    assert p._client.api_key == default_key


@pytest.mark.parametrize(
    "model,effort,allowed",
    [
        ("gpt-6-astra", None, False),
        ("gpt-6-astra", "none", False),
        ("gpt-6-astra", "high", False),
        ("gpt-6-sol", None, False),
        ("gpt-6-sol", "high", False),
        ("gpt-6-sol", "none", True),
        ("gpt-6-luna", None, False),
        ("gpt-6-luna", "high", False),
        ("gpt-6-luna", "none", True),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
def test_gpt6_forced_chat_checks_tool_restrictions(model, effort, allowed, streaming):
    p = create("openai", model, {"effort": effort, "models": {model: {"apiMode": "chat_completions"}}})
    context = p._create_chat_request_context(streaming=streaming)
    assert "tools" not in p._build_chat_completion_kwargs([], "system", None, 100, context)
    if allowed:
        assert p._build_chat_completion_kwargs([], "system", TOOLS, 100, context)["reasoning_effort"] == "none"
    else:
        with pytest.raises(ValueError, match="requires apiMode=responses"):
            p._build_chat_completion_kwargs([], "system", TOOLS, 100, context)


@pytest.mark.parametrize(
    "tool_efforts,effort,allowed",
    [
        (None, "high", True),
        ((), None, False),
        (("none",), None, False),
        (("none",), "none", True),
        (("high",), "high", True),
    ],
)
def test_chat_tool_restrictions_follow_model_configuration(monkeypatch, tool_efforts, effort, allowed):
    model = "test-configured-model"
    monkeypatch.setitem(
        PROVIDER_REGISTRY,
        "openai",
        replace(
            PROVIDER_REGISTRY["openai"],
            models=[ModelEntry(model, chat_completions_tool_efforts=tool_efforts)],
        ),
    )
    monkeypatch.setitem(
        MODEL_THINKING["openai"],
        model,
        ThinkingSpec(
            ThinkingFamily.OPENAI, (EffortLevel.NONE, EffortLevel.MEDIUM, EffortLevel.HIGH), EffortLevel.MEDIUM
        ),
    )
    p = OpenAIProvider(model, client=object(), effort=effort)
    context = p._create_chat_request_context(streaming=False)
    if allowed:
        assert p._build_chat_completion_kwargs([], "system", TOOLS, 100, context)["tools"]
    else:
        with pytest.raises(ValueError, match="requires apiMode=responses"):
            p._build_chat_completion_kwargs([], "system", TOOLS, 100, context)


@pytest.mark.parametrize(
    "key,model",
    [
        ("openai", "gpt-5.6-sol"),
        ("openai", "test-unknown-model"),
        ("openai_compatible", "gpt-6-astra"),
        ("azure_openai", "gpt-6-astra"),
    ],
)
def test_chat_tool_restrictions_do_not_affect_other_models_or_providers(key, model):
    p = OpenAIProvider(model, client=object(), provider_key=key, effort="high")
    context = p._create_chat_request_context(streaming=False)
    assert p._build_chat_completion_kwargs([], "system", TOOLS, 100, context)["tools"]


def test_gpt6_astra_responses_cannot_disable_reasoning():
    p = create("openai", "gpt-6-astra", {"thinkingEnabled": False})
    with pytest.raises(ValueError, match="reasoning effort"):
        p._build_responses_kwargs([], "system", None, 100, streaming=False)


@pytest.mark.parametrize(
    "key,model,expected",
    [
        ("openai", "gpt-6-astra", "medium"),
        ("openai", "gpt-6-sol", "medium"),
        ("openai", "gpt-6-luna", "medium"),
        ("openai", "gpt-5.6-sol", "medium"),
        ("dashscope", "qwen3.8-max", "xhigh"),
        ("dashscope", "qwen3.8-flash", "xhigh"),
        ("dashscope", "qwen3.8-2.4t-a95b", "xhigh"),
        ("dashscope", "qwen3.8-27b", "xhigh"),
        ("dashscope", "qwen3.8-omni-flash", "xhigh"),
        ("dashscope", "qwen3.7-plus", None),
        ("dashscope", "qwen3.6-plus", None),
        ("dashscope", "qwen3.6-flash", None),
    ],
)
def test_responses_saved_unsupported_effort_uses_protocol_default(key, model, expected):
    p = create(key, model, {"effort": "ultra", "models": {model: {"apiMode": "responses"}}})
    kwargs = p._build_responses_kwargs([], "system", None, 100, streaming=False)
    assert kwargs.get("reasoning") == ({"effort": expected} if expected is not None else None)
    assert p._effort == "ultra"


@pytest.mark.parametrize(
    "provider_config,model_config,request_policy,expected",
    [
        ({"thinkingEnabled": False}, {}, ProviderRequestPolicy(effort="low"), "low"),
        ({"thinkingEnabled": False}, {"effort": "low"}, None, "low"),
        ({}, {"thinkingEnabled": False}, ProviderRequestPolicy(effort="low"), "low"),
        ({"thinkingEnabled": False, "effort": "low"}, {}, None, "none"),
        ({}, {"thinkingEnabled": False, "effort": "low"}, None, "none"),
        ({"effort": "low"}, {}, ProviderRequestPolicy(thinking_enabled=False, effort="low"), "none"),
        ({"effort": "none"}, {"thinkingEnabled": True}, None, "xhigh"),
    ],
)
def test_qwen_responses_preserves_thinking_source_precedence(provider_config, model_config, request_policy, expected):
    p = create_provider(
        "qwen3.8-max",
        {"dashscope": "fake"},
        provider_key_override="dashscope",
        provider_config_override={
            **provider_config,
            "models": {"qwen3.8-max": {"apiMode": "responses", **model_config}},
        },
        request_policy_override=request_policy,
    )
    kwargs = p._build_responses_kwargs([], "system", None, 100, streaming=False)
    assert kwargs["reasoning"] == {"effort": expected}
    assert "extra_body" not in kwargs
