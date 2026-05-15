"""LM Studio provider — OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.openai_provider import OpenAIProvider


class LMStudioProvider(OpenAIProvider):
    """LM Studio — OpenAI-compatible endpoint."""

    _PROVIDER_KEY = "lmstudio"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "lmstudio",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key or "lm-studio",
            base_url=base_url,
            effort=effort,
        )
        self._PROVIDER_KEY = provider_key
