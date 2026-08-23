"""Pure helpers for finite pipeline completion handoff."""

from __future__ import annotations

import json
from typing import Literal

TerminalOutcome = Literal["completed", "early_exit", "failed", "canceled"]


def terminal_outcome_from_completed_event(data: dict) -> TerminalOutcome:
    """Map a pipeline-completed event payload to a stable terminal outcome."""
    if data.get("failed"):
        return "failed"
    if data.get("canceled"):
        return "canceled"
    if data.get("early_exit"):
        return "early_exit"
    return "completed"


def build_handoff_summary(
    pipeline_name: str,
    outcome: TerminalOutcome,
    context_snapshot: dict,
    include_fields: list[str],
    candidate_progress: list[dict] | None = None,
) -> str:
    """Build deterministic text for continuing in normal chat after a pipeline."""
    included = {
        field_name: context_snapshot[field_name] for field_name in include_fields if field_name in context_snapshot
    }
    missing = [field_name for field_name in include_fields if field_name not in context_snapshot]

    lines = [
        "[Pipeline Handoff Context]",
        "This is injected context for the assistant, not a user request.",
        f"Pipeline: {pipeline_name}",
        f"Outcome: {outcome}",
        "",
        "Included context:",
        json.dumps(included, ensure_ascii=False, indent=2),
    ]
    if missing:
        lines.extend(["", "Missing context fields:"])
        lines.extend(f"- {field_name}" for field_name in missing)
    if candidate_progress:
        lines.extend(
            [
                "",
                "Candidate sub-step progress:",
                json.dumps(candidate_progress, ensure_ascii=False, indent=2),
                (
                    "Sub-steps listed under completed_sub_steps already produced their conclusions; "
                    "reuse them instead of re-evaluating the candidate from scratch."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "Safety requirements for normal chat:",
            (
                "- Before performing any operation that releases, deletes, or otherwise destroys a resource, "
                "obtain a fresh, explicit confirmation from the user in normal chat. Any confirmation given "
                "during the pipeline does not count."
            ),
            ("- Exception: pipeline-managed automatic cleanup may proceed without this additional confirmation."),
            "",
            "Use this context when answering follow-up questions after the pipeline handoff.",
        ]
    )
    return "\n".join(lines)


def candidate_progress_from_execution(
    execution: dict | None,
    sub_step_ids: list[str],
) -> list[dict]:
    """Derive machine-readable candidate sub-step progress from sidecar execution state.

    Interrupting ``evaluate_candidates`` leaves per-candidate progress in the
    sidecar's ``execution.candidates`` map, which the pipeline context alone does
    not expose. Surfacing it in the handoff lets normal chat resume from the
    already-completed sub-steps instead of re-evaluating every candidate.
    """
    if not isinstance(execution, dict) or execution.get("kind") != "parallel_sub_pipeline":
        return []
    candidates = execution.get("candidates")
    if not isinstance(candidates, dict):
        return []

    progress: list[dict] = []
    for raw_index, state in candidates.items():
        try:
            candidate_index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        step_conclusions = state.get("step_conclusions")
        if not isinstance(step_conclusions, dict):
            step_conclusions = {}
        completed = [step_id for step_id in sub_step_ids if step_id in step_conclusions]
        pending = [step_id for step_id in sub_step_ids if step_id not in completed]
        candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else {}
        cached = state.get("tool_result_cache")
        progress.append(
            {
                "candidate_index": candidate_index,
                "candidate_name": candidate.get("name", ""),
                "status": state.get("status", "unknown"),
                "current_sub_step": state.get("current_sub_step", ""),
                "completed_sub_steps": completed,
                "pending_sub_steps": pending,
                "cached_tool_results": len(cached) if isinstance(cached, dict) else 0,
            }
        )
    progress.sort(key=lambda item: item["candidate_index"])
    return progress
