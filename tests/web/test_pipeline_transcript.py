"""Tests for A2A pipeline envelope -> web transcript translation."""

from __future__ import annotations

from typing import Any

from iac_code.web.pipeline_transcript import (
    PIPELINE_MARKER_EVENT,
    PipelineTranscriptTranslator,
    build_pipeline_transcript_rows,
)


def _envelope(event_type: str, scope: str, sequence: int, **extra: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "eventType": event_type,
        "scope": scope,
        "sequence": sequence,
        "data": extra.pop("data", {}),
    }
    envelope.update(extra)
    return envelope


def _sample_envelopes() -> list[dict[str, Any]]:
    step_normal = {"id": "intent_parsing", "runId": "step-intent_parsing-1", "index": 1, "total": 5}
    step_eval = {"id": "evaluate_candidates", "runId": "step-evaluate_candidates-1", "index": 3, "total": 5}
    candidate = {"id": "evaluate_candidate_x", "runId": "cand-x-0-1", "index": 0, "name": "方案甲"}
    tool_a = {"toolName": "read_memory", "toolUseId": "call_a", "isError": False, "result": "no memory"}
    tool_b = {"toolName": "read_file", "toolUseId": "call_b", "isError": False, "result": "file body"}
    return [
        _envelope("pipeline_started", "pipeline", 1, data={"totalSteps": 5}),
        _envelope("step_started", "step", 2, step=step_normal),
        _envelope("tool_result", "step", 3, step=step_normal, data=tool_a),
        _envelope("text_delta", "step", 4, step=step_normal, data={"text": "解析"}),
        _envelope("text_delta", "step", 5, step=step_normal, data={"text": "需求"}),
        _envelope("step_completed", "step", 6, step=step_normal, data={"durationS": 1.0}),
        _envelope("step_started", "step", 7, step=step_eval),
        _envelope(
            "candidate_started", "candidate", 8, step=step_eval, candidate=candidate, data={"candidateName": "方案甲"}
        ),
        _envelope(
            "candidate_step_started",
            "candidate_step",
            9,
            step=step_eval,
            candidate=candidate,
            data={"stepId": "template_generating"},
        ),
        _envelope("tool_result", "candidate_step", 10, step=step_eval, candidate=candidate, data=tool_b),
        _envelope("text_delta", "candidate_step", 11, step=step_eval, candidate=candidate, data={"text": "生成模板"}),
        _envelope(
            "candidate_step_completed",
            "candidate_step",
            12,
            step=step_eval,
            candidate=candidate,
            data={"stepId": "template_generating"},
        ),
        _envelope(
            "candidate_completed",
            "candidate",
            13,
            step=step_eval,
            candidate=candidate,
            data={"candidateName": "方案甲"},
        ),
        _envelope("step_completed", "step", 14, step=step_eval, data={"durationS": 2.0}),
    ]


def test_translator_emits_start_before_text_delta():
    translator = PipelineTranscriptTranslator()
    envs = _sample_envelopes()
    # Feed only up to the first text delta of the normal step.
    events = translator.translate_all(envs[:4])
    types = [event["type"] for event in events]
    # step marker, then tool triple (which opens the message), then text delta.
    assert types[0] == PIPELINE_MARKER_EVENT
    assert "assistant.message.start" in types
    start_index = types.index("assistant.message.start")
    delta_index = next(i for i, event in enumerate(events) if event["type"] == "assistant.text.delta")
    assert start_index < delta_index
    # Text that follows a tool opens a fresh segment message, so the tool and the
    # text land in different messages (text → tool → text interleaving).
    assert types.count("assistant.message.start") == 2
    tool_message_id = next(e["payload"]["messageId"] for e in events if e["type"] == "tool.started")
    text_message_id = next(e["payload"]["messageId"] for e in events if e["type"] == "assistant.text.delta")
    assert tool_message_id == "pl-step-intent_parsing-1"
    assert text_message_id == "pl-step-intent_parsing-1#1"


def test_translator_interleaves_text_and_tools_within_a_step():
    # text → tool → text must produce three ordered segments, not one lump.
    step = {"id": "s", "runId": "r-1", "index": 1, "total": 3}
    envelopes = [
        _envelope("step_started", "step", 1, step=step),
        _envelope("text_delta", "step", 2, step=step, data={"text": "先说明"}),
        _envelope(
            "tool_result", "step", 3, step=step, data={"toolName": "read_file", "toolUseId": "t1", "result": "ok"}
        ),
        _envelope("text_delta", "step", 4, step=step, data={"text": "再总结"}),
    ]
    events = PipelineTranscriptTranslator().translate_all(envelopes)
    # Segment 0 carries the first text and the tool; segment 1 carries later text.
    first_text = next(e for e in events if e["type"] == "assistant.text.delta" and e["payload"]["delta"] == "先说明")
    tool = next(e for e in events if e["type"] == "tool.started")
    second_text = next(e for e in events if e["type"] == "assistant.text.delta" and e["payload"]["delta"] == "再总结")
    assert first_text["payload"]["messageId"] == "pl-r-1"
    assert tool["payload"]["messageId"] == "pl-r-1"
    assert second_text["payload"]["messageId"] == "pl-r-1#1"


def test_translator_carries_tool_input_and_step_duration():
    step = {"id": "intent_parsing", "runId": "r-2", "index": 1, "total": 5}
    envelopes = [
        _envelope("step_started", "step", 1, step=step),
        _envelope(
            "tool_result",
            "step",
            2,
            step=step,
            data={
                "toolName": "complete_step",
                "toolUseId": "t-conc",
                "result": "步骤 意图解析 已完成",
                "input": {"conclusion": {"is_infra_intent": True, "confidence": 0.9}},
            },
        ),
        _envelope("step_completed", "step", 3, step=step, data={"durationS": 4.5}),
    ]
    events = PipelineTranscriptTranslator().translate_all(envelopes)
    input_delta = next((e for e in events if e["type"] == "tool.input.delta"), None)
    assert input_delta is not None, "tool input should stream via tool.input.delta"
    assert '"is_infra_intent": true' in input_delta["payload"]["delta"]
    completed_marker = next(
        e
        for e in events
        if e["type"] == PIPELINE_MARKER_EVENT and e["payload"]["pipelineStep"]["status"] == "completed"
    )
    assert completed_marker["payload"]["pipelineStep"]["durationS"] == 4.5


def test_translator_tracks_active_sub_step_for_candidate_scope():
    translator = PipelineTranscriptTranslator()
    events = translator.translate_all(_sample_envelopes())
    # The candidate-scoped tool + text should attach to the sub-step message id.
    tool_started = [e for e in events if e["type"] == "tool.started" and e["payload"]["toolUseId"] == "call_b"]
    assert tool_started, "candidate-scoped tool result should produce tool.started"
    assert tool_started[0]["payload"]["messageId"] == "pl-cand-x-0-1-template_generating"


