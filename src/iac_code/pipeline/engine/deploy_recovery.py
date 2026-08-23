"""Deployment recovery evidence derived from ros_deploy tool results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_CREATE_ACTIONS = ("create", "continue_create", "delete_and_create")


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class DeployAttemptEvidence:
    """A single failed ros_deploy creation attempt observed in the current step."""

    action: str
    stack_id: str
    status: str
    reason: str


def failed_deploy_attempts(tool_result_records: Any) -> list[DeployAttemptEvidence]:
    """Return the failed ros_deploy creation attempts recorded for the current step."""

    if not isinstance(tool_result_records, list):
        return []

    attempts: list[DeployAttemptEvidence] = []
    for record in tool_result_records:
        if not isinstance(record, dict) or record.get("tool_name") != "ros_deploy":
            continue
        tool_input = _dict(record.get("input"))
        action = tool_input.get("action")
        if action not in _CREATE_ACTIONS:
            continue
        result = _dict(record.get("result"))
        if not _attempt_failed(record, result):
            continue
        attempts.append(
            DeployAttemptEvidence(
                action=str(action),
                stack_id=_string(result.get("stack_id")),
                status=_string(result.get("status")),
                reason=_string(result.get("status_reason")) or _string(result.get("message")),
            )
        )
    return attempts


def _attempt_failed(record: dict[str, Any], result: dict[str, Any]) -> bool:
    if record.get("is_error"):
        return True
    return result.get("is_success") is False


def validate_deployment_recovery(
    recovery: Any,
    attempts: list[DeployAttemptEvidence],
) -> str | None:
    """Validate a conclusion's deployment_recovery against the observed attempts.

    Returns a model-actionable detail string, or ``None`` when the record is consistent.
    """

    if not attempts:
        return None

    if not isinstance(recovery, dict):
        return _missing_recovery_detail(attempts)

    retry_count = recovery.get("retry_count")
    if not isinstance(retry_count, int) or isinstance(retry_count, bool) or retry_count != len(attempts):
        return "retry_count must be {expected}, matching the {expected} failed ros_deploy attempts".format(
            expected=len(attempts)
        )

    failed_attempts = recovery.get("failed_attempts")
    if not isinstance(failed_attempts, list) or len(failed_attempts) != len(attempts):
        return "failed_attempts must contain exactly {expected} entries, one per failed ros_deploy attempt".format(
            expected=len(attempts)
        )

    for index, (raw_reported, observed) in enumerate(zip(failed_attempts, attempts)):
        if not isinstance(raw_reported, dict):
            return f"failed_attempts[{index}] must be an object"
        reported = _dict(raw_reported)
        if not _string(reported.get("reason")):
            return f"failed_attempts[{index}].reason must state why the attempt failed ({observed.status or 'failed'})"
        for field, observed_value in (
            ("action", observed.action),
            ("status", observed.status),
            ("stack_id", observed.stack_id),
        ):
            if not observed_value:
                continue
            if _string(reported.get(field)) != observed_value:
                return f"failed_attempts[{index}].{field} must be {observed_value}"

    if not _string(recovery.get("recovery_path")):
        return "recovery_path must describe the CREATE_FAILED to repair to success path"

    return None


def _missing_recovery_detail(attempts: list[DeployAttemptEvidence]) -> str:
    statuses = ", ".join(sorted({attempt.status for attempt in attempts if attempt.status})) or "failed"
    return (
        "deployment_recovery is required because ros_deploy failed {count} time(s) ({statuses}) "
        "before succeeding"
    ).format(count=len(attempts), statuses=statuses)
