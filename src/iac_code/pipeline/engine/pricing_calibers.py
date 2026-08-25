"""Validation for cost-estimate caliber alignment between planning and pricing steps."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_DEVIATION_THRESHOLD = Decimal("1.3")
UNKNOWN_PLANNING_ESTIMATE_MARKERS = {"无", "无粗估", "未提供", "未估算", "待估算"}
PRICING_FAILED_MARKERS = {"询价失败"}
_NEAR_ZERO = Decimal("0.01")
_DISCOUNT_SOURCE_RATIO = Decimal("0.99")
_AMOUNT_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class CostEstimateIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def validate_pricing_calibers(
    conclusion: dict[str, Any],
    *,
    planning_estimate: Any = None,
    tool_result_records: list[Any] | None = None,
    calibers_field: str = "pricing_calibers",
    monthly_estimate_field: str = "monthly_estimate",
    deviation_threshold: Decimal = DEFAULT_DEVIATION_THRESHOLD,
) -> list[CostEstimateIssue]:
    """Check that the pricing conclusion reconciles the planning estimate and sources every discounted price."""

    monthly_estimate = conclusion.get(monthly_estimate_field)
    if isinstance(monthly_estimate, str) and monthly_estimate.strip() in PRICING_FAILED_MARKERS:
        return []

    calibers = conclusion.get(calibers_field)
    if not isinstance(calibers, dict):
        return [CostEstimateIssue("pricing_calibers_missing", calibers_field)]

    issues: list[CostEstimateIssue] = []
    reported_planning = calibers.get("planning_estimate")
    if not isinstance(reported_planning, str) or not reported_planning.strip():
        issues.append(CostEstimateIssue("pricing_calibers_missing", "planning_estimate"))
        reported_planning = ""
    elif isinstance(planning_estimate, str) and planning_estimate.strip():
        if reported_planning.strip() != planning_estimate.strip():
            issues.append(CostEstimateIssue("planning_estimate_mismatch", planning_estimate.strip()))

    list_price = _parse_amount(calibers.get("list_price"))
    if list_price is None:
        issues.append(CostEstimateIssue("pricing_calibers_missing", "list_price"))

    if reported_planning.strip() not in UNKNOWN_PLANNING_ESTIMATE_MARKERS:
        issues.extend(_validate_deviation(calibers, deviation_threshold))
    issues.extend(_validate_effective_price(calibers, list_price, tool_result_records or []))
    return issues


def _validate_deviation(calibers: dict[str, Any], deviation_threshold: Decimal) -> list[CostEstimateIssue]:
    aligned = calibers.get("calibers_aligned")
    if not isinstance(aligned, bool):
        return [CostEstimateIssue("pricing_calibers_missing", "calibers_aligned")]

    reason = calibers.get("deviation_reason")
    reason_given = isinstance(reason, str) and bool(reason.strip())
    ratio = _decimal_or_none(calibers.get("deviation_ratio"))
    if ratio is None:
        # Without a computable ratio the alignment cannot be proven by code, so an explanation is mandatory.
        return [] if reason_given else [CostEstimateIssue("deviation_reason_missing", "deviation_ratio")]
    if ratio <= 0:
        return [CostEstimateIssue("invalid_deviation_ratio", str(ratio))]

    deviates = ratio > deviation_threshold or ratio < (Decimal(1) / deviation_threshold)
    if (deviates or not aligned) and not reason_given:
        return [CostEstimateIssue("deviation_reason_missing", str(ratio))]
    return []


def _validate_effective_price(
    calibers: dict[str, Any],
    list_price: Decimal | None,
    tool_result_records: list[Any],
) -> list[CostEstimateIssue]:
    raw_effective = calibers.get("effective_price")
    if raw_effective in (None, ""):
        return []
    effective = _parse_amount(raw_effective)
    if effective is None:
        return [CostEstimateIssue("invalid_effective_price", str(raw_effective))]
    if list_price is None or list_price <= _NEAR_ZERO or effective > list_price * _DISCOUNT_SOURCE_RATIO:
        return []

    source = calibers.get("discount_source")
    if not isinstance(source, str) or not source.strip():
        return [CostEstimateIssue("discount_source_missing", str(effective))]
    if effective <= _NEAR_ZERO and not _trade_amount_reported(tool_result_records):
        return [CostEstimateIssue("zero_effective_price_without_source", str(effective))]
    return []


def _trade_amount_reported(tool_result_records: list[Any]) -> bool:
    return any(
        isinstance(record, dict) and not record.get("is_error") and _contains_key(record.get("result"), "tradeamount")
        for record in tool_result_records
    )


def _contains_key(value: Any, target: str) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold() == target:
                return True
            if _contains_key(nested, target):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


def _parse_amount(value: Any) -> Decimal | None:
    number = _decimal_or_none(value)
    if number is not None:
        return number
    if not isinstance(value, str):
        return None
    match = _AMOUNT_PATTERN.search(value.replace(",", ""))
    return _decimal_or_none(match.group()) if match else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None
