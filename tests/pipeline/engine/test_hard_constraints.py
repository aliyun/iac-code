from decimal import Decimal

import pytest

from iac_code.pipeline.engine.hard_constraints import (
    collect_hard_constraints,
    constraint_satisfied,
    validate_hard_constraint_checks,
)


def _constraint(**overrides):
    constraint = {
        "id": "storage-min",
        "target": "Database",
        "property": "storage",
        "operator": "gte",
        "value": 100,
        "unit": "GiB",
        "verification_mode": "direct",
        "source": "user",
        "source_text": "storage at least 100 GiB",
    }
    constraint.update(overrides)
    return constraint


def _check(
    constraint,
    *,
    status="satisfied",
    actual_value=120,
    actual_unit="GiB",
    parameter_values=None,
    evidence=None,
):
    return {
        "constraint": constraint,
        "status": status,
        "actual_value": actual_value,
        "actual_unit": actual_unit,
        "parameter_values": parameter_values or {"Storage": actual_value},
        "evidence": evidence
        or [{"type": "template", "summary": "resolved property", "actual_value": actual_value}],
    }


def test_collect_hard_constraints_uses_latest_constraint_version_by_id():
    original = _constraint()
    conflicting = _constraint(value=200)

    constraints, issues = collect_hard_constraints(
        {"intent": {"hard_constraints": [original]}, "candidate": {"hard_constraints": [original]}},
        ["intent.hard_constraints", "candidate.hard_constraints"],
    )
    assert constraints == [original]
    assert issues == []

    constraints, issues = collect_hard_constraints(
        {"intent": {"hard_constraints": [original]}, "candidate": {"hard_constraints": [conflicting]}},
        ["intent.hard_constraints", "candidate.hard_constraints"],
    )
    assert constraints == [conflicting]
    assert issues == []


@pytest.mark.parametrize(
    ("constraint", "actual_value", "actual_unit", "expected"),
    [
        (_constraint(operator="eq", value=4, unit="GiB"), 4096, "MiB", True),
        (_constraint(operator="gte", value=100, unit="GiB"), 120, "GiB", True),
        (_constraint(operator="lte", value=200, unit="GiB"), 220, "GiB", False),
        (_constraint(operator="in", value=["8.0", "8.4"], unit=None), "8.0", None, True),
        (_constraint(operator="not_in", value=[1, 2], unit="count"), 3, None, True),
        (_constraint(operator="eq", value=2, unit="count"), 2, None, True),
        (_constraint(operator="eq", value=2, unit=None), 2, "核", True),
        (_constraint(operator="eq", value=4, unit="G"), 4, "GiB", True),
        (_constraint(operator="eq", value=4, unit="GiB"), 4096, "MiB", True),
        (_constraint(operator="eq", value=4, unit="GiB"), 4, "G", True),
        (_constraint(operator="eq", value=2, unit="vCPUs"), 2, "count", True),
        (_constraint(operator="contains", value="private", unit=None), "private-network", None, True),
        (_constraint(operator="contains", value="20", unit=None), "2024", None, True),
        (_constraint(operator="not_contains", value="30", unit=None), "2024", None, True),
        (_constraint(operator="in", value=[4, "latest"], unit="GiB"), 4, "GiB", True),
        (_constraint(operator="not_in", value=[8, "latest"], unit="GiB"), 4, "GiB", True),
        (_constraint(operator="eq", value=False, unit=None), 0, None, False),
        (_constraint(operator="ne", value=False, unit=None), 0, None, True),
    ],
)
def test_constraint_satisfied_uses_generic_operators_and_units(
    constraint, actual_value, actual_unit, expected
):
    if constraint.get("unit") is None:
        constraint.pop("unit")
    assert constraint_satisfied(constraint, actual_value, actual_unit=actual_unit) is expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", Decimal("sNaN")])
def test_constraint_satisfied_rejects_non_finite_numbers_without_raising(value):
    assert constraint_satisfied(_constraint(operator="eq", value=value, unit=None), value) is False
    assert constraint_satisfied(_constraint(operator="gte", value=1, unit=None), value) is False


