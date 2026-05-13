"""Token budget management for controlling LLM usage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TokenBudget:
    """Tracks token consumption against a total budget."""

    total: int | None = None  # None means unlimited
    used: int = field(default=0, init=False)

    @property
    def remaining(self) -> int | float:
        if self.total is None:
            return float("inf")
        return max(0, self.total - self.used)

    @property
    def is_exhausted(self) -> bool:
        if self.total is None:
            return False
        return self.used >= self.total

    @property
    def usage_percent(self) -> float:
        if self.total is None or self.total == 0:
            return 0.0
        return (self.used / self.total) * 100.0

    def consume(self, tokens: int) -> None:
        self.used += tokens

    @staticmethod
    def parse_shorthand(text: str) -> int:
        cleaned = text.strip().lstrip("+")
        match = re.match(r"^(\d+(?:\.\d+)?)\s*([kmKM])?$", cleaned)
        if not match:
            raise ValueError(f"Invalid token shorthand: '{text}'")
        value = float(match.group(1))
        suffix = (match.group(2) or "").lower()
        multipliers = {"k": 1_000, "m": 1_000_000}
        return int(value * multipliers.get(suffix, 1))

    @classmethod
    def unlimited(cls) -> TokenBudget:
        return cls(total=None)

    @classmethod
    def from_shorthand(cls, text: str) -> TokenBudget:
        return cls(total=cls.parse_shorthand(text))
