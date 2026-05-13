"""Token counting utilities with model-aware encoding selection."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

_MESSAGE_OVERHEAD = 4
_TOOL_USE_OVERHEAD = 10

# Map model name prefixes to tiktoken encoding names.
_MODEL_ENCODING_MAP = {
    "gpt-4o": "o200k_base",
    "gpt-4": "cl100k_base",
    "gpt-5": "o200k_base",
    "claude": "cl100k_base",
    "qwen": "cl100k_base",
    "qwq": "cl100k_base",
    "o3": "o200k_base",
    "o4": "o200k_base",
}
_DEFAULT_ENCODING = "cl100k_base"


def _select_encoding(model: str) -> str:
    model_lower = model.lower()
    for prefix, encoding in _MODEL_ENCODING_MAP.items():
        if model_lower.startswith(prefix):
            return encoding
    return _DEFAULT_ENCODING


class TokenCounter:
    """Count tokens using tiktoken with model-aware encoding selection."""

    def __init__(self, model: str = "") -> None:
        self._encoder = None
        encoding_name = _select_encoding(model)
        try:
            import tiktoken

            self._encoder = tiktoken.get_encoding(encoding_name)
        except Exception:
            logger.debug("tiktoken not available, using estimation fallback")

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        if self._encoder:
            return len(self._encoder.encode(text))
        return max(1, len(text) // 4)

    def count_message(self, message: dict[str, Any]) -> int:
        count = _MESSAGE_OVERHEAD
        content = message.get("content", "")
        if isinstance(content, str):
            count += self.count_text(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        count += self.count_text(block.get("text", ""))
                    elif block_type == "tool_use":
                        count += _TOOL_USE_OVERHEAD
                        count += self.count_text(block.get("name", ""))
                        count += self.count_text(json.dumps(block.get("input", {})))
                    elif block_type == "tool_result":
                        count += self.count_text(block.get("content", ""))
                        count += _TOOL_USE_OVERHEAD
        return count

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        return sum(self.count_message(m) for m in messages)
