"""Validate a chosen plan while the confirmation agent can still correct it."""

from typing import Any

from iac_code.pipeline.engine.complete_step_tool import CompletionEnrichmentError
from iac_code.pipeline.selling.hooks.deploying import normalize_selected_plan


def enrich_completion_input(
    *, tool_input: dict[str, Any], context_snapshot: dict[str, Any],
    completion_guard_state: dict[str, Any] | None = None, **_ignored: Any,
) -> dict[str, Any]:
    conclusion = tool_input.get("conclusion")
    if not isinstance(conclusion, dict) or tool_input.get("rollback_request"):
        return tool_input
    choosing = bool(conclusion.get("user_input")) or bool(conclusion.get("selected_candidate_name")) or any(
        conclusion.get(key) is not None for key in (
            "selected_candidate_index", "selected_evaluated_candidate_index",
        )
    )
    if not choosing and not (completion_guard_state or {}).get("resuming_candidate_selection"):
        return tool_input  # Initial presentation has no user selection yet.
    evaluated = context_snapshot.get("evaluated_candidates")
    normalized = normalize_selected_plan(conclusion, evaluated if isinstance(evaluated, list) else [])
    if not normalized["selection_valid"]:
        # Reuse the exact deployment resolver. No replacement candidate, name,
        # index or parameter is inferred when the submitted choice is invalid.
        selection_error = normalized["selection_error"]
        raise CompletionEnrichmentError(selection_error)
    return tool_input
