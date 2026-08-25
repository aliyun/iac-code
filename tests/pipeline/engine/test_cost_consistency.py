from decimal import Decimal

from iac_code.pipeline.engine.cost_consistency import (
    BUDGET_ABOVE,
    BUDGET_BELOW,
    BUDGET_UNKNOWN,
    BUDGET_WITHIN,
    evaluate_budget_deviation,
    evaluate_discount_disclosure,
    parse_monthly_amounts,
    reconcile_instance_spec,
    validate_cost_consistency,
)


def _codes(issues):
    return [issue.code for issue in issues]


class TestParseMonthlyAmounts:
    def test_parses_list_and_discounted_prices(self):
        assert parse_monthly_amounts("¥96.80/月（列表价，合同优惠后约¥13.76/月）") == (
            Decimal("96.80"),
            Decimal("13.76"),
        )

    def test_single_amount_has_no_discounted_price(self):
        assert parse_monthly_amounts("¥281.77/月（列表价）") == (Decimal("281.77"), None)

    def test_thousands_separator_is_normalized(self):
        assert parse_monthly_amounts("¥1,234.50/月（列表价）") == (Decimal("1234.50"), None)

    def test_pricing_failure_yields_no_amount(self):
        assert parse_monthly_amounts("询价失败") == (None, None)
        assert parse_monthly_amounts(None) == (None, None)


class TestEvaluateBudgetDeviation:
    def test_within_planned_range(self):
        status, actual = evaluate_budget_deviation(
            {"monthly_min": 50, "monthly_max": 100, "currency": "CNY"},
            "¥96.80/月（列表价）",
        )
        assert status == BUDGET_WITHIN
        assert actual == Decimal("96.80")

    def test_session_4a2b12c_exceeds_planned_range(self):
        status, actual = evaluate_budget_deviation(
            {"monthly_min": 50, "monthly_max": 100, "currency": "CNY"},
            "¥281.77/月（列表价）",
        )
        assert status == BUDGET_ABOVE
        assert actual == Decimal("281.77")

    def test_session_64ceeb9b_exceeds_planned_range(self):
        status, actual = evaluate_budget_deviation(
            {"monthly_min": 200, "monthly_max": 300, "currency": "CNY"},
            "¥443.29/月（列表价）",
        )
        assert status == BUDGET_ABOVE
        assert actual == Decimal("443.29")

    def test_below_planned_range(self):
        status, _actual = evaluate_budget_deviation({"monthly_min": 200, "monthly_max": 300}, "¥12/月（列表价）")
        assert status == BUDGET_BELOW

    def test_missing_baseline_is_unknown(self):
        assert evaluate_budget_deviation(None, "¥100/月")[0] == BUDGET_UNKNOWN
        assert evaluate_budget_deviation({"monthly_min": 200}, "¥100/月")[0] == BUDGET_UNKNOWN

    def test_unparsable_estimate_is_unknown(self):
        assert evaluate_budget_deviation({"monthly_min": 1, "monthly_max": 2}, "询价失败") == (BUDGET_UNKNOWN, None)


class TestReconcileInstanceSpec:
    def test_matching_spec_has_no_issue(self):
        assert (
            reconcile_instance_spec(
                {"instance_type": "ecs.t6-c1m1.large"},
                {"InstanceType": "ecs.t6-c1m1.large"},
                {"InstanceType": "ecs.t6-c1m1.large"},
            )
            == []
        )

    def test_session_43672b3_spec_drift_is_reported(self):
        issues = reconcile_instance_spec(
            {"instance_type": "ecs.t6-c1m1.large"},
            {"InstanceType": "ecs.c5.large"},
            {"InstanceType": "ecs.g5.large"},
        )
        assert _codes(issues) == ["spec_deviates_from_plan", "spec_preview_mismatch"]

    def test_missing_pricing_parameter_is_reported(self):
        issues = reconcile_instance_spec({"instance_type": "ecs.g7.large"}, {}, {})
        assert _codes(issues) == ["spec_missing_in_deployment_parameters"]

    def test_no_planned_compute_skips_check(self):
        assert reconcile_instance_spec(None, {"InstanceType": "ecs.g7.large"}, {}) == []
        assert reconcile_instance_spec({}, {"InstanceType": "ecs.g7.large"}, {}) == []

    def test_case_and_whitespace_are_normalized(self):
        assert reconcile_instance_spec({"instance_type": " ECS.G7.Large "}, {"InstanceType": "ecs.g7.large"}, {}) == []

    def test_image_id_is_reconciled(self):
        issues = reconcile_instance_spec(
            {"image_id": "centos_stream_9_x64_20G_alibase_20260414.vhd"},
            {"ImageId": "aliyun_3_x64_20G_alibase_20260101.vhd"},
            {},
        )
        assert _codes(issues) == ["spec_deviates_from_plan"]


