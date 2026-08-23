from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.selling.hooks.confirm_and_select import (
    annotate_cost_consistency,
    evaluate_cost_consistency,
    on_enter,
    parse_actual_estimate,
    parse_planning_estimate,
)


def test_parse_planning_estimate_uses_upper_bound_of_range():
    assert parse_planning_estimate("¥80-120/月") == 120.0


def test_parse_planning_estimate_single_value():
    assert parse_planning_estimate("约 ¥100/月") == 100.0


def test_parse_planning_estimate_handles_thousands_separator():
    assert parse_planning_estimate("¥1,234-2,000/月") == 2000.0


def test_parse_planning_estimate_returns_none_when_unparseable():
    assert parse_planning_estimate("询价失败") is None
    assert parse_planning_estimate(None) is None


def test_parse_actual_estimate_uses_list_price_first_amount():
    assert parse_actual_estimate("¥289.81/月") == 289.81


def test_parse_actual_estimate_uses_list_price_when_discount_present():
    assert parse_actual_estimate("¥96.80/月（列表价，合同优惠后约¥13.76/月）") == 96.80


def test_parse_actual_estimate_returns_none_on_pricing_failure():
    assert parse_actual_estimate("询价失败") is None


def test_evaluate_cost_consistency_flags_large_deviation():
    result = evaluate_cost_consistency("¥80-120/月", "¥289.81/月")
    assert result is not None
    assert result["deviation_ratio"] == round(289.81 / 120.0, 2)
    assert result["exceeds_threshold"] is True
    assert "偏差" in result["message"]


def test_evaluate_cost_consistency_within_threshold():
    result = evaluate_cost_consistency("¥80-120/月", "¥130/月", threshold=1.5)
    assert result is not None
    assert result["exceeds_threshold"] is False
    assert "message" not in result


def test_evaluate_cost_consistency_flags_reverse_deviation():
    # Actual far below planning also breaks consistency.
    result = evaluate_cost_consistency("¥1000/月", "¥100/月", threshold=1.5)
    assert result is not None
    assert result["exceeds_threshold"] is True


def test_evaluate_cost_consistency_returns_none_when_pricing_failed():
    assert evaluate_cost_consistency("¥80-120/月", "询价失败") is None
    assert evaluate_cost_consistency(None, "¥100/月") is None


def test_evaluate_cost_consistency_respects_custom_threshold():
    result = evaluate_cost_consistency("¥100/月", "¥180/月", threshold=2.0)
    assert result is not None
    assert result["exceeds_threshold"] is False


def test_evaluate_cost_consistency_respects_env_threshold(monkeypatch):
    monkeypatch.setenv("IAC_CODE_SELLING_COST_DEVIATION_THRESHOLD", "3.0")
    result = evaluate_cost_consistency("¥100/月", "¥250/月")
    assert result is not None
    assert result["threshold"] == 3.0
    assert result["exceeds_threshold"] is False


def test_annotate_cost_consistency_marks_over_threshold_candidate():
    evaluated = [
        {
            "candidate": {"name": "Cheap", "monthly_estimate": "¥80-120/月"},
            "cost": {"monthly_estimate": "¥289.81/月"},
            "failed": False,
        }
    ]
    any_exceeds = annotate_cost_consistency(evaluated)
    assert any_exceeds is True
    assert evaluated[0]["cost_consistency"]["exceeds_threshold"] is True


def test_annotate_cost_consistency_skips_failed_candidate():
    evaluated = [
        {
            "candidate": {"name": "Broken", "monthly_estimate": "¥80-120/月"},
            "cost": {"monthly_estimate": "¥289.81/月"},
            "failed": True,
        }
    ]
    any_exceeds = annotate_cost_consistency(evaluated)
    assert any_exceeds is False
    assert "cost_consistency" not in evaluated[0]


def test_annotate_cost_consistency_omits_when_pricing_missing():
    evaluated = [
        {
            "candidate": {"name": "NoPrice", "monthly_estimate": "¥80-120/月"},
            "cost": {"monthly_estimate": "询价失败"},
            "failed": False,
        }
    ]
    any_exceeds = annotate_cost_consistency(evaluated)
    assert any_exceeds is False
    assert "cost_consistency" not in evaluated[0]


def test_annotate_cost_consistency_is_idempotent_and_clears_stale():
    evaluated = [
        {
            "candidate": {"name": "Cheap", "monthly_estimate": "¥80-120/月"},
            "cost": {"monthly_estimate": "¥130/月"},
            "failed": False,
            "cost_consistency": {"stale": True},
        }
    ]
    annotate_cost_consistency(evaluated)
    assert evaluated[0]["cost_consistency"]["exceeds_threshold"] is False
    # Re-running keeps the same non-stale structure.
    annotate_cost_consistency(evaluated)
    assert "stale" not in evaluated[0]["cost_consistency"]


def test_annotate_cost_consistency_handles_non_list():
    assert annotate_cost_consistency(None) is False
    assert annotate_cost_consistency({}) is False


def test_on_enter_annotates_evaluated_candidates_in_context():
    context = PipelineContext({"evaluated_candidates": [], "selected_plan": ["evaluated_candidates"]})
    context.set_conclusion(
        "evaluated_candidates",
        [
            {
                "candidate": {"name": "Cheap", "monthly_estimate": "¥80-120/月"},
                "cost": {"monthly_estimate": "¥289.81/月"},
                "failed": False,
            }
        ],
    )

    on_enter(context)

    evaluated = context.get_conclusion("evaluated_candidates")
    assert evaluated[0]["cost_consistency"]["exceeds_threshold"] is True


def test_on_enter_no_op_when_field_missing():
    context = PipelineContext({"evaluated_candidates": []})
    # Should not raise when the field was never set.
    on_enter(context)
    assert context.get_conclusion("evaluated_candidates") is None
