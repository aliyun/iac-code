"""Acceptance distinguishes real period mistakes from free resources and prose."""

import pytest

from scripts.ci.run_coupon_budget_a2a import hourly_costs_match


@pytest.mark.parametrize(("outlines", "details", "expected"), [
    (["¥30～¥60（本次 8 小时使用合计，非月费）"], ["¥10/8 小时", "¥0（VPC 本身免费）"], True),
    (["¥24～¥40/8 小时"], ["¥3～¥5/小时", "¥0", "0", "免费"], True),
    (["¥300/月（小时费用待确认）"], ["¥3/小时"], False),
    (["¥3/小时"], ["¥300/月"], False),
    ([], ["¥3/小时"], False),
])
def test_hourly_estimate_acceptance(outlines, details, expected):
    assert hourly_costs_match(outlines, details) is expected
