import json

from iac_code.pipeline.engine.display_replay import (
    PipelineDisplayRecorder,
    PipelineDisplayReducer,
    load_display_events,
)


def test_recorder_and_reducer_preserve_repeated_attempts_after_rollback(tmp_path):
    path = tmp_path / "display.jsonl"
    recorder = PipelineDisplayRecorder(path)

    recorder.record("pipeline_started", pipeline_name="selling", payload={"step_names": ["intent", "plan"]})
    recorder.record("step_started", step_id="intent", payload={"index": 1, "total": 2})
    recorder.record("step_completed", step_id="intent")
    recorder.record("step_started", step_id="plan", payload={"index": 2, "total": 2})
    recorder.record("rollback_triggered", step_id="plan", payload={"from_step": "plan", "to_step": "intent"})
    recorder.record("step_started", step_id="intent", payload={"index": 1, "total": 2})
    recorder.record("pipeline_user_aborted")

    events = load_display_events(path)
    model = PipelineDisplayReducer().reduce(events)

    assert model.pipeline_name == "selling"
    assert [attempt.step_id for attempt in model.attempts] == ["intent", "plan", "intent"]
    assert [attempt.attempt_no for attempt in model.attempts] == [1, 1, 2]
    assert model.attempts[0].status == "completed"
    assert model.attempts[1].status == "rolled_back"
    assert model.attempts[1].rollback_to == "intent"
    assert model.attempts[2].status == "interrupted"
    assert model.interrupted is True


def test_display_replay_ignores_pipeline_warning_without_terminal_change(tmp_path) -> None:
    path = tmp_path / "display.jsonl"
    recorder = PipelineDisplayRecorder(path)

    recorder.record("pipeline_started", pipeline_name="selling", timestamp=1.0)
    recorder.record("step_started", step_id="deploying", payload={"index": 1, "total": 1}, timestamp=1.5)
    recorder.record(
        "pipeline_warning",
        step_id="deploying",
        payload={"reason": "cleanup_tracking_unavailable"},
        timestamp=2.0,
    )

    model = PipelineDisplayReducer().reduce(load_display_events(path))

    assert model.interrupted is False
    assert model.failed is False
    assert model.attempts[-1].status == "running"


def test_reducer_attaches_transcript_ids_from_event_payload_and_attempt_metadata(tmp_path):
    path = tmp_path / "display.jsonl"
    recorder = PipelineDisplayRecorder(path)

    recorder.record(
        "step_started",
        step_id="intent",
        payload={"active_attempt_id": "att_0001", "transcript_id": "transcript_att_0001"},
    )
    recorder.record("step_completed", step_id="intent")
    recorder.record("step_started", step_id="plan")

    model = PipelineDisplayReducer().reduce(
        load_display_events(path),
        {
            "items": {
                "att_0001": {
                    "attempt_id": "att_0001",
                    "scope": "parent",
                    "step_id": "intent",
                    "transcript_id": "transcript_att_0001",
                },
                "att_0002": {
                    "attempt_id": "att_0002",
                    "scope": "parent",
                    "step_id": "plan",
                    "transcript_id": "transcript_att_0002",
                },
            }
        },
    )

    assert [(attempt.attempt_id, attempt.transcript_id) for attempt in model.attempts] == [
        ("att_0001", "transcript_att_0001"),
        ("att_0002", "transcript_att_0002"),
    ]


def test_reducer_tracks_parallel_sub_pipeline_intermediate_and_completed_states(tmp_path):
    path = tmp_path / "display.jsonl"
    recorder = PipelineDisplayRecorder(path)

    recorder.record("pipeline_started", pipeline_name="selling")
    recorder.record(
        "step_started",
        step_id="evaluate_candidates",
        payload={"index": 3, "total": 5, "step_type": "parallel_sub_pipeline"},
    )
    recorder.record(
        "sub_pipeline_started",
        step_id="evaluate_candidates",
        payload={
            "sub_pipeline_id": "candidate_0",
            "candidate_index": 0,
            "candidate_name": "低成本方案",
            "sub_pipeline_name": "evaluate_candidate",
            "total_steps": 2,
        },
    )
    recorder.record(
        "sub_step_started",
        step_id="template_generating",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "template_generating", "step_index": 0},
    )
    recorder.record(
        "sub_step_completed",
        step_id="template_generating",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "template_generating"},
    )
    recorder.record(
        "sub_step_started",
        step_id="reviewing",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "reviewing", "step_index": 1},
    )
    recorder.record(
        "sub_step_completed",
        step_id="reviewing",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "reviewing"},
    )
    recorder.record(
        "sub_step_started",
        step_id="template_generating",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "template_generating", "step_index": 0},
    )

    intermediate = PipelineDisplayReducer().reduce(load_display_events(path))
    attempt = intermediate.attempts[-1]
    assert attempt.status == "running"
    sub = attempt.sub_pipelines["candidate_0"]
    assert sub.status == "running"
    assert [(step.step_id, step.attempt_no, step.status) for step in sub.steps] == [
        ("template_generating", 1, "completed"),
        ("reviewing", 1, "completed"),
        ("template_generating", 2, "running"),
    ]

    recorder.record(
        "sub_step_completed",
        step_id="template_generating",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "template_generating"},
    )
    recorder.record(
        "sub_pipeline_completed",
        step_id="evaluate_candidates",
        payload={"sub_pipeline_id": "candidate_0", "failed": False},
    )
    recorder.record("step_completed", step_id="evaluate_candidates", payload={"candidates_count": 1})

    completed = PipelineDisplayReducer().reduce(load_display_events(path))
    attempt = completed.attempts[-1]
    assert attempt.status == "completed"
    assert attempt.sub_pipelines["candidate_0"].status == "completed"
    assert attempt.summary == "1 candidates"


