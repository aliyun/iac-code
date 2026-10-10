import copy
from pathlib import Path

import pytest

from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.types import StepConfig
from iac_code.tools.base import ToolContext


def _tool(requested):
    pipeline = load_pipeline_dir(Path(__file__).parents[3] / 'src/iac_code/pipeline/selling')
    step = next(s for s in pipeline.steps if s.step_id == 'architecture_planning')
    config = StepConfig(step_id=step.step_id, conclusion_field=step.conclusion_field, forward=step.forward,
                        conclusion_schema=step.conclusion_schema, completion_enricher=step.completion_enricher,
                        rollback_targets=['intent_parsing'])
    return CompleteStepTool(config, completion_guard_state={'context_snapshot': {'intent': requested}})


def _payload(count):
    return {'conclusion': {'candidates': [
        {'name': f'Plan {i}', 'output_path': f'templates/{i}.yml', 'products': ['VPC'],
         'hard_constraints': [], 'topology': 'Single VPC', 'monthly_estimate': '0', 'pros': [], 'cons': []}
        for i in range(count)
    ]}}


@pytest.mark.asyncio
@pytest.mark.parametrize('expected,actual', [(2, 4), (3, 1), (1, 2), (4, 3)])
async def test_architecture_rejects_wrong_requested_count_and_allows_model_correction(expected, actual):
    tool = _tool({'requested_candidate_count': expected})
    payload = _payload(actual)
    original = copy.deepcopy(payload)
    result = await tool.execute(tool_input=payload, context=ToolContext())
    assert result.is_error, 'explicit user candidate count must be checked before evaluation starts'
    assert str(expected) in result.content and str(actual) in result.content
    assert payload == original, 'validation must not truncate, pad or replace model candidates'
    corrected = await tool.execute(tool_input=_payload(expected), context=ToolContext())
    assert not corrected.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', [{}, {'requested_candidate_count': None}])
async def test_architecture_keeps_adaptive_count_without_explicit_request(intent):
    assert not (await _tool(intent).execute(tool_input=_payload(3), context=ToolContext())).is_error


@pytest.mark.asyncio
async def test_architecture_can_request_clarification_instead_of_inventing_candidates():
    payload = {**_payload(1), 'rollback_request': {
        'target_step': 'intent_parsing', 'reason': 'Need clarification to provide distinct alternatives',
    }}
    assert not (await _tool({'requested_candidate_count': 3}).execute(
        tool_input=payload, context=ToolContext(),
    )).is_error


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [0, -1, True, '2', 2.5])
async def test_architecture_rejects_invalid_retained_requested_count(invalid):
    assert (await _tool({'requested_candidate_count': invalid}).execute(
        tool_input=_payload(2), context=ToolContext(),
    )).is_error


@pytest.mark.asyncio
async def test_architecture_count_accepts_json_schema_integer_number_without_rounding():
    tool = _tool({'requested_candidate_count': 2.0})
    assert (await tool.execute(tool_input=_payload(3), context=ToolContext())).is_error
    assert not (await tool.execute(tool_input=_payload(2), context=ToolContext())).is_error


def test_intent_count_schema_accepts_explicit_positive_counts_and_legacy_absence():
    import jsonschema

    pipeline = load_pipeline_dir(Path(__file__).parents[3] / 'src/iac_code/pipeline/selling')
    schema = next(s for s in pipeline.steps if s.step_id == 'intent_parsing').conclusion_schema
    intent = {'is_infra_intent': True, 'confidence': 'high', 'hard_constraints': []}
    for fields in ({}, {'requested_candidate_count': None}, {'requested_candidate_count': 3}):
        jsonschema.validate({**intent, **fields}, schema)
    for invalid in (0, -1, True, '2', 1.5):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**intent, 'requested_candidate_count': invalid}, schema)
