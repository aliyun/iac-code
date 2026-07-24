"""Cleanup state helpers for the Web workbench."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from iac_code.web.pipeline import (
    PipelineRecoveryService,
    PipelineStateNotFoundError,
    PipelineStateRequestError,
    pipeline_state_from_query,
)

_RUNNING_SNAPSHOT_STATUSES = {"started", "in_progress"}
_BLOCKING_STATUSES = {"pending", "running", "failed", "unreadable"}


def cleanup_blocks_normal_chat(status: str | None) -> bool:
    """Return whether a cleanup status should block normal chat."""
    return _normalized_cleanup_status(status) in _BLOCKING_STATUSES


def cleanup_status_summary(
    cleanup: Mapping[str, Any] | None = None,
    *,
    status: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe cleanup summary from a recovered snapshot section."""
    cleanup_data = dict(cleanup or {})
    raw_status = _raw_cleanup_status(status if status is not None else cleanup_data.get("status"))
    normalized_status = _normalized_cleanup_status(raw_status)
    resources = cleanup_data.get("resources")
    if not isinstance(resources, list):
        resources = []
    history = cleanup_data.get("history")
    if not isinstance(history, list):
        history = []
    resource_count = cleanup_data.get("resourceCount")
    if not isinstance(resource_count, int):
        resource_count = len(resources)

    summary: dict[str, Any] = {
        "status": normalized_status,
        "rawStatus": raw_status,
        "blocksNormalChat": cleanup_blocks_normal_chat(normalized_status),
        "resourceCount": resource_count,
        "resources": resources,
        "history": history,
    }
    status_message = cleanup_data.get("statusMessage")
    if isinstance(status_message, str) and status_message:
        summary["statusMessage"] = status_message
    if message:
        summary["message"] = message
    return summary


async def session_cleanup_summary(
    session: Any,
    *,
    recovery_service: PipelineRecoveryService | None = None,
) -> dict[str, Any]:
    """Return the latest cleanup summary for a web session when recoverable."""
    context_id = getattr(session, "context_id", None)
    task_id = getattr(session, "task_id", None)
    base_identity = {
        "sessionId": getattr(session, "session_id", ""),
        "contextId": context_id,
        "taskId": task_id,
    }
    if not context_id and not task_id:
        return {**base_identity, **cleanup_status_summary(status="none")}

    try:
        state = await pipeline_state_from_query(
            {
                "contextId": context_id or "",
                "taskId": task_id or "",
            },
            recovery_service=recovery_service,
        )
    except PipelineStateNotFoundError:
        return {
            **base_identity,
            **cleanup_status_summary(status="unreadable", message="cleanup state is unreadable"),
        }
    except PipelineStateRequestError:
        return {
            "sessionId": base_identity["sessionId"],
            "contextId": None,
            "taskId": None,
            **cleanup_status_summary(status="unreadable", message="cleanup state is unreadable"),
        }

    snapshot = state.get("snapshot") if isinstance(state, dict) else None
    snapshot_data = snapshot if isinstance(snapshot, dict) else {}
    cleanup = snapshot_data.get("cleanup")
    summary = cleanup_status_summary(cleanup if isinstance(cleanup, dict) else None)
    return {
        **base_identity,
        "contextId": snapshot_data.get("contextId") or context_id,
        "taskId": snapshot_data.get("taskId") or task_id,
        "lastSequence": snapshot_data.get("lastSequence"),
        **summary,
    }


def _raw_cleanup_status(value: Any) -> str:
    if value is None:
        return "none"
    raw_status = str(value).strip().lower()
    return raw_status or "none"


def _normalized_cleanup_status(status: str | None) -> str:
    raw_status = _raw_cleanup_status(status)
    if raw_status in _RUNNING_SNAPSHOT_STATUSES:
        return "running"
    return raw_status