def test_build_rows_nesting_and_tool_attachment():
    rows = build_pipeline_transcript_rows(_sample_envelopes())
    kinds = [(row.get("kind"), (row.get("pipelineStep") or {}).get("depth")) for row in rows]
    assert ("pipeline_step", 0) in kinds
    assert ("pipeline_candidate", 1) in kinds
    assert ("pipeline_sub_step", 2) in kinds

    # Marker order: intent step, eval step, candidate, sub-step. Completion events
    # reuse (update in place) the same markers rather than appending new rows.
    marker_rows = [row for row in rows if row.get("kind", "").startswith("pipeline_")]
    depths = [(row["pipelineStep"]["depth"]) for row in marker_rows]
    assert depths == [0, 0, 1, 2]

    # The normal step assistant row carries the read_memory tool.
    normal_assistant = next(row for row in rows if not row.get("kind") and "call_a" in row["toolUseIds"])
    assert normal_assistant["tools"]["call_a"]["toolName"] == "read_memory"
    assert normal_assistant["tools"]["call_a"]["status"] == "completed"

    # The candidate sub-step tool row carries the read_file tool; the text that follows
    # it lands in a separate segment row (text → tool → text interleaving).
    sub_assistant = next(row for row in rows if not row.get("kind") and "call_b" in row["toolUseIds"])
    assert sub_assistant["content"] == ""
    assert sub_assistant["tools"]["call_b"]["toolName"] == "read_file"
    sub_text = next(row for row in rows if not row.get("kind") and row.get("id") == f"{sub_assistant['id']}#1")
    assert sub_text["content"] == "生成模板"


def test_build_rows_carry_live_stable_ids_for_reload_dedup():
    # Each reload row must carry the exact stable id the live translator uses as its
    # ensureMessage key, so a mid-run reload dedups stored rows against the replayed
    # live SSE stream instead of duplicating markers (regression: reload-mid-run dup).
    envelopes = _sample_envelopes()
    rows = build_pipeline_transcript_rows(envelopes)
    row_ids = {row.get("id") for row in rows}
    assert "" not in row_ids and None not in row_ids

    live_events = PipelineTranscriptTranslator().translate_all(envelopes)
    live_ids = set()
    for event in live_events:
        payload = event.get("payload") or {}
        live_ids.add(payload.get("markerId") or payload.get("messageId"))
    live_ids.discard(None)

    # Every stored row id is a real live ensureMessage key.
    assert row_ids <= live_ids
    # The known stable ids appear on both sides.
    assert "plmk-step-intent_parsing-1" in row_ids
    assert "pl-cand-x-0-1-template_generating" in row_ids


def _candidate_step_progress_envelopes(candidate_step: dict[str, Any]) -> list[dict[str, Any]]:
    step_eval = {"id": "evaluate_candidates", "runId": "step-evaluate_candidates-1", "index": 3, "total": 5}
    candidate = {"id": "evaluate_candidate_a", "runId": "cand-a-0-1", "index": 0, "name": "方案甲"}
    return [
        _envelope("pipeline_started", "pipeline", 1, data={"totalSteps": 5}),
        _envelope("step_started", "step", 2, step=step_eval),
        _envelope(
            "candidate_started", "candidate", 3, step=step_eval, candidate=candidate, data={"candidateName": "方案甲"}
        ),
        _envelope(
            "candidate_step_started",
            "candidate_step",
            4,
            step=step_eval,
            candidate=candidate,
            candidateStep=candidate_step,
        ),
        _envelope(
            "candidate_step_completed",
            "candidate_step",
            5,
            step=step_eval,
            candidate=candidate,
            candidateStep=candidate_step,
            data={"durationS": 1.0},
        ),
    ]


def test_candidate_sub_step_marker_carries_progress_suffix_live():
    # Issue 2: 方案里的子步骤也应带 N/M 进度(如 1/3),与顶层步骤一致。
    candidate_step = {
        "id": "template_generating",
        "runId": "cand-a-0-1-template-generating-1",
        "attempt": 1,
        "index": 1,
        "total": 3,
    }
    events = PipelineTranscriptTranslator().translate_all(_candidate_step_progress_envelopes(candidate_step))
    sub_markers = [
        event
        for event in events
        if event["type"] == PIPELINE_MARKER_EVENT
        and (event["payload"].get("pipelineStep") or {}).get("level") == "sub_step"
    ]
    assert sub_markers, "expected a sub_step marker for the candidate step"
    for marker in sub_markers:
        payload = marker["payload"]
        assert payload["content"].startswith("· ")
        assert payload["content"].endswith(" (1/3)")
        assert payload["pipelineStep"]["index"] == 1
        assert payload["pipelineStep"]["total"] == 3


def test_candidate_sub_step_marker_carries_progress_suffix_on_reload():
    # The reloaded transcript must match the live one: the persisted sub_step row
    # keeps the (N/M) suffix so a mid-run reload does not lose the progress info.
    candidate_step = {
        "id": "template_generating",
        "runId": "cand-a-0-1-template-generating-1",
        "attempt": 1,
        "index": 2,
        "total": 2,
    }
    rows = build_pipeline_transcript_rows(_candidate_step_progress_envelopes(candidate_step))
    sub_step = next(row for row in rows if row.get("kind") == "pipeline_sub_step")
    assert sub_step["content"].endswith(" (2/2)")
    assert sub_step["pipelineStep"]["index"] == 2
    assert sub_step["pipelineStep"]["total"] == 2


def test_candidate_sub_step_marker_omits_progress_without_coordinate():
    # Legacy journals (no candidateStep index/total) must not gain a bogus suffix;
    # the sub-step stays a bare title so old transcripts render unchanged.
    rows = build_pipeline_transcript_rows(_sample_envelopes())
    sub_step = next(row for row in rows if row.get("kind") == "pipeline_sub_step")
    assert "/" not in sub_step["content"]
    assert sub_step["pipelineStep"]["index"] is None
    assert sub_step["pipelineStep"]["total"] is None


def test_build_rows_marks_completed_status():
    rows = build_pipeline_transcript_rows(_sample_envelopes())
    step_markers = [row for row in rows if row.get("kind") == "pipeline_step"]
    # Both step markers were completed within the sample.
    assert all(row["pipelineStep"]["status"] == "completed" for row in step_markers)
    candidate_marker = next(row for row in rows if row.get("kind") == "pipeline_candidate")
    assert candidate_marker["pipelineStep"]["status"] == "completed"


def test_translator_tool_started_marks_running_then_result_reuses_segment():
    # Issue 2/6: a tool announces itself as running the moment its call is emitted
    # (正在…), and its later result completes the same card in place instead of
    # re-opening a fresh tool.started.
    step = {"id": "gen", "runId": "r-9", "index": 1, "total": 2}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    started = translator.push(
        _envelope(
            "tool_started",
            "step",
            2,
            step=step,
            data={"toolName": "run_bash", "toolUseId": "b1", "input": {"command": "ls"}},
        )
    )
    tool_started = next(e for e in started if e["type"] == "tool.started")
    assert tool_started["payload"]["status"] == "running"
    running_msg = tool_started["payload"]["messageId"]
    assert any(e["type"] == "tool.input.delta" and "ls" in e["payload"]["delta"] for e in started)

    result = translator.push(
        _envelope(
            "tool_result",
            "step",
            3,
            step=step,
            data={"toolName": "run_bash", "toolUseId": "b1", "result": "done", "isError": False},
        )
    )
    result_types = [e["type"] for e in result]
    # The running card is completed in place: no second tool.started, same message id.
    assert "tool.started" not in result_types
    finished = next(e for e in result if e["type"] == "tool.finished")
    assert finished["payload"]["messageId"] == running_msg
    assert finished["payload"]["status"] == "completed"


