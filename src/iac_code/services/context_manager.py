"""Context manager for conversation history, token tracking, and segmented compaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from iac_code.agent.message import (
    ContentBlock,
    Conversation,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    create_recalled_memory_message,
    get_recalled_memory_files,
    is_compaction_summary_message,
    is_recalled_memory_message,
)
from iac_code.services.token_counter import TokenCounter


@dataclass
class ContextWindowConfig:
    """Model-specific context window configuration."""

    context_window: int
    max_output_tokens: int
    compact_buffer: int
    compact_threshold: float
    preserve_recent_turns: int


_LONG_CONTEXT_BUFFER = 20_000
_COMPACT_THRESHOLD = 0.93
_PRESERVE_RECENT_TURNS = 3


def _context_config(context_window: int, max_output_tokens: int = 8_192) -> ContextWindowConfig:
    return ContextWindowConfig(
        context_window,
        max_output_tokens,
        _LONG_CONTEXT_BUFFER,
        _COMPACT_THRESHOLD,
        _PRESERVE_RECENT_TURNS,
    )


_MODEL_EXACT_CONFIGS: dict[str, ContextWindowConfig] = {
    "claude-fable-5": _context_config(1_000_000, 128_000),
    "claude-opus-4-8": _context_config(1_000_000, 128_000),
    "claude-sonnet-5": _context_config(1_000_000, 128_000),
    "claude-sonnet-4-6-1m": _context_config(1_000_000, 64_000),
    "gpt-5.5": _context_config(1_050_000, 128_000),
    "gpt-5.4": _context_config(1_050_000, 128_000),
    "gpt-5.4-mini": _context_config(400_000, 128_000),
    "gpt-5.4-nano": _context_config(400_000, 128_000),
    "gpt-5.3-codex": _context_config(400_000, 128_000),
    "gpt-5.2": _context_config(400_000, 128_000),
    "gemini-3.6-flash": _context_config(1_048_576, 65_536),
    "gemini-3.5-flash": _context_config(1_048_576, 65_536),
    "gemini-3.5-flash-lite": _context_config(1_048_576, 65_536),
    "gemini-3.1-pro-preview": _context_config(1_048_576, 65_536),
    "gemini-3.1-pro-preview-customtools": _context_config(1_048_576, 65_536),
    "gemini-3-flash-preview": _context_config(1_048_576, 65_536),
    "gemini-3.1-flash-lite": _context_config(1_048_576, 65_536),
    "gemini-2.5-pro": _context_config(1_048_576, 65_536),
    "gemini-2.5-flash": _context_config(1_048_576, 65_536),
    "gemini-2.5-flash-lite": _context_config(1_048_576, 65_536),
    "kimi-k3": _context_config(1_000_000),
    # DashScope's Moonshot-hosted K3 advertises a 1M-token context window.
    # Keep the output cap conservative until the endpoint documents a limit.
    "kimi/kimi-k3": _context_config(1_000_000),
    "kimi-k2.7-code": _context_config(262_144),
    "kimi-k2.7-code-highspeed": _context_config(262_144),
    "kimi-k2.6": _context_config(262_144),
    "kimi-k2.5": _context_config(262_144),
    "qwen3.8-max-preview": _context_config(1_000_000),
    "qwen3.7-max": _context_config(1_000_000),
    "qwen3.7-plus": _context_config(1_000_000),
    "qwen3.6-plus": _context_config(1_000_000),
    "qwen3.6-flash": _context_config(1_000_000),
    "qwen3.5-plus": _context_config(1_000_000),
    "qwen3.5-flash": _context_config(1_000_000),
    "qwen-plus": _context_config(1_000_000),
    "qwen-flash": _context_config(1_000_000),
    "qwen3-coder-plus": _context_config(1_000_000),
    "qwen3.6-max-preview": _context_config(262_144),
    "qwen3-max": _context_config(262_144),
    "qwen3-coder-next": _context_config(262_144),
    "deepseek-v4-pro": _context_config(1_000_000),
    "deepseek-v4-flash": _context_config(1_000_000),
    "glm-5.2": _context_config(1_000_000, 128_000),
    "glm-5.1": _context_config(202_752),
    "glm-5": _context_config(202_752),
    "minimax-m3": _context_config(1_000_000),
    "minimax/minimax-m3": _context_config(196_608),
    "minimax-m2.7": _context_config(196_608),
    "minimax-m2.7-highspeed": _context_config(196_608),
    "minimax-m2.5": _context_config(196_608),
    "minimax-m2.5-highspeed": _context_config(196_608),
}

_MODEL_CONFIGS: dict[str, ContextWindowConfig] = {
    "claude": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
    "gpt-5.6": ContextWindowConfig(1_050_000, 128_000, 20_000, 0.93, 3),
    "gpt-5": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
    "gpt-4": ContextWindowConfig(128_000, 8_192, 15_000, 0.93, 3),
    "qwen": ContextWindowConfig(131_072, 8_192, 15_000, 0.93, 3),
    "qwq": ContextWindowConfig(131_072, 8_192, 15_000, 0.93, 3),
    "o3": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
    "o4": ContextWindowConfig(200_000, 8_192, 20_000, 0.93, 3),
}
_DEFAULT_CONFIG = ContextWindowConfig(128_000, 8_192, 15_000, 0.93, 3)

# 摘要 prompt 里单个 tool_use.input / tool_result 正文的截断上限（字符）。
# 流水线步骤的真实内容几乎全在工具块里（读文件、API schema、生成的模板、校验输出），
# 必须纳入摘要才能避免退化摘要（见问题 #2），但需截断以免摘要 prompt 反而爆量。
_SUMMARY_BLOCK_TEXT_LIMIT = 2_000


def get_context_window_config(model: str) -> ContextWindowConfig:
    model_lower = model.lower()
    exact_config = _MODEL_EXACT_CONFIGS.get(model_lower)
    if exact_config is not None:
        return exact_config
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

    def add_recalled_memory_message(self, content: str, selected_files: list[str]) -> Message:
        msg = create_recalled_memory_message(content, selected_files)
        self._conversation.messages.append(msg)
        msg.token_count = self._token_counter.count_message(msg.to_api_format())
        return msg

    def add_raw_message(self, raw_msg: dict[str, Any]) -> Message:
        """Add a raw message dict (e.g. from ToolResult.new_messages) to the conversation."""
        role = raw_msg.get("role", "user")
        content = raw_msg.get("content", "")
        metadata = raw_msg.get("metadata")
        msg = Message(role=role, content=content, metadata=dict(metadata) if isinstance(metadata, dict) else {})
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

    def _last_compaction_index(self) -> int | None:
        messages = self._conversation.messages
        for i in range(len(messages) - 1, -1, -1):
            if is_compaction_summary_message(messages[i]):
                return i
        return None

    def get_context_messages(self) -> list[Message]:
        """有效上下文：最后一个压缩标记起到末尾；无标记则完整历史。"""
        idx = self._last_compaction_index()
        if idx is None:
            return self._conversation.messages
        return self._conversation.messages[idx:]

    def remove_cleanup_prompt_messages(self) -> int:
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        kept = [message for message in self._conversation.messages if not is_cleanup_prompt_message(message)]
        removed = len(self._conversation.messages) - len(kept)
        if removed:
            self._conversation.replace_messages(kept)
        return removed

    def get_api_messages(self) -> list[dict[str, Any]]:
        return [m.to_api_format() for m in self.get_context_messages()]

    def get_surfaced_memory_files(self) -> set[str]:
        files: set[str] = set()
        for msg in self.get_context_messages():
            files.update(get_recalled_memory_files(msg))
        return files

    def get_total_tokens(self) -> int:
        return (
            self._system_prompt_tokens
            + self._tool_definition_tokens
            + sum(m.token_count for m in self.get_context_messages())
        )

    def get_usage(self) -> dict[str, Any]:
        """Return detailed token usage breakdown by category."""
        user_tokens = 0
        assistant_tokens = 0
        tool_result_tokens = 0

        for msg in self.get_context_messages():
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
            "message_count": len(self.get_context_messages()),
        }

    def needs_compaction(self) -> bool:
        total = self.get_total_tokens()
        threshold = self._config.context_window * self._config.compact_threshold
        if total <= threshold:
            return False
        # 超阈值但没有可压缩的旧消息（token 权重全落在保留尾部，例如单条超大工具结果）时，
        # 压缩是空操作：build_compaction_prompt 为空 → 不留会话记录，且因为上下文没变，下一回合
        # 仍判定需要压缩，于是每回合反复空转（见问题 #2 无记录 / #3 反复触发）。仅当存在可压缩的
        # 旧消息、压缩能真正推进时才触发；这与 apply_compaction 自身的 `if not old` 空操作守卫一致。
        old, _recent = self._split_messages_for_compaction()
        return bool(old)

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
    def _is_user_turn_start(cls, message: Message) -> bool:
        """Whether ``message`` opens a fresh user turn.

        A user turn starts with a user-authored message that is *not* a
        ``tool_result`` carrier. The preserved compaction tail must begin
        here so the model never sees an assistant reply (or a bare
        ``tool_result``) whose originating user prompt was folded into the
        summary — a shape that leaves weaker models spinning without the
        prompt that drove the work.
        """
        return message.role == "user" and not cls._tool_result_ids(message)

    @classmethod
    def _is_safe_tail_start(cls, message: Message, *, allow_assistant_start: bool) -> bool:
        """Whether the preserved tail may begin at ``message``.

        Strict mode (default) requires a real user-turn start. Relaxed mode
        only forbids starting on a ``tool_result`` carrier — an assistant
        message qualifies. Relaxed mode is a fallback for conversations whose
        only user-turn start is the very first message (e.g. a pipeline step:
        one initial instruction followed by a long run of tool round-trips),
        where strict mode collapses the split to index 0 and compaction can
        never make progress. The compaction marker (a ``user`` message) is
        inserted immediately before the tail, so an assistant start still has
        a driving prompt in front of it.
        """
        if allow_assistant_start:
            return not cls._tool_result_ids(message)
        return cls._is_user_turn_start(message)

    @classmethod
    def _find_safe_compaction_split(
        cls, messages: list[Message], split_point: int, *, allow_assistant_start: bool = False
    ) -> int:
        split_point = max(0, min(split_point, len(messages)))
        if split_point >= len(messages):
            return split_point
        while split_point > 0:
            # 1) 保留尾部必须从一个安全边界开始:向前回退跳过 tool_result 载体(严格模式
            #    还会跳过 assistant 回复),避免尾部丢失引导它的 user 提问、或以未配对
            #    tool_result 开头。
            while split_point > 0 and not cls._is_safe_tail_start(
                messages[split_point], allow_assistant_start=allow_assistant_start
            ):
                split_point -= 1
            if split_point == 0:
                break

            # 2) 旧消息内不得残留未配对的 tool_use(否则摘要段丢掉其 tool_result)。
            old_tool_uses: dict[str, int] = {}
            old_tool_results: set[str] = set()

            for index, message in enumerate(messages[:split_point]):
                for tool_use_id in cls._tool_use_ids(message):
                    old_tool_uses.setdefault(tool_use_id, index)
                old_tool_results.update(cls._tool_result_ids(message))

            unpaired_tool_uses = set(old_tool_uses) - old_tool_results
            if not unpaired_tool_uses:
                return split_point

            # 回退到最早未配对 tool_use 处;下一轮再对齐到 user 回合开头。
            split_point = min(old_tool_uses[tool_use_id] for tool_use_id in unpaired_tool_uses)

        return split_point

    @staticmethod
    def _has_compactible_content(messages: list[Message]) -> bool:
        """``old`` 里是否存在重压能真正带来缩减的消息。

        旧摘要标记、召回记忆、清理提示这三类要么会被 ``build_compaction_prompt``
        跳过、要么只是把摘要再总结一遍——都不产生实质缩减。若 ``old`` 全由它们
        组成(极端形态:多层旧摘要 + 单个超大工具往返回合,尾部整条工具链回退进
        recent、old 仅剩上一个摘要标记),压缩就是空操作。此时应让位给放宽切分,
        把较早的大块工具往返折进摘要。
        """
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        return any(
            not (is_compaction_summary_message(m) or is_recalled_memory_message(m) or is_cleanup_prompt_message(m))
            for m in messages
        )

    def _split_messages_for_compaction(self) -> tuple[list[Message], list[Message]]:
        """Split messages into [old_messages, recent_messages].

        A "turn" is a user+assistant message pair. We preserve the last
        `preserve_recent_turns` turns (counting from the end).
        """
        messages = self.get_context_messages()
        preserve_count = self._config.preserve_recent_turns * 2

        if len(messages) <= preserve_count:
            return [], messages

        naive_split = len(messages) - preserve_count
        # 第一级(严格):优先把尾部对齐到真正的 user 回合开头,普通聊天沿用既有行为。
        strict_split = self._find_safe_compaction_split(messages, naive_split)
        strict_old = messages[:strict_split]
        if strict_split > 0 and self._has_compactible_content(strict_old):
            return strict_old, messages[strict_split:]

        # 第二级(放宽):严格切出的 old 里没有任何可压缩内容(为空,或只剩旧摘要标记/召回记忆/
        # 清理提示)。两种形态:
        #   a) 整段只有一个 user 回合开头(流水线步骤:初始指令 + 成串工具往返),严格切分塌缩到
        #      0、old 为空、压缩永远空转;
        #   b) 「多层旧摘要 + 单个超大工具往返回合」:尾部整条工具链回退进 recent,old 仅剩上一个
        #      摘要标记,重压只是把摘要再总结一遍、大块工具结果原样保留 → 毫无缩减。
        # 放宽为从一条非 tool_result 载体(assistant)的消息开头,把较早的大块工具往返折进摘要;
        # 压缩标记(user 摘要)插在其前提供引导上下文。放宽模式内部仍守「old 不得残留未配对
        # tool_use」,不会拆散 tool_use/tool_result 配对。
        relaxed_split = self._find_safe_compaction_split(messages, naive_split, allow_assistant_start=True)
        if relaxed_split > strict_split:
            # 放宽确实能多折进内容(而非仅退回同一批标记)时才改用它;否则保持严格结果:
            # 严格 old 若是多条旧摘要标记则照旧合并(有缩减),单条标记则维持既有空操作语义。
            return messages[:relaxed_split], messages[relaxed_split:]
        return strict_old, messages[strict_split:]

    @staticmethod
    def _truncate_for_summary(text: str) -> str:
        text = text.strip()
        if len(text) <= _SUMMARY_BLOCK_TEXT_LIMIT:
            return text
        return text[:_SUMMARY_BLOCK_TEXT_LIMIT] + "…"

    @classmethod
    def _render_message_for_summary(cls, msg: Message) -> str:
        """Render a message for the summary prompt, including tool activity.

        ``Message.get_text()`` only surfaces ``TextBlock`` text, so a pipeline
        step — whose real work lives almost entirely in ``tool_use`` /
        ``tool_result`` blocks — would feed the summarizer next to nothing,
        yielding a degenerate "no prior conversation history" summary (见问题
        #2). Include a compact rendering of tool calls and (truncated) tool
        results so the summary reflects what actually happened. ``thinking``
        and image blocks stay excluded (noisy / non-textual).
        """
        if isinstance(msg.content, str):
            return msg.content.strip()
        parts: list[str] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                text = block.text.strip()
                if text:
                    parts.append(text)
            elif isinstance(block, ToolUseBlock):
                preview = cls._truncate_for_summary(str(block.input)) if block.input else ""
                parts.append("[调用工具 {}] {}".format(block.name, preview).rstrip())
            elif isinstance(block, ToolResultBlock):
                result_text = cls._truncate_for_summary(block.content) if isinstance(block.content, str) else ""
                if result_text:
                    label = "工具报错" if block.is_error else "工具结果"
                    parts.append("[{}] {}".format(label, result_text))
        return "\n".join(parts)

    def build_compaction_prompt(self) -> str:
        """Build compaction prompt from old messages only (recent are preserved)."""
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        old_messages, _recent = self._split_messages_for_compaction()
        if not old_messages:
            return ""

        conversation_text = []
        for msg in old_messages:
            if is_recalled_memory_message(msg) or is_cleanup_prompt_message(msg):
                continue
            role = msg.role.upper()
            text = self._render_message_for_summary(msg)
            if text:
                conversation_text.append(f"{role}: {text}")
        if not conversation_text:
            return ""

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
        """插入压缩标记，保留完整历史；有效上下文收缩为 [marker]+cleanup+尾部。"""
        from iac_code.agent.message import (
            COMPACTION_SUMMARY_TAIL_METADATA_KEY,
            create_compaction_summary_message,
        )
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        original_tokens = self.get_total_tokens()  # 有效切片(压缩前)

        old, recent = self._split_messages_for_compaction()  # 有效切片内切分
        if not old:
            return (original_tokens, original_tokens)  # 无可压缩，空操作

        marker = create_compaction_summary_message(summary)
        marker.token_count = self._token_counter.count_message(marker.to_api_format())

        preserved_hidden = [m for m in old if is_cleanup_prompt_message(m)]
        preserved_ids = {id(m) for m in preserved_hidden}

        messages = self._conversation.messages
        insert_index = len(messages) - len(recent)  # recent 是完整历史后缀
        head = [m for m in messages[:insert_index] if id(m) not in preserved_ids]

        # 压缩这一刻，标记之后的所有消息（preserved_hidden + recent）都是时间上早于它的保留尾部；
        # 记录条数，供可见转录把边界下沉到尾部之后。之后新追加的回合不计入，自然排在标记之后。
        marker.metadata[COMPACTION_SUMMARY_TAIL_METADATA_KEY] = len(preserved_hidden) + len(recent)

        self._conversation.replace_messages(head + [marker] + preserved_hidden + recent)

        new_tokens = self.get_total_tokens()  # 有效切片(压缩后)
        logger.info(f"Compaction: {original_tokens} -> {new_tokens} tokens (history preserved)")
        return (original_tokens, new_tokens)

    def reset(self) -> None:
        self._conversation = Conversation()
