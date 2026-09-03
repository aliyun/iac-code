"""Step 1 candidate outline tool and the local planned-architecture renderer.

``show_architecture_plan`` now submits one complete, lightweight candidate outline batch.  Rich
topology rendering remains in this module so the pipeline-local ``show_candidate_detail`` tool can
reuse the existing sanitizing and Mermaid behavior without moving or duplicating it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from iac_code.i18n import _
from iac_code.pipeline.selling_solution_first.tools.candidate_planning_records import (
    latest_candidate_outline_batch,
    normalize_outline_candidates,
)
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent

_MAX_NODES = 60
_MAX_EDGES = 120
_MAX_LABEL_CHARS = 60
# Sanitizing caps the stored text; these cap what a single rendered line may show, so Mermaid node
# boxes stay narrow enough to read in the Step 1 plan panel.
_MAX_NODE_LINE_CHARS = 28
_MAX_NODE_DETAIL_CHARS = 20
_MAX_GROUP_TITLE_CHARS = 32
_UNSAFE_ID_CHARS = re.compile(r"[^0-9A-Za-z_]")
# Mermaid treats these as syntax inside node/edge labels; drop or fold them into safe text.
_UNSAFE_LABEL_CHARS = re.compile(r"[\"'`\[\]{}()<>|;\\]")


class ShowArchitecturePlanTool(Tool):
    """Submit the complete lightweight outline batch for the current planning revision."""

    def __init__(self, completion_guard_state: dict[str, Any] | None = None) -> None:
        self._completion_guard_state = completion_guard_state if completion_guard_state is not None else {}

    @property
    def name(self) -> str:
        return "show_architecture_plan"

    @property
    def description(self) -> str:
        return _(
            "Display one complete batch of lightweight candidate outlines before rich details are generated. "
            "Submit every current candidate in order with its name, summary, monthly estimate and key trade-off. "
            "Do not include topology nodes, resource inventory or detailed cost items."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "description": _(
                        "The complete current candidate batch. Array order defines zero-based candidate indexes."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "candidate_name": {
                                "type": "string",
                                "minLength": 1,
                                "description": _("Unique user-facing candidate name"),
                            },
                            "summary": {
                                "type": "string",
                                "minLength": 1,
                                "description": _("Short product combination and architecture summary"),
                            },
                            "total_monthly_cost": {
                                "type": "string",
                                "minLength": 1,
                                "description": _("Rough monthly range, such as ¥230～¥380/month"),
                            },
                            "key_tradeoff": {
                                "type": "string",
                                "minLength": 1,
                                "description": _("The most important cost, availability or complexity trade-off"),
                            },
                        },
                        "required": ["candidate_name", "summary", "total_monthly_cost", "key_tradeoff"],
                    },
                },
            },
            "required": ["candidates"],
            "additionalProperties": False,
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def needs_event_queue(self) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        candidates = normalize_outline_candidates(tool_input.get("candidates"))
        if candidates is None:
            return ToolResult.error(
                _(
                    "candidates must be a non-empty array of unique outlines with candidate_name, summary, "
                    "total_monthly_cost and key_tradeoff"
                )
            )
        records = self._completion_guard_state.get("tool_result_records")
        records = records if isinstance(records, list) else []
        active_batch = latest_candidate_outline_batch(records)
        if active_batch is not None and active_batch.candidates == candidates:
            return ToolResult(
                content=_(
                    "This identical candidate outline batch is already active as "
                    "candidateSetId={candidate_set_id}. Do not repeat show_architecture_plan; "
                    "continue with show_candidate_detail for the first missing candidate."
                ).format(candidate_set_id=active_batch.candidate_set_id),
                metadata={"candidate_set_id": active_batch.candidate_set_id, "idempotent": True},
            )
        candidate_set_id = context.tool_use_id or "candidate-set-local"
        if context.event_queue is not None:
            for candidate_index, candidate in enumerate(candidates):
                await context.event_queue.put(
                    CandidateDetailEvent(
                        tool_use_id=f"{candidate_set_id}:outline:{candidate_index}",
                        candidate_name=candidate["candidate_name"],
                        summary=candidate["summary"],
                        cost_items=[],
                        total_monthly_cost=candidate["total_monthly_cost"],
                        candidate_index=candidate_index,
                        candidate_set_id=candidate_set_id,
                        detail_stage="outline",
                        key_tradeoff=candidate["key_tradeoff"],
                    )
                )
        else:
            logger.debug(
                "{} invoked without event_queue; skipping event emit "
                "(typically means pipeline mode not active for this tool call)",
                type(self).__name__,
            )

        return ToolResult(
            content=_(
                "Displayed {count} candidate outlines; candidateSetId={candidate_set_id}. "
                "Do not repeat show_architecture_plan unless the user changes the candidate set; "
                "continue with show_candidate_detail."
            ).format(count=len(candidates), candidate_set_id=candidate_set_id),
            metadata={"candidate_set_id": candidate_set_id},
        )


class _ArchitecturePlan:
    """Validated and sanitized plan graph ready for Mermaid rendering."""

    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []
        self.groups: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.warnings: list[str] = []


def render_architecture_graph(topology_graph: Any) -> tuple[str, dict[str, Any], list[str]]:
    """Validate one rich detail graph and return its Mermaid source and UI context."""

    if not isinstance(topology_graph, dict):
        raise ValueError(_("topology_graph must be an object with nodes and edges"))
    plan = _build_architecture_plan(topology_graph.get("nodes"), topology_graph.get("edges"))
    return _render_plan_mermaid(plan), _plan_architecture_context(plan), list(plan.warnings)


def _normalized_candidate_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _build_architecture_plan(raw_nodes: Any, raw_edges: Any) -> _ArchitecturePlan:
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError(_("nodes must be a non-empty array of architecture nodes"))
    if raw_edges is not None and not isinstance(raw_edges, list):
        raise ValueError(_("edges must be an array of architecture edges"))

    plan = _ArchitecturePlan()
    if len(raw_nodes) > _MAX_NODES:
        plan.warnings.append(
            _("Only the first {limit} nodes are rendered; the plan declared {count}.").format(
                limit=_MAX_NODES, count=len(raw_nodes)
            )
        )

    raw_ids: set[str] = set()
    safe_ids: dict[str, str] = {}
    used_safe_ids: set[str] = set()
    group_ids: dict[str, str] = {}

    for position, raw_node in enumerate(raw_nodes[:_MAX_NODES]):
        if not isinstance(raw_node, dict):
            raise ValueError(_("nodes[{index}] must be an object").format(index=position))
        raw_id = str(raw_node.get("id") or "").strip()
        if not raw_id:
            raise ValueError(_("nodes[{index}].id must not be empty").format(index=position))
        if raw_id in raw_ids:
            raise ValueError(_("Duplicate node id: {node_id}").format(node_id=raw_id))
        raw_ids.add(raw_id)

        safe_id = _safe_mermaid_id(raw_id, position, used_safe_ids)
        used_safe_ids.add(safe_id)
        safe_ids[raw_id] = safe_id

        label = _safe_label(raw_node.get("label")) or _safe_label(raw_id) or safe_id
        product = _safe_label(raw_node.get("product"))
        role = _safe_label(raw_node.get("role"))
        raw_group = str(raw_node.get("group") or "").strip()
        group_id = None
        if raw_group:
            group_id = group_ids.get(raw_group)
            if group_id is None:
                group_id = _safe_mermaid_id(f"group_{raw_group}", len(group_ids), used_safe_ids)
                used_safe_ids.add(group_id)
                group_ids[raw_group] = group_id
                plan.groups.append(
                    {
                        "id": group_id,
                        "raw_id": raw_group,
                        "label": _safe_label(raw_group) or group_id,
                    }
                )

        plan.nodes.append(
            {
                "id": safe_id,
                "raw_id": raw_id,
                "label": label,
                "product": product,
                "role": role,
                "group": group_id,
            }
        )

    seen_edges: set[tuple[str, str, str]] = set()
    for position, raw_edge in enumerate(raw_edges or []):
        if len(plan.edges) >= _MAX_EDGES:
            plan.warnings.append(
                _("Only the first {limit} edges are rendered.").format(limit=_MAX_EDGES),
            )
            break
        if not isinstance(raw_edge, dict):
            plan.warnings.append(_("Skipped edges[{index}]: not an object.").format(index=position))
            continue
        source = str(raw_edge.get("source") or "").strip()
        target = str(raw_edge.get("target") or "").strip()
        # Dangling references are dropped instead of failing the whole render, so a single bad
        # edge cannot hide the plan and block candidate selection (design 7.4).
        if source not in safe_ids or target not in safe_ids:
            plan.warnings.append(
                _("Skipped edge {source} -> {target}: it references a node id that is not defined.").format(
                    source=source or "?", target=target or "?"
                )
            )
            continue
        if source == target:
            plan.warnings.append(_("Skipped self-referencing edge on node {node_id}.").format(node_id=source))
            continue
        label = _safe_label(raw_edge.get("label")) or _safe_label(raw_edge.get("relation"))
        key = (safe_ids[source], safe_ids[target], label)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        plan.edges.append({"source": safe_ids[source], "target": safe_ids[target], "label": label})

    _fold_group_container_nodes(plan)
    return plan


def _fold_group_container_nodes(plan: _ArchitecturePlan) -> None:
    """Fold a node that other nodes use as their ``group`` into that group's subgraph title.

    Models routinely emit a ``vpc`` node *and* put the resources inside ``group: "vpc"``, which used
    to render a ``VPC`` box next to a ``vpc`` subgraph plus a "contains" arrow between them — three
    ways of saying the same thing. Marking the node here lets :func:`_render_plan_mermaid` show it as
    the subgraph title only. ``plan.nodes``/``plan.edges`` stay faithful to the model's plan, so the
    architecture context handed to downstream surfaces is unchanged.

    A container node that itself belongs to another group is left alone: the flat plan schema cannot
    express nested subgraphs, so folding it would silently drop that membership.
    """
    by_raw_id: dict[str, dict[str, Any]] = {}
    by_folded_raw_id: dict[str, dict[str, Any]] = {}
    for node in plan.nodes:
        by_raw_id.setdefault(node["raw_id"], node)
        by_folded_raw_id.setdefault(node["raw_id"].casefold(), node)

    member_counts: dict[str, int] = {}
    for node in plan.nodes:
        if node["group"]:
            member_counts[node["group"]] = member_counts.get(node["group"], 0) + 1

    for group in plan.groups:
        container = by_raw_id.get(group["raw_id"]) or by_folded_raw_id.get(group["raw_id"].casefold())
        if container is None or container["group"] or container.get("container_of_group"):
            continue
        # Invariant guard: a group only exists because some node declared it, but an empty group is
        # never rendered, so folding into one would make the container node vanish from the diagram.
        if not member_counts.get(group["id"]):
            continue
        container["container_of_group"] = group["id"]
        group["container_node"] = container["id"]
        group["label"] = _group_mermaid_title(container)


def _group_mermaid_title(container: dict[str, Any]) -> str:
    """Single-line subgraph title for a folded node; subgraph titles get no Mermaid line break."""
    detail = _node_detail_line(container)
    title = f"{container['label']} {detail}" if detail else container["label"]
    return _clip_display(" ".join(title.split()), _MAX_GROUP_TITLE_CHARS)


def _safe_mermaid_id(raw_id: str, position: int, used: set[str]) -> str:
    """Build a deterministic Mermaid-safe identifier for a raw plan id."""
    candidate = _UNSAFE_ID_CHARS.sub("_", raw_id).strip("_")
    if not candidate or candidate[0].isdigit():
        candidate = f"n{position}_{candidate}" if candidate else f"n{position}"
    if candidate not in used:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in used:
        suffix += 1
    return f"{candidate}_{suffix}"


def _safe_label(value: Any) -> str:
    """Collapse whitespace, strip Mermaid-hostile characters and cap the length."""
    if value is None:
        return ""
    text = " ".join(_UNSAFE_LABEL_CHARS.sub(" ", str(value)).split())
    if len(text) > _MAX_LABEL_CHARS:
        text = text[: _MAX_LABEL_CHARS - 1].rstrip() + "…"
    return text


def _clip_display(text: str, limit: int) -> str:
    """Cap one rendered line; the sanitizing cap is far too wide for a readable node box."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _label_already_says(part: str, label: str) -> bool:
    """True when the primary label already tells the reader what ``part`` would repeat."""
    if not part:
        return True
    folded_part = part.casefold()
    folded_label = label.casefold()
    return folded_part == folded_label or folded_part in folded_label


