"""Context manager for conversation history, token tracking, and segmented compaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from iac_code.agent.message import ContentBlock, Conversation, Message, ToolResultBlock, ToolUseBlock
from iac_code.services.token_counter import TokenCounter


@dataclass
class ContextWindowConfig:
    """Model-specific context window configuration."""

    context_window: int
    max_output_tokens: int
    compact_buffer: int
    compact_threshold: float
    preserve_recent_turns: int


_MODEL_CONFIGS: dict[str, ContextWindowConfig] = {
    "claude": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
    "gpt-5": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
    "gpt-4": ContextWindowConfig(128_000, 8_192, 15_000, 0.93, 3),
    "qwen": ContextWindowConfig(131_072, 8_192, 15_000, 0.93, 3),
    "qwq": ContextWindowConfig(131_072, 8_192, 15_000, 0.93, 3),
    "o3": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
    "o4": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
}
_DEFAULT_CONFIG = ContextWindowConfig(128_000, 8_192, 15_000, 0.93, 3)


def get_context_window_config(model: str) -> ContextWindowConfig:
    model_lower = model.lower()
    for prefix, config in _MODEL_CONFIGS.items():
        if model_lower.startswith(prefix):
            return config
    return _DEFAULT_CONFIG


class ContextManager:
    def __init__(
        self,
        system_prompt: str,
        model: str = "",
    ) -> None:
        self._system_prompt = system_prompt
        self._conversation = Conversation()
        self._model = model
        self._token_counter = TokenCounter(model=model)
        self._config = get_context_window_config(model)
        self._system_prompt_tokens = self._token_counter.count_text(system_prompt)
        self._tool_definitions: list[Any] = []
        self._tool_definition_tokens = 0

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def preserve_recent_turns(self) -> int:
        return self._config.preserve_recent_turns

    @property
    def context_window(self) -> int:
        """Total context-window size in tokens for the current model."""
        return self._config.context_window

    def set_model(self, model: str) -> None:
        """Switch tokenizer/context-window config for a model change.

        Recomputes cached token counts so compaction thresholds stay
        accurate after a `/model` or `/auth` switch.
        """
        if model == self._model:
            return
        self._model = model
        self._token_counter = TokenCounter(model=model)
        self._config = get_context_window_config(model)
        self._system_prompt_tokens = self._token_counter.count_text(self._system_prompt)
        self._tool_definition_tokens = self._token_counter.count_tool_definitions(self._tool_definitions)
        for msg in self._conversation.messages:
            msg.token_count = self._token_counter.count_message(msg.to_api_format())

    def set_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt and refresh its cached token count."""
        if system_prompt == self._system_prompt:
            return
        self._system_prompt = system_prompt
        self._system_prompt_tokens = self._token_counter.count_text(system_prompt)

    def set_tool_definitions(self, tool_definitions: list[Any]) -> None:
        """Cache provider tool definitions and their current token footprint."""
        self._tool_definitions = list(tool_definitions)
        self._tool_definition_tokens = self._token_counter.count_tool_definitions(self._tool_definitions)

    def add_user_message(self, content: str | list[ContentBlock]) -> Message:
        msg = self._conversation.add_user_message(content)
        msg.token_count = self._token_counter.count_message(msg.to_api_format())
        return msg

    def add_assistant_message(self, content: str | list[ContentBlock]) -> Message:
        msg = self._conversation.add_assistant_message(content)
        msg.token_count = self._token_counter.count_message(msg.to_api_format())
        return msg

    def add_tool_results(self, tool_results: list[ToolResultBlock]) -> Message:
        msg = self._conversation.add_tool_results(tool_results)
        msg.token_count = self._token_counter.count_message(msg.to_api_format())
        return msg

    def add_raw_message(self, raw_msg: dict[str, Any]) -> Message:
        """Add a raw message dict (e.g. from ToolResult.new_messages) to the conversation."""
        role = raw_msg.get("role", "user")
        content = raw_msg.get("content", "")
        msg = Message(role=role, content=content)
        self._conversation.messages.append(msg)
        msg.token_count = self._token_counter.count_message(msg.to_api_format())
        return msg

    def load_messages(self, messages: list[Message]) -> None:
        """Inject pre-existing messages (e.g. from a resumed session)."""
        for msg in messages:
            self._conversation.messages.append(msg)
            if msg.token_count == 0:
                msg.token_count = self._token_counter.count_message(msg.to_api_format())

    def get_messages(self) -> list[Message]:
        return self._conversation.messages

    def get_api_messages(self) -> list[dict[str, Any]]:
        return self._conversation.to_api_messages()

    def get_total_tokens(self) -> int:
        return self._system_prompt_tokens + self._tool_definition_tokens + self._conversation.get_total_tokens()

    def get_usage(self) -> dict[str, Any]:
        """Return detailed token usage breakdown by category."""
        user_tokens = 0
        assistant_tokens = 0
        tool_result_tokens = 0

        for msg in self._conversation.messages:
            if msg.role == "user":
                if isinstance(msg.content, list) and any(isinstance(b, ToolResultBlock) for b in msg.content):
                    tool_result_tokens += msg.token_count
                else:
                    user_tokens += msg.token_count
            elif msg.role == "assistant":
                assistant_tokens += msg.token_count

        total = (
            self._system_prompt_tokens
            + self._tool_definition_tokens
            + user_tokens
            + assistant_tokens
            + tool_result_tokens
        )
        return {
            "system_prompt_tokens": self._system_prompt_tokens,
            "tool_definition_tokens": self._tool_definition_tokens,
            "user_message_tokens": user_tokens,
            "assistant_message_tokens": assistant_tokens,
            "tool_result_tokens": tool_result_tokens,
            "total_tokens": total,
            "context_window": self._config.context_window,
            "usage_percent": (total / self._config.context_window * 100) if self._config.context_window > 0 else 0,
            "message_count": len(self._conversation.messages),
        }

    def needs_compaction(self) -> bool:
        total = self.get_total_tokens()
        threshold = self._config.context_window * self._config.compact_threshold
        return total > threshold

    @staticmethod
    def _tool_use_ids(message: Message) -> set[str]:
        if isinstance(message.content, str):
            return set()
        return {block.id for block in message.content if isinstance(block, ToolUseBlock)}

    @staticmethod
    def _tool_result_ids(message: Message) -> set[str]:
        if isinstance(message.content, str):
            return set()
        return {block.tool_use_id for block in message.content if isinstance(block, ToolResultBlock)}

    @classmethod
    def _find_safe_compaction_split(cls, messages: list[Message], split_point: int) -> int:
        split_point = max(0, min(split_point, len(messages)))
        while split_point > 0:
            old_tool_uses: dict[str, int] = {}
            old_tool_results: set[str] = set()

            for index, message in enumerate(messages[:split_point]):
                for tool_use_id in cls._tool_use_ids(message):
                    old_tool_uses.setdefault(tool_use_id, index)
                old_tool_results.update(cls._tool_result_ids(message))

            unpaired_tool_uses = set(old_tool_uses) - old_tool_results
            if not unpaired_tool_uses:
                return split_point

            split_point = min(old_tool_uses[tool_use_id] for tool_use_id in unpaired_tool_uses)

        return split_point

    def _split_messages_for_compaction(self) -> tuple[list[Message], list[Message]]:
        """Split messages into [old_messages, recent_messages].

        A "turn" is a user+assistant message pair. We preserve the last
        `preserve_recent_turns` turns (counting from the end).
        """
        messages = self._conversation.messages
        preserve_count = self._config.preserve_recent_turns * 2

        if len(messages) <= preserve_count:
            return [], messages

        split_point = len(messages) - preserve_count
        split_point = self._find_safe_compaction_split(messages, split_point)
        return messages[:split_point], messages[split_point:]

    def build_compaction_prompt(self) -> str:
        """Build compaction prompt from old messages only (recent are preserved)."""
        old_messages, _recent = self._split_messages_for_compaction()
        if not old_messages:
            return ""

        conversation_text = []
        for msg in old_messages:
            role = msg.role.upper()
            text = msg.get_text()
            if text:
                conversation_text.append(f"{role}: {text}")

        joined = "\n".join(conversation_text)
        return (
            "Please provide a concise summary of this conversation so far. "
            "Focus on:\n"
            "1. Key decisions made\n"
            "2. Important code changes or file modifications\n"
            "3. Current task status and next steps\n"
            "4. Any errors encountered and how they were resolved\n\n"
            "Keep the summary focused and actionable. Preserve specific file paths, "
            "function names, and technical details that are needed to continue the work.\n\n"
            f"Conversation:\n{joined}"
        )

    def apply_compaction(self, summary: str) -> tuple[int, int]:
        """Replace old messages with summary, keep recent messages intact."""
        original_tokens = self._conversation.get_total_tokens()

        _old, recent = self._split_messages_for_compaction()

        summary_msg = Message(role="user", content=f"[Conversation Summary]\n{summary}")
        summary_msg.token_count = self._token_counter.count_message(summary_msg.to_api_format())

        self._conversation.replace_messages([summary_msg] + recent)
        new_tokens = self._conversation.get_total_tokens()
        logger.info(f"Compaction: {original_tokens} -> {new_tokens} tokens")
        return (original_tokens, new_tokens)

    def reset(self) -> None:
        self._conversation = Conversation()
