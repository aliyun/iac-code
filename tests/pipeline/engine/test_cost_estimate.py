"""Tests for deterministic monthly cost estimate plausibility checks."""

from decimal import Decimal

from iac_code.pipeline.engine.cost_estimate import (
    MIN_DISCOUNTED_RATIO,
    parse_monthly_amounts,
    validate_monthly_estimate,
)


class TestParseMonthlyAmounts:
    def test_parses_list_and_discounted_prices_in_order(self):
        amounts = parse_monthly_amounts("¥289.81/月（列表价，合同优惠后约¥13.76/月）")

        assert amounts == [Decimal("289.81"), Decimal("13.76")]

    def test_parses_thousands_separator_and_alternate_symbols(self):
        assert parse_monthly_amounts("￥1,234.50/月") == [Decimal("1234.50")]
        assert parse_monthly_amounts("CNY 96.80/月") == [Decimal("96.80")]

    def test_ignores_text_without_currency_amounts(self):
        assert parse_monthly_amounts("询价失败") == []


class TestValidateMonthlyEstimate:
    def test_rejects_zero_discounted_price_reported_from_the_session(self):
        issues = validate_monthly_estimate("¥289.81/月（列表价，合同优惠后约¥0.00/月）")

        assert [issue.code for issue in issues] == ["discounted_monthly_price_not_positive"]
        assert "289.81" in issues[0].detail

    def test_rejects_discounted_price_above_list_price(self):
        issues = validate_monthly_estimate("¥96.80/月（列表价，合同优惠后约¥120.00/月）")

        assert [issue.code for issue in issues] == ["discounted_monthly_price_above_list_price"]

    def test_rejects_discounted_price_below_plausible_ratio(self):
        issues = validate_monthly_estimate("¥1000.00/月（列表价，合同优惠后约¥1.00/月）")

        assert [issue.code for issue in issues] == ["discounted_monthly_price_implausible_ratio"]

    def test_accepts_real_contract_discount(self):
        assert validate_monthly_estimate("¥96.80/月（列表价，合同优惠后约¥13.76/月）") == []

    def test_accepts_identical_list_and_discounted_prices(self):
        assert validate_monthly_estimate("¥96.80/月（列表价，合同优惠后约¥96.80/月）") == []

    def test_accepts_single_price_and_pricing_failure(self):
        assert validate_monthly_estimate("¥289.81/月（列表价）") == []
        assert validate_monthly_estimate("询价失败") == []

    def test_accepts_genuinely_free_template(self):
        assert validate_monthly_estimate("¥0/月（列表价，合同优惠后约¥0/月）") == []

    def test_rejects_missing_or_empty_estimate(self):
        assert [issue.code for issue in validate_monthly_estimate(None)] == ["invalid_monthly_estimate"]
        assert [issue.code for issue in validate_monthly_estimate("   ")] == ["invalid_monthly_estimate"]

    def test_ratio_boundary_is_inclusive(self):
        boundary = Decimal("1000") * MIN_DISCOUNTED_RATIO

        assert validate_monthly_estimate("¥1000.00/月（列表价，合同优惠后约¥{}/月）".format(boundary)) == []
