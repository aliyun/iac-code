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


def terminal_outcome_from_final_conclusion(conclusion: dict | None) -> TerminalOutcome | None:
    """Map a terminal step's structured conclusion ``status`` to a terminal outcome.

    Steps that report their own outcome (e.g. the deploying step's
    ``status: success|failed|cancelled``) must drive the pipeline terminal
    outcome. Otherwise a step that completed its agent turn but concluded with
    ``status: failed`` (e.g. WAF blocked CreateStack, or ``statuses.failed>0``)
    would be reported as a successful ``pipeline_completed``.

    Returns ``None`` when the conclusion carries no recognizable failure/cancel
    signal so callers keep the existing default ``completed`` behavior.
    """
    if not isinstance(conclusion, dict):
        return None
    status = conclusion.get("status")
    if not isinstance(status, str):
        return None
    normalized = status.strip().lower()
    if normalized == "failed":
        return "failed"
    if normalized in {"cancelled", "canceled"}:
        return "canceled"
    return None


def build_handoff_summary(
    pipeline_name: str,
    outcome: TerminalOutcome,
    context_snapshot: dict,
    include_fields: list[str],
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
