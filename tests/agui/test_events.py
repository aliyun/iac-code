from __future__ import annotations

import json

from ag_ui.core import EventType

from iac_code.agui.events import (
    A2AEventMapper,
    a2a_iac_code_session_id,
    a2a_inputs,
    a2a_sideband_input_ids,
    interrupt_from_a2a,
)


def test_reasoning_is_mapped_from_a2a_event_without_a_second_agui_gate() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")

    mapped = mapper.map(
        {"result": {"metadata": {"iac_code": {"thinking": {"type": "raw_thinking", "text": "reasoning"}}}}}
    )

    assert [event.type for event in mapped] == [
        EventType.REASONING_START,
        EventType.REASONING_MESSAGE_START,
        EventType.REASONING_MESSAGE_CONTENT,
    ]
    assert mapped[-1].delta == "reasoning"


def test_a2a_usage_accepts_integer_valued_protobuf_struct_numbers() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")

    mapper.map(
        {
            "result": {
                "metadata": {
                    "iac_code": {
                        "usage": {
                            "inputTokens": 6497.0,
                            "outputTokens": 26.0,
                            "totalTokens": 6523.0,
                            "cachedInputTokens": 0.0,
                        }
                    }
                }
            }
        }
    )

    assert len(mapper.usage) == 1
    assert mapper.usage[0].input_tokens == 6497
    assert mapper.usage[0].output_tokens == 26
    assert mapper.usage[0].total_tokens == 6523
    assert mapper.usage[0].cached_input_tokens == 0


def test_pipeline_usage_envelope_is_aggregated_for_run_usage() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")

    mapped = mapper.map(
        {
            "result": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventId": "usage-1",
                            "eventType": "usage",
                            "data": {
                                "provider": "dashscope",
                                "model": "qwen-test",
                                "inputTokens": 12,
                                "outputTokens": 3,
                                "totalTokens": 15,
                                "cachedInputTokens": 2,
                            },
                        }
                    }
                }
            }
        }
    )

    assert len(mapper.usage) == 1
    assert mapper.usage[0].provider == "dashscope"
    assert mapper.usage[0].model == "qwen-test"
    assert mapper.usage[0].total_tokens == 15
    assert all(event.type != EventType.CUSTOM for event in mapped)


def test_pipeline_custom_projection_suppresses_standard_and_internal_events() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    envelopes = [
        {"eventType": "message_started", "data": {"messageId": "message-1"}},
        {"eventType": "context_usage", "data": {"currentTokens": 10}},
        {
            "eventType": "tool_started",
            "data": {"toolUseId": "tool-1", "toolName": "bash", "input": {"cmd": "pwd"}},
        },
        {
            "eventType": "tool_result",
            "data": {"toolUseId": "tool-1", "toolName": "bash", "result": "ok"},
        },
        {"eventType": "step_started", "step": {"id": "prepare"}},
        {"eventType": "step_completed", "step": {"id": "prepare"}},
        {"eventType": "permission_requested", "data": {"toolUseId": "tool-2"}},
        {"eventType": "backup_committed", "data": {"committedSequence": 1}},
    ]

    mapped = []
    for sequence, envelope in enumerate(envelopes, start=1):
        envelope.update({"eventId": f"event-{sequence}", "sequence": sequence})
        mapped.extend(mapper.map({"result": {"metadata": {"iac_code": {"pipeline": envelope}}}}))

    assert all(event.type != EventType.CUSTOM for event in mapped)
    assert EventType.TOOL_CALL_START in {event.type for event in mapped}
    assert EventType.TOOL_CALL_RESULT in {event.type for event in mapped}
    assert EventType.STEP_STARTED in {event.type for event in mapped}
    assert EventType.STEP_FINISHED in {event.type for event in mapped}


def test_pipeline_custom_projection_keeps_ui_semantic_extensions() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    event_types = [
        "pipeline_started",
        "pipeline_completed",
        "candidate_started",
        "candidate_detail_shown",
        "stack_progress",
        "pipeline_warning",
        "candidate_step_failed",
    ]

    mapped = []
    for sequence, event_type in enumerate(event_types, start=1):
        envelope = {
            "eventId": f"event-{sequence}",
            "eventType": event_type,
            "sequence": sequence,
            "data": {"summary": event_type},
        }
        mapped.extend(mapper.map({"result": {"metadata": {"iac_code": {"pipeline": envelope}}}}))

    assert [event.value["eventType"] for event in mapped if event.type == EventType.CUSTOM] == event_types


