"""Opt-in OpenAI and DashScope Responses providers."""

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

# Models documented in Bailian's Responses API reference, 2026-10-03.
DASHSCOPE_RESPONSES_MODELS = frozenset(
    {
        "qwen3.8-max",
        "qwen3.8-max-0902",
        "qwen3.8-flash",
        "qwen3.8-2.4t-a95b",
        "qwen3.8-27b",
        "qwen3.8-omni-flash",
        "qwen3.7-max",
        "qwen3.7-max-2026-05-20",
        "qwen3.7-max-2026-06-08",
        "qwen3.7-max-2026-05-17",
        "qwen3.7-max-preview",
        "qwen3-max",
        "qwen3-max-2026-01-23",
        "qwen3.7-plus",
        "qwen3.7-plus-2026-05-26",
        "qwen3.6-plus",
        "qwen3.6-plus-2026-04-02",
        "qwen3.5-plus",
        "qwen3.5-plus-2026-04-20",
        "qwen3.5-plus-2026-02-15",
        "qwen3.7-flash",
        "qwen3.7-flash-2026-07-15",
        "qwen3.6-flash",
        "qwen3.6-flash-2026-04-16",
        "qwen3.5-flash",
        "qwen3.5-flash-2026-02-23",
        "qwen3.6-35b-a3b",
        "qwen3.5-397b-a17b",
        "qwen3.5-122b-a10b",
        "qwen3.5-27b",
        "qwen3.5-35b-a3b",
    }
)
_DASHSCOPE_REGIONS = {
    "cn-beijing",
    "ap-southeast-1",
    "us-east-1",
    "eu-central-1",
    "ap-northeast-1",
    "cn-hongkong",
}


def validate_responses_endpoint(provider_key: str, base_url: str | None, model: str) -> None:
    if provider_key not in {"openai", "dashscope"}:
        raise ValueError("Responses API is supported only for OpenAI and standard DashScope in this release.")
    if provider_key == "dashscope" and model not in DASHSCOPE_RESPONSES_MODELS:
        raise ValueError("This DashScope model is not registered for Responses API.")
    endpoint = urlsplit(base_url or "https://api.openai.com/v1")
    if endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("Responses API requires a supported HTTPS base URL.")
    if endpoint.port not in {None, 443}:
        raise ValueError("Responses API requires a supported HTTPS base URL.")
    host = endpoint.hostname or ""
    if provider_key == "openai":
        supported = host == "api.openai.com" and endpoint.path.rstrip("/") == "/v1"
    else:
        workspace = host.split(".")
        supported = (
            host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
            or (
                len(workspace) == 5
                and workspace[0] not in {"token-plan", "coding", "coding-intl"}
                and workspace[1] in _DASHSCOPE_REGIONS
                and workspace[2:] == ["maas", "aliyuncs", "com"]
            )
        ) and endpoint.path.rstrip("/") == "/compatible-mode/v1"
    if not supported:
        raise ValueError("This base URL is not registered for Responses API.")


class ResponsesProvider(OpenAIProvider):
    """Reuse SDK client setup; keep all Chat wire conversion out of this path."""

    api_mode = "responses"
    input_window_ratio = 1.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        actual_base_url = getattr(self._client, "base_url", None)
        validate_responses_endpoint(
            self._PROVIDER_KEY, str(actual_base_url) if actual_base_url else self._base_url, self._model
        )
        self._responses_identity = {
            "profile": self._PROVIDER_KEY,
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
                raise ValueError("The selected reasoning effort is not supported by this Responses model.")
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
            raise ValueError("Responses max_output_tokens must be a positive integer.")
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
            kwargs["tools"] = encode_tools(tools, strict=self._PROVIDER_KEY == "openai")
        effort = self._responses_effort()
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}
        if self._PROVIDER_KEY == "openai":
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
            raise ValueError("DashScope Responses max_output_tokens must be at least 16.")
        return result
