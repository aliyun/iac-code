"""Hook for the confirm_and_select step."""

from typing import Any

from iac_code.pipeline.engine.context import PipelineContext

_PREVIEW_REJECTED_KEY = "preview_rejected"


def candidate_preview_rejection(result: dict[str, Any]) -> str | None:
    """Return why a candidate is not selectable, or None when it stays selectable.

    ``cost_estimating`` treats PreviewStack as a soft gate: when the full deployment
    parameter set cannot be solved automatically it reports the gap in
    ``missing_deployment_parameters`` so the user can still fill it in through
    ``parameter_overrides``. Those candidates stay selectable. A preview that failed
    without any declared parameter gap means the template itself cannot be deployed,
    so the candidate must not reach the user's selection list.
    """
    cost = result.get("cost")
    if not isinstance(cost, dict):
        return "cost estimation did not report preview validation"

    preview_validation = cost.get("preview_validation")
    if not isinstance(preview_validation, dict):
        return "cost estimation did not report preview validation"
    if preview_validation.get("succeeded") is True:
        return None

    if cost.get("missing_deployment_parameters"):
        return None

    error = preview_validation.get("error")
    reason = error if isinstance(error, str) and error else "preview validation did not succeed"
    return f"preview validation failed: {reason}"


def reject_preview_failed_candidates(evaluated_candidates: list[Any]) -> list[Any]:
    """Mark candidates whose preview validation failed as failed candidates."""
    rejected: list[Any] = []
    for result in evaluated_candidates:
        if not isinstance(result, dict) or result.get("failed"):
            continue
        reason = candidate_preview_rejection(result)
        if reason is None:
            continue
        result["failed"] = True
        result[_PREVIEW_REJECTED_KEY] = True
        result["error"] = reason
        rejected.append(result)
    return rejected


def on_enter(ctx: PipelineContext) -> None:
    """Exclude preview-invalid candidates before the selection list is built.

    Candidates are updated in place so the evaluated_candidates version is not bumped;
    bumping it would mark selected_plan stale and drop a resumed candidate selection.
    """
    evaluated_candidates = ctx.get_conclusion("evaluated_candidates")
    if not isinstance(evaluated_candidates, list):
        return
    reject_preview_failed_candidates(evaluated_candidates)
