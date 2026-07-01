from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderRequestPolicy:
    thinking_enabled: bool | None = None
    effort: str | None = None
    thinking_budget: int | None = None
    max_completion_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "thinking_enabled", bool_or_none(self.thinking_enabled))
        object.__setattr__(self, "effort", _stripped_or_none(self.effort))
        object.__setattr__(self, "thinking_budget", positive_int_or_none(self.thinking_budget))
        object.__setattr__(self, "max_completion_tokens", positive_int_or_none(self.max_completion_tokens))

    @property
    def has_values(self) -> bool:
        return (
            self.thinking_enabled is not None
            or self.effort is not None
            or self.thinking_budget is not None
            or self.max_completion_tokens is not None
        )


def _stripped_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    if stripped in {"1", "true", "t", "yes", "y", "on", "enable", "enabled"}:
        return True
    if stripped in {"0", "false", "f", "no", "n", "off", "disable", "disabled"}:
        return False
    return None


def positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 and value.is_integer() else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped.isdigit():
        return None
    parsed = int(stripped)
    return parsed if parsed > 0 else None