def test_iac_code_session_id_is_read_from_pipeline_batch_envelopes() -> None:
    assert (
        a2a_iac_code_session_id(
            {
                "result": {
                    "metadata": {
                        "iac_code": {
                            "pipelineBatch": {
                                "events": [{"eventType": "step_started", "iacCodeSessionId": "session-1"}]
                            }
                        }
                    }
                }
            }
        )
        == "session-1"
    )


def test_a2a_tool_metadata_maps_to_standard_tool_events() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    started = mapper.map(
        {
            "result": {
                "taskId": "task-1",
                "contextId": "context-1",
                "status": {"state": "TASK_STATE_WORKING"},
                "metadata": {"iac_code": {"tool": {"status": "started", "toolUseId": "tool-1", "name": "bash"}}},
            }
        }
    )
    completed = mapper.map(
        {
            "result": {
                "taskId": "task-1",
                "contextId": "context-1",
                "status": {"state": "TASK_STATE_WORKING"},
                "metadata": {
                    "iac_code": {
                        "tool": {
                            "status": "input_complete",
                            "toolUseId": "tool-1",
                            "name": "bash",
                            "toolInput": {"command": "pwd", "token": "[REDACTED]"},
                        }
                    }
                },
            }
        }
    )

    assert started[0].type == EventType.TOOL_CALL_START
    assert [event.type for event in completed] == [EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END]
    assert json.loads(completed[0].delta) == {"command": "pwd", "token": "[REDACTED]"}


def test_permission_metadata_becomes_self_describing_standard_interrupt() -> None:
    interrupt = interrupt_from_a2a(
        {
            "schemaVersion": 1,
            "kind": "permission",
            "inputId": "permission-1",
            "toolUseId": "tool-1",
            "toolName": "aliyun_api",
            "title": "Create ROS stack",
            "purpose": "Create the requested infrastructure.",
            "effect": "cloud_change",
            "target": "ROS CreateStack in cn-hangzhou",
            "prompt": "Create ROS stack. Allow once?",
            "safeSummary": "Create stack demo",
            "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
            "required": True,
        },
    )

    assert interrupt.id == "permission-1"
    assert interrupt.message == "Create ROS stack. Allow once?"
    assert interrupt.response_schema["properties"]["decision"]["enum"] == ["allow_once", "deny"]
    assert interrupt.metadata["purpose"] == "Create the requested infrastructure."


def test_interrupt_fallback_messages_use_projection_language(monkeypatch) -> None:
    monkeypatch.setattr(
        "iac_code.agui.events.translate_message",
        lambda message, *, language: f"{language}:{message}",
    )

    permission = interrupt_from_a2a(
        {"kind": "permission", "inputId": "permission-1", "language": "zh-CN"},
    )
    question = interrupt_from_a2a(
        {"kind": "ask_user_question", "inputId": "question-1", "language": "ja-JP"},
    )

    assert permission.message == "zh:Permission required"
    assert question.message == "ja:Input required"


def test_interleaved_pipeline_tools_and_steps_remain_balanced() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    envelopes = [
        {"eventId": "step-a-start", "eventType": "step_started", "step": {"id": "step-a"}},
        {"eventId": "step-b-start", "eventType": "step_started", "step": {"id": "step-b"}},
        {"eventId": "step-b-end", "eventType": "step_completed", "step": {"id": "step-b"}},
        {"eventId": "step-a-end", "eventType": "step_completed", "step": {"id": "step-a"}},
    ]

    mapped = []
    for envelope in envelopes:
        mapped.extend(
            mapper.map(
                {
                    "result": {
                        "metadata": {"iac_code": {"pipeline": envelope}},
                    }
                }
            )
        )

    assert [event.step_name for event in mapped if event.type == EventType.STEP_STARTED] == ["step-a", "step-b"]
    assert [event.step_name for event in mapped if event.type == EventType.STEP_FINISHED] == ["step-b", "step-a"]
    snapshots = [event for event in mapped if event.type == EventType.ACTIVITY_SNAPSHOT]
    assert snapshots == []


