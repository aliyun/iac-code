"""Coverage validation between declared resource intents and generated plan items."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceIntentIssue:
    code: str
    product: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def collect_resource_intents(
    context_snapshot: dict[str, Any],
    source_fields: list[str],
) -> tuple[list[dict[str, Any]], list[ResourceIntentIssue]]:
    """Merge declared resource intents from oldest to newest, later revisions winning by product."""

    merged: dict[str, dict[str, Any]] = {}
    issues: list[ResourceIntentIssue] = []
    for source_field in source_fields:
        raw_intents = _resolve_dotted(context_snapshot, source_field)
        if raw_intents is None:
            continue
        if not isinstance(raw_intents, list):
            issues.append(ResourceIntentIssue("invalid_resource_intent_source", detail=source_field))
            continue
        for raw_intent in raw_intents:
            if not isinstance(raw_intent, dict):
                issues.append(ResourceIntentIssue("invalid_resource_intent"))
                continue
            product = raw_intent.get("product")
            if not isinstance(product, str) or not product.strip():
                issues.append(ResourceIntentIssue("missing_resource_intent_product"))
                continue
            merged[normalize_product(product)] = raw_intent
    return list(merged.values()), issues


def validate_resource_intent_coverage(
    intents: list[dict[str, Any]],
    items: Any,
    *,
    covered_products_fields: list[str],
    gaps_field: str,
) -> list[ResourceIntentIssue]:
    """Require every non-forbidden intent to be covered by each item or declared as an explicit gap."""

    required = {
        normalize_product(str(intent.get("product") or "")): intent
        for intent in intents
        if isinstance(intent, dict) and intent.get("action") != "forbid"
    }
    forbidden = {
        normalize_product(str(intent.get("product") or ""))
        for intent in intents
        if isinstance(intent, dict) and intent.get("action") == "forbid"
    }
    if not required and not forbidden:
        return []
    if not isinstance(items, list) or not items:
        return [ResourceIntentIssue("invalid_coverage_items")]

    issues: list[ResourceIntentIssue] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(ResourceIntentIssue("invalid_coverage_item", detail=str(index)))
            continue
        covered = _collect_covered_products(item, covered_products_fields)
        declared_gaps, gap_issues = _collect_declared_gaps(item, gaps_field)
        issues.extend(gap_issues)
        for product, intent in required.items():
            if product in covered:
                continue
            if product in declared_gaps:
                continue
            issues.append(
                ResourceIntentIssue("uncovered_resource_intent", str(intent.get("product") or ""), str(index))
            )
        for product in declared_gaps - required.keys():
            issues.append(ResourceIntentIssue("unexpected_resource_intent_gap", product, str(index)))
        for product in covered & forbidden:
            issues.append(ResourceIntentIssue("forbidden_resource_intent_covered", product, str(index)))
    return issues


def normalize_product(product: str) -> str:
    """Fold product aliases such as ``NAT Gateway``/``NATGateway``/``nat-gateway`` into one key."""

    return "".join(char for char in product.casefold() if char.isalnum())


def _collect_covered_products(item: dict[Any, Any], covered_products_fields: list[str]) -> set[str]:
    covered: set[str] = set()
    for field in covered_products_fields:
        values = _resolve_dotted(item, field)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str):
                covered.add(normalize_product(value))
            elif isinstance(value, dict) and value.get("action") != "forbid":
                product = value.get("product")
                if isinstance(product, str):
                    covered.add(normalize_product(product))
    covered.discard("")
    return covered


def _collect_declared_gaps(item: dict[Any, Any], gaps_field: str) -> tuple[set[str], list[ResourceIntentIssue]]:
    raw_gaps = _resolve_dotted(item, gaps_field)
    if raw_gaps is None:
        return set(), []
    if not isinstance(raw_gaps, list):
        return set(), [ResourceIntentIssue("invalid_resource_intent_gap", detail=gaps_field)]
    gaps: set[str] = set()
    issues: list[ResourceIntentIssue] = []
    for raw_gap in raw_gaps:
        if not isinstance(raw_gap, dict):
            issues.append(ResourceIntentIssue("invalid_resource_intent_gap"))
            continue
        product = raw_gap.get("product")
        reason = raw_gap.get("reason")
        if not isinstance(product, str) or not product.strip():
            issues.append(ResourceIntentIssue("invalid_resource_intent_gap"))
            continue
        if not isinstance(reason, str) or not reason.strip():
            issues.append(ResourceIntentIssue("invalid_resource_intent_gap", normalize_product(product)))
            continue
        gaps.add(normalize_product(product))
    return gaps, issues


def _resolve_dotted(value: Any, path: str) -> Any:
    if not path:
        return None
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if 0 <= index < len(value) else None
        else:
            return None
        if value is None:
            return None
    return value
