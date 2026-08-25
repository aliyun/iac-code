"""Hook for the deploying step."""

import logging
import time
from dataclasses import dataclass
from typing import Any

from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource, ObservedResource
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.ui_contract import SelectedCandidate, parse_selected_candidate
from iac_code.types.stream_events import ResourceObservedEvent

_DEPLOYING_STEP_ID = "deploying"
logger = logging.getLogger(__name__)
_REDACTION_PLACEHOLDER_TOKENS = {"***", "[redacted]", "<redacted>"}


@dataclass(frozen=True)
class CandidateResolution:
    candidate: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None = None


def contains_redaction_placeholder(value: Any, *, _seen: set[int] | None = None) -> bool:
    """Return whether a deployment parameter tree still contains a redaction placeholder."""

    if isinstance(value, str):
        return value.strip().casefold() in _REDACTION_PLACEHOLDER_TOKENS
    if not isinstance(value, (dict, list, tuple, set, frozenset)):
        return False
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if isinstance(value, dict):
        return any(contains_redaction_placeholder(item, _seen=seen) for item in value.values())
    return any(contains_redaction_placeholder(item, _seen=seen) for item in value)


def _candidate_from_result(result: dict[str, Any]) -> dict[str, Any]:
    candidate = result.get("candidate")
    return candidate if isinstance(candidate, dict) else result


def resolve_selected_candidate(
    selected: SelectedCandidate,
    evaluated_candidates: list[dict[str, Any]],
    *,
    options: list[Any] | None = None,
) -> CandidateResolution:
    idx = selected.selected_evaluated_candidate_index
    if idx is None and selected.selected_candidate_index is not None:
        display_idx = selected.selected_candidate_index
        selectable_indices = _selectable_evaluated_indices(evaluated_candidates, options)
        if display_idx < 0 or display_idx >= len(selectable_indices):
            return CandidateResolution(None, None, f"selected candidate index {display_idx} not found")
        idx = selectable_indices[display_idx]
    if idx is not None:
        if idx < 0 or idx >= len(evaluated_candidates):
            return CandidateResolution(None, None, f"selected evaluated candidate index {idx} not found")
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


def _selectable_evaluated_indices(
    evaluated_candidates: list[dict[str, Any]],
    options: list[Any] | None,
) -> list[int]:
    if options:
        indices: list[int] = []
        for option in options:
            candidate_index = option.get("candidate_index") if isinstance(option, dict) else None
            if not isinstance(candidate_index, int) or isinstance(candidate_index, bool):
                return []
            indices.append(candidate_index)
        return indices
    return [index for index, result in enumerate(evaluated_candidates) if not result.get("failed")]


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
    raw_options = plan.get("options")
    options = raw_options if isinstance(raw_options, list) else None
    resolution = resolve_selected_candidate(selected, candidates, options=options)
    plan["selection"] = _selection_dict(selected)
    if resolution.error:
        plan["selection_valid"] = False
        plan["selection_error"] = resolution.error
        return plan

    plan["selection_valid"] = True
    plan["selected_candidate"] = resolution.candidate
    plan["selected_candidate_result"] = resolution.result
    template_url = _template_url_from_resolution(resolution.candidate, resolution.result)
    if template_url:
        plan["template_url"] = template_url
    plan["parameter_overrides"] = dict(selected.parameter_overrides)
    effective_parameters = _effective_deployment_parameters(resolution.result, selected.parameter_overrides)
    if effective_parameters:
        plan["effective_deployment_parameters"] = effective_parameters
    plan["preview_ready_for_create"] = _preview_ready_for_create(
        selected_candidate_result=resolution.result,
        template_url=template_url,
    )
    plan["cost_estimate_parameter_overridden"] = bool(selected.parameter_overrides)
    return plan


