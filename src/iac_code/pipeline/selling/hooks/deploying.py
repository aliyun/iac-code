"""Hook for the deploying step."""

from dataclasses import asdict, dataclass
from typing import Any

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.ui_contract import SelectedCandidate, parse_selected_candidate


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