def test_translator_folds_stack_progress_into_pipeline_event():
    # A2A stack_progress envelope → inline web pipeline.event that the frontend
    # reducer attaches to state.tools[toolUseId].stackProgress (REPL-style card).
    step = {"id": "deploying", "runId": "r-dep", "index": 1, "total": 2}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    translator.push(
        _envelope(
            "tool_started",
            "step",
            2,
            step=step,
            data={"toolName": "ros_deploy", "toolUseId": "call_deploy", "input": {}},
        )
    )
    events = translator.push(
        _envelope(
            "stack_progress",
            "step",
            3,
            step=step,
            data={
                "toolUseId": "call_deploy",
                "stackId": "stk-1",
                "stackName": "prod-stack",
                "status": "CREATE_IN_PROGRESS",
                "progressPercentage": 55.0,
                "resources": [{"logicalId": "vpc", "status": "CREATE_COMPLETE"}],
                "elapsedSeconds": 20,
            },
        )
    )

    pipeline_event = next(e for e in events if e["type"] == "pipeline.event")
    payload = pipeline_event["payload"]
    assert payload["kind"] == "stack.progress"
    assert payload["toolUseId"] == "call_deploy"
    assert payload["stackName"] == "prod-stack"
    assert payload["progressPercentage"] == 55.0
    assert payload["resources"] == [{"logicalId": "vpc", "status": "CREATE_COMPLETE"}]
    # Bound to the tool card's message so reload attaches to the right step.
    assert payload["messageId"]


def test_translator_folds_stack_instances_progress_into_pipeline_event():
    step = {"id": "deploying", "runId": "r-dep", "index": 1, "total": 2}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    translator.push(
        _envelope(
            "tool_started",
            "step",
            2,
            step=step,
            data={"toolName": "ros_stack_instances", "toolUseId": "call_inst", "input": {}},
        )
    )
    events = translator.push(
        _envelope(
            "stack_instances_progress",
            "step",
            3,
            step=step,
            data={
                "toolUseId": "call_inst",
                "stackGroupName": "grp-1",
                "operationId": "op-1",
                "status": "RUNNING",
                "progressPercentage": 40,
                "instances": [{"accountId": "1", "status": "SUCCEEDED"}],
                "elapsedSeconds": 8,
            },
        )
    )

    payload = next(e for e in events if e["type"] == "pipeline.event")["payload"]
    assert payload["kind"] == "stack.instances.progress"
    assert payload["toolUseId"] == "call_inst"
    assert payload["stackGroupName"] == "grp-1"
    assert payload["operationId"] == "op-1"
    assert payload["instances"] == [{"accountId": "1", "status": "SUCCEEDED"}]


def test_stack_progress_missing_tool_use_id_is_dropped():
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step={"id": "deploying", "runId": "r"}))
    events = translator.push(
        _envelope("stack_progress", "step", 2, data={"stackName": "x", "status": "CREATE_IN_PROGRESS"})
    )
    assert not any(e["type"] == "pipeline.event" for e in events)


def test_build_rows_attaches_stack_progress_to_tool_on_reload():
    # Reload path: the folded pipeline.event must re-attach stackProgress to the
    # tool card so a returning browser sees the same resource table + percentage.
    step = {"id": "deploying", "runId": "r-dep", "index": 1, "total": 2}
    envelopes = [
        _envelope("pipeline_started", "pipeline", 1, data={"totalSteps": 2}),
        _envelope("step_started", "step", 2, step=step),
        _envelope(
            "tool_started",
            "step",
            3,
            step=step,
            data={"toolName": "ros_deploy", "toolUseId": "call_deploy", "input": {}},
        ),
        _envelope(
            "stack_progress",
            "step",
            4,
            step=step,
            data={
                "toolUseId": "call_deploy",
                "stackId": "stk-1",
                "stackName": "prod-stack",
                "status": "CREATE_COMPLETE",
                "progressPercentage": 100.0,
                "resources": [{"logicalId": "vpc", "status": "CREATE_COMPLETE"}],
                "elapsedSeconds": 247,
            },
        ),
    ]
    rows = build_pipeline_transcript_rows(envelopes)
    tool = None
    for row in rows:
        if "call_deploy" in row.get("tools", {}):
            tool = row["tools"]["call_deploy"]
            break
    assert tool is not None
    assert tool["stackProgress"]["stackName"] == "prod-stack"
    assert tool["stackProgress"]["progressPercentage"] == 100.0
    assert tool["stackProgress"]["resources"] == [{"logicalId": "vpc", "status": "CREATE_COMPLETE"}]


def test_translator_forwards_tombstone_and_forgets_orphaned_tool_segment():
    translator = PipelineTranscriptTranslator()
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1"}

    translator.push(
        _envelope(
            "tool_started",
            "step",
            1,
            step=step,
            data={"toolName": "bash", "toolUseId": "tool-orphaned", "input": {"cmd": "false"}},
        )
    )
    tombstoned = translator.push(
        _envelope(
            "message_tombstone",
            "step",
            2,
            step=step,
            data={"messageId": "provider-message", "affectedToolUseIds": ["tool-orphaned"]},
        )
    )

    assert tombstoned == [
        {
            "type": "assistant.message.tombstone",
            "payload": {
                "messageId": "pl-step-intent_parsing-1",
                "affectedToolUseIds": ["tool-orphaned"],
            },
        }
    ]
    replacement = translator.push(
        _envelope(
            "tool_result",
            "step",
            3,
            step=step,
            data={"toolName": "bash", "toolUseId": "tool-orphaned", "result": "replacement"},
        )
    )
    assert any(event["type"] == "tool.started" for event in replacement)


def test_translator_tombstones_failed_partial_text_before_fallback_replacement():
    translator = PipelineTranscriptTranslator()
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1"}

    partial = translator.push(
        _envelope("text_delta", "step", 1, step=step, data={"text": "partial-from-failed-stream"})
    )
    message_id = next(event["payload"]["messageId"] for event in partial if event["type"] == "assistant.message.start")
    tombstone = translator.push(
        _envelope(
            "message_tombstone",
            "step",
            2,
            step=step,
            data={"messageId": "provider-message-42", "affectedToolUseIds": []},
        )
    )
    replacement = translator.push(
        _envelope("text_delta", "step", 3, step=step, data={"text": "complete-fallback-answer"})
    )

    assert tombstone == [
        {
            "type": "assistant.message.tombstone",
            "payload": {"messageId": message_id, "affectedToolUseIds": []},
        }
    ]
    assert replacement == [
        {"type": "assistant.message.start", "payload": {"messageId": message_id}},
        {
            "type": "assistant.text.delta",
            "payload": {"messageId": message_id, "delta": "complete-fallback-answer"},
        },
    ]


def test_translator_tombstones_every_segment_from_failed_provider_message():
    translator = PipelineTranscriptTranslator()
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1"}
    translator.push(_envelope("step_started", "step", 1, step=step))
    translator.push(
        _envelope(
            "message_started",
            "step",
            2,
            step=step,
            data={"messageId": "provider-message-42"},
        )
    )
    translator.push(_envelope("text_delta", "step", 3, step=step, data={"text": "partial"}))
    translator.push(
        _envelope(
            "tool_started",
            "step",
            4,
            step=step,
            data={"toolName": "bash", "toolUseId": "tool-orphaned", "input": {"cmd": "false"}},
        )
    )
    translator.push(_envelope("text_delta", "step", 5, step=step, data={"text": "after-tool"}))

    tombstones = translator.push(
        _envelope(
            "message_tombstone",
            "step",
            6,
            step=step,
            data={"messageId": "provider-message-42", "affectedToolUseIds": ["tool-orphaned"]},
        )
    )
    translator.push(
        _envelope(
            "message_started",
            "step",
            7,
            step=step,
            data={"messageId": "provider-message-fallback"},
        )
    )
    replacement = translator.push(
        _envelope("text_delta", "step", 8, step=step, data={"text": "complete-fallback-answer"})
    )

    assert [event["payload"]["messageId"] for event in tombstones] == [
        "pl-step-intent_parsing-1",
        "pl-step-intent_parsing-1#1",
    ]
    assert replacement == [
        {"type": "assistant.message.start", "payload": {"messageId": "pl-step-intent_parsing-1"}},
        {
            "type": "assistant.text.delta",
            "payload": {
                "messageId": "pl-step-intent_parsing-1",
                "delta": "complete-fallback-answer",
            },
        },
    ]


