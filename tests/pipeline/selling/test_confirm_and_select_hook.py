from pathlib import Path

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.selling.hooks.confirm_and_select import (
    candidate_preview_rejection,
    on_enter,
    reject_preview_failed_candidates,
)


def _candidate(name, preview_validation=None, missing_deployment_parameters=None, cost=True):
    result = {"candidate": {"name": name, "output_path": f"templates/{name}.yml"}, "failed": False}
    if cost:
        cost_payload = {}
        if preview_validation is not None:
            cost_payload["preview_validation"] = preview_validation
        if missing_deployment_parameters is not None:
            cost_payload["missing_deployment_parameters"] = missing_deployment_parameters
        result["cost"] = cost_payload
    return result


def test_preview_succeeded_candidate_stays_selectable():
    result = _candidate("ok", preview_validation={"succeeded": True, "template_url": "https://x", "parameters": {}})
    assert candidate_preview_rejection(result) is None
    assert reject_preview_failed_candidates([result]) == []
    assert result["failed"] is False


def test_preview_failed_without_parameter_gap_is_rejected():
    result = _candidate(
        "broken",
        preview_validation={"succeeded": False, "error": "StackValidationFailed: RdsInstance property invalid"},
    )
    reason = candidate_preview_rejection(result)
    assert reason is not None
    assert "StackValidationFailed" in reason

    rejected = reject_preview_failed_candidates([result])
    assert len(rejected) == 1
    assert result["failed"] is True
    assert result["preview_rejected"] is True
    assert "StackValidationFailed" in result["error"]


def test_preview_failed_with_parameter_gap_stays_selectable():
    result = _candidate(
        "needs-params",
        preview_validation={"succeeded": False, "error": "missing deployment parameters"},
        missing_deployment_parameters=["DBPassword"],
    )
    assert candidate_preview_rejection(result) is None
    assert reject_preview_failed_candidates([result]) == []
    assert result["failed"] is False


def test_preview_failed_without_error_message_uses_default_reason():
    result = _candidate("silent", preview_validation={"succeeded": False})
    assert candidate_preview_rejection(result) == "preview validation failed: preview validation did not succeed"


def test_missing_cost_or_preview_validation_is_rejected():
    without_cost = _candidate("no-cost", cost=False)
    without_preview = _candidate("no-preview")
    non_dict_preview = _candidate("bad-preview", preview_validation="nope")

    for result in (without_cost, without_preview, non_dict_preview):
        assert candidate_preview_rejection(result) == "cost estimation did not report preview validation"

    assert len(reject_preview_failed_candidates([without_cost, without_preview, non_dict_preview])) == 3


def test_already_failed_candidates_are_left_untouched():
    result = _candidate("dead", preview_validation={"succeeded": False, "error": "boom"})
    result["failed"] = True
    result["error"] = "sub-pipeline crashed"

    assert reject_preview_failed_candidates([result]) == []
    assert result["error"] == "sub-pipeline crashed"
    assert "preview_rejected" not in result


def test_reject_is_idempotent_across_reentry():
    result = _candidate("broken", preview_validation={"succeeded": False, "error": "StackValidationFailed"})

    assert len(reject_preview_failed_candidates([result])) == 1
    assert reject_preview_failed_candidates([result]) == []
    assert result["failed"] is True
    assert result["preview_rejected"] is True


def test_on_enter_rejects_preview_failed_candidates_without_staling_selected_plan():
    evaluated = [
        _candidate("ok", preview_validation={"succeeded": True, "template_url": "https://x", "parameters": {}}),
        _candidate("broken", preview_validation={"succeeded": False, "error": "StackValidationFailed"}),
    ]
    ctx = PipelineContext({"selected_plan": ["evaluated_candidates"], "evaluated_candidates": []})
    ctx.set_conclusion("evaluated_candidates", evaluated)
    ctx.set_conclusion("selected_plan", {"selected_candidate_index": 0})
    selected_field_before = ctx.get_field("selected_plan")
    version_before = ctx.get_field("evaluated_candidates").version

    on_enter(ctx)

    assert evaluated[0]["failed"] is False
    assert evaluated[1]["failed"] is True
    assert ctx.get_field("evaluated_candidates").version == version_before
    assert ctx.get_field("selected_plan").stale == selected_field_before.stale


def test_on_enter_tolerates_missing_evaluated_candidates():
    ctx = PipelineContext({"selected_plan": ["evaluated_candidates"], "evaluated_candidates": []})
    on_enter(ctx)
    assert ctx.get_conclusion("evaluated_candidates") is None


def test_selling_confirm_and_select_step_binds_on_enter_hook():
    selling_dir = Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"
    loaded = load_pipeline_dir(selling_dir)
    step = next(step for step in loaded.steps if step.step_id == "confirm_and_select")

    assert step.on_enter is not None
    assert step.on_enter.__name__ == "on_enter"

    evaluated = [_candidate("broken", preview_validation={"succeeded": False, "error": "StackValidationFailed"})]
    ctx = PipelineContext({"selected_plan": ["evaluated_candidates"], "evaluated_candidates": []})
    ctx.set_conclusion("evaluated_candidates", evaluated)
    step.on_enter(ctx)

    assert evaluated[0]["failed"] is True
