"""OpenRouter provider — OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from iac_code.providers.openai_provider import OpenAIProvider


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter — OpenAI-compatible endpoint."""

    _PROVIDER_KEY = "openrouter"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "openrouter",
        thinking_enabled: bool | None = None,
        thinking_budget: int | None = None,
        max_completion_tokens: int | None = None,
        **kwargs: Any,
    ) -> None:
        client = kwargs.pop("client", None)
        if client is None:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers={
                    "HTTP-Referer": "https://github.com/aliyun/iac-code",
                    "X-Title": "iac-code",
                },
            )
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            client=client,
            effort=effort,
            thinking_enabled=thinking_enabled,
            thinking_budget=thinking_budget,
            max_completion_tokens=max_completion_tokens,
            provider_key=provider_key,
        )
