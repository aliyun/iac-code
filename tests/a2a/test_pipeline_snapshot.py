from __future__ import annotations

import json
import logging
from pathlib import Path

from iac_code.a2a import pipeline_snapshot
from iac_code.a2a.pipeline_snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    A2APipelineSnapshotStore,
    reduce_pipeline_events,
)


def _base(
    event_id: str,
    sequence: int,
    event_type: str,
    *,
    scope: str = "pipeline",
    status: str = "working",
) -> dict:
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "sequence": sequence,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": event_type,
        "scope": scope,
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": status,
        "data": {},
    }


def test_snapshot_load_logs_parse_failures(tmp_path, caplog) -> None:
    store = A2APipelineSnapshotStore(tmp_path)
    store.path.write_text("{not json", encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="iac_code.a2a.pipeline_snapshot")

    assert store.load() is None
    assert "Failed to load A2A pipeline snapshot" in caplog.text
    assert str(store.path) in caplog.text


def test_snapshot_save_cleans_temp_file_when_replace_fails(monkeypatch, tmp_path, caplog) -> None:
    store = A2APipelineSnapshotStore(tmp_path)

    def fail_replace(self: Path, target: Path) -> Path:
        raise PermissionError(f"locked: {target}")

    monkeypatch.setattr(Path, "replace", fail_replace)
    caplog.set_level(logging.WARNING, logger="iac_code.a2a.pipeline_snapshot")

    assert store.save({"status": "working"}) is False
    assert not store.path.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert "Failed to persist A2A pipeline snapshot" in caplog.text


def test_reduce_steps_and_pending_input() -> None:
    started = _base("evt-1", 1, "pipeline_started")
    started["data"] = {"totalSteps": 2, "stepIds": ["intent_parsing", "confirm_and_select"]}
    step = _base("evt-2", 2, "step_started", scope="step")
    step["step"] = {
        "runId": "step-confirm_and_select-1",
        "id": "confirm_and_select",
        "index": 2,
        "total": 2,
        "attempt": 1,
    }
    waiting = _base("evt-3", 3, "input_required", scope="input", status="waiting_input")
    waiting["step"] = step["step"]
    waiting["input"] = {
        "inputId": "input-confirm_and_select-1",
        "kind": "choice",
        "prompt": "choose",
        "required": True,
        "options": [],
    }

    snapshot = reduce_pipeline_events([started, step, waiting])

    assert snapshot["status"] == "waiting_input"
    assert snapshot["lastSequence"] == 3
    assert snapshot["steps"][0]["id"] == "confirm_and_select"
    assert snapshot["pendingInput"]["inputId"] == "input-confirm_and_select-1"


