"""DashScope provider — Aliyun DashScope's OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any, cast

from iac_code.agent.message import RECALLED_MEMORY_MARKER
from iac_code.agent.system_prompt import split_by_dynamic_boundary
from iac_code.providers.base import Message
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_TOKEN_PLAN_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

# Models that support DashScope explicit context cache (cache_control markers).
# Prefix-matched against the model name.  Extend when new models are added.
# Ref: https://help.aliyun.com/zh/model-studio/context-cache
_EXPLICIT_CACHE_MODEL_PREFIXES: tuple[str, ...] = (
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

_RECALLED_MEMORY_REMINDER_PREFIX = f"<system-reminder>\n{RECALLED_MEMORY_MARKER}:"


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
        **kwargs,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            effort=effort,
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
            self._mark_last_user_message_cacheable(api_messages)

        return api_messages

    @staticmethod
    def _mark_last_user_message_cacheable(api_messages: list[dict[str, Any]]) -> None:
        """Add ``cache_control`` to the last user message in *api_messages*.

        This extends the cache prefix to cover all conversation history up to
        and including the most recent user turn, so that successive rounds hit
        the cached prefix.  DashScope allows up to 4 ``cache_control`` markers;
        we use one for system-static and one here.
        """
        for msg in reversed(api_messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if _is_recalled_memory_reminder(content):
                continue
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            elif isinstance(content, list):
                # Content is already a list of blocks — tag the last text block.
                for block in reversed(content):
                    if isinstance(block, dict):
                        block_dict: dict[str, Any] = cast(dict[str, Any], block)
                        if block_dict.get("type") == "text":
                            block_dict["cache_control"] = {"type": "ephemeral"}
                            break
            break

    # -- Thinking kwargs -------------------------------------------------------

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.DASHSCOPE:
            return {}
        return {"extra_body": {"enable_thinking": True}}


def _is_recalled_memory_reminder(content: Any) -> bool:
    if isinstance(content, str):
        return content.startswith(_RECALLED_MEMORY_REMINDER_PREFIX)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and str(block.get("text") or "").startswith(_RECALLED_MEMORY_REMINDER_PREFIX):
                return True
    return False
