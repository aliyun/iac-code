"""Pipeline event types for UI/ACP/telemetry consumption."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from iac_code.utils.public_errors import sanitize_public_text


class PipelineEventType(str, Enum):
    PIPELINE_STARTED = "pipeline_started"
    PIPELINE_COMPLETED = "pipeline_completed"
    PIPELINE_RESUMED = "pipeline_resumed"
    BACKUP_BLOCKED = "backup_blocked"
    PIPELINE_ERROR = "pipeline_error"
    PIPELINE_WARNING = "pipeline_warning"

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


def backup_blocked_event(step_id: str | None, reason: object, error: object) -> PipelineEvent:
    reason_text = getattr(reason, "value", reason)
    return PipelineEvent(
        type=PipelineEventType.BACKUP_BLOCKED,
        step_id=step_id,
        timestamp=time.time(),
        data={
            "reason": sanitize_public_text(str(reason_text)),
            "error": sanitize_public_text(str(error)),
            "recoverable": True,
        },
    )
