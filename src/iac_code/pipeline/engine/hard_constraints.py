"""Generic validation for user-authored pipeline hard constraints."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class ConstraintValidationIssue:
    code: str
    constraint_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def collect_hard_constraints(
    context_snapshot: dict[str, Any],
    source_fields: list[str],
) -> tuple[list[dict[str, Any]], list[ConstraintValidationIssue]]:
    """Merge constraints from oldest to newest, with later user revisions winning by ID."""

    merged: dict[str, dict[str, Any]] = {}
    issues: list[ConstraintValidationIssue] = []
    for source_field in source_fields:
        raw_constraints = _resolve_dotted(context_snapshot, source_field)
        if raw_constraints is None:
            continue
        if not isinstance(raw_constraints, list):
            issues.append(ConstraintValidationIssue("invalid_constraint_source", detail=source_field))
            continue
        for raw_constraint in raw_constraints:
            if not isinstance(raw_constraint, dict):
                issues.append(ConstraintValidationIssue("invalid_constraint"))
                continue
            constraint_id = raw_constraint.get("id")
            if not isinstance(constraint_id, str) or not constraint_id:
                issues.append(ConstraintValidationIssue("missing_constraint_id"))
                continue
            merged[constraint_id] = raw_constraint
    return list(merged.values()), issues


def validate_hard_constraint_checks(
    constraints: list[dict[str, Any]],
    checks: Any,
    deployment_parameters: Any,
    *,
    tool_result_records: list[Any] | None = None,
    validate_tool_records: bool = True,
) -> list[ConstraintValidationIssue]:
    """Validate coverage and accept each constraint when either LLM or code verification succeeds."""

    issues: list[ConstraintValidationIssue] = []
    expected = {str(item.get("id") or ""): item for item in constraints if isinstance(item, dict)}
    if not expected:
        return []
    if not isinstance(checks, list):
        return [ConstraintValidationIssue("invalid_constraint_checks")]
    if not isinstance(deployment_parameters, dict):
        return [ConstraintValidationIssue("invalid_deployment_parameters")]

    checks_by_id: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict):
            issues.append(ConstraintValidationIssue("invalid_constraint_check"))
            continue
        checked_constraint = check.get("constraint")
        constraint_id = checked_constraint.get("id") if isinstance(checked_constraint, dict) else None
        if not isinstance(constraint_id, str) or not constraint_id:
            issues.append(ConstraintValidationIssue("missing_check_constraint_id"))
            continue
        if constraint_id in checks_by_id:
            issues.append(ConstraintValidationIssue("duplicate_constraint_check", constraint_id))
            continue
        checks_by_id[constraint_id] = check

    for constraint_id, constraint in expected.items():
        check = checks_by_id.get(constraint_id)
        if check is None:
            issues.append(ConstraintValidationIssue("missing_constraint_check", constraint_id))
            continue
        if check.get("constraint") != constraint:
            issues.append(ConstraintValidationIssue("constraint_copy_mismatch", constraint_id))
            continue
        verification_mode = constraint.get("verification_mode")
        if verification_mode not in {"direct", "tool"}:
            issues.append(ConstraintValidationIssue("invalid_constraint_verification_mode", constraint_id))
            continue

        code_issues: list[ConstraintValidationIssue] = []
        actual_value = check.get("actual_value")
        actual_unit = check.get("actual_unit")
        if not constraint_satisfied(constraint, actual_value, actual_unit=actual_unit):
            code_issues.append(ConstraintValidationIssue("constraint_comparison_failed", constraint_id))

        parameter_values = check.get("parameter_values")
        if not isinstance(parameter_values, dict):
            issues.append(ConstraintValidationIssue("invalid_constraint_parameter_values", constraint_id))
            continue
        else:
            for name, value in parameter_values.items():
                if name not in deployment_parameters or not _values_equal(deployment_parameters[name], value):
                    code_issues.append(
                        ConstraintValidationIssue("constraint_parameter_mismatch", constraint_id, str(name))
                    )

        evidence = check.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            issues.append(ConstraintValidationIssue("missing_constraint_evidence", constraint_id))
            continue
        matching_evidence = [
            item
            for item in evidence
            if isinstance(item, dict) and _values_equal(item.get("actual_value"), actual_value)
        ]
        if not matching_evidence:
            code_issues.append(ConstraintValidationIssue("constraint_evidence_value_mismatch", constraint_id))
        tool_evidence = [item for item in evidence if isinstance(item, dict) and item.get("type") == "tool"]
        if verification_mode == "tool":
            if not tool_evidence:
                code_issues.append(ConstraintValidationIssue("missing_tool_evidence", constraint_id))
            elif not any(_values_equal(item.get("actual_value"), actual_value) for item in tool_evidence):
                code_issues.append(ConstraintValidationIssue("tool_evidence_value_mismatch", constraint_id))
        if validate_tool_records:
            code_issues.extend(_validate_tool_evidence(constraint_id, tool_evidence, tool_result_records or []))

        llm_passed = check.get("status") == "satisfied"
        code_passed = not code_issues
        if not (llm_passed or code_passed):
            issues.append(ConstraintValidationIssue("constraint_not_satisfied", constraint_id))
            issues.extend(code_issues)

    for constraint_id in checks_by_id.keys() - expected.keys():
        issues.append(ConstraintValidationIssue("unexpected_constraint_check", constraint_id))
    return issues


def constraint_satisfied(constraint: dict[str, Any], actual_value: Any, *, actual_unit: Any = None) -> bool:
    """Evaluate a generic hard-constraint operator after compatible unit normalization."""

    operator = constraint.get("operator")
    expected_value = constraint.get("value")
    expected_unit = constraint.get("unit")
    if operator in {"in", "not_in"}:
        if not isinstance(expected_value, list):
            return False
        comparisons = [_comparable_pair(actual_value, item, actual_unit, expected_unit) for item in expected_value]
        matched = any(pair is not None and _values_equal(*pair) for pair in comparisons)
        return matched if operator == "in" else not matched

    if operator in {"contains", "not_contains"}:
        try:
            matched = expected_value in actual_value
        except TypeError:
            return False
        return matched if operator == "contains" else not matched

    pair = _comparable_pair(actual_value, expected_value, actual_unit, expected_unit)
    if pair is None:
        return False
    actual, expected = pair
    if operator == "eq":
        return _values_equal(actual, expected)
    if operator == "ne":
        return not _values_equal(actual, expected)
    if operator in {"gt", "gte", "lt", "lte"}:
        try:
            if operator == "gt":
                return actual > expected
            if operator == "gte":
                return actual >= expected
            if operator == "lt":
                return actual < expected
            return actual <= expected
        except (DecimalException, TypeError):
            return False
    return False


def _validate_tool_evidence(
    constraint_id: str,
    tool_evidence: list[dict[str, Any]],
    records: list[Any],
) -> list[ConstraintValidationIssue]:
    issues: list[ConstraintValidationIssue] = []
    for item in tool_evidence:
        if not _matching_tool_evidence_exists(item, records):
            issues.append(ConstraintValidationIssue("tool_evidence_not_found", constraint_id))
    return issues


def _matching_tool_evidence_exists(evidence: dict[str, Any], records: list[Any]) -> bool:
    for record in records:
        if not isinstance(record, dict) or record.get("is_error"):
            continue
        if record.get("tool_name") != evidence.get("tool_name"):
            continue
        tool_input = record.get("input") if isinstance(record.get("input"), dict) else {}
        if (
            evidence.get("product")
            and str(tool_input.get("product") or "").casefold() != str(evidence["product"]).casefold()
        ):
            continue
        if evidence.get("action") and tool_input.get("action") != evidence.get("action"):
            continue
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        actual = _resolve_dotted(result, str(evidence.get("result_path") or ""))
        if _values_equal(actual, evidence.get("actual_value")):
            return True
    return False


def _comparable_pair(actual: Any, expected: Any, actual_unit: Any, expected_unit: Any) -> tuple[Any, Any] | None:
    actual_unit_missing = actual_unit is None or not str(actual_unit).strip()
    expected_unit_missing = expected_unit is None or not str(expected_unit).strip()
    if actual_unit_missing and not expected_unit_missing:
        actual_unit = expected_unit
    elif expected_unit_missing and not actual_unit_missing:
        expected_unit = actual_unit
    actual_dimension, actual_factor = _unit_factor(actual_unit)
    expected_dimension, expected_factor = _unit_factor(expected_unit)
    if actual_dimension != expected_dimension:
        return None
    actual_number = _decimal_or_none(actual)
    expected_number = _decimal_or_none(expected)
    if actual_number is not None and expected_number is not None:
        return actual_number * actual_factor, expected_number * expected_factor
    if actual_factor != Decimal(1) or expected_factor != Decimal(1):
        return None
    return actual, expected


def _unit_factor(unit: Any) -> tuple[str, Decimal]:
    normalized = str(unit or "").strip().casefold().replace(" ", "")
    aliases: dict[str, tuple[str, Decimal]] = {
        "": ("", Decimal(1)),
        "count": ("count", Decimal(1)),
        "cpu": ("count", Decimal(1)),
        "vcpu": ("count", Decimal(1)),
        "vcpus": ("count", Decimal(1)),
        "core": ("count", Decimal(1)),
        "cores": ("count", Decimal(1)),
        "核": ("count", Decimal(1)),
        "核心": ("count", Decimal(1)),
        "b": ("bytes", Decimal(1)),
        "k": ("bytes", Decimal(1024)),
        "m": ("bytes", Decimal(1024) ** 2),
        "g": ("bytes", Decimal(1024) ** 3),
        "t": ("bytes", Decimal(1024) ** 4),
        "kb": ("bytes", Decimal(1000)),
        "mb": ("bytes", Decimal(1000) ** 2),
        "gb": ("bytes", Decimal(1000) ** 3),
        "tb": ("bytes", Decimal(1000) ** 4),
        "kib": ("bytes", Decimal(1024)),
        "mib": ("bytes", Decimal(1024) ** 2),
        "gib": ("bytes", Decimal(1024) ** 3),
        "tib": ("bytes", Decimal(1024) ** 4),
        "bps": ("bitrate", Decimal(1)),
        "kbps": ("bitrate", Decimal(1000)),
        "mbps": ("bitrate", Decimal(1000) ** 2),
        "gbps": ("bitrate", Decimal(1000) ** 3),
        "ms": ("time", Decimal("0.001")),
        "s": ("time", Decimal(1)),
        "min": ("time", Decimal(60)),
        "h": ("time", Decimal(3600)),
        "%": ("percent", Decimal(1)),
        "percent": ("percent", Decimal(1)),
    }
    return aliases.get(normalized, (f"literal:{normalized}", Decimal(1)))


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


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if _is_non_finite_number(left) or _is_non_finite_number(right):
        return False
    try:
        if left == right:
            return True
    except DecimalException:
        return False
    left_number = _decimal_or_none(left)
    right_number = _decimal_or_none(right)
    return left_number is not None and right_number is not None and left_number == right_number


def _is_non_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return False
    try:
        return not Decimal(str(value)).is_finite()
    except InvalidOperation:
        return False


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
