"""Acceptance distinguishes real period mistakes from free resources and prose."""

from copy import deepcopy

import pytest

from scripts.ci.run_coupon_budget_a2a import (
    accepted_price_calls,
    eight_hour_candidate_prices_match,
    hourly_costs_match,
)


@pytest.mark.parametrize(("outlines", "details", "expected"), [
    (["¥30～¥60（本次 8 小时使用合计，非月费）"], ["¥10/8 小时", "¥0（VPC 本身免费）"], True),
    (["¥24～¥40/8 小时"], ["¥3～¥5/小时", "¥0", "0", "免费"], True),
    (["¥300/月（小时费用待确认）"], ["¥3/小时"], False),
    (["¥3/小时"], ["¥300/月"], False),
    ([], ["¥3/小时"], False),
    (["¥2.60～¥2.65/8小时"], ["¥0.3165/h × 8h = ¥2.53", "¥0.0026/h × 8h = ¥0.02"], True),
    (["¥2.60/8H"], ["¥0.325/H", "0"], True),
    (["¥2.60/8h"], ["保有费 ¥0（配额 ≤2000 免收），流量费另计"], True),
    (["¥2.60/8h"], ["IP 保有费 ¥0/月（绑定 ECS 免收），流量费另计"], True),
    (["¥2.60/8h"], ["保有费免收，流量费另计"], True),
    (["¥2.60/8h"], ["系统盘费用包含在实例按量单价中"], True),
    (["¥2.60/8h"], ["included in instance hourly unit price"], True),
    (["¥2.60/8h"], ["¥5/月 → ¥0.0069/小时 × 8h ≈ ¥0.06/8小时"], True),
    (["¥5/month -> ¥0.0069/h × 8h ≈ ¥0.06/8h"], ["¥0.06/8h"], True),
    (["¥300/月（8h 费用待确认）"], ["¥3/h"], False),
    (["¥300/月，换算小时费用待确认"], ["¥3/h"], False),
    (["¥2.60/8h"], ["¥300/月（小时费用待确认）"], False),
    (["¥2.60/8h"], ["保有费 ¥0.5/月"], False),
    (["¥2.60/8h"], ["保有费 ¥0，但另收 5元/月"], False),
    (["¥2.60/8h"], ["免费额度用尽，保有费 ¥5/月"], False),
    (["¥2.60/8h"], ["未包含在实例单价中，待询价"], False),
    (["¥2.60/8h"], ["包含在实例月价中"], False),
    (["¥2.60/8h"], ["¥0.325/h", "另计（公网流量费不含）"], True),
    (["¥2.60/8h"], ["假定出流量10GB，单独计费，不含在总价中"], True),
    (["¥2.60/8h"], ["Outbound traffic billed separately"], True),
    (["¥2.60/8h"], ["流量费用待确认"], False),
    (["¥2.60/8h"], ["ECS ¥300/月，流量另计"], False),
    (["¥2.60/8h"], ["ECS 300/月，公网流量另计"], False),
    (["¥2.60/8h"], ["ECS $300/month, traffic billed separately"], False),
    (["¥300/月"], ["流量另计"], False),
])
def test_hourly_estimate_acceptance(outlines, details, expected):
    assert hourly_costs_match(outlines, details) is expected


def test_price_collection_uses_successful_presentations_instead_of_rejected_arguments():
    calls = [
        {"id": "plan", "name": "show_architecture_plan", "input": {"candidates": []}},
        {"id": "bad-json", "name": "show_candidate_detail", "input": {}},
        {"id": "detail", "name": "show_candidate_detail", "input": {"total_cost": "¥300/月"}},
        {"id": "pending", "name": "show_candidate_detail", "input": {}},
        {"id": "api", "name": "aliyun_api", "input": {}},
    ]
    outcomes = {name: {"is_error": False} for name in ("plan", "detail", "api")}
    outcomes["bad-json"] = {"is_error": True}
    assert [call["id"] for call in accepted_price_calls(calls, outcomes)] == ["plan", "detail"]
    # A successful but still-monthly detail remains visible to period validation.
    assert not hourly_costs_match(["¥3/8h"], [accepted_price_calls(calls, outcomes)[1]["input"]["total_cost"]])


def test_failed_presentations_cannot_supply_a_passing_hourly_estimate():
    calls = [{"id": "bad", "name": "show_candidate_detail", "input": {"total_cost": "¥3/8h"}}]
    assert accepted_price_calls(calls, {"bad": {"is_error": True}}) == []
    assert not hourly_costs_match([], [])


