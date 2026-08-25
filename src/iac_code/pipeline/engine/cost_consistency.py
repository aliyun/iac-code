"""Consistency checks between architecture planning baselines and cost estimation results."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_AMOUNT_PATTERN = re.compile(r"\d+(?:[.,]\d+)*(?:\.\d+)?")

BUDGET_WITHIN = "within"
BUDGET_ABOVE = "above"
BUDGET_BELOW = "below"
BUDGET_UNKNOWN = "unknown"

_SPEC_PARAMETER_NAMES = {
    "instance_type": ("InstanceType", "EcsInstanceType"),
    "image_id": ("ImageId",),
}


@dataclass(frozen=True)
class CostConsistencyIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def parse_monthly_amounts(monthly_estimate: Any) -> tuple[Decimal | None, Decimal | None]:
    """Extract (list_price, discounted_price) from a rendered monthly estimate string.

    A single amount means only the list price is disclosed; the discounted price is None.
    """

    if not isinstance(monthly_estimate, str):
        return None, None
    amounts = [amount for amount in (_decimal_or_none(raw) for raw in _AMOUNT_PATTERN.findall(monthly_estimate))]
    amounts = [amount for amount in amounts if amount is not None]
    if not amounts:
        return None, None
    if len(amounts) == 1:
        return amounts[0], None
    return amounts[0], amounts[1]


def evaluate_budget_deviation(planned_budget: Any, monthly_estimate: Any) -> tuple[str, Decimal | None]:
    """Compare the actual monthly cost against the planned budget range."""

    actual, _discounted = parse_monthly_amounts(monthly_estimate)
    if actual is None:
        return BUDGET_UNKNOWN, None
    if not isinstance(planned_budget, dict):
        return BUDGET_UNKNOWN, actual
    minimum = _decimal_or_none(planned_budget.get("monthly_min"))
    maximum = _decimal_or_none(planned_budget.get("monthly_max"))
    if minimum is None or maximum is None:
        return BUDGET_UNKNOWN, actual
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    if actual > maximum:
        return BUDGET_ABOVE, actual
    if actual < minimum:
        return BUDGET_BELOW, actual
    return BUDGET_WITHIN, actual


def reconcile_instance_spec(
    planned_compute: Any,
    deployment_parameters: Any,
    preview_parameters: Any,
) -> list[CostConsistencyIssue]:
    """Verify the planned compute spec is carried unchanged into pricing and preview parameters."""

    if not isinstance(planned_compute, dict):
        return []
    issues: list[CostConsistencyIssue] = []
    deployment = deployment_parameters if isinstance(deployment_parameters, dict) else {}
    preview = preview_parameters if isinstance(preview_parameters, dict) else {}
    for field, parameter_names in _SPEC_PARAMETER_NAMES.items():
        planned = planned_compute.get(field)
        if not isinstance(planned, str) or not planned.strip():
            continue
        pricing_value = _first_parameter(deployment, parameter_names)
        if pricing_value is None:
            issues.append(CostConsistencyIssue("spec_missing_in_deployment_parameters", field))
        elif not _spec_equal(pricing_value, planned):
            issues.append(CostConsistencyIssue("spec_deviates_from_plan", f"{field}: {planned} -> {pricing_value}"))
        preview_value = _first_parameter(preview, parameter_names)
        if preview_value is not None and pricing_value is not None and not _spec_equal(preview_value, pricing_value):
            issues.append(CostConsistencyIssue("spec_preview_mismatch", f"{field}: {pricing_value} -> {preview_value}"))
    return issues


def evaluate_discount_disclosure(monthly_estimate: Any) -> list[CostConsistencyIssue]:
    """Reject a contract-discount claim when the discounted price equals the list price."""

    if not isinstance(monthly_estimate, str) or "合同优惠" not in monthly_estimate:
        return []
    list_price, discounted = parse_monthly_amounts(monthly_estimate)
    if list_price is None or discounted is None:
        return []
    if list_price == discounted:
        return [CostConsistencyIssue("discount_without_reduction", f"{list_price}")]
    return []


def validate_cost_consistency(
    planned_compute: Any,
    planned_budget: Any,
    conclusion: dict[str, Any],
    *,
    spec_reconciliation_field: str = "spec_reconciliation",
    budget_deviation_field: str = "budget_deviation",
) -> list[CostConsistencyIssue]:
    """Validate spec reconciliation, budget deviation disclosure and discount honesty."""

    monthly_estimate = conclusion.get("monthly_estimate")
    deployment_parameters = conclusion.get("deployment_parameters")
    preview_validation = conclusion.get("preview_validation")
    preview_parameters = preview_validation.get("parameters") if isinstance(preview_validation, dict) else None

    issues = reconcile_instance_spec(planned_compute, deployment_parameters, preview_parameters)
    issues.extend(evaluate_discount_disclosure(monthly_estimate))

    if isinstance(planned_compute, dict) and any(
        isinstance(planned_compute.get(field), str) and planned_compute[field].strip()
        for field in _SPEC_PARAMETER_NAMES
    ):
        reconciliation = conclusion.get(spec_reconciliation_field)
        if not isinstance(reconciliation, dict):
            issues.append(CostConsistencyIssue("missing_spec_reconciliation"))
        else:
            reported_match = reconciliation.get("matches_plan")
            actual_match = not any(issue.code == "spec_deviates_from_plan" for issue in issues)
            if reported_match is not actual_match:
                issues.append(CostConsistencyIssue("spec_reconciliation_mismatch", str(reported_match)))
            if not actual_match and not str(reconciliation.get("deviation_note") or "").strip():
                issues.append(CostConsistencyIssue("missing_spec_deviation_note"))

    status, actual = evaluate_budget_deviation(planned_budget, monthly_estimate)
    if status != BUDGET_UNKNOWN:
        deviation = conclusion.get(budget_deviation_field)
        if not isinstance(deviation, dict):
            issues.append(CostConsistencyIssue("missing_budget_deviation"))
        else:
            if deviation.get("status") != status:
                issues.append(CostConsistencyIssue("budget_deviation_status_mismatch", f"{status}"))
            reported_actual = _decimal_or_none(deviation.get("actual_monthly"))
            if actual is not None and (reported_actual is None or reported_actual != actual):
                issues.append(CostConsistencyIssue("budget_deviation_amount_mismatch", f"{actual}"))
            if status in {BUDGET_ABOVE, BUDGET_BELOW} and not str(deviation.get("note") or "").strip():
                issues.append(CostConsistencyIssue("missing_budget_deviation_note", status))
    return issues


def _first_parameter(parameters: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = parameters.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _spec_equal(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None