def test_validate_checks_covers_constraints_and_binds_final_parameters():
    constraint = _constraint()
    issues = validate_hard_constraint_checks(
        [constraint],
        [_check(constraint)],
        {"Storage": 120},
    )
    assert issues == []

    issues = validate_hard_constraint_checks(
        [constraint],
        [_check(constraint, status="unresolved")],
        {"Storage": 80},
    )
    assert [issue.code for issue in issues] == ["constraint_not_satisfied", "constraint_parameter_mismatch"]


def test_validate_checks_accepts_llm_pass_when_code_verification_fails():
    constraint = _constraint(verification_mode="tool")
    check = _check(
        constraint,
        evidence=[
            {
                "type": "tool",
                "summary": "preview contains only a VSwitch",
                "tool_name": "ros_preview_template",
                "result_path": "Stack.Resources",
                "actual_value": 0,
            }
        ],
    )
    records = [
        {
            "tool_name": "ros_preview_template",
            "input": {},
            "result": {"Stack": {"Resources": [{"ResourceType": "ALIYUN::ECS::VSwitch"}]}},
            "is_error": False,
        }
    ]

    assert validate_hard_constraint_checks(
        [constraint],
        [check],
        {"Storage": 120},
        tool_result_records=records,
    ) == []


def test_validate_checks_accepts_code_pass_when_llm_does_not_pass():
    constraint = _constraint()
    check = _check(constraint, status="unresolved")

    assert validate_hard_constraint_checks([constraint], [check], {"Storage": 120}) == []


def test_validate_checks_requires_actual_value_on_direct_evidence():
    constraint = _constraint()
    check = _check(
        constraint,
        status="unresolved",
        evidence=[{"type": "template", "summary": "resolved property"}],
    )

    issues = validate_hard_constraint_checks([constraint], [check], {"Storage": 120})

    assert [issue.code for issue in issues] == [
        "constraint_not_satisfied",
        "constraint_evidence_value_mismatch",
    ]


def test_validate_checks_accepts_empty_constraint_set_without_deployment_parameters():
    assert validate_hard_constraint_checks([], [], None) == []


def test_tool_verification_mode_requires_evidence_backed_by_a_real_tool_result():
    constraint = _constraint(verification_mode="tool")
    direct_check = _check(constraint, status="unresolved")
    issues = validate_hard_constraint_checks([constraint], [direct_check], {"Storage": 120})
    assert [issue.code for issue in issues] == ["constraint_not_satisfied", "missing_tool_evidence"]

    tool_evidence = {
        "type": "tool",
        "summary": "cloud metadata",
        "tool_name": "aliyun_api",
        "product": "rds",
        "action": "DescribeDBInstanceAttribute",
        "result_path": "Items.0.Storage",
        "actual_value": 120,
    }
    tool_check = _check(constraint, evidence=[tool_evidence])
    records = [
        {
            "tool_name": "aliyun_api",
            "input": {"product": "rds", "action": "DescribeDBInstanceAttribute"},
            "result": {"Items": [{"Storage": 120}]},
            "is_error": False,
        }
    ]
    assert validate_hard_constraint_checks(
        [constraint],
        [tool_check],
        {"Storage": 120},
        tool_result_records=records,
    ) == []

    mismatched_check = _check(
        constraint,
        status="unresolved",
        evidence=[{**tool_evidence, "actual_value": 80}],
    )
    issues = validate_hard_constraint_checks(
        [constraint],
        [mismatched_check],
        {"Storage": 120},
        tool_result_records=records,
    )
    assert {issue.code for issue in issues} == {
        "constraint_not_satisfied",
        "constraint_evidence_value_mismatch",
        "tool_evidence_value_mismatch",
        "tool_evidence_not_found",
    }

    records[0]["result"] = {"Items": [{"Storage": 80}]}
    unresolved_tool_check = _check(constraint, status="unresolved", evidence=[tool_evidence])
    issues = validate_hard_constraint_checks(
        [constraint],
        [unresolved_tool_check],
        {"Storage": 120},
        tool_result_records=records,
    )
    assert [issue.code for issue in issues] == ["constraint_not_satisfied", "tool_evidence_not_found"]
