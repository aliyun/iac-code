"""Generic validation for pipeline cost conclusions that mix currencies."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

SUPPORTED_CURRENCIES = ("CNY", "USD")

_SYMBOL_CURRENCIES = {
    "¥": "CNY",
    "￥": "CNY",
    "$": "USD",
}
_CODE_CURRENCIES = {
    "CNY": "CNY",
    "RMB": "CNY",
    "USD": "USD",
}
_CODE_PATTERN = re.compile(r"[A-Za-z]{3}")


@dataclass(frozen=True)
class CurrencyValidationIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def validate_currency_consistency(
    *,
    currency: Any,
    source_currency: Any,
    exchange_rate: Any,
    amount_texts: list[Any],
) -> list[CurrencyValidationIssue]:
    """Reject conclusions whose declared currency contradicts amounts or lacks an exchange rate."""

    if not isinstance(currency, str) or currency not in SUPPORTED_CURRENCIES:
        return [CurrencyValidationIssue("unsupported_currency", str(currency))]

    issues: list[CurrencyValidationIssue] = []
    if source_currency is not None:
        if not isinstance(source_currency, str) or source_currency not in SUPPORTED_CURRENCIES:
            issues.append(CurrencyValidationIssue("unsupported_source_currency", str(source_currency)))
        elif source_currency != currency and exchange_rate is None:
            issues.append(CurrencyValidationIssue("missing_exchange_rate", f"{source_currency}->{currency}"))

    if exchange_rate is not None:
        issues.extend(_validate_exchange_rate(exchange_rate, currency=currency, source_currency=source_currency))

    for text in amount_texts:
        detected = detect_currencies(text)
        for detected_currency in sorted(detected - {currency}):
            issues.append(CurrencyValidationIssue("amount_currency_mismatch", f"{detected_currency}:{text}"))
    return issues


def detect_currencies(text: Any) -> set[str]:
    """Detect currencies written into a human-readable amount string."""

    if not isinstance(text, str) or not text:
        return set()
    detected = {currency for symbol, currency in _SYMBOL_CURRENCIES.items() if symbol in text}
    for match in _CODE_PATTERN.findall(text):
        currency = _CODE_CURRENCIES.get(match.upper())
        if currency is not None:
            detected.add(currency)
    return detected


def _validate_exchange_rate(
    exchange_rate: Any,
    *,
    currency: str,
    source_currency: Any,
) -> list[CurrencyValidationIssue]:
    if not isinstance(exchange_rate, dict):
        return [CurrencyValidationIssue("invalid_exchange_rate", str(exchange_rate))]

    issues: list[CurrencyValidationIssue] = []
    rate_from = exchange_rate.get("from")
    rate_to = exchange_rate.get("to")
    if isinstance(source_currency, str) and rate_from != source_currency:
        issues.append(CurrencyValidationIssue("exchange_rate_source_mismatch", f"{rate_from}!={source_currency}"))
    if source_currency is None and rate_from not in SUPPORTED_CURRENCIES:
        issues.append(CurrencyValidationIssue("exchange_rate_source_mismatch", str(rate_from)))
    if rate_to != currency:
        issues.append(CurrencyValidationIssue("exchange_rate_target_mismatch", f"{rate_to}!={currency}"))
    rate = _positive_decimal_or_none(exchange_rate.get("rate"))
    if rate is None:
        issues.append(CurrencyValidationIssue("invalid_exchange_rate_value", str(exchange_rate.get("rate"))))
    elif rate_from == rate_to and rate != Decimal(1):
        issues.append(CurrencyValidationIssue("invalid_exchange_rate_value", str(exchange_rate.get("rate"))))
    return issues


def _positive_decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number
