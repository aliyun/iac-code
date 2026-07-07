"""LLM input facts and deterministic rule signals for ROS architecture rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iac_code.pipeline.engine.architecture_resource_inventory import (
    ResourceInventoryItem,
    ResourceInventorySnapshot,
)
from iac_code.pipeline.engine.architecture_rule_candidates import RuleCandidate, extract_rule_candidates
from iac_code.pipeline.engine.architecture_rules import ArchitectureRules


@dataclass(frozen=True)
class ResourceRuleFact:
    resource_type: str
    product_code: str
    source_state: str
    name: dict[str, str | None]
    description: str | None
    category_code: str | None
    properties: dict[str, dict[str, Any]]
    related_properties: dict[str, list[str]]
    main_resource_type: dict[str, str] | None
    fixed_rule_hits: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "product_code": self.product_code,
            "source_state": self.source_state,
            "name": self.name,
            "description": self.description,
            "category_code": self.category_code,
            "properties": self.properties,
            "related_properties": self.related_properties,
            "main_resource_type": self.main_resource_type,
            "fixed_rule_hits": list(self.fixed_rule_hits),
        }


@dataclass(frozen=True)
class RuleSignal:
    category: str
    resource_type: str
    product_code: str
    property_name: str | None
    target_resource_types: tuple[str, ...]
    confidence: str
    evidence: tuple[str, ...]
    suggested_patch: dict[str, Any]

    @classmethod
    def from_candidate(cls, candidate: RuleCandidate) -> RuleSignal:
        return cls(
            category=candidate.category,
            resource_type=candidate.resource_type,
            product_code=candidate.product_code,
            property_name=candidate.property_name,
            target_resource_types=candidate.target_resource_types,
            confidence=candidate.confidence,
            evidence=candidate.evidence,
            suggested_patch=candidate.suggested_config,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "resource_type": self.resource_type,
            "product_code": self.product_code,
            "property_name": self.property_name,
            "target_resource_types": list(self.target_resource_types),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "suggested_patch": self.suggested_patch,
        }


@dataclass(frozen=True)
class ResourceRuleFactsBundle:
    resource_facts: tuple[ResourceRuleFact, ...]
    rule_signals: tuple[RuleSignal, ...]
    api_resource_type_count: int
    local_resource_type_count: int
    api_only_resource_types: tuple[str, ...]
    local_only_resource_types: tuple[str, ...]
    fetch_errors: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "api_resource_types": self.api_resource_type_count,
                "local_resource_types": self.local_resource_type_count,
                "api_only_resource_types": len(self.api_only_resource_types),
                "local_only_resource_types": len(self.local_only_resource_types),
                "fetch_errors": len(self.fetch_errors),
                "resource_facts": len(self.resource_facts),
                "rule_signals": len(self.rule_signals),
            },
            "resource_facts": [fact.to_dict() for fact in self.resource_facts],
            "rule_signals": [signal.to_dict() for signal in self.rule_signals],
        }


def build_resource_rule_facts(
    snapshot: ResourceInventorySnapshot,
    rules: ArchitectureRules | None = None,
) -> ResourceRuleFactsBundle:
    """Build deterministic LLM facts and signals without making final rule decisions."""

    rules = rules or ArchitectureRules.load_default()
    facts = tuple(
        _build_resource_fact(snapshot.items[resource_type], rules)
        for resource_type in snapshot.api_resource_types
        if resource_type in snapshot.items
    )
    signals = tuple(RuleSignal.from_candidate(candidate) for candidate in extract_rule_candidates(snapshot))
    return ResourceRuleFactsBundle(
        resource_facts=facts,
        rule_signals=signals,
        api_resource_type_count=len(snapshot.api_resource_types),
        local_resource_type_count=len(snapshot.local_resource_types),
        api_only_resource_types=snapshot.api_only_resource_types,
        local_only_resource_types=snapshot.local_only_resource_types,
        fetch_errors=snapshot.fetch_errors,
    )


def render_resource_rule_facts_markdown(bundle: ResourceRuleFactsBundle) -> str:
    payload = bundle.to_dict()
    lines = [
        "# ROS 架构图 Resource Facts / Rule Signals",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend(["", "## Rule Signals", ""])

    signals_by_category: dict[str, list[RuleSignal]] = {}
    for signal in bundle.rule_signals:
        signals_by_category.setdefault(signal.category, []).append(signal)
    for category in sorted(signals_by_category):
        lines.extend(
            [
                f"### `{category}`",
                "",
                "| Resource type | Confidence | Targets | Evidence |",
                "| --- | --- | --- | --- |",
            ]
        )
        for signal in sorted(signals_by_category[category], key=lambda item: item.resource_type):
            targets = ", ".join(f"`{target}`" for target in signal.target_resource_types) or "-"
            evidence = "; ".join(signal.evidence[:2]).replace("|", "\\|")
            lines.append(f"| `{signal.resource_type}` | `{signal.confidence}` | {targets} | {evidence} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_resource_fact(item: ResourceInventoryItem, rules: ArchitectureRules) -> ResourceRuleFact:
    return ResourceRuleFact(
        resource_type=item.resource_type,
        product_code=item.product_code,
        source_state=item.source_state,
        name={"zh": item.name_zh, "en": item.name_en},
        description=item.detail.description if item.detail is not None else None,
        category_code=item.category_code,
        properties=_property_facts(item),
        related_properties=_related_properties(item),
        main_resource_type=_main_resource_type(item),
        fixed_rule_hits=tuple(_fixed_rule_hits(item.resource_type, rules)),
    )


def _property_facts(item: ResourceInventoryItem) -> dict[str, dict[str, Any]]:
    property_names = set()
    if item.detail is not None:
        property_names.update(item.detail.properties)
    if item.meta is not None:
        property_names.update(prop.name for prop in item.meta.related_properties)
        if item.meta.main_resource_type is not None:
            property_names.add(item.meta.main_resource_type.ref_property)

    result: dict[str, dict[str, Any]] = {}
    detail_properties = item.detail.properties if item.detail is not None else {}
    meta_properties = item.meta.related_properties_by_name if item.meta is not None else {}
    for name in sorted(property_names):
        raw = detail_properties.get(name, {})
        meta = meta_properties.get(name)
        result[name] = {
            "type": raw.get("Type") or (meta.value_type if meta is not None else None),
            "required": raw.get("Required") if isinstance(raw.get("Required"), bool) else None,
            "description": raw.get("Description") if isinstance(raw.get("Description"), str) else None,
            "related_targets": list(meta.targets) if meta is not None else [],
        }
    return result


def _related_properties(item: ResourceInventoryItem) -> dict[str, list[str]]:
    if item.meta is None:
        return {}
    return {prop.name: list(prop.targets) for prop in item.meta.related_properties}


def _main_resource_type(item: ResourceInventoryItem) -> dict[str, str] | None:
    if item.meta is None or item.meta.main_resource_type is None:
        return None
    return {
        "resource_type": item.meta.main_resource_type.resource_type,
        "ref_property": item.meta.main_resource_type.ref_property,
    }


def _fixed_rule_hits(resource_type: str, rules: ArchitectureRules) -> list[str]:
    hits: list[str] = []
    if resource_type in rules.network_layer_types:
        hits.append("network_layer_types")
    for role, types in rules.containment_layer_types.items():
        if resource_type in types:
            hits.append(f"containment_layer_types.{role}")
    if resource_type in rules.compact_attachment_marker_types:
        hits.append("compact_attachment_marker_types")
    if resource_type in rules.supplemental_related_properties:
        hits.append("supplemental_related_properties")
    if resource_type in rules.compact_orchestration_action_types:
        hits.append("compact_orchestration_actions")
    for key, collections in (
        ("compact_child_attachments", rules.compact_child_attachments),
        ("compact_bridge_attachments", rules.compact_bridge_attachments),
        ("compact_attachment_edges", rules.compact_attachment_edges),
    ):
        if any(resource_type in rule.resource_types for rule in collections):
            hits.append(key)
    if any(resource_type in node.via_resource_types for node in rules.compact_concept_nodes):
        hits.append("compact_concept_nodes")
    return hits
