"""Keep mock identity/pagination and answer acceptance from hiding model errors."""

import pytest

from scripts.ci.coupon_fixtures import coupon, response
from scripts.ci.run_coupon_budget_mock_a2a import amount_matches, cash_cell, evaluate_answer, has_assistant_reference


@pytest.mark.parametrize(("text", "expected"), [
    ("[AI Assistant](https://ros.console.aliyun.com/cn-beijing/chat)", True),
    ("https://ROS.CONSOLE.ALIYUN.COM/cn-hangzhou/chat", True),
    ("https://example.com/ros.console.aliyun.com/cn-beijing/chat", False),
    ("https://ros.console.aliyun.com.example.com/cn-beijing/chat", False),
    ("https://ros.console.aliyun.com@example.com/cn-beijing/chat", False),
    ("https://example.com/?next=https%3A%2F%2Fros.console.aliyun.com%2Fchat", False),
    ("https://[invalid/path", False),
    ("No link", False),
])
def test_assistant_reference_uses_the_exact_url_host(text, expected):
    assert has_assistant_reference(text) is expected


def test_an_old_coupon_id_cannot_observe_a_new_fixture():
    old = coupon("applicable")["CouponId"]
    result = response("balance_60", "DescribeCouponItemList", {"CouponId": old})
    assert result["Data"] == []


def test_pagination_does_not_repeat_the_coupon_on_later_pages():
    first = response("applicable", "DescribeCoupon", {"CurrentPage": 1, "PageSize": 1})
    second = response("applicable", "DescribeCoupon", {"CurrentPage": 2, "PageSize": 1})
    assert len(first["Data"]) == 1
    assert second["Data"] == []
    assert first["TotalCount"] == second["TotalCount"] == 1


def test_answer_acceptance_ignores_the_face_value_and_estimate_rows():
    answer = "| 券面值 | 300 元 |\n| GPU计算费 | 240 元 |\n| 计算部分需补现金 | **180.00 元** |"
    assert cash_cell(answer) == "**180.00 元**"
    assert amount_matches(cash_cell(answer), 180)
    assert not amount_matches(cash_cell(answer), 300)


def test_horizontal_answer_table_is_not_mistaken_for_a_missing_cash_value():
    answer = "| GPU计算费 | 本次计算费可抵扣金额 | 计算部分需补现金 |\n|---|---|---|\n|240 元|240 元|0 元|"
    assert cash_cell(answer) == "0 元"
    assert all(evaluate_answer("applicable", answer).values())


def test_an_explanation_mentioning_cash_is_not_a_horizontal_header():
    answer = (
        "| 项目 | 结果 |\n|---|---|\n"
        "| 券的可用余额 | 300 元（满减券，非现金抵扣余额） |\n"
        "| 本次计算费可抵扣金额 | 0 元（未达门槛） |\n"
        "| 计算部分需补现金 | 240 元 |"
    )
    assert cash_cell(answer) == "240 元"
    assert all(evaluate_answer("threshold", answer).values())


def test_a_known_zero_deduction_is_distinct_from_uncertain_coupon_eligibility():
    estimate = "|本次计算费可抵扣金额|¥0|\n|计算部分需补现金|待确认 → 预计 ¥240|"
    uncertain = "|本次计算费可抵扣金额|待确认|\n|计算部分需补现金|待确认|"
    assert all(evaluate_answer("gpu_excluded", estimate).values())
    assert not all(evaluate_answer("expired", uncertain).values())


@pytest.mark.parametrize(("cell", "expected", "matches"), [
    ("¥0.00（仅计算费用，实际结算为准）", 0, True),
    ("180 元", 180, True),
    ("0 元（待确认项：无，判定明确）", 0, True),
    ("0 元（待确认项：无，判定明确）", None, False),
    ("0 元（待确认金额）", 0, False),
    ("待确认（如果可用则 0 元）", 0, False),
    ("待确认（如果可用则 0 元）", None, True),
    ("¥0.00", None, False),
    ("", None, False),
])
def test_cash_acceptance_preserves_unknowns(cell, expected, matches):
    assert amount_matches(cell, expected) is matches