def test_reduce_input_received_completes_waiting_step() -> None:
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {
        "runId": "step-confirm_and_select-1",
        "id": "confirm_and_select",
        "index": 4,
        "total": 5,
        "attempt": 1,
    }
    waiting = _base("evt-2", 2, "input_required", scope="step", status="waiting_input")
    waiting["step"] = step["step"]
    waiting["data"] = {"prompt": "choose"}
    received = _base("evt-3", 3, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {"userInputLength": 4}

    snapshot = reduce_pipeline_events([step, waiting, received])

    assert snapshot["status"] == "working"
    assert snapshot["pendingInput"] is None
    assert snapshot["steps"][0]["status"] == "completed"
    assert snapshot["steps"][0]["completedAt"] == "2026-06-08T10:00:00Z"


def test_reduce_cleanup_handoff_updates_snapshot_cleanup() -> None:
    handoff = _base("evt-cleanup-handoff", 1, "pipeline_handoff_ready", status="completed")
    handoff["data"] = {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "outcome": "completed",
        "summary": "[Pipeline Handoff Context]",
        "cleanup": {
            "status": "pending",
            "resourceCount": 1,
            "statusMessage": "检测到 1 个回滚残留资源，开始清理流程。",
            "resources": [{"resourceId": "stack-123", "regionId": "cn-hangzhou"}],
        },
    }

    snapshot = reduce_pipeline_events([handoff])

    assert snapshot["cleanup"]["status"] == "pending"
    assert snapshot["cleanup"]["resourceCount"] == 1
    assert snapshot["cleanup"]["resources"] == [{"resourceId": "stack-123", "regionId": "cn-hangzhou"}]
    assert snapshot["cleanup"]["history"][-1]["eventType"] == "pipeline_handoff_ready"
    assert snapshot["normalHandoff"]["data"]["cleanup"]["resourceCount"] == 1


def test_reduce_cleanup_progress_events_update_snapshot_cleanup() -> None:
    started = _base("evt-cleanup-started", 1, "cleanup_started", scope="cleanup")
    started["data"] = {
        "status": "started",
        "resourceCount": 1,
        "resources": [{"resourceId": "stack-123", "regionId": "cn-hangzhou"}],
    }
    progress = _base("evt-cleanup-progress", 2, "cleanup_progress", scope="cleanup")
    progress["data"] = {
        "status": "in_progress",
        "resourceId": "stack-123",
        "regionId": "cn-hangzhou",
        "stackStatus": "DELETE_IN_PROGRESS",
    }
    completed = _base("evt-cleanup-completed", 3, "cleanup_completed", scope="cleanup", status="completed")
    completed["data"] = {
        "status": "completed",
        "resourceId": "stack-123",
        "regionId": "cn-hangzhou",
        "stackStatus": "DELETE_COMPLETE",
    }

    snapshot = reduce_pipeline_events([started, progress, completed])

    assert snapshot["cleanup"]["status"] == "completed"
    assert snapshot["cleanup"]["resourceCount"] == 1
    assert snapshot["cleanup"]["resources"][0]["resourceId"] == "stack-123"
    assert snapshot["cleanup"]["resources"][0]["stackStatus"] == "DELETE_COMPLETE"
    assert [item["eventType"] for item in snapshot["cleanup"]["history"]] == [
        "cleanup_started",
        "cleanup_progress",
        "cleanup_completed",
    ]


def test_reduce_cleanup_status_aggregates_multiple_resources() -> None:
    started = _base("evt-cleanup-started", 1, "cleanup_started", scope="cleanup")
    started["data"] = {
        "status": "pending",
        "resourceCount": 2,
        "resources": [
            {
                "provider": "ros",
                "resourceType": "stack",
                "resourceId": "stack-a",
                "regionId": "cn-hangzhou",
                "cleanupStatus": "pending",
            },
            {
                "provider": "ros",
                "resourceType": "stack",
                "resourceId": "stack-b",
                "regionId": "cn-hangzhou",
                "cleanupStatus": "pending",
            },
        ],
    }
    completed_one = _base("evt-cleanup-one-complete", 2, "cleanup_completed", scope="cleanup")
    completed_one["data"] = {
        "status": "completed",
        "provider": "ros",
        "resourceType": "stack",
        "resourceId": "stack-a",
        "regionId": "cn-hangzhou",
        "cleanupStatus": "completed",
        "stackStatus": "DELETE_COMPLETE",
    }
    failed_one = _base("evt-cleanup-one-failed", 3, "cleanup_failed", scope="cleanup")
    failed_one["data"] = {
        "status": "failed",
        "provider": "ros",
        "resourceType": "stack",
        "resourceId": "stack-b",
        "regionId": "cn-hangzhou",
        "cleanupStatus": "failed",
        "stackStatus": "DELETE_FAILED",
    }

    partial = reduce_pipeline_events([started, completed_one])
    failed = reduce_pipeline_events([started, completed_one, failed_one])

    assert partial["cleanup"]["status"] == "pending"
    assert failed["cleanup"]["status"] == "failed"


def test_reduce_cleanup_progress_distinguishes_provider_and_resource_type() -> None:
    started = _base("evt-cleanup-started", 1, "cleanup_started", scope="cleanup")
    started["data"] = {
        "status": "started",
        "resourceCount": 3,
        "resources": [
            {
                "provider": "ros",
                "resourceType": "stack",
                "resourceId": "shared-id",
                "regionId": "cn-hangzhou",
                "stackStatus": "DELETE_IN_PROGRESS",
            },
            {
                "provider": "ros",
                "resourceType": "stack_set",
                "resourceId": "shared-id",
                "regionId": "cn-hangzhou",
                "stackStatus": "DELETE_IN_PROGRESS",
            },
            {
                "provider": "terraform",
                "resourceType": "stack",
                "resourceId": "shared-id",
                "regionId": "cn-hangzhou",
                "stackStatus": "DELETE_IN_PROGRESS",
            },
        ],
    }
    type_progress = _base("evt-cleanup-type-progress", 2, "cleanup_progress", scope="cleanup")
    type_progress["data"] = {
        "status": "in_progress",
        "provider": "ros",
        "resourceType": "stack_set",
        "resourceId": "shared-id",
        "regionId": "cn-hangzhou",
        "stackStatus": "DELETE_COMPLETE",
    }
    provider_progress = _base("evt-cleanup-provider-progress", 3, "cleanup_progress", scope="cleanup")
    provider_progress["data"] = {
        "status": "in_progress",
        "provider": "terraform",
        "resourceType": "stack",
        "resourceId": "shared-id",
        "regionId": "cn-hangzhou",
        "stackStatus": "DELETE_FAILED",
    }

    snapshot = reduce_pipeline_events([started, type_progress, provider_progress])

    resources = {
        (resource["provider"], resource["resourceType"]): resource for resource in snapshot["cleanup"]["resources"]
    }
    assert resources[("ros", "stack")]["stackStatus"] == "DELETE_IN_PROGRESS"
    assert resources[("ros", "stack_set")]["stackStatus"] == "DELETE_COMPLETE"
    assert resources[("terraform", "stack")]["stackStatus"] == "DELETE_FAILED"


def test_reduce_input_received_records_candidate_selection_details_on_step() -> None:
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {
        "runId": "step-confirm_and_select-1",
        "id": "confirm_and_select",
        "index": 4,
        "total": 5,
        "attempt": 1,
    }
    waiting = _base("evt-2", 2, "input_required", scope="step", status="waiting_input")
    waiting["step"] = step["step"]
    waiting["data"] = {"prompt": "choose"}
    received = _base("evt-3", 3, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {
        "kind": "candidate_selection",
        "userInputLength": 4,
        "selectedIndex": 1,
        "selectedValue": "方案B",
        "selectedOption": {"name": "方案B", "candidate_index": 1},
    }

    snapshot = reduce_pipeline_events([step, waiting, received])

    assert snapshot["steps"][0]["inputReceived"] == {
        "kind": "candidate_selection",
        "userInputLength": 4,
        "selectedIndex": 1,
        "selectedValue": "方案B",
        "selectedOption": {"name": "方案B", "candidate_index": 1},
    }


def test_reduce_ask_user_question_input_received_reopens_waiting_step() -> None:
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {
        "runId": "step-intent_parsing-1",
        "id": "intent_parsing",
        "index": 1,
        "total": 5,
        "attempt": 1,
    }
    waiting = _base("evt-2", 2, "input_required", scope="step", status="input_required")
    waiting["step"] = step["step"]
    waiting["input"] = {
        "inputId": "ask-ask-1",
        "kind": "ask_user_question",
        "toolUseId": "ask-1",
        "question": "choose",
        "prompt": "choose",
        "required": True,
        "options": [],
    }
    received = _base("evt-3", 3, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {"kind": "ask_user_question", "toolUseId": "ask-1", "answerTextLength": 4}

    snapshot = reduce_pipeline_events([step, waiting, received])

    assert snapshot["status"] == "working"
    assert snapshot["pendingInput"] is None
    assert snapshot["steps"][0]["status"] == "working"
    assert "completedAt" not in snapshot["steps"][0]


def test_reduce_pipeline_pause_confirmation_input_received_reopens_waiting_step() -> None:
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {
        "runId": "step-deploying-1",
        "id": "deploying",
        "index": 5,
        "total": 5,
        "attempt": 1,
    }
    waiting = _base("evt-2", 2, "input_required", scope="step", status="input_required")
    waiting["step"] = step["step"]
    waiting["input"] = {
        "inputId": "pause-deploying-1",
        "kind": "pipeline_pause_confirmation",
        "prompt": "Hard interrupt timed out; continue?",
        "required": True,
        "options": [],
    }
    received = _base("evt-3", 3, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {"kind": "pipeline_pause_confirmation", "answerTextLength": 8}

    snapshot = reduce_pipeline_events([step, waiting, received])

    assert snapshot["status"] == "working"
    assert snapshot["pendingInput"] is None
    assert snapshot["steps"][0]["status"] == "working"
    assert "completedAt" not in snapshot["steps"][0]


def test_reduce_records_input_interrupt_and_handoff_histories() -> None:
    step = _base("evt-step", 1, "step_started", scope="step")
    step["step"] = {
        "runId": "step-confirm_and_select-1",
        "id": "confirm_and_select",
        "index": 4,
        "total": 5,
        "attempt": 1,
    }
    waiting = _base("evt-input-required", 2, "input_required", scope="step", status="input_required")
    waiting["step"] = step["step"]
    waiting["input"] = {
        "inputId": "input-confirm_and_select-1",
        "kind": "candidate_selection",
        "prompt": "请选择方案",
        "options": [{"name": "方案A", "candidate_index": 0}],
    }
    received = _base("evt-input-received", 3, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {
        "kind": "candidate_selection",
        "selectedValue": "方案A",
        "selectedIndex": 0,
    }
    interrupt_received = _base("evt-interrupt-received", 4, "interrupt_received", scope="interrupt")
    interrupt_received["data"] = {"messageLength": 8}
    interrupt_classified = _base("evt-interrupt-classified", 5, "interrupt_classified", scope="interrupt")
    interrupt_classified["data"] = {
        "action": "supplement",
        "reason": "补充部署约束",
        "targetStepId": None,
        "candidateScope": None,
    }
    rollback = _base("evt-rollback", 6, "rollback_completed", scope="interrupt")
    rollback["step"] = step["step"]
    rollback["data"] = {"rollbackScope": "parent", "toStepId": "confirm_and_select", "reason": "重新选择"}
    handoff = _base("evt-handoff", 7, "pipeline_handoff_ready", status="completed")
    handoff["data"] = {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "outcome": "completed",
        "summary": "[Pipeline Handoff Context]",
    }

    snapshot = reduce_pipeline_events(
        [
            step,
            waiting,
            received,
            interrupt_received,
            interrupt_classified,
            rollback,
            handoff,
        ]
    )

    assert [item["eventType"] for item in snapshot["control"]["inputHistory"]] == [
        "input_required",
        "input_received",
    ]
    assert snapshot["control"]["inputHistory"][0]["inputId"] == "input-confirm_and_select-1"
    assert snapshot["control"]["inputHistory"][0]["prompt"] == "请选择方案"
    assert snapshot["control"]["inputHistory"][1]["selectedValue"] == "方案A"
    assert [item["eventType"] for item in snapshot["control"]["interruptHistory"]] == [
        "interrupt_received",
        "interrupt_classified",
        "rollback_completed",
    ]
    assert snapshot["control"]["interruptHistory"][1]["action"] == "supplement"
    assert snapshot["control"]["interruptHistory"][2]["step"]["id"] == "confirm_and_select"
    assert len(snapshot["control"]["handoffHistory"]) == 1
    assert snapshot["control"]["handoffHistory"][0]["action"] == "switch_to_normal"


def test_reduce_records_normal_handoff_ready() -> None:
    event = _base("evt-1", 1, "pipeline_handoff_ready", status="completed")
    event["data"] = {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "outcome": "completed",
        "summary": "[Pipeline Handoff Context]",
    }

    snapshot = reduce_pipeline_events([event])

    assert snapshot["status"] == "completed"
    assert snapshot["normalHandoff"]["eventId"] == "evt-1"
    assert snapshot["normalHandoff"]["sequence"] == 1
    assert snapshot["normalHandoff"]["action"] == "switch_to_normal"
    assert snapshot["normalHandoff"]["targetMode"] == "normal"
    assert snapshot["normalHandoff"]["outcome"] == "completed"
    assert snapshot["normalHandoff"]["summary"] == "[Pipeline Handoff Context]"


def test_reduce_is_idempotent_by_event_id() -> None:
    event = _base("evt-1", 1, "text_delta", scope="step")
    event["step"] = {"runId": "step-a-1", "id": "a", "index": 1, "total": 1, "attempt": 1}
    event["data"] = {"text": "hello"}

    snapshot = reduce_pipeline_events([event, event])

    assert len(snapshot["display"]["messages"]) == 1
    assert snapshot["display"]["messages"][0]["text"] == "hello"


def test_reduce_skips_non_dict_events() -> None:
    event = _base("evt-1", 1, "pipeline_started")

    snapshot = reduce_pipeline_events([None, event])

    assert snapshot["lastSequence"] == 1
    assert snapshot["pipelineRunId"] == "ctx-1"


def test_store_writes_and_loads_snapshot(tmp_path) -> None:
    store = A2APipelineSnapshotStore(tmp_path / "pipeline")
    snapshot = reduce_pipeline_events([_base("evt-1", 1, "pipeline_started")])

    store.save(snapshot)

    loaded = store.load()
    assert loaded is not None
    assert loaded["pipelineRunId"] == "ctx-1"


def test_reduce_text_deltas_append_per_scope_run_id() -> None:
    first = _base("evt-1", 1, "text_delta", scope="step")
    first["step"] = {"runId": "step-a-1", "id": "a", "index": 1, "total": 1, "attempt": 1}
    first["data"] = {"text": "hello"}
    second = _base("evt-2", 2, "text_delta", scope="step")
    second["step"] = first["step"]
    second["data"] = {"text": " world"}

    snapshot = reduce_pipeline_events([second, first])

    assert len(snapshot["display"]["messages"]) == 1
    assert snapshot["display"]["messages"][0]["runId"] == "step-a-1"
    assert snapshot["display"]["messages"][0]["text"] == "hello world"


def test_reduce_resumes_existing_snapshot_and_skips_seen_events() -> None:
    started = _base("evt-1", 1, "pipeline_started")
    first = _base("evt-2", 2, "text_delta", scope="step")
    first["step"] = {"runId": "step-a-1", "id": "a", "index": 1, "total": 2, "attempt": 1}
    first["data"] = {"text": "hello"}
    initial = reduce_pipeline_events([started, first])

    step = _base("evt-3", 3, "step_started", scope="step")
    step["step"] = {"runId": "step-b-1", "id": "b", "index": 2, "total": 2, "attempt": 1}
    second = _base("evt-4", 4, "text_delta", scope="step")
    second["step"] = first["step"]
    second["data"] = {"text": " world"}

    resumed = reduce_pipeline_events([first, step, second], existing_snapshot=initial)

    assert resumed["pipelineRunId"] == "ctx-1"
    assert resumed["lastSequence"] == 4
    assert [step["id"] for step in resumed["steps"]] == ["a", "b"]
    assert len(resumed["display"]["messages"]) == 1
    assert resumed["display"]["messages"][0]["text"] == "hello world"
    assert "evt-2" in resumed["seenEventIds"]
    assert "evt-4" in resumed["seenEventIds"]


def test_reduce_resume_without_seen_ids_uses_last_sequence_conservatively() -> None:
    first = _base("evt-1", 1, "text_delta", scope="step")
    first["step"] = {"runId": "step-a-1", "id": "a", "index": 1, "total": 1, "attempt": 1}
    first["data"] = {"text": "hello"}
    initial = reduce_pipeline_events([first])
    initial.pop("seenEventIds")

    second = _base("evt-2", 2, "text_delta", scope="step")
    second["step"] = first["step"]
    second["data"] = {"text": " world"}

    resumed = reduce_pipeline_events([first, second], existing_snapshot=initial)

    assert len(resumed["display"]["messages"]) == 1
    assert resumed["display"]["messages"][0]["text"] == "hello world"


def test_reduce_sanitizes_existing_bad_last_sequence() -> None:
    existing = reduce_pipeline_events([])
    existing["lastSequence"] = "bad"
    event = _base("evt-1", 3, "pipeline_started")

    snapshot = reduce_pipeline_events([event], existing_snapshot=existing)

    assert snapshot["lastSequence"] == 3
    assert snapshot["pipelineRunId"] == "ctx-1"


def test_reduce_sanitizes_existing_messages_before_appending_text() -> None:
    existing = reduce_pipeline_events([])
    existing["display"]["messages"] = [
        {"scope": "step", "runId": "step-a-1", "eventId": "evt-old-a"},
        {"scope": "step", "runId": "step-b-1", "eventId": "evt-old-b", "text": None},
    ]
    first = _base("evt-1", 1, "text_delta", scope="step")
    first["step"] = {"runId": "step-a-1", "id": "a", "index": 1, "total": 2, "attempt": 1}
    first["data"] = {"text": "hello"}
    second = _base("evt-2", 2, "text_delta", scope="step")
    second["step"] = {"runId": "step-b-1", "id": "b", "index": 2, "total": 2, "attempt": 1}
    second["data"] = {"text": "world"}

    snapshot = reduce_pipeline_events([first, second], existing_snapshot=existing)

    assert [message["text"] for message in snapshot["display"]["messages"]] == ["hello", "world"]


def test_reduce_candidate_lifecycle_and_candidate_steps() -> None:
    parent = _base("evt-1", 1, "step_started", scope="step")
    parent["step"] = {"runId": "step-evaluate-1", "id": "evaluate", "index": 1, "total": 1, "attempt": 1}
    candidate = _base("evt-2", 2, "candidate_started", scope="candidate")
    candidate["step"] = parent["step"]
    candidate["candidate"] = {
        "runId": "candidate-eval-0-1",
        "id": "eval",
        "index": 0,
        "attempt": 1,
        "name": "low cost",
    }
    candidate_step = _base("evt-3", 3, "candidate_step_started", scope="candidate_step")
    candidate_step["step"] = parent["step"]
    candidate_step["candidate"] = candidate["candidate"]
    candidate_step["candidateStep"] = {
        "runId": "candidate-eval-0-1-template_generating-1",
        "id": "template_generating",
        "index": 1,
        "total": 1,
        "attempt": 1,
    }
    completed = _base("evt-4", 4, "candidate_completed", scope="candidate")
    completed["step"] = parent["step"]
    completed["candidate"] = candidate["candidate"]

    snapshot = reduce_pipeline_events([candidate_step, completed, parent, candidate])

    step = snapshot["steps"][0]
    assert step["candidates"][0]["status"] == "completed"
    assert step["candidates"][0]["steps"][0]["id"] == "template_generating"
    assert snapshot["control"]["activeCandidateRunIds"] == []


def test_reduce_candidate_failure_keeps_snapshot_working() -> None:
    parent = _base("evt-1", 1, "step_started", scope="step")
    parent["step"] = {"runId": "step-evaluate-1", "id": "evaluate", "index": 1, "total": 1, "attempt": 1}
    candidate = _base("evt-2", 2, "candidate_started", scope="candidate")
    candidate["step"] = parent["step"]
    candidate["candidate"] = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    failed = _base("evt-3", 3, "candidate_failed", scope="candidate", status="working")
    failed["step"] = parent["step"]
    failed["candidate"] = candidate["candidate"]

    snapshot = reduce_pipeline_events([parent, candidate, failed])

    assert snapshot["status"] == "working"
    assert snapshot["steps"][0]["candidates"][0]["status"] == "failed"


def test_reduce_candidate_step_failure_keeps_snapshot_working() -> None:
    parent = _base("evt-1", 1, "step_started", scope="step")
    parent["step"] = {"runId": "step-evaluate-1", "id": "evaluate", "index": 1, "total": 1, "attempt": 1}
    candidate = _base("evt-2", 2, "candidate_started", scope="candidate")
    candidate["step"] = parent["step"]
    candidate["candidate"] = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    failed_step = _base("evt-3", 3, "candidate_step_failed", scope="candidate_step", status="working")
    failed_step["step"] = parent["step"]
    failed_step["candidate"] = candidate["candidate"]
    failed_step["candidateStep"] = {
        "runId": "candidate-eval-0-1-template-1",
        "id": "template",
        "index": 1,
        "total": 1,
        "attempt": 1,
    }

    snapshot = reduce_pipeline_events([parent, candidate, failed_step])

    assert snapshot["status"] == "working"
    assert snapshot["steps"][0]["candidates"][0]["steps"][0]["status"] == "failed"


def test_reduce_completion_events_keep_conclusions_on_pipeline_state_nodes() -> None:
    parent = _base("evt-1", 1, "step_completed", scope="step")
    parent["step"] = {"runId": "step-evaluate-1", "id": "evaluate", "index": 1, "total": 1, "attempt": 1}
    parent["data"] = {
        "conclusionField": "evaluated",
        "conclusion": {"selected": "Plan A"},
        "durationS": 1.5,
    }
    candidate = _base("evt-2", 2, "candidate_completed", scope="candidate")
    candidate["step"] = parent["step"]
    candidate["candidate"] = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    candidate["data"] = {"conclusions": {"template": {"body": "ros"}}}
    candidate_step = _base("evt-3", 3, "candidate_step_completed", scope="candidate_step")
    candidate_step["step"] = parent["step"]
    candidate_step["candidate"] = candidate["candidate"]
    candidate_step["candidateStep"] = {
        "runId": "candidate-eval-0-1-template-1",
        "id": "template",
        "index": 1,
        "total": 1,
        "attempt": 1,
    }
    candidate_step["data"] = {
        "conclusionField": "template",
        "conclusion": {"body": "ros"},
    }

    snapshot = reduce_pipeline_events([parent, candidate, candidate_step])

    step = snapshot["steps"][0]
    assert step["conclusionField"] == "evaluated"
    assert step["conclusion"] == {"selected": "Plan A"}
    assert step["durationS"] == 1.5
    assert step["candidates"][0]["conclusions"] == {"template": {"body": "ros"}}
    assert step["candidates"][0]["steps"][0]["conclusionField"] == "template"
    assert step["candidates"][0]["steps"][0]["conclusion"] == {"body": "ros"}


def test_reduce_candidate_without_parent_step_does_not_create_none_step() -> None:
    candidate = _base("evt-1", 1, "candidate_started", scope="candidate")
    candidate["candidate"] = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}

    snapshot = reduce_pipeline_events([candidate])

    assert snapshot["steps"] == []


def test_reduce_display_items_and_rollback_are_deduplicated() -> None:
    detail = _base("evt-1", 1, "candidate_detail_shown", scope="candidate")
    detail["candidate"] = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    detail["data"] = {"detailId": "detail-1", "summary": "single ecs"}
    diagram = _base("evt-2", 2, "diagram_shown", scope="candidate")
    diagram["candidate"] = detail["candidate"]
    diagram["data"] = {"diagramId": "diagram-1", "format": "mermaid", "mermaidSource": "graph TD"}
    artifact = _base("evt-3", 3, "artifact_created", scope="pipeline")
    artifact["data"] = {"artifactId": "artifact-1", "name": "template.yaml"}
    rollback = _base("evt-4", 4, "rollback_completed", scope="pipeline")
    rollback["data"] = {"fromStep": "review", "toStep": "plan"}

    snapshot = reduce_pipeline_events(
        [detail, detail.copy(), diagram, diagram.copy(), artifact, artifact.copy(), rollback]
    )

    assert len(snapshot["display"]["candidateDetails"]) == 1
    assert len(snapshot["display"]["diagrams"]) == 1
    assert len(snapshot["display"]["artifacts"]) == 1
    assert len(snapshot["control"]["rollbackHistory"]) == 1


def test_reduce_permission_and_tool_result_display_items() -> None:
    permission = _base("evt-permission", 1, "permission_requested", scope="pipeline")
    permission["permission"] = {
        "permissionId": "perm-toolu-1",
        "toolName": "bash",
        "toolUseId": "toolu-1",
        "safeSummary": "bash permission request (fields: cmd)",
        "approved": True,
        "decision": "allow_once",
    }
    permission["data"] = {"toolName": "bash", "toolUseId": "toolu-1"}
    tool_result = _base("evt-tool", 2, "tool_result", scope="pipeline")
    tool_result["data"] = {
        "toolName": "bash",
        "toolUseId": "toolu-1",
        "isError": False,
        "result": {"stdout": "done"},
    }

    snapshot = reduce_pipeline_events([permission, tool_result])

    assert snapshot["lastSequence"] == 2
    assert snapshot["display"]["permissions"][0]["permissionId"] == "perm-toolu-1"
    assert snapshot["display"]["permissions"][0]["approved"] is True
    assert "toolInput" not in snapshot["display"]["permissions"][0]
    assert snapshot["display"]["toolResults"][0]["toolUseId"] == "toolu-1"
    assert snapshot["display"]["toolResults"][0]["result"] == {"stdout": "done"}


def test_reduce_tool_result_drops_legacy_artifact_file_uri() -> None:
    tool_result = _base("evt-tool", 1, "tool_result", scope="pipeline")
    tool_result["data"] = {
        "toolName": "write_file",
        "toolUseId": "toolu-1",
        "isError": False,
        "result": {
            "artifact": {
                "filename": "template.yaml",
                "uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                "publicUrl": r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml",
                "encodedOwnerUrl": (
                    "iac-code-artifact://C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo/template.yaml"
                ),
                "backupUri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
                "source": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                "metadata": {
                    "uri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
                    "byteSize": 10,
                },
                "parts": [
                    {
                        "url": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                        "metadata": {"uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"},
                    }
                ],
            }
        },
    }

    snapshot = reduce_pipeline_events([tool_result])

    artifact = snapshot["display"]["toolResults"][0]["result"]["artifact"]
    assert artifact["filename"] == "template.yaml"
    assert artifact["metadata"] == {"byteSize": 10}
    assert "uri" not in artifact
    assert "publicUrl" not in artifact
    assert "encodedOwnerUrl" not in artifact
    assert "backupUri" not in artifact
    assert artifact["source"] == "[PATH]"
    assert "url" not in artifact["parts"][0]
    assert "uri" not in artifact["parts"][0]["metadata"]
    rendered = str(snapshot)
    assert "file://" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


def test_reduce_tool_result_sanitizes_artifact_list() -> None:
    legacy_uri = r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"
    tool_result = _base("evt-tool", 1, "tool_result", scope="pipeline")
    tool_result["data"] = {
        "toolName": "write_file",
        "toolUseId": "toolu-1",
        "isError": False,
        "result": {
            "artifact": [
                legacy_uri,
                {
                    "filename": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
                    "uri": [legacy_uri],
                    "parts": [legacy_uri, {"url": legacy_uri}],
                },
            ]
        },
    }

    snapshot = reduce_pipeline_events([tool_result])

    artifact = snapshot["display"]["toolResults"][0]["result"]["artifact"]
    assert artifact[0] == "[PATH]"
    assert artifact[1]["filename"] == "template.yaml"
    assert "uri" not in artifact[1]
    assert artifact[1]["parts"][0] == "[PATH]"
    assert "url" not in artifact[1]["parts"][1]
    rendered = str(snapshot)
    assert "file://" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


def test_reduce_tool_result_sanitizes_root_list_artifact_payloads() -> None:
    tool_result = _base("evt-tool", 1, "tool_result", scope="pipeline")
    tool_result["data"] = {
        "toolName": "write_file",
        "toolUseId": "toolu-1",
        "isError": False,
        "result": [
            {
                "artifacts": [
                    {
                        "filename": "template.yaml",
                        "Content": "RAW-TEMPLATE-CONTENT",
                        "metadata": {"api_key": "plain-secret"},
                        "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
                    }
                ],
                "api_key": "secret-key",
            }
        ],
    }

    snapshot = reduce_pipeline_events([tool_result])

    result = snapshot["display"]["toolResults"][0]["result"][0]
    assert result == {
        "artifacts": [{"filename": "template.yaml", "metadata": {"api_key": "[REDACTED]"}}],
        "api_key": "[REDACTED]",
    }
    rendered = str(snapshot)
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "plain-secret" not in rendered
    assert "secret-key" not in rendered
    assert "Alice and Bob" not in rendered
    assert ".iac-code" not in rendered


def test_reduce_stack_current_changed_updates_snapshot_stack_state() -> None:
    created = _base("evt-create", 1, "stack_current_changed", scope="stack")
    created["data"] = {
        "toolName": "aliyun_api",
        "toolUseId": "toolu-create",
        "provider": "ros",
        "action": "CreateStack",
        "regionId": "cn-hangzhou",
        "stackId": "stack-123",
        "stackName": "demo",
        "isSuccess": True,
        "current": True,
    }
    deleted = _base("evt-delete", 2, "stack_current_changed", scope="stack")
    deleted["data"] = {
        "toolName": "ros_stack",
        "toolUseId": "toolu-delete",
        "provider": "ros",
        "action": "DeleteStack",
        "regionId": "cn-hangzhou",
        "stackId": "stack-123",
        "stackName": "demo",
        "stackStatus": "DELETE_COMPLETE",
        "isSuccess": True,
        "current": False,
        "cleared": True,
    }

    created_snapshot = reduce_pipeline_events([created])
    deleted_snapshot = reduce_pipeline_events([created, deleted])

    assert created_snapshot["stacks"]["current"]["stackId"] == "stack-123"
    assert created_snapshot["stacks"]["byId"]["stack-123"]["current"] is True
    assert deleted_snapshot["stacks"]["current"] is None
    assert deleted_snapshot["stacks"]["byId"]["stack-123"]["current"] is False
    assert [item["eventId"] for item in deleted_snapshot["stacks"]["history"]] == ["evt-create", "evt-delete"]


def test_reduce_stack_current_changed_keeps_current_for_delete_requested() -> None:
    created = _base("evt-create", 1, "stack_current_changed", scope="stack")
    created["data"] = {
        "toolName": "aliyun_api",
        "toolUseId": "toolu-create",
        "provider": "ros",
        "action": "CreateStack",
        "regionId": "cn-hangzhou",
        "stackId": "stack-123",
        "stackName": "demo",
        "isSuccess": True,
        "current": True,
    }
    delete_requested = _base("evt-delete-requested", 2, "stack_current_changed", scope="stack")
    delete_requested["data"] = {
        "toolName": "ros_stack",
        "toolUseId": "toolu-delete",
        "provider": "ros",
        "action": "DeleteStack",
        "regionId": "cn-hangzhou",
        "stackId": "stack-123",
        "stackName": "demo",
        "stackStatus": "DELETE_REQUESTED",
        "isSuccess": True,
        "current": True,
    }

    snapshot = reduce_pipeline_events([created, delete_requested])

    assert snapshot["stacks"]["current"]["stackId"] == "stack-123"
    assert snapshot["stacks"]["byId"]["stack-123"]["current"] is True
    assert snapshot["stacks"]["byId"]["stack-123"]["stackStatus"] == "DELETE_REQUESTED"


def test_reduce_artifact_created_prefers_top_level_artifact_metadata() -> None:
    artifact = _base("evt-1", 1, "artifact_created", scope="step")
    artifact["step"] = {"runId": "step-a-1", "id": "a", "index": 1, "total": 1, "attempt": 1}
    artifact["artifact"] = {
        "artifact_id": "artifact-top-1",
        "name": "top.yaml",
        "mediaType": "text/yaml",
        "metadata": {"byteSize": 12},
    }
    artifact["data"] = {"artifactId": "artifact-data-1", "name": "data.yaml"}
    duplicate = _base("evt-2", 2, "artifact_created", scope="step")
    duplicate["step"] = artifact["step"]
    duplicate["artifact"] = {"artifactId": "artifact-top-1", "name": "updated.yaml", "uri": "file:///tmp/top.yaml"}

    snapshot = reduce_pipeline_events([artifact, duplicate])

    artifacts = snapshot["display"]["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["id"] == "artifact-top-1"
    assert artifacts[0]["artifactId"] == "artifact-top-1"
    assert artifacts[0]["name"] == "updated.yaml"
    assert artifacts[0]["mediaType"] == "text/yaml"
    assert artifacts[0]["metadata"] == {"byteSize": 12}
    assert "uri" not in artifacts[0]
    assert artifacts[0]["sequence"] == 2
    assert artifacts[0]["scope"] == "step"
    assert artifacts[0]["runId"] == "step-a-1"
    assert artifacts[0]["step"]["id"] == "a"


def test_reduce_artifact_created_keeps_opaque_artifact_uri() -> None:
    artifact = _base("evt-1", 1, "artifact_created", scope="pipeline")
    artifact["data"] = {
        "artifactId": "artifact-1",
        "name": "template.yaml",
        "uri": "iac-code-artifact://artifact-1/template.yaml",
    }

    snapshot = reduce_pipeline_events([artifact])

    assert snapshot["display"]["artifacts"][0]["uri"] == "iac-code-artifact://artifact-1/template.yaml"


def test_reduce_deduplicates_existing_display_items_and_rollbacks() -> None:
    existing = reduce_pipeline_events([])
    existing["display"]["diagrams"] = [
        {"id": "diagram-1", "diagramId": "diagram-1", "eventId": "evt-old-diagram-1", "format": "mermaid"},
        {"id": "diagram-1", "diagramId": "diagram-1", "eventId": "evt-old-diagram-2", "format": "stale"},
    ]
    existing["display"]["artifacts"] = [
        {"id": "artifact-1", "artifactId": "artifact-1", "eventId": "evt-old-artifact-1", "name": "old.yaml"},
        {"id": "artifact-1", "artifactId": "artifact-1", "eventId": "evt-old-artifact-2", "name": "stale.yaml"},
    ]
    existing["control"]["rollbackHistory"] = [
        {"eventId": "evt-rollback", "sequence": 7, "data": {"fromStep": "a"}},
        {"eventId": "evt-rollback", "sequence": 7, "data": {"fromStep": "stale"}},
    ]

    diagram = _base("evt-new-diagram", 8, "diagram_shown")
    diagram["data"] = {"diagramId": "diagram-1", "format": "mermaid", "mermaidSource": "graph TD"}
    artifact = _base("evt-new-artifact", 9, "artifact_created")
    artifact["data"] = {"artifactId": "artifact-1", "name": "updated.yaml"}

    snapshot = reduce_pipeline_events([diagram, artifact], existing_snapshot=existing)

    assert len(snapshot["display"]["diagrams"]) == 1
    assert snapshot["display"]["diagrams"][0]["mermaidSource"] == "graph TD"
    assert len(snapshot["display"]["artifacts"]) == 1
    assert snapshot["display"]["artifacts"][0]["name"] == "updated.yaml"
    assert len(snapshot["control"]["rollbackHistory"]) == 1
    assert snapshot["control"]["rollbackHistory"][0]["data"] == {"fromStep": "a"}


def test_reduce_sanitizes_malformed_existing_step_and_control_records() -> None:
    existing = reduce_pipeline_events([])
    existing["steps"] = [
        {"id": "missing-run", "candidates": []},
        {
            "runId": "step-a-1",
            "id": "a",
            "candidates": [
                {"id": "candidate-without-run", "steps": []},
                {
                    "runId": "candidate-a-0-1",
                    "id": "candidate-a",
                    "steps": [{"id": "missing-run"}, {"runId": "candidate-a-0-1-template-1", "id": "template"}],
                },
            ],
        },
    ]
    existing["control"]["activeCandidateRunIds"] = [
        "candidate-a-0-1",
        None,
        "candidate-a-0-1",
        "missing-candidate",
    ]
    existing["control"]["rollbackHistory"] = [
        {"eventId": "evt-rollback", "sequence": 1, "data": {"fromStep": "a"}},
        {"eventId": "evt-rollback", "sequence": 1, "data": {"fromStep": "duplicate"}},
    ]

    snapshot = reduce_pipeline_events([], existing_snapshot=existing)

    assert [step["runId"] for step in snapshot["steps"]] == ["step-a-1"]
    assert [candidate["runId"] for candidate in snapshot["steps"][0]["candidates"]] == ["candidate-a-0-1"]
    assert [step["runId"] for step in snapshot["steps"][0]["candidates"][0]["steps"]] == ["candidate-a-0-1-template-1"]
    assert snapshot["control"]["activeCandidateRunIds"] == ["candidate-a-0-1"]
    assert len(snapshot["control"]["rollbackHistory"]) == 1


def test_reduce_candidate_restart_removes_old_run_from_active_when_next_attempt_starts() -> None:
    step = _base("evt-step", 1, "step_started", scope="step")
    step["step"] = {"runId": "step-evaluate_candidates-1", "id": "evaluate_candidates", "attempt": 1}
    old_started = _base("evt-old-start", 2, "candidate_started", scope="candidate")
    old_started["step"] = step["step"]
    old_started["candidate"] = {
        "runId": "candidate-eval-0-1",
        "id": "eval",
        "index": 0,
        "attempt": 1,
    }
    restart = _base("evt-restart", 3, "candidate_restart_requested", scope="candidate")
    restart["step"] = step["step"]
    restart["candidate"] = old_started["candidate"]
    restart["data"] = {"candidateScope": "candidate:0", "nextCandidateAttempt": 2}
    new_started = _base("evt-new-start", 4, "candidate_started", scope="candidate")
    new_started["step"] = step["step"]
    new_started["candidate"] = {
        "runId": "candidate-eval-0-2",
        "id": "eval",
        "index": 0,
        "attempt": 2,
    }

    snapshot = reduce_pipeline_events([step, old_started, restart, new_started])

    candidates = snapshot["steps"][0]["candidates"]
    assert [candidate["runId"] for candidate in candidates] == ["candidate-eval-0-1", "candidate-eval-0-2"]
    assert candidates[0]["status"] == "restarting"
    assert candidates[1]["status"] == "working"
    assert snapshot["control"]["activeCandidateRunIds"] == ["candidate-eval-0-2"]


def test_reduce_ignores_bool_and_float_sequences() -> None:
    bool_sequence = _base("evt-bool", 99, "artifact_created")
    bool_sequence["sequence"] = True
    bool_sequence["data"] = {"name": "bool.yaml"}
    bool_sequence.pop("eventId")
    float_sequence = _base("evt-float", 99, "artifact_created")
    float_sequence["sequence"] = 4.5
    float_sequence["data"] = {"name": "float.yaml"}
    float_sequence.pop("eventId")
    digit_sequence = _base("evt-digit", 2, "artifact_created")
    digit_sequence["sequence"] = "2"
    digit_sequence["data"] = {"name": "digit.yaml"}
    digit_sequence.pop("eventId")

    snapshot = reduce_pipeline_events([digit_sequence, bool_sequence, float_sequence])

    assert snapshot["lastSequence"] == 2
    assert [artifact["artifactId"] for artifact in snapshot["display"]["artifacts"]] == [
        "artifact-0",
        "artifact-2",
    ]
    assert snapshot["display"]["artifacts"][0]["name"] == "float.yaml"


def test_store_increments_snapshot_version_and_handles_invalid_load(tmp_path) -> None:
    store = A2APipelineSnapshotStore(tmp_path / "pipeline")
    first = reduce_pipeline_events([_base("evt-1", 1, "pipeline_started")])
    second = reduce_pipeline_events([_base("evt-2", 2, "pipeline_completed", status="completed")])

    store.save(first)
    store.save(second)

    loaded = store.load()
    assert loaded is not None
    assert loaded["snapshotVersion"] == 2

    store.path.write_text("not-json", encoding="utf-8")
    assert store.load() is None


def test_store_sanitizes_non_finite_and_non_json_values(tmp_path) -> None:
    store = A2APipelineSnapshotStore(tmp_path / "pipeline")
    snapshot = reduce_pipeline_events([_base("evt-1", 1, "candidate_detail_shown")])
    snapshot["display"]["candidateDetails"] = [{"totalMonthlyCost": float("inf"), "raw": object()}]

    store.save(snapshot)

    loaded = store.load()
    assert loaded is not None
    assert loaded["display"]["candidateDetails"][0]["totalMonthlyCost"] is None
    assert loaded["display"]["candidateDetails"][0]["raw"].startswith("<object object at ")


def test_store_sanitizes_cleanup_private_fields_without_dropping_input_prompt(tmp_path) -> None:
    store = A2APipelineSnapshotStore(tmp_path / "pipeline")
    raw_error = (
        "DeleteStack failed AccessKeySecret=super-secret token=sk-live-1234567890 "
        "at /Users/alice/.iac-code/projects/session/pipeline/cleanup.yaml"
    )
    snapshot = reduce_pipeline_events([_base("evt-1", 1, "pipeline_started")])
    snapshot["pendingInput"] = {"prompt": "choose deployment target"}
    snapshot["control"]["inputHistory"] = [{"prompt": "choose deployment target"}]
    snapshot["control"]["handoffHistory"] = [
        {
            "data": {
                "cleanup": {
                    "prompt": "hidden cleanup prompt",
                    "ledgerPath": "/tmp/cleanup.yaml",
                    "lastError": raw_error,
                }
            }
        }
    ]
    snapshot["normalHandoff"] = {
        "data": {
            "cleanup": {
                "prompt": "hidden cleanup prompt",
                "ledgerPath": "/tmp/cleanup.yaml",
                "lastError": raw_error,
            }
        }
    }
    snapshot["cleanup"] = {
        "status": "pending",
        "resourceCount": 1,
        "resources": [{"resourceId": "stack-123", "lastError": raw_error}],
        "history": [
            {"data": {"prompt": "hidden cleanup prompt", "ledgerPath": "/tmp/cleanup.yaml", "lastError": raw_error}}
        ],
        "prompt": "hidden cleanup prompt",
        "ledgerPath": "/tmp/cleanup.yaml",
        "last_error": raw_error,
    }

    store.save(snapshot)

    loaded = store.load()
    assert loaded is not None
    assert loaded["pendingInput"]["prompt"] == "choose deployment target"
    assert loaded["control"]["inputHistory"][0]["prompt"] == "choose deployment target"
    assert "prompt" not in loaded["control"]["handoffHistory"][0]["data"]["cleanup"]
    assert raw_error not in loaded["control"]["handoffHistory"][0]["data"]["cleanup"]["lastError"]
    assert "ledgerPath" not in loaded["normalHandoff"]["data"]["cleanup"]
    assert raw_error not in loaded["normalHandoff"]["data"]["cleanup"]["lastError"]
    assert "prompt" not in loaded["cleanup"]
    assert raw_error not in loaded["cleanup"]["last_error"]
    assert raw_error not in loaded["cleanup"]["resources"][0]["lastError"]
    assert "ledgerPath" not in loaded["cleanup"]["history"][0]["data"]
    assert raw_error not in loaded["cleanup"]["history"][0]["data"]["lastError"]
    rendered = json.dumps(loaded, ensure_ascii=False)
    assert "super-secret" not in rendered
    assert "sk-live-1234567890" not in rendered
    assert "/Users/alice" not in rendered
    assert "[REDACTED]" in rendered
    assert "[PATH]" in rendered

    store.path.write_text(json.dumps(snapshot), encoding="utf-8")
    loaded = store.load()
    assert loaded is not None
    assert loaded["pendingInput"]["prompt"] == "choose deployment target"
    assert "prompt" not in loaded["normalHandoff"]["data"]["cleanup"]
    assert "ledgerPath" not in loaded["cleanup"]
    rendered = json.dumps(loaded, ensure_ascii=False)
    assert "super-secret" not in rendered
    assert "sk-live-1234567890" not in rendered
    assert "/Users/alice" not in rendered


def test_store_returns_none_for_invalid_utf8_snapshot(tmp_path) -> None:
    store = A2APipelineSnapshotStore(tmp_path / "pipeline")
    store.pipeline_dir.mkdir(parents=True)
    store.path.write_bytes(b"\xff\xfe\x00")

    assert store.load() is None


def test_snapshot_schema_version_is_exported() -> None:
    assert SNAPSHOT_SCHEMA_VERSION == "1.1"
    assert "SNAPSHOT_SCHEMA_VERSION" in pipeline_snapshot.__all__
