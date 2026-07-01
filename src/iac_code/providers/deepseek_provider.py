"""DeepSeek provider — uses OpenAI-compatible API with thinking mode support."""

from __future__ import annotations

from iac_code.providers.openai_provider import OpenAIProvider

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider(OpenAIProvider):
    """Provider backed by DeepSeek's OpenAI-compatible endpoint.

    Wire format is identical to ``OpenAIProvider`` (``reasoning_effort`` +
    ``extra_body.thinking.type=enabled``); the registry's ``allowed_efforts``
    constrains the effort vocabulary to ``high`` / ``max`` for DeepSeek V4.

    Reasoning content is captured via ``reasoning_content`` in the stream
    and echoed back as ``reasoning_content`` on subsequent assistant
    messages (required by DeepSeek when tool calls are involved).
    """

    _PROVIDER_KEY = "deepseek"
    supports_stream_options = True

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "deepseek",
        thinking_enabled: bool | None = None,
        thinking_budget: int | None = None,
        max_completion_tokens: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url or DEEPSEEK_BASE_URL,
            effort=effort,
            thinking_enabled=thinking_enabled,
            thinking_budget=thinking_budget,
            max_completion_tokens=max_completion_tokens,
            provider_key=provider_key,
        )
