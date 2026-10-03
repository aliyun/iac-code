import pytest

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.manager import create_provider
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.qwen_provider import QwenProvider
from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.providers.responses_provider import DashScopeResponsesProvider, ResponsesProvider
from iac_code.providers.thinking import get_thinking_spec
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
        ("openai_compatible", "custom"),
        ("dashscope_token_plan", "qwen3.8-max"),
        ("dashscope", "qwen3-coder-plus"),
    ],
)
def test_unsupported_responses_provider_or_model_fails(key, model):
    with pytest.raises(ValueError):
        create(key, model, {"models": {model: {"apiMode": "responses"}}})


@pytest.mark.parametrize(
    "model,effort,allowed",
    [
        ("gpt-6-astra", "none", False),
        ("gpt-6-sol", "high", False),
        ("gpt-6-sol", "none", True),
        ("gpt-6-luna", "none", True),
    ],
)
def test_gpt6_forced_chat_checks_tool_restrictions(model, effort, allowed):
    p = create("openai", model, {"effort": effort, "models": {model: {"apiMode": "chat_completions"}}})
    context = p._create_chat_request_context(streaming=False)
    if allowed:
        assert p._build_chat_completion_kwargs([], "system", TOOLS, 100, context)["reasoning_effort"] == "none"
    else:
        with pytest.raises(ValueError, match="requires apiMode=responses"):
            p._build_chat_completion_kwargs([], "system", TOOLS, 100, context)


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
