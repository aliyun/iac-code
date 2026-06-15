"""ShowCandidateDetailTool — emits structured candidate details for the selection UI."""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import CandidateDetailEvent


class ShowCandidateDetailTool(Tool):
    """Pipeline tool that emits candidate summary and cost data for tabbed display."""

    @property
    def name(self) -> str:
        return "show_candidate_detail"

    @property
    def description(self) -> str:
        return _("Display candidate details (summary and cost breakdown) in the comparison tabs.")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "candidate_name": {
                    "type": "string",
                    "description": _("Candidate name; must match candidate_name in show_architecture_diagram"),
                },
                "candidate_index": {
                    "type": "integer",
                    "description": _(
                        "Zero-based candidate index in evaluated_candidates; used to distinguish duplicate names"
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": _("Candidate summary description"),
                },
                "cost_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "spec": {"type": "string"},
                            "monthly_cost": {"type": "string"},
                        },
                    },
                    "description": _("Cost breakdown list"),
                },
                "total_monthly_cost": {
                    "type": "string",
                    "description": _("Total monthly cost, such as CNY 1,234/month"),
                },
            },
            "required": ["candidate_name", "candidate_index", "summary", "cost_items", "total_monthly_cost"],
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def needs_event_queue(self) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        candidate_name = tool_input["candidate_name"]
        candidate_index = tool_input.get("candidate_index")

        if context.event_queue is not None:
            event = CandidateDetailEvent(
                # U-I14: tag with the current tool_use_id so multiple parallel
                # show_candidate_detail calls in the same step don't collide
                # on the renderer's per-tab accumulator key.
                tool_use_id=context.tool_use_id or "",
                candidate_name=candidate_name,
                summary=tool_input["summary"],
                cost_items=tool_input["cost_items"],
                total_monthly_cost=tool_input["total_monthly_cost"],
                candidate_index=candidate_index,
            )
            await context.event_queue.put(event)
        else:
            from loguru import logger

            logger.debug(
                "{} invoked without event_queue; skipping event emit "
                "(typically means pipeline mode not active for this tool call)",
                type(self).__name__,
            )

        return ToolResult.success(_('Displayed details for "{candidate_name}".').format(candidate_name=candidate_name))