def _template_url_from_resolution(
    selected_candidate: dict[str, Any] | None,
    selected_candidate_result: dict[str, Any] | None,
) -> str:
    if isinstance(selected_candidate_result, dict):
        template = selected_candidate_result.get("template")
        if isinstance(template, dict):
            file_path = template.get("file_path")
            if isinstance(file_path, str) and file_path:
                return file_path

    if isinstance(selected_candidate, dict):
        output_path = selected_candidate.get("output_path")
        if isinstance(output_path, str) and output_path:
            return output_path
    return ""


def _selection_payload(plan: dict[str, Any]) -> Any:
    if any(
        field in plan
        for field in ("selected_candidate_index", "selected_evaluated_candidate_index", "selected_candidate_name")
    ):
        payload = {
            "selected_candidate_name": plan.get("selected_candidate_name", ""),
            "selected_candidate_index": plan.get("selected_candidate_index"),
            "selected_evaluated_candidate_index": plan.get("selected_evaluated_candidate_index"),
        }
        if "parameter_overrides" in plan:
            payload["parameter_overrides"] = plan.get("parameter_overrides")
        elif "parameters" in plan:
            payload["parameters"] = plan.get("parameters")
        return payload
    return plan.get("user_input")


def _selection_dict(selected: SelectedCandidate) -> dict[str, Any]:
    data: dict[str, Any] = {
        "selected_candidate_name": selected.selected_candidate_name,
        "selected_candidate_index": selected.selected_candidate_index,
    }
    if selected.selected_evaluated_candidate_index is not None:
        data["selected_evaluated_candidate_index"] = selected.selected_evaluated_candidate_index
    if selected.parameter_overrides:
        data["parameter_overrides"] = dict(selected.parameter_overrides)
    return data


def _effective_deployment_parameters(
    selected_candidate_result: dict[str, Any] | None,
    parameter_overrides: dict[str, Any],
) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if isinstance(selected_candidate_result, dict):
        cost = selected_candidate_result.get("cost")
        if isinstance(cost, dict):
            deployment_parameters = cost.get("deployment_parameters")
            if isinstance(deployment_parameters, dict):
                parameters.update(deployment_parameters)
    parameters.update(parameter_overrides)
    return parameters


def _preview_ready_for_create(
    *,
    selected_candidate_result: dict[str, Any] | None,
    template_url: str,
) -> bool:
    if not template_url or not isinstance(selected_candidate_result, dict):
        return False

    cost = selected_candidate_result.get("cost")
    if not isinstance(cost, dict):
        return False
    if cost.get("missing_deployment_parameters"):
        return False

    preview_validation = cost.get("preview_validation")
    if not isinstance(preview_validation, dict):
        return False
    if preview_validation.get("succeeded") is not True:
        return False
    if preview_validation.get("template_url") != template_url:
        return False

    preview_parameters = preview_validation.get("parameters")
    if not isinstance(preview_parameters, dict):
        return False
    return True


def on_enter(ctx: PipelineContext) -> None:
    """Resolve the structured selected candidate before rendering the deploying prompt."""
    selected_plan = ctx.get_conclusion("selected_plan")
    evaluated_candidates = ctx.get_conclusion("evaluated_candidates")
    normalized = normalize_selected_plan(
        selected_plan if isinstance(selected_plan, dict) else {},
        evaluated_candidates if isinstance(evaluated_candidates, list) else [],
    )
    ctx.set_conclusion("selected_plan", normalized)


def on_exit(ctx: PipelineContext, conclusion: dict[str, Any]) -> None:
    """Make sure a failed deployment conclusion always carries a ROS failure reason."""
    _ = ctx
    if not isinstance(conclusion, dict) or conclusion.get("status") != "failed":
        return
    status_reason = conclusion.get("status_reason")
    if isinstance(status_reason, str) and status_reason.strip():
        return
    error = conclusion.get("error")
    if isinstance(error, str) and error.strip():
        conclusion["status_reason"] = error.strip()


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
    if from_step != _DEPLOYING_STEP_ID:
        return []
    if not from_attempt_id:
        logger.warning("Skipping deploying cleanup hook because from_attempt_id is missing")
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
