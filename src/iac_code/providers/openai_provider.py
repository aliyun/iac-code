"""OpenAI Provider implementation with streaming and tool call support."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
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
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec, normalize_effort
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.utils.tool_input_parser import parse_tool_input_events


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
        # Subclasses may set this before calling super().stream/complete to
        # inject provider-specific kwargs (e.g. DeepSeek thinking mode).
        self._extra_request_kwargs: dict[str, Any] = {}
        if client is not None:
            self._client = client
        else:
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._PROVIDER_KEY = provider_key

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        compatible_kwargs = self._build_openai_compatible_thinking_kwargs()
        if compatible_kwargs:
            return compatible_kwargs

        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.OPENAI:
            return {}
        if self._thinking_disabled():
            return {}
        effort = self._effective_effort(spec)
        if effort is None:
            return {}
        allowed = {e.value for e in spec.allowed_efforts}
        if effort not in allowed:
            if spec.default_effort is None:
                return {}
            effort = spec.default_effort.value
        return {
            "reasoning_effort": effort,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

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
        if self._thinking_disabled():
            return {"max_tokens": max_tokens}
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if not spec.use_max_completion_tokens:
            return {"max_tokens": max_tokens}
        if self._max_completion_tokens is not None:
            return {"max_completion_tokens": self._max_completion_tokens}
        thinking_budget = self._effective_thinking_budget()
        if thinking_budget is None:
            return {"max_tokens": max_tokens}
        return {"max_completion_tokens": max_tokens + thinking_budget}

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
                msg["reasoning_content"] = "".join(b.text or "" for b in thinking_blocks)
            if tool_uses:
                msg["tool_calls"] = [
                    {
                        "id": b.tool_use_id or "",
                        "type": "function",
                        "function": {
                            "name": b.name or "",
                            "arguments": json.dumps(b.input or {}),
                        },
                    }
                    for b in tool_uses
                ]
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
        api_messages = self._build_api_messages(messages, system)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "stream": True,
        }
        kwargs.update(self._token_limit_kwargs(max_tokens))
        if self.supports_stream_options:
            kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        for k, v in self._effort_request_kwargs().items():
            kwargs[k] = v
        for k, v in self._extra_request_kwargs.items():
            kwargs[k] = v

        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        yield MessageStartEvent(message_id=message_id)

        # Accumulators for tool calls (index-based)
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        usage = Usage()
        has_content = False

        log_provider_request_policy(
            self._PROVIDER_KEY,
            self._model,
            "chat.completions.stream",
            kwargs,
        )
        response = await self._client.chat.completions.create(**kwargs)
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
                )

            if not chunk.choices:
                continue

            choice = chunk.choices[0]

            # Finish reason
            if choice.finish_reason:
                if choice.finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                elif choice.finish_reason == "length":
                    stop_reason = "max_tokens"
                else:
                    stop_reason = "end_turn"

            delta = choice.delta
            if delta is None:
                continue

            # Reasoning content (DeepSeek V4, Qwen thinking mode via OpenAI-compat)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield ThinkingDeltaEvent(text=reasoning)

            # Text content
            if delta.content:
                yield TextDeltaEvent(text=delta.content)

            # Tool calls (streamed with index-based accumulation)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                        if tc_delta.function and tc_delta.function.name:
                            tool_calls_acc[idx]["name"] = tc_delta.function.name
                            yield ToolUseStartEvent(
                                tool_use_id=tool_calls_acc[idx]["id"],
                                name=tc_delta.function.name,
                            )
                    if tc_delta.function and tc_delta.function.arguments:
                        tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments
                        yield ToolInputDeltaEvent(
                            tool_use_id=tool_calls_acc[idx]["id"],
                            partial_json=tc_delta.function.arguments,
                        )

        if not has_content:
            base_url = str(self._base_url or self._client.base_url).rstrip("/")
            raise RuntimeError(
                _(
                    "API returned no data. Please check that your API Base URL is correct (current: {base_url}). "
                    "Many OpenAI-compatible endpoints require a /v1 suffix (e.g. {base_url}/v1)."
                ).format(base_url=base_url)
            )

        # Emit ToolUseEndEvent for each accumulated tool call
        for idx in sorted(tool_calls_acc):
            tc = tool_calls_acc[idx]
            for ev in parse_tool_input_events(tc["id"], tc["name"], tc["arguments"]):
                yield ev

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
        api_messages = self._build_api_messages(messages, system, cache_policy=cache_policy)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
        }
        kwargs.update(self._token_limit_kwargs(max_tokens))
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        for k, v in self._effort_request_kwargs().items():
            kwargs[k] = v
        for k, v in self._extra_request_kwargs.items():
            kwargs[k] = v

        log_provider_request_policy(
            self._PROVIDER_KEY,
            self._model,
            "chat.completions.create",
            kwargs,
        )
        response = await self._client.chat.completions.create(**kwargs)
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
        thinking = getattr(message, "reasoning_content", None) or ""
        tool_uses: list[dict[str, Any]] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                raw_args = tc.function.arguments or ""
                for ev in parse_tool_input_events(tc.id, tc.function.name, raw_args):
                    if isinstance(ev, ToolUseEndEvent):
                        tool_uses.append({"id": ev.tool_use_id, "name": tc.function.name, "input": ev.input})

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
        )

        return NonStreamingResponse(
            message_id=response.id,
            text=text,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            usage=usage,
            thinking=thinking,
        )
