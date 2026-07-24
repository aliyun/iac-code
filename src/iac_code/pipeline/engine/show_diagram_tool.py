"""ShowArchitectureDiagramTool — reads a ROS YAML template and emits a Mermaid architecture diagram."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from iac_code.i18n import _
from iac_code.pipeline.engine.architecture_graph import (
    ArchitectureMultiViewRenderResult,
    render_ros_template_architecture,
    render_ros_template_architecture_views,
)
from iac_code.pipeline.engine.architecture_semantic_planning import (
    create_semantic_plan_for_architecture_with_llm,
    repair_semantic_plan_locally,
)
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import DiagramEvent


def ros_template_to_mermaid(template_yaml: str, *, semantic_plan: dict[str, Any] | None = None) -> str:
    """Convert a ROS YAML template into a Mermaid graph with nested infrastructure layers.

    VPC / VSwitch / SecurityGroup are rendered as nested subgraphs (container layers).
    Compute, gateway, storage resources are rendered as nodes inside the appropriate layer.
    Auxiliary resources (SecurityGroupIngress, EIPAssociation, etc.) are hidden; their
    relationships are expressed as edges between the visible nodes.
    """
    return render_ros_template_architecture(template_yaml, semantic_plan=semantic_plan).mermaid_source


class ShowArchitectureDiagramTool(Tool):
    """Pipeline-specific tool that reads a ROS template and emits a Mermaid architecture diagram."""

    @property
    def name(self) -> str:
        return "show_architecture_diagram"

    @property
    def description(self) -> str:
        return _(
            "Read a ROS template YAML file and generate an architecture diagram. "
            "Pass the template file path relative to the working directory and the candidate name."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": _(
                        "Relative path to the ROS template YAML file, such as templates/1-simple-nginx.yml"
                    ),
                },
                "candidate_name": {
                    "type": "string",
                    "description": _("Candidate name, such as Simple Nginx single-instance plan"),
                },
                "candidate_index": {
                    "type": "integer",
                    "description": _(
                        "Zero-based candidate index in evaluated_candidates; used to distinguish duplicate names"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["render", "facts"],
                    "description": _(
                        "Use facts to emit an immediate draft diagram and internally optimize it with the LLM; "
                        "use render to emit a diagram from an explicit semantic_plan."
                    ),
                },
                "semantic_plan": {
                    "type": "object",
                    "description": _(
                        "Optional LLM-generated semantic graph plan. Edges must reference visible node ids from "
                        "a prior facts response."
                    ),
                },
            },
            "required": ["file_path", "candidate_name", "candidate_index"],
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    @property
    def timeout(self) -> float | None:
        return 600.0

    def needs_event_queue(self) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = tool_input["file_path"]
        candidate_name = tool_input["candidate_name"]
        candidate_index = tool_input.get("candidate_index")

        try:
            abs_path = _resolve_cwd_relative_file(context.cwd, file_path)
        except ValueError as exc:
            await _emit_diagram_error_event(
                context,
                candidate_name=candidate_name,
                candidate_index=candidate_index,
                message=str(exc),
            )
            return ToolResult.error(str(exc))

        if not abs_path.exists():
            message = _("Template file does not exist: {file_path}").format(file_path=file_path)
            await _emit_diagram_error_event(
                context,
                candidate_name=candidate_name,
                candidate_index=candidate_index,
                message=message,
            )
            return ToolResult.error(message)

        template_content = abs_path.read_text(encoding="utf-8")
        mode = tool_input.get("mode")
        mode = mode if mode in {"render", "facts"} else "render"
        semantic_plan = tool_input.get("semantic_plan")
        if not isinstance(semantic_plan, dict):
            semantic_plan = None

        # Rendering walks the whole template graph (pure CPU); keep it off the
        # shared event loop so web agent turns, SSE, and HTTP handlers stay responsive.
        base_render_result = await asyncio.to_thread(render_ros_template_architecture_views, template_content)
        if mode == "facts":
            if context.event_queue is not None:
                await context.event_queue.put(
                    _diagram_event_from_render_result(
                        candidate_name=candidate_name,
                        template_content=template_content,
                        candidate_index=candidate_index,
                        render_result=base_render_result,
                        diagram_stage="draft",
                    )
                )

            render_result = base_render_result
            try:
                generated_semantic_plan = await create_semantic_plan_for_architecture_with_llm(
                    base_render_result.architecture_context,
                    template_content,
                )
            except asyncio.CancelledError:
                if context.event_queue is not None:
                    await context.event_queue.put(
                        _diagram_event_from_render_result(
                            candidate_name=candidate_name,
                            template_content=template_content,
                            candidate_index=candidate_index,
                            render_result=base_render_result,
                            diagram_stage="optimized",
                        )
                    )
                raise
            except Exception:
                logger.exception("Failed to optimize architecture diagram with the LLM; keeping draft diagram")
            else:
                if generated_semantic_plan:
                    render_result = await asyncio.to_thread(
                        render_ros_template_architecture_views,
                        template_content,
                        semantic_plan=generated_semantic_plan,
                    )

            if context.event_queue is not None:
                await context.event_queue.put(
                    _diagram_event_from_render_result(
                        candidate_name=candidate_name,
                        template_content=template_content,
                        candidate_index=candidate_index,
                        render_result=render_result,
                        diagram_stage="optimized",
                    )
                )
            return ToolResult.success(
                _('Generated and optimized the architecture diagram for "{candidate_name}".').format(
                    candidate_name=candidate_name
                )
            )

        if semantic_plan is not None:
            semantic_plan = await asyncio.to_thread(
                repair_semantic_plan_locally, base_render_result.architecture_context, semantic_plan
            )
            render_result = await asyncio.to_thread(
                render_ros_template_architecture_views, template_content, semantic_plan=semantic_plan
            )
        else:
            render_result = base_render_result

        if context.event_queue is not None:
            event = _diagram_event_from_render_result(
                candidate_name=candidate_name,
                template_content=template_content,
                candidate_index=candidate_index,
                render_result=render_result,
                diagram_stage="optimized",
            )
            await context.event_queue.put(event)
        else:
            logger.debug(
                "{} invoked without event_queue; skipping event emit "
                "(typically means pipeline mode not active for this tool call)",
                type(self).__name__,
            )

        return ToolResult.success(
            _('Generated the architecture diagram for "{candidate_name}".').format(candidate_name=candidate_name)
        )


def _resolve_cwd_relative_file(cwd: str, file_path: str) -> Path:
    path = Path(file_path)
    if path.is_absolute():
        raise ValueError(_("Template file path must be relative to the working directory"))

    root = Path(cwd).resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(_("Template file path cannot escape the working directory"))
    return resolved


async def _emit_diagram_error_event(
    context: ToolContext,
    *,
    candidate_name: str,
    candidate_index: int | None,
    message: str,
) -> None:
    if context.event_queue is None:
        return
    mermaid_source = _error_diagram_mermaid(message)
    await context.event_queue.put(
        DiagramEvent(
            candidate_name=candidate_name,
            template_content="",
            mermaid_source=mermaid_source,
            candidate_index=candidate_index,
            architecture_context={"error": message},
            diagram_stage="optimized",
            views=[
                {
                    "id": "overview",
                    "title": "overview",
                    "purpose": "",
                    "mermaid_source": mermaid_source,
                }
            ],
        )
    )


def _error_diagram_mermaid(message: str) -> str:
    label = " ".join(str(message).split()) or "Architecture diagram unavailable"
    return "graph TD\n  ArchitectureDiagramUnavailable[" + json.dumps(label, ensure_ascii=False) + "]"


def _diagram_event_from_render_result(
    *,
    candidate_name: str,
    template_content: str,
    candidate_index: int | None,
    render_result: ArchitectureMultiViewRenderResult,
    diagram_stage: str,
) -> DiagramEvent:
    views = [
        {
            "id": view.id,
            "title": view.title,
            "purpose": view.purpose,
            "mermaid_source": view.mermaid_source,
        }
        for view in render_result.views
    ]
    mermaid_source = views[0]["mermaid_source"] if views else "graph TD"
    return DiagramEvent(
        candidate_name=candidate_name,
        template_content=template_content,
        mermaid_source=mermaid_source,
        candidate_index=candidate_index,
        architecture_context=render_result.architecture_context,
        diagram_stage="draft" if diagram_stage == "draft" else "optimized",
        views=views,
    )
