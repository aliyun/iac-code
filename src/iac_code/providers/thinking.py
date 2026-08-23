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
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    NONE = "none"
    AUTO = "auto"


EFFORT_ORDER: list[EffortLevel] = [
    EffortLevel.NONE,
    EffortLevel.MINIMAL,
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
    EffortLevel.AUTO,
]


EFFORT_SYMBOLS: dict[EffortLevel, str] = {
    EffortLevel.NONE: "◇",
    EffortLevel.MINIMAL: "◈",
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
    ANTHROPIC_ADAPTIVE = "anthropic_adaptive"
    OPENAI = "openai"  # reasoning_effort
    DASHSCOPE = "dashscope"  # extra_body.enable_thinking [+ thinking_budget]
    GEMINI = "gemini"
    KIMI = "kimi"  # K3 uses reasoning_effort; K2.7-code is always-on; older K2 uses extra_body.thinking
    MINIMAX = "minimax"  # Anthropic-compatible thinking.type=adaptive for MiniMax-M3
    ZHIPU = "zhipu"  # extra_body.thinking.type=enabled


@dataclass(frozen=True)
class ThinkingSpec:
    family: ThinkingFamily
    allowed_efforts: tuple[EffortLevel, ...] = ()
    default_effort: EffortLevel | None = None
    default_thinking_budget: int | None = None
    supports_thinking_budget: bool = False
    use_max_completion_tokens: bool = False
    uses_reasoning_effort_param: bool = False
    adaptive_always_on: bool = False
    supports_disable: bool = True
    thinking_enabled_by_default: bool = False
    disable_forbidden_efforts: tuple[EffortLevel, ...] = ()

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
    EffortLevel.NONE,
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
)

_OPENAI_GPT56_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.NONE,
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
)

_OPENAI_CODEX_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
)

_OPENAI_O_SERIES_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
)

_GEMINI_3_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.MINIMAL,
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
)

# Gemini 3.7 Flash documents thinking levels low/medium/high and returns an
# error for ``minimal``.
_GEMINI_37_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
)

_GEMINI_25_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.MINIMAL,
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
)

_ANTHROPIC_MODERN_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
    EffortLevel.AUTO,
)

_ANTHROPIC_46_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.MAX,
    EffortLevel.AUTO,
)

# The official DeepSeek endpoint exposes low/high/max — MEDIUM and XHIGH are
# intentionally skipped because the API rejects them.
_DEEPSEEK_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.HIGH,
    EffortLevel.MAX,
)

# DashScope accepts the full effort vocabulary for its hosted DeepSeek V4
# models. Low/medium currently map to high and xhigh maps to max server-side,
# but keeping the accepted wire values lets callers migrate without a 400.
_DASHSCOPE_DEEPSEEK_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
)

_GLM_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.NONE,
    EffortLevel.MINIMAL,
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
)

_GLM51_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.NONE,
    EffortLevel.MINIMAL,
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
)

_KIMI_K3_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.HIGH,
    EffortLevel.MAX,
)

_ZHIPU_GLM52_EFFORTS: tuple[EffortLevel, ...] = (EffortLevel.HIGH, EffortLevel.MAX)

# GLM-5.3 always thinks; only low/high/max are documented and
# thinking.type=disabled is rejected.
_ZHIPU_GLM53_EFFORTS: tuple[EffortLevel, ...] = (
    EffortLevel.LOW,
    EffortLevel.HIGH,
    EffortLevel.MAX,
)


_NONE_SPEC = ThinkingSpec(family=ThinkingFamily.NONE)

# qwen3.7-max thinks without a server-side length bound unless a budget is sent,
# which lets a single turn spend minutes inside the thinking phase. Cap it so the
# reasoning length is bounded at the source; an explicitly configured
# ``thinkingBudget`` still wins (see ``_effective_thinking_budget_for_spec``).
_DASHSCOPE_QWEN37_MAX_SPEC = ThinkingSpec(
    ThinkingFamily.DASHSCOPE,
    default_thinking_budget=16384,
    supports_thinking_budget=True,
)

_DASHSCOPE_KIMI_K27_CODE_SPEC = ThinkingSpec(
    ThinkingFamily.DASHSCOPE,
    default_thinking_budget=8192,
    supports_thinking_budget=True,
    use_max_completion_tokens=True,
)