def test_close_all_finishes_run_steps_without_losing_durable_pipeline_state() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    mapped = mapper.map(
        {
            "result": {
                "metadata": {
                    "iac_code": {
                        "pipelineBatch": {
                            "events": [
                                {
                                    "eventId": "parent-start",
                                    "eventType": "step_started",
                                    "step": {"id": "evaluate_candidates"},
                                },
                                {
                                    "eventId": "candidate-start",
                                    "eventType": "candidate_step_started",
                                    "candidate": {"runId": "candidate-0"},
                                    "candidateStep": {"id": "template_generating"},
                                },
                            ]
                        }
                    }
                }
            }
        }
    )

    closing = mapper.close_all()

    assert [event.step_name for event in mapped if event.type == EventType.STEP_STARTED] == [
        "evaluate_candidates",
        "candidate:candidate-0:template_generating",
    ]
    assert [event.step_name for event in closing if event.type == EventType.STEP_FINISHED] == [
        "candidate:candidate-0:template_generating",
        "evaluate_candidates",
    ]
    assert mapper.open_pipeline_steps == {
        "step:evaluate_candidates",
        "candidate:candidate-0:template_generating",
    }
    assert mapper.close_all() == []


def test_resume_reopens_durable_pipeline_steps_and_finishes_them_in_the_new_run() -> None:
    mapper = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-2",
        open_pipeline_steps={
            "step:evaluate_candidates",
            "candidate:candidate-0:template_generating",
        },
    )

    reopened = mapper.reopen_pipeline_steps()
    completed = mapper.map(
        {
            "result": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventId": "candidate-complete",
                            "eventType": "candidate_step_completed",
                            "candidate": {"runId": "candidate-0"},
                            "candidateStep": {"id": "template_generating"},
                        }
                    }
                }
            }
        }
    )
    closing = mapper.close_all()

    assert [event.step_name for event in reopened] == [
        "candidate:candidate-0:template_generating",
        "evaluate_candidates",
    ]
    assert [event.step_name for event in completed if event.type == EventType.STEP_FINISHED] == [
        "candidate:candidate-0:template_generating"
    ]
    assert [event.step_name for event in closing if event.type == EventType.STEP_FINISHED] == ["evaluate_candidates"]
    assert mapper.open_pipeline_steps == {"step:evaluate_candidates"}


def test_parallel_candidate_steps_use_unique_agui_step_names() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    mapped = []
    for candidate in ("candidate-0", "candidate-1"):
        mapped.extend(
            mapper.map(
                {
                    "result": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventId": f"{candidate}-start",
                                    "eventType": "candidate_step_started",
                                    "candidate": {"runId": candidate},
                                    "candidateStep": {"id": "template_generating"},
                                }
                            }
                        }
                    }
                }
            )
        )

    names = [event.step_name for event in mapped if event.type == EventType.STEP_STARTED]
    assert names == [
        "candidate:candidate-0:template_generating",
        "candidate:candidate-1:template_generating",
    ]
    assert len(names) == len(set(names))


def test_interleaved_pipeline_text_keeps_one_message_span_per_candidate() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    candidates = ("candidate-a", "candidate-b", "candidate-a", "candidate-b")

    mapped = []
    for sequence, candidate in enumerate(candidates, start=1):
        mapped.extend(
            mapper.map(
                {
                    "result": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventId": f"text-{sequence}",
                                    "eventType": "text_delta",
                                    "sequence": sequence,
                                    "scope": "candidate_step",
                                    "candidateStep": {"runId": candidate},
                                    "data": {"text": f"delta-{sequence}"},
                                }
                            }
                        }
                    }
                }
            )
        )
    mapped.extend(mapper.close_all())

    starts = [event.message_id for event in mapped if event.type == EventType.TEXT_MESSAGE_START]
    contents = [event.message_id for event in mapped if event.type == EventType.TEXT_MESSAGE_CONTENT]
    ends = [event.message_id for event in mapped if event.type == EventType.TEXT_MESSAGE_END]
    assert len(starts) == 2
    assert len(contents) == 4
    assert sorted(starts) == sorted(ends)
    assert all(event.type != EventType.CUSTOM for event in mapped)


