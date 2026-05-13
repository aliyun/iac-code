"""ACP Server basic runtime metrics."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ACPMetrics:
    """ACP Server basic runtime metrics.

    Thread-safety note: this class is designed for single-threaded async use
    within one event loop.  All mutations happen from coroutine code on the
    same loop, so no locking is required.
    """

    start_time: float = field(default_factory=time.monotonic)
    total_sessions: int = 0
    active_sessions: int = 0
    total_prompts: int = 0
    total_errors: int = 0
    _prompt_durations: list[float] = field(default_factory=list)

    # -- mutators ------------------------------------------------------------

    def record_session_created(self) -> None:
        """Record that a new session was created."""
        self.total_sessions += 1
        self.active_sessions += 1

    def record_session_closed(self) -> None:
        """Record that a session was closed."""
        if self.active_sessions > 0:
            self.active_sessions -= 1

    def record_prompt(self, duration_ms: float) -> None:
        """Record a completed prompt with its duration in milliseconds."""
        self.total_prompts += 1
        self._prompt_durations.append(duration_ms)

    def record_error(self) -> None:
        """Record an error occurrence."""
        self.total_errors += 1

    # -- computed properties -------------------------------------------------

    @property
    def avg_prompt_duration_ms(self) -> float:
        """Average prompt duration in milliseconds, or 0.0 if no prompts."""
        if not self._prompt_durations:
            return 0.0
        return sum(self._prompt_durations) / len(self._prompt_durations)

    @property
    def uptime_seconds(self) -> float:
        """Seconds since the metrics tracker was created."""
        return time.monotonic() - self.start_time

    # -- snapshot ------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of all metrics."""
        return {
            "uptime_seconds": round(self.uptime_seconds, 2),
            "total_sessions": self.total_sessions,
            "active_sessions": self.active_sessions,
            "total_prompts": self.total_prompts,
            "total_errors": self.total_errors,
            "avg_prompt_duration_ms": round(self.avg_prompt_duration_ms, 2),
        }
