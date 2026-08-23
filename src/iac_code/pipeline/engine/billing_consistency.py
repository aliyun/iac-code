"""Billing consistency validation across user intent, pricing result and deployment parameters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# ROS accepts many spellings for the same billing mode (see the InstanceChargeType
# AllowedValues of ALIYUN::ECS::InstanceGroup and friends). Normalize them so the
# three-way comparison does not depend on which spelling a template happens to use.
_PREPAID_ALIASES = {
    "pre",
    "prepaid",
    "prepay",
    "subscription",
    "包年包月",
    "预付费",
}
_POSTPAID_ALIASES = {
    "post",
    "postpaid",
    "postpay",
    "payasyougo",
    "payondemand",
    "paybyspec",
    "paybyclcu",
    "按量付费",
    "后付费",
    "cdt",
}

PREPAID = "prepaid"
POSTPAID = "postpaid"

# Template parameters and resource properties that carry a billing mode.
BILLING_PARAMETER_HINTS = (
    "instancechargetype",
    "chargetype",
    "paytype",
    "paymenttype",
    "billingmethod",
)


@dataclass(frozen=True)
class BillingValidationIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def normalize_charge_type(value: Any) -> str | None:
    """Normalize a billing mode spelling to ``prepaid``/``postpaid``.

    Returns ``None`` when the value is empty or cannot be classified, so callers can
    distinguish "not declared" from "declared but conflicting".
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    normalized = str(value).strip().casefold().replace(" ", "").replace("_", "").replace("-", "")
    if not normalized:
        return None
    if normalized in _PREPAID_ALIASES:
        return PREPAID
    if normalized in _POSTPAID_ALIASES:
        return POSTPAID
    return None


def charge_types_in_parameters(deployment_parameters: Any) -> dict[str, str]:
    """Collect normalized billing modes declared by deployment parameters."""
    if not isinstance(deployment_parameters, dict):
        return {}
    found: dict[str, str] = {}
    for name, value in deployment_parameters.items():
        if not isinstance(name, str):
            continue
        normalized_name = name.casefold().replace("_", "")
        if not any(hint in normalized_name for hint in BILLING_PARAMETER_HINTS):
            continue
        charge_type = normalize_charge_type(value)
        if charge_type is not None:
            found[name] = charge_type
    return found


def validate_billing_consistency(
    disclosure: Any,
    deployment_parameters: Any,
    *,
    required_fields: tuple[str, ...] = (
        "user_intent_charge_type",
        "priced_charge_type",
        "deployed_charge_type",
        "consistent",
    ),
) -> list[BillingValidationIssue]:
    """Validate that a billing disclosure tells the truth about the three parties.

    The disclosure must not claim consistency when the user intent, the priced mode
    and the mode actually written into ``deployment_parameters`` disagree, and any
    inconsistency must be escalated for explicit user confirmation instead of being
    silently repaired.
    """
    if not isinstance(disclosure, dict):
        return [BillingValidationIssue("missing_billing_disclosure")]

    issues: list[BillingValidationIssue] = []
    for field_name in required_fields:
        if disclosure.get(field_name) in (None, "", [], {}):
            issues.append(BillingValidationIssue("missing_billing_field", field_name))
    if issues:
        return issues

    user_intent = normalize_charge_type(disclosure.get("user_intent_charge_type"))
    priced = normalize_charge_type(disclosure.get("priced_charge_type"))
    declared_deployed = normalize_charge_type(disclosure.get("deployed_charge_type"))
    for field_name, normalized in (
        ("user_intent_charge_type", user_intent),
        ("priced_charge_type", priced),
        ("deployed_charge_type", declared_deployed),
    ):
        if normalized is None:
            issues.append(BillingValidationIssue("unrecognized_charge_type", field_name))
    if issues:
        return issues

    # The declared deployed mode must match what deployment_parameters really carry.
    parameter_charge_types = charge_types_in_parameters(deployment_parameters)
    conflicting = sorted(
        name for name, charge_type in parameter_charge_types.items() if charge_type != declared_deployed
    )
    if conflicting:
        issues.append(BillingValidationIssue("deployed_charge_type_mismatch", ", ".join(conflicting)))

    actual_consistent = user_intent == priced == declared_deployed
    claimed_consistent = disclosure.get("consistent") is True
    if claimed_consistent and not actual_consistent:
        issues.append(
            BillingValidationIssue(
                "billing_inconsistency_undisclosed",
                "user_intent={user}, priced={priced}, deployed={deployed}".format(
                    user=user_intent,
                    priced=priced,
                    deployed=declared_deployed,
                ),
            )
        )
    if not claimed_consistent:
        if disclosure.get("user_confirmation_required") is not True:
            issues.append(BillingValidationIssue("billing_user_confirmation_not_requested"))
        inconsistencies = disclosure.get("inconsistencies")
        if not isinstance(inconsistencies, list) or not inconsistencies:
            issues.append(BillingValidationIssue("missing_billing_inconsistencies"))
    return issues


def validate_priced_currency(
    declared_currency: Any,
    disclosure: Any,
) -> list[BillingValidationIssue]:
    """Validate that the reported currency equals the currency the pricing API returned."""
    if not isinstance(disclosure, dict):
        return [BillingValidationIssue("missing_billing_disclosure")]
    priced_currency = disclosure.get("priced_currency")
    if not isinstance(priced_currency, str) or not priced_currency.strip():
        return [BillingValidationIssue("missing_billing_field", "priced_currency")]
    if not isinstance(declared_currency, str) or not declared_currency.strip():
        return [BillingValidationIssue("missing_billing_field", "currency")]
    if declared_currency.strip().upper() != priced_currency.strip().upper():
        return [
            BillingValidationIssue(
                "currency_mismatch",
                "currency={declared}, priced_currency={priced}".format(
                    declared=declared_currency.strip(),
                    priced=priced_currency.strip(),
                ),
            )
        ]
    return []


def validate_billing_confirmation(
    notices: Any,
    confirmation: Any,
) -> list[BillingValidationIssue]:
    """Validate that an escalated billing inconsistency carries an explicit user decision."""
    if not isinstance(notices, list) or not notices:
        return []
    if not isinstance(confirmation, dict):
        return [BillingValidationIssue("missing_billing_confirmation")]
    issues: list[BillingValidationIssue] = []
    if confirmation.get("confirmed") not in (True, False):
        issues.append(BillingValidationIssue("missing_billing_field", "billing_confirmation.confirmed"))
    user_input = confirmation.get("user_input")
    if not isinstance(user_input, str) or not user_input.strip():
        issues.append(BillingValidationIssue("missing_billing_field", "billing_confirmation.user_input"))
    acknowledged = confirmation.get("acknowledged_charge_type")
    if normalize_charge_type(acknowledged) is None:
        issues.append(BillingValidationIssue("missing_billing_field", "billing_confirmation.acknowledged_charge_type"))
    return issues
