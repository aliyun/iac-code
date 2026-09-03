"""OpenAI Provider implementation with streaming and tool call support."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from iac_code.i18n import _
from iac_code.providers.base import (
    ContentBlock,
    Message,
    NonStreamingResponse,
    Provider,
    ToolDefinition,
)
from iac_code.providers.request_logging import log_provider_request_policy
from iac_code.providers.request_policy import bool_or_none, positive_int_or_none
from iac_code.providers.streaming import OpenAIStreamResponseAdapter
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec, normalize_effort
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    ToolUseEndEvent,
    Usage,
)
from iac_code.utils.tool_input_parser import parse_tool_input_events


def _plain_mapping(value: Any) -> dict[str, Any]:
    """Convert SDK response objects into JSON-compatible provider metadata."""
    if isinstance(value, dict):
        return dict(value)
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            dumped = method(exclude_none=True)
            if isinstance(dumped, dict):
                return dumped
    return {}


@dataclass
class ChatRequestContext:
    streaming: bool
    cache_policy: str = "default"
    mandatory_thinking_retry: bool = False
    retry_attempted: bool = False
    mandatory_thinking: bool | None = None


class OpenAIProvider(Provider):
    """Provider implementation for OpenAI API (GPT-4, etc.)."""

    _PROVIDER_KEY = "openai"

    # Subclasses can set this to True for endpoints known to support stream_options
    supports_stream_options: bool = False

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any = None,
        effort: str | None = None,
        thinking_enabled: bool | None = None,
        thinking_budget: int | None = None,
        max_completion_tokens: int | None = None,
        provider_key: str = "openai",
        **kwargs,
    ):
        self._model = model
        self._base_url = base_url
        self._effort = effort
        self._thinking_enabled = bool_or_none(thinking_enabled)
        self._thinking_budget = positive_int_or_none(thinking_budget)
        self._max_completion_tokens = positive_int_or_none(max_completion_tokens)
        if provider_key == "openai":
            self.supports_stream_options = True
        # Subclasses may set this before calling super().stream/complete to
        # inject provider-specific kwargs (e.g. DeepSeek thinking mode).
        self._extra_request_kwargs: dict[str, Any] = {}
        if client is not None:
            self._client = client
        else:
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        actual_base_url = getattr(self._client, "base_url", None) or base_url
        self._metadata_endpoint_id = self._endpoint_id(actual_base_url)
        self._PROVIDER_KEY = provider_key

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        compatible_kwargs = self._build_openai_compatible_thinking_kwargs()
        if compatible_kwargs:
            return compatible_kwargs

        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.OPENAI:
            return {}
        allowed = {e.value for e in spec.allowed_efforts}
        if self._thinking_disabled():
            return {"reasoning_effort": "none"} if "none" in allowed else {}
        effort = self._effective_effort(spec)
        if effort is None:
            return {}
        if effort not in allowed:
            if spec.default_effort is None:
                return {}
            effort = spec.default_effort.value
        return {"reasoning_effort": effort}

    def _effort_request_kwargs(self) -> dict[str, Any]:
        # Backwards-compatible alias used by the streaming/non-streaming paths.
        return self._build_thinking_kwargs()

    def _build_openai_compatible_thinking_kwargs(self) -> dict[str, Any]:
        if self._PROVIDER_KEY != "openai_compatible":
            return {}
        spec = get_thinking_spec("dashscope", self._model)
        if spec.family is not ThinkingFamily.DASHSCOPE:
            return {}
        if self._thinking_disabled():
            return {"extra_body": {"enable_thinking": False}}
        if (
            not self._thinking_forced()
            and self._thinking_budget is None
            and normalize_effort(self._effort) in {None, "auto"}
        ):
            return {}

        extra_body: dict[str, Any] = {"enable_thinking": True}
        thinking_budget = self._effective_thinking_budget_for_spec(spec)
        if thinking_budget is not None:
            extra_body["thinking_budget"] = thinking_budget
        kwargs: dict[str, Any] = {"extra_body": extra_body}
        if spec.uses_reasoning_effort_param:
            effort = normalize_effort(self._effort)
            allowed = {e.value for e in spec.allowed_efforts}
            if effort in allowed:
                kwargs["reasoning_effort"] = effort
            elif effort not in {None, "auto"} and spec.default_effort is not None:
                kwargs["reasoning_effort"] = spec.default_effort.value
        return kwargs

    def _effective_thinking_budget(self) -> int | None:
        if self._thinking_disabled():
            return None
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        return self._effective_thinking_budget_for_spec(spec)

    def _effective_thinking_budget_for_spec(self, spec: Any) -> int | None:
        if not spec.supports_thinking_budget:
            return None
        return self._thinking_budget if self._thinking_budget is not None else spec.default_thinking_budget

    def _token_limit_kwargs(self, max_tokens: int) -> dict[str, int]:
        # 用户配置的「最大输出 tokens」是硬上限,覆盖调用方传入的默认值;留空时沿用默认。
        configured = self._max_completion_tokens
        if self._thinking_disabled():
            return {"max_tokens": configured or max_tokens}
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if not spec.use_max_completion_tokens:
            return {"max_tokens": configured or max_tokens}
        # use_max_completion_tokens 家族:该参数限制「含推理」的总生成量,思考预算另经
        # extra_body 单独下发。故最终额度 = 可见输出上限 + 思考预算,否则推理会挤占用户要的输出。
        # configured 与 budget 均可覆盖默认,configured 分支同样要叠加预算(与留空分支一致)。
        thinking_budget = self._effective_thinking_budget()
        return {"max_completion_tokens": (configured or max_tokens) + (thinking_budget or 0)}

    def _thinking_disabled(self) -> bool:
        return bool_or_none(getattr(self, "_thinking_enabled", None)) is False

    def _thinking_forced(self) -> bool:
        return bool_or_none(getattr(self, "_thinking_enabled", None)) is True

    def _effective_effort(self, spec: Any) -> str | None:
        effort = normalize_effort(self._effort)
        if effort in {None, "auto"}:
            return spec.default_effort.value if self._thinking_forced() and spec.default_effort is not None else None
        return effort

    def get_model_name(self) -> str:
        return self._model

    def prepare_system_prompt(self, system: str, tools: list[ToolDefinition] | None) -> str:
        """Prepare the request-local system prompt without mutating provider state."""
        return system

    def _extract_reasoning_text(self, message_or_delta: Any) -> str:
        reasoning = getattr(message_or_delta, "reasoning_content", None)
        return reasoning if isinstance(reasoning, str) else ""

    def _create_stream_response_adapter(
        self, tools: list[ToolDefinition] | None
    ) -> OpenAIStreamResponseAdapter:
        return OpenAIStreamResponseAdapter(self, tools)

    # -- Message conversion ----------------------------------------------------

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert unified Message objects to OpenAI API format."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg.content, str):
                result.append({"role": msg.role, "content": msg.content})
            elif isinstance(msg.content, list):
                result.extend(self._convert_content_blocks(msg.role, msg.content))
        return result

    def _convert_content_blocks(self, role: str, blocks: list[ContentBlock]) -> list[dict[str, Any]]:
        """Convert a list of ContentBlocks into one or more OpenAI messages."""
        messages: list[dict[str, Any]] = []

        # Group tool_use blocks into a single assistant message with tool_calls
        tool_uses = [b for b in blocks if b.type == "tool_use"]
        text_blocks = [b for b in blocks if b.type == "text"]
        thinking_blocks = [b for b in blocks if b.type == "thinking"]
        tool_results = [b for b in blocks if b.type == "tool_result"]

        # Assistant message with text and/or tool_calls
        if role == "assistant" and (text_blocks or tool_uses or thinking_blocks):
            msg: dict[str, Any] = {"role": "assistant"}
            if text_blocks:
                msg["content"] = "".join(b.text or "" for b in text_blocks)
            else:
                msg["content"] = None
            if thinking_blocks:
                # DeepSeek / Qwen thinking-mode models require the prior-turn
                # reasoning_content to be echoed back in assistant messages.
                reasoning_content = "".join(b.text or "" for b in thinking_blocks)
                if reasoning_content:
                    msg["reasoning_content"] = reasoning_content
                if self._PROVIDER_KEY == "gemini":
                    for block in thinking_blocks:
                        metadata = block.provider_metadata
                        if not self._is_current_gemini_metadata(metadata):
                            continue
                        extra_content = metadata.get("extra_content")
                        if isinstance(extra_content, dict) and extra_content:
                            msg["extra_content"] = extra_content
            if tool_uses:
                tool_calls: list[dict[str, Any]] = []
                for block in tool_uses:
                    tool_call: dict[str, Any] = {
                        "id": block.tool_use_id or "",
                        "type": "function",
                        "function": {
                            "name": block.name or "",
                            "arguments": json.dumps(block.input or {}),
                        },
                    }
                    metadata = block.provider_metadata
                    if self._PROVIDER_KEY == "gemini" and self._is_current_gemini_metadata(metadata):
                        extra_content = metadata.get("extra_content")
                        if isinstance(extra_content, dict) and extra_content:
                            tool_call["extra_content"] = extra_content
                    tool_calls.append(tool_call)
                msg["tool_calls"] = tool_calls
            messages.append(msg)

        # User message with text and/or image blocks. tool_result blocks are
        # handled by the role="tool" branch below; if the user message contains
        # only tool_result blocks, user_parts stays empty and nothing is emitted.
        if role == "user":
            user_parts: list[dict[str, Any]] = []
            for b in blocks:
                if b.type == "text":
                    user_parts.append({"type": "text", "text": b.text or ""})
                elif b.type == "image":
                    user_parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{b.media_type or 'image/png'};base64,{b.data or ''}"},
                        }
                    )
            if user_parts:
                messages.append({"role": "user", "content": user_parts})

        # Tool result messages (role="tool")
        for b in tool_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": b.tool_use_id or "",
                    "content": b.content or "",
                }
            )

        return messages

    # -- Tool conversion -------------------------------------------------------

    def _convert_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert unified ToolDefinition objects to OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    def _prepare_tool_schema_for_wire(self, schema: dict[str, Any]) -> dict[str, Any]:
        return schema

    def _build_api_tools(
        self,
        tools: list[ToolDefinition],
        *,
        streaming: bool,
        cache_policy: str = "default",
    ) -> list[dict[str, Any]]:
        prepared = [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                input_schema=self._prepare_tool_schema_for_wire(tool.input_schema),
            )
            for tool in tools
        ]
        return self._convert_tools(prepared)

    def _request_headers(self, *, cache_policy: str = "default") -> dict[str, str]:
        return {}

    def _thinking_kwargs_for_context(self, context: ChatRequestContext) -> dict[str, Any]:
        return self._effort_request_kwargs()

    def _create_chat_request_context(
        self,
        *,
        streaming: bool,
        cache_policy: str = "default",
    ) -> ChatRequestContext:
        return ChatRequestContext(streaming=streaming, cache_policy=cache_policy)

    def _build_chat_completion_kwargs(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        context: ChatRequestContext,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._build_api_messages(messages, system, cache_policy=context.cache_policy),
        }
        if context.streaming:
            kwargs["stream"] = True
        kwargs.update(self._token_limit_kwargs(max_tokens))
        if context.streaming and self.supports_stream_options:
            kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = self._build_api_tools(
                tools,
                streaming=context.streaming,
                cache_policy=context.cache_policy,
            )
        headers = self._request_headers(cache_policy=context.cache_policy)
        if headers:
            kwargs["extra_headers"] = headers
        kwargs.update(self._thinking_kwargs_for_context(context))
        kwargs.update(self._extra_request_kwargs)
        return kwargs

    async def _create_chat_completion(
        self,
        build_kwargs: Any,
        context: ChatRequestContext,
        operation_name: str,
    ) -> Any:
        kwargs = build_kwargs()
        log_provider_request_policy(self._PROVIDER_KEY, self._model, operation_name, kwargs)
        request_started = time.monotonic()
        try:
            return await self._client.chat.completions.create(**kwargs)
        except Exception as error:
            if context.retry_attempted or not self._retry_request_context_after_error(error, kwargs, context):
                raise
            context.retry_attempted = True
            self._record_request_compatibility_retry(
                error,
                context,
                operation_name=operation_name,
                duration_ms=int((time.monotonic() - request_started) * 1000),
            )
            retry_kwargs = build_kwargs()
            log_provider_request_policy(self._PROVIDER_KEY, self._model, operation_name, retry_kwargs)
            return await self._client.chat.completions.create(**retry_kwargs)

    def _record_request_compatibility_retry(
        self,
        error: Exception,
        context: ChatRequestContext,
        *,
        operation_name: str,
        duration_ms: int,
    ) -> None:
        """Let provider adapters observe a consumed compatibility attempt."""

    def _retry_request_context_after_error(
        self,
        error: Exception,
        sent_kwargs: dict[str, Any],
        context: ChatRequestContext,
    ) -> bool:
        return False

    # -- API message assembly ---------------------------------------------------

    def _build_api_messages(
        self,
        messages: list[Message],
        system: str,
        cache_policy: str = "default",
    ) -> list[dict[str, Any]]:
        """Build the ``messages`` list sent to the OpenAI Chat API.

        Subclasses may override this to alter the system-message format
        (e.g. to inject ``cache_control`` markers for DashScope).
        """
        api_messages: list[dict[str, Any]] = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend(self._convert_messages(messages))
        return api_messages

    # -- Streaming -------------------------------------------------------------

    async def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ) -> AsyncGenerator[StreamEvent, None]:
        context = self._create_chat_request_context(streaming=True)

        def build_kwargs() -> dict[str, Any]:
            return self._build_chat_completion_kwargs(messages, system, tools, max_tokens, context)

        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        yield MessageStartEvent(message_id=message_id)

        adapter = self._create_stream_response_adapter(tools)
        stop_reason = "end_turn"
        raw_finish_reason: str | None = None
        usage = Usage()
        has_content = False

        response = await self._create_chat_completion(build_kwargs, context, "chat.completions.stream")
        async for chunk in response:
            has_content = True
            # Usage info (final chunk)
            if chunk.usage is not None:
                cache_read = 0
                cache_create = 0
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                if details:
                    cache_read = getattr(details, "cached_tokens", 0) or 0
                    cache_create = getattr(details, "cache_creation_input_tokens", 0) or 0
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                    cache_read_input_tokens=cache_read,
                    cache_creation_input_tokens=cache_create,
                    reported=True,
                )

            if not chunk.choices:
                continue

            choice = chunk.choices[0]

            # Finish reason
            if choice.finish_reason:
                raw_finish_reason = choice.finish_reason
                if choice.finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                elif choice.finish_reason == "length":
                    stop_reason = "max_tokens"
                else:
                    stop_reason = "end_turn"

            delta = choice.delta
            if delta is None:
                continue

            for event in adapter.feed(delta, raw_finish_reason):
                yield event

        if not has_content:
            base_url = str(self._base_url or self._client.base_url).rstrip("/")
            raise RuntimeError(
                _(
                    "API returned no data. Please check that your API Base URL is correct (current: {base_url}). "
                    "Many OpenAI-compatible endpoints require a /v1 suffix (e.g. {base_url}/v1)."
                ).format(base_url=base_url)
            )

        for event in adapter.finalize(raw_finish_reason):
            yield event
        if adapter.terminal_stop_reason_override is not None:
            stop_reason = adapter.terminal_stop_reason_override

        yield MessageEndEvent(stop_reason=stop_reason, usage=usage)

    # -- Non-streaming ---------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        cache_policy: str = "default",
    ) -> NonStreamingResponse:
        context = self._create_chat_request_context(streaming=False, cache_policy=cache_policy)

        def build_kwargs() -> dict[str, Any]:
            return self._build_chat_completion_kwargs(messages, system, tools, max_tokens, context)

        response = await self._create_chat_completion(build_kwargs, context, "chat.completions.create")
        if not hasattr(response, "choices"):
            base_url = str(self._base_url or self._client.base_url).rstrip("/")
            raise RuntimeError(
                _(
                    "API returned an invalid response. Please check that your "
                    "API Base URL is correct (current: {base_url}). "
                    "Many OpenAI-compatible endpoints require a /v1 suffix "
                    "(e.g. {base_url}/v1)."
                ).format(base_url=base_url)
            )
        if not response.choices:
            base_url = str(self._base_url or self._client.base_url).rstrip("/")
            message = _(
                "API returned an invalid response. Please check that your "
                "API Base URL is correct (current: {base_url}). "
                "Many OpenAI-compatible endpoints require a /v1 suffix "
                "(e.g. {base_url}/v1)."
            ).format(base_url=base_url)
            raise RuntimeError(message + " Response choices were empty.")
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        thinking = self._extract_reasoning_text(message)
        message_provider_metadata = self._message_provider_metadata(message)
        thinking_blocks: list[dict[str, Any]] = []
        if message_provider_metadata:
            thinking_blocks.append(
                {
                    "type": "thinking",
                    "text": thinking,
                    "provider_metadata": message_provider_metadata,
                }
            )
        tool_uses = self._parse_non_streaming_tool_calls(message.tool_calls)

        stop_reason = "end_turn"
        if choice.finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "max_tokens"

        cache_read = 0
        cache_create = 0
        if response.usage:
            details = getattr(response.usage, "prompt_tokens_details", None)
            if details:
                cache_read = getattr(details, "cached_tokens", 0) or 0
                cache_create = getattr(details, "cache_creation_input_tokens", 0) or 0
        usage = Usage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
            reported=response.usage is not None,
        )

        unified_response = NonStreamingResponse(
            message_id=response.id,
            text=text,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            usage=usage,
            thinking=thinking,
            thinking_blocks=thinking_blocks,
        )
        return self._postprocess_non_streaming_response(message, unified_response, tools)

    def _parse_non_streaming_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        tool_uses: list[dict[str, Any]] = []
        for tc in tool_calls or []:
            raw_args = tc.function.arguments or ""
            provider_metadata = self._tool_provider_metadata(tc)
            for event in parse_tool_input_events(tc.id, tc.function.name, raw_args):
                if isinstance(event, ToolUseEndEvent):
                    tool_use = {"id": event.tool_use_id, "name": tc.function.name, "input": event.input}
                    if event.input_error:
                        # Preserve malformed-input diagnostics so the agent loop does not
                        # execute an accidental empty object on the non-streaming path.
                        tool_use["input_error"] = event.input_error
                    if event.tool_use_id == tc.id and provider_metadata:
                        tool_use["provider_metadata"] = provider_metadata
                    tool_uses.append(tool_use)
        return tool_uses

    def _postprocess_non_streaming_response(
        self,
        raw_message: Any,
        response: NonStreamingResponse,
        tools: list[ToolDefinition] | None,
    ) -> NonStreamingResponse:
        return response

    def _message_provider_metadata(self, message: Any) -> dict[str, Any]:
        """Extract opaque metadata attached to an assistant message or delta."""
        if self._PROVIDER_KEY != "gemini":
            return {}
        extra_content = getattr(message, "extra_content", None)
        if extra_content is None:
            model_extra = getattr(message, "model_extra", None)
            if isinstance(model_extra, dict):
                extra_content = model_extra.get("extra_content")
        plain_extra_content = _plain_mapping(extra_content)
        if not plain_extra_content:
            return {}
        return self._gemini_provider_metadata(plain_extra_content)

    def _tool_provider_metadata(self, tool_call: Any) -> dict[str, Any]:
        """Extract opaque metadata that a provider requires on the next turn."""
        if self._PROVIDER_KEY != "gemini":
            return {}
        extra_content = getattr(tool_call, "extra_content", None)
        if extra_content is None:
            model_extra = getattr(tool_call, "model_extra", None)
            if isinstance(model_extra, dict):
                extra_content = model_extra.get("extra_content")
        plain_extra_content = _plain_mapping(extra_content)
        if not plain_extra_content:
            return {}
        return self._gemini_provider_metadata(plain_extra_content)

    def _gemini_provider_metadata(self, extra_content: dict[str, Any]) -> dict[str, Any]:
        metadata = {"provider": "gemini", "model": self._model, "extra_content": extra_content}
        if self._metadata_endpoint_id is not None:
            metadata["endpoint"] = self._metadata_endpoint_id
        return metadata

    def _is_current_gemini_metadata(self, metadata: dict[str, Any]) -> bool:
        if metadata.get("provider") != "gemini":
            return False
        if metadata.get("model") != self._model:
            return False
        source_endpoint = metadata.get("endpoint")
        if source_endpoint is None and self._metadata_endpoint_id is None:
            return True
        return source_endpoint == self._metadata_endpoint_id

    @staticmethod
    def _endpoint_id(base_url: Any) -> str | None:
        if base_url is None:
            return None
        normalized = str(base_url).strip().rstrip("/")
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
