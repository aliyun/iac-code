"""Model-family predicates used by provider selection and adapters."""

from __future__ import annotations


def normalized_model_name(model: str) -> str:
    """Return the provider-independent leaf model identifier."""
    return model.strip().lower().rsplit("/", 1)[-1]


def is_qwen_model(model: str) -> bool:
    """Whether *model* uses the Qwen wire behaviour implemented here.

    ``qwq`` intentionally remains outside this family until an observed wire
    fixture proves that it needs the Qwen-specific parser and prompt adapter.
    """
    normalized = normalized_model_name(model)
    return normalized.startswith("qwen") or normalized == "coder-model"
