from __future__ import annotations

import time

import pytest

from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator, _usage_data
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import ContextUsageEvent

_SNAKE = {
    "system_prompt_tokens": 100,
    "tool_definition_tokens": 200,
    "user_message_tokens": 300,
    "assistant_message_tokens": 400,
    "tool_result_tokens": 500,
    "total_tokens": 1500,
    "context_window": 60000,
    "usage_percent": 2.5,
    "message_count": 7,
}


def _ctx() -> PipelineA2AContext:
    return PipelineA2AContext(
        pipeline_run_id="ctx-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
        parent_step_order=["intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select"],
        candidate_step_order=["template_generating", "cost_estimating", "reviewing"],
    )


@pytest.fixture
def pipeline_translator_with_active_step() -> tuple[PipelineEventTranslator, str]:
    translator = PipelineEventTranslator(_ctx())
    step_id = "architecture_planning"
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id=step_id,
            timestamp=time.time(),
            data={"index": 2, "total": 4},
        )
    )
    return translator, step_id


def test_usage_data_maps_snake_to_camel():
    data = _usage_data(_SNAKE)
    assert data == {
        "totalTokens": 1500,
        "contextWindow": 60000,
        "usagePercent": 2.5,
        "messageCount": 7,
        "systemPromptTokens": 100,
        "toolDefinitionTokens": 200,
        "userMessageTokens": 300,
        "assistantMessageTokens": 400,
        "toolResultTokens": 500,
    }


def test_usage_data_tolerates_missing_keys():
    assert _usage_data({}) == {
        "totalTokens": None,
        "contextWindow": None,
        "usagePercent": None,
        "messageCount": None,
        "systemPromptTokens": None,
        "toolDefinitionTokens": None,
        "userMessageTokens": None,
        "assistantMessageTokens": None,
        "toolResultTokens": None,
    }


def test_parent_scoped_context_usage_envelope(pipeline_translator_with_active_step):
    # Fixture returns a translator whose _current_parent_step_id is set to a known
    # step (built the way the sibling tests set an active parent step: a STEP_STARTED
    # PipelineEvent).
    translator, step_id = pipeline_translator_with_active_step
    envelopes = translator.translate(ContextUsageEvent(usage=_SNAKE))
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["eventType"] == "context_usage"
    assert env["scope"] == "step"
    assert env["status"] == "working"
    assert env["data"]["totalTokens"] == 1500
    assert env["step"]["id"] == step_id
