"""Pipeline event types for UI/ACP/telemetry consumption."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PipelineEventType(str, Enum):
    PIPELINE_STARTED = "pipeline_started"
    PIPELINE_COMPLETED = "pipeline_completed"
    PIPELINE_RESUMED = "pipeline_resumed"
    PIPELINE_ERROR = "pipeline_error"

    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"

    SUB_PIPELINE_STARTED = "sub_pipeline_started"
    SUB_PIPELINE_COMPLETED = "sub_pipeline_completed"
    SUB_STEP_STARTED = "sub_step_started"
    SUB_STEP_COMPLETED = "sub_step_completed"
    SUB_STEP_FAILED = "sub_step_failed"

    ROLLBACK_TRIGGERED = "rollback_triggered"
    FIELDS_MARKED_STALE = "fields_marked_stale"

    USER_INPUT_REQUIRED = "user_input_required"
    USER_INPUT_RECEIVED = "user_input_received"

    CONCLUSION_EXTRACTED = "conclusion_extracted"
    CONCLUSION_UPDATED = "conclusion_updated"

    INTERRUPTED = "interrupted"
    CANDIDATE_INTERRUPTED = "candidate_interrupted"


@dataclass
class PipelineEvent:
    type: PipelineEventType
    step_id: str | None
    timestamp: float
    data: dict