def _node_detail_line(node: dict[str, Any]) -> str:
    """Product/role detail line with everything the primary label already carries removed.

    Models very often send ``label`` and ``product`` as the same product name (the skill's own
    example did), which used to render ``VPC`` above ``VPC · 虚拟私有网络``. Only the parts that add
    information survive here.
    """
    label = node["label"]
    product = node.get("product") or ""
    role = node.get("role") or ""
    parts: list[str] = []
    if not _label_already_says(product, label):
        parts.append(product)
    if not _label_already_says(role, label) and role.casefold() != product.casefold():
        parts.append(role)
    return " · ".join(parts)


def _node_mermaid_label(node: dict[str, Any]) -> str:
    """Primary label plus a deduplicated detail line, using the repo's Mermaid line break."""
    lines = [_clip_display(node["label"], _MAX_NODE_LINE_CHARS)]
    detail = _node_detail_line(node)
    if detail:
        lines.append(_clip_display(detail, _MAX_NODE_DETAIL_CHARS))
    return "\\n".join(lines)


def _render_plan_mermaid(plan: _ArchitecturePlan) -> str:
    lines = ["flowchart TD"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for node in plan.nodes:
        if node["group"]:
            grouped.setdefault(node["group"], []).append(node)

    for group in plan.groups:
        members = grouped.get(group["id"]) or []
        if not members:
            continue
        lines.append(f'  subgraph {group["id"]}["{group["label"]}"]')
        for node in members:
            lines.append(f'    {node["id"]}["{_node_mermaid_label(node)}"]')
        lines.append("  end")

    for node in plan.nodes:
        if node["group"] or node.get("container_of_group"):
            continue
        lines.append(f'  {node["id"]}["{_node_mermaid_label(node)}"]')

    for source, target, label in _rendered_edges(plan):
        if label:
            lines.append(f"  {source} -->|{label}| {target}")
        else:
            lines.append(f"  {source} --> {target}")

    return "\n".join(lines)


def _rendered_edges(plan: _ArchitecturePlan) -> list[tuple[str, str, str]]:
    """Edges as drawn: folded container nodes become their subgraph, containment arrows disappear."""
    container_groups = {node["id"]: node["container_of_group"] for node in plan.nodes if node.get("container_of_group")}
    if not container_groups:
        return [(edge["source"], edge["target"], edge["label"]) for edge in plan.edges]

    node_groups = {node["id"]: node["group"] for node in plan.nodes}
    rendered: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in plan.edges:
        source, target, label = edge["source"], edge["target"], edge["label"]
        source_group = container_groups.get(source)
        target_group = container_groups.get(target)
        # A "contains" arrow between a folded node and one of its own members is exactly what the
        # subgraph box already shows, so it is dropped instead of redrawn against the cluster.
        if source_group and node_groups.get(target) == source_group:
            continue
        if target_group and node_groups.get(source) == target_group:
            continue
        key = (source_group or source, target_group or target, label)
        if key[0] == key[1] or key in seen:
            continue
        seen.add(key)
        rendered.append(key)
    return rendered


def _plan_architecture_context(plan: _ArchitecturePlan) -> dict[str, Any]:
    return {
        "version": "1.0",
        "source": "architecture_plan",
        "nodes": [
            {
                "id": node["id"],
                "plan_id": node["raw_id"],
                "label": node["label"],
                "product": node["product"],
                "role": node["role"],
                "group": node["group"],
            }
            for node in plan.nodes
        ],
        "groups": [{"id": group["id"], "plan_id": group["raw_id"], "label": group["label"]} for group in plan.groups],
        "edges": list(plan.edges),
        "warnings": list(plan.warnings),
    }


async def _emit_plan_error_event(
    context: ToolContext,
    *,
    candidate_name: str,
    candidate_index: int | None,
    message: str,
) -> None:
    if context.event_queue is None:
        return
    mermaid_source = _error_plan_mermaid(message)
    await context.event_queue.put(
        DiagramEvent(
            candidate_name=candidate_name,
            template_content="",
            mermaid_source=mermaid_source,
            candidate_index=candidate_index,
            architecture_context={"error": message, "source": "architecture_plan"},
            diagram_stage="optimized",
            views=[
                {
                    "id": "overview",
                    "title": _("Architecture plan"),
                    "purpose": "",
                    "mermaid_source": mermaid_source,
                }
            ],
        )
    )


def _error_plan_mermaid(message: str) -> str:
    label = " ".join(str(message).split()) or _("Architecture plan unavailable")
    return "graph TD\n  ArchitecturePlanUnavailable[" + json.dumps(label, ensure_ascii=False) + "]"