def test_translator_does_not_tombstone_prior_segment_when_failed_provider_message_has_no_delta():
    translator = PipelineTranscriptTranslator()
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1"}
    translator.push(
        _envelope(
            "message_started",
            "step",
            1,
            step=step,
            data={"messageId": "provider-success"},
        )
    )
    successful = translator.push(_envelope("text_delta", "step", 2, step=step, data={"text": "keep-me"}))
    translator.push(
        _envelope(
            "message_started",
            "step",
            3,
            step=step,
            data={"messageId": "provider-empty-failure"},
        )
    )

    tombstones = translator.push(
        _envelope(
            "message_tombstone",
            "step",
            4,
            step=step,
            data={"messageId": "provider-empty-failure", "affectedToolUseIds": []},
        )
    )
    translator.push(
        _envelope(
            "message_started",
            "step",
            5,
            step=step,
            data={"messageId": "provider-fallback"},
        )
    )
    replacement = translator.push(_envelope("text_delta", "step", 6, step=step, data={"text": "replacement"}))

    assert any(event["payload"].get("delta") == "keep-me" for event in successful)
    assert tombstones == []
    assert replacement == [
        {
            "type": "assistant.text.delta",
            "payload": {"messageId": "pl-step-intent_parsing-1", "delta": "replacement"},
        }
    ]


def test_build_rows_replaces_tombstoned_partial_segment_with_fallback():
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1"}
    envelopes = [
        _envelope("step_started", "step", 1, step=step),
        _envelope(
            "message_started",
            "step",
            2,
            step=step,
            data={"messageId": "provider-failed"},
        ),
        _envelope("text_delta", "step", 3, step=step, data={"text": "FAILED-PARTIAL"}),
        _envelope(
            "tool_started",
            "step",
            4,
            step=step,
            data={"toolName": "bash", "toolUseId": "failed-tool", "input": {"cmd": "false"}},
        ),
        _envelope(
            "message_tombstone",
            "step",
            5,
            step=step,
            data={"messageId": "provider-failed", "affectedToolUseIds": ["failed-tool"]},
        ),
        _envelope(
            "message_started",
            "step",
            6,
            step=step,
            data={"messageId": "provider-fallback"},
        ),
        _envelope("text_delta", "step", 7, step=step, data={"text": "GOOD-FALLBACK"}),
    ]

    rows = build_pipeline_transcript_rows(envelopes)
    content_rows = [row for row in rows if row["id"] == "pl-step-intent_parsing-1"]

    assert len(content_rows) == 1
    assert content_rows[0]["content"] == "GOOD-FALLBACK"
    assert content_rows[0]["toolUseIds"] == []
    assert content_rows[0]["tools"] == {}


def test_translator_derives_duration_from_created_at_when_missing():
    # Issue 1: a *_completed envelope without durationS falls back to the elapsed
    # time between the *_started and *_completed createdAt timestamps.
    step = {"id": "s3", "runId": "r-dur", "index": 3, "total": 5}
    envelopes = [
        _envelope("step_started", "step", 1, step=step, createdAt="2026-01-01T00:00:00Z"),
        _envelope("step_completed", "step", 2, step=step, createdAt="2026-01-01T00:00:03Z"),
    ]
    events = PipelineTranscriptTranslator().translate_all(envelopes)
    completed_marker = next(
        e
        for e in events
        if e["type"] == PIPELINE_MARKER_EVENT and e["payload"]["pipelineStep"]["status"] == "completed"
    )
    assert completed_marker["payload"]["pipelineStep"]["durationS"] == 3.0


def test_translator_derives_duration_when_duration_zero():
    # Issue 1: a *_completed envelope whose durationS is 0 (not just missing) also
    # falls back to the elapsed createdAt span, so e.g. step 3 shows its real time.
    step = {"id": "evaluate_candidates", "runId": "r-0", "index": 3, "total": 5}
    envelopes = [
        _envelope("step_started", "step", 1, step=step, createdAt="2026-01-01T00:00:00Z"),
        _envelope("step_completed", "step", 2, step=step, data={"durationS": 0}, createdAt="2026-01-01T00:05:07Z"),
    ]
    events = PipelineTranscriptTranslator().translate_all(envelopes)
    completed_marker = next(
        e
        for e in events
        if e["type"] == PIPELINE_MARKER_EVENT and e["payload"]["pipelineStep"]["status"] == "completed"
    )
    assert completed_marker["payload"]["pipelineStep"]["durationS"] == 307.0


def test_pipeline_canceled_marks_running_step_canceled():
    # Issue 7b: a step interrupted mid-run (deploying started, never completed)
    # flips to canceled on pipeline_canceled instead of staying "working" forever.
    step = {"id": "deploying", "runId": "step-deploying-1", "index": 5, "total": 5}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    translator.push(
        _envelope("tool_result", "step", 2, step=step, data={"toolName": "ros_stack", "toolUseId": "d1", "result": "x"})
    )
    canceled = translator.push(_envelope("pipeline_canceled", "pipeline", 3))
    markers = [e for e in canceled if e["type"] == PIPELINE_MARKER_EVENT]
    assert len(markers) == 1
    assert markers[0]["payload"]["markerId"] == "plmk-step-deploying-1"
    assert markers[0]["payload"]["pipelineStep"]["status"] == "canceled"
    # Idempotent: a replayed cancel produces no further markers.
    assert translator.push(_envelope("pipeline_canceled", "pipeline", 4)) == []


def test_pipeline_canceled_leaves_completed_steps_alone():
    # Only still-running markers flip; a step already completed is untouched.
    step = {"id": "intent_parsing", "runId": "r-1", "index": 1, "total": 5}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    translator.push(_envelope("step_completed", "step", 2, step=step, data={"durationS": 1.0}))
    assert translator.push(_envelope("pipeline_canceled", "pipeline", 3)) == []


def test_build_rows_marks_running_step_canceled_on_cancel():
    # Reload path: the un-completed deploying step is stored as canceled, in place.
    step_done = {"id": "intent_parsing", "runId": "r-1", "index": 1, "total": 5}
    step_run = {"id": "deploying", "runId": "step-deploying-1", "index": 5, "total": 5}
    envelopes = [
        _envelope("step_started", "step", 1, step=step_done),
        _envelope("step_completed", "step", 2, step=step_done, data={"durationS": 1.0}),
        _envelope("step_started", "step", 3, step=step_run),
        _envelope(
            "tool_result", "step", 4, step=step_run, data={"toolName": "ros_stack", "toolUseId": "d1", "result": "x"}
        ),
        _envelope("pipeline_canceled", "pipeline", 5),
    ]
    rows = build_pipeline_transcript_rows(envelopes)
    deploying = next(row for row in rows if row.get("id") == "plmk-step-deploying-1")
    assert deploying["pipelineStep"]["status"] == "canceled"
    intent = next(row for row in rows if row.get("id") == "plmk-r-1")
    assert intent["pipelineStep"]["status"] == "completed"
    # The deploying marker keeps its original position (its content row follows it),
    # not shoved to the end by the late cancel.
    ids = [row.get("id") for row in rows]
    assert ids.index("plmk-step-deploying-1") < ids.index("pl-step-deploying-1")


