"""Resolved Qwen thinking values with their configuration provenance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")
ThinkingSource = Literal["default", "provider", "model", "request"]


@dataclass(frozen=True)
class SourcedValue(Generic[T]):
    value: T | None = None
    source: ThinkingSource = "default"

    @property
    def priority(self) -> int:
        return {"default": 0, "provider": 1, "model": 2, "request": 3}[self.source]


@dataclass(frozen=True)
class ResolvedThinkingIntent:
    enabled: SourcedValue[bool] = SourcedValue()
    effort: SourcedValue[str] = SourcedValue()
    budget: SourcedValue[int] = SourcedValue()

    def dominant_concrete_field(self) -> str | None:
        """Return the closest explicit disable/effort/budget instruction.

        An explicit enable only permits thinking and does not erase a more
        distant concrete effort or budget. At one source level, disable wins,
        followed by effort and budget.
        """
        candidates: list[tuple[int, int, str]] = []
        if self.enabled.value is False:
            candidates.append((self.enabled.priority, 3, "disabled"))
        if self.effort.value is not None:
            candidates.append((self.effort.priority, 2, "effort"))
        if self.budget.value is not None:
            candidates.append((self.budget.priority, 1, "budget"))
        return max(candidates)[2] if candidates else None
