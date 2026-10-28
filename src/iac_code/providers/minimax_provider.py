"""MiniMax provider — Anthropic-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec, normalize_effort


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
        thinking_enabled: bool | None = None,
        thinking_budget: int | None = None,
        max_completion_tokens: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            effort=effort,
            thinking_enabled=thinking_enabled,
            thinking_budget=thinking_budget,
            max_completion_tokens=max_completion_tokens,
            provider_key=provider_key,
        )
        self._PROVIDER_KEY = provider_key

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.MINIMAX:
            return {}
        if spec.supports_effort:
            kwargs: dict[str, Any] = {"thinking": {"type": "adaptive"}}
            effort = normalize_effort(self._effort)
            if effort in {None, "auto"}:
                effort = spec.default_effort_value if self._thinking_forced() else None
            if effort is not None:
                selected = effort if effort in spec.effort_values else spec.default_effort_value
                if selected is not None:
                    kwargs["output_config"] = {"effort": selected}
            return kwargs
        if self._thinking_disabled():
            return {"thinking": {"type": "disabled"}}
        if self._model == "MiniMax-M3":
            return {"thinking": {"type": "adaptive"}}
        return {}