def test_failed_envelopes_finalize_step_candidate_and_sub_step_markers():
    step = {"id": "evaluate_candidates", "runId": "step-evaluate-1", "index": 3, "total": 5}
    candidate = {"id": "candidate-a", "runId": "candidate-a-0-1", "name": "方案甲"}
    candidate_step = {
        "id": "template_generating",
        "runId": "candidate-a-0-1-template-generating-1",
        "attempt": 1,
    }
    envelopes = [
        _envelope("step_started", "step", 1, step=step),
        _envelope("candidate_started", "candidate", 2, step=step, candidate=candidate),
        _envelope(
            "candidate_step_started",
            "candidate_step",
            3,
            step=step,
            candidate=candidate,
            candidateStep=candidate_step,
        ),
        _envelope(
            "candidate_step_failed",
            "candidate_step",
            4,
            step=step,
            candidate=candidate,
            candidateStep=candidate_step,
        ),
        _envelope("candidate_failed", "candidate", 5, step=step, candidate=candidate),
        _envelope("step_failed", "step", 6, step=step),
    ]

    rows = build_pipeline_transcript_rows(envelopes)
    statuses = {row["id"]: row["pipelineStep"]["status"] for row in rows if row.get("pipelineStep") is not None}
    assert statuses["plmk-candidate-a-0-1-template-generating-1"] == "failed"
    assert statuses["plmk-candidate-a-0-1"] == "failed"
    assert statuses["plmk-step-evaluate-1"] == "failed"


def test_pipeline_failed_finalizes_any_remaining_active_markers():
    step = {"id": "deploying", "runId": "step-deploying-1", "index": 5, "total": 5}
    rows = build_pipeline_transcript_rows(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("text_delta", "step", 2, step=step, data={"text": "deploying"}),
            _envelope("pipeline_failed", "pipeline", 3, data={"error": "boom"}),
        ]
    )

    marker = next(row for row in rows if row.get("id") == "plmk-step-deploying-1")
    assert marker["pipelineStep"]["status"] == "failed"


def test_handoff_ready_emits_normal_chat_boundary_once():
    # Issue 7: the handoff-to-normal envelope emits a single "↪ 普通对话" boundary
    # matching the reload marker shape, and re-pushing does not duplicate it.
    translator = PipelineTranscriptTranslator()
    events = translator.push(
        _envelope(
            "pipeline_handoff_ready",
            "pipeline",
            20,
            data={"action": "switch_to_normal", "targetMode": "normal"},
        )
    )
    assert len(events) == 1
    payload = events[0]["payload"]
    assert events[0]["type"] == PIPELINE_MARKER_EVENT
    assert payload["markerId"] == "plmk-normal-chat"
    assert payload["kind"] == "normal_chat_boundary"
    assert payload["content"] == "↪ Normal chat"
    step = payload["pipelineStep"]
    assert step["level"] == "normal_chat"
    assert step["title"] == "Normal chat"
    assert step["groupId"] == "normal-chat"
    assert step["depth"] == 0
    # Idempotent: a replayed handoff envelope produces no second boundary.
    again = translator.push(
        _envelope(
            "pipeline_handoff_ready",
            "pipeline",
            21,
            data={"action": "switch_to_normal", "targetMode": "normal"},
        )
    )
    assert again == []


def test_handoff_ready_ignores_non_switch_actions():
    translator = PipelineTranscriptTranslator()
    assert translator.push(_envelope("pipeline_handoff_ready", "pipeline", 5, data={"action": "resume"})) == []
    assert translator.push(_envelope("pipeline_handoff_ready", "pipeline", 6, data={})) == []


def test_handoff_ready_emits_outcome_marker_before_boundary():
    # 交接信封带 outcome 时,翻译器在 boundary 之前先发一条 pipeline_outcome 彩条,
    # 携带终态枚举,序号必早于 boundary → 落在「↪ 普通对话」的紧前方。
    translator = PipelineTranscriptTranslator()
    events = translator.push(
        _envelope(
            "pipeline_handoff_ready",
            "pipeline",
            20,
            data={"action": "switch_to_normal", "targetMode": "normal", "outcome": "failed"},
        )
    )
    assert [e["payload"]["kind"] for e in events] == ["pipeline_outcome", "normal_chat_boundary"]
    outcome = events[0]["payload"]
    assert outcome["markerId"] == "plmk-outcome"
    assert outcome["pipelineStep"]["outcome"] == "failed"
    assert outcome["pipelineStep"]["depth"] == 0


def test_handoff_ready_without_outcome_emits_boundary_only():
    # 无 outcome(旧信封/不可知终态)时不发空彩条,仅保留 boundary。
    translator = PipelineTranscriptTranslator()
    events = translator.push(
        _envelope(
            "pipeline_handoff_ready",
            "pipeline",
            20,
            data={"action": "switch_to_normal", "targetMode": "normal"},
        )
    )
    assert [e["payload"]["kind"] for e in events] == ["normal_chat_boundary"]


def test_pending_backup_handoff_is_ignored_until_committed():
    translator = PipelineTranscriptTranslator()
    pending = _envelope(
        "pipeline_handoff_ready",
        "pipeline",
        20,
        visibility="pending_backup",
        data={"action": "switch_to_normal", "targetMode": "normal", "outcome": "failed"},
    )
    committed = _envelope(
        "pipeline_handoff_ready",
        "pipeline",
        21,
        visibility="committed",
        data={"action": "switch_to_normal", "targetMode": "normal", "outcome": "failed"},
    )

    assert translator.push(pending) == []
    assert [event["payload"]["kind"] for event in translator.push(committed)] == [
        "pipeline_outcome",
        "normal_chat_boundary",
    ]


def test_pending_backup_cancel_is_ignored_until_committed():
    translator = PipelineTranscriptTranslator()
    step = {"id": "deploying", "runId": "step-deploying-1", "index": 4, "total": 5}
    translator.push(_envelope("step_started", "step", 1, step=step))

    assert translator.push(_envelope("pipeline_canceled", "pipeline", 2, visibility="pending_backup")) == []
    committed = translator.push(_envelope("pipeline_canceled", "pipeline", 3, visibility="committed"))

    markers = [event for event in committed if event["type"] == PIPELINE_MARKER_EVENT]
    assert len(markers) == 1
    assert markers[0]["payload"]["pipelineStep"]["status"] == "canceled"


def test_unknown_event_types_are_ignored():
    translator = PipelineTranscriptTranslator()
    assert translator.push({"eventType": "permission_requested", "scope": "step"}) == []
    assert translator.push({"eventType": "totally_unknown"}) == []
    assert translator.push({}) == []


def test_input_required_reopens_completed_step_marker():
    # Issue 1: confirm_and_select emits step_completed *before* input_required (it
    # computes the options, marks itself done, then asks the user to pick). The
    # completed step must be re-emitted as status="input" — same markerId — so the
    # frontend keeps it expanded (with a "等待输入" hint) instead of folding a step
    # that is actually waiting on the user, who would otherwise think it is stuck.
    step = {"id": "confirm_and_select", "runId": "r-sel", "index": 4, "total": 5}
    translator = PipelineTranscriptTranslator()
    translator.translate_all(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("step_completed", "step", 2, step=step, data={"durationS": 2.0}),
        ]
    )
    events = translator.push(_envelope("input_required", "step", 3, step=step, data={"prompt": "请选择方案"}))
    # The prompt is streamed as assistant text so the question is visible in the transcript.
    assert any(e["type"] == "assistant.text.delta" and e["payload"]["delta"] == "请选择方案" for e in events)
    input_marker = next(e for e in events if e["type"] == PIPELINE_MARKER_EVENT)
    assert input_marker["payload"]["pipelineStep"]["status"] == "input"
    # Same markerId as the original step marker → frontend updates in place, no dup.
    assert input_marker["payload"]["markerId"] == "plmk-r-sel"


