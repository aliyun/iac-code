from pathlib import Path

import pytest

from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.types import StepConfig
from iac_code.tools.base import ToolContext


@pytest.mark.asyncio
async def test_confirm_completion_rejects_unknown_candidate_before_advancing_to_deployment():
    root = Path(__file__).parents[3] / 'src/iac_code/pipeline/selling'
    pipeline = load_pipeline_dir(root)
    step = next(s for s in pipeline.steps if s.step_id == 'confirm_and_select')
    config = StepConfig(step_id=step.step_id, conclusion_field=step.conclusion_field, forward=step.forward,
                        conclusion_schema=step.conclusion_schema, completion_enricher=step.completion_enricher)
    tool = CompleteStepTool(config, completion_guard_state={'context_snapshot': {
        'evaluated_candidates': [{'candidate': {'name': 'Actual', 'output_path': 'fake.yml'}, 'failed': False}]}})
    payload = {'conclusion': {'user_prompt': 'Choose',
               'options': [{'name': 'Actual', 'summary': 'Summary', 'candidate_index': 0}],
               'user_input': 'Choose any available plan', 'selected_candidate_name': 'Invented'}}
    result = await tool.execute(tool_input=payload, context=ToolContext())
    assert result.is_error, 'invalid selection should remain in confirm step for correction'
    assert 'not found' in result.content
    payload['conclusion']['selected_candidate_name'] = 'Actual'
    corrected = await tool.execute(tool_input=payload, context=ToolContext())
    assert not corrected.is_error


@pytest.mark.parametrize('name', [None, ''])
def test_initial_candidate_presentation_is_not_a_user_choice(name):
    from iac_code.pipeline.selling.hooks.confirm_and_select import enrich_completion_input
    payload = {'conclusion': {'user_prompt': 'Choose', 'options': [], 'selected_candidate_name': name}}
    assert enrich_completion_input(tool_input=payload, context_snapshot={}) == payload


@pytest.mark.parametrize('selected', [
    {'selected_candidate_index': 9},
    {'selected_candidate_name': 'Same'},
    {'selected_candidate_index': 0, 'selected_candidate_name': 'Wrong'},
    {'selected_evaluated_candidate_index': 2},
])
def test_confirm_guard_does_not_replace_invalid_ambiguous_or_failed_selection(selected):
    from iac_code.pipeline.engine.complete_step_tool import CompletionEnrichmentError
    from iac_code.pipeline.selling.hooks.confirm_and_select import enrich_completion_input
    candidates = [
        {'candidate': {'name': 'Same'}, 'failed': False},
        {'candidate': {'name': 'Same'}, 'failed': False},
        {'candidate': {'name': 'Failed'}, 'failed': True},
    ]
    payload = {'conclusion': selected.copy()}
    with pytest.raises(CompletionEnrichmentError):
        enrich_completion_input(tool_input=payload, context_snapshot={'evaluated_candidates': candidates})
    assert payload == {'conclusion': selected}


@pytest.mark.asyncio
async def test_resumed_selection_cannot_submit_only_initial_presentation_and_advance():
    pipeline = load_pipeline_dir(Path(__file__).parents[3] / 'src/iac_code/pipeline/selling')
    step = next(s for s in pipeline.steps if s.step_id == 'confirm_and_select')
    config = StepConfig(step_id=step.step_id, conclusion_field=step.conclusion_field, forward=step.forward,
                        conclusion_schema=step.conclusion_schema, completion_enricher=step.completion_enricher)
    payload = {'conclusion': {'user_prompt': 'Choose',
               'options': [{'name': 'Actual', 'summary': 'Summary', 'candidate_index': 0}]}}
    snapshot = {'evaluated_candidates': [{'candidate': {'name': 'Actual'}, 'failed': False}]}
    tool = CompleteStepTool(config, completion_guard_state={
        'context_snapshot': snapshot, 'resuming_candidate_selection': True})
    result = await tool.execute(tool_input=payload, context=ToolContext())
    assert result.is_error, 'resumed choice without a selection must not advance to deployment'
    payload['conclusion']['selected_candidate_index'] = 0
    corrected = await tool.execute(tool_input=payload, context=ToolContext())
    assert not corrected.is_error
    initial = CompleteStepTool(config, completion_guard_state={'context_snapshot': snapshot})
    payload['conclusion'].pop('selected_candidate_index')
    assert not (await initial.execute(tool_input=payload, context=ToolContext())).is_error
