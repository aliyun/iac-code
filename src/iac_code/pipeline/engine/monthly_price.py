"""Generic validation for the cost step's list price vs contract-discounted price disclosure."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

PRICING_FAILED_ESTIMATE = "询价失败"

_AMOUNT_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass(frozen=True)
class PriceValidationIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def validate_monthly_price_breakdown(monthly_estimate: Any, breakdown: Any) -> list[PriceValidationIssue]:
    """Validate that the list price and contract-discounted price are truly separated.

    The cost step reports ``monthly_estimate`` as display text, so identical list and
    discounted prices are only acceptable when the step explicitly states that no
    discount applied. This keeps a zero discount distinguishable from a discounted
    price that was silently overwritten with the list price.
    """

    if isinstance(monthly_estimate, str) and monthly_estimate.strip() == PRICING_FAILED_ESTIMATE:
        return []
    if not isinstance(monthly_estimate, str) or not monthly_estimate.strip():
        return [PriceValidationIssue("invalid_monthly_estimate")]
    if not isinstance(breakdown, dict):
        return [PriceValidationIssue("missing_monthly_price_breakdown")]

    list_price = _decimal_or_none(breakdown.get("list_price"))
    discounted_price = _decimal_or_none(breakdown.get("discounted_price"))
    issues: list[PriceValidationIssue] = []
    if list_price is None or list_price < 0:
        issues.append(PriceValidationIssue("invalid_list_price", _text(breakdown.get("list_price"))))
    if discounted_price is None or discounted_price < 0:
        issues.append(PriceValidationIssue("invalid_discounted_price", _text(breakdown.get("discounted_price"))))
    if issues:
        return issues

    assert list_price is not None and discounted_price is not None
    if discounted_price > list_price:
        return [
            PriceValidationIssue(
                "discounted_price_above_list_price",
                "{} > {}".format(_text(discounted_price), _text(list_price)),
            )
        ]

    discount_applied = breakdown.get("discount_applied")
    if not isinstance(discount_applied, bool):
        return [PriceValidationIssue("invalid_discount_applied", _text(discount_applied))]

    same_price = discounted_price == list_price
    same_price_reason = breakdown.get("same_price_reason")
    if same_price:
        if discount_applied:
            issues.append(PriceValidationIssue("discount_applied_without_price_difference"))
        if not isinstance(same_price_reason, str) or not same_price_reason.strip():
            issues.append(PriceValidationIssue("missing_same_price_reason"))
    elif not discount_applied:
        issues.append(PriceValidationIssue("discount_not_declared_despite_price_difference"))

    issues.extend(_estimate_text_issues(monthly_estimate, list_price, discounted_price, same_price=same_price))
    return issues


def _estimate_text_issues(
    monthly_estimate: str,
    list_price: Decimal,
    discounted_price: Decimal,
    *,
    same_price: bool,
) -> list[PriceValidationIssue]:
    amounts = _amounts_in_text(monthly_estimate)
    if not amounts:
        return [PriceValidationIssue("monthly_estimate_missing_amount", monthly_estimate)]
    if list_price not in amounts:
        return [
            PriceValidationIssue(
                "monthly_estimate_list_price_mismatch",
                "{} not in {}".format(_text(list_price), monthly_estimate),
            )
        ]
    if not same_price and discounted_price not in amounts:
        return [
            PriceValidationIssue(
                "monthly_estimate_discounted_price_mismatch",
                "{} not in {}".format(_text(discounted_price), monthly_estimate),
            )
        ]
    return []


def _amounts_in_text(text: str) -> set[Decimal]:
    amounts: set[Decimal] = set()
    for match in _AMOUNT_RE.finditer(text):
        number = _decimal_or_none(match.group(0).replace(",", ""))
        if number is not None:
            amounts.add(number)
    return amounts


def _decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _text(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return "" if value is None else str(value)
