"""Kimi provider — OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec


class KimiProvider(OpenAIProvider):
    """Kimi — OpenAI-compatible endpoint."""

    _PROVIDER_KEY = "kimi_cn"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "kimi_cn",
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
        if spec.family is not ThinkingFamily.KIMI:
            return {}
        if self._model == "kimi-k2.7-code":
            return {}
        return {"extra_body": {"thinking": {"type": "enabled"}}}
