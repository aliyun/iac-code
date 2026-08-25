"""Tests for pricing caliber reconciliation between architecture planning and cost estimating.

Root cause covered: architecture_planning 的月费粗估与 cost_estimating 的 ROS 列表价口径不一致
(约 2.5 倍偏差),且出现无来源的 ¥0 有效价。
"""

from __future__ import annotations

from decimal import Decimal

from iac_code.pipeline.engine.pricing_calibers import (
    DEFAULT_DEVIATION_THRESHOLD,
    CostEstimateIssue,
    validate_pricing_calibers,
)


def _codes(issues: list[CostEstimateIssue]) -> list[str]:
    return [issue.code for issue in issues]


def _conclusion(**calibers) -> dict:
    base = {
        "planning_estimate": "¥300/月",
        "list_price": "¥289.81/月",
        "calibers_aligned": True,
        "deviation_ratio": 0.97,
    }
    base.update(calibers)
    return {"monthly_estimate": "¥289.81/月", "pricing_calibers": base}


def _trade_amount_record() -> dict:
    return {
        "tool_name": "ros_estimate_template_cost",
        "is_error": False,
        "result": {"Result": {"OriginalAmount": "289.81", "TradeAmount": "0.00"}},
    }


class TestAlignedCalibers:
    def test_accepts_reconciled_calibers(self):
        issues = validate_pricing_calibers(_conclusion(), planning_estimate="¥300/月")
        assert issues == []

    def test_skips_validation_when_pricing_failed(self):
        conclusion = {"monthly_estimate": "询价失败"}
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []

    def test_skips_deviation_check_when_planning_estimate_unknown(self):
        conclusion = _conclusion(planning_estimate="未提供", calibers_aligned=False, deviation_ratio=None)
        del conclusion["pricing_calibers"]["deviation_ratio"]
        assert validate_pricing_calibers(conclusion) == []

    def test_issue_to_dict_drops_empty_detail(self):
        assert CostEstimateIssue("some_code").to_dict() == {"code": "some_code"}
        assert CostEstimateIssue("some_code", "d").to_dict() == {"code": "some_code", "detail": "d"}


class TestMissingReconciliation:
    def test_rejects_conclusion_without_calibers_block(self):
        issues = validate_pricing_calibers({"monthly_estimate": "¥289.81/月"}, planning_estimate="¥300/月")
        assert _codes(issues) == ["pricing_calibers_missing"]
        assert issues[0].detail == "pricing_calibers"

    def test_rejects_missing_planning_estimate(self):
        conclusion = _conclusion()
        del conclusion["pricing_calibers"]["planning_estimate"]
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月")
        assert "pricing_calibers_missing" in _codes(issues)
        assert any(issue.detail == "planning_estimate" for issue in issues)

    def test_rejects_missing_list_price(self):
        conclusion = _conclusion()
        del conclusion["pricing_calibers"]["list_price"]
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月")
        assert any(issue.detail == "list_price" for issue in issues)

    def test_rejects_missing_alignment_flag(self):
        conclusion = _conclusion()
        del conclusion["pricing_calibers"]["calibers_aligned"]
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月")
        assert any(issue.detail == "calibers_aligned" for issue in issues)

    def test_rejects_non_boolean_alignment_flag(self):
        conclusion = _conclusion(calibers_aligned="yes")
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月")
        assert any(issue.detail == "calibers_aligned" for issue in issues)

    def test_rejects_planning_estimate_not_copied_verbatim(self):
        # The planning estimate must be echoed as-is so the two calibers stay comparable.
        conclusion = _conclusion(planning_estimate="¥120/月")
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月")
        assert "planning_estimate_mismatch" in _codes(issues)

    def test_ignores_surrounding_whitespace_when_comparing(self):
        conclusion = _conclusion(planning_estimate="  ¥300/月  ")
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []

    def test_accepts_any_planning_estimate_when_context_missing(self):
        assert validate_pricing_calibers(_conclusion(planning_estimate="¥120/月")) == []


