"""Core data types for the pipeline engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    STALE = "stale"
    FAILED = "failed"


@dataclass
class StepConfig:
    """Static configuration for a pipeline step."""

    step_id: str
    conclusion_field: str
    forward: str | None
    auto_advance: bool = True
    complete_step_terminal: bool = True
    max_agent_turns: int = 50
    conclusion_schema: dict | None = None
    rollback_targets: list[str] = field(default_factory=list)
    max_conclusion_retries: int = 2
    rollback_count: int = 0
    max_rollbacks: int = 5
    rollback_exhausted_target: str | None = None


@dataclass
class StepResult:
    """Outcome of executing a pipeline step."""

    step_id: str
    status: StepStatus
    conclusion: dict | None = None
    rollback_request: tuple[str, str] | None = None
    error: str | None = None
    # Set when the rollback budget is exhausted and the request was redirected to
    # ``rollback_exhausted_target`` so the user can confirm again instead of the
    # pipeline terminating as failed.
    rollback_exhausted: bool = False
