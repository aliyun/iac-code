"""Anthropic provider — streams and completes via the Anthropic SDK."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import anthropic

from iac_code.providers.base import (
    ContentBlock,
    Message,
    NonStreamingResponse,
    Provider,
    ToolDefinition,
)
from iac_code.providers.request_logging import log_provider_request_policy
from iac_code.providers.request_policy import bool_or_none, positive_int_or_none
from iac_code.providers.thinking import (
    ANTHROPIC_BUDGET,
    ThinkingFamily,
    get_thinking_spec,
    normalize_effort,
)
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.utils.tool_input_parser import parse_tool_input_events

# Model aliases for variants that share a real model ID but require beta flags.
# Value format: (real_model_id, extra_beta_features)
_MODEL_ALIAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "claude-sonnet-4-6-1m": ("claude-sonnet-4-6", ("context-1m-2025-08-07",)),
}


class AnthropicProvider(Provider):
    """Provider implementation backed by ``anthropic.AsyncAnthropic``."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8192,
        client: Any = None,
        effort: str | None = None,
        thinking_enabled: bool | None = None,
        thinking_budget: int | None = None,
        provider_key: str = "anthropic",
        **kwargs: Any,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._thinking_enabled = bool_or_none(thinking_enabled)
        normalized_thinking_budget = positive_int_or_none(thinking_budget)
        if normalized_thinking_budget is not None:
            self._thinking_budget = normalized_thinking_budget
        if client is not None:
            self._client = client
        else:
            client_kwargs: dict[str, Any] = {}
            if api_key is not None:
                client_kwargs["api_key"] = api_key
            if base_url is not None:
                client_kwargs["base_url"] = base_url
            client_kwargs.update(kwargs)
            self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._PROVIDER_KEY = provider_key

    # -- public interface ------------------------------------------------------

    _PROVIDER_KEY = "anthropic"

    def get_model_name(self) -> str:
        return self._model

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        budget = self._effective_thinking_budget()
        if budget is None:
            return {}
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    def _effective_thinking_budget(self) -> int | None:
        if self._thinking_disabled():
            return None
        explicit_budget = getattr(self, "_thinking_budget", None)
        if isinstance(explicit_budget, int) and explicit_budget > 0:
            return explicit_budget
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.ANTHROPIC:
            return None
        effort = normalize_effort(self._effort)
        if effort is None or effort == "auto":
            effort = spec.default_effort.value if self._thinking_forced() and spec.default_effort is not None else None
        if effort is None:
            return None
        budget = ANTHROPIC_BUDGET.get(effort)
        if budget is None:
            return None
        return budget

    def _adjust_max_tokens(self, max_tokens: int) -> int:
        budget = self._effective_thinking_budget()
        if budget is None:
            return max_tokens
        min_max = budget + 4096
        return max(max_tokens, min_max)

    def _thinking_disabled(self) -> bool:
        return bool_or_none(getattr(self, "_thinking_enabled", None)) is False

    def _thinking_forced(self) -> bool:
        return bool_or_none(getattr(self, "_thinking_enabled", None)) is True

    async def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ) -> AsyncGenerator[StreamEvent, None]:
        kwargs = self._build_kwargs(messages, system, tools, max_tokens)

        log_provider_request_policy(
            self._PROVIDER_KEY,
            self._model,
            "messages.stream",
            kwargs,
        )
        async with self._client.messages.stream(**kwargs) as stream:
            # Track current content block state
            current_tool_use_id: str | None = None
            current_tool_name: str = ""
            current_tool_input_json: str = ""

            async for event in stream:
                if event.type == "message_start":
                    event_data: Any = event
                    yield MessageStartEvent(message_id=event_data.message.id)

                elif event.type == "content_block_start":
                    event_data: Any = event
                    block: Any = event_data.content_block
                    if block.type == "text":
                        pass  # text deltas will follow
                    elif block.type == "tool_use":
                        current_tool_use_id = block.id
                        current_tool_name = block.name
                        current_tool_input_json = ""
                        yield ToolUseStartEvent(tool_use_id=block.id, name=block.name)
                    elif block.type == "thinking":
                        pass  # thinking deltas will follow

                elif event.type == "content_block_delta":
                    event_data: Any = event
                    delta: Any = event_data.delta
                    if delta.type == "text_delta":
                        yield TextDeltaEvent(text=delta.text)
                    elif delta.type == "input_json_delta":
                        current_tool_input_json += delta.partial_json
                        if current_tool_use_id is not None:
                            yield ToolInputDeltaEvent(
                                tool_use_id=current_tool_use_id,
                                partial_json=delta.partial_json,
                            )
                    elif delta.type == "thinking_delta":
                        yield ThinkingDeltaEvent(text=delta.thinking)

                elif event.type == "content_block_stop":
                    if current_tool_use_id is not None:
                        events = list(
                            parse_tool_input_events(
                                current_tool_use_id,
                                current_tool_name,
                                current_tool_input_json,
                            )
                        )
                        for ev in events:
                            yield ev
                        current_tool_use_id = None
                        current_tool_name = ""
                        current_tool_input_json = ""

            # After the stream ends, emit the final message event
            final = await stream.get_final_message()
            usage = Usage(
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
                cache_creation_input_tokens=getattr(final.usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(final.usage, "cache_read_input_tokens", 0) or 0,
            )
            yield MessageEndEvent(stop_reason=final.stop_reason or "end_turn", usage=usage)

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        cache_policy: str = "default",
    ) -> NonStreamingResponse:
        kwargs = self._build_kwargs(messages, system, tools, max_tokens)
        log_provider_request_policy(
            self._PROVIDER_KEY,
            self._model,
            "messages.create",
            kwargs,
        )
        response = await self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append({"id": block.id, "name": block.name, "input": block.input})

        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

        return NonStreamingResponse(
            message_id=response.id,
            text="".join(text_parts),
            tool_uses=tool_uses,
            stop_reason=response.stop_reason,
            usage=usage,
        )

    # -- conversion helpers ----------------------------------------------------

    def _build_kwargs(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        thinking_kwargs = self._build_thinking_kwargs()
        effective_max_tokens = self._adjust_max_tokens(max_tokens)

        model_id, extra_betas = _MODEL_ALIAS.get(self._model, (self._model, ()))
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": effective_max_tokens,
            "system": system,
            "messages": self._convert_messages(messages),
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        kwargs.update(thinking_kwargs)
        if extra_betas:
            kwargs["extra_headers"] = {"anthropic-beta": ",".join(extra_betas)}
        return kwargs

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal ``Message`` list to Anthropic API format."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            converted = {"role": msg.role, "content": self._convert_message_content(msg.content)}
            if result and result[-1]["role"] == converted["role"]:
                result[-1]["content"] = self._merge_message_content(result[-1]["content"], converted["content"])
            else:
                result.append(converted)
        return result

    def _convert_message_content(self, content: Any) -> Any:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return [self._convert_content_block(block) for block in content]
        return content

    @classmethod
    def _merge_message_content(cls, left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            if left and right:
                return f"{left}\n\n{right}"
            return left or right
        return [*cls._content_to_blocks(left), *cls._content_to_blocks(right)]

    @staticmethod
    def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        return [{"type": "text", "text": str(content)}]

    @staticmethod
    def _convert_content_block(block: ContentBlock) -> dict[str, Any]:
        """Convert a single ``ContentBlock`` to Anthropic dict."""
        if block.type == "text":
            return {"type": "text", "text": block.text or ""}
        elif block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": block.tool_use_id or "",
                "name": block.name or "",
                "input": block.input or {},
            }
        elif block.type == "tool_result":
            d: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id or "",
                "content": block.content or "",
            }
            if block.is_error:
                d["is_error"] = True
            return d
        elif block.type == "thinking":
            return {"type": "thinking", "thinking": block.text or ""}
        elif block.type == "image":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type or "image/png",
                    "data": block.data or "",
                },
            }
        else:
            return {"type": block.type}

    @staticmethod
    def _convert_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert ``ToolDefinition`` list to Anthropic API format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]
