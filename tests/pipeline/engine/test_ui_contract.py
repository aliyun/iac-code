import json

from iac_code.pipeline.engine.ui_contract import (
    PipelineStepType,
    PipelineUiMode,
    encode_selected_candidate,
    parse_selected_candidate,
)


def test_pipeline_step_type_values_match_yaml_strings():
    assert PipelineStepType.NORMAL.value == "normal"
    assert PipelineStepType.PARALLEL_SUB_PIPELINE.value == "parallel_sub_pipeline"


def test_pipeline_ui_mode_values_match_yaml_strings():
    assert PipelineUiMode.CANDIDATE_SELECTION.value == "candidate_selection"


def test_encode_selected_candidate_returns_json_string():
    payload = json.loads(encode_selected_candidate("Same", 1))
    assert payload == {"selected_candidate_name": "Same", "selected_candidate_index": 1}


def test_encode_selected_candidate_can_include_parameter_overrides():
    payload = json.loads(encode_selected_candidate("Same", 1, {"InstanceType": "ecs.g7.large"}))
    assert payload == {
        "selected_candidate_name": "Same",
        "selected_candidate_index": 1,
        "parameter_overrides": {"InstanceType": "ecs.g7.large"},
    }


def test_parse_selected_candidate_accepts_structured_json_string():
    parsed = parse_selected_candidate('{"selected_candidate_name": "Same", "selected_candidate_index": 1}')
    assert parsed is not None
    assert parsed.selected_candidate_name == "Same"
    assert parsed.selected_candidate_index == 1
    assert parsed.parameter_overrides == {}


def test_parse_selected_candidate_accepts_parameter_overrides():
    parsed = parse_selected_candidate(
        '{"selected_candidate_name": "Same", "selected_candidate_index": 1, '
        '"parameter_overrides": {"InstanceType": "ecs.g7.large", "Optional": null}}'
    )
    assert parsed is not None
    assert parsed.selected_candidate_name == "Same"
    assert parsed.selected_candidate_index == 1
    assert parsed.parameter_overrides == {"InstanceType": "ecs.g7.large"}


def test_parse_selected_candidate_accepts_parameters_alias_for_a2a_payloads():
    parsed = parse_selected_candidate('{"selected_candidate_index": 1, "parameters": {"ZoneId": "cn-hangzhou-k"}}')
    assert parsed is not None
    assert parsed.selected_candidate_index == 1
    assert parsed.parameter_overrides == {"ZoneId": "cn-hangzhou-k"}


def test_parse_selected_candidate_rejects_invalid_parameter_overrides():
    parsed = parse_selected_candidate(
        '{"selected_candidate_name": "Same", "selected_candidate_index": 1, "parameter_overrides": "bad"}'
    )
    assert parsed is None


def test_parse_selected_candidate_accepts_legacy_plain_name():
    parsed = parse_selected_candidate("Same")
    assert parsed is not None
    assert parsed.selected_candidate_name == "Same"
    assert parsed.selected_candidate_index is None
    assert parsed.parameter_overrides == {}


def test_parse_selected_candidate_extracts_zero_based_index_from_natural_language_choice():
    parsed = parse_selected_candidate("我选择方案0")
    assert parsed is not None
    assert parsed.selected_candidate_name == ""
    assert parsed.selected_candidate_index == 0