def test_parallel_candidate_text_uses_candidate_run_identity_before_shared_step_id() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    mapped = []
    for sequence, candidate in enumerate(("candidate-a", "candidate-b"), start=1):
        mapped.extend(
            mapper.map(
                {
                    "result": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventId": f"text-{sequence}",
                                    "eventType": "text_delta",
                                    "sequence": sequence,
                                    "scope": "candidate_step",
                                    "candidate": {"runId": candidate},
                                    "candidateStep": {"id": "template_generating"},
                                    "data": {"text": candidate},
                                }
                            }
                        }
                    }
                }
            )
        )

    message_ids = [event.message_id for event in mapped if event.type == EventType.TEXT_MESSAGE_START]
    assert len(message_ids) == 2
    assert len(set(message_ids)) == 2
    assert "candidate-a:template_generating" in message_ids[0]
    assert "candidate-b:template_generating" in message_ids[1]


def test_pipeline_thinking_maps_to_standard_reasoning_without_custom_event() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    mapped = []
    for sequence, candidate in enumerate(("candidate-a", "candidate-b"), start=1):
        mapped.extend(
            mapper.map(
                {
                    "result": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventId": f"thinking-{sequence}",
                                    "eventType": "thinking_delta",
                                    "sequence": sequence,
                                    "scope": "candidate_step",
                                    "candidate": {"runId": candidate},
                                    "candidateStep": {"id": "template_generating"},
                                    "data": {"type": "raw_thinking", "text": candidate},
                                }
                            }
                        }
                    }
                }
            )
        )

    contents = [event for event in mapped if event.type == EventType.REASONING_MESSAGE_CONTENT]
    assert [event.delta for event in contents] == ["candidate-a", "candidate-b"]
    assert len({event.message_id for event in contents}) == 2
    assert all(event.type != EventType.CUSTOM for event in mapped)


def test_input_received_reopens_a_step_closed_for_interactive_selection() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    envelopes = [
        {"eventId": "start", "eventType": "step_started", "step": {"id": "confirm_and_select"}},
        {"eventId": "pause", "eventType": "step_completed", "step": {"id": "confirm_and_select"}},
        {"eventId": "answer", "eventType": "input_received", "step": {"id": "confirm_and_select"}},
        {"eventId": "done", "eventType": "step_completed", "step": {"id": "confirm_and_select"}},
    ]

    mapped = []
    for envelope in envelopes:
        mapped.extend(mapper.map({"result": {"metadata": {"iac_code": {"pipeline": envelope}}}}))

    assert sum(event.type == EventType.STEP_STARTED for event in mapped) == 2
    assert sum(event.type == EventType.STEP_FINISHED for event in mapped) == 2


def test_unknown_pipeline_events_are_not_forwarded_as_custom() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")

    mapped = []
    for sequence in range(500):
        mapped.extend(
            mapper.map(
                {
                    "result": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventId": f"progress-{sequence}",
                                    "eventType": "pipeline_progress",
                                    "sequence": sequence,
                                }
                            }
                        }
                    }
                }
            )
        )

    assert mapped == []


def test_repeated_pipeline_batch_event_id_is_mapped_once() -> None:
    mapper = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-1",
        open_pipeline_steps={"step:candidate-a"},
    )
    payload = {
        "result": {
            "metadata": {
                "iac_code": {
                    "pipelineBatch": {
                        "events": [
                            {
                                "eventId": "step-1",
                                "eventType": "candidate_step_completed",
                                "candidateStep": {"id": "candidate-a"},
                            }
                        ]
                    }
                }
            }
        }
    }

    mapped = [*mapper.map(payload), *mapper.map(payload)]

    assert sum(event.type == EventType.STEP_FINISHED for event in mapped) == 1
    assert sum(event.type == EventType.CUSTOM for event in mapped) == 0


