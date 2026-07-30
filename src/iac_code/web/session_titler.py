"""Web 会话标题的一次性 LLM 生成(纯逻辑,不碰 session/持久化)。"""

from __future__ import annotations

import asyncio
import logging

from iac_code.config import get_active_provider_key, load_credentials
from iac_code.providers.base import ContentBlock, Message
from iac_code.providers.manager import ProviderManager
from iac_code.providers.thinking import get_thinking_spec
from iac_code.services.session_index import _trim_title
from iac_code.web.diagram_optimizer import provider_overrides_from
from iac_code.web.runtime import WebModelSelection

logger = logging.getLogger(__name__)

TITLE_MAX_TOKENS = 32
TITLE_TEXT_INPUT_LIMIT = 2000
TITLE_SYSTEM_PROMPT = (
    "You generate a very short title for a conversation based on the user's first message. "
    "Rules: at most 6 words or about 20 CJK characters; use the same language as the input; "
    "no surrounding quotes, no trailing punctuation, no explanation. Output only the title."
)


def _title_effort_override(provider_key: str | None, model: str) -> str:
    """关闭 thinking;模型不支持关闭时回落到最低 effort(minimal)。"""
    if not provider_key:
        return "none"
    spec = get_thinking_spec(provider_key, model)
    if spec.supports_disable or not spec.allowed_efforts:
        return "none"
    return spec.allowed_efforts[0].value


def _build_messages(text: str | None, image_blocks: list[ContentBlock]) -> list[Message]:
    trimmed = (text or "").strip()[:TITLE_TEXT_INPUT_LIMIT]
    if not image_blocks:
        return [Message(role="user", content=trimmed or " ")]
    blocks: list[ContentBlock] = []
    if trimmed:
        blocks.append(ContentBlock(type="text", text=trimmed))
    blocks.extend(image_blocks)
    return [Message(role="user", content=blocks)]


def _build_manager(selection: WebModelSelection) -> ProviderManager:
    overrides = provider_overrides_from(selection)
    credentials = overrides.pop("credentials_override", None) or load_credentials(model=selection.model)
    return ProviderManager(
        model=selection.model,
        credentials=credentials,
        effort_override=_title_effort_override(
            selection.provider or get_active_provider_key(), selection.model
        ),
        **overrides,
    )


async def generate_session_title(
    *,
    text: str | None,
    image_blocks: list[ContentBlock],
    selection: WebModelSelection,
    timeout: float = 20.0,
) -> str | None:
    """一次性 LLM 调用生成标题;首次失败重试一次;仍失败返回 None。不做持久化。"""
    messages = _build_messages(text, image_blocks)
    for attempt in range(2):
        try:
            manager = _build_manager(selection)
            response = await asyncio.wait_for(
                manager.complete(messages, TITLE_SYSTEM_PROMPT, max_tokens=TITLE_MAX_TOKENS),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - best-effort side query
            logger.debug("session title generation attempt %d failed: %s", attempt + 1, exc)
            continue
        title = _trim_title((response.text or "").strip()) if response is not None else ""
        if title:
            return title
        return None
    return None
