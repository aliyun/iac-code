"""Core message types for the agent system."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    """A text content block."""

    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """A tool use request block from the assistant."""

    type: Literal["tool_use"] = "tool_use"
    id: str = Field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:24]}")
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """A tool result block sent back to the assistant."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class ThinkingBlock(BaseModel):
    """An assistant reasoning/thinking block.

    Used to round-trip reasoning content for models that require it
    (DeepSeek V4 thinking mode, Qwen thinking mode, Anthropic extended thinking).
    """

    type: Literal["thinking"] = "thinking"
    thinking: str


# Union type for all content blocks
ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock


class Message(BaseModel):
    """A single message in the conversation."""

    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]
    token_count: int = 0
    elapsed_seconds: float = 0.0

    def get_text(self) -> str:
        """Extract text content from the message."""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(block.text for block in self.content if isinstance(block, TextBlock))

    def get_tool_use_blocks(self) -> list[ToolUseBlock]:
        """Extract tool use blocks from the message."""
        if isinstance(self.content, str):
            return []
        return [block for block in self.content if isinstance(block, ToolUseBlock)]

    def has_tool_use(self) -> bool:
        """Check if this message contains tool use blocks."""
        return len(self.get_tool_use_blocks()) > 0

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict for JSONL persistence."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Deserialize from a dict."""
        return cls.model_validate(data)

    def to_api_format(self) -> dict:
        """Convert to API-compatible format for litellm."""
        if isinstance(self.content, str):
            return {"role": self.role, "content": self.content}

        content_list = []
        for block in self.content:
            if isinstance(block, TextBlock):
                content_list.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                content_list.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
            elif isinstance(block, ToolResultBlock):
                content_list.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
            elif isinstance(block, ThinkingBlock):
                content_list.append({"type": "thinking", "thinking": block.thinking})
        return {"role": self.role, "content": content_list}


class Conversation(BaseModel):
    """Manages the conversation message history."""

    messages: list[Message] = Field(default_factory=list)

    def add_user_message(self, content: str) -> Message:
        """Add a user message to the conversation."""
        msg = Message(role="user", content=content)
        self.messages.append(msg)
        return msg

    def add_assistant_message(self, content: str | list[ContentBlock]) -> Message:
        """Add an assistant message to the conversation."""
        msg = Message(role="assistant", content=content)
        self.messages.append(msg)
        return msg

    def add_tool_results(self, tool_results: list[ToolResultBlock]) -> Message:
        """Add tool results as a user message."""
        msg = Message(role="user", content=list(tool_results))
        self.messages.append(msg)
        return msg

    def to_api_format(self) -> list[dict]:
        """Convert the entire conversation to API format."""
        return [msg.to_api_format() for msg in self.messages]

    def to_api_messages(self) -> list[dict]:
        """Alias for to_api_format() for clarity in context management."""
        return self.to_api_format()

    def get_total_tokens(self) -> int:
        """Sum token counts across all messages."""
        return sum(msg.token_count for msg in self.messages)

    def replace_messages(self, messages: list[Message]) -> None:
        """Replace all messages (used after compaction)."""
        self.messages = messages
