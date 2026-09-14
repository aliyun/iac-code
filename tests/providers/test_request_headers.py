from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import Message
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.qwen_provider import QwenProvider
from iac_code.providers.request_headers import (
    merge_provider_request_headers,
    use_provider_request_headers,
)


def test_merge_provider_request_headers_overrides_case_insensitively() -> None:
    assert merge_provider_request_headers(
        {"X-Provider": "default", "X-Keep": "yes"},
        {"x-provider": "a2a"},
    ) == {"X-Keep": "yes", "x-provider": "a2a"}


def test_openai_provider_adds_request_local_headers() -> None:
    provider = OpenAIProvider(model="gpt-5.5", api_key="test")

    with use_provider_request_headers({"X-A2A-Session": "session-1"}):
        kwargs = provider._build_chat_completion_kwargs(
            [Message.user("hello")],
            "system",
            None,
            1024,
            provider._create_chat_request_context(streaming=False),
        )

    assert kwargs["extra_headers"] == {"X-A2A-Session": "session-1"}


def test_qwen_provider_merges_adapter_and_request_local_headers() -> None:
    provider = QwenProvider(
        model="qwen3.7-plus",
        api_key="test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    with use_provider_request_headers({"X-A2A-Session": "session-1"}):
        kwargs = provider._build_chat_completion_kwargs(
            [Message.user("hello")],
            "system",
            None,
            1024,
            provider._create_chat_request_context(streaming=False),
        )

    assert kwargs["extra_headers"] == {
        "X-DashScope-CacheControl": "enable",
        "X-A2A-Session": "session-1",
    }


def test_anthropic_provider_merges_alias_and_request_local_headers() -> None:
    provider = AnthropicProvider(model="claude-sonnet-4-6-1m", api_key="test")

    with use_provider_request_headers({"X-A2A-Session": "session-1"}):
        kwargs = provider._build_kwargs([Message.user("hello")], "system", None, 1024)

    assert kwargs["extra_headers"] == {
        "anthropic-beta": "context-1m-2025-08-07",
        "X-A2A-Session": "session-1",
    }