class TestDeviationExplanation:
    def test_requires_reason_for_the_reported_2_5x_deviation(self):
        # Evidence session d4c6272042684114b45d190aaefc753e: 粗估与列表价相差约 2.5 倍。
        conclusion = _conclusion(deviation_ratio=2.5)
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月")
        assert _codes(issues) == ["deviation_reason_missing"]

    def test_accepts_large_deviation_when_explained(self):
        conclusion = _conclusion(deviation_ratio=2.5, deviation_reason="粗估按 5Mbps 固定带宽,询价按 1Mbps 按量计费")
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []

    def test_requires_reason_when_deviation_is_far_below_threshold(self):
        conclusion = _conclusion(deviation_ratio=0.4)
        assert _codes(validate_pricing_calibers(conclusion, planning_estimate="¥300/月")) == [
            "deviation_reason_missing"
        ]

    def test_requires_reason_when_calibers_declared_unaligned(self):
        conclusion = _conclusion(calibers_aligned=False)
        assert _codes(validate_pricing_calibers(conclusion, planning_estimate="¥300/月")) == [
            "deviation_reason_missing"
        ]

    def test_requires_reason_when_ratio_is_not_computable(self):
        conclusion = _conclusion()
        del conclusion["pricing_calibers"]["deviation_ratio"]
        assert _codes(validate_pricing_calibers(conclusion, planning_estimate="¥300/月")) == [
            "deviation_reason_missing"
        ]

    def test_accepts_uncomputable_ratio_when_explained(self):
        conclusion = _conclusion(deviation_reason="粗估未给出区间中值,无法计算比值")
        del conclusion["pricing_calibers"]["deviation_ratio"]
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []

    def test_rejects_non_positive_ratio(self):
        conclusion = _conclusion(deviation_ratio=0, deviation_reason="x")
        assert _codes(validate_pricing_calibers(conclusion, planning_estimate="¥300/月")) == ["invalid_deviation_ratio"]

    def test_rejects_negative_ratio_even_with_reason(self):
        conclusion = _conclusion(deviation_ratio=-1.2, deviation_reason="x")
        assert _codes(validate_pricing_calibers(conclusion, planning_estimate="¥300/月")) == ["invalid_deviation_ratio"]

    def test_boundary_ratio_at_threshold_needs_no_reason(self):
        conclusion = _conclusion(deviation_ratio=float(DEFAULT_DEVIATION_THRESHOLD))
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []


class TestEffectivePriceSourcing:
    def test_accepts_absent_effective_price(self):
        assert validate_pricing_calibers(_conclusion(), planning_estimate="¥300/月") == []

    def test_accepts_effective_price_equal_to_list_price(self):
        conclusion = _conclusion(effective_price="¥289.81/月")
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []

    def test_rejects_discounted_effective_price_without_source(self):
        conclusion = _conclusion(effective_price="¥6.08/月")
        assert _codes(validate_pricing_calibers(conclusion, planning_estimate="¥300/月")) == ["discount_source_missing"]

    def test_accepts_discounted_effective_price_with_source(self):
        conclusion = _conclusion(effective_price="¥6.08/月", discount_source="GetTemplateEstimateCost.TradeAmount")
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []

    def test_rejects_zero_effective_price_without_any_trade_amount_evidence(self):
        # 核心缺陷:出现无来源的 ¥0.00/月 有效价。
        conclusion = _conclusion(effective_price="¥0.00/月", discount_source="合同优惠")
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月", tool_result_records=[])
        assert _codes(issues) == ["zero_effective_price_without_source"]

    def test_accepts_zero_effective_price_backed_by_trade_amount(self):
        conclusion = _conclusion(effective_price="¥0.00/月", discount_source="GetTemplateEstimateCost.TradeAmount")
        issues = validate_pricing_calibers(
            conclusion,
            planning_estimate="¥300/月",
            tool_result_records=[_trade_amount_record()],
        )
        assert issues == []

    def test_ignores_trade_amount_from_errored_tool_results(self):
        record = _trade_amount_record()
        record["is_error"] = True
        conclusion = _conclusion(effective_price="¥0.00/月", discount_source="合同优惠")
        issues = validate_pricing_calibers(conclusion, planning_estimate="¥300/月", tool_result_records=[record])
        assert _codes(issues) == ["zero_effective_price_without_source"]

    def test_rejects_unparsable_effective_price(self):
        conclusion = _conclusion(effective_price="免费")
        assert _codes(validate_pricing_calibers(conclusion, planning_estimate="¥300/月")) == ["invalid_effective_price"]

    def test_skips_sourcing_check_when_list_price_is_near_zero(self):
        conclusion = _conclusion(list_price="¥0.00/月", effective_price="¥0.00/月")
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []

    def test_parses_amounts_with_thousand_separators(self):
        conclusion = _conclusion(
            list_price="¥1,234.00/月",
            effective_price="¥1,230.00/月",
            deviation_ratio=1.0,
        )
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []


class TestCustomFieldsAndThreshold:
    def test_custom_field_names_are_honoured(self):
        conclusion = {
            "total": "¥100/月",
            "calibers": {
                "planning_estimate": "¥100/月",
                "list_price": "¥100/月",
                "calibers_aligned": True,
                "deviation_ratio": 1.0,
            },
        }
        issues = validate_pricing_calibers(
            conclusion,
            planning_estimate="¥100/月",
            calibers_field="calibers",
            monthly_estimate_field="total",
        )
        assert issues == []

    def test_custom_calibers_field_is_reported_in_missing_detail(self):
        issues = validate_pricing_calibers({"total": "¥1/月"}, calibers_field="calibers")
        assert issues[0].detail == "calibers"

    def test_stricter_threshold_flags_smaller_deviation(self):
        conclusion = _conclusion(deviation_ratio=1.15)
        assert validate_pricing_calibers(conclusion, planning_estimate="¥300/月") == []
        issues = validate_pricing_calibers(
            conclusion,
            planning_estimate="¥300/月",
            deviation_threshold=Decimal("1.1"),
        )
        assert _codes(issues) == ["deviation_reason_missing"]