def test_input_received_restores_step_marker_status():
    # Once the user answers, the step must fold back to its real status (completed),
    # so a fully-answered run does not leave a step stuck showing "等待输入".
    step = {"id": "confirm_and_select", "runId": "r-sel", "index": 4, "total": 5}
    translator = PipelineTranscriptTranslator()
    translator.translate_all(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("step_completed", "step", 2, step=step, data={"durationS": 2.0}),
            _envelope("input_required", "step", 3, step=step, data={"prompt": "请选择方案"}),
        ]
    )
    events = translator.push(_envelope("input_received", "step", 4, step=step, data={"value": "1"}))
    restored = next(e for e in events if e["type"] == PIPELINE_MARKER_EVENT)
    assert restored["payload"]["pipelineStep"]["status"] == "completed"
    assert restored["payload"]["markerId"] == "plmk-r-sel"


def test_build_rows_paused_at_input_keeps_step_status_input():
    # Reloading a run paused at the selection prompt (input_required with no
    # input_received) must render the owning step with status="input" so the
    # reload path shows the same forced-open "等待输入" step the live stream does.
    step = {"id": "confirm_and_select", "runId": "r-sel", "index": 4, "total": 5}
    rows = build_pipeline_transcript_rows(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("step_completed", "step", 2, step=step, data={"durationS": 2.0}),
            _envelope("input_required", "step", 3, step=step, data={"prompt": "请选择方案"}),
        ]
    )
    sel_marker = next(row for row in rows if row.get("kind") == "pipeline_step")
    assert sel_marker["pipelineStep"]["status"] == "input"


def test_compaction_envelope_folds_to_boundary_row_step_scope():
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1", "index": 1, "total": 5}
    envelopes = [
        _envelope("step_started", "step", 1, step=step),
        _envelope(
            "context_compacted",
            "step",
            2,
            step=step,
            eventId="evt-abc",
            data={"summary": "S", "originalTokens": 100, "compactedTokens": 10},
        ),
    ]
    rows = build_pipeline_transcript_rows(envelopes)
    boundary = [r for r in rows if r.get("kind") == "context_compaction_boundary"]
    assert len(boundary) == 1
    assert boundary[0]["content"] == "S"
    assert boundary[0]["role"] == "assistant"
    assert boundary[0]["pipelineStep"]["stepId"] == "intent_parsing"


def test_compaction_envelope_folds_within_candidate_step_group():
    step = {"id": "evaluate_candidates", "runId": "step-evaluate_candidates-1", "index": 3, "total": 5}
    candidate = {"id": "evaluate_candidate_x", "runId": "cand-x-0-1", "index": 0, "name": "方案甲"}
    envelopes = [
        _envelope("step_started", "step", 1, step=step),
        _envelope(
            "candidate_started", "candidate", 2, step=step, candidate=candidate, data={"candidateName": "方案甲"}
        ),
        _envelope(
            "candidate_step_started",
            "candidate_step",
            3,
            step=step,
            candidate=candidate,
            data={"stepId": "template_generating"},
        ),
        _envelope(
            "context_compacted",
            "candidate_step",
            4,
            step=step,
            candidate=candidate,
            eventId="evt-c",
            data={"summary": "CS", "originalTokens": 50, "compactedTokens": 5},
        ),
    ]
    rows = build_pipeline_transcript_rows(envelopes)
    boundary = [r for r in rows if r.get("kind") == "context_compaction_boundary"]
    assert len(boundary) == 1
    assert boundary[0]["content"] == "CS"
    # coordinates cloned from the candidate-step group's base marker
    assert boundary[0]["pipelineStep"]["groupId"] == "cand-x-0-1:template_generating"


def test_compaction_started_emits_running_sse():
    # started 相位折成 compaction.started SSE,驱动前端底部「正在自动压缩上下文」流光条
    # (buildCompactionIndicator)。这是本次修复的核心:旧实现里 started 被 pipeline_events 丢弃,
    # 转录器无从发条。
    translator = PipelineTranscriptTranslator()
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1", "index": 1, "total": 5}
    events = translator.translate_all(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("context_compaction_started", "step", 2, step=step, data={}),
        ]
    )
    started = [e for e in events if e.get("type") == "compaction.started"]
    assert len(started) == 1
    assert started[0]["payload"]["auto"] is True
    assert started[0]["payload"]["state"] == "started"
    # 携带 groupId(与结束态边界条同源经 _group_id_for 解析),让前端按 groupId 把运行态压缩条
    # 精确挂进触发压缩的步骤——并行候选阶段单凭「首个进行中叶子」会错挂到另一候选。
    assert started[0]["payload"]["groupId"] == "step:step-intent_parsing-1"


def test_compaction_started_group_id_targets_candidate_step():
    # 并行候选阶段(两个方案同时跑 模板生成)是 groupId 归属的核心场景:方案2 触发压缩,started 的
    # groupId 必须解析到方案2 的候选步骤组(candidate-step:<runId>),而非方案1——前端据此把运行态
    # 压缩条挂进正确候选,消除「方案2压缩却显示在方案1」的错位。
    translator = PipelineTranscriptTranslator()
    step_eval = {"id": "evaluate_candidates", "runId": "step-evaluate_candidates-1", "index": 3, "total": 5}
    cand_b = {"id": "evaluate_candidate_b", "runId": "cand-b-1-1", "index": 1, "name": "方案2"}
    cand_step_b = {"id": "template_gen", "runId": "cand-b-1-1-template", "index": 1, "total": 3}
    events = translator.translate_all(
        [
            _envelope("step_started", "step", 1, step=step_eval),
            _envelope("candidate_started", "candidate", 2, step=step_eval, candidate=cand_b),
            _envelope(
                "candidate_step_started",
                "candidate_step",
                3,
                step=step_eval,
                candidate=cand_b,
                candidateStep=cand_step_b,
            ),
            _envelope(
                "context_compaction_started",
                "candidate_step",
                4,
                step=step_eval,
                candidate=cand_b,
                candidateStep=cand_step_b,
                data={},
            ),
        ]
    )
    started = [e for e in events if e.get("type") == "compaction.started"]
    assert len(started) == 1
    assert started[0]["payload"]["groupId"] == "candidate-step:cand-b-1-1-template"


def test_compaction_finished_emits_finished_sse_and_boundary():
    # finished 既要撤掉运行态压缩条(compaction.finished SSE),又要落持久分隔条
    # (context_compaction_boundary marker),缺一不可。
    translator = PipelineTranscriptTranslator()
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1", "index": 1, "total": 5}
    events = translator.translate_all(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope(
                "context_compacted",
                "step",
                2,
                step=step,
                eventId="evt-abc",
                data={"summary": "S", "originalTokens": 100, "compactedTokens": 10},
            ),
        ]
    )
    finished = [e for e in events if e.get("type") == "compaction.finished"]
    assert len(finished) == 1
    assert finished[0]["payload"]["auto"] is True
    # 不带 state="success",避免触发 app.js 的手动压缩重载路径(仅普通/手动成功才重载)。
    assert finished[0]["payload"].get("state") != "success"
    boundary = [e for e in events if e.get("payload", {}).get("kind") == "context_compaction_boundary"]
    assert len(boundary) == 1


