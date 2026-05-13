"""DashScope provider — Aliyun DashScope's OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_TOKEN_PLAN_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


class DashScopeProvider(OpenAIProvider):
    """Provider backed by Aliyun DashScope's OpenAI-compatible endpoint.

    Both standard DashScope and DashScope Token Plan share the same wire
    protocol (extra_body.enable_thinking=True); only the base URL and
    thinking-registry key differ. Both are injected via __init__.
    """

    _PROVIDER_KEY = "dashscope"
    supports_stream_options = True

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        effort: str | None = None,
        base_url: str = DASHSCOPE_BASE_URL,
        provider_key: str = "dashscope",
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            effort=effort,
        )
        # Instance attribute shadows the class attribute so per-variant
        # thinking-registry lookups resolve to the right MODEL_THINKING bucket.
        self._PROVIDER_KEY = provider_key

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.DASHSCOPE:
            return {}
        return {"extra_body": {"enable_thinking": True}}
