"""DashScope provider — Aliyun DashScope's OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any, cast

from iac_code.agent.message import RECALLED_MEMORY_MARKER
from iac_code.agent.system_prompt import split_by_dynamic_boundary
from iac_code.providers.base import Message
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec, normalize_effort

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_TOKEN_PLAN_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

# Models that support DashScope explicit context cache (cache_control markers).
# Prefix-matched against the model name.  Extend when new models are added.
# Ref: https://help.aliyun.com/zh/model-studio/context-cache
_EXPLICIT_CACHE_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen3.8-max-preview",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "qwen3.5-plus",
    "qwen3.6-plus",
    "qwen-plus",
    "qwen3.5-flash",
    "qwen3.6-flash",
    "qwen-flash",
)

# Models documented to accept extra_body.preserve_thinking. Keep this list
# separate from thinking support: sending the parameter to other models fails.
_PRESERVE_THINKING_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen3.8-max-preview",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-max-preview",
    "qwen3.6-plus",
    "qwen3.6-flash",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "kimi/kimi-k3",
    "kimi/kimi-k2.7-code",
    "kimi/kimi-k2.6",
)

_RECALLED_MEMORY_REMINDER_PREFIX = f"<system-reminder>\n{RECALLED_MEMORY_MARKER}:"
_DISABLE_THINKING_EFFORTS = {"none", "off", "disable", "disabled", "false", "0"}


class DashScopeProvider(OpenAIProvider):
    """Provider backed by Aliyun DashScope's OpenAI-compatible endpoint.

    Both standard DashScope and DashScope Token Plan share the same wire
    protocol (extra_body.enable_thinking=True); only the base URL and
    thinking-registry key differ. Both are injected via __init__.
    """

    _PROVIDER_KEY = "dashscope"
    supports_stream_options = True

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        effort: str | None = None,
        base_url: str = DASHSCOPE_BASE_URL,
        provider_key: str = "dashscope",
        thinking_enabled: bool | None = None,
        thinking_budget: int | None = None,
        max_completion_tokens: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            effort=effort,
            thinking_enabled=thinking_enabled,
            thinking_budget=thinking_budget,
            max_completion_tokens=max_completion_tokens,
            provider_key=provider_key,
        )

    # -- Explicit context cache ------------------------------------------------

    def _supports_explicit_cache(self) -> bool:
        return self._model.startswith(_EXPLICIT_CACHE_MODEL_PREFIXES)

    def _build_api_messages(
        self,
        messages: list[Message],
        system: str,
        cache_policy: str = "default",
    ) -> list[dict[str, Any]]:
        api_messages: list[dict[str, Any]] = []
        explicit_cache_enabled = cache_policy != "no_explicit_cache" and self._supports_explicit_cache()
        if system:
            if explicit_cache_enabled:
                static_part, dynamic_part = split_by_dynamic_boundary(system)
                content_blocks: list[dict[str, Any]] = [
                    {"type": "text", "text": static_part, "cache_control": {"type": "ephemeral"}},
                ]
                if dynamic_part:
                    content_blocks.append({"type": "text", "text": dynamic_part})
                api_messages.append({"role": "system", "content": content_blocks})
            else:
                api_messages.append({"role": "system", "content": system})
        api_messages.extend(self._convert_messages(messages))

        if explicit_cache_enabled:
            self._mark_user_messages_cacheable(api_messages)

        return api_messages

    @staticmethod
    def _mark_user_messages_cacheable(api_messages: list[dict[str, Any]]) -> None:
        """Add ``cache_control`` to cacheable user message prefixes.

        This extends the cache prefix to cover all conversation history up to
        marked user turns. User messages with DYNAMIC_BOUNDARY get their static
        prefix marked even when they are not the last user turn. For ordinary
        conversations, only the last user message is marked.
        """
        last_plain_user_message: dict[str, Any] | None = None
        for msg in api_messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if _is_recalled_memory_reminder(content):
                continue
            if _content_has_dynamic_boundary(content):
                _mark_message_content_cacheable(msg)
            else:
                last_plain_user_message = msg
        if last_plain_user_message is not None:
            _mark_message_content_cacheable(last_plain_user_message)

    # -- Thinking kwargs -------------------------------------------------------

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is ThinkingFamily.MINIMAX:
            thinking_type = "disabled" if self._thinking_disabled() else "adaptive"
            return {"extra_body": {"thinking": {"type": thinking_type}}}
        if spec.family is not ThinkingFamily.DASHSCOPE:
            return {}
        effort = normalize_effort(self._effort)
        allowed = {e.value for e in spec.allowed_efforts}
        if self._model in {"kimi/kimi-k3", "qwen3.8-max-preview"}:
            kwargs: dict[str, Any] = {"extra_body": {"preserve_thinking": True}}
            if self._thinking_disabled():
                return kwargs
            if effort in {None, "auto"} and self._thinking_forced() and spec.default_effort is not None:
                effort = spec.default_effort.value
            if effort in allowed:
                kwargs["reasoning_effort"] = effort
            elif effort not in {None, "auto"} and spec.default_effort is not None:
                kwargs["reasoning_effort"] = spec.default_effort.value
            return kwargs
        disabled_by_effort = effort in _DISABLE_THINKING_EFFORTS and effort not in allowed
        if self._thinking_disabled() or disabled_by_effort:
            if not spec.supports_disable:
                return self._preserve_thinking_kwargs()
            return {"extra_body": {"enable_thinking": False}}
        extra_body: dict[str, Any] = {"enable_thinking": True}
        if self._supports_preserve_thinking():
            extra_body["preserve_thinking"] = True
        thinking_budget = self._effective_thinking_budget()
        if thinking_budget is not None:
            extra_body["thinking_budget"] = thinking_budget

        kwargs: dict[str, Any] = {"extra_body": extra_body}
        if not spec.uses_reasoning_effort_param:
            return kwargs
        if effort in {None, "auto"} and self._thinking_forced() and spec.default_effort is not None:
            effort = spec.default_effort.value
        if effort is None or effort == "auto":
            return kwargs
        if effort in allowed:
            kwargs["reasoning_effort"] = effort
        elif spec.default_effort is not None:
            kwargs["reasoning_effort"] = spec.default_effort.value
        return kwargs

    def _supports_preserve_thinking(self) -> bool:
        return self._model.startswith(_PRESERVE_THINKING_MODEL_PREFIXES)

    def _preserve_thinking_kwargs(self) -> dict[str, Any]:
        if not self._supports_preserve_thinking():
            return {}
        return {"extra_body": {"preserve_thinking": True}}


def _mark_message_content_cacheable(msg: dict[str, Any]) -> None:
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = _cacheable_text_blocks(content)
    elif isinstance(content, list):
        # Content is already a list of blocks — tag the last text block.
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if isinstance(block, dict):
                block_dict: dict[str, Any] = cast(dict[str, Any], block)
                if block_dict.get("type") == "text":
                    text = block_dict.get("text")
                    if isinstance(text, str):
                        content[index : index + 1] = _cacheable_text_blocks(text)
                    else:
                        block_dict["cache_control"] = {"type": "ephemeral"}
                    break


def _is_recalled_memory_reminder(content: Any) -> bool:
    if isinstance(content, str):
        return content.startswith(_RECALLED_MEMORY_REMINDER_PREFIX)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and str(block.get("text") or "").startswith(_RECALLED_MEMORY_REMINDER_PREFIX):
                return True
    return False


def _content_has_dynamic_boundary(content: Any) -> bool:
    if isinstance(content, str):
        return bool(split_by_dynamic_boundary(content)[1])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and bool(split_by_dynamic_boundary(str(block.get("text") or ""))[1]):
                return True
    return False


def _cacheable_text_blocks(text: str) -> list[dict[str, Any]]:
    static_part, dynamic_part = split_by_dynamic_boundary(text)
    blocks: list[dict[str, Any]] = []
    if static_part:
        blocks.append({"type": "text", "text": static_part, "cache_control": {"type": "ephemeral"}})
    if dynamic_part:
        blocks.append({"type": "text", "text": dynamic_part})
    if not blocks:
        blocks.append({"type": "text", "text": text, "cache_control": {"type": "ephemeral"}})
    return blocks
