"""Sanity validation for the cost step's list-price / discounted-price monthly estimate."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# A discounted monthly price below this share of the list price is treated as suspicious.
# Real contract discounts stay well above it, while a mis-read discount field collapses to ~0.
SUSPICIOUS_DISCOUNT_RATIO = Decimal("0.01")

_PRICING_FAILED_MARKERS = ("询价失败", "pricing failed", "estimate failed")
_AMOUNT_PATTERN = re.compile(r"(?:¥|￥|CNY|RMB)\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)", re.IGNORECASE)
_DISCOUNTED_MARKERS = ("合同优惠后", "优惠后", "discounted", "after discount")


@dataclass(frozen=True)
class CostEstimateIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


@dataclass(frozen=True)
class MonthlyEstimatePrices:
    """Amounts parsed out of a ``monthly_estimate`` string."""

    list_price: Decimal | None = None
    discounted_price: Decimal | None = None
    pricing_failed: bool = False

    @property
    def has_both_prices(self) -> bool:
        return self.list_price is not None and self.discounted_price is not None


def parse_monthly_estimate(monthly_estimate: Any) -> MonthlyEstimatePrices:
    """Parse the list price and the contract-discounted price out of ``monthly_estimate``.

    The field is free-form text produced by the cost step, for example
    ``¥289.81/月（列表价，合同优惠后约¥0.00/月）``. Only amounts are extracted; when the
    step reports a pricing failure or no amount is present, the corresponding price stays
    ``None`` so callers can skip validation instead of guessing.
    """

    if not isinstance(monthly_estimate, str) or not monthly_estimate.strip():
        return MonthlyEstimatePrices()

    text = monthly_estimate.strip()
    if any(marker in text.casefold() for marker in _PRICING_FAILED_MARKERS):
        return MonthlyEstimatePrices(pricing_failed=True)

    matches = list(_AMOUNT_PATTERN.finditer(text))
    if not matches:
        return MonthlyEstimatePrices()

    amounts = [_decimal_or_none(match.group(1).replace(",", "")) for match in matches]
    if amounts[0] is None:
        return MonthlyEstimatePrices()
    if len(amounts) == 1:
        # A single amount carries only one gauge; a discount marker means it is already the
        # final price, otherwise treat it as the list price.
        if _is_discounted_amount(text, matches[0].start()):
            return MonthlyEstimatePrices(discounted_price=amounts[0])
        return MonthlyEstimatePrices(list_price=amounts[0])

    discounted = next(
        (
            amount
            for amount, match in zip(amounts[1:], matches[1:])
            if amount is not None and _is_discounted_amount(text, match.start())
        ),
        None,
    )
    return MonthlyEstimatePrices(list_price=amounts[0], discounted_price=discounted)


def validate_cost_estimate_sanity(
    conclusion: Any,
    *,
    monthly_estimate_field: str = "monthly_estimate",
    discount_basis_field: str = "discount_basis",
    api_raw_summary_field: str = "api_raw_summary",
) -> list[CostEstimateIssue]:
    """Reject a zeroed-out discounted monthly price that has no basis and no list-price fallback.

    A contract discount that collapses a running deployment's monthly cost to zero is either
    backed by an explicit basis or a mis-read discount field. Without a basis the step must
    fall back to the list price rather than reporting a free deployment.
    """

    if not isinstance(conclusion, dict):
        return [CostEstimateIssue("invalid_cost_conclusion")]

    monthly_estimate = conclusion.get(monthly_estimate_field)
    prices = parse_monthly_estimate(monthly_estimate)
    if prices.pricing_failed or not prices.has_both_prices:
        return []

    list_price = prices.list_price
    discounted_price = prices.discounted_price
    assert list_price is not None and discounted_price is not None  # noqa: S101 - narrowed by has_both_prices

    if discounted_price < 0 or list_price < 0:
        return [CostEstimateIssue("negative_monthly_estimate", str(monthly_estimate))]
    if list_price <= 0 or discounted_price > list_price * SUSPICIOUS_DISCOUNT_RATIO:
        return []
    if _has_discount_basis(conclusion, discount_basis_field, api_raw_summary_field):
        return []
    return [CostEstimateIssue("discounted_monthly_estimate_zeroed", str(monthly_estimate))]


def _has_discount_basis(conclusion: dict[str, Any], discount_basis_field: str, api_raw_summary_field: str) -> bool:
    for field in (discount_basis_field, api_raw_summary_field):
        value = conclusion.get(field)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _is_discounted_amount(text: str, amount_start: int) -> bool:
    preceding = text[:amount_start].casefold()
    return any(marker in preceding for marker in _DISCOUNTED_MARKERS)


def _decimal_or_none(value: str) -> Decimal | None:
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None