_DASHSCOPE_GLM52_SPEC = ThinkingSpec(
    ThinkingFamily.DASHSCOPE,
    _GLM_EFFORTS,
    use_max_completion_tokens=True,
    uses_reasoning_effort_param=True,
)

_DASHSCOPE_GLM51_SPEC = ThinkingSpec(
    ThinkingFamily.DASHSCOPE,
    _GLM51_EFFORTS,
    uses_reasoning_effort_param=True,
)

_DASHSCOPE_QWEN38_SPEC = ThinkingSpec(
    ThinkingFamily.DASHSCOPE,
    (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.XHIGH),
    EffortLevel.XHIGH,
    uses_reasoning_effort_param=True,
    thinking_enabled_by_default=True,
)

_DASHSCOPE_QWEN38_PREVIEW_SPEC = ThinkingSpec(
    ThinkingFamily.DASHSCOPE,
    (EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.XHIGH),
    EffortLevel.XHIGH,
    uses_reasoning_effort_param=True,
    supports_disable=False,
    thinking_enabled_by_default=True,
)

_DASHSCOPE_KIMI_K3_SPEC = ThinkingSpec(
    ThinkingFamily.DASHSCOPE,
    (EffortLevel.MAX,),
    EffortLevel.MAX,
    uses_reasoning_effort_param=True,
    supports_disable=False,
)

_ANTHROPIC_ADAPTIVE_SPEC = ThinkingSpec(
    ThinkingFamily.ANTHROPIC_ADAPTIVE,
    _ANTHROPIC_MODERN_EFFORTS,
    EffortLevel.HIGH,
)

_ANTHROPIC_OPUS5_SPEC = ThinkingSpec(
    ThinkingFamily.ANTHROPIC_ADAPTIVE,
    _ANTHROPIC_MODERN_EFFORTS,
    EffortLevel.HIGH,
    thinking_enabled_by_default=True,
    disable_forbidden_efforts=(EffortLevel.XHIGH, EffortLevel.MAX),
)

_ANTHROPIC_46_ADAPTIVE_SPEC = ThinkingSpec(
    ThinkingFamily.ANTHROPIC_ADAPTIVE,
    _ANTHROPIC_46_EFFORTS,
    EffortLevel.HIGH,
    supports_thinking_budget=True,
)

_ANTHROPIC_ADAPTIVE_ALWAYS_ON_SPEC = ThinkingSpec(
    ThinkingFamily.ANTHROPIC_ADAPTIVE,
    _ANTHROPIC_MODERN_EFFORTS,
    EffortLevel.HIGH,
    adaptive_always_on=True,
    supports_disable=False,
)

_OPENAI_GPT56_SPEC = ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_GPT56_EFFORTS, EffortLevel.MEDIUM)
_OPENAI_GPT55_SPEC = ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.MEDIUM)
_OPENAI_REASONING_SPEC = ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_EFFORTS, EffortLevel.HIGH)
_OPENAI_CODEX_SPEC = ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_CODEX_EFFORTS, EffortLevel.MEDIUM)
_OPENAI_O_SERIES_SPEC = ThinkingSpec(ThinkingFamily.OPENAI, _OPENAI_O_SERIES_EFFORTS, EffortLevel.HIGH)
_DEEPSEEK_SPEC = ThinkingSpec(
    ThinkingFamily.OPENAI,
    _DEEPSEEK_EFFORTS,
    EffortLevel.HIGH,
    thinking_enabled_by_default=True,
)
_KIMI_K3_SPEC = ThinkingSpec(ThinkingFamily.KIMI, _KIMI_K3_EFFORTS, EffortLevel.MAX)
_ZHIPU_GLM52_SPEC = ThinkingSpec(
    ThinkingFamily.ZHIPU,
    _ZHIPU_GLM52_EFFORTS,
    EffortLevel.MAX,
    uses_reasoning_effort_param=True,
)
_ZHIPU_GLM53_SPEC = ThinkingSpec(
    ThinkingFamily.ZHIPU,
    _ZHIPU_GLM53_EFFORTS,
    EffortLevel.MAX,
    uses_reasoning_effort_param=True,
    supports_disable=False,
    thinking_enabled_by_default=True,
)


