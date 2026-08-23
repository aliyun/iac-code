"""Deterministic plausibility checks for pipeline cost estimate conclusions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException

# ROS pricing returns OriginalAmount (list price) and TradeAmount (contract price).
# Real Alibaba Cloud contract discounts never drive a positive list price down to
# a rounding-level residue, so a discounted price below this ratio means the model
# mishandled a missing/zero TradeAmount instead of reporting a real price.
MIN_DISCOUNTED_RATIO = Decimal("0.01")

_AMOUNT_PATTERN = re.compile(r"(?:¥|￥|CNY\s*|RMB\s*)\s*(\d+(?:,\d{3})*(?:\.\d+)?)", re.IGNORECASE)


@dataclass(frozen=True)
class MonthlyEstimateIssue:
    code: str
    detail: str = ""


def parse_monthly_amounts(monthly_estimate: str) -> list[Decimal]:
    """Extract the currency amounts from a monthly estimate string, in text order."""

    amounts: list[Decimal] = []
    for raw in _AMOUNT_PATTERN.findall(monthly_estimate):
        try:
            amounts.append(Decimal(raw.replace(",", "")))
        except (DecimalException, ValueError):
            continue
    return amounts


def validate_monthly_estimate(
    monthly_estimate: object,
    *,
    min_discounted_ratio: Decimal = MIN_DISCOUNTED_RATIO,
) -> list[MonthlyEstimateIssue]:
    """Validate the list/discounted price pair carried by ``monthly_estimate``.

    Only the dual-price form is constrained: when a positive list price is reported
    together with a discounted price, the discounted price must be positive, must not
    exceed the list price, and must stay above ``min_discounted_ratio`` of it. Single
    prices, genuinely free templates and the ``"询价失败"`` form stay valid.
    """

    if not isinstance(monthly_estimate, str) or not monthly_estimate.strip():
        return [MonthlyEstimateIssue("invalid_monthly_estimate")]

    amounts = parse_monthly_amounts(monthly_estimate)
    if len(amounts) < 2:
        return []

    list_price, discounted_price = amounts[0], amounts[1]
    if list_price <= 0:
        return []

    if discounted_price <= 0:
        return [
            MonthlyEstimateIssue(
                "discounted_monthly_price_not_positive",
                "list={} discounted={}".format(list_price, discounted_price),
            )
        ]
    if discounted_price > list_price:
        return [
            MonthlyEstimateIssue(
                "discounted_monthly_price_above_list_price",
                "list={} discounted={}".format(list_price, discounted_price),
            )
        ]
    if discounted_price < list_price * min_discounted_ratio:
        return [
            MonthlyEstimateIssue(
                "discounted_monthly_price_implausible_ratio",
                "list={} discounted={} min_ratio={}".format(list_price, discounted_price, min_discounted_ratio),
            )
        ]
    return []
