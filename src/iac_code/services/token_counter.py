"""Token counting utilities with model-aware encoding selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from loguru import logger

_MESSAGE_OVERHEAD = 4
_TOOL_USE_OVERHEAD = 10
_TOOL_DEFINITION_OVERHEAD = 12


@dataclass(frozen=True)
class TokenEstimateProfile:
    chars_per_token: float
    cjk_chars_per_token: float


# Map model name prefixes to tiktoken encoding names.
_MODEL_ENCODING_MAP = {
    "gpt-4o": "o200k_base",
    "gpt-4": "cl100k_base",
    "gpt-5": "o200k_base",
    "claude": "cl100k_base",
    "o3": "o200k_base",
    "o4": "o200k_base",
}

_DEFAULT_PROFILE = TokenEstimateProfile(chars_per_token=4.0, cjk_chars_per_token=1.6)
_MODEL_ESTIMATE_PROFILES = {
    "qwen": TokenEstimateProfile(chars_per_token=3.5, cjk_chars_per_token=1.0),
    "qwq": TokenEstimateProfile(chars_per_token=3.5, cjk_chars_per_token=1.0),
    "kimi": TokenEstimateProfile(chars_per_token=3.5, cjk_chars_per_token=1.1),
    "glm": TokenEstimateProfile(chars_per_token=3.5, cjk_chars_per_token=1.1),
    "doubao": TokenEstimateProfile(chars_per_token=3.5, cjk_chars_per_token=1.1),
    "minimax": TokenEstimateProfile(chars_per_token=3.5, cjk_chars_per_token=1.1),
    "gemini": TokenEstimateProfile(chars_per_token=4.0, cjk_chars_per_token=1.2),
}


def _select_profile(model: str) -> TokenEstimateProfile:
    model_lower = model.lower()
    for prefix, profile in _MODEL_ESTIMATE_PROFILES.items():
        if model_lower.startswith(prefix):
            return profile
    return _DEFAULT_PROFILE


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _tool_value(tool: Any, key: str, default: Any) -> Any:
    if isinstance(tool, dict):
        return tool.get(key, default)
    return getattr(tool, key, default)


def _select_encoding(model: str) -> str | None:
    model_lower = model.lower()
    for prefix, encoding in _MODEL_ENCODING_MAP.items():
        if model_lower.startswith(prefix):
            return encoding
    return None


class TokenCounter:
    """Count tokens using tiktoken with model-aware encoding selection."""

    def __init__(self, model: str = "") -> None:
        self._encoder = None
        self._profile = _select_profile(model)
        encoding_name = _select_encoding(model)
        if encoding_name is None:
            return
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
        return self._estimate_text(text)

    def _estimate_text(self, text: str) -> int:
        cjk_chars = 0
        other_chars = 0
        for char in text:
            if char.isspace():
                continue
            if _is_cjk(char):
                cjk_chars += 1
            else:
                other_chars += 1
        estimated = (cjk_chars / self._profile.cjk_chars_per_token) + (other_chars / self._profile.chars_per_token)
        return max(1, int(estimated + 0.999))

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

    def count_tool_definition(self, tool: Any) -> int:
        name = _tool_value(tool, "name", "")
        description = _tool_value(tool, "description", "")
        input_schema = _tool_value(tool, "input_schema", {})
        schema_text = json.dumps(input_schema, ensure_ascii=False, sort_keys=True)
        return (
            _TOOL_DEFINITION_OVERHEAD
            + self.count_text(str(name))
            + self.count_text(str(description))
            + self.count_text(schema_text)
        )

    def count_tool_definitions(self, tools: list[Any]) -> int:
        return sum(self.count_tool_definition(tool) for tool in tools)
