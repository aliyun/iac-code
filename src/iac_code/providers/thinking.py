"""Centralized thinking-mode registry keyed by (provider_key, model_name).

Two-layer registry: outer key is the provider key (matches ``auth.py``
``key_name`` and ``settings.yml`` ``providers.<key>``); inner key is the model
name. The same model name can appear under multiple providers with different
specs — e.g. ``deepseek-v4-pro`` is ``OPENAI`` family on the official DeepSeek
endpoint but ``DASHSCOPE`` family when proxied through Aliyun's compatible-mode
service.

Wire-format assembly lives in each provider subclass's
``_build_thinking_kwargs()``. This module only declares capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EffortLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    AUTO = "auto"


EFFORT_ORDER: list[EffortLevel] = [
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
    EffortLevel.AUTO,
]


EFFORT_SYMBOLS: dict[EffortLevel, str] = {
    EffortLevel.LOW: "◆",
    EffortLevel.MEDIUM: "◆◆",
    EffortLevel.HIGH: "◆◆◆",
    EffortLevel.XHIGH: "◆◆◆◆",
    EffortLevel.MAX: "◆◆◆◆◆",
    EffortLevel.AUTO: "◆",
}


class ThinkingFamily(Enum):
    """The model's thinking protocol family. Wire format depends on provider."""

    NONE = "none"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"  # reasoning_effort + extra_body.thinking.type=enabled
    DASHSCOPE = "dashscope"  # extra_body.enable_thinking [+ thinking_budget]
    GEMINI = "gemini"


@dataclass(frozen=True)
class ThinkingSpec:
    family: ThinkingFamily
    allowed_efforts: tuple[EffortLevel, ...] = ()
    default_effort: EffortLevel | None = None
    default_thinking_budget: int | None = None  # reserved; not yet emitted

    @property
    def supports_effort(self) -> bool:
        return bool(self.allowed_efforts)

    @property
    def effort_range(self) -> tuple[EffortLevel, EffortLevel] | None:
        if not self.allowed_efforts:
            return None
        return self.allowed_efforts[0], self.allowed_efforts[-1]


# ---------------------------------------------------------------------------
# Per-(provider, model) registry
# ---------------------------------------------------------------------------


_OPENAI_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
)

_GEMINI_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
)

_ANTHROPIC_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
    EffortLevel.AUTO,
)

# DeepSeek V4 accepts only high/max — XHIGH is intentionally skipped.
_DEEPSEEK_EFFORTS: tuple[EffortLevel, ...] = (EffortLevel.HIGH, EffortLevel.MAX)


_NONE_SPEC = ThinkingSpec(family=ThinkingFamily.NONE)


MODEL_THINKING: dict[str, dict[str, ThinkingSpec]] = {
    "anthropic": {
        "claude-opus-4-7": ThinkingSpec(ThinkingFamily.ANTHROPIC, _ANTHROPIC_EFFORTS, EffortLevel.HIGH),
        "claude-opus-4-6": ThinkingSpec(ThinkingFamily.ANTHROPIC, _ANTHROPIC_EFFORTS, EffortLevel.HIGH),
        "claude-sonnet-4-6": ThinkingSpec(ThinkingFamily.ANTHROPIC, _ANTHROPIC_EFFORTS, EffortLevel.HIGH),
        "claude-sonnet-4-6-1m": ThinkingSpec(ThinkingFamily.ANTHROPIC, _ANTHROPIC_EFFORTS, EffortLevel.HIGH),
        "claude-haiku-4-5-20251001": ThinkingSpec(ThinkingFamily.ANTHROPIC, _ANTHROPIC_EFFORTS, EffortLevel.HIGH),
    },
    "openai": {
        "gpt-5.5": ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH),
        "gpt-5.4": ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH),
        "gpt-5.4-mini": ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH),
        "gpt-5.3-codex": ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH),
        "gpt-5.2": ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH),
        "o3": ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH),
        "o4-mini": ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH),
    },
    "deepseek": {
        "deepseek-v4-pro": ThinkingSpec(ThinkingFamily.OPENAI, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
        "deepseek-v4-flash": ThinkingSpec(ThinkingFamily.OPENAI, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
    },
    "dashscope": {
        "qwen3.6-max-preview": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.6-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.5-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.5-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwq-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "kimi-k2.6": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "glm-5.1": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "deepseek-v4-pro": ThinkingSpec(ThinkingFamily.DASHSCOPE, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
        "deepseek-v4-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
    },
    "dashscope_token_plan": {
        "qwen3.6-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "deepseek-v3.2": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "glm-5": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "MiniMax-M2.5": ThinkingSpec(ThinkingFamily.DASHSCOPE),
    },
    "gemini": {
        "gemini-3.1-pro-preview": ThinkingSpec(ThinkingFamily.GEMINI, _GEMINI_EFFORTS, EffortLevel.MEDIUM),
        "gemini-3-flash-preview": ThinkingSpec(ThinkingFamily.GEMINI, _GEMINI_EFFORTS, EffortLevel.MEDIUM),
        "gemini-2.5-pro": ThinkingSpec(ThinkingFamily.GEMINI, _GEMINI_EFFORTS, EffortLevel.MEDIUM),
        "gemini-2.5-flash": ThinkingSpec(ThinkingFamily.GEMINI, _GEMINI_EFFORTS, EffortLevel.MEDIUM),
    },
}


_THINKING_FALLBACK: dict[str, str] = {
    "aliyun_codingplan": "dashscope",
    "aliyun_codingplan_intl": "dashscope",
    "zhipu_cn_codingplan": "zhipu_cn",
    "zhipu_intl_codingplan": "zhipu_intl",
    "volcengine_cn_codingplan": "volcengine_cn",
}


def get_thinking_spec(provider_key: str, model: str) -> ThinkingSpec:
    """Return spec for (provider_key, model). Unknown combos → ``NONE`` spec."""
    spec = MODEL_THINKING.get(provider_key, {}).get(model)
    if spec is not None:
        return spec
    fallback_key = _THINKING_FALLBACK.get(provider_key)
    if fallback_key:
        return MODEL_THINKING.get(fallback_key, {}).get(model, _NONE_SPEC)
    return _NONE_SPEC


def normalize_effort(effort: str | None) -> str | None:
    """Lowercased, stripped effort string; empty returns None."""
    if effort is None:
        return None
    value = effort.strip().lower()
    return value or None


# Anthropic extended-thinking budget tokens per effort level.
# Used by ``AnthropicProvider._build_thinking_kwargs``.
ANTHROPIC_BUDGET: dict[str, int] = {
    "low": 1024,
    "medium": 4096,
    "high": 16384,
    "xhigh": 32000,
    "max": 64000,
}
