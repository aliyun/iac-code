"""Keep explicit user candidate counts authoritative before evaluation starts."""

from typing import Any

from iac_code.i18n import _
from iac_code.pipeline.engine.complete_step_tool import CompletionEnrichmentError


def enrich_completion_input(
    *, tool_input: dict[str, Any], context_snapshot: dict[str, Any], **_ignored: Any,
) -> dict[str, Any]:
    if tool_input.get("rollback_request"):
        return tool_input
    intent = context_snapshot.get("intent")
    requested = intent.get("requested_candidate_count") if isinstance(intent, dict) else None
    if requested is None:
        return tool_input  # Existing sessions and unspecified counts remain adaptive.
    if isinstance(requested, float) and requested.is_integer():
        requested = int(requested)  # JSON Schema integer also accepts values such as 2.0.
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        raise CompletionEnrichmentError(_("requested_candidate_count must be a positive integer or null"))
    conclusion = tool_input.get("conclusion")
    candidates = conclusion.get("candidates") if isinstance(conclusion, dict) else None
    if isinstance(candidates, list) and len(candidates) != requested:
        raise CompletionEnrichmentError(_(
            "The user requested {expected} candidate plans; {actual} were submitted. "
            "Correct the candidates or ask for clarification; do not silently change the requested count."
        ).format(expected=requested, actual=len(candidates)))
    return tool_input
