"""Opt-in Responses providers for OpenAI-style transports."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlsplit

from iac_code.providers.base import Message, NonStreamingResponse, ToolDefinition
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.request_headers import get_provider_request_headers
from iac_code.providers.request_logging import log_provider_request_policy
from iac_code.providers.responses_codec import (
    ResponsesConfigurationError,
    ResponsesContextLimitError,
    ResponsesProtocolError,
    ResponsesStreamAdapter,
    decode_response,
    encode_input,
    encode_tools,
)
from iac_code.providers.thinking import get_thinking_spec, normalize_effort
from iac_code.providers.thinking_intent import ResolvedThinkingIntent
from iac_code.types.stream_events import MessageEndEvent, MessageStartEvent, StreamEvent


def validate_responses_endpoint(base_url: str | None) -> None:
    """Validate URL syntax without restricting providers, hosts, ports, or models."""
    try:
        endpoint = urlsplit(base_url or "https://api.openai.com/v1")
        endpoint.port
    except ValueError as error:
        raise ResponsesConfigurationError("Responses API requires a valid HTTP or HTTPS base URL.") from error
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise ResponsesConfigurationError("Responses API requires a valid HTTP or HTTPS base URL.")


class ResponsesProvider(OpenAIProvider):
    """Reuse SDK client setup; keep all Chat wire conversion out of this path."""

    api_mode = "responses"
    input_window_ratio = 1.0
    responses_profile = "openai"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        actual_base_url = getattr(self._client, "base_url", None)
        validate_responses_endpoint(str(actual_base_url) if actual_base_url else self._base_url)
        self._responses_identity = {
            "profile": self.responses_profile,
            "provider": self._PROVIDER_KEY,
            "model": self._model,
            "endpoint": self._metadata_endpoint_id,
        }

    def _responses_effort(self) -> str | None:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        effort = normalize_effort(self._effort)
        if self._thinking_disabled():
            effort = "none"
        elif effort in {None, "auto"}:
            effort = spec.default_effort_value
        if effort is not None and spec.effort_values and effort not in spec.effort_values:
            if effort == "none":
                raise ResponsesConfigurationError(
                    "The selected reasoning effort is not supported by this Responses model."
                )
            effort = spec.default_effort_value
        return effort

    def _build_responses_kwargs(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        *,
        streaming: bool,
    ) -> dict[str, Any]:
        limit = self._max_completion_tokens or max_tokens
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ResponsesConfigurationError("Responses max_output_tokens must be a positive integer.")
        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": encode_input(messages, self._responses_identity),
            "max_output_tokens": limit,
            "store": False,
            "stream": streaming,
        }
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = encode_tools(tools, strict=self.responses_profile == "openai")
        effort = self._responses_effort()
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}
        if self.responses_profile == "openai":
            kwargs["include"] = ["reasoning.encrypted_content"]
        headers = get_provider_request_headers()
        if headers:
            kwargs["extra_headers"] = dict(headers)
        return kwargs

    async def _create_response(self, kwargs: dict[str, Any]) -> Any:
        try:
            return await self._client.responses.create(**kwargs)
        except Exception as error:
            body = getattr(error, "body", None)
            code = getattr(error, "code", None)
            if isinstance(body, dict):
                detail = body.get("error", body)
                if isinstance(detail, dict):
                    code = detail.get("code", code)
            if code in {"context_length_exceeded", "context_window_exceeded", "max_input_tokens"}:
                raise ResponsesContextLimitError("Responses input exceeds the model context limit.") from error
            raise

    async def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ) -> AsyncGenerator[StreamEvent, None]:
        kwargs = self._build_responses_kwargs(messages, system, tools, max_tokens, streaming=True)
        log_provider_request_policy(self._PROVIDER_KEY, self._model, "responses.stream", kwargs)
        yield MessageStartEvent(message_id=str(uuid.uuid4()))
        response = await self._create_response(kwargs)
        adapter = ResponsesStreamAdapter(self._responses_identity, tools)
        try:
            async for event in response:
                for unified in adapter.feed(event):
                    yield unified
                if adapter.terminal is not None:
                    terminal = adapter.terminal
                    yield MessageEndEvent(
                        stop_reason=terminal.stop_reason,
                        usage=terminal.usage,
                        provider_metadata=terminal.provider_metadata,
                    )
                    return
            raise ResponsesProtocolError("Responses stream ended without a terminal response.")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                await close()

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        cache_policy: str = "default",
    ) -> NonStreamingResponse:
        kwargs = self._build_responses_kwargs(messages, system, tools, max_tokens, streaming=False)
        log_provider_request_policy(self._PROVIDER_KEY, self._model, "responses.create", kwargs)
        response = await self._create_response(kwargs)
        return decode_response(response, self._responses_identity, tools)


class DashScopeResponsesProvider(ResponsesProvider):
    # Bailian silently truncates at about 80%; leave additional local headroom.
    input_window_ratio = 0.75
    responses_profile = "dashscope"

    def __init__(self, *args: Any, thinking_intent: ResolvedThinkingIntent | None = None, **kwargs: Any) -> None:
        self._thinking_intent = thinking_intent
        kwargs.setdefault("provider_key", "dashscope")
        kwargs.setdefault("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        super().__init__(*args, **kwargs)

    def _responses_effort(self) -> str | None:
        intent = self._thinking_intent
        effort = normalize_effort(self._effort if intent is None else intent.effort.value)
        disabled = self._thinking_disabled()
        if intent is not None:
            if intent.enabled.value is not None:
                disabled = not intent.enabled.value
            dominant = intent.dominant_concrete_field()
            if dominant == "disabled":
                disabled = True
            elif dominant in {"effort", "budget"}:
                disabled = False
        if disabled:
            return "none"
        default_effort = "xhigh" if self._model.startswith("qwen3.8-") else None
        if effort in {None, "auto"}:
            return default_effort
        if self._model.startswith("qwen3.8-"):
            effort = {"minimal": "low", "high": "xhigh", "max": "xhigh"}.get(effort, effort)
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            # Ignore saved efforts from other families, using this protocol's default.
            return default_effort
        return effort

    def _build_responses_kwargs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super()._build_responses_kwargs(*args, **kwargs)
        if result["max_output_tokens"] < 16:
            raise ResponsesConfigurationError("DashScope Responses max_output_tokens must be at least 16.")
        return result
