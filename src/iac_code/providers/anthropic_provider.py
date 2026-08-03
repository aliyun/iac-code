"""Anthropic provider — streams and completes via the Anthropic SDK."""

from __future__ import annotations

import hashlib
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

_MIN_MANUAL_THINKING_BUDGET = 1024


def _anthropic_usage(raw_usage: Any) -> Usage:
    """Preserve Anthropic's separate input and cache counters for normalized reporting."""
    cache_creation = getattr(raw_usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(raw_usage, "cache_read_input_tokens", 0) or 0
    return Usage(
        input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
        output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        input_tokens_include_cache=False,
        reported=True,
    )


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
        max_completion_tokens: int | None = None,
        provider_key: str = "anthropic",
        **kwargs: Any,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        # 用户配置的「最大输出 tokens」硬上限(留空为 None,沿用调用方默认)。
        self._max_output_tokens = positive_int_or_none(max_completion_tokens)
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
        actual_base_url = getattr(self._client, "base_url", None) or base_url
        self._metadata_endpoint_id = self._endpoint_id(actual_base_url)
        self._PROVIDER_KEY = provider_key

    # -- public interface ------------------------------------------------------

    _PROVIDER_KEY = "anthropic"

    def get_model_name(self) -> str:
        return self._model

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        explicit_budget = getattr(self, "_thinking_budget", None)
        if (
            spec.supports_thinking_budget
            and not self._thinking_disabled()
            and isinstance(explicit_budget, int)
            and explicit_budget > 0
        ):
            self._validate_manual_thinking_budget(explicit_budget)
            kwargs = (
                self._build_adaptive_thinking_kwargs(spec) if spec.family is ThinkingFamily.ANTHROPIC_ADAPTIVE else {}
            )
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": explicit_budget}
            return kwargs
        if spec.family is ThinkingFamily.ANTHROPIC_ADAPTIVE:
            return self._build_adaptive_thinking_kwargs(spec)
        budget = self._effective_thinking_budget()
        if budget is None:
            return {}
        self._validate_manual_thinking_budget(budget)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    @staticmethod
    def _validate_manual_thinking_budget(budget: int) -> None:
        if budget < _MIN_MANUAL_THINKING_BUDGET:
            raise ValueError("Anthropic thinking_budget must be at least 1024 tokens")

    def _build_adaptive_thinking_kwargs(self, spec: Any) -> dict[str, Any]:
        effort = normalize_effort(self._effort)
        if effort is None or effort == "auto":
            effort = (
                spec.default_effort.value
                if self._thinking_forced() and not self._thinking_disabled() and spec.default_effort is not None
                else None
            )

        kwargs: dict[str, Any] = {}
        if self._thinking_disabled():
            forbidden_disable_efforts = {item.value for item in spec.disable_forbidden_efforts}
            if effort in forbidden_disable_efforts:
                raise ValueError(
                    f"Anthropic thinking cannot be disabled for {self._model} at {effort} effort; use high or lower"
                )
            if spec.supports_disable:
                kwargs["thinking"] = {"type": "disabled"}
        elif not spec.adaptive_always_on and (effort is not None or self._thinking_forced()):
            kwargs["thinking"] = {"type": "adaptive"}
        if effort is None:
            return kwargs

        allowed = {e.value for e in spec.allowed_efforts}
        if effort not in allowed:
            if spec.default_effort is None:
                return kwargs
            effort = spec.default_effort.value
        kwargs["output_config"] = {"effort": effort}
        return kwargs

    def _effective_thinking_budget(self) -> int | None:
        if self._thinking_disabled():
            return None
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        explicit_budget = getattr(self, "_thinking_budget", None)
        if spec.supports_thinking_budget and isinstance(explicit_budget, int) and explicit_budget > 0:
            return explicit_budget
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
        # 用户配置的输出上限覆盖调用方默认;仍保证 max_tokens 高于思考预算,预留正文空间。
        base = self._max_output_tokens or max_tokens
        budget = self._effective_thinking_budget()
        if budget is None:
            return base
        min_max = budget + 4096
        return max(base, min_max)

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
                    block_index = int(getattr(event_data, "index", 0) or 0)
                    if block.type == "text":
                        pass  # text deltas will follow
                    elif block.type == "tool_use":
                        current_tool_use_id = block.id
                        current_tool_name = block.name
                        current_tool_input_json = ""
                        yield ToolUseStartEvent(tool_use_id=block.id, name=block.name)
                    elif block.type == "thinking":
                        initial_thinking = getattr(block, "thinking", "") or ""
                        signature = getattr(block, "signature", "") or ""
                        if initial_thinking or signature:
                            metadata = self._provider_metadata()
                            if signature:
                                metadata["signature"] = signature
                            yield ThinkingDeltaEvent(
                                text=initial_thinking,
                                block_index=block_index,
                                provider_metadata=metadata,
                            )
                    elif block.type == "redacted_thinking":
                        data = getattr(block, "data", "") or ""
                        yield ThinkingDeltaEvent(
                            text="",
                            block_index=block_index,
                            block_type="redacted_thinking",
                            provider_metadata=self._provider_metadata(data=data),
                        )

                elif event.type == "content_block_delta":
                    event_data: Any = event
                    delta: Any = event_data.delta
                    block_index = int(getattr(event_data, "index", 0) or 0)
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
                        yield ThinkingDeltaEvent(text=delta.thinking, block_index=block_index)
                    elif delta.type == "signature_delta":
                        yield ThinkingDeltaEvent(
                            text="",
                            block_index=block_index,
                            provider_metadata=self._provider_metadata(signature=delta.signature),
                        )

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
            usage = _anthropic_usage(final.usage)
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
        thinking_parts: list[str] = []
        thinking_blocks: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append({"id": block.id, "name": block.name, "input": block.input})
            elif block.type == "thinking":
                thinking = block.thinking or ""
                thinking_parts.append(thinking)
                metadata = self._provider_metadata()
                signature = getattr(block, "signature", "") or ""
                if signature:
                    metadata["signature"] = signature
                thinking_blocks.append({"type": "thinking", "text": thinking, "provider_metadata": metadata})
            elif block.type == "redacted_thinking":
                data = block.data or ""
                thinking_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "provider_metadata": self._provider_metadata(data=data),
                    }
                )

        usage = _anthropic_usage(response.usage)

        return NonStreamingResponse(
            message_id=response.id,
            text="".join(text_parts),
            tool_uses=tool_uses,
            stop_reason=response.stop_reason,
            usage=usage,
            thinking="".join(thinking_parts),
            thinking_blocks=thinking_blocks,
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

        model_id, extra_betas = self._wire_model()
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
            content = self._convert_message_content(msg.content)
            if isinstance(content, list) and not content:
                continue
            converted = {"role": msg.role, "content": content}
            if result and result[-1]["role"] == converted["role"]:
                result[-1]["content"] = self._merge_message_content(result[-1]["content"], converted["content"])
            else:
                result.append(converted)
        return result

    def _convert_message_content(self, content: Any) -> Any:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            converted = [self._convert_content_block(block) for block in content]
            return [block for block in converted if block is not None]
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

    def _convert_content_block(self, block: ContentBlock) -> dict[str, Any] | None:
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
            metadata = block.provider_metadata
            signature = metadata.get("signature")
            if not self._is_current_provider_metadata(metadata) or not isinstance(signature, str) or not signature:
                return None
            return {"type": "thinking", "thinking": block.text or "", "signature": signature}
        elif block.type == "redacted_thinking":
            if not self._is_current_provider_metadata(block.provider_metadata):
                return None
            return {"type": "redacted_thinking", "data": block.data or ""}
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

    def _provider_metadata(self, **values: Any) -> dict[str, Any]:
        model_id, _ = self._wire_model()
        metadata = {"provider": self._PROVIDER_KEY, "model": model_id, **values}
        if self._metadata_endpoint_id is not None:
            metadata["endpoint"] = self._metadata_endpoint_id
        return metadata

    def _is_current_provider_metadata(self, metadata: dict[str, Any]) -> bool:
        if metadata.get("provider") != self._PROVIDER_KEY:
            return False
        model_id, _ = self._wire_model()
        if metadata.get("model") != model_id:
            return False
        source_endpoint = metadata.get("endpoint")
        if source_endpoint is None and self._metadata_endpoint_id is None:
            return True
        return source_endpoint == self._metadata_endpoint_id

    def _wire_model(self) -> tuple[str, tuple[str, ...]]:
        return _MODEL_ALIAS.get(self._model, (self._model, ()))

    @staticmethod
    def _endpoint_id(base_url: Any) -> str | None:
        if base_url is None:
            return None
        normalized = str(base_url).strip().rstrip("/")
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

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