MODEL_THINKING: dict[str, dict[str, ThinkingSpec]] = {
    "anthropic": {
        "claude-fable-5": _ANTHROPIC_ADAPTIVE_ALWAYS_ON_SPEC,
        "claude-opus-5": _ANTHROPIC_OPUS5_SPEC,
        "claude-sonnet-5": _ANTHROPIC_ADAPTIVE_SPEC,
        "claude-opus-4-8": _ANTHROPIC_ADAPTIVE_SPEC,
        "claude-opus-4-7": _ANTHROPIC_ADAPTIVE_SPEC,
        "claude-opus-4-6": _ANTHROPIC_46_ADAPTIVE_SPEC,
        "claude-sonnet-4-6": _ANTHROPIC_46_ADAPTIVE_SPEC,
        "claude-sonnet-4-6-1m": _ANTHROPIC_46_ADAPTIVE_SPEC,
        "claude-haiku-4-5-20251001": ThinkingSpec(
            ThinkingFamily.ANTHROPIC,
            _ANTHROPIC_MODERN_EFFORTS,
            EffortLevel.HIGH,
            supports_thinking_budget=True,
        ),
    },
    "openai": {
        "gpt-5.6": _OPENAI_GPT56_SPEC,
        "gpt-5.6-sol": _OPENAI_GPT56_SPEC,
        "gpt-5.6-terra": _OPENAI_GPT56_SPEC,
        "gpt-5.6-luna": _OPENAI_GPT56_SPEC,
        "gpt-5.5": _OPENAI_GPT55_SPEC,
        "gpt-5.4": _OPENAI_REASONING_SPEC,
        "gpt-5.4-mini": _OPENAI_REASONING_SPEC,
        "gpt-5.4-nano": _OPENAI_REASONING_SPEC,
        "gpt-5.3-codex": _OPENAI_CODEX_SPEC,
        "gpt-5.2-codex": _OPENAI_CODEX_SPEC,
        "gpt-5.2": _OPENAI_REASONING_SPEC,
        "o3": _OPENAI_O_SERIES_SPEC,
        "o4-mini": _OPENAI_O_SERIES_SPEC,
    },
    "deepseek": {
        "deepseek-v4-pro": _DEEPSEEK_SPEC,
        "deepseek-v4-flash": _DEEPSEEK_SPEC,
    },
    "dashscope": {
        "qwen3.8-max": _DASHSCOPE_QWEN38_SPEC,
        "qwen3.7-max": _DASHSCOPE_QWEN37_MAX_SPEC,
        "qwen3.7-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.7-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.6-max-preview": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.6-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.6-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.5-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.5-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwq-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "kimi-k2.6": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "kimi-k2.5": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "kimi-k2.7-code": _DASHSCOPE_KIMI_K27_CODE_SPEC,
        "kimi/kimi-k3": _DASHSCOPE_KIMI_K3_SPEC,
        "glm-5.2-fast-preview": _DASHSCOPE_GLM52_SPEC,
        "glm-5.2": _DASHSCOPE_GLM52_SPEC,
        "glm-5.1": _DASHSCOPE_GLM51_SPEC,
        "MiniMax-M2.5": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "MiniMax/MiniMax-M3": ThinkingSpec(ThinkingFamily.MINIMAX),
        "deepseek-v4-pro": ThinkingSpec(
            ThinkingFamily.DASHSCOPE,
            _DASHSCOPE_DEEPSEEK_EFFORTS,
            EffortLevel.HIGH,
            uses_reasoning_effort_param=True,
        ),
        "deepseek-v4-pro-0813": ThinkingSpec(
            ThinkingFamily.DASHSCOPE,
            _DASHSCOPE_DEEPSEEK_EFFORTS,
            EffortLevel.HIGH,
            uses_reasoning_effort_param=True,
        ),
        "deepseek-v4-flash-0731": ThinkingSpec(
            ThinkingFamily.DASHSCOPE,
            _DASHSCOPE_DEEPSEEK_EFFORTS,
            EffortLevel.HIGH,
            uses_reasoning_effort_param=True,
        ),
        "deepseek-v4-flash": ThinkingSpec(
            ThinkingFamily.DASHSCOPE,
            _DASHSCOPE_DEEPSEEK_EFFORTS,
            EffortLevel.HIGH,
            uses_reasoning_effort_param=True,
        ),
    },
    "dashscope_token_plan": {
        "qwen3.8-max": _DASHSCOPE_QWEN38_SPEC,
        "qwen3.8-max-preview": _DASHSCOPE_QWEN38_PREVIEW_SPEC,
        "qwen3.7-max": _DASHSCOPE_QWEN37_MAX_SPEC,
        "qwen3.7-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.6-plus": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "qwen3.6-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "deepseek-v4-pro": ThinkingSpec(ThinkingFamily.DASHSCOPE, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
        "deepseek-v4-pro-0813": ThinkingSpec(ThinkingFamily.DASHSCOPE, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
        "deepseek-v4-flash-0731": ThinkingSpec(ThinkingFamily.DASHSCOPE, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
        "deepseek-v4-flash": ThinkingSpec(ThinkingFamily.DASHSCOPE, _DEEPSEEK_EFFORTS, EffortLevel.HIGH),
        "deepseek-v3.2": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "glm-5.1": _DASHSCOPE_GLM51_SPEC,
        "glm-5": _DASHSCOPE_GLM51_SPEC,
        "MiniMax-M2.5": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "kimi-k2.5": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "kimi-k2.6": ThinkingSpec(ThinkingFamily.DASHSCOPE),
        "kimi-k2.7-code": _DASHSCOPE_KIMI_K27_CODE_SPEC,
        "glm-5.2": _DASHSCOPE_GLM52_SPEC,
    },
    "gemini": {
        "gemini-3.7-flash": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_37_EFFORTS,
            EffortLevel.MEDIUM,
            supports_disable=False,
        ),
        "gemini-3.6-flash": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_3_EFFORTS,
            EffortLevel.MEDIUM,
            supports_disable=False,
        ),
        "gemini-3.5-flash": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_3_EFFORTS,
            EffortLevel.MEDIUM,
            supports_disable=False,
        ),
        "gemini-3.5-flash-lite": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_3_EFFORTS,
            EffortLevel.MINIMAL,
            supports_disable=False,
        ),
        "gemini-3.1-pro-preview": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_25_EFFORTS,
            EffortLevel.HIGH,
            supports_disable=False,
        ),
        "gemini-3.1-pro-preview-customtools": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_25_EFFORTS,
            EffortLevel.HIGH,
            supports_disable=False,
        ),
        "gemini-3-flash-preview": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_3_EFFORTS,
            EffortLevel.HIGH,
            supports_disable=False,
        ),
        "gemini-3.1-flash-lite": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_3_EFFORTS,
            EffortLevel.MINIMAL,
            supports_disable=False,
        ),
        "gemini-2.5-pro": ThinkingSpec(
            ThinkingFamily.GEMINI,
            _GEMINI_25_EFFORTS,
            EffortLevel.HIGH,
            supports_disable=False,
        ),
        "gemini-2.5-flash": ThinkingSpec(
            ThinkingFamily.GEMINI,
            (EffortLevel.NONE, *_GEMINI_25_EFFORTS),
            EffortLevel.HIGH,
        ),
        "gemini-2.5-flash-lite": ThinkingSpec(
            ThinkingFamily.GEMINI,
            (EffortLevel.NONE, *_GEMINI_25_EFFORTS),
            EffortLevel.NONE,
        ),
    },
    "kimi_cn": {
        "kimi-k3": _KIMI_K3_SPEC,
        "kimi-k2.6": ThinkingSpec(ThinkingFamily.KIMI),
        "kimi-k2.5": ThinkingSpec(ThinkingFamily.KIMI),
        "kimi-k2.7-code": ThinkingSpec(ThinkingFamily.KIMI),
        "kimi-k2.7-code-highspeed": ThinkingSpec(ThinkingFamily.KIMI),
    },
    "minimax_cn": {
        "MiniMax-M3": ThinkingSpec(ThinkingFamily.MINIMAX),
        "MiniMax-M2.7": ThinkingSpec(ThinkingFamily.MINIMAX),
        "MiniMax-M2.7-highspeed": ThinkingSpec(ThinkingFamily.MINIMAX),
        "MiniMax-M2.5": ThinkingSpec(ThinkingFamily.MINIMAX),
        "MiniMax-M2.5-highspeed": ThinkingSpec(ThinkingFamily.MINIMAX),
    },
    "zhipu_cn": {
        "glm-5.2": _ZHIPU_GLM52_SPEC,
        "glm-5.1": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-5": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-5-turbo": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.7": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.7-flash": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.7-flashx": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.6": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.5": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.5-air": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.5-x": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.5-airx": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.5-flash": ThinkingSpec(ThinkingFamily.ZHIPU),
    },
    "zhipu_cn_codingplan": {
        "glm-5.3": _ZHIPU_GLM53_SPEC,
        "glm-5.2": _ZHIPU_GLM52_SPEC,
        "glm-5-turbo": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.7": ThinkingSpec(ThinkingFamily.ZHIPU),
        "glm-4.5-air": ThinkingSpec(ThinkingFamily.ZHIPU),
    },
}


