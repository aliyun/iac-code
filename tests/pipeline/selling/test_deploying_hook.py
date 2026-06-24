from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.ui_contract import encode_selected_candidate, parse_selected_candidate
from iac_code.pipeline.selling.hooks.deploying import normalize_selected_plan, on_enter, resolve_selected_candidate


def _evaluated_candidates():
    return [
        {"candidate": {"name": "Same", "output_path": "templates/a.yml"}, "failed": False},
        {"candidate": {"name": "Same", "output_path": "templates/b.yml"}, "failed": False},
        {"candidate": {"name": "Broken", "output_path": "templates/c.yml"}, "failed": True},
    ]


def test_resolve_selected_candidate_prefers_index_for_duplicate_names():
    selected = parse_selected_candidate('{"selected_candidate_name": "Same", "selected_candidate_index": 1}')
    assert selected is not None
    resolved = resolve_selected_candidate(selected, _evaluated_candidates())
    assert resolved.error is None
    assert resolved.candidate["output_path"] == "templates/b.yml"


def test_resolve_selected_candidate_rejects_failed_candidate():
    selected = parse_selected_candidate('{"selected_candidate_name": "Broken", "selected_candidate_index": 2}')
    assert selected is not None
    resolved = resolve_selected_candidate(selected, _evaluated_candidates())
    assert resolved.candidate is None
    assert "failed" in resolved.error


def test_normalize_selected_plan_adds_resolution_metadata():
    selected_plan = {"user_input": encode_selected_candidate("Same", 0), "options": []}
    normalized = normalize_selected_plan(selected_plan, _evaluated_candidates())
    assert normalized["selection_valid"] is True
    assert normalized["selected_candidate"]["output_path"] == "templates/a.yml"
    assert normalized["selection"]["selected_candidate_index"] == 0


def test_normalize_selected_plan_preserves_cost_deployment_parameters():
    evaluated_candidates = [
        {
            "candidate": {"name": "WithParams", "output_path": "templates/a.yml"},
            "failed": False,
            "cost": {"deployment_parameters": {"ZoneId": "cn-hangzhou-k", "InstanceType": "ecs.g7.large"}},
        }
    ]
    selected_plan = {"user_input": encode_selected_candidate("WithParams", 0), "options": []}

    normalized = normalize_selected_plan(selected_plan, evaluated_candidates)

    assert normalized["selection_valid"] is True
    assert normalized["selected_candidate_result"]["cost"]["deployment_parameters"] == {
        "ZoneId": "cn-hangzhou-k",
        "InstanceType": "ecs.g7.large",
    }


def test_normalize_selected_plan_applies_user_parameter_overrides():
    evaluated_candidates = [
        {
            "candidate": {"name": "WithParams", "output_path": "templates/a.yml"},
            "failed": False,
            "cost": {
                "deployment_parameters": {
                    "ZoneId": "cn-hangzhou-k",
                    "InstanceType": "ecs.g7.large",
                    "SystemDiskCategory": "cloud_essd",
                }
            },
        }
    ]
    selected_plan = {
        "user_input": encode_selected_candidate(
            "WithParams",
            0,
            {"InstanceType": "ecs.c7.large", "ImageId": "centos_stream_9_x64_20G_alibase_20260414.vhd"},
        ),
        "options": [],
    }

    normalized = normalize_selected_plan(selected_plan, evaluated_candidates)

    assert normalized["selection_valid"] is True
    assert normalized["parameter_overrides"] == {
        "InstanceType": "ecs.c7.large",
        "ImageId": "centos_stream_9_x64_20G_alibase_20260414.vhd",
    }
    assert normalized["effective_deployment_parameters"] == {
        "ZoneId": "cn-hangzhou-k",
        "InstanceType": "ecs.c7.large",
        "SystemDiskCategory": "cloud_essd",
        "ImageId": "centos_stream_9_x64_20G_alibase_20260414.vhd",
    }
    assert normalized["cost_estimate_parameter_overridden"] is True


def test_normalize_selected_plan_resolves_natural_language_zero_based_choice():
    selected_plan = {"user_input": "我选择方案0", "options": []}
    normalized = normalize_selected_plan(selected_plan, _evaluated_candidates())
    assert normalized["selection_valid"] is True
    assert normalized["selected_candidate"]["output_path"] == "templates/a.yml"
    assert normalized["selection"] == {"selected_candidate_name": "", "selected_candidate_index": 0}


def test_normalize_selected_plan_prefers_structured_selection_fields_from_confirm_step():
    selected_plan = {
        "selected_candidate_name": "Same",
        "selected_candidate_index": 1,
        "options": [],
    }
    normalized = normalize_selected_plan(selected_plan, _evaluated_candidates())
    assert normalized["selection_valid"] is True
    assert normalized["selected_candidate"]["output_path"] == "templates/b.yml"
    assert normalized["selection"] == {"selected_candidate_name": "Same", "selected_candidate_index": 1}


def test_normalize_selected_plan_marks_invalid_selection():
    selected_plan = {"user_input": encode_selected_candidate("Missing", 9), "options": []}
    normalized = normalize_selected_plan(selected_plan, _evaluated_candidates())
    assert normalized["selection_valid"] is False
    assert "not found" in normalized["selection_error"]


def test_on_enter_normalizes_selected_plan_in_context():
    context = PipelineContext({"selected_plan": [], "evaluated_candidates": []})
    context.set_conclusion("selected_plan", {"user_input": encode_selected_candidate("Same", 0), "options": []})
    context.set_conclusion("evaluated_candidates", _evaluated_candidates())

    on_enter(context)

    selected_plan = context.get_conclusion("selected_plan")
    assert selected_plan["selection_valid"] is True
    assert selected_plan["selected_candidate"]["output_path"] == "templates/a.yml"
