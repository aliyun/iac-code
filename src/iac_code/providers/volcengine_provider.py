"""Volcengine provider — OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from iac_code.providers.base import ContentBlock, ToolDefinition
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.streaming import OpenAIStreamResponseAdapter
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec
from iac_code.types.stream_events import StreamEvent, ThinkingDeltaEvent


class _VolcengineStreamAdapter(OpenAIStreamResponseAdapter):
    """Accumulate encrypted deltas within one request, including metadata-only chunks."""

    def __init__(self, provider: Any, tools: list[ToolDefinition] | None) -> None:
        super().__init__(provider, tools)
        self._encrypted_content = ""

    def feed(self, delta: Any, finish_reason: str | None) -> list[StreamEvent]:
        events = super().feed(delta, finish_reason)
        # The base adapter deduplicates complete metadata. Ark sends fragments,
        # so even two identical adjacent chunks must both be preserved.
        events = [event for event in events if not isinstance(event, ThinkingDeltaEvent) or not event.provider_metadata]
        metadata = self._provider._message_provider_metadata(delta)
        if metadata:
            self._encrypted_content += metadata["encrypted_content"]
            events.insert(
                0,
                ThinkingDeltaEvent(
                    text="", provider_metadata={**metadata, "encrypted_content": self._encrypted_content}
                ),
            )
        return events


class VolcengineProvider(OpenAIProvider):
    """Volcengine — OpenAI-compatible endpoint."""

    _PROVIDER_KEY = "volcengine_cn"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        provider_key: str = "volcengine_cn",
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
            **kwargs,
        )

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.VOLCENGINE:
            return {}
        if self._thinking_disabled() and spec.supports_disable:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        effort = self._effective_effort(spec)
        if effort is None:
            return {}
        selected = effort if effort in spec.effort_values else spec.default_effort_value
        return {"reasoning_effort": selected} if selected is not None else {}

    def _token_limit_kwargs(self, max_tokens: int) -> dict[str, int]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.VOLCENGINE:
            return super()._token_limit_kwargs(max_tokens)
        limit = self._output_token_limit(max_tokens)
        return {"max_completion_tokens": limit} if limit is not None else {}

    def _create_stream_response_adapter(self, tools: list[ToolDefinition] | None) -> OpenAIStreamResponseAdapter:
        return _VolcengineStreamAdapter(self, tools)

    def _message_provider_metadata(self, message: Any) -> dict[str, Any]:
        encrypted = getattr(message, "encrypted_content", None)
        if not isinstance(encrypted, str) or not encrypted:
            return {}
        return {
            "provider": self._PROVIDER_KEY,
            "model": self._model,
            "endpoint": self._metadata_endpoint_id,
            "encrypted_content": encrypted,
        }

    def _convert_content_blocks(self, role: str, blocks: list[ContentBlock]) -> list[dict[str, Any]]:
        messages = super()._convert_content_blocks(role, blocks)
        if role != "assistant":
            return messages
        for block in blocks:
            metadata = block.provider_metadata
            if (
                block.type != "thinking"
                or metadata.get("provider") != self._PROVIDER_KEY
                or metadata.get("model") != self._model
                or metadata.get("endpoint") != self._metadata_endpoint_id
            ):
                continue
            encrypted = metadata.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted and messages:
                messages[0]["encrypted_content"] = encrypted
        return messages
