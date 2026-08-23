"""Tests for cost-step monthly estimate parsing and zeroed-discount sanity validation."""

from decimal import Decimal

from iac_code.pipeline.engine.cost_estimate import (
    parse_monthly_estimate,
    validate_cost_estimate_sanity,
)

ZEROED_ESTIMATE = "¥289.81/月（列表价，合同优惠后约¥0.00/月）"
NORMAL_ESTIMATE = "¥289.81/月（列表价，合同优惠后约¥13.76/月）"


def _conclusion(monthly_estimate: str, **extra) -> dict:
    return {
        "monthly_estimate": monthly_estimate,
        "currency": "CNY",
        "resources": [{"type": "ALIYUN::ECS::InstanceGroup", "cost": "¥289.81/月"}],
        "template_fixed": False,
        "deployment_parameters": {"ZoneId": "cn-hangzhou-k"},
        "hard_constraint_checks": [],
        "preview_validation": {"succeeded": False, "error": "missing VpcId"},
        **extra,
    }


class TestParseMonthlyEstimate:
    def test_parses_both_price_gauges(self):
        prices = parse_monthly_estimate(NORMAL_ESTIMATE)

        assert prices.list_price == Decimal("289.81")
        assert prices.discounted_price == Decimal("13.76")
        assert prices.has_both_prices is True
        assert prices.pricing_failed is False

    def test_parses_zeroed_discounted_price(self):
        prices = parse_monthly_estimate(ZEROED_ESTIMATE)

        assert prices.list_price == Decimal("289.81")
        assert prices.discounted_price == Decimal("0.00")

    def test_parses_thousands_separator(self):
        prices = parse_monthly_estimate("¥12,345.60/月（列表价，合同优惠后约¥1,234.56/月）")

        assert prices.list_price == Decimal("12345.60")
        assert prices.discounted_price == Decimal("1234.56")

    def test_single_amount_is_the_list_price(self):
        prices = parse_monthly_estimate("¥289.81/月")

        assert prices.list_price == Decimal("289.81")
        assert prices.discounted_price is None
        assert prices.has_both_prices is False

    def test_single_amount_after_discount_marker_is_the_final_price(self):
        prices = parse_monthly_estimate("合同优惠后约¥13.76/月")

        assert prices.list_price is None
        assert prices.discounted_price == Decimal("13.76")

    def test_pricing_failure_is_reported(self):
        prices = parse_monthly_estimate("询价失败")

        assert prices.pricing_failed is True
        assert prices.has_both_prices is False

    def test_missing_or_unparsable_values_yield_no_prices(self):
        for value in (None, "", "   ", 289.81, "价格未知"):
            prices = parse_monthly_estimate(value)

            assert prices.list_price is None
            assert prices.discounted_price is None
            assert prices.pricing_failed is False


class TestValidateCostEstimateSanity:
    def test_rejects_zeroed_discount_without_basis(self):
        issues = validate_cost_estimate_sanity(_conclusion(ZEROED_ESTIMATE))

        assert [issue.code for issue in issues] == ["discounted_monthly_estimate_zeroed"]
        assert issues[0].detail == ZEROED_ESTIMATE

    def test_accepts_zeroed_discount_with_discount_basis(self):
        conclusion = _conclusion(ZEROED_ESTIMATE, discount_basis="Result.TradeAmount=0，账号命中 100% 合同折扣")

        assert validate_cost_estimate_sanity(conclusion) == []

    def test_accepts_zeroed_discount_explained_in_api_raw_summary(self):
        conclusion = _conclusion(ZEROED_ESTIMATE, api_raw_summary="TradeAmount 为 0，来源为免费额度")

        assert validate_cost_estimate_sanity(conclusion) == []

    def test_blank_basis_does_not_count_as_explanation(self):
        conclusion = _conclusion(ZEROED_ESTIMATE, discount_basis="   ", api_raw_summary="")

        assert [issue.code for issue in validate_cost_estimate_sanity(conclusion)] == [
            "discounted_monthly_estimate_zeroed"
        ]

    def test_accepts_fallback_to_list_price(self):
        conclusion = _conclusion("¥289.81/月（列表价，合同优惠后约¥289.81/月）")

        assert validate_cost_estimate_sanity(conclusion) == []

    def test_accepts_normal_contract_discount(self):
        assert validate_cost_estimate_sanity(_conclusion(NORMAL_ESTIMATE)) == []

    def test_rejects_suspiciously_low_discount_without_basis(self):
        conclusion = _conclusion("¥289.81/月（列表价，合同优惠后约¥1.00/月）")

        assert [issue.code for issue in validate_cost_estimate_sanity(conclusion)] == [
            "discounted_monthly_estimate_zeroed"
        ]

    def test_accepts_discount_just_above_the_suspicious_ratio(self):
        conclusion = _conclusion("¥289.81/月（列表价，合同优惠后约¥5.00/月）")

        assert validate_cost_estimate_sanity(conclusion) == []

    def test_rejects_negative_amounts(self):
        conclusion = _conclusion("¥289.81/月（列表价，合同优惠后约¥-1.00/月）")

        assert [issue.code for issue in validate_cost_estimate_sanity(conclusion)] == ["negative_monthly_estimate"]

    def test_pricing_failure_is_not_flagged(self):
        conclusion = _conclusion("询价失败", error="GetTemplateEstimateCost 报错")

        assert validate_cost_estimate_sanity(conclusion) == []

    def test_single_price_gauge_is_not_flagged(self):
        assert validate_cost_estimate_sanity(_conclusion("¥289.81/月")) == []

    def test_free_tier_list_price_is_not_flagged(self):
        conclusion = _conclusion("¥0.00/月（列表价，合同优惠后约¥0.00/月）")

        assert validate_cost_estimate_sanity(conclusion) == []

    def test_non_dict_conclusion_is_reported(self):
        assert [issue.code for issue in validate_cost_estimate_sanity("¥0/月")] == ["invalid_cost_conclusion"]

    def test_custom_field_names_are_honored(self):
        conclusion = {"cost_text": ZEROED_ESTIMATE, "basis": "TradeAmount=0，免费额度"}

        assert validate_cost_estimate_sanity(
            conclusion,
            monthly_estimate_field="cost_text",
            discount_basis_field="basis",
        ) == []
        assert [
            issue.code
            for issue in validate_cost_estimate_sanity(
                {"cost_text": ZEROED_ESTIMATE},
                monthly_estimate_field="cost_text",
                discount_basis_field="basis",
            )
        ] == ["discounted_monthly_estimate_zeroed"]

    def test_issue_to_dict_omits_empty_detail(self):
        issues = validate_cost_estimate_sanity("not-a-dict")

        assert issues[0].to_dict() == {"code": "invalid_cost_conclusion"}