def test_task_snapshot_exposes_all_pending_pipeline_permissions_once() -> None:
    first = {"kind": "permission", "inputId": "permission-1", "required": True}
    second = {"kind": "permission", "inputId": "permission-2", "required": True}
    event = {
        "result": {
            "metadata": {
                "iac_code": {
                    "input": first,
                    "pendingPermissions": [first, second],
                }
            }
        }
    }

    assert [value["inputId"] for value in a2a_inputs(event)] == ["permission-1", "permission-2"]
    assert a2a_sideband_input_ids(event) == {"permission-1", "permission-2"}


def test_direct_candidate_permission_is_recognized_as_sideband() -> None:
    event = {
        "result": {
            "metadata": {
                "iac_code": {
                    "input": {
                        "kind": "permission",
                        "inputId": "permission-1",
                        "scope": "candidate",
                        "subPipelineId": "candidate-a",
                        "required": True,
                    }
                }
            }
        }
    }

    assert a2a_sideband_input_ids(event) == {"permission-1"}


def test_pipeline_recovery_emits_one_replace_snapshot_and_deduplicated_steps() -> None:
    mapper = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-2",
        open_pipeline_steps={"step:candidate-b"},
    )
    event = {
        "eventId": "candidate-b-complete",
        "eventType": "candidate_step_completed",
        "sequence": 8,
        "candidateStep": {"id": "candidate-b"},
    }

    mapped = mapper.map_pipeline_recovery(
        {
            "snapshot": {
                "schemaVersion": "1.0",
                "pipelineRunId": "pipeline-1",
                "lastSequence": 8,
                "display": {"messages": [{"text": "candidate B continued"}]},
            },
            "events": [event, event],
        }
    )

    snapshots = [item for item in mapped if item.type == EventType.ACTIVITY_SNAPSHOT]
    finished = [item for item in mapped if item.type == EventType.STEP_FINISHED]
    customs = [item for item in mapped if item.type == EventType.CUSTOM]
    assert len(snapshots) == 1
    assert snapshots[0].replace is True
    assert len(finished) == 1
    assert finished[0].step_name == "candidate-b"
    assert len(customs) == 0
    assert mapper.last_pipeline_sequence == 8


def test_pipeline_recovery_maps_post_disconnect_text_and_tool_to_standard_events() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-2")

    mapped = mapper.map_pipeline_recovery(
        {
            "snapshot": {"schemaVersion": "1.0", "pipelineRunId": "pipeline-1", "lastSequence": 4},
            "events": [
                {
                    "eventId": "candidate-text",
                    "eventType": "text_delta",
                    "sequence": 2,
                    "scope": "candidate",
                    "candidate": {"runId": "candidate-b"},
                    "data": {"text": "candidate B completed"},
                },
                {
                    "eventId": "tool-started",
                    "eventType": "tool_started",
                    "sequence": 3,
                    "data": {"toolUseId": "tool-1", "toolName": "bash", "input": {"cmd": "pwd"}},
                },
                {
                    "eventId": "tool-result",
                    "eventType": "tool_result",
                    "sequence": 4,
                    "data": {"toolUseId": "tool-1", "toolName": "bash", "result": "ok", "isError": False},
                },
            ],
        }
    )

    event_types = [event.type for event in mapped]
    assert EventType.TEXT_MESSAGE_CONTENT in event_types
    assert EventType.TOOL_CALL_START in event_types
    assert EventType.TOOL_CALL_ARGS in event_types
    assert EventType.TOOL_CALL_END in event_types
    assert EventType.TOOL_CALL_RESULT in event_types
    assert all(event.type != EventType.CUSTOM for event in mapped)


def test_pipeline_recovery_maps_thinking_without_replaying_custom_event() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-2")

    mapped = mapper.map_pipeline_recovery(
        {
            "snapshot": {"pipelineRunId": "pipeline-1", "lastSequence": 1},
            "events": [
                {
                    "eventId": "thinking-1",
                    "eventType": "thinking_delta",
                    "sequence": 1,
                    "data": {"type": "raw_thinking", "text": "recovered reasoning"},
                }
            ],
        }
    )

    assert [event.delta for event in mapped if event.type == EventType.REASONING_MESSAGE_CONTENT] == [
        "recovered reasoning"
    ]
    assert all(
        not (event.type == EventType.CUSTOM and event.value.get("eventType") == "thinking_delta") for event in mapped
    )


