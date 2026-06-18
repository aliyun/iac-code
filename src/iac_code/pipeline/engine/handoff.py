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
    lines.extend(["", "Use this context when answering follow-up questions after the pipeline handoff."])
    return "\n".join(lines)
