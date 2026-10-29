"""Acceptance distinguishes real period mistakes from free resources and prose."""

import pytest

from scripts.ci.run_coupon_budget_a2a import hourly_costs_match


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
])
def test_hourly_estimate_acceptance(outlines, details, expected):
    assert hourly_costs_match(outlines, details) is expected