class TestEvaluateDiscountDisclosure:
    def test_real_reduction_is_accepted(self):
        assert evaluate_discount_disclosure("¥96.80/月（列表价，合同优惠后约¥13.76/月）") == []

    def test_session_64ceeb9b_identical_prices_are_rejected(self):
        issues = evaluate_discount_disclosure("¥443.29/月（列表价，合同优惠后约¥443.29/月）")
        assert _codes(issues) == ["discount_without_reduction"]

    def test_list_price_only_is_accepted(self):
        assert evaluate_discount_disclosure("¥443.29/月（列表价）") == []

    def test_pricing_failure_is_accepted(self):
        assert evaluate_discount_disclosure("询价失败") == []


class TestValidateCostConsistency:
    def _conclusion(self, **overrides):
        conclusion = {
            "monthly_estimate": "¥96.80/月（列表价，合同优惠后约¥13.76/月）",
            "deployment_parameters": {"InstanceType": "ecs.t6-c1m1.large"},
            "preview_validation": {"succeeded": True, "parameters": {"InstanceType": "ecs.t6-c1m1.large"}},
            "spec_reconciliation": {"instance_type": "ecs.t6-c1m1.large", "matches_plan": True},
            "budget_deviation": {"status": "within", "actual_monthly": 96.80},
        }
        conclusion.update(overrides)
        return conclusion

    def test_consistent_conclusion_passes(self):
        assert (
            validate_cost_consistency(
                {"instance_type": "ecs.t6-c1m1.large"},
                {"monthly_min": 50, "monthly_max": 100, "currency": "CNY"},
                self._conclusion(),
            )
            == []
        )

    def test_no_baseline_skips_all_checks(self):
        assert validate_cost_consistency(None, None, {"monthly_estimate": "¥281.77/月（列表价）"}) == []

    def test_missing_spec_reconciliation_is_reported(self):
        conclusion = self._conclusion()
        del conclusion["spec_reconciliation"]
        issues = validate_cost_consistency({"instance_type": "ecs.t6-c1m1.large"}, None, conclusion)
        assert _codes(issues) == ["missing_spec_reconciliation"]

    def test_spec_drift_claimed_as_matching_is_reported(self):
        conclusion = self._conclusion(
            deployment_parameters={"InstanceType": "ecs.c5.large"},
            preview_validation={"succeeded": True, "parameters": {"InstanceType": "ecs.c5.large"}},
        )
        issues = validate_cost_consistency({"instance_type": "ecs.t6-c1m1.large"}, None, conclusion)
        assert _codes(issues) == [
            "spec_deviates_from_plan",
            "spec_reconciliation_mismatch",
            "missing_spec_deviation_note",
        ]

    def test_declared_spec_deviation_with_note_is_accepted(self):
        conclusion = self._conclusion(
            deployment_parameters={"InstanceType": "ecs.c6.xlarge"},
            preview_validation={"succeeded": True, "parameters": {"InstanceType": "ecs.c6.xlarge"}},
            spec_reconciliation={
                "instance_type": "ecs.c6.xlarge",
                "matches_plan": False,
                "deviation_note": "规划规格不满足用户 4 vCPU 硬约束",
            },
        )
        issues = validate_cost_consistency({"instance_type": "ecs.t6-c1m1.large"}, None, conclusion)
        assert _codes(issues) == ["spec_deviates_from_plan"]

    def test_missing_budget_deviation_is_reported(self):
        conclusion = self._conclusion()
        del conclusion["budget_deviation"]
        issues = validate_cost_consistency(None, {"monthly_min": 50, "monthly_max": 100}, conclusion)
        assert _codes(issues) == ["missing_budget_deviation"]

    def test_unreported_budget_overrun_is_reported(self):
        conclusion = self._conclusion(monthly_estimate="¥443.29/月（列表价）")
        issues = validate_cost_consistency(None, {"monthly_min": 200, "monthly_max": 300}, conclusion)
        assert _codes(issues) == [
            "budget_deviation_status_mismatch",
            "budget_deviation_amount_mismatch",
            "missing_budget_deviation_note",
        ]

    def test_declared_budget_overrun_with_note_is_accepted(self):
        conclusion = self._conclusion(
            monthly_estimate="¥443.29/月（列表价）",
            budget_deviation={
                "status": "above",
                "planned_range": "¥200-300/月",
                "actual_monthly": 443.29,
                "note": "通用方案实际费用超出规划区间约 48%",
            },
        )
        assert validate_cost_consistency(None, {"monthly_min": 200, "monthly_max": 300}, conclusion) == []

    def test_ineffective_discount_is_reported(self):
        conclusion = self._conclusion(monthly_estimate="¥443.29/月（列表价，合同优惠后约¥443.29/月）")
        issues = validate_cost_consistency(None, None, conclusion)
        assert _codes(issues) == ["discount_without_reduction"]
