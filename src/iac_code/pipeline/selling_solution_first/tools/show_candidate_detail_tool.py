"""Rich candidate detail display tool for ``selling_solution_first`` Step 1 only."""

from __future__ import annotations

from typing import Any

from loguru import logger

from iac_code.i18n import _
from iac_code.pipeline.selling_solution_first.tools.candidate_planning_records import (
    first_missing_candidate_detail_index,
    latest_candidate_outline_batch,
)
from iac_code.pipeline.selling_solution_first.tools.show_architecture_plan_tool import (
    render_architecture_graph,
)
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent


class ShowCandidateDetailTool(Tool):
    """Validate and display one rich candidate after the complete outline batch."""

    def __init__(self, completion_guard_state: dict[str, Any] | None = None) -> None:
        self._completion_guard_state = completion_guard_state if completion_guard_state is not None else {}

    @property
    def name(self) -> str:
        return "show_candidate_detail"

    @property
    def description(self) -> str:
        return _(
            "Display the rich detail for exactly one candidate from the latest show_architecture_plan batch. "
            "Call once per model turn in candidate index order. Include resource lifecycle intent, topology graph, "
            "resource inventory, cost assumptions and decision notes; do not repeat summary or monthly total."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "candidate_index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": _("Zero-based index from the latest candidate outline batch"),
                },
                "candidate_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": _("Exact candidate name at candidate_index in the latest outline batch"),
                },
                "applicable_scenarios": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "resource_intents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["product", "action"],
                        "properties": {
                            "product": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["create", "use_existing", "reference", "forbid"],
                            },
                            "role": {"type": "string"},
                            "source": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                    },
                },
                "topology_graph": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["nodes", "edges"],
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["id", "label", "product"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "product": {"type": "string"},
                                    "role": {"type": "string"},
                                    "group": {"type": "string"},
                                },
                            },
                        },
                        "edges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["source", "target"],
                                "properties": {
                                    "source": {"type": "string"},
                                    "target": {"type": "string"},
                                    "label": {"type": "string"},
                                    "relation": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "resource_inventory": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["resource_id", "product", "purpose", "quantity", "lifecycle"],
                        "properties": {
                            "resource_id": {"type": "string"},
                            "product": {"type": "string"},
                            "resource_type": {"type": "string"},
                            "purpose": {"type": "string"},
                            "quantity": {"type": "integer", "minimum": 1},
                            "recommended_spec": {"type": "string"},
                            "billing_method": {"type": "string"},
                            "rough_monthly_cost": {"type": "string"},
                            "lifecycle": {
                                "type": "string",
                                "enum": ["create", "use_existing", "reference", "forbid"],
                            },
                        },
                    },
                },
                "cost_assumptions": {"type": "array", "items": {"type": "string"}},
                "cost_exclusions": {"type": "array", "items": {"type": "string"}},
                "cost_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "decision_notes": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["why_recommended", "problems_solved", "pros", "cons"],
                    "properties": {
                        "why_recommended": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "problems_solved": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "pros": {"type": "array", "minItems": 2, "items": {"type": "string"}},
                        "cons": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "risks": {"type": "array", "items": {"type": "string"}},
                        "tradeoffs": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "required": [
                "candidate_index",
                "candidate_name",
                "applicable_scenarios",
                "resource_intents",
                "topology_graph",
                "resource_inventory",
                "cost_assumptions",
                "cost_exclusions",
                "cost_confidence",
                "decision_notes",
            ],
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def needs_event_queue(self) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        records = self._completion_guard_state.get("tool_result_records")
        records = records if isinstance(records, list) else []
        batch = latest_candidate_outline_batch(records)
        if batch is None:
            return ToolResult.error(
                _(
                    "show_candidate_detail is not allowed before a successful show_architecture_plan outline batch."
                )
            )

        expected_index = first_missing_candidate_detail_index(records, batch)
        if expected_index is None:
            return ToolResult(
                content=_("All candidates in candidateSetId={candidate_set_id} already have rich details.").format(
                    candidate_set_id=batch.candidate_set_id
                ),
                is_error=True,
                metadata={"candidate_set_id": batch.candidate_set_id},
            )
        expected_name = batch.candidates[expected_index]["candidate_name"]
        actual_index = tool_input.get("candidate_index")
        actual_name = str(tool_input.get("candidate_name") or "").strip()
        if actual_index != expected_index or actual_name != expected_name:
            return ToolResult(
                content=_(
                    "show_candidate_detail candidate_index={actual_index} is not allowed yet; expected "
                    "candidate_index={expected_index}, candidate_name={expected_name!r} from "
                    "candidateSetId={candidate_set_id}."
                ).format(
                    actual_index=actual_index,
                    expected_index=expected_index,
                    expected_name=expected_name,
                    candidate_set_id=batch.candidate_set_id,
                ),
                is_error=True,
                metadata={"candidate_set_id": batch.candidate_set_id},
            )

        try:
            mermaid_source, architecture_context, warnings = render_architecture_graph(
                tool_input.get("topology_graph")
            )
        except ValueError as exc:
            return ToolResult(
                content=_("Failed to render the candidate topology: {reason}").format(reason=str(exc)),
                is_error=True,
                metadata={"candidate_set_id": batch.candidate_set_id},
            )

        outline = batch.candidates[expected_index]
        cost_items = _cost_items_from_inventory(tool_input.get("resource_inventory"))
        if context.event_queue is not None:
            await context.event_queue.put(
                CandidateDetailEvent(
                    tool_use_id=context.tool_use_id or f"{batch.candidate_set_id}:detail:{expected_index}",
                    candidate_name=expected_name,
                    summary=outline["summary"],
                    cost_items=cost_items,
                    total_monthly_cost=outline["total_monthly_cost"],
                    candidate_index=expected_index,
                    candidate_set_id=batch.candidate_set_id,
                    detail_stage="detail",
                )
            )
            await context.event_queue.put(
                DiagramEvent(
                    candidate_name=expected_name,
                    template_content="",
                    mermaid_source=mermaid_source,
                    candidate_index=expected_index,
                    architecture_context=architecture_context,
                    diagram_stage="optimized",
                    views=[
                        {
                            "id": "overview",
                            "title": _("Architecture plan"),
                            "purpose": "",
                            "mermaid_source": mermaid_source,
                        }
                    ],
                    candidate_set_id=batch.candidate_set_id,
                    detail_stage="detail",
                )
            )
        else:
            logger.debug("ShowCandidateDetailTool invoked without event_queue; skipping display events")

        message = _(
            'Displayed rich detail for candidate {candidate_index} "{candidate_name}" '
            "in candidateSetId={candidate_set_id}."
        ).format(
            candidate_index=expected_index,
            candidate_name=expected_name,
            candidate_set_id=batch.candidate_set_id,
        )
        if warnings:
            message = "{}\n{}".format(message, "\n".join(warnings))
        return ToolResult(content=message, metadata={"candidate_set_id": batch.candidate_set_id})


def _cost_items_from_inventory(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        product = str(raw.get("product") or raw.get("resource_id") or "").strip()
        quantity = raw.get("quantity")
        spec = str(raw.get("recommended_spec") or "").strip()
        if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 1:
            spec = f"{spec} × {quantity}" if spec else f"× {quantity}"
        items.append(
            {
                "name": product,
                "spec": spec,
                "monthly_cost": str(raw.get("rough_monthly_cost") or "").strip(),
            }
        )
    return items


__all__ = ["ShowCandidateDetailTool"]