def _candidate_prices():
    return [
        {"id": "plan", "name": "show_architecture_plan", "input": {"candidates": [
            {"candidate_name": "按量主方案", "total_cost": "¥120/8 小时"},
            {"candidate_name": "包月方案（对比项）", "total_cost": "¥4500/月"},
        ]}},
        {"id": "main", "name": "show_candidate_detail", "input": {
            "candidate_index": 0, "candidate_name": "按量主方案", "total_cost": "¥120/8 小时",
            "resource_inventory": [{"rough_cost": "¥15/小时 × 8 小时"}, {"rough_cost": "免费"}],
        }},
        {"id": "comparison", "name": "show_candidate_detail", "input": {
            "candidate_index": 1, "candidate_name": "包月方案（对比项）", "total_cost": "¥4500/月",
            "resource_inventory": [{"rough_cost": "¥4500/月"}],
        }},
    ]


def test_explicit_monthly_comparison_does_not_fail_valid_eight_hour_main_plan():
    calls = _candidate_prices()
    original = deepcopy(calls)
    accepted = accepted_price_calls(calls, {c["id"]: {"is_error": False} for c in calls})
    assert eight_hour_candidate_prices_match(accepted)
    # Preserve the old flat verdict as a diagnostic; its monthly comparison still fails.
    assert not hourly_costs_match([c["input"]["total_cost"] for c in calls[1:]], ["¥15/小时", "¥4500/月"])
    assert calls == original


@pytest.mark.parametrize("defect", [
    "main_monthly_total", "main_monthly_item", "main_wrong_duration", "main_hourly_rate_only",
    "unmarked_comparison", "nonmonthly_comparison", "missing_main", "failed_main", "failed_comparison",
    "wrong_candidate_name", "no_presentation", "comparison_only", "hidden_comparison_label",
])
def test_comparison_exception_cannot_hide_invalid_or_missing_main_prices(defect):
    calls = _candidate_prices()
    outcomes = {c["id"]: {"is_error": False} for c in calls}
    if defect == "main_monthly_total":
        calls[1]["input"]["total_cost"] = "¥4500/月（8 小时价格待确认）"
    elif defect == "main_monthly_item":
        calls[1]["input"]["resource_inventory"][0]["rough_cost"] = "¥4500/月"
    elif defect == "main_wrong_duration":
        calls[1]["input"]["total_cost"] = "¥90/6 小时"
    elif defect == "main_hourly_rate_only":
        calls[1]["input"]["total_cost"] = "¥15/小时"
    elif defect in {"unmarked_comparison", "hidden_comparison_label"}:
        calls[0]["input"]["candidates"][1]["candidate_name"] = "包月方案"
        calls[2]["input"]["candidate_name"] = "包月方案"
        if defect == "hidden_comparison_label":
            calls[2]["input"]["cost_assumptions"] = ["仅供对比"]
    elif defect == "nonmonthly_comparison":
        calls[2]["input"]["total_cost"] = "费用待确认"
    elif defect == "missing_main":
        calls.pop(1)
    elif defect in {"failed_main", "failed_comparison"}:
        outcomes["main" if defect == "failed_main" else "comparison"]["is_error"] = True
    elif defect == "wrong_candidate_name":
        calls[1]["input"]["candidate_name"] = "另外的主方案"
    elif defect == "no_presentation":
        calls = []
    elif defect == "comparison_only":
        calls[0]["input"]["candidates"].pop(0)
        calls.pop(1)
        calls[1]["input"]["candidate_index"] = 0
    assert not eight_hour_candidate_prices_match(accepted_price_calls(calls, outcomes))


def test_period_check_accepts_main_without_comparison_and_visible_comparison_tradeoff():
    calls = _candidate_prices()
    calls[0]["input"]["candidates"].pop()
    assert eight_hour_candidate_prices_match(calls[:2])
    calls = _candidate_prices()
    calls[0]["input"]["candidates"][1]["candidate_name"] = "包月方案"
    calls[2]["input"]["candidate_name"] = "包月方案"
    calls[2]["input"]["key_tradeoff"] = "仅供对比，短租明显更贵"
    assert eight_hour_candidate_prices_match(calls)


def test_new_batch_cannot_borrow_old_details_and_latest_detail_is_authoritative():
    calls = _candidate_prices()
    calls[1]["input"]["total_cost"] = "¥4500/月"
    replacement = deepcopy(calls[1])
    replacement["input"]["total_cost"] = "¥120/8 小时"
    assert eight_hour_candidate_prices_match(calls + [replacement])
    assert not eight_hour_candidate_prices_match(calls + [deepcopy(calls[0])])