def test_compaction_failed_emits_finished_failed_sse():
    translator = PipelineTranscriptTranslator()
    step = {"id": "intent_parsing", "runId": "step-intent_parsing-1", "index": 1, "total": 5}
    events = translator.translate_all(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("context_compaction_failed", "step", 2, step=step, data={}),
        ]
    )
    finished = [e for e in events if e.get("type") == "compaction.finished"]
    assert len(finished) == 1
    assert finished[0]["payload"]["state"] == "failed"


def test_candidate_step_retries_keep_distinct_markers_and_content_segments():
    translator = PipelineTranscriptTranslator()
    step = {"id": "evaluate_candidates", "runId": "step-evaluate_candidates-1"}
    candidate = {"id": "candidate-a", "runId": "candidate-a-0-1", "name": "方案甲"}
    first = {
        "id": "template_generating",
        "runId": "candidate-a-0-1-template_generating-1",
        "attempt": 1,
    }
    second = {
        "id": "template_generating",
        "runId": "candidate-a-0-1-template_generating-2",
        "attempt": 2,
    }

    events = translator.translate_all(
        [
            _envelope(
                "candidate_step_started",
                "candidate_step",
                1,
                step=step,
                candidate=candidate,
                candidateStep=first,
                data={"stepId": "template_generating"},
            ),
            _envelope(
                "text_delta",
                "candidate_step",
                2,
                step=step,
                candidate=candidate,
                candidateStep=first,
                data={"text": "first"},
            ),
            _envelope(
                "candidate_step_completed",
                "candidate_step",
                3,
                step=step,
                candidate=candidate,
                candidateStep=first,
                data={"stepId": "template_generating"},
            ),
            _envelope(
                "candidate_step_started",
                "candidate_step",
                4,
                step=step,
                candidate=candidate,
                candidateStep=second,
                data={"stepId": "template_generating"},
            ),
            _envelope(
                "text_delta",
                "candidate_step",
                5,
                step=step,
                candidate=candidate,
                candidateStep=second,
                data={"text": "second"},
            ),
        ]
    )

    markers = [event["payload"] for event in events if event["type"] == PIPELINE_MARKER_EVENT]
    assert [marker["markerId"] for marker in markers] == [
        "plmk-candidate-a-0-1-template_generating-1",
        "plmk-candidate-a-0-1-template_generating-1",
        "plmk-candidate-a-0-1-template_generating-2",
    ]
    assert [marker["pipelineStep"]["attemptNo"] for marker in markers] == [1, 1, 2]
    deltas = [event["payload"] for event in events if event["type"] == "assistant.text.delta"]
    assert [(payload["messageId"], payload["delta"]) for payload in deltas] == [
        ("pl-candidate-a-0-1-template_generating-1", "first"),
        ("pl-candidate-a-0-1-template_generating-2", "second"),
    ]


def test_compaction_no_envelope_produces_no_boundary_row():
    rows = build_pipeline_transcript_rows(
        [
            _envelope(
                "step_started",
                "step",
                1,
                step={"id": "intent_parsing", "runId": "step-intent_parsing-1", "index": 1, "total": 5},
            )
        ]
    )
    assert not any(r.get("kind") == "context_compaction_boundary" for r in rows)


def test_translator_emits_thinking_delta_on_same_segment_as_text():
    # 流水线现在产出 thinking 事件：思考先于正文到达（此时段内还没有工具），故思考与其后的
    # 正文落在同一条消息段上，与普通模式「一条消息同时带 thinking 与 content」一致。
    step = {"id": "s", "runId": "r-9", "index": 1, "total": 3}
    envelopes = [
        _envelope("step_started", "step", 1, step=step),
        _envelope("thinking_delta", "step", 2, step=step, data={"type": "raw_thinking", "text": "思考中"}),
        _envelope("text_delta", "step", 3, step=step, data={"text": "回答"}),
    ]
    events = PipelineTranscriptTranslator().translate_all(envelopes)
    thinking = next(e for e in events if e["type"] == "assistant.thinking.delta")
    text = next(e for e in events if e["type"] == "assistant.text.delta")
    assert thinking["payload"] == {"messageId": "pl-r-9", "delta": "思考中"}
    assert text["payload"]["messageId"] == "pl-r-9"
    # start 必须先于 thinking delta。
    types = [e["type"] for e in events]
    assert types.index("assistant.message.start") < types.index("assistant.thinking.delta")


def test_translator_skips_empty_thinking_delta():
    step = {"id": "s", "runId": "r-10", "index": 1, "total": 3}
    events = PipelineTranscriptTranslator().translate_all(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("thinking_delta", "step", 2, step=step, data={"type": "raw_thinking", "text": ""}),
        ]
    )
    assert not any(e["type"] == "assistant.thinking.delta" for e in events)


def test_build_rows_accumulate_thinking_into_message_row():
    step = {"id": "s", "runId": "r-11", "index": 1, "total": 3}
    rows = build_pipeline_transcript_rows(
        [
            _envelope("step_started", "step", 1, step=step),
            _envelope("thinking_delta", "step", 2, step=step, data={"type": "raw_thinking", "text": "先想"}),
            _envelope("thinking_delta", "step", 3, step=step, data={"type": "raw_thinking", "text": "再想"}),
            _envelope("text_delta", "step", 4, step=step, data={"text": "最终答案"}),
        ]
    )
    message_row = next(r for r in rows if r.get("id") == "pl-r-11")
    assert message_row["thinking"] == "先想再想"
    assert message_row["content"] == "最终答案"


def _ask_question_data(**overrides: Any) -> dict[str, Any]:
    data = {
        "kind": "ask_user_question",
        "inputId": "ask-call_q",
        "toolUseId": "call_q",
        "question": "选择部署地域",
        "prompt": "选择部署地域",
        "options": [
            {"id": "cn-hangzhou", "label": "华东1(杭州)"},
            {"id": "cn-beijing", "label": "华北2(北京)"},
        ],
        "allowFreeText": True,
        "freeTextPrompt": "或直接输入地域 ID",
    }
    data.update(overrides)
    return data


def test_translator_suppresses_ask_user_question_tool_card():
    # Bug 1: ask_user_question emits a tool_started envelope (from ToolUseEndEvent)
    # that never receives a result while paused, so finalizeOrphanedTool marked it
    # 「已取消」. The transcript must not render a generic tool card for it.
    step = {"id": "step1", "runId": "r-q1", "index": 1, "total": 3}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    started = translator.push(
        _envelope(
            "tool_started",
            "step",
            2,
            step=step,
            data={"toolName": "ask_user_question", "toolUseId": "call_q", "input": {"question": "选择部署地域"}},
        )
    )
    assert not any(e["type"] == "tool.started" for e in started)
    # A later tool_result for the same question also stays cardless.
    result = translator.push(
        _envelope(
            "tool_result",
            "step",
            3,
            step=step,
            data={"toolName": "ask_user_question", "toolUseId": "call_q", "result": "picked", "isError": False},
        )
    )
    assert not any(e["type"] in {"tool.started", "tool.result", "tool.finished"} for e in result)


def test_translator_emits_question_request_for_ask_user_question():
    # Bug 2: input_required(kind=ask_user_question) must surface an interactive
    # question.request carrying options + allowFreeText so the blocking panel can
    # render the selection UI (options + free text), keyed by the stable inputId.
    step = {"id": "step1", "runId": "r-q2", "index": 1, "total": 3}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    events = translator.push(
        _envelope(
            "input_required",
            "step",
            2,
            step=step,
            status="input_required",
            input={"inputId": "ask-call_q", "required": True},
            data=_ask_question_data(),
        )
    )
    request = next(e for e in events if e["type"] == "question.request")
    assert request["payload"]["requestId"] == "ask-call_q"
    payload = request["payload"]["payload"]
    assert payload["pipeline"] is True
    assert payload["question"] == "选择部署地域"
    assert payload["allowFreeText"] is True
    assert [opt["id"] for opt in payload["options"]] == ["cn-hangzhou", "cn-beijing"]
    # The question prompt bubble is still shown inline in the transcript.
    assert any(e["type"] == "assistant.text.delta" for e in events)


