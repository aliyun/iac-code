"""Renderer-neutral DiagramGraph construction helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def build_diagram_graph(
    *,
    nodes: Sequence[Mapping[str, Any]],
    containers: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    direction: str = "LR",
) -> dict[str, Any]:
    """Build the shared, versioned graph payload used by Web diagram views."""
    return {
        "version": 1,
        "nodes": [dict(node) for node in nodes],
        "containers": [dict(container) for container in containers],
        "edges": [dict(edge) for edge in edges],
        "layout": {"direction": direction},
    }


def project_plan_diagram_graph(
    *,
    nodes: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    node_labels: Mapping[str, str],
    group_anchor_labels: Mapping[str, str],
) -> dict[str, Any]:
    """Project sanitized planning topology into DiagramGraph v1.

    Planning models often emit a resource node whose raw id is also used as a group name.
    DiagramGraph can represent that node as the group container, including nested groups, so the
    graph does not repeat the same resource as a box, a container, and a containment edge.
    """
    nodes_by_raw_id = {str(node["raw_id"]).casefold(): node for node in nodes}
    group_anchors: dict[str, Mapping[str, Any]] = {}
    for group in groups:
        group_id = str(group["id"])
        anchor = nodes_by_raw_id.get(str(group["raw_id"]).casefold())
        if anchor is not None and anchor.get("group") != group_id:
            group_anchors[group_id] = anchor

    # Reject malformed mutual containment instead of emitting a cyclic parentId chain.
    def has_anchor_cycle(group_id: str) -> bool:
        seen: set[str] = set()
        current: str | None = group_id
        while current is not None and current in group_anchors:
            if current in seen:
                return True
            seen.add(current)
            parent = group_anchors[current].get("group")
            current = str(parent) if parent is not None else None
        return False

    group_anchors = {group_id: anchor for group_id, anchor in group_anchors.items() if not has_anchor_cycle(group_id)}
    folded_groups = {str(anchor["id"]): group_id for group_id, anchor in group_anchors.items()}

    graph_nodes: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node["id"])
        if node_id in folded_groups:
            continue
        item: dict[str, Any] = {
            "id": node_id,
            "label": node_labels.get(node_id, str(node.get("label") or node_id)),
            "resourceType": "",
            "parentId": node.get("group"),
        }
        if node.get("product"):
            item["product"] = node["product"]
        if node.get("role"):
            item["role"] = node["role"]
        graph_nodes.append(item)

    container_parents = {
        str(group["id"]): group_anchors[str(group["id"])].get("group") if str(group["id"]) in group_anchors else None
        for group in groups
    }

    def is_descendant(item_parent: object, container_id: str) -> bool:
        current = str(item_parent) if item_parent is not None else None
        seen: set[str] = set()
        while current is not None and current not in seen:
            if current == container_id:
                return True
            seen.add(current)
            current = container_parents.get(current)
        return False

    graph_edges: list[tuple[str, str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    node_groups = {str(node["id"]): node.get("group") for node in nodes}
    for edge in edges:
        raw_source = str(edge["source"])
        raw_target = str(edge["target"])
        label = str(edge.get("label") or "")
        source = folded_groups.get(raw_source, raw_source)
        target = folded_groups.get(raw_target, raw_target)
        if source == target:
            continue
        if raw_source in folded_groups and is_descendant(node_groups.get(raw_target), source):
            continue
        if raw_target in folded_groups and is_descendant(node_groups.get(raw_source), target):
            continue
        key = (source, target, label)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        graph_edges.append(key)

    graph_containers: list[dict[str, Any]] = []
    for group in groups:
        group_id = str(group["id"])
        if not any(node.get("group") == group_id for node in nodes):
            continue
        anchor = group_anchors.get(group_id)
        anchor_id = str(anchor["id"]) if anchor is not None else ""
        graph_containers.append(
            {
                "id": group_id,
                "label": group_anchor_labels.get(anchor_id, str(group.get("label") or group_id)),
                "resourceType": "",
                "parentId": container_parents[group_id],
            }
        )

    return build_diagram_graph(
        nodes=graph_nodes,
        containers=graph_containers,
        edges=[
            {
                "id": f"edge_{index}_{source}_{target}",
                "from": source,
                "to": target,
                "label": label,
                "style": "solid_arrow",
            }
            for index, (source, target, label) in enumerate(graph_edges, start=1)
        ],
    )
