"""Keep the live price oracle strict about charges while accepting display rounding."""

from decimal import Decimal

import pytest

from scripts.ci.run_plan_cost_refresh_a2a import matches_estimate, price_bounds


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("约 ¥7.5～¥14.5 / 48 小时（流量 ¥1～¥5 另计）", ("7.5", "14.5")),
        ("CNY 7.50-14.50 / 2 days", ("7.5", "14.5")),
        ("￥3.75～7.25 / 12小时", ("3.75", "7.25")),
        ("¥0/小时", ("0", "0")),
    ],
)
def test_price_bounds_reads_total_before_excluded_usage(text, expected):
    assert price_bounds(text) == tuple(Decimal(value) for value in expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¥3.8～¥7.3 / 12小时＋流量¥1～¥5另计", True),
        ("¥3.75～¥7.25 / 12小时", True),
        ("¥5.5～¥10.5 / 12小时", False),  # Invented minimum billing periods.
        ("¥4.75～¥12.25 / 12小时", False),  # Usage included in total.
        ("¥3.75～¥7.35 / 12小时", False),
        ("待确认", False),
    ],
)
def test_estimate_oracle_accepts_only_small_display_rounding(text, expected):
    assert matches_estimate(text, (Decimal("3.75"), Decimal("7.25"))) is expected
