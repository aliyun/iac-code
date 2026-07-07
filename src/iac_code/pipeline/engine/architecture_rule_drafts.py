"""LLM draft rule parsing, validation, review, and patch application."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from iac_code.pipeline.engine.architecture_rule_facts import ResourceRuleFact, ResourceRuleFactsBundle

RuleLabel = str | dict[str, str]

CLASSIFICATIONS = {
    "core_node",
    "container",
    "attachment",
    "bridge_attachment",
    "attachment_edge",
    "orchestration_action",
    "concept_node",
    "supplemental_relation",
    "display",
    "ignore",
    "needs_review",
}

SUPPORTED_PATCH_KEYS = {
    "network_layer_types",
    "containment_layer_types",
    "fallback_related_properties",
    "supplemental_related_properties",
    "compact_hidden_short_types",
    "compact_resource_labels",
    "compact_attachment_marker_types",
    "compact_child_attachments",
    "compact_bridge_attachments",
    "compact_attachment_edges",
    "compact_orchestration_actions",
    "compact_concept_nodes",
}

CLASSIFICATION_PATCH_KEYS = {
    "container": {"network_layer_types", "containment_layer_types"},
    "supplemental_relation": {"fallback_related_properties", "supplemental_related_properties"},
    "display": {"compact_hidden_short_types", "compact_resource_labels"},
    "attachment": {"compact_attachment_marker_types", "compact_child_attachments"},
    "bridge_attachment": {"compact_bridge_attachments"},
    "attachment_edge": {"compact_attachment_edges"},
    "orchestration_action": {"compact_orchestration_actions"},
    "concept_node": {"compact_concept_nodes"},
    "core_node": set(),
    "ignore": set(),
    "needs_review": set(),
}

LIST_PATCH_KEYS = {
    "network_layer_types",
    "compact_hidden_short_types",
    "compact_attachment_marker_types",
    "compact_child_attachments",
    "compact_bridge_attachments",
    "compact_attachment_edges",
    "compact_orchestration_actions",
    "compact_concept_nodes",
}

DICT_PATCH_KEYS = {
    "containment_layer_types",
    "fallback_related_properties",
    "supplemental_related_properties",
    "compact_resource_labels",
}


@dataclass(frozen=True)
class DraftRule:
    resource_type: str
    classification: str
    target_resource_types: tuple[str, ...]
    property_names: tuple[str, ...]
    label: RuleLabel | None
    edge_label: RuleLabel | None
    confidence: float
    evidence: tuple[str, ...]
    suggested_architecture_rules_patch: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: Any) -> DraftRule | None:
        if not isinstance(raw, dict):
            return None
        resource_type = raw.get("resource_type")
        classification = raw.get("classification")
        if not isinstance(resource_type, str) or not isinstance(classification, str):
            return None
        confidence = raw.get("confidence")
        try:
            normalized_confidence = float(confidence)
        except (TypeError, ValueError):
            normalized_confidence = 0.0
        patch = raw.get("suggested_architecture_rules_patch")
        return cls(
            resource_type=resource_type,
            classification=classification,
            target_resource_types=_string_tuple(raw.get("target_resource_types")),
            property_names=_string_tuple(raw.get("property_names")),
            label=_rule_label(raw.get("label")),
            edge_label=_rule_label(raw.get("edge_label")),
            confidence=max(0.0, min(1.0, normalized_confidence)),
            evidence=_string_tuple(raw.get("evidence")),
            suggested_architecture_rules_patch=patch if isinstance(patch, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "classification": self.classification,
            "target_resource_types": list(self.target_resource_types),
            "property_names": list(self.property_names),
            "label": self.label,
            "edge_label": self.edge_label,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "suggested_architecture_rules_patch": self.suggested_architecture_rules_patch,
        }


@dataclass(frozen=True)
class DraftValidationResult:
    draft: DraftRule
    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class DraftReviewDecision:
    draft: DraftRule
    accepted: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "draft": self.draft.to_dict(),
        }


def parse_draft_rules_response(text: str) -> list[DraftRule]:
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if match is None:
            raise ValueError("LLM output does not contain a JSON object") from None
        value = json.loads(match.group(0))
    if isinstance(value, list):
        raw_rules = value
    elif isinstance(value, dict):
        raw_rules = value.get("draft_rules")
    else:
        raise ValueError("LLM output JSON must be an object or draft rule list")
    if not isinstance(raw_rules, list):
        raise ValueError("LLM output JSON must contain draft_rules list")
    drafts = [DraftRule.from_raw(item) for item in raw_rules]
    return [draft for draft in drafts if draft is not None]


def validate_draft_rules(
    drafts: list[DraftRule],
    facts: ResourceRuleFactsBundle,
) -> list[DraftValidationResult]:
    known_types = {fact.resource_type for fact in facts.resource_facts}
    properties_by_type = {
        fact.resource_type: set(fact.properties) | set(fact.related_properties) for fact in facts.resource_facts
    }
    results: list[DraftValidationResult] = []
    for draft in drafts:
        errors: list[str] = []
        if draft.resource_type not in known_types:
            errors.append(f"unknown resource type `{draft.resource_type}`")
        if draft.classification not in CLASSIFICATIONS:
            errors.append(f"unsupported classification `{draft.classification}`")
        property_names = properties_by_type.get(draft.resource_type, set())
        for property_name in draft.property_names:
            if property_name not in property_names:
                errors.append(f"unknown property `{property_name}` for `{draft.resource_type}`")
        for target in draft.target_resource_types:
            if target not in known_types and not target.startswith("CONCEPT::"):
                errors.append(f"unknown target resource type `{target}`")
        errors.extend(_validate_patch_keys(draft, known_types, properties_by_type))
        if draft.classification not in {"core_node", "ignore", "needs_review"}:
            if draft.classification == "attachment_edge":
                if draft.edge_label is None:
                    errors.append("edge_label is required for attachment_edge draft rules")
            elif draft.label is None:
                errors.append("label is required for actionable draft rules")
            if not draft.evidence:
                errors.append("evidence is required for actionable draft rules")
        results.append(DraftValidationResult(draft=draft, valid=not errors, errors=tuple(errors)))
    return results


def review_valid_draft_rules(
    validations: list[DraftValidationResult],
    *,
    minimum_confidence: float = 0.75,
) -> list[DraftReviewDecision]:
    decisions: list[DraftReviewDecision] = []
    for validation in validations:
        if not validation.valid:
            decisions.append(
                DraftReviewDecision(
                    draft=validation.draft,
                    accepted=False,
                    reasons=validation.errors,
                )
            )
            continue

        draft = validation.draft
        reasons: list[str] = []
        if draft.classification in {"ignore", "needs_review", "core_node"}:
            reasons.append(f"classification `{draft.classification}` is not automatically applied")
        if draft.confidence < minimum_confidence:
            reasons.append(
                "confidence {:.2f} is below automatic review threshold {:.2f}".format(
                    draft.confidence, minimum_confidence
                )
            )
        if draft.classification in {"attachment", "bridge_attachment", "attachment_edge", "orchestration_action"}:
            if _patch_uses_solid_business_edge(draft.suggested_architecture_rules_patch):
                reasons.append("attachment/configuration rules must not use solid business traffic edges")
        if draft.classification == "container" and _looks_like_security_or_whitelist(draft.resource_type):
            reasons.append("security/whitelist resources are not accepted as containers by automatic review")
        decisions.append(
            DraftReviewDecision(
                draft=draft,
                accepted=not reasons,
                reasons=tuple(reasons or ("schema validation and automatic review passed",)),
            )
        )
    return decisions


def apply_reviewed_draft_patches(
    raw_rules: dict[str, Any],
    decisions: list[DraftReviewDecision],
    *,
    facts: ResourceRuleFactsBundle | None = None,
) -> dict[str, Any]:
    updated = copy.deepcopy(raw_rules)
    facts_by_type = {fact.resource_type: fact for fact in facts.resource_facts} if facts is not None else {}
    for decision in decisions:
        if not decision.accepted:
            continue
        for key, value in decision.draft.suggested_architecture_rules_patch.items():
            localized_value = _localize_patch_labels(value, facts_by_type, decision.draft.resource_type)
            if key in LIST_PATCH_KEYS:
                updated[key] = _merge_list(updated.get(key), localized_value)
            elif key in DICT_PATCH_KEYS:
                updated[key] = _merge_dict(updated.get(key), localized_value)
    return updated


def render_draft_review_report_markdown(decisions: list[DraftReviewDecision]) -> str:
    accepted_count = sum(1 for decision in decisions if decision.accepted)
    rejected_count = len(decisions) - accepted_count
    lines = [
        "# ROS 架构图 LLM Draft Rules Review",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        f"| Draft rules | {len(decisions)} |",
        f"| Accepted | {accepted_count} |",
        f"| Rejected | {rejected_count} |",
        "",
        "## By Classification",
        "",
        "| Classification | Drafts | Accepted | Rejected |",
        "| --- | ---: | ---: | ---: |",
    ]
    for classification in sorted({decision.draft.classification for decision in decisions}):
        class_decisions = [decision for decision in decisions if decision.draft.classification == classification]
        class_accepted = sum(1 for decision in class_decisions if decision.accepted)
        lines.append(
            "| `{}` | {} | {} | {} |".format(
                classification,
                len(class_decisions),
                class_accepted,
                len(class_decisions) - class_accepted,
            )
        )
    lines.extend(
        [
            "",
            "## Decisions",
            "",
            "| Resource type | Classification | Accepted | Reasons |",
            "| --- | --- | --- | --- |",
        ]
    )
    for decision in sorted(decisions, key=lambda item: (item.draft.classification, item.draft.resource_type)):
        reasons = "; ".join(decision.reasons).replace("|", "\\|")
        lines.append(
            "| `{}` | `{}` | `{}` | {} |".format(
                decision.draft.resource_type,
                decision.draft.classification,
                "yes" if decision.accepted else "no",
                reasons,
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _validate_patch_keys(
    draft: DraftRule,
    known_types: set[str],
    properties_by_type: dict[str, set[str]],
) -> list[str]:
    errors: list[str] = []
    allowed_keys = CLASSIFICATION_PATCH_KEYS.get(draft.classification, set())
    for key, value in draft.suggested_architecture_rules_patch.items():
        if key not in SUPPORTED_PATCH_KEYS:
            errors.append(f"unsupported patch key `{key}`")
            continue
        if key not in allowed_keys:
            errors.append(f"patch key `{key}` is not allowed for `{draft.classification}`")
        if key in LIST_PATCH_KEYS and not isinstance(value, list):
            errors.append(f"patch key `{key}` must be a list")
        if key in DICT_PATCH_KEYS and not isinstance(value, dict):
            errors.append(f"patch key `{key}` must be an object")
        errors.extend(_validate_patch_value(key, value, known_types, properties_by_type, draft.resource_type))
    if draft.classification not in {"core_node", "ignore", "needs_review"}:
        if not draft.suggested_architecture_rules_patch:
            errors.append("actionable draft rules require suggested_architecture_rules_patch")
    return errors


def _validate_patch_value(
    key: str,
    value: Any,
    known_types: set[str],
    properties_by_type: dict[str, set[str]],
    draft_resource_type: str,
) -> list[str]:
    errors: list[str] = []
    if key in {"network_layer_types", "compact_hidden_short_types", "compact_attachment_marker_types"}:
        for resource_type in _string_list(value):
            if resource_type not in known_types and not resource_type.startswith("CONCEPT::"):
                errors.append(f"unknown patch resource type `{resource_type}` in `{key}`")
        return errors
    if key == "containment_layer_types":
        if not isinstance(value, dict):
            return errors
        for role, resource_types in value.items():
            if not isinstance(role, str):
                errors.append(f"non-string containment role `{role}`")
            for resource_type in _string_list(resource_types):
                if resource_type not in known_types:
                    errors.append(f"unknown patch resource type `{resource_type}` in `{key}`")
        return errors
    if key == "compact_resource_labels":
        if not isinstance(value, dict):
            return errors
        for resource_type in value:
            if isinstance(resource_type, str) and resource_type not in known_types:
                errors.append(f"unknown patch resource type `{resource_type}` in `{key}`")
        return errors
    if key == "fallback_related_properties":
        if not isinstance(value, dict):
            return errors
        for targets in value.values():
            for resource_type in _string_list(targets):
                if resource_type not in known_types:
                    errors.append(f"unknown patch resource type `{resource_type}` in `{key}`")
        return errors
    if key == "supplemental_related_properties":
        if not isinstance(value, dict):
            return errors
        for resource_type, properties in value.items():
            if not isinstance(resource_type, str):
                continue
            if resource_type not in known_types:
                errors.append(f"unknown patch resource type `{resource_type}` in `{key}`")
            if not isinstance(properties, dict):
                continue
            for property_name, targets in properties.items():
                if isinstance(property_name, str):
                    _validate_property_name(
                        property_name,
                        {resource_type},
                        properties_by_type,
                        errors,
                    )
                for target in _string_list(targets):
                    if target not in known_types:
                        errors.append(f"unknown patch resource type `{target}` in `{key}`")
        return errors
    if key in LIST_PATCH_KEYS:
        for item in value if isinstance(value, list) else []:
            if isinstance(item, dict):
                errors.extend(
                    _validate_patch_rule_object(item, known_types, properties_by_type, draft_resource_type, key)
                )
    return errors


def _validate_patch_rule_object(
    item: dict[str, Any],
    known_types: set[str],
    properties_by_type: dict[str, set[str]],
    draft_resource_type: str,
    patch_key: str,
) -> list[str]:
    errors: list[str] = []
    relevant_resource_types = {draft_resource_type}
    if patch_key == "compact_bridge_attachments" and not _string_list(item.get("via_resource_types")):
        errors.append("compact_bridge_attachments require non-empty via_resource_types")
    for field in ("resource_types", "via_resource_types", "source_types", "target_types", "marker_types"):
        for resource_type in _string_list(item.get(field)):
            if resource_type not in known_types and not resource_type.startswith("CONCEPT::"):
                errors.append(f"unknown patch resource type `{resource_type}` in `{patch_key}.{field}`")
            else:
                relevant_resource_types.add(resource_type)
    if isinstance(item.get("resource_type"), str):
        resource_type = item["resource_type"]
        if resource_type not in known_types and not resource_type.startswith("CONCEPT::"):
            errors.append(f"unknown patch resource type `{resource_type}` in `{patch_key}.resource_type`")
        else:
            relevant_resource_types.add(resource_type)

    property_fields = (
        "source_properties",
        "target_properties",
        "marker_properties",
        "command_properties",
        "evidence_properties",
        "via_source_properties",
        "via_target_properties",
    )
    for field in property_fields:
        for property_name in _string_list(item.get(field)):
            _validate_property_name(property_name, relevant_resource_types, properties_by_type, errors)
    for field in ("controller_property", "source_property"):
        property_name = item.get(field)
        if isinstance(property_name, str):
            _validate_property_name(property_name, relevant_resource_types, properties_by_type, errors)
    return errors


def _localize_patch_labels(value: Any, facts_by_type: dict[str, ResourceRuleFact], draft_resource_type: str) -> Any:
    if isinstance(value, list):
        return [_localize_patch_labels(item, facts_by_type, draft_resource_type) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = copy.deepcopy(value)
    for key in ("label", "edge_label"):
        raw_label = result.get(key)
        if isinstance(raw_label, str) and raw_label:
            result[key] = _localized_label(raw_label, facts_by_type.get(draft_resource_type))
    for key, nested in list(result.items()):
        if key in {"label", "edge_label"}:
            continue
        if isinstance(nested, (dict, list)):
            result[key] = _localize_patch_labels(nested, facts_by_type, draft_resource_type)
    return result


def _localized_label(label: str, fact: ResourceRuleFact | None) -> dict[str, str]:
    zh = label
    if fact is not None and isinstance(fact.name.get("zh"), str) and fact.name["zh"]:
        zh = fact.name["zh"]
    return {"zh": zh, "en": label}


def _validate_property_name(
    property_name: str,
    resource_types: set[str],
    properties_by_type: dict[str, set[str]],
    errors: list[str],
) -> None:
    if any(property_name in properties_by_type.get(resource_type, set()) for resource_type in resource_types):
        return
    resources = ", ".join(sorted(resource_types))
    errors.append(f"unknown patch property `{property_name}` for `{resources}`")


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _patch_uses_solid_business_edge(patch: dict[str, Any]) -> bool:
    for key in ("compact_attachment_edges", "compact_relation_folds"):
        values = patch.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict) and item.get("edge_style") == "solid_arrow":
                return True
    return False


def _looks_like_security_or_whitelist(resource_type: str) -> bool:
    lowered = resource_type.lower()
    return any(token in lowered for token in ("securityip", "whitelist", "securitygroup", "accessrule"))


def _merge_list(current: Any, patch: Any) -> list[Any]:
    values = list(current) if isinstance(current, list) else []
    additions = patch if isinstance(patch, list) else []
    seen = {_stable_json(value) for value in values}
    semantic_seen = {_semantic_rule_key(value) for value in values}
    for item in additions:
        key = _stable_json(item)
        semantic_key = _semantic_rule_key(item)
        if key in seen or (semantic_key is not None and semantic_key in semantic_seen):
            continue
        seen.add(key)
        semantic_seen.add(semantic_key)
        values.append(copy.deepcopy(item))
    return values


def _merge_dict(current: Any, patch: Any) -> dict[str, Any]:
    values = copy.deepcopy(current) if isinstance(current, dict) else {}
    if not isinstance(patch, dict):
        return values
    for key, patch_value in patch.items():
        if not isinstance(key, str):
            continue
        current_value = values.get(key)
        if isinstance(current_value, list) and isinstance(patch_value, list):
            values[key] = _merge_list(current_value, patch_value)
        elif isinstance(current_value, dict) and isinstance(patch_value, dict):
            values[key] = _merge_dict(current_value, patch_value)
        else:
            values[key] = copy.deepcopy(patch_value)
    return values


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _semantic_rule_key(value: Any) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(value, dict):
        return None
    for key in ("resource_types", "via_resource_types"):
        raw = value.get(key)
        if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
            return key, tuple(sorted(raw))
    return None


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _rule_label(raw: Any) -> RuleLabel | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        labels = {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}
        return labels or None
    return None
