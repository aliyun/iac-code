"""Abstract Provider interface — unified across Anthropic, OpenAI, DashScope."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from iac_code.types.stream_events import StreamEvent, Usage


@dataclass
class ToolDefinition:
    """Tool schema passed to the model."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ContentBlock:
    """A block of content within a message."""

    type: str  # "text", "tool_use", "tool_result", "thinking"
    text: str | None = None
    tool_use_id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    content: str | None = None
    is_error: bool = False


@dataclass
class Message:
    """Unified message format for all providers."""

    role: str  # "user", "assistant"
    content: str | list[ContentBlock] = ""

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=text)

    @classmethod
    def assistant_text(cls, text: str) -> Message:
        return cls(role="assistant", content=[ContentBlock(type="text", text=text)])

    @classmethod
    def assistant_tool_use(cls, *, tool_use_id: str, name: str, input: dict[str, Any]) -> Message:
        return cls(
            role="assistant",
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id=tool_use_id,
                    name=name,
                    input=input,
                )
            ],
        )

    @classmethod
    def tool_result(cls, *, tool_use_id: str, content: str, is_error: bool = False) -> Message:
        return cls(
            role="user",
            content=[
                ContentBlock(
                    type="tool_result",
                    tool_use_id=tool_use_id,
                    content=content,
                    is_error=is_error,
                )
            ],
        )


@dataclass
class NonStreamingResponse:
    """Complete response from a non-streaming API call."""

    message_id: str
    text: str
    tool_uses: list[dict[str, Any]]
    stop_reason: str
    usage: Usage
    thinking: str = ""


class Provider(ABC):
    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ) -> AsyncGenerator[StreamEvent, None]: ...

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ) -> NonStreamingResponse: ...

    @abstractmethod
    def get_model_name(self) -> str: ...

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        """Wire-level thinking kwargs to merge into the request payload.

        Default: emit nothing. Subclasses override to translate
        ``self._effort`` + the model's ``ThinkingSpec`` into provider-specific
        request fields (e.g. ``reasoning_effort``, ``extra_body.thinking``).
        """
        return {}

    def _adjust_max_tokens(self, max_tokens: int) -> int:
        """Provider-specific ``max_tokens`` adjustment.

        Anthropic raises this to leave room for the configured thinking
        budget; other providers leave it unchanged.
        """
        return max_tokens
