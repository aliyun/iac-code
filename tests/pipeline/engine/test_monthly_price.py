"""Tests for the monthly list price vs contract-discounted price validation."""

from __future__ import annotations

import pytest

from iac_code.pipeline.engine.monthly_price import validate_monthly_price_breakdown

LIST_AND_DISCOUNTED_TEXT = "¥96.80/月（列表价，合同优惠后约¥13.76/月）"
SAME_PRICE_TEXT = "¥373.54/月（列表价，合同优惠后约¥373.54/月）"


def _codes(monthly_estimate, breakdown) -> list[str]:
    return [issue.code for issue in validate_monthly_price_breakdown(monthly_estimate, breakdown)]


class TestAcceptedDisclosures:
    def test_real_discount_passes(self):
        breakdown = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": True}
        assert validate_monthly_price_breakdown(LIST_AND_DISCOUNTED_TEXT, breakdown) == []

    def test_zero_discount_with_explicit_reason_passes(self):
        breakdown = {
            "list_price": 373.54,
            "discounted_price": 373.54,
            "discount_applied": False,
            "same_price_reason": "当前账号无合同优惠，折扣为 0",
        }
        assert validate_monthly_price_breakdown(SAME_PRICE_TEXT, breakdown) == []

    def test_pricing_failure_is_exempt(self):
        assert validate_monthly_price_breakdown("询价失败", None) == []
        assert validate_monthly_price_breakdown("  询价失败  ", {"list_price": "x"}) == []

    def test_string_amounts_are_accepted(self):
        breakdown = {"list_price": "96.80", "discounted_price": "13.76", "discount_applied": True}
        assert validate_monthly_price_breakdown(LIST_AND_DISCOUNTED_TEXT, breakdown) == []

    def test_thousands_separator_in_text_is_matched(self):
        breakdown = {"list_price": 1234.50, "discounted_price": 1000, "discount_applied": True}
        text = "¥1,234.50/月（列表价，合同优惠后约¥1,000/月）"
        assert validate_monthly_price_breakdown(text, breakdown) == []


class TestRejectedDisclosures:
    def test_identical_prices_without_reason_are_rejected(self):
        """The reported regression: both prices equal and no zero-discount explanation."""
        breakdown = {"list_price": 373.54, "discounted_price": 373.54, "discount_applied": True}
        codes = _codes(SAME_PRICE_TEXT, breakdown)
        assert "discount_applied_without_price_difference" in codes
        assert "missing_same_price_reason" in codes

    def test_identical_prices_with_blank_reason_are_rejected(self):
        breakdown = {
            "list_price": 373.54,
            "discounted_price": 373.54,
            "discount_applied": False,
            "same_price_reason": "   ",
        }
        assert _codes(SAME_PRICE_TEXT, breakdown) == ["missing_same_price_reason"]

    def test_price_difference_must_declare_discount(self):
        breakdown = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": False}
        assert _codes(LIST_AND_DISCOUNTED_TEXT, breakdown) == ["discount_not_declared_despite_price_difference"]

    def test_discounted_price_above_list_price_is_rejected(self):
        breakdown = {"list_price": 10, "discounted_price": 20, "discount_applied": True}
        text = "¥10/月（列表价，合同优惠后约¥20/月）"
        assert _codes(text, breakdown) == ["discounted_price_above_list_price"]

    def test_missing_breakdown_is_rejected(self):
        assert _codes("¥96.80/月", None) == ["missing_monthly_price_breakdown"]

    @pytest.mark.parametrize("estimate", ["", "   ", None, 96.8])
    def test_invalid_monthly_estimate_is_rejected(self, estimate):
        breakdown = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": True}
        assert _codes(estimate, breakdown) == ["invalid_monthly_estimate"]

    @pytest.mark.parametrize("value", [None, "", "abc", -1, True, [96.8]])
    def test_invalid_list_price_is_rejected(self, value):
        breakdown = {"list_price": value, "discounted_price": 13.76, "discount_applied": True}
        assert "invalid_list_price" in _codes(LIST_AND_DISCOUNTED_TEXT, breakdown)

    @pytest.mark.parametrize("value", [None, "", "abc", -1, True])
    def test_invalid_discounted_price_is_rejected(self, value):
        breakdown = {"list_price": 96.80, "discounted_price": value, "discount_applied": True}
        assert "invalid_discounted_price" in _codes(LIST_AND_DISCOUNTED_TEXT, breakdown)

    @pytest.mark.parametrize("value", [None, "true", 1, "yes"])
    def test_non_boolean_discount_applied_is_rejected(self, value):
        breakdown = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": value}
        assert _codes(LIST_AND_DISCOUNTED_TEXT, breakdown) == ["invalid_discount_applied"]

    def test_text_without_any_amount_is_rejected(self):
        breakdown = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": True}
        assert _codes("费用待确认", breakdown) == ["monthly_estimate_missing_amount"]

    def test_text_list_price_must_match_breakdown(self):
        breakdown = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": True}
        text = "¥500/月（列表价，合同优惠后约¥13.76/月）"
        assert _codes(text, breakdown) == ["monthly_estimate_list_price_mismatch"]

    def test_text_discounted_price_must_match_breakdown(self):
        breakdown = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": True}
        text = "¥96.80/月（列表价，合同优惠后约¥50/月）"
        assert _codes(text, breakdown) == ["monthly_estimate_discounted_price_mismatch"]

    def test_single_amount_text_is_accepted_only_when_prices_are_equal(self):
        equal = {
            "list_price": 373.54,
            "discounted_price": 373.54,
            "discount_applied": False,
            "same_price_reason": "折扣为 0",
        }
        assert validate_monthly_price_breakdown("¥373.54/月", equal) == []

        differing = {"list_price": 96.80, "discounted_price": 13.76, "discount_applied": True}
        assert _codes("¥96.80/月", differing) == ["monthly_estimate_discounted_price_mismatch"]


class TestIssueSerialization:
    def test_issue_to_dict_drops_empty_detail(self):
        issues = validate_monthly_price_breakdown("¥96.80/月", None)
        assert [issue.to_dict() for issue in issues] == [{"code": "missing_monthly_price_breakdown"}]

    def test_issue_to_dict_keeps_detail(self):
        breakdown = {"list_price": 10, "discounted_price": 20, "discount_applied": True}
        issue = validate_monthly_price_breakdown("¥10/月 ¥20/月", breakdown)[0]
        assert issue.to_dict() == {"code": "discounted_price_above_list_price", "detail": "20 > 10"}
