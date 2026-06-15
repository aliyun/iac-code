"""MiniMax provider — Anthropic-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec


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
            provider_key=provider_key,
        )
        self._PROVIDER_KEY = provider_key

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.MINIMAX:
            return {}
        if self._model == "MiniMax-M3":
            return {"thinking": {"type": "adaptive"}}
        return {}
