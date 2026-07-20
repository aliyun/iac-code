from __future__ import annotations

import copy
from typing import Any

from iac_code.a2a.pipeline_delta_coalescing import (
    coalesce_pipeline_delta_envelopes,
    coalesce_pipeline_delta_envelopes_by_source,
)


def envelope(
    sequence: int,
    text: str,
    *,
    event_type: str = "text_delta",
    candidate_run_id: str = "candidate-eval-0-1",
    candidate_step_run_id: str = "candidate-eval-0-1-generate-1",
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": f"evt-{sequence}",
        "sequence": sequence,
        "createdAt": f"2026-07-17T00:00:{sequence:02d}Z",
        "eventType": event_type,
        "scope": "candidate_step",
        "pipelineRunId": "run-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "step": {"id": "evaluate_candidates", "runId": "step-evaluate-1", "attempt": 1},
        "candidate": {"id": "eval", "index": 0, "runId": candidate_run_id, "attempt": 1},
        "candidateStep": {"id": "generate", "runId": candidate_step_run_id, "attempt": 1},
        "data": {"text": text, **({"type": "raw_thinking"} if event_type == "thinking_delta" else {})},
    }


def test_coalesces_text_into_first_envelope_with_last_sequence() -> None:
    source = [envelope(1, "hello "), envelope(2, "world")]
    before = copy.deepcopy(source)

    result = coalesce_pipeline_delta_envelopes(source)

    assert len(result) == 1
    assert result[0]["data"]["text"] == "hello world"
    assert result[0]["eventId"] == "evt-1"
    assert result[0]["createdAt"] == "2026-07-17T00:00:01Z"
    assert result[0]["sequence"] == 2
    assert result[0]["candidate"] == source[0]["candidate"]
    assert result[0]["candidateStep"] == source[0]["candidateStep"]
    assert source == before
    assert result[0] is not source[0]
    assert result[0]["data"] is not source[0]["data"]


def test_preserves_a1_b1_a2_as_three_groups() -> None:
    source = [
        envelope(1, "A1", candidate_run_id="candidate-a", candidate_step_run_id="candidate-a-step"),
        envelope(2, "B1", candidate_run_id="candidate-b", candidate_step_run_id="candidate-b-step"),
        envelope(3, "A2", candidate_run_id="candidate-a", candidate_step_run_id="candidate-a-step"),
    ]

    result = coalesce_pipeline_delta_envelopes(source)

    assert [item["data"]["text"] for item in result] == ["A1", "B1", "A2"]
    assert [item["sequence"] for item in result] == [1, 2, 3]


def test_coalesces_contiguous_per_source_deltas_across_candidate_interleaving() -> None:
    source = [
        envelope(1, "A1", candidate_run_id="candidate-a", candidate_step_run_id="candidate-a-step"),
        envelope(2, "B1", candidate_run_id="candidate-b", candidate_step_run_id="candidate-b-step"),
        envelope(3, "A2", candidate_run_id="candidate-a", candidate_step_run_id="candidate-a-step"),
    ]

    result = coalesce_pipeline_delta_envelopes_by_source(source)

    assert [item["data"]["text"] for item in result] == ["B1", "A1A2"]
    assert [item["sequence"] for item in result] == [2, 3]


def test_attempt_type_metadata_and_barrier_changes_close_groups() -> None:
    first = envelope(1, "a")
    new_attempt = envelope(2, "b", candidate_step_run_id="candidate-eval-0-1-generate-2")
    thinking = envelope(3, "c", event_type="thinking_delta")
    thinking_type_change = envelope(4, "d", event_type="thinking_delta")
    thinking_type_change["data"]["type"] = "summary_thinking"
    barrier = envelope(5, "", event_type="tool_result")
    barrier["data"] = {"toolName": "write_file"}

    result = coalesce_pipeline_delta_envelopes([first, new_attempt, thinking, thinking_type_change, barrier])

    assert [item["eventType"] for item in result] == [
        "text_delta",
        "text_delta",
        "thinking_delta",
        "thinking_delta",
        "tool_result",
    ]