_THINKING_FALLBACK: dict[str, str] = {
    "azure_openai": "openai",
    "kimi_intl": "kimi_cn",
    "minimax_intl": "minimax_cn",
    "zhipu_intl": "zhipu_cn",
    "aliyun_codingplan": "dashscope",
    "aliyun_codingplan_intl": "dashscope",
    "zhipu_cn_codingplan": "zhipu_cn",
    "zhipu_intl_codingplan": "zhipu_cn_codingplan",
    "volcengine_cn_codingplan": "volcengine_cn",
}


def get_thinking_spec(provider_key: str, model: str) -> ThinkingSpec:
    """Return spec for (provider_key, model). Unknown combos → ``NONE`` spec."""
    visited: set[str] = set()
    current_key: str | None = provider_key
    while current_key and current_key not in visited:
        visited.add(current_key)
        spec = MODEL_THINKING.get(current_key, {}).get(model)
        if spec is not None:
            return spec
        current_key = _THINKING_FALLBACK.get(current_key)
    return _NONE_SPEC


def normalize_effort(effort: str | None) -> str | None:
    """Lowercased, stripped effort string; empty returns None."""
    if effort is None:
        return None
    value = effort.strip().lower()
    return value or None


# Effort tokens that explicitly disable DashScope thinking; mirrors
# ``dashscope_provider._DISABLE_THINKING_EFFORTS``.
_THINKING_DISABLE_EFFORTS: frozenset[str] = frozenset({"none", "off", "disable", "disabled", "false", "0"})

