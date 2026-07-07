"""Rule candidate extraction for ROS architecture diagram metadata."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from iac_code.pipeline.engine.architecture_resource_inventory import ResourceInventoryItem, ResourceInventorySnapshot

RULE_CATEGORIES = (
    "container",
    "supplemental_relation",
    "display",
    "attachment",
    "bridge_attachment",
    "attachment_edge",
    "orchestration_action",
    "concept_node",
)

DECISION_PRIORITY = {
    "container": 100,
    "concept_node": 90,
    "orchestration_action": 80,
    "bridge_attachment": 70,
    "attachment_edge": 65,
    "attachment": 60,
    "supplemental_relation": 30,
    "display": 10,
}

HIGH_CONFIDENCE_DECISIONS = {
    "container",
    "concept_node",
    "orchestration_action",
    "bridge_attachment",
    "attachment_edge",
    "attachment",
}


@dataclass(frozen=True)
class RuleCandidate:
    category: str
    resource_type: str
    product_code: str
    property_name: str | None
    target_resource_types: tuple[str, ...]
    confidence: str
    evidence: tuple[str, ...]
    suggested_config: dict[str, Any]


@dataclass(frozen=True)
class ResourceTypeDecision:
    resource_type: str
    product_code: str
    decision: str
    source_state: str
    candidate_categories: tuple[str, ...]
    evidence: tuple[str, ...]


def extract_rule_candidates(snapshot: ResourceInventorySnapshot) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    api_items = [snapshot.items[resource_type] for resource_type in snapshot.api_resource_types]
    known_resource_types = tuple(snapshot.api_resource_types)

    for item in api_items:
        candidates.extend(_display_candidates(item))
        candidates.extend(_container_candidates(item))
        candidates.extend(_supplemental_relation_candidates(item))
        candidates.extend(_attachment_candidates(item))
        candidates.extend(_attachment_edge_candidates(item, known_resource_types))
        candidates.extend(_orchestration_action_candidates(item))
        candidates.extend(_concept_node_candidates(item))

    candidates.extend(_bridge_attachment_candidates(api_items))
    return _dedupe_candidates(candidates)


def build_resource_type_decisions(
    snapshot: ResourceInventorySnapshot,
    candidates: list[RuleCandidate],
) -> dict[str, ResourceTypeDecision]:
    candidates_by_type: dict[str, list[RuleCandidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_type[candidate.resource_type].append(candidate)

    decisions: dict[str, ResourceTypeDecision] = {}
    for resource_type in snapshot.api_resource_types:
        item = snapshot.items[resource_type]
        resource_candidates = sorted(
            candidates_by_type.get(resource_type, ()),
            key=lambda candidate: DECISION_PRIORITY.get(candidate.category, 0),
            reverse=True,
        )
        categories = tuple(dict.fromkeys(candidate.category for candidate in resource_candidates))
        if item.source_state == "api-only" and not _has_high_confidence_candidate(resource_candidates):
            decisions[resource_type] = ResourceTypeDecision(
                resource_type=resource_type,
                product_code=item.product_code,
                decision="needs_review",
                source_state=item.source_state,
                candidate_categories=categories,
                evidence=_api_only_evidence(item),
            )
            continue
        if resource_candidates:
            winner = resource_candidates[0]
            decisions[resource_type] = ResourceTypeDecision(
                resource_type=resource_type,
                product_code=item.product_code,
                decision=winner.category,
                source_state=item.source_state,
                candidate_categories=categories,
                evidence=winner.evidence,
            )
            continue
        decisions[resource_type] = ResourceTypeDecision(
            resource_type=resource_type,
            product_code=item.product_code,
            decision="core_node",
            source_state=item.source_state,
            candidate_categories=(),
            evidence=("No compacting rule candidate matched; keep as a visible resource node.",),
        )
    return decisions


def render_rule_candidate_report_markdown(
    snapshot: ResourceInventorySnapshot,
    candidates: list[RuleCandidate],
    decisions: dict[str, ResourceTypeDecision],
) -> str:
    lines = [
        "# ROS 架构图规则候选报告",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        f"| API resource types | {len(snapshot.api_resource_types)} |",
        f"| Local metadata resource types | {len(snapshot.local_resource_types)} |",
        f"| API-only resource types | {len(snapshot.api_only_resource_types)} |",
        f"| Local-only resource types | {len(snapshot.local_only_resource_types)} |",
        f"| Rule candidates | {len(candidates)} |",
        "",
        "## Candidate Categories",
        "",
    ]

    candidates_by_category: dict[str, list[RuleCandidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_category[candidate.category].append(candidate)
    for category in RULE_CATEGORIES:
        category_candidates = sorted(candidates_by_category.get(category, ()), key=_candidate_sort_key)
        if not category_candidates:
            continue
        lines.extend(
            [
                f"### {category}",
                "",
                "| Resource type | Confidence | Targets | Evidence |",
                "| --- | --- | --- | --- |",
            ]
        )
        for candidate in category_candidates:
            evidence = "; ".join(candidate.evidence[:2]).replace("|", "\\|")
            targets = ", ".join(f"`{target}`" for target in candidate.target_resource_types) or "-"
            lines.append(f"| `{candidate.resource_type}` | `{candidate.confidence}` | {targets} | {evidence} |")
        lines.append("")

    lines.extend(["## Decisions by Product", ""])
    decisions_by_product: dict[str, list[ResourceTypeDecision]] = defaultdict(list)
    for decision in decisions.values():
        decisions_by_product[decision.product_code].append(decision)
    for product_code in sorted(decisions_by_product):
        lines.extend(
            [
                f"### `{product_code}`",
                "",
                "| Resource type | Decision | Source | Evidence |",
                "| --- | --- | --- | --- |",
            ]
        )
        for decision in sorted(decisions_by_product[product_code], key=lambda value: value.resource_type):
            evidence = "; ".join(decision.evidence[:2]).replace("|", "\\|")
            lines.append(
                f"| `{decision.resource_type}` | `{decision.decision}` | `{decision.source_state}` | {evidence} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _display_candidates(item: ResourceInventoryItem) -> list[RuleCandidate]:
    label = item.name_zh or item.name_en or _short_resource_type(item.resource_type)
    evidence = [f"Default display label: {label}."]
    if item.detail is not None and item.detail.description:
        evidence.append(_shorten_text(item.detail.description))
    return [
        RuleCandidate(
            category="display",
            resource_type=item.resource_type,
            product_code=item.product_code,
            property_name=None,
            target_resource_types=(),
            confidence="medium" if item.meta is not None else "low",
            evidence=tuple(evidence),
            suggested_config={"label": {"zh": item.name_zh, "en": item.name_en}},
        )
    ]


def _container_candidates(item: ResourceInventoryItem) -> list[RuleCandidate]:
    short_type = _short_resource_type(item.resource_type)
    explicit = {
        "ALIYUN::ECS::VPC",
        "ALIYUN::ECS::VSwitch",
        "ALIYUN::ECS::SecurityGroup",
        "ALIYUN::CS::Cluster",
        "ALIYUN::ACS::Cluster",
    }
    if item.resource_type not in explicit and not _looks_like_container_identity(item):
        return []
    text = _container_name_text(item)
    role = _container_role(short_type, text)
    return [
        RuleCandidate(
            category="container",
            resource_type=item.resource_type,
            product_code=item.product_code,
            property_name=None,
            target_resource_types=(),
            confidence="high" if item.resource_type in explicit else "medium",
            evidence=(f"{item.resource_type} looks like a {role} boundary/container.",),
            suggested_config={
                "network_layer_types": [item.resource_type],
                "containment_layer_types": {role: [item.resource_type]},
            },
        )
    ]


def _supplemental_relation_candidates(item: ResourceInventoryItem) -> list[RuleCandidate]:
    known_names = set(_related_properties(item))
    candidates: list[RuleCandidate] = []
    for name, prop in _detail_properties(item).items():
        if name in known_names or not _looks_like_reference_property(name, prop):
            continue
        candidates.append(
            RuleCandidate(
                category="supplemental_relation",
                resource_type=item.resource_type,
                product_code=item.product_code,
                property_name=name,
                target_resource_types=(),
                confidence="low",
                evidence=(
                    f"GetResourceType property `{name}` looks like a reference but local RelatedTo is missing.",
                    _shorten_text(str(prop.get("Description") or "")),
                ),
                suggested_config={"supplemental_related_properties": {item.resource_type: {name: []}}},
            )
        )
    return candidates


def _attachment_candidates(item: ResourceInventoryItem) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    meta = item.meta
    if meta is not None and meta.main_resource_type is not None:
        main_type = meta.main_resource_type.resource_type
        candidates.append(
            RuleCandidate(
                category="attachment",
                resource_type=item.resource_type,
                product_code=item.product_code,
                property_name=meta.main_resource_type.ref_property,
                target_resource_types=(main_type,),
                confidence="high",
                evidence=(f"MainResourceType points to `{main_type}`.",),
                suggested_config={"compact_attachment_marker_types": [main_type]},
            )
        )

    short_type = _short_resource_type(item.resource_type)
    text = _resource_identity_text(item)
    relation_targets = _all_relation_targets(item)
    if not relation_targets:
        return candidates
    is_attachment_name = any(
        token in short_type.lower()
        for token in (
            "acl",
            "addon",
            "association",
            "attachment",
            "binding",
            "addition",
            "entry",
            "rule",
            "securityip",
            "whitelist",
        )
    )
    is_attachment_desc = any(
        token in text for token in ("associate", "attach", "bind", "configure", "entry", "rule", "whitelist")
    )
    if is_attachment_name or is_attachment_desc:
        candidates.append(
            RuleCandidate(
                category="attachment",
                resource_type=item.resource_type,
                product_code=item.product_code,
                property_name=None,
                target_resource_types=tuple(sorted(relation_targets)),
                confidence="medium",
                evidence=(f"Name/description indicates auxiliary resource `{short_type}`.",),
                suggested_config={
                    "compact_child_attachments": [
                        {
                            "resource_types": [item.resource_type],
                            "target_types": sorted(relation_targets),
                        }
                    ]
                },
            )
        )
    return candidates


def _attachment_edge_candidates(
    item: ResourceInventoryItem,
    known_resource_types: tuple[str, ...],
) -> list[RuleCandidate]:
    related = _related_properties(item)
    if not related:
        return []
    names = _all_property_names(item)
    package_props = [name for name in names if "package" in name.lower() or "bandwidth" in name.lower()]
    marker_props = [name for name in names if "eip" in name.lower() or "ip" == name.lower()]
    if not package_props or not marker_props:
        return []
    source_targets = sorted({target for name in package_props for target in related.get(name, ())})
    marker_targets = sorted({target for name in marker_props for target in related.get(name, ())})
    if not source_targets:
        source_targets = sorted(_infer_property_targets(item, package_props, known_resource_types))
    if not marker_targets:
        marker_targets = sorted(_infer_property_targets(item, marker_props, known_resource_types))
    if not source_targets or not marker_targets:
        return []
    return [
        RuleCandidate(
            category="attachment_edge",
            resource_type=item.resource_type,
            product_code=item.product_code,
            property_name=None,
            target_resource_types=tuple(source_targets + marker_targets),
            confidence="high",
            evidence=(
                "Resource links an aggregate capability resource with marker resources.",
                f"Source properties: {', '.join(package_props)}; marker properties: {', '.join(marker_props)}.",
            ),
            suggested_config={
                "resource_types": [item.resource_type],
                "source_properties": package_props,
                "marker_properties": marker_props,
                "source_types": source_targets,
                "marker_types": marker_targets,
                "edge_style": "dotted_open",
            },
        )
    ]


def _orchestration_action_candidates(item: ResourceInventoryItem) -> list[RuleCandidate]:
    text = _resource_identity_text(item)
    short_type = _short_resource_type(item.resource_type)
    action_terms = ("command", "invocation", "run", "enable", "deploy", "execute", "execution")
    if not any(term in text for term in action_terms):
        return []
    target_properties = tuple(
        name for name in _all_property_names(item) if name.lower() in {"instanceid", "instanceids", "targets"}
    )
    command_properties = tuple(name for name in _all_property_names(item) if "command" in name.lower())
    return [
        RuleCandidate(
            category="orchestration_action",
            resource_type=item.resource_type,
            product_code=item.product_code,
            property_name=None,
            target_resource_types=tuple(sorted(_all_relation_targets(item))),
            confidence="high" if "command" in short_type.lower() else "medium",
            evidence=(f"`{short_type}` describes an action/orchestration resource.",),
            suggested_config={
                "resource_types": [item.resource_type],
                "command_properties": command_properties,
                "target_properties": target_properties,
            },
        )
    ]


def _concept_node_candidates(item: ResourceInventoryItem) -> list[RuleCandidate]:
    text = _item_text(item)
    props = set(_all_property_names(item))
    has_controller = any(name.lower().endswith("groupid") for name in props)
    has_source = any(name.lower() == "instanceid" for name in props)
    if "scaling" not in text or not has_controller or not has_source:
        return []
    return [
        RuleCandidate(
            category="concept_node",
            resource_type=item.resource_type,
            product_code=item.product_code,
            property_name=None,
            target_resource_types=tuple(sorted(_all_relation_targets(item))),
            confidence="medium",
            evidence=("Scaling configuration links a controller group with a source ECS instance.",),
            suggested_config={
                "via_resource_types": [item.resource_type],
                "controller_property": _first_matching_property(props, "groupid"),
                "source_property": "InstanceId",
            },
        )
    ]


def _bridge_attachment_candidates(items: list[ResourceInventoryItem]) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    for source in items:
        source_related = _related_properties(source)
        if not source_related:
            continue
        source_props = set(_all_property_names(source))
        if not _looks_like_bridge_source(source):
            continue
        for via in items:
            if via.resource_type == source.resource_type or via.product_code != source.product_code:
                continue
            via_props = set(_all_property_names(via))
            shared_props = sorted(source_props & via_props)
            if not shared_props:
                continue
            via_related = _related_properties(via)
            terminal_targets = sorted(
                {
                    target
                    for prop_name, targets in via_related.items()
                    if prop_name not in shared_props
                    for target in targets
                    if target != source.resource_type
                    and target
                    not in {
                        "ALIYUN::ECS::VPC",
                        "ALIYUN::ECS::VSwitch",
                        "ALIYUN::ECS::SecurityGroup",
                    }
                }
            )
            if not terminal_targets:
                continue
            candidates.append(
                RuleCandidate(
                    category="bridge_attachment",
                    resource_type=source.resource_type,
                    product_code=source.product_code,
                    property_name=shared_props[0],
                    target_resource_types=tuple(terminal_targets),
                    confidence="medium",
                    evidence=(
                        f"`{source.resource_type}` shares `{shared_props[0]}` with `{via.resource_type}`.",
                        f"`{via.resource_type}` links onward to {', '.join(terminal_targets)}.",
                    ),
                    suggested_config={
                        "resource_types": [source.resource_type],
                        "source_properties": shared_props,
                        "via_resource_types": [via.resource_type],
                        "via_source_properties": shared_props,
                        "via_target_properties": [
                            prop_name for prop_name in via_related if prop_name not in shared_props
                        ],
                        "target_types": terminal_targets,
                    },
                )
            )
    return candidates


def _dedupe_candidates(candidates: list[RuleCandidate]) -> list[RuleCandidate]:
    seen: set[tuple[str, str, str | None, tuple[str, ...]]] = set()
    deduped: list[RuleCandidate] = []
    for candidate in candidates:
        key = (
            candidate.category,
            candidate.resource_type,
            candidate.property_name,
            candidate.target_resource_types,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return sorted(deduped, key=_candidate_sort_key)


def _candidate_sort_key(candidate: RuleCandidate) -> tuple[str, str, str]:
    return (candidate.category, candidate.product_code, candidate.resource_type)


def _related_properties(item: ResourceInventoryItem) -> dict[str, tuple[str, ...]]:
    if item.meta is None:
        return {}
    return {prop.name: prop.targets for prop in item.meta.related_properties}


def _infer_property_targets(
    item: ResourceInventoryItem,
    property_names: list[str],
    known_resource_types: tuple[str, ...],
) -> set[str]:
    detail_properties = _detail_properties(item)
    inferred: set[str] = set()
    for property_name in property_names:
        prop = detail_properties.get(property_name, {})
        text = "{} {}".format(property_name, prop.get("Description") or "").lower()
        for resource_type in known_resource_types:
            if resource_type == item.resource_type:
                continue
            if _property_text_mentions_resource(text, resource_type):
                inferred.add(resource_type)
    return inferred


def _property_text_mentions_resource(text: str, resource_type: str) -> bool:
    short_type = _short_resource_type(resource_type)
    if short_type.lower() in {
        "cluster",
        "group",
        "instance",
        "instancegroup",
        "namespace",
        "resourcegroup",
        "rule",
        "vpc",
        "vswitch",
        "workspace",
    }:
        return False
    aliases = {short_type.lower()}
    aliases.add(_split_camel_case(short_type).lower())
    if short_type.lower() == "eip":
        aliases.update({"eips", "elastic ip", "elastic public ip"})
    return any(len(alias) >= 3 and alias in text for alias in aliases)


def _split_camel_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", value)


def _detail_properties(item: ResourceInventoryItem) -> dict[str, dict[str, Any]]:
    if item.detail is None:
        return {}
    return item.detail.properties


def _all_property_names(item: ResourceInventoryItem) -> tuple[str, ...]:
    names = {*_related_properties(item), *_detail_properties(item)}
    return tuple(sorted(names))


def _all_relation_targets(item: ResourceInventoryItem) -> set[str]:
    return {target for targets in _related_properties(item).values() for target in targets}


def _looks_like_reference_property(name: str, prop: dict[str, Any]) -> bool:
    lowered_name = name.lower()
    if lowered_name.endswith(("id", "ids", "name", "names")):
        return True
    description = str(prop.get("Description") or "").lower()
    return " id of " in description or "the id of" in description or "name of" in description


def _looks_like_bridge_source(item: ResourceInventoryItem) -> bool:
    text = _item_text(item)
    short_type = _short_resource_type(item.resource_type).lower()
    return any(token in short_type for token in ("rule", "policy", "access")) or any(
        token in text for token in ("permission", "access", "policy", "rule")
    )


def _has_high_confidence_candidate(candidates: list[RuleCandidate]) -> bool:
    return any(candidate.category in HIGH_CONFIDENCE_DECISIONS for candidate in candidates)


def _api_only_evidence(item: ResourceInventoryItem) -> tuple[str, ...]:
    evidence = ["Resource type exists in ListResourceTypes but not in local synced metadata."]
    if item.detail is not None and item.detail.description:
        evidence.append(_shorten_text(item.detail.description))
    elif item.detail is not None and item.detail.properties:
        evidence.append("GetResourceType returned properties: {}.".format(", ".join(item.detail.properties)))
    else:
        evidence.append("No local RelatedTo/MainResourceType evidence is available.")
    return tuple(evidence)


def _item_text(item: ResourceInventoryItem) -> str:
    parts = [item.resource_type, item.name_en or "", item.name_zh or ""]
    if item.detail is not None:
        parts.append(item.detail.description or "")
        for name, prop in item.detail.properties.items():
            parts.append(name)
            parts.append(str(prop.get("Description") or ""))
    return " ".join(parts).lower()


def _resource_identity_text(item: ResourceInventoryItem) -> str:
    parts = [item.resource_type, item.name_en or "", item.name_zh or ""]
    if item.detail is not None and item.detail.description:
        parts.append(item.detail.description)
    return " ".join(parts).lower()


def _looks_like_container_identity(item: ResourceInventoryItem) -> bool:
    short_type = _short_resource_type(item.resource_type)
    lowered = short_type.lower()
    excluded_terms = (
        "access",
        "account",
        "addon",
        "application",
        "association",
        "attachment",
        "backup",
        "binding",
        "config",
        "endpoint",
        "entry",
        "policy",
        "rule",
        "securityip",
        "whitelist",
    )
    if any(term in lowered for term in excluded_terms):
        return False
    exact_container_types = {"vpc", "vswitch", "securitygroup", "networkacl", "namespace", "workspace"}
    if lowered in exact_container_types:
        return True
    if lowered.endswith(("cluster", "cluster2", "clusterv2", "namespace", "workspace")):
        return True
    name_text = _container_name_text(item)
    return any(term in name_text for term in ("专有网络", "交换机", "命名空间"))


def _container_name_text(item: ResourceInventoryItem) -> str:
    parts = [_short_resource_type(item.resource_type), item.name_en or "", item.name_zh or ""]
    return " ".join(parts).lower()


def _short_resource_type(resource_type: str) -> str:
    return resource_type.split("::")[-1]


def _container_role(short_type: str, text: str) -> str:
    lowered = short_type.lower()
    if "vswitch" in lowered or "vswitch" in text:
        return "vswitch"
    if "securitygroup" in lowered or "security group" in text:
        return "security_group"
    if "cluster" in lowered:
        return "cluster"
    if "workspace" in lowered:
        return "workspace"
    if "namespace" in lowered:
        return "namespace"
    return "vpc" if "vpc" in lowered or "vpc" in text else "container"


def _first_matching_property(properties: set[str], suffix: str) -> str:
    return next((name for name in sorted(properties) if name.lower().endswith(suffix)), sorted(properties)[0])


def _shorten_text(value: str, max_length: int = 180) -> str:
    compact = " ".join(value.split())
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 3] + "..."
