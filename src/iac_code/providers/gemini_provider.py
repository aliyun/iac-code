"""Gemini provider — Google AI via OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec, normalize_effort

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class GeminiProvider(OpenAIProvider):
    """Provider backed by Google Gemini's OpenAI-compatible endpoint."""

    _PROVIDER_KEY = "gemini"
    supports_stream_options = True

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "gemini",
        **kwargs,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url or GEMINI_BASE_URL,
            effort=effort,
            provider_key=provider_key,
        )

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.GEMINI:
            return {}
        effort = normalize_effort(self._effort)
        if effort is None or effort == "auto":
            return {}
        allowed = {e.value for e in spec.allowed_efforts}
        if effort not in allowed:
            if spec.default_effort is None:
                return {}
            effort = spec.default_effort.value
        return {"reasoning_effort": effort}
