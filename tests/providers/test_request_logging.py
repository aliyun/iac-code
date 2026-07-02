from iac_code.providers.request_logging import build_provider_request_policy_log


def test_provider_request_policy_log_keeps_only_safe_thinking_fields() -> None:
    payload = build_provider_request_policy_log(
        "dashscope",
        "glm-5.2",
        "chat.completions.stream",
        {
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "secret prompt"}],
            "system": "secret system prompt",
            "tools": [{"function": {"description": "secret tool"}}],
            "api_key": "sk-secret",
            "stream": True,
            "max_completion_tokens": 10240,
            "reasoning_effort": "low",
            "extra_body": {
                "enable_thinking": True,
                "thinking_budget": 2048,
                "prompt_cache_key": "do-not-log",
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": 2048,
                    "text": "hidden reasoning",
                },
            },
        },
    )

    assert payload == {
        "provider": "dashscope",
        "model": "glm-5.2",
        "operation": "chat.completions.stream",
        "request": {
            "stream": True,
            "max_completion_tokens": 10240,
            "reasoning_effort": "low",
            "extra_body": {
                "enable_thinking": True,
                "thinking_budget": 2048,
                "thinking": {"type": "enabled", "budget_tokens": 2048},
            },
        },
    }


def test_provider_request_policy_log_sanitizes_anthropic_thinking() -> None:
    payload = build_provider_request_policy_log(
        "anthropic",
        "claude-opus-4-7",
        "messages.create",
        {
            "model": "claude-opus-4-7",
            "system": "secret system prompt",
            "messages": [{"role": "user", "content": "secret prompt"}],
            "max_tokens": 20480,
            "thinking": {"type": "enabled", "budget_tokens": 16384, "signature": "do-not-log"},
        },
    )

    assert payload == {
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "operation": "messages.create",
        "request": {
            "max_tokens": 20480,
            "thinking": {"type": "enabled", "budget_tokens": 16384},
        },
    }


def test_provider_request_policy_log_keeps_disabled_flag_without_budget_or_effort() -> None:
    payload = build_provider_request_policy_log(
        "dashscope",
        "glm-5.2",
        "chat.completions.stream",
        {
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "secret prompt"}],
            "max_tokens": 8192,
            "extra_body": {"enable_thinking": False},
        },
    )

    assert payload == {
        "provider": "dashscope",
        "model": "glm-5.2",
        "operation": "chat.completions.stream",
        "request": {
            "max_tokens": 8192,
            "extra_body": {"enable_thinking": False},
        },
    }
