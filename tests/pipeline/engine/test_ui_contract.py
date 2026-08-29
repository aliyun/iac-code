import json

from iac_code.pipeline.engine.ui_contract import (
    PipelineStepType,
    PipelineUiMode,
    encode_deployment_confirmation,
    encode_selected_candidate,
    parse_deployment_confirmation,
    parse_selected_candidate,
)


def test_pipeline_step_type_values_match_yaml_strings():
    assert PipelineStepType.NORMAL.value == "normal"
    assert PipelineStepType.PARALLEL_SUB_PIPELINE.value == "parallel_sub_pipeline"


def test_pipeline_ui_mode_values_match_yaml_strings():
    assert PipelineUiMode.CANDIDATE_SELECTION.value == "candidate_selection"
    assert PipelineUiMode.DEPLOYMENT_CONFIRMATION.value == "deployment_confirmation"


def test_deployment_confirmation_round_trip_preserves_parameter_overrides():
    encoded = encode_deployment_confirmation("adjust", {"InstanceType": "ecs.g7.large"})

    assert json.loads(encoded) == {
        "action": "adjust",
        "parameter_overrides": {"InstanceType": "ecs.g7.large"},
    }
    parsed = parse_deployment_confirmation(encoded)
    assert parsed is not None
    assert parsed.action == "adjust"
    assert parsed.parameter_overrides == {"InstanceType": "ecs.g7.large"}
    assert parsed.parameter_overrides_provided is True


def test_deployment_confirmation_accepts_legacy_parameter_aliases():
    parsed = parse_deployment_confirmation('{"action":"confirm","parameters":{"ZoneId":"cn-hangzhou-k"}}')

    assert parsed is not None
    assert parsed.action == "confirm"
    assert parsed.parameter_overrides == {"ZoneId": "cn-hangzhou-k"}


def test_deployment_confirmation_leaves_natural_language_for_the_llm():
    assert parse_deployment_confirmation("按现在这个方案部署") is None


def test_deployment_confirmation_treats_empty_overrides_as_no_new_override():
    omitted = parse_deployment_confirmation('{"action":"confirm"}')
    explicit = parse_deployment_confirmation('{"action":"confirm","parameter_overrides":{}}')
    encoded_explicit = parse_deployment_confirmation(encode_deployment_confirmation("confirm", {}))

    assert omitted is not None and omitted.parameter_overrides_provided is False
    assert explicit is not None and explicit.parameter_overrides_provided is False
    assert encoded_explicit is not None and encoded_explicit.parameter_overrides_provided is False


def test_deployment_confirmation_rejects_unknown_actions_and_non_object_parameters():
    assert parse_deployment_confirmation('{"action":"deploy"}') is None
    assert parse_deployment_confirmation('{"action":"confirm","parameter_overrides":"bad"}') is None


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


def test_encode_selected_candidate_can_include_evaluated_candidate_index():
    payload = json.loads(encode_selected_candidate("Same", 0, evaluated_candidate_index=2))
    assert payload == {
        "selected_candidate_name": "Same",
        "selected_candidate_index": 0,
        "selected_evaluated_candidate_index": 2,
    }


def test_parse_selected_candidate_accepts_structured_json_string():
    parsed = parse_selected_candidate('{"selected_candidate_name": "Same", "selected_candidate_index": 1}')
    assert parsed is not None
    assert parsed.selected_candidate_name == "Same"
    assert parsed.selected_candidate_index == 1
    assert parsed.parameter_overrides == {}


def test_parse_selected_candidate_prefers_explicit_evaluated_candidate_coordinate():
    parsed = parse_selected_candidate(
        '{"selected_candidate_name": "Same", "selected_candidate_index": 0, "selected_evaluated_candidate_index": 2}'
    )
    assert parsed is not None
    assert parsed.selected_candidate_index == 0
    assert parsed.selected_evaluated_candidate_index == 2


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
