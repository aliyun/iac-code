"""Hook for the deploying step."""

import time
from dataclasses import asdict, dataclass
from typing import Any

from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource, ObservedResource
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.ui_contract import SelectedCandidate, parse_selected_candidate
from iac_code.types.stream_events import ResourceObservedEvent

_DEPLOYING_STEP_ID = "deploying"


@dataclass(frozen=True)
class CandidateResolution:
    candidate: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None = None


def _candidate_from_result(result: dict[str, Any]) -> dict[str, Any]:
    candidate = result.get("candidate")
    return candidate if isinstance(candidate, dict) else result


def resolve_selected_candidate(
    selected: SelectedCandidate,
    evaluated_candidates: list[dict[str, Any]],
) -> CandidateResolution:
    if selected.selected_candidate_index is not None:
        idx = selected.selected_candidate_index
        if idx < 0 or idx >= len(evaluated_candidates):
            return CandidateResolution(None, None, f"selected candidate index {idx} not found")
        result = evaluated_candidates[idx]
        candidate = _candidate_from_result(result)
        if selected.selected_candidate_name and candidate.get("name") != selected.selected_candidate_name:
            return CandidateResolution(
                None,
                result,
                (
                    "selected candidate name mismatch: "
                    f"{selected.selected_candidate_name!r} != {candidate.get('name')!r}"
                ),
            )
        if result.get("failed"):
            label = selected.selected_candidate_name or f"index {idx}"
            return CandidateResolution(None, result, f"selected candidate {label!r} failed")
        return CandidateResolution(candidate, result)

    matches = [
        result
        for result in evaluated_candidates
        if _candidate_from_result(result).get("name") == selected.selected_candidate_name
    ]
    successful = [result for result in matches if not result.get("failed")]
    if len(successful) == 1:
        result = successful[0]
        return CandidateResolution(_candidate_from_result(result), result)
    if not successful:
        return CandidateResolution(None, None, f"selected candidate {selected.selected_candidate_name!r} not found")
    return CandidateResolution(
        None,
        None,
        f"selected candidate {selected.selected_candidate_name!r} is ambiguous; candidate index is required",
    )


def normalize_selected_plan(
    selected_plan: dict[str, Any] | None,
    evaluated_candidates: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    plan = dict(selected_plan or {})
    selected = parse_selected_candidate(_selection_payload(plan))
    if selected is None:
        plan["selection_valid"] = False
        plan["selection_error"] = "selected candidate payload is missing or invalid"
        return plan

    candidates = evaluated_candidates or []
    resolution = resolve_selected_candidate(selected, candidates)
    plan["selection"] = asdict(selected)
    if resolution.error:
        plan["selection_valid"] = False
        plan["selection_error"] = resolution.error
        return plan

    plan["selection_valid"] = True
    plan["selected_candidate"] = resolution.candidate
    plan["selected_candidate_result"] = resolution.result
    return plan


def _selection_payload(plan: dict[str, Any]) -> Any:
    if "selected_candidate_index" in plan or "selected_candidate_name" in plan:
        return {
            "selected_candidate_name": plan.get("selected_candidate_name", ""),
            "selected_candidate_index": plan.get("selected_candidate_index"),
        }
    return plan.get("user_input")


def on_enter(ctx: PipelineContext) -> None:
    """Resolve the structured selected candidate before rendering the deploying prompt."""
    selected_plan = ctx.get_conclusion("selected_plan")
    evaluated_candidates = ctx.get_conclusion("evaluated_candidates")
    normalized = normalize_selected_plan(
        selected_plan if isinstance(selected_plan, dict) else {},
        evaluated_candidates if isinstance(evaluated_candidates, list) else [],
    )
    ctx.set_conclusion("selected_plan", normalized)


def on_resource_observed(
    ctx: PipelineContext,
    event: ResourceObservedEvent,
    *,
    ledger: CleanupLedger,
    step_id: str,
    attempt_id: str | None,
) -> ObservedResource | None:
    """Persist only ROS stacks created by the deploying step."""
    _ = ctx
    if step_id != _DEPLOYING_STEP_ID:
        return None
    if event.provider.lower() != "ros" or event.resource_type.lower() != "stack":
        return None
    if event.action != "CreateStack" or not event.resource_id:
        return None

    observed = ObservedResource(
        provider="ros",
        resource_type="stack",
        resource_id=event.resource_id,
        resource_name=event.resource_name,
        region_id=event.region_id,
        source_step_id=step_id,
        source_attempt_id=attempt_id,
        observed_action=event.action,
        observed_at=time.time(),
        metadata={
            "tool_name": event.tool_name,
            "tool_use_id": event.tool_use_id,
        },
    )
    return observed


def on_rollback_cleanup_required(
    ctx: PipelineContext,
    *,
    ledger: CleanupLedger,
    from_step: str,
    from_attempt_id: str | None,
    to_step: str,
    reason: str,
) -> list[CleanupResource]:
    """Mark deploying-created ROS stacks for cleanup when deploying rolls back."""
    _ = (ctx, to_step)
    if from_step != _DEPLOYING_STEP_ID or not from_attempt_id:
        return []
    resources = [
        CleanupResource.from_observed(resource, reason=reason)
        for resource in ledger.observed_resources()
        if resource.source_step_id == _DEPLOYING_STEP_ID
        and resource.source_attempt_id == from_attempt_id
        and resource.provider.lower() == "ros"
        and resource.resource_type.lower() == "stack"
        and resource.observed_action == "CreateStack"
    ]
    return resources