def test_translator_resolves_question_on_input_received():
    step = {"id": "step1", "runId": "r-q3", "index": 1, "total": 3}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    translator.push(
        _envelope(
            "input_required",
            "step",
            2,
            step=step,
            status="input_required",
            input={"inputId": "ask-call_q", "required": True},
            data=_ask_question_data(),
        )
    )
    received = translator.push(
        _envelope(
            "input_received",
            "step",
            3,
            step=step,
            data=_ask_question_data(),
        )
    )
    resolved = next(e for e in received if e["type"] == "question.resolved")
    assert resolved["payload"]["requestId"] == "ask-call_q"


def test_translator_renders_answered_ask_user_question_as_tool_card():
    # Image #44: once the interactive panel is resolved away, an answered
    # ask_user_question collapsed to just its prompt bubble — the tool call
    # itself was invisible on reload. It must instead render as a completed
    # tool card (question + options as input, the chosen option as result).
    # The journal carries no tool_started/tool_result for ask_user_question,
    # so the card is synthesized at input_received from the question stashed
    # at input_required plus this envelope's answer summary.
    step = {"id": "step1", "runId": "r-q5", "index": 1, "total": 3}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    required = translator.push(
        _envelope(
            "input_required",
            "step",
            2,
            step=step,
            status="input_required",
            input={"inputId": "ask-call_q", "required": True},
            data=_ask_question_data(),
        )
    )
    prompt = next(e for e in required if e["type"] == "assistant.text.delta")
    prompt_message_id = prompt["payload"]["messageId"]

    received = translator.push(
        _envelope(
            "input_received",
            "step",
            3,
            step=step,
            data={
                "kind": "ask_user_question",
                "inputId": "ask-call_q",
                "toolUseId": "call_q",
                "selectedId": "cn-hangzhou",
                "selectedLabel": "华东1(杭州)",
                "freeTextLength": 0,
            },
        )
    )
    # Panel still resolves away, AND a completed tool card is emitted.
    assert any(e["type"] == "question.resolved" for e in received)
    started = next(e for e in received if e["type"] == "tool.started")
    assert started["payload"]["toolName"] == "ask_user_question"
    assert started["payload"]["toolUseId"] == "call_q"
    # Card attaches to the same message as the prompt bubble (reads as
    # "assistant asked X" followed by the tool card, like normal chat).
    assert started["payload"]["messageId"] == prompt_message_id
    input_delta = next(e for e in received if e["type"] == "tool.input.delta")
    assert "选择部署地域" in input_delta["payload"]["delta"]
    assert "华东1(杭州)" in input_delta["payload"]["delta"]
    result = next(e for e in received if e["type"] == "tool.result")
    assert result["payload"]["content"] == "华东1(杭州)"
    assert result["payload"]["isError"] is False
    finished = next(e for e in received if e["type"] == "tool.finished")
    assert finished["payload"]["status"] == "completed"


def test_translator_ask_user_question_card_free_text_answer():
    # Free-text answers record only a length in the journal (the text lives in
    # the woven answer bubble), so the card still renders with the question as
    # input and an empty result body — never crashing on the missing label.
    step = {"id": "step1", "runId": "r-q6", "index": 1, "total": 3}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    translator.push(
        _envelope(
            "input_required",
            "step",
            2,
            step=step,
            status="input_required",
            input={"inputId": "ask-call_q", "required": True},
            data=_ask_question_data(),
        )
    )
    received = translator.push(
        _envelope(
            "input_received",
            "step",
            3,
            step=step,
            data={
                "kind": "ask_user_question",
                "inputId": "ask-call_q",
                "toolUseId": "call_q",
                "selectedId": "",
                "selectedLabel": "",
                "freeTextLength": 29,
            },
        )
    )
    started = next(e for e in received if e["type"] == "tool.started")
    assert started["payload"]["toolName"] == "ask_user_question"
    result = next(e for e in received if e["type"] == "tool.result")
    assert result["payload"]["content"] == ""
    finished = next(e for e in received if e["type"] == "tool.finished")
    assert finished["payload"]["status"] == "completed"


def test_translator_ask_card_reads_question_from_input_received_on_resume():
    # Live resume boundary (session 54411…): answering an ask_user_question
    # starts a *fresh* pipeline action run, hence a fresh translator that never
    # saw the paused run's input_required — so the in-memory question stash is
    # empty. The input_received envelope is therefore self-contained (it echoes
    # question/options/allowFreeText from the input_required it answers), so the
    # synthesized card still shows the real question + options instead of
    # collapsing to {"question": ""} with no options.
    step = {"id": "step1", "runId": "r-q7", "index": 1, "total": 3}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    received = translator.push(
        _envelope(
            "input_received",
            "step",
            2,
            step=step,
            data=_ask_question_data(
                selectedId="cn-hangzhou",
                selectedLabel="华东1(杭州)",
                answerTextLength=6,
                freeTextLength=0,
            ),
        )
    )
    started = next(e for e in received if e["type"] == "tool.started")
    assert started["payload"]["toolName"] == "ask_user_question"
    input_delta = next(e for e in received if e["type"] == "tool.input.delta")
    assert "选择部署地域" in input_delta["payload"]["delta"]
    assert "华东1(杭州)" in input_delta["payload"]["delta"]  # option label carried through
    result = next(e for e in received if e["type"] == "tool.result")
    assert result["payload"]["content"] == "华东1(杭州)"


def test_translator_ignores_non_ask_input_required():
    # confirm_and_select has its own inline candidate selector; input_required for
    # anything other than ask_user_question must NOT create a bottom question panel.
    step = {"id": "confirm_and_select", "runId": "r-q4", "index": 2, "total": 3}
    translator = PipelineTranscriptTranslator()
    translator.push(_envelope("step_started", "step", 1, step=step))
    events = translator.push(
        _envelope(
            "input_required",
            "step",
            2,
            step=step,
            status="input_required",
            data={"kind": "confirm_and_select", "prompt": "请选择要部署的方案", "options": []},
        )
    )
    assert not any(e["type"] == "question.request" for e in events)


def test_context_usage_envelope_emits_step_context_event():
    translator = PipelineTranscriptTranslator()
    env = _envelope(
        "context_usage",
        "step",
        1,
        step={"id": "step1", "runId": "step-step1-1", "attempt": 1},
        data={"totalTokens": 1500, "contextWindow": 60000, "usagePercent": 2.5},
    )
    events = translator.push(env)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert events[0]["type"] == "pipeline.step.context"
    assert payload["groupId"] == "step:step-step1-1"
    assert payload["level"] == "step"
    assert payload["contextUsage"]["totalTokens"] == 1500


def test_candidate_step_context_usage_uses_candidate_step_group():
    translator = PipelineTranscriptTranslator()
    env = _envelope(
        "context_usage",
        "candidate_step",
        1,
        candidate={"id": "c0", "runId": "cand-0", "name": "Plan A"},
        candidateStep={"id": "gen", "runId": "cand-0-gen-1", "attempt": 1},
        data={"totalTokens": 800, "contextWindow": 60000},
    )
    events = translator.push(env)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["groupId"] == "candidate-step:cand-0-gen-1"
    assert payload["level"] == "sub_step"
    assert payload["candidateName"] == "Plan A"
