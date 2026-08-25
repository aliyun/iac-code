"""Cross-step validation for candidate cost caliber and planning reconciliation.

``architecture_planning`` produces a rough monthly range while ``cost_estimating``
produces the authoritative quote. Without reconciliation the two stages can use
different billing calibers, and a contract-discounted price can be surfaced as
the final price without any provenance. This module validates both.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_EXPECTED_CALIBER = "pay_as_you_go_monthly"
DEFAULT_DEVIATION_TOLERANCE_RATIO = 0.2

QUOTE_FAILED_TEXT = "询价失败"
ESTIMATE_MARKERS = ("估算", "estimate")

DEVIATION_STATUS_ALIGNED = "aligned"
DEVIATION_STATUS_DEVIATED = "deviated"
DEVIATION_STATUS_PLANNING_UNAVAILABLE = "planning_estimate_unavailable"

_AMOUNT_PATTERN = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)")


@dataclass(frozen=True)
class CostCaliberIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def parse_monthly_amounts(text: Any) -> list[Decimal]:
    """Extract the monetary amounts from a monthly estimate string, in order."""

    if not isinstance(text, str):
        return []
    amounts: list[Decimal] = []
    for raw in _AMOUNT_PATTERN.findall(text):
        try:
            amounts.append(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    return amounts


def parse_planning_range(text: Any) -> tuple[Decimal, Decimal] | None:
    """Parse the planning rough estimate into a (low, high) range."""

    amounts = parse_monthly_amounts(text)
    if not amounts:
        return None
    return min(amounts), max(amounts)


def quote_failed(monthly_estimate: Any) -> bool:
    return isinstance(monthly_estimate, str) and QUOTE_FAILED_TEXT in monthly_estimate


def _has_estimate_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ESTIMATE_MARKERS)


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_cost_caliber(
    context_snapshot: dict[str, Any],
    conclusion: dict[str, Any],
    *,
    planning_estimate_field: str = "candidate.monthly_estimate",
    monthly_estimate_field: str = "monthly_estimate",
    provenance_field: str = "pricing_provenance",
    deviation_field: str = "planning_deviation",
    expected_caliber: str = DEFAULT_EXPECTED_CALIBER,
    deviation_tolerance_ratio: float = DEFAULT_DEVIATION_TOLERANCE_RATIO,
) -> list[CostCaliberIssue]:
    """Validate the cost conclusion against the planning stage rough estimate."""

    monthly_estimate = _resolve_dotted(conclusion, monthly_estimate_field)
    if quote_failed(monthly_estimate):
        if _non_empty_str(conclusion.get("error")):
            return []
        return [CostCaliberIssue("quote_failure_reason_missing", detail="error")]

    if not _non_empty_str(monthly_estimate):
        return [CostCaliberIssue("monthly_estimate_missing", detail=monthly_estimate_field)]

    issues: list[CostCaliberIssue] = []
    provenance = _resolve_dotted(conclusion, provenance_field)
    if not isinstance(provenance, dict):
        return [CostCaliberIssue("pricing_provenance_missing", detail=provenance_field)]

    caliber = provenance.get("caliber")
    if caliber != expected_caliber:
        issues.append(
            CostCaliberIssue("pricing_caliber_mismatch", detail=f"expected {expected_caliber}, got {caliber!r}")
        )
    if not _non_empty_str(provenance.get("list_price_source")):
        issues.append(CostCaliberIssue("list_price_source_missing", detail=f"{provenance_field}.list_price_source"))

    issues.extend(_validate_contract_price(monthly_estimate, provenance, provenance_field))
    issues.extend(
        _validate_planning_deviation(
            context_snapshot=context_snapshot,
            conclusion=conclusion,
            monthly_estimate=monthly_estimate,
            planning_estimate_field=planning_estimate_field,
            deviation_field=deviation_field,
            deviation_tolerance_ratio=deviation_tolerance_ratio,
        )
    )
    return issues


def _validate_contract_price(
    monthly_estimate: str,
    provenance: dict[str, Any],
    provenance_field: str,
) -> list[CostCaliberIssue]:
    """A contract-discounted price is only presentable with a source or an estimate label."""

    if len(parse_monthly_amounts(monthly_estimate)) < 2:
        return []
    if _non_empty_str(provenance.get("contract_price_source")):
        return []
    if provenance.get("contract_price_is_estimate") is True and _has_estimate_marker(monthly_estimate):
        return []
    return [
        CostCaliberIssue(
            "contract_price_provenance_missing",
            detail=f"{provenance_field}.contract_price_source",
        )
    ]


def _validate_planning_deviation(
    *,
    context_snapshot: dict[str, Any],
    conclusion: dict[str, Any],
    monthly_estimate: str,
    planning_estimate_field: str,
    deviation_field: str,
    deviation_tolerance_ratio: float,
) -> list[CostCaliberIssue]:
    deviation = _resolve_dotted(conclusion, deviation_field)
    if not isinstance(deviation, dict):
        return [CostCaliberIssue("planning_deviation_missing", detail=deviation_field)]

    status = deviation.get("status")
    planning_range = parse_planning_range(_resolve_dotted(context_snapshot, planning_estimate_field))
    final_amounts = parse_monthly_amounts(monthly_estimate)

    if planning_range is None:
        if status != DEVIATION_STATUS_PLANNING_UNAVAILABLE:
            return [
                CostCaliberIssue(
                    "planning_deviation_status_invalid",
                    detail=f"planning estimate unavailable, expected {DEVIATION_STATUS_PLANNING_UNAVAILABLE}",
                )
            ]
        return []

    if status == DEVIATION_STATUS_PLANNING_UNAVAILABLE:
        return [
            CostCaliberIssue(
                "planning_deviation_status_invalid",
                detail=f"planning estimate is available, {DEVIATION_STATUS_PLANNING_UNAVAILABLE} not allowed",
            )
        ]
    if status not in (DEVIATION_STATUS_ALIGNED, DEVIATION_STATUS_DEVIATED):
        return [CostCaliberIssue("planning_deviation_status_invalid", detail=f"status={status!r}")]

    if status == DEVIATION_STATUS_DEVIATED:
        if not _non_empty_str(deviation.get("reason")):
            return [CostCaliberIssue("planning_deviation_reason_missing", detail=f"{deviation_field}.reason")]
        return []

    if not final_amounts:
        return []
    low, high = planning_range
    list_price = final_amounts[0]
    if _within_tolerance(list_price, low, high, deviation_tolerance_ratio):
        return []
    return [
        CostCaliberIssue(
            "planning_deviation_unreported",
            detail=f"list price {list_price} outside planning range {low}-{high}",
        )
    ]


def _within_tolerance(value: Decimal, low: Decimal, high: Decimal, tolerance_ratio: float) -> bool:
    tolerance = Decimal(str(max(tolerance_ratio, 0.0)))
    return low * (1 - tolerance) <= value <= high * (1 + tolerance)


def _resolve_dotted(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current
