"""Localized display labels for pipeline identifiers.

Machine-facing identifiers such as ``complete_step`` and ``intent_parsing``
must stay stable in protocol payloads and tool registries. These helpers are
only for user-facing labels in terminal UI and replay text.
"""

from __future__ import annotations

from iac_code.i18n import _


def display_pipeline_name(pipeline_name: str) -> str:
    """Return a localized display label for a pipeline identifier."""
    return _known_pipeline_names().get(pipeline_name, _humanize_identifier(pipeline_name))


def display_step_name(step_id: str) -> str:
    """Return a localized display label for a pipeline step identifier."""
    return _known_step_names().get(step_id, _humanize_identifier(step_id))


def display_tool_name(tool_name: str) -> str:
    """Return a localized display label for a tool identifier."""
    return _known_tool_names().get(tool_name, _humanize_identifier(tool_name))


def known_tool_display_name(tool_name: str) -> str | None:
    """Return the localized label for a known pipeline tool identifier, if any."""
    return _known_tool_names().get(tool_name)


def _known_pipeline_names() -> dict[str, str]:
    return {
        "selling": _("Selling"),
    }


def _known_step_names() -> dict[str, str]:
    return {
        "intent_parsing": _("Intent parsing"),
        "architecture_planning": _("Architecture planning"),
        "evaluate_candidates": _("Evaluate candidates"),
        "confirm_and_select": _("Confirm and select"),
        "deploying": _("Deploying"),
        "evaluate_candidate": _("Evaluate candidate"),
        "template_generating": _("Template generation"),
        "reviewing": _("Review"),
        "cost_estimating": _("Cost estimation"),
        "current_step": _("Current step"),
    }


def _known_tool_names() -> dict[str, str]:
    return {
        "complete_step": _("Complete step"),
        "ask_user_question": _("Ask user question"),
        "show_architecture_diagram": _("Show architecture diagram"),
        "show_candidate_detail": _("Show candidate details"),
    }


def _humanize_identifier(value: str) -> str:
    if not value:
        return ""
    label = value.replace("_", " ").replace("-", " ").strip()
    return label[:1].upper() + label[1:]
