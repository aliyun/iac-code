"""ZhiPu provider — OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec, normalize_effort


class ZhiPuProvider(OpenAIProvider):
    """ZhiPu — OpenAI-compatible endpoint."""

    _PROVIDER_KEY = "zhipu_cn"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "zhipu_cn",
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
        if spec.family is not ThinkingFamily.ZHIPU:
            return {}
        if self._thinking_disabled():
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        kwargs: dict[str, Any] = {"extra_body": {"thinking": {"type": "enabled"}}}
        if not spec.uses_reasoning_effort_param:
            return kwargs
        effort = normalize_effort(self._effort)
        allowed = {item.value for item in spec.allowed_efforts}
        if effort in allowed:
            kwargs["reasoning_effort"] = effort
        elif effort not in {None, "auto"} and spec.default_effort is not None:
            kwargs["reasoning_effort"] = spec.default_effort.value
        return kwargs
