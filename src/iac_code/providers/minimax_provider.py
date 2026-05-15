"""MiniMax provider — Anthropic-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.anthropic_provider import AnthropicProvider


class MiniMaxProvider(AnthropicProvider):
    """MiniMax — Anthropic-compatible endpoint."""

    _PROVIDER_KEY = "minimax_cn"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "minimax_cn",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            effort=effort,
        )
        self._PROVIDER_KEY = provider_key
