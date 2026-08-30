"""Core data types for the pipeline engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


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
    completion_input_schema: dict | None = None
    completion_enricher: Callable[..., dict[str, Any]] | None = None
    rollback_targets: list[str] = field(default_factory=list)
    max_conclusion_retries: int = 2
    rollback_count: int = 0
    max_rollbacks: int = 5
    compact_completion_schema: bool = False
    compact_completion_errors: bool = False
    completion_validation_error_limit: int = 1
    conclusion_merge_context_field: str | None = None
    conclusion_merge_statuses: tuple[str, ...] = ()
    hydrate_selected_candidate: bool = False
    authoritative_candidate_context_field: str | None = None
    authoritative_candidate_targets: tuple[str, ...] = ()
    completion_record_contract: str | None = None
    hard_constraint_evidence_contract: str | None = None
    completion_context_paths: tuple[str, ...] = ()
    #: Opt-in: an explicit structured confirmation may carry parameter overrides that differ
    #: from the last quote input, and is still resolved deterministically in one shot.
    confirmation_accepts_parameter_overrides: bool = False


@dataclass
class StepResult:
    """Outcome of executing a pipeline step."""

    step_id: str
    status: StepStatus
    conclusion: dict | None = None
    rollback_request: tuple[str, str] | None = None
    error: str | None = None