# Families whose providers emit an explicit "thinking on" directive by default
# when the user has NOT configured ``thinkingEnabled`` (DashScope
# ``enable_thinking=True``, Kimi/Zhipu ``thinking.type=enabled``). For these an
# unset config still means the next turn thinks. Most reasoning-effort models
# (OpenAI/Anthropic/Gemini) instead emit nothing when unset and resolve to off;
# models with a documented server-side thinking default opt in explicitly.
_DEFAULT_ON_FAMILIES: frozenset[ThinkingFamily] = frozenset(
    {ThinkingFamily.DASHSCOPE, ThinkingFamily.KIMI, ThinkingFamily.ZHIPU}
)


def resolve_thinking_active(
    provider_key: str | None,
    model: str | None,
    thinking_enabled: bool | None,
    effort: str | None = None,
) -> bool:
    """Whether the next turn would run with thinking, mirroring the provider
    ``_build_thinking_kwargs`` decisions at the family level.

    ``thinking_enabled`` is the already-resolved config/override (``None`` = not
    set). This powers the web composer's thinking toggle so its initial state
    matches what the turn actually does — e.g. a DashScope/Kimi/Zhipu session
    with no override still thinks, so the toggle shows on.
    """
    spec = get_thinking_spec(provider_key or "", model or "")
    if spec.family is ThinkingFamily.NONE:
        return False
    normalized_effort = normalize_effort(effort)
    if thinking_enabled is False:
        if not spec.supports_disable:
            return True
        forbidden = {item.value for item in spec.disable_forbidden_efforts}
        return normalized_effort in forbidden
    # DashScope disable-effort tokens force thinking off even when configured on.
    if spec.family is ThinkingFamily.DASHSCOPE and normalized_effort in _THINKING_DISABLE_EFFORTS:
        return False
    if thinking_enabled is True:
        return True
    # thinking_enabled is None (not configured): use the family default.
    return spec.thinking_enabled_by_default or spec.adaptive_always_on or spec.family in _DEFAULT_ON_FAMILIES


# Anthropic extended-thinking budget tokens per effort level.
# Used by ``AnthropicProvider._build_thinking_kwargs``.
ANTHROPIC_BUDGET: dict[str, int] = {
    "low": 1024,
    "medium": 4096,
    "high": 16384,
    "xhigh": 32000,
    "max": 64000,
}
