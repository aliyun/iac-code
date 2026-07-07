"""Fixed supplemental rules for architecture diagram rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

RuleLabel = str | dict[str, str]


@dataclass(frozen=True)
class EdgeStyles:
    auxiliary_relation: str
    direct_relation: str


@dataclass(frozen=True)
class CompactRelationFold:
    via_resource_types: frozenset[str]
    source_property: str
    target_property: str
    edge_style: str
    edge_label: RuleLabel | None
    render_as: str = "edge"


@dataclass(frozen=True)
class CompactChildAttachment:
    resource_types: tuple[str, ...]
    target_properties: tuple[str, ...]
    target_types: tuple[str, ...]
    label: RuleLabel
    keep_when_bridge_via: bool = False


@dataclass(frozen=True)
class CompactBridgeAttachment:
    resource_types: tuple[str, ...]
    source_properties: tuple[str, ...]
    via_resource_types: tuple[str, ...]
    via_source_properties: tuple[str, ...]
    via_target_properties: tuple[str, ...]
    target_types: tuple[str, ...]
    label: RuleLabel
    target_self: bool = False


@dataclass(frozen=True)
class CompactAttachmentEdge:
    resource_types: tuple[str, ...]
    source_properties: tuple[str, ...]
    marker_properties: tuple[str, ...]
    source_types: tuple[str, ...]
    marker_types: tuple[str, ...]
    edge_style: str
    edge_label: RuleLabel | None


@dataclass(frozen=True)
class CompactOrchestrationAction:
    resource_types: tuple[str, ...]
    command_properties: tuple[str, ...]
    target_properties: tuple[str, ...]
    evidence_properties: tuple[str, ...]


@dataclass(frozen=True)
class ConceptEdgeRule:
    style: str
    label: RuleLabel | None
    target: str


@dataclass(frozen=True)
class CompactConceptGroup:
    id_suffix: str
    resource_type: str
    label: RuleLabel
    members: tuple[str, ...]
    rewrite_edge_kinds: tuple[str, ...]


@dataclass(frozen=True)
class CompactConceptNode:
    via_resource_types: frozenset[str]
    controller_property: str
    source_property: str
    id_suffix: str
    resource_type: str
    label: RuleLabel
    controller_edge: ConceptEdgeRule | None
    source_edge: ConceptEdgeRule | None
    group: CompactConceptGroup | None


@dataclass(frozen=True)
class ArchitectureRules:
    """Supplemental rendering rules kept separate from synced resource metadata."""

    network_layer_types: frozenset[str]
    containment_layer_types: dict[str, tuple[str, ...]]
    legacy_auxiliary_short_types: frozenset[str]
    legacy_resource_labels: dict[str, str]
    legacy_layer_labels: dict[str, str]
    fallback_related_properties: dict[str, tuple[str, ...]]
    supplemental_related_properties: dict[str, dict[str, tuple[str, ...]]]
    compact_hidden_short_types: frozenset[str]
    compact_resource_labels: dict[str, str]
    compact_attachment_marker_types: frozenset[str]
    compact_child_attachments: tuple[CompactChildAttachment, ...]
    compact_bridge_attachments: tuple[CompactBridgeAttachment, ...]
    compact_attachment_edges: tuple[CompactAttachmentEdge, ...]
    compact_orchestration_actions: tuple[CompactOrchestrationAction, ...]
    compact_flatten_layer_roles: frozenset[str]
    attachment_label_prefix: str
    edge_styles: EdgeStyles
    compact_relation_folds: tuple[CompactRelationFold, ...]
    compact_concept_nodes: tuple[CompactConceptNode, ...]
    edge_operators: dict[str, str]

    _default: ClassVar[ArchitectureRules | None] = None

    @classmethod
    def load_default(cls) -> ArchitectureRules:
        if cls._default is None:
            cls._default = cls.from_file(Path(__file__).with_name("architecture_rules.json"))
        return cls._default

    @classmethod
    def from_file(cls, path: Path) -> ArchitectureRules:
        return cls.from_raw(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_raw(cls, raw: Any) -> ArchitectureRules:
        if not isinstance(raw, dict):
            raise ValueError("architecture rules must be a JSON object")
        edge_styles = _dict_value(raw, "edge_styles")
        return cls(
            network_layer_types=_string_set(raw.get("network_layer_types")),
            containment_layer_types=_string_tuple_map(raw.get("containment_layer_types")),
            legacy_auxiliary_short_types=_string_set(raw.get("legacy_auxiliary_short_types")),
            legacy_resource_labels=_string_map(raw.get("legacy_resource_labels")),
            legacy_layer_labels=_string_map(raw.get("legacy_layer_labels")),
            fallback_related_properties=_string_tuple_map(raw.get("fallback_related_properties")),
            supplemental_related_properties=_nested_string_tuple_map(raw.get("supplemental_related_properties")),
            compact_hidden_short_types=_string_set(raw.get("compact_hidden_short_types")),
            compact_resource_labels=_string_map(raw.get("compact_resource_labels")),
            compact_attachment_marker_types=_string_set(raw.get("compact_attachment_marker_types")),
            compact_child_attachments=tuple(_iter_compact_child_attachments(raw.get("compact_child_attachments"))),
            compact_bridge_attachments=tuple(_iter_compact_bridge_attachments(raw.get("compact_bridge_attachments"))),
            compact_attachment_edges=tuple(_iter_compact_attachment_edges(raw.get("compact_attachment_edges"))),
            compact_orchestration_actions=tuple(
                _iter_compact_orchestration_actions(raw.get("compact_orchestration_actions"))
            ),
            compact_flatten_layer_roles=_string_set(raw.get("compact_flatten_layer_roles")),
            attachment_label_prefix=(
                raw.get("attachment_label_prefix") if isinstance(raw.get("attachment_label_prefix"), str) else ""
            ),
            edge_styles=EdgeStyles(
                auxiliary_relation=_string_value(edge_styles, "auxiliary_relation"),
                direct_relation=_string_value(edge_styles, "direct_relation"),
            ),
            compact_relation_folds=tuple(_iter_compact_relation_folds(raw.get("compact_relation_folds"))),
            compact_concept_nodes=tuple(_iter_compact_concept_nodes(raw.get("compact_concept_nodes"))),
            edge_operators=_string_map(raw.get("edge_operators")),
        )

    def edge_operator(self, style: str) -> str:
        return self.edge_operators.get(style, "-->")

    def containment_types(self, role: str) -> tuple[str, ...]:
        return self.containment_layer_types.get(role, ())

    @property
    def compact_orchestration_action_types(self) -> frozenset[str]:
        return frozenset(
            resource_type for action in self.compact_orchestration_actions for resource_type in action.resource_types
        )


def _dict_value(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _string_value(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def _string_set(raw: Any) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item for item in raw if isinstance(item, str))


def _string_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def _string_tuple_map(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        result[key] = tuple(item for item in value if isinstance(item, str))
    return result


def _nested_string_tuple_map(raw: Any) -> dict[str, dict[str, tuple[str, ...]]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for key, value in raw.items():
        nested = _string_tuple_map(value)
        if isinstance(key, str) and nested:
            result[key] = nested
    return result


def _iter_compact_relation_folds(raw: Any) -> list[CompactRelationFold]:
    if not isinstance(raw, list):
        return []
    folds: list[CompactRelationFold] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        via_resource_types = _string_set(item.get("via_resource_types"))
        source_property = item.get("source_property")
        target_property = item.get("target_property")
        edge_style = item.get("edge_style")
        edge_label = _rule_label(item.get("edge_label"))
        render_as = item.get("render_as") if isinstance(item.get("render_as"), str) else "edge"
        if not via_resource_types or not isinstance(source_property, str) or not isinstance(target_property, str):
            continue
        if not isinstance(edge_style, str):
            continue
        folds.append(
            CompactRelationFold(
                via_resource_types=via_resource_types,
                source_property=source_property,
                target_property=target_property,
                edge_style=edge_style,
                edge_label=edge_label,
                render_as=render_as,
            )
        )
    return folds


def _iter_compact_child_attachments(raw: Any) -> list[CompactChildAttachment]:
    if not isinstance(raw, list):
        return []
    attachments: list[CompactChildAttachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        resource_types = _string_tuple(item.get("resource_types"))
        target_properties = _string_tuple(item.get("target_properties"))
        if not target_properties:
            target_property = item.get("target_property")
            target_properties = (target_property,) if isinstance(target_property, str) else ()
        target_types = _string_tuple(item.get("target_types"))
        label = _rule_label(item.get("label"))
        keep_when_bridge_via = item.get("keep_when_bridge_via") is True
        if not resource_types or not target_properties or not target_types or label is None:
            continue
        attachments.append(
            CompactChildAttachment(
                resource_types=resource_types,
                target_properties=target_properties,
                target_types=target_types,
                label=label,
                keep_when_bridge_via=keep_when_bridge_via,
            )
        )
    return attachments


def _iter_compact_bridge_attachments(raw: Any) -> list[CompactBridgeAttachment]:
    if not isinstance(raw, list):
        return []
    attachments: list[CompactBridgeAttachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        resource_types = _string_tuple(item.get("resource_types"))
        source_properties = _string_tuple(item.get("source_properties"))
        via_resource_types = _string_tuple(item.get("via_resource_types"))
        via_source_properties = _string_tuple(item.get("via_source_properties"))
        via_target_properties = _string_tuple(item.get("via_target_properties"))
        target_types = _string_tuple(item.get("target_types"))
        label = _rule_label(item.get("label"))
        target_self = item.get("target_self") is True
        if (
            not resource_types
            or not via_resource_types
            or not via_source_properties
            or (not via_target_properties and not target_self)
            or not target_types
            or label is None
        ):
            continue
        attachments.append(
            CompactBridgeAttachment(
                resource_types=resource_types,
                source_properties=source_properties,
                via_resource_types=via_resource_types,
                via_source_properties=via_source_properties,
                via_target_properties=via_target_properties,
                target_types=target_types,
                label=label,
                target_self=target_self,
            )
        )
    return attachments


def _iter_compact_attachment_edges(raw: Any) -> list[CompactAttachmentEdge]:
    if not isinstance(raw, list):
        return []
    edges: list[CompactAttachmentEdge] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        resource_types = _string_tuple(item.get("resource_types"))
        source_properties = _string_tuple(item.get("source_properties"))
        marker_properties = _string_tuple(item.get("marker_properties"))
        source_types = _string_tuple(item.get("source_types"))
        marker_types = _string_tuple(item.get("marker_types"))
        edge_style = item.get("edge_style")
        edge_label = _rule_label(item.get("edge_label"))
        if not resource_types or not source_properties or not marker_properties or not isinstance(edge_style, str):
            continue
        edges.append(
            CompactAttachmentEdge(
                resource_types=resource_types,
                source_properties=source_properties,
                marker_properties=marker_properties,
                source_types=source_types,
                marker_types=marker_types,
                edge_style=edge_style,
                edge_label=edge_label,
            )
        )
    return edges


def _iter_compact_orchestration_actions(raw: Any) -> list[CompactOrchestrationAction]:
    if not isinstance(raw, list):
        return []
    actions: list[CompactOrchestrationAction] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        resource_types = _string_tuple(item.get("resource_types"))
        if not resource_types:
            continue
        actions.append(
            CompactOrchestrationAction(
                resource_types=resource_types,
                command_properties=_string_tuple(item.get("command_properties")),
                target_properties=_string_tuple(item.get("target_properties")),
                evidence_properties=_string_tuple(item.get("evidence_properties")),
            )
        )
    return actions


def _iter_compact_concept_nodes(raw: Any) -> list[CompactConceptNode]:
    if not isinstance(raw, list):
        return []
    nodes: list[CompactConceptNode] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        via_resource_types = _string_set(item.get("via_resource_types"))
        controller_property = item.get("controller_property")
        source_property = item.get("source_property")
        id_suffix = item.get("id_suffix")
        resource_type = item.get("resource_type")
        label = _rule_label(item.get("label"))
        if (
            not via_resource_types
            or not isinstance(controller_property, str)
            or not isinstance(source_property, str)
            or not isinstance(id_suffix, str)
            or not isinstance(resource_type, str)
            or label is None
        ):
            continue
        nodes.append(
            CompactConceptNode(
                via_resource_types=via_resource_types,
                controller_property=controller_property,
                source_property=source_property,
                id_suffix=id_suffix,
                resource_type=resource_type,
                label=label,
                controller_edge=_concept_edge_rule(item.get("controller_edge")),
                source_edge=_concept_edge_rule(item.get("source_edge")),
                group=_concept_group(item.get("group")),
            )
        )
    return nodes


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _concept_edge_rule(raw: Any) -> ConceptEdgeRule | None:
    if not isinstance(raw, dict):
        return None
    style = raw.get("style")
    label = _rule_label(raw.get("label"))
    if not isinstance(style, str):
        return None
    target = raw.get("target")
    if target not in {"concept", "controller", "source"}:
        target = "concept"
    return ConceptEdgeRule(style=style, label=label, target=target)


def _concept_group(raw: Any) -> CompactConceptGroup | None:
    if not isinstance(raw, dict):
        return None
    id_suffix = raw.get("id_suffix")
    resource_type = raw.get("resource_type")
    label = _rule_label(raw.get("label"))
    members = _string_tuple(raw.get("members"))
    rewrite_edge_kinds = _string_tuple(raw.get("rewrite_edge_kinds"))
    if (
        not isinstance(id_suffix, str)
        or not isinstance(resource_type, str)
        or label is None
        or not members
        or not rewrite_edge_kinds
    ):
        return None
    return CompactConceptGroup(
        id_suffix=id_suffix,
        resource_type=resource_type,
        label=label,
        members=members,
        rewrite_edge_kinds=rewrite_edge_kinds,
    )


def _rule_label(raw: Any) -> RuleLabel | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        labels = {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}
        if labels:
            return labels
    return None
