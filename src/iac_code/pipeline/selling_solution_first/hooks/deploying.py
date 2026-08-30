"""Hook for the ``deploying`` step of ``selling_solution_first``.

Step 2 (``materialize_selected_candidate``) already emits a normalized and user-confirmed
``selected_plan``. This hook only decides whether that hand-off actually authorizes cloud writes and
records the verdict in place inside the existing ``selected_plan`` object, so both the deploying
prompt and the pipeline-local :mod:`~..tools.confirmed_ros_deploy_tool` wrapper read one shared
source of truth (design 9.1 / 9.4).

Resource observation and rollback cleanup are imported from the existing ``selling`` hook — no ROS
stack lifecycle, failure-recovery or cleanup logic is reimplemented here.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from iac_code.pipeline.engine.complete_step_tool import CompletionEnrichmentError
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.selling.hooks.deploying import (
    contains_redaction_placeholder,
    on_resource_observed,
    on_rollback_cleanup_required,
)

logger = logging.getLogger(__name__)

__all__ = [
    "contains_redaction_placeholder",
    "evaluate_deployment_gate",
    "enrich_completion_input",
    "on_enter",
    "on_resource_observed",
    "on_rollback_cleanup_required",
]


def enrich_completion_input(
    *,
    tool_input: dict[str, Any],
    tool_result_records: list[dict[str, Any]],
    **_: Any,
) -> dict[str, Any]:
    """Inject only real ``ros_deploy`` facts into the Step 3 runtime conclusion."""

    raw = tool_input.get("conclusion")
    if not isinstance(raw, dict):
        raise CompletionEnrichmentError("Step 3 completion conclusion must be an object")
    status = raw.get("status")
    if status == "cancelled":
        tool_input["conclusion"] = {"status": "cancelled"}
        return tool_input
    records = [
        record
        for record in tool_result_records
        if isinstance(record, dict) and record.get("tool_name") == "ros_deploy"
    ]
    if status == "success":
        record = next(
            (
                item
                for item in reversed(records)
                if not item.get("is_error")
                and isinstance(item.get("result"), dict)
                and item["result"].get("status") == "CREATE_COMPLETE"
                and isinstance(item["result"].get("stack_id"), str)
                and item["result"].get("stack_id")
            ),
            None,
        )
        if record is None:
            raise CompletionEnrichmentError("success requires a real ros_deploy CREATE_COMPLETE result")
        result = record["result"]
        conclusion: dict[str, Any] = {"status": "success", "stack_id": result["stack_id"]}
        outputs = result.get("outputs", result.get("Outputs"))
        if isinstance(outputs, dict):
            conclusion["outputs"] = copy.deepcopy(outputs)
        resources = result.get("resources_created", result.get("resources"))
        if isinstance(resources, list) and all(isinstance(item, str) for item in resources):
            conclusion["resources_created"] = copy.deepcopy(resources)
        tool_input["conclusion"] = conclusion
        return tool_input
    if status == "failed":
        record = next(
            (
                item
                for item in reversed(records)
                if item.get("is_error") or str((item.get("result") or {}).get("status", "")).endswith("FAILED")
            ),
            None,
        )
        if record is None:
            raise CompletionEnrichmentError("failed requires a real failing ros_deploy result")
        raw_result = record.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        error = record.get("error_summary") or result.get("error") or result.get("status")
        if not error:
            raise CompletionEnrichmentError("the failing ros_deploy record has no recoverable error")
        tool_input["conclusion"] = {"status": "failed", "error": str(error)}
        return tool_input
    raise CompletionEnrichmentError("Step 3 status must be success, failed, or cancelled")


def evaluate_deployment_gate(selected_plan: Any) -> str:
    """Return an empty string when ``selected_plan`` authorizes deployment, else the blocking reason.

    Pure function shared by :func:`on_enter` and the confirmed ``ros_deploy`` wrapper, so the prompt
    gate and the tool gate can never disagree.
    """

    if not isinstance(selected_plan, dict):
        return "selected_plan is missing; the confirmation hand-off from materialize_selected_candidate is absent"

    status = selected_plan.get("status")
    if status != "confirmed":
        return f"selected_plan.status must be 'confirmed', got {status!r}"
    if selected_plan.get("continue_pipeline") is not True:
        return "selected_plan.continue_pipeline is not true"
    if selected_plan.get("deployment_confirmed") is not True:
        return "selected_plan.deployment_confirmed is not true; the user did not confirm deployment"
    if selected_plan.get("selection_valid") is not True:
        return "selected_plan.selection_valid is not true; the selected candidate could not be resolved"

    template_url = selected_plan.get("template_url")
    if not isinstance(template_url, str) or not template_url.strip():
        return "selected_plan.template_url is empty; there is no validated template to deploy"

    result = selected_plan.get("selected_candidate_result")
    if isinstance(result, dict) and result.get("failed") is True:
        return "selected_plan.selected_candidate_result.failed is true"
    return ""


def on_enter(ctx: PipelineContext) -> None:
    """Record the deployment gate verdict inside the existing ``selected_plan`` conclusion."""

    selected_plan = ctx.get_conclusion("selected_plan")
    error = evaluate_deployment_gate(selected_plan)
    if not isinstance(selected_plan, dict):
        # Nothing to annotate in place; the prompt and the ros_deploy wrapper both re-evaluate the
        # gate from the same context value, so deployment stays blocked.
        logger.warning("deploying step entered without a selected_plan object: %s", error)
        return

    selected_plan["deployment_gate_valid"] = not error
    selected_plan["deployment_gate_error"] = error
    if error:
        logger.warning("deploying step entered with an invalid deployment gate: %s", error)