def test_mapper_suppresses_task_pipeline_suffix_before_authoritative_recovery() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-2")
    payload = {
        "result": {
            "metadata": {
                "iac_code": {
                    "pipeline": {
                        "eventId": "old-step-finished",
                        "eventType": "step_completed",
                        "sequence": 7,
                        "step": {"id": "confirm_and_select"},
                    }
                }
            }
        }
    }

    assert mapper.map(payload, include_pipeline=False) == []
    assert mapper.last_pipeline_sequence == 0


def test_mapper_deduplicates_tool_results_from_snapshot_and_live_stream() -> None:
    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-2")
    payload = {
        "result": {
            "metadata": {
                "iac_code": {
                    "tool": {
                        "status": "completed",
                        "toolUseId": "tool-1",
                        "name": "aliyun_api",
                        "result": "Permission denied.",
                    }
                }
            }
        }
    }

    mapped = [*mapper.map(payload), *mapper.map(payload)]

    assert sum(event.type == EventType.TOOL_CALL_RESULT for event in mapped) == 1


def test_mapper_emits_only_new_suffix_from_cumulative_resume_text() -> None:
    def status_text(text: str) -> dict[str, object]:
        return {
            "result": {
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "message": {
                        "messageId": "assistant-1",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": text}],
                    },
                }
            }
        }

    first = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    first.map(status_text("first"))
    first.finalize_text_snapshots()

    second = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-2",
        text_snapshot_digests=first.text_snapshot_digests,
    )
    second_events = second.map(status_text("firstsecond"))
    second.finalize_text_snapshots()

    third = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-3",
        text_snapshot_digests=second.text_snapshot_digests,
    )
    third_events = third.map(status_text("firstsecondthird"))

    assert [event.delta for event in second_events if event.type == EventType.TEXT_MESSAGE_CONTENT] == ["second"]
    assert [event.delta for event in third_events if event.type == EventType.TEXT_MESSAGE_CONTENT] == ["third"]


def test_mapper_keeps_unmatched_resume_text_and_tracks_exact_replay_for_later_suffix() -> None:
    def status_text(text: str) -> dict[str, object]:
        return {
            "result": {
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "message": {
                        "messageId": "assistant-1",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": text}],
                    },
                }
            }
        }

    prior_text = "before interrupt"
    prior = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    prior.map(status_text(prior_text))
    prior.finalize_text_snapshots()

    replay = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-2",
        text_snapshot_digests=prior.text_snapshot_digests,
    )
    exact_replay = status_text(prior_text)
    exact_replay["result"]["metadata"] = {"iac_code": {"assistantFinal": {"complete": True}}}
    exact_events = replay.map(exact_replay)
    suffix_events = replay.map(status_text(" after resume"))
    replay.finalize_text_snapshots()

    unrelated = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-2",
        text_snapshot_digests=prior.text_snapshot_digests,
    )
    unrelated_events = unrelated.map(status_text("different response"))

    assert exact_events == []
    assert [event.delta for event in suffix_events if event.type == EventType.TEXT_MESSAGE_CONTENT] == [" after resume"]
    assert [event.delta for event in unrelated_events if event.type == EventType.TEXT_MESSAGE_CONTENT] == [
        "different response"
    ]
    assert replay.text_snapshot_digests != prior.text_snapshot_digests


def test_authoritative_recovery_can_fill_unseen_event_below_live_cursor() -> None:
    mapper = A2AEventMapper(
        thread_id="thread-1",
        run_id="run-2",
        open_pipeline_steps={"step:candidate-a"},
    )
    mapper.last_pipeline_sequence = 10

    mapped = mapper.map_pipeline_recovery(
        {
            "snapshot": {"pipelineRunId": "pipeline-1", "lastSequence": 12},
            "events": [
                {
                    "eventId": "missed-step-9",
                    "eventType": "candidate_step_completed",
                    "sequence": 9,
                    "candidateStep": {"id": "candidate-a"},
                }
            ],
        }
    )

    assert sum(event.type == EventType.STEP_FINISHED for event in mapped) == 1
    assert mapper.last_pipeline_sequence == 12
