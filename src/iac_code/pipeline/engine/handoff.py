"""Pure helpers for finite pipeline completion handoff."""

from __future__ import annotations

import json
import os
from typing import Literal

TerminalOutcome = Literal["completed", "early_exit", "failed", "canceled"]

_USE_TOOL_CONFIRMATION_ENV = "IAC_CODE_HANDOFF_USE_TOOL_CONFIRMATION"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


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
    use_tool_confirmation = os.environ.get(_USE_TOOL_CONFIRMATION_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES
    if use_tool_confirmation:
        release_requirement = (
            "- For operations that release, delete, or otherwise destroy a resource, rely on the tool "
            "permission confirmation as the sole confirmation. Do not ask for a separate confirmation "
            "in normal chat, and do not proceed unless the permission request is approved."
        )
        cleanup_exception = (
            "- Exception: pipeline-managed automatic cleanup may proceed without an additional confirmation."
        )
    else:
        release_requirement = (
            "- Before performing any operation that releases, deletes, or otherwise destroys a resource, "
            "obtain a fresh, explicit confirmation from the user in normal chat. Any confirmation given "
            "during the pipeline does not count."
        )
        cleanup_exception = (
            "- Exception: pipeline-managed automatic cleanup may proceed without this additional confirmation."
        )
    lines.extend(
        [
            "",
            "Safety requirements for normal chat:",
            release_requirement,
            cleanup_exception,
            "",
            "Use this context when answering follow-up questions after the pipeline handoff.",
        ]
    )
    return "\n".join(lines)
