from decimal import Decimal

from iac_code.pipeline.engine.cost_caliber import (
    parse_monthly_amounts,
    parse_planning_range,
    validate_cost_caliber,
)


def _conclusion(**overrides):
    conclusion = {
        "monthly_estimate": "¥281.77/月（列表价，合同优惠后约¥38.96/月）",
        "pricing_provenance": {
            "caliber": "pay_as_you_go_monthly",
            "list_price_source": "GetTemplateEstimateCost.OriginalAmount",
            "contract_price_source": "GetTemplateEstimateCost.TradeAmount",
        },
        "planning_deviation": {"status": "aligned"},
    }
    conclusion.update(overrides)
    return conclusion


def _context(planning_estimate="¥250-300/月（按量付费列表价，架构规划粗估）"):
    return {"candidate": {"monthly_estimate": planning_estimate}}


def _codes(issues):
    return [issue.code for issue in issues]


class TestParsing:
    def test_parse_monthly_amounts_keeps_list_price_first(self):
        amounts = parse_monthly_amounts("¥281.77/月（列表价，合同优惠后约¥38.96/月）")
        assert amounts == [Decimal("281.77"), Decimal("38.96")]

    def test_parse_monthly_amounts_handles_thousands_separator(self):
        assert parse_monthly_amounts("¥1,234.50/月") == [Decimal("1234.50")]

    def test_parse_planning_range_normalizes_order(self):
        assert parse_planning_range("¥190-140/月") == (Decimal("140"), Decimal("190"))

    def test_parse_planning_range_returns_none_without_amount(self):
        assert parse_planning_range("待评估") is None
        assert parse_planning_range(None) is None


class TestCaliber:
    def test_aligned_conclusion_passes(self):
        assert validate_cost_caliber(_context(), _conclusion()) == []

    def test_rejects_non_pay_as_you_go_caliber(self):
        conclusion = _conclusion(
            pricing_provenance={
                "caliber": "subscription_monthly",
                "list_price_source": "GetTemplateEstimateCost.OriginalAmount",
            }
        )
        assert "pricing_caliber_mismatch" in _codes(validate_cost_caliber(_context(), conclusion))

    def test_rejects_missing_list_price_source(self):
        conclusion = _conclusion(pricing_provenance={"caliber": "pay_as_you_go_monthly"})
        assert "list_price_source_missing" in _codes(validate_cost_caliber(_context(), conclusion))

    def test_rejects_missing_pricing_provenance(self):
        conclusion = _conclusion()
        del conclusion["pricing_provenance"]
        assert _codes(validate_cost_caliber(_context(), conclusion)) == ["pricing_provenance_missing"]


class TestContractPriceProvenance:
    def test_rejects_contract_price_without_source_or_estimate_label(self):
        conclusion = _conclusion(
            pricing_provenance={
                "caliber": "pay_as_you_go_monthly",
                "list_price_source": "GetTemplateEstimateCost.OriginalAmount",
            }
        )
        assert "contract_price_provenance_missing" in _codes(validate_cost_caliber(_context(), conclusion))

    def test_accepts_contract_price_marked_as_estimate(self):
        conclusion = _conclusion(
            monthly_estimate="¥281.77/月（列表价，合同优惠后约¥38.96/月，估算）",
            pricing_provenance={
                "caliber": "pay_as_you_go_monthly",
                "list_price_source": "GetTemplateEstimateCost.OriginalAmount",
                "contract_price_is_estimate": True,
            },
        )
        assert validate_cost_caliber(_context(), conclusion) == []

    def test_estimate_flag_without_label_is_rejected(self):
        conclusion = _conclusion(
            pricing_provenance={
                "caliber": "pay_as_you_go_monthly",
                "list_price_source": "GetTemplateEstimateCost.OriginalAmount",
                "contract_price_is_estimate": True,
            }
        )
        assert "contract_price_provenance_missing" in _codes(validate_cost_caliber(_context(), conclusion))

    def test_list_price_only_needs_no_contract_provenance(self):
        conclusion = _conclusion(
            monthly_estimate="¥281.77/月（列表价）",
            pricing_provenance={
                "caliber": "pay_as_you_go_monthly",
                "list_price_source": "GetTemplateEstimateCost.OriginalAmount",
            },
        )
        assert validate_cost_caliber(_context(), conclusion) == []


class TestPlanningDeviation:
    def test_rejects_unreported_deviation_outside_tolerance(self):
        issues = validate_cost_caliber(_context("¥80-120/月（按量付费列表价，架构规划粗估）"), _conclusion())
        assert _codes(issues) == ["planning_deviation_unreported"]

    def test_accepts_reported_deviation_with_reason(self):
        conclusion = _conclusion(
            planning_deviation={
                "status": "deviated",
                "planning_monthly_estimate": "¥80-120/月",
                "final_monthly_estimate": "¥281.77/月",
                "spec_changes": [{"item": "ECS InstanceType", "planned": "1vCPU/1GiB", "actual": "2vCPU/4GiB"}],
                "reason": "模板生成阶段按硬约束升档至 2vCPU/4GiB，列表价随之上升",
            }
        )
        assert validate_cost_caliber(_context("¥80-120/月"), conclusion) == []

    def test_rejects_reported_deviation_without_reason(self):
        conclusion = _conclusion(planning_deviation={"status": "deviated"})
        assert _codes(validate_cost_caliber(_context("¥80-120/月"), conclusion)) == [
            "planning_deviation_reason_missing"
        ]

    def test_accepts_price_within_tolerance_band(self):
        conclusion = _conclusion(monthly_estimate="¥330/月（列表价）")
        assert validate_cost_caliber(_context("¥250-300/月"), conclusion) == []

    def test_requires_unavailable_status_when_planning_estimate_unparsable(self):
        assert _codes(validate_cost_caliber(_context("待评估"), _conclusion())) == ["planning_deviation_status_invalid"]
        conclusion = _conclusion(planning_deviation={"status": "planning_estimate_unavailable"})
        assert validate_cost_caliber(_context("待评估"), conclusion) == []

    def test_rejects_unavailable_status_when_planning_estimate_exists(self):
        conclusion = _conclusion(planning_deviation={"status": "planning_estimate_unavailable"})
        assert _codes(validate_cost_caliber(_context(), conclusion)) == ["planning_deviation_status_invalid"]

    def test_rejects_missing_planning_deviation(self):
        conclusion = _conclusion()
        del conclusion["planning_deviation"]
        assert _codes(validate_cost_caliber(_context(), conclusion)) == ["planning_deviation_missing"]


class TestQuoteFailure:
    def test_quote_failure_with_error_skips_caliber_rules(self):
        conclusion = {"monthly_estimate": "询价失败", "error": "GetTemplateEstimateCost 返回 InvalidParameter"}
        assert validate_cost_caliber(_context(), conclusion) == []

    def test_quote_failure_without_error_is_rejected(self):
        assert _codes(validate_cost_caliber(_context(), {"monthly_estimate": "询价失败"})) == [
            "quote_failure_reason_missing"
        ]

    def test_empty_monthly_estimate_is_rejected(self):
        assert _codes(validate_cost_caliber(_context(), {"monthly_estimate": ""})) == ["monthly_estimate_missing"]
