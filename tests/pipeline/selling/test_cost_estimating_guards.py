from __future__ import annotations

import re
from pathlib import Path

import pytest

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _cost_estimating_step():
    loaded = load_pipeline_dir(_selling_dir())
    sub_spec = loaded.sub_pipelines["evaluate_candidate"]
    return next(step for step in sub_spec.steps if step.step_id == "cost_estimating")


def _zero_amount_guard() -> dict:
    step = _cost_estimating_step()
    return next(
        guard
        for guard in step.completion_guards
        if guard.get("message_key") == "cost_zero_monthly_estimate_forbidden"
    )


def test_cost_estimating_forbids_zero_monthly_estimate():
    guard = _zero_amount_guard()

    assert guard.get("forbid_completion") is True
    assert "monthly_estimate" in (guard.get("when_conclusion_field_matches") or {})


def test_cost_estimating_requires_gap_declaration_when_preview_fails():
    step = _cost_estimating_step()

    guard = next(
        guard
        for guard in step.completion_guards
        if guard.get("message_key") == "cost_preview_failed_requires_gap_declaration"
    )
    assert guard.get("when_conclusion_field_equals") == {"preview_validation.succeeded": False}
    assert guard.get("required_conclusion_any_of") == ["missing_deployment_parameters", "error"]


@pytest.mark.parametrize(
    "value",
    ["¥0/月", "¥0", "¥0.00/月", "0元/月", "¥0,00/月", "CNY 0/month", "0.0"],
)
def test_zero_amount_pattern_matches_bare_zero_estimates(value):
    pattern = _zero_amount_guard()["when_conclusion_field_matches"]["monthly_estimate"]

    assert re.search(pattern, value, flags=re.IGNORECASE) is not None


@pytest.mark.parametrize(
    "value",
    [
        "¥96.80/月（列表价，合同优惠后约¥13.76/月）",
        "约¥10~¥60/月（按量计费，按 50GB 存储 + 100GB CDN 流量估算）",
        "询价失败",
        "¥800/月",
        "¥0.5/小时",
        "¥1,024/月",
        "¥10/月",
        "¥0.01/月",
    ],
)
def test_zero_amount_pattern_does_not_match_valid_estimates(value):
    pattern = _zero_amount_guard()["when_conclusion_field_matches"]["monthly_estimate"]

    assert re.search(pattern, value, flags=re.IGNORECASE) is None