def test_reducer_attaches_sub_step_transcript_ids_from_attempt_metadata(tmp_path):
    path = tmp_path / "display.jsonl"
    recorder = PipelineDisplayRecorder(path)

    recorder.record("step_started", step_id="evaluate_candidates", payload={"step_type": "parallel_sub_pipeline"})
    recorder.record(
        "sub_pipeline_started",
        step_id="evaluate_candidates",
        payload={"sub_pipeline_id": "candidate_0"},
    )
    recorder.record(
        "sub_step_started",
        step_id="template_generating",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "template_generating"},
    )

    model = PipelineDisplayReducer().reduce(
        load_display_events(path),
        {
            "items": {
                "att_0001": {
                    "attempt_id": "att_0001",
                    "scope": "parent",
                    "step_id": "evaluate_candidates",
                    "transcript_id": "transcript_att_0001",
                },
                "att_0002": {
                    "attempt_id": "att_0002",
                    "scope": "sub_step",
                    "parent_step_id": "evaluate_candidates",
                    "sub_pipeline_id": "candidate_0",
                    "sub_step_id": "template_generating",
                    "transcript_id": "transcript_att_0002",
                },
            }
        },
    )

    sub_step = model.attempts[-1].sub_pipelines["candidate_0"].steps[0]
    assert sub_step.attempt_id == "att_0002"
    assert sub_step.transcript_id == "transcript_att_0002"


def test_reducer_marks_running_sub_step_failed_when_sub_pipeline_fails(tmp_path):
    path = tmp_path / "display.jsonl"
    recorder = PipelineDisplayRecorder(path)

    recorder.record("step_started", step_id="evaluate_candidates", payload={"step_type": "parallel_sub_pipeline"})
    recorder.record(
        "sub_pipeline_started",
        step_id="evaluate_candidates",
        payload={"sub_pipeline_id": "candidate_0", "candidate_name": "低成本方案"},
    )
    recorder.record(
        "sub_step_started",
        step_id="template_generating",
        payload={"sub_pipeline_id": "candidate_0", "step_id": "template_generating"},
    )
    recorder.record(
        "sub_pipeline_completed",
        step_id="evaluate_candidates",
        payload={"sub_pipeline_id": "candidate_0", "failed": True, "error": "template failed"},
    )

    sub = PipelineDisplayReducer().reduce(load_display_events(path)).attempts[-1].sub_pipelines["candidate_0"]

    assert sub.status == "failed"
    assert [(step.step_id, step.status, step.error) for step in sub.steps] == [
        ("template_generating", "failed", "template failed")
    ]


def test_reducer_tracks_candidate_selection_phases(tmp_path):
    path = tmp_path / "display.jsonl"
    recorder = PipelineDisplayRecorder(path)

    recorder.record(
        "step_started",
        step_id="confirm_and_select",
        payload={"index": 4, "total": 5, "ui_mode": "candidate_selection"},
    )
    recorder.record(
        "candidate_diagram",
        step_id="confirm_and_select",
        payload={"candidate_name": "低成本方案", "candidate_index": 0, "mermaid_source": "graph TD; A-->B"},
    )
    recorder.record(
        "candidate_detail",
        step_id="confirm_and_select",
        payload={
            "candidate_name": "低成本方案",
            "candidate_index": 0,
            "summary": "单 ECS Nginx",
            "cost_items": [{"name": "ECS", "monthly_cost": "¥30/月"}],
            "total_monthly_cost": "¥30/月",
        },
    )

    preparing = PipelineDisplayReducer().reduce(load_display_events(path)).attempts[-1].candidate_selection
    assert preparing.state == "preparing"
    assert preparing.candidates[0].summary == "单 ECS Nginx"

    options = [{"name": "低成本方案", "summary": "单 ECS Nginx", "candidate_index": 0}]
    recorder.record(
        "candidate_selection_ready",
        step_id="confirm_and_select",
        payload={"prompt": "请选择", "options": options},
    )
    waiting = PipelineDisplayReducer().reduce(load_display_events(path)).attempts[-1].candidate_selection
    assert waiting.state == "waiting"
    assert waiting.options == options

    recorder.record(
        "candidate_selected",
        step_id="confirm_and_select",
        payload={"candidate_name": "低成本方案", "candidate_index": 0},
    )
    selected = PipelineDisplayReducer().reduce(load_display_events(path)).attempts[-1].candidate_selection
    assert selected.state == "selected"
    assert selected.selected_name == "低成本方案"

    recorder.record("step_completed", step_id="confirm_and_select")
    completed = PipelineDisplayReducer().reduce(load_display_events(path)).attempts[-1].candidate_selection
    assert completed.state == "completed"
    assert completed.selected_name == "低成本方案"


def test_load_display_events_skips_invalid_trailing_jsonl(tmp_path):
    path = tmp_path / "display.jsonl"
    path.write_text(
        json.dumps({"version": 1, "type": "pipeline_started", "pipeline_name": "selling"}) + "\n{broken",
        encoding="utf-8",
    )

    events = load_display_events(path)

    assert len(events) == 1
    assert events[0]["type"] == "pipeline_started"
