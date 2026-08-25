"""Generic validation that architecture candidates cover parsed intent resources."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

REQUIRED_COVERAGE_ACTIONS = frozenset({"create", "use_existing", "reference"})
FORBIDDEN_ACTION = "forbid"


@dataclass(frozen=True)
class IntentCoverageIssue:
    code: str
    product: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def collect_resource_intents(
    context_snapshot: dict[str, Any],
    source_fields: list[str],
) -> tuple[list[dict[str, Any]], list[IntentCoverageIssue]]:
    """Merge resource intents from oldest to newest source, with later sources winning by product."""

    merged: dict[str, dict[str, Any]] = {}
    issues: list[IntentCoverageIssue] = []
    for source_field in source_fields:
        raw_intents = _resolve_dotted(context_snapshot, source_field)
        if raw_intents is None:
            continue
        if not isinstance(raw_intents, list):
            issues.append(IntentCoverageIssue("invalid_intent_source", detail=source_field))
            continue
        for raw_intent in raw_intents:
            if not isinstance(raw_intent, dict):
                issues.append(IntentCoverageIssue("invalid_resource_intent"))
                continue
            product = raw_intent.get("product")
            if not isinstance(product, str) or not product.strip():
                issues.append(IntentCoverageIssue("missing_intent_product"))
                continue
            merged[_normalize_product(product)] = raw_intent
    return list(merged.values()), issues


def validate_intent_coverage(intents: list[dict[str, Any]], candidates: Any) -> list[IntentCoverageIssue]:
    """Require every parsed intent resource to be covered by a candidate or explicitly excluded."""

    if not intents:
        return []
    if not isinstance(candidates, list) or not candidates:
        return [IntentCoverageIssue("invalid_candidates")]

    issues: list[IntentCoverageIssue] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            issues.append(IntentCoverageIssue("invalid_candidate", detail=f"candidates.{index}"))
            continue
        issues.extend(_validate_candidate(intents, candidate, index))
    return issues


def _validate_candidate(
    intents: list[dict[str, Any]],
    candidate: Any,
    index: int,
) -> list[IntentCoverageIssue]:
    location = f"candidates.{index}"
    issues: list[IntentCoverageIssue] = []
    covered = _candidate_covered_products(candidate)
    created = _candidate_created_products(candidate)
    excluded = _candidate_excluded_reasons(candidate)

    for intent in intents:
        product = str(intent.get("product") or "")
        key = _normalize_product(product)
        action = intent.get("action")
        if action == FORBIDDEN_ACTION:
            if key in created:
                issues.append(IntentCoverageIssue("forbidden_intent_resource_present", product, location))
            continue
        if action not in REQUIRED_COVERAGE_ACTIONS:
            continue
        if key in covered:
            continue
        if key not in excluded:
            issues.append(IntentCoverageIssue("intent_resource_not_covered", product, location))
            continue
        if not excluded[key]:
            issues.append(IntentCoverageIssue("intent_resource_exclusion_reason_missing", product, location))

    intent_keys = {_normalize_product(str(intent.get("product") or "")) for intent in intents}
    for key, product in _candidate_excluded_products(candidate).items():
        if key not in intent_keys:
            issues.append(IntentCoverageIssue("unexpected_intent_exclusion", product, location))
    return issues


def _candidate_covered_products(candidate: Any) -> set[str]:
    covered: set[str] = set()
    for raw_intent in _list_value(candidate.get("resource_intents")):
        if not isinstance(raw_intent, dict):
            continue
        if raw_intent.get("action") == FORBIDDEN_ACTION:
            continue
        product = raw_intent.get("product")
        if isinstance(product, str) and product.strip():
            covered.add(_normalize_product(product))
    for product in _list_value(candidate.get("products")):
        if isinstance(product, str) and product.strip():
            covered.add(_normalize_product(product))
    return covered


def _candidate_created_products(candidate: Any) -> set[str]:
    created: set[str] = set()
    for raw_intent in _list_value(candidate.get("resource_intents")):
        if not isinstance(raw_intent, dict) or raw_intent.get("action") != "create":
            continue
        product = raw_intent.get("product")
        if isinstance(product, str) and product.strip():
            created.add(_normalize_product(product))
    declared_intents = {
        _normalize_product(str(raw_intent.get("product") or ""))
        for raw_intent in _list_value(candidate.get("resource_intents"))
        if isinstance(raw_intent, dict) and isinstance(raw_intent.get("product"), str)
    }
    for product in _list_value(candidate.get("products")):
        if not isinstance(product, str) or not product.strip():
            continue
        key = _normalize_product(product)
        if key not in declared_intents:
            created.add(key)
    return created


def _candidate_excluded_reasons(candidate: Any) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for raw_exclusion in _list_value(candidate.get("excluded_resource_intents")):
        if not isinstance(raw_exclusion, dict):
            continue
        product = raw_exclusion.get("product")
        if not isinstance(product, str) or not product.strip():
            continue
        reason = raw_exclusion.get("reason")
        reasons[_normalize_product(product)] = reason.strip() if isinstance(reason, str) else ""
    return reasons


def _candidate_excluded_products(candidate: Any) -> dict[str, str]:
    products: dict[str, str] = {}
    for raw_exclusion in _list_value(candidate.get("excluded_resource_intents")):
        if not isinstance(raw_exclusion, dict):
            continue
        product = raw_exclusion.get("product")
        if isinstance(product, str) and product.strip():
            products[_normalize_product(product)] = product
    return products


def _normalize_product(value: str) -> str:
    return re.sub(r"[\s_\-:]+", "", value).casefold()


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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
