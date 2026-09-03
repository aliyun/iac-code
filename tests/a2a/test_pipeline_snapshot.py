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
    assert str(store.path) not in caplog.text
    assert "path=[PATH]" in caplog.text


def test_snapshot_save_cleans_temp_file_when_replace_fails(monkeypatch, tmp_path, caplog) -> None:
    store = A2APipelineSnapshotStore(tmp_path)

    def fail_write(path: Path, value: dict, *, durable: bool = True) -> None:
        raise PermissionError(f"locked: {path}")

    monkeypatch.setattr(pipeline_snapshot, "atomic_write_json", fail_write)
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


def test_pipeline_warning_does_not_change_terminal_snapshot_status() -> None:
    started = _base("evt-start", 1, "pipeline_started")
    warning = _base("evt-warning", 2, "pipeline_warning", status="working")
    warning["data"] = {"reason": "cleanup_tracking_unavailable"}

    snapshot = reduce_pipeline_events([started, warning])

    assert snapshot["status"] == "working"
    assert snapshot["lastSequence"] == 2
    assert snapshot.get("completedAt") is None
    assert snapshot["control"]["warningHistory"] == [
        {
            "eventId": "evt-warning",
            "sequence": 2,
            "createdAt": "2026-06-08T10:00:00Z",
            "data": {"reason": "cleanup_tracking_unavailable"},
        }
    ]


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


def test_reduce_input_received_records_selected_candidate_in_control() -> None:
    # Issue 1/3: persist the picked plan in control.selectedCandidate so a reload
    # renders the ✓ and keeps the selection buttons suppressed.
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "index": 4, "total": 5}
    waiting = _base("evt-2", 2, "input_required", scope="step", status="waiting_input")
    waiting["step"] = step["step"]
    waiting["data"] = {"prompt": "choose"}
    received = _base("evt-3", 3, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {
        "kind": "candidate_selection",
        "selectedIndex": 0,
        "selectedValue": "最低成本测试方案",
        "selectedOption": {"name": "最低成本测试方案", "candidate_index": 0},
    }

    snapshot = reduce_pipeline_events([step, waiting, received])

    assert snapshot["control"]["selectedCandidate"] == {
        "candidateName": "最低成本测试方案",
        "candidateIndex": 0,
    }


def test_reduce_input_received_recovers_selection_from_selected_value_json() -> None:
    # Fallback: name/index missing at top level, recover from selectedValue JSON.
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "index": 4, "total": 5}
    received = _base("evt-2", 2, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {
        "kind": "candidate_selection",
        "selectedValue": json.dumps(
            {"selected_candidate_name": "高可用方案", "selected_candidate_index": 2},
            ensure_ascii=False,
        ),
    }

    snapshot = reduce_pipeline_events([step, received])

    assert snapshot["control"]["selectedCandidate"] == {
        "candidateName": "高可用方案",
        "candidateIndex": 2,
    }


def test_reduce_input_received_non_candidate_kind_leaves_selection_absent() -> None:
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {"runId": "step-intent_parsing-1", "id": "intent_parsing", "index": 1, "total": 5}
    received = _base("evt-2", 2, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {"kind": "ask_user_question", "selectedValue": "补充说明"}

    snapshot = reduce_pipeline_events([step, received])

    assert "selectedCandidate" not in snapshot["control"]


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


def test_reduce_records_first_user_request_from_pipeline_started() -> None:
    """会话恢复要还原「用户发的第一句话」。

    流水线会话的 JSONL 只记录 pipeline_init / step_complete 元信息,首句 prompt 只随
    pipeline_started 落到快照里,因此这里是唯一的持久化点。
    """
    started = _base("evt-1", 1, "pipeline_started")
    started["data"] = {
        "pipelineType": "selling_solution_first",
        "totalSteps": 3,
        "userRequest": "帮我搭一个静态网站",
    }

    snapshot = reduce_pipeline_events([started])

    assert snapshot["control"]["userRequest"] == "帮我搭一个静态网站"


def test_reduce_pipeline_started_without_user_request_leaves_control_absent() -> None:
    """旧快照/旧事件没有该字段时不写空值,前端据此退回原来的行为。"""
    started = _base("evt-1", 1, "pipeline_started")
    started["data"] = {"totalSteps": 3, "userRequest": ""}

    snapshot = reduce_pipeline_events([started])

    assert "userRequest" not in snapshot["control"]


def test_reduce_ask_user_question_input_history_keeps_free_text() -> None:
    """问答卡的自由文本要能回填。

    控制台的问答卡只渲染「选中项 label」与「自由文本」,恢复时两者都得从 inputHistory 拿到,
    所以 input_received 不能只留 freeTextLength。
    """
    step = _base("evt-1", 1, "step_started", scope="step")
    step["step"] = {
        "runId": "step-confirm_and_select-1",
        "id": "confirm_and_select",
        "index": 1,
        "total": 3,
        "attempt": 1,
    }
    waiting = _base("evt-2", 2, "input_required", scope="step", status="input_required")
    waiting["step"] = step["step"]
    waiting["input"] = {
        "inputId": "ask-ask-1",
        "kind": "ask_user_question",
        "toolUseId": "ask-1",
        "question": "要不要高可用?",
        "prompt": "要不要高可用?",
        "options": [{"id": "cheap", "label": "不需要"}],
        "allowFreeText": True,
    }
    received = _base("evt-3", 3, "input_received", scope="step")
    received["step"] = step["step"]
    received["data"] = {
        "kind": "ask_user_question",
        "toolUseId": "ask-1",
        "answerTextLength": 8,
        "selectedId": "cheap",
        "selectedLabel": "不需要",
        "freeText": "预算优先",
        "freeTextLength": 4,
    }

    snapshot = reduce_pipeline_events([step, waiting, received])

    history = snapshot["control"]["inputHistory"]
    assert [item["eventType"] for item in history] == ["input_required", "input_received"]
    assert history[0]["options"] == [{"id": "cheap", "label": "不需要"}]
    assert history[0]["allowFreeText"] is True
    assert history[1]["selectedId"] == "cheap"
    assert history[1]["selectedLabel"] == "不需要"
    assert history[1]["freeText"] == "预算优先"


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


def test_reduce_deployment_confirmation_resumes_step_and_accumulates_processing_time() -> None:
    step_coordinate = {
        "runId": "step-materialize-selected-candidate-1",
        "id": "materialize_selected_candidate",
        "index": 2,
        "total": 3,
        "attempt": 1,
    }
    started = _base("evt-1", 1, "step_started", scope="step")
    started["step"] = step_coordinate
    first_completed = _base("evt-2", 2, "step_completed", scope="step")
    first_completed["step"] = step_coordinate
    first_completed["data"] = {"durationS": 135.4}
    waiting = _base("evt-3", 3, "input_required", scope="step", status="input_required")
    waiting["step"] = step_coordinate
    waiting["data"] = {"kind": "deployment_confirmation", "prompt": "请选择下一步"}
    received = _base("evt-4", 4, "input_received", scope="step")
    received["step"] = step_coordinate
    received["data"] = {"kind": "deployment_confirmation", "userInputLength": 12}

    resumed = reduce_pipeline_events([started, first_completed, waiting, received])

    assert resumed["steps"][0]["status"] == "working"
    assert "completedAt" not in resumed["steps"][0]

    second_completed = _base("evt-5", 5, "step_completed", scope="step")
    second_completed["step"] = step_coordinate
    second_completed["data"] = {"durationS": 0.04}
    completed = reduce_pipeline_events([started, first_completed, waiting, received, second_completed])

    assert completed["steps"][0]["status"] == "completed"
    assert completed["steps"][0]["durationS"] == 135.44


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
    interrupt_received["data"] = {"messageLength": 8, "userInput": "change it"}
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
    assert snapshot["control"]["interruptHistory"][0]["userInput"] == "change it"
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


def test_reduce_defers_committed_handoff_until_backup_ack() -> None:
    pending = _base("evt-pending", 1, "pipeline_handoff_ready", status="completed")
    pending["visibility"] = "pending_backup"
    pending["data"] = {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "outcome": "completed",
        "summary": "[Pipeline Handoff Context]",
    }
    committed = dict(pending)
    committed["eventId"] = "evt-committed"
    committed["sequence"] = 2
    committed["visibility"] = "committed"

    snapshot = reduce_pipeline_events([pending, committed])

    assert snapshot["normalHandoff"] is None
    assert snapshot["pendingNormalHandoff"]["eventId"] == "evt-pending"

    ack = _base("evt-ack", 3, "backup_committed", status=None)
    ack["data"] = {
        "committedEventId": "evt-committed",
        "committedEventType": "pipeline_handoff_ready",
        "committedSequence": 2,
    }

    snapshot = reduce_pipeline_events([pending, committed, ack])

    assert snapshot["normalHandoff"]["eventId"] == "evt-committed"
    assert snapshot["pendingNormalHandoff"] is None


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


def _step_text_delta(event_id: str, sequence: int, text: str, run_id: str = "step-a-1") -> dict:
    event = _base(event_id, sequence, "text_delta", scope="step")
    event["step"] = {"runId": run_id, "id": "a", "index": 1, "total": 1, "attempt": 1}
    event["data"] = {"text": text}
    return event


def _step_input_received(
    event_id: str,
    sequence: int,
    *,
    run_id: str = "step-a-1",
    kind: str = "candidate_selection",
    selected_value: str = "再便宜一点",
) -> dict:
    event = _base(event_id, sequence, "input_received", scope="step")
    event["step"] = {"runId": run_id, "id": "a", "index": 1, "total": 1, "attempt": 1}
    event["data"] = {"kind": kind, "selectedValue": selected_value}
    return event


def test_reduce_text_deltas_open_new_round_after_user_input() -> None:
    """A re-plan inside one step attempt must not merge into one narration blob.

    Answering mid-step resumes the same attempt (same runId), so without rounds both
    plans accumulate into a single message and replay cannot tell them apart.
    """
    events = [
        _step_text_delta("evt-1", 1, "first plan"),
        _step_input_received("evt-2", 2),
        _step_text_delta("evt-3", 3, "second plan"),
        _step_text_delta("evt-4", 4, " continued"),
    ]

    snapshot = reduce_pipeline_events(events)

    messages = snapshot["display"]["messages"]
    assert [(message["round"], message["text"]) for message in messages] == [
        (1, "first plan"),
        (2, "second plan continued"),
    ]
    assert [message["id"] for message in messages] == [
        "message-step-step-a-1",
        "message-step-step-a-1-round-2",
    ]
    assert all(message["runId"] == "step-a-1" for message in messages)


def test_reduce_text_deltas_open_new_round_after_active_supplement() -> None:
    interrupt_received = _base("evt-2", 2, "interrupt_received", scope="interrupt")
    interrupt_received["data"] = {"messageLength": 9, "userInput": "add a subnet"}
    interrupt_classified = _base("evt-3", 3, "interrupt_classified", scope="interrupt")
    interrupt_classified["data"] = {"action": "supplement", "reason": "additional constraint"}

    snapshot = reduce_pipeline_events(
        [
            _step_text_delta("evt-1", 1, "first plan"),
            interrupt_received,
            interrupt_classified,
            _step_text_delta("evt-4", 4, "supplemented plan"),
        ]
    )

    assert [(message["round"], message["text"]) for message in snapshot["display"]["messages"]] == [
        (1, "first plan"),
        (2, "supplemented plan"),
    ]


def test_reduce_message_rounds_are_scoped_to_the_answering_run() -> None:
    events = [
        _step_text_delta("evt-1", 1, "step a"),
        _step_text_delta("evt-2", 2, "step b", run_id="step-b-1"),
        _step_input_received("evt-3", 3),
        _step_text_delta("evt-4", 4, " more b", run_id="step-b-1"),
        _step_text_delta("evt-5", 5, "round two a"),
    ]

    snapshot = reduce_pipeline_events(events)

    assert [(message["runId"], message["round"], message["text"]) for message in snapshot["display"]["messages"]] == [
        ("step-a-1", 1, "step a"),
        ("step-b-1", 1, "step b more b"),
        ("step-a-1", 2, "round two a"),
    ]


def test_reduce_resumed_snapshot_keeps_appending_to_the_open_round() -> None:
    initial = reduce_pipeline_events([_step_text_delta("evt-1", 1, "first plan"), _step_input_received("evt-2", 2)])

    resumed = reduce_pipeline_events([_step_text_delta("evt-3", 3, "second plan")], existing_snapshot=initial)

    assert [(message["round"], message["text"]) for message in resumed["display"]["messages"]] == [
        (1, "first plan"),
        (2, "second plan"),
    ]


def test_reduce_resumes_pre_round_snapshot_into_the_next_round() -> None:
    """Snapshots written before rounds existed carry no ``round`` on their message.

    ``inputHistory`` still records every answer, so the recovered counter puts new
    text in a fresh round instead of appending it to the merged historical blob.
    """
    existing = reduce_pipeline_events([])
    existing["display"]["messages"] = [{"scope": "step", "runId": "step-a-1", "text": "merged history"}]
    existing["control"]["inputHistory"] = [
        {
            "eventType": "input_received",
            "eventId": "evt-old",
            "sequence": 2,
            "runId": "step-a-1",
            "kind": "candidate_selection",
        }
    ]

    resumed = reduce_pipeline_events([_step_text_delta("evt-3", 3, "after reload")], existing_snapshot=existing)

    messages = resumed["display"]["messages"]
    assert [(message["round"], message["text"]) for message in messages] == [
        (1, "merged history"),
        (2, "after reload"),
    ]


def _step_message_started(
    event_id: str,
    sequence: int,
    *,
    run_id: str = "step-a-1",
    message_id: str = "msg-1",
) -> dict:
    event = _base(event_id, sequence, "message_started", scope="step")
    event["step"] = {"runId": run_id, "id": "a", "index": 1, "total": 1, "attempt": 1}
    event["data"] = {"messageId": message_id}
    return event


def _step_thinking_delta(
    event_id: str,
    sequence: int,
    text: str,
    *,
    run_id: str = "step-a-1",
) -> dict:
    event = _base(event_id, sequence, "thinking_delta", scope="step")
    event["step"] = {"runId": run_id, "id": "a", "index": 1, "total": 1, "attempt": 1}
    event["data"] = {"text": text}
    return event


def test_reduce_persists_public_narrative_shape_without_thinking_content() -> None:
    events = [
        _step_thinking_delta("evt-1", 1, "private reasoning one"),
        _step_thinking_delta("evt-2", 2, "private reasoning two"),
        _step_text_delta("evt-3", 3, "public answer one"),
        _step_thinking_delta("evt-4", 4, "private reasoning three"),
        _step_text_delta("evt-5", 5, "public answer two"),
    ]

    snapshot = reduce_pipeline_events(events)

    message = snapshot["display"]["messages"][0]
    assert message["text"] == "public answer onepublic answer two"
    assert message["segments"] == [
        {"kind": "thinking"},
        {"kind": "text", "text": "public answer one"},
        {"kind": "thinking"},
        {"kind": "text", "text": "public answer two"},
    ]
    assert "private reasoning" not in json.dumps(snapshot)


def test_reduce_public_narrative_shape_survives_incremental_reduction() -> None:
    initial = reduce_pipeline_events(
        [
            _step_thinking_delta("evt-1", 1, "private one"),
            _step_text_delta("evt-2", 2, "public one"),
        ]
    )

    resumed = reduce_pipeline_events(
        [
            _step_thinking_delta("evt-3", 3, "private two"),
            _step_text_delta("evt-4", 4, "public two"),
        ],
        existing_snapshot=initial,
    )

    assert resumed["display"]["messages"][0]["segments"] == [
        {"kind": "thinking"},
        {"kind": "text", "text": "public one"},
        {"kind": "thinking"},
        {"kind": "text", "text": "public two"},
    ]


def test_reduce_legacy_message_keeps_marker_fallback_after_resume() -> None:
    existing = reduce_pipeline_events([])
    existing["display"]["messages"] = [
        {"scope": "step", "runId": "step-a-1", "round": 1, "text": "legacy public text"}
    ]

    resumed = reduce_pipeline_events(
        [
            _step_thinking_delta("evt-1", 1, "new private reasoning"),
            _step_text_delta("evt-2", 2, " plus new public text"),
        ],
        existing_snapshot=existing,
    )

    message = resumed["display"]["messages"][0]
    assert message["text"] == "legacy public text plus new public text"
    assert "segments" not in message
    assert "new private reasoning" not in json.dumps(resumed)


def test_reduce_breaks_the_paragraph_at_each_llm_turn_boundary() -> None:
    """Live, the tool run between two LLM turns opens a fresh text block.

    Replay has no tool segments to separate them, so without a break here the two
    turns render as one run-on paragraph ("best practicesResource schemas").
    """
    events = [
        _step_message_started("evt-1", 1),
        _step_text_delta("evt-2", 2, "restricted SSH access as per best practices"),
        _step_message_started("evt-3", 3, message_id="msg-2"),
        _step_text_delta("evt-4", 4, "Resource schemas confirmed."),
    ]

    snapshot = reduce_pipeline_events(events)

    messages = snapshot["display"]["messages"]
    assert len(messages) == 1
    assert messages[0]["text"] == (
        "restricted SSH access as per best practices\n\n<!-- iac-code:model-run -->\n\nResource schemas confirmed."
    )
    assert messages[0]["segments"] == [
        {"kind": "text", "text": "restricted SSH access as per best practices"},
        {"kind": "turn"},
        {"kind": "text", "text": "Resource schemas confirmed."},
    ]


def test_reduce_collapses_repeated_turn_boundaries_into_one_break() -> None:
    """A turn that only ran tools carries no text, so it adds no second break."""
    events = [
        _step_text_delta("evt-1", 1, "first turn"),
        _step_message_started("evt-2", 2, message_id="msg-2"),
        _step_message_started("evt-3", 3, message_id="msg-3"),
        _step_text_delta("evt-4", 4, "second turn"),
    ]

    snapshot = reduce_pipeline_events(events)

    assert [message["text"] for message in snapshot["display"]["messages"]] == [
        "first turn\n\n<!-- iac-code:model-run -->\n\nsecond turn"
    ]
    assert snapshot["display"]["messages"][0]["segments"] == [
        {"kind": "text", "text": "first turn"},
        {"kind": "turn"},
        {"kind": "text", "text": "second turn"},
    ]


def test_reduce_turn_boundary_before_any_text_creates_no_message() -> None:
    snapshot = reduce_pipeline_events([_step_message_started("evt-1", 1)])

    assert snapshot["display"]["messages"] == []


def test_reduce_turn_boundary_survives_incremental_reduction() -> None:
    """Reduction resumes from the stored snapshot, so the break must live in the text."""
    initial = reduce_pipeline_events([_step_text_delta("evt-1", 1, "first turn")])
    resumed = reduce_pipeline_events([_step_message_started("evt-2", 2, message_id="msg-2")], existing_snapshot=initial)

    final = reduce_pipeline_events([_step_text_delta("evt-3", 3, "second turn")], existing_snapshot=resumed)

    assert [message["text"] for message in final["display"]["messages"]] == [
        "first turn\n\n<!-- iac-code:model-run -->\n\nsecond turn"
    ]
    assert final["display"]["messages"][0]["segments"] == [
        {"kind": "text", "text": "first turn"},
        {"kind": "turn"},
        {"kind": "text", "text": "second turn"},
    ]


def test_reduce_preserves_turn_boundary_between_adjacent_thinking_segments() -> None:
    snapshot = reduce_pipeline_events(
        [
            _step_thinking_delta("evt-1", 1, "private first"),
            _step_message_started("evt-2", 2, message_id="msg-2"),
            _step_thinking_delta("evt-3", 3, "private second"),
        ]
    )

    assert snapshot["display"]["messages"][0]["segments"] == [
        {"kind": "thinking"},
        {"kind": "turn"},
        {"kind": "thinking"},
    ]
    assert "private" not in json.dumps(snapshot)


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
    candidate["data"] = {
        "conclusions": {"template": {"body": "ros"}},
        "step_conclusions": {"template_generating": {"body": "ros", "password": "real-secret"}},
    }
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
    assert step["candidates"][0]["step_conclusions"] == {
        "template_generating": {"body": "ros", "password": "real-secret"}
    }
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


def test_reduce_snapshot_preserves_progressive_candidate_batch_metadata() -> None:
    detail = _base("evt-1", 1, "candidate_detail_shown", scope="step")
    detail["step"] = {"id": "solution_planning_and_selection", "runId": "step-plan-1"}
    detail["data"] = {
        "detailId": "detail-outline-1-0",
        "candidateSetId": "outline-1",
        "candidateIndex": 0,
        "detailStage": "outline",
        "keyTradeoff": "成本最低，但没有高可用",
        "detail": {
            "candidateName": "单机方案",
            "candidateSetId": "outline-1",
            "candidateIndex": 0,
            "detailStage": "outline",
            "keyTradeoff": "成本最低，但没有高可用",
        },
    }
    diagram = _base("evt-2", 2, "diagram_shown", scope="step")
    diagram["step"] = detail["step"]
    diagram["data"] = {
        "diagramId": "diagram-outline-1-0",
        "candidateSetId": "outline-1",
        "candidateIndex": 0,
        "detailStage": "detail",
        "format": "mermaid",
        "mermaidSource": "flowchart TD",
    }

    snapshot = reduce_pipeline_events([detail, diagram])

    restored_detail = snapshot["display"]["candidateDetails"][0]
    restored_diagram = snapshot["display"]["diagrams"][0]
    assert restored_detail["candidateSetId"] == "outline-1"
    assert restored_detail["detailStage"] == "outline"
    assert restored_detail["keyTradeoff"] == "成本最低，但没有高可用"
    assert restored_detail["detail"]["keyTradeoff"] == "成本最低，但没有高可用"
    assert restored_diagram["candidateSetId"] == "outline-1"
    assert restored_diagram["detailStage"] == "detail"


def test_solution_first_canonical_conclusion_keeps_full_candidate_in_snapshot() -> None:
    completed = _base("evt-complete", 1, "step_completed", scope="step")
    completed["step"] = {
        "id": "solution_planning_and_selection",
        "runId": "step-plan-1",
        "attempt": 1,
    }
    candidate = {
        "candidate_id": "candidate-0",
        "name": "单机方案",
        "summary": "一台 ECS",
        "topology_graph": {
            "nodes": [{"id": "ecs", "label": "ECS", "product": "ECS"}],
            "edges": [],
        },
        "resource_inventory": [
            {
                "resource_id": "ecs",
                "product": "ECS",
                "purpose": "应用计算",
                "quantity": 1,
                "lifecycle": "create",
            }
        ],
        "rough_cost": {
            "currency": "CNY",
            "monthly_range": "¥100/月",
            "items": [{"name": "ECS", "spec": "ecs.e-c1m1.large", "monthly_cost": "¥100/月"}],
            "assumptions": ["杭州地域"],
            "exclusions": ["流量费"],
            "confidence": "medium",
        },
        "why_recommended": ["成本最低"],
        "problems_solved": ["快速上线"],
        "pros": ["简单", "便宜"],
        "cons": ["无高可用"],
    }
    completed["data"] = {
        "conclusionField": "solution_selection",
        "conclusion": {
            "status": "awaiting_selection",
            "candidate_set_id": "outline-1",
            "candidates": [candidate],
            "options": [{"name": "单机方案", "candidate_index": 0}],
        },
    }

    snapshot = reduce_pipeline_events([completed])

    conclusion = snapshot["steps"][0]["conclusion"]
    assert conclusion["candidate_set_id"] == "outline-1"
    assert conclusion["candidates"] == [candidate]


def test_reduce_permission_and_tool_result_display_items() -> None:
    permission = _base("evt-permission", 1, "permission_requested", scope="pipeline")
    permission["permission"] = {
        "permissionId": "perm-toolu-1",
        "toolName": "bash",
        "toolUseId": "toolu-1",
        "safeSummary": "bash permission request (fields: cmd)",
        "approved": True,
        "decision": "allow_once",
        "operation": {
            "product": "ROS",
            "action": "DeleteStack",
            "apiCalls": [{"product": "ROS", "action": "DeleteStack", "effect": "change"}],
        },
        "displayParameters": {"format": "json", "value": {"StackId": "stack-1"}},
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
    assert snapshot["display"]["permissions"][0]["operation"]["apiCalls"][0]["action"] == "DeleteStack"
    assert snapshot["display"]["permissions"][0]["displayParameters"]["value"] == {"StackId": "stack-1"}
    assert "toolInput" not in snapshot["display"]["permissions"][0]
    assert snapshot["display"]["toolResults"][0]["toolUseId"] == "toolu-1"
    assert snapshot["display"]["toolResults"][0]["result"] == {"stdout": "done"}


def test_reduce_tool_result_preserves_canonical_artifact_file_uri() -> None:
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
    assert artifact["metadata"]["byteSize"] == 10
    assert artifact["uri"] == r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"
    assert artifact["source"] == artifact["uri"]
    assert artifact["parts"][0]["url"] == artifact["uri"]
    rendered = str(snapshot)
    assert "file://" in rendered
    assert ".iac-code" in rendered


def test_reduce_tool_result_preserves_canonical_artifact_list() -> None:
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
    assert artifact[0] == legacy_uri
    assert artifact[1]["filename"] == r"C:\Users\alice\.iac-code\projects\demo\template.yaml"
    assert artifact[1]["uri"] == [legacy_uri]
    assert artifact[1]["parts"][0] == legacy_uri
    assert artifact[1]["parts"][1]["url"] == legacy_uri
    rendered = str(snapshot)
    assert "file://" in rendered
    assert ".iac-code" in rendered


def test_reduce_tool_result_preserves_canonical_root_list_artifact_payloads() -> None:
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
    assert result["api_key"] == "secret-key"
    assert result["artifacts"][0]["Content"] == "RAW-TEMPLATE-CONTENT"
    assert result["artifacts"][0]["metadata"]["api_key"] == "plain-secret"
    rendered = str(snapshot)
    assert "RAW-TEMPLATE-CONTENT" in rendered
    assert "plain-secret" in rendered
    assert "secret-key" in rendered
    assert "Alice and Bob" in rendered


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
    assert artifacts[0]["uri"] == "file:///tmp/top.yaml"
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


def test_reduce_final_artifact_supersedes_intermediate_for_same_candidate_path() -> None:
    parent = {"runId": "step-evaluate-1", "id": "evaluate", "index": 1, "total": 1, "attempt": 1}
    candidate_a = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    candidate_b = {"runId": "candidate-eval-1-1", "id": "eval", "index": 1, "attempt": 1}
    generated_step_a = {
        "runId": "candidate-eval-0-1-template_generating-1",
        "id": "template_generating",
        "index": 1,
        "total": 2,
        "attempt": 1,
    }
    review_step_a = {
        "runId": "candidate-eval-0-1-reviewing-1",
        "id": "reviewing",
        "index": 2,
        "total": 2,
        "attempt": 1,
    }
    generated_step_b = {
        "runId": "candidate-eval-1-1-template_generating-1",
        "id": "template_generating",
        "index": 1,
        "total": 2,
        "attempt": 1,
    }
    generated_a = _base("evt-generated-a", 1, "artifact_created", scope="candidate_step")
    generated_a["step"] = parent
    generated_a["candidate"] = candidate_a
    generated_a["candidateStep"] = generated_step_a
    generated_a["artifact"] = {
        "artifactId": "generated-a",
        "filename": "main.yaml",
        "role": "intermediate",
        "supersedesPath": "templates/main.yaml",
        "content": "generated A",
    }
    generated_b = _base("evt-generated-b", 2, "artifact_created", scope="candidate_step")
    generated_b["step"] = parent
    generated_b["candidate"] = candidate_b
    generated_b["candidateStep"] = generated_step_b
    generated_b["artifact"] = {
        "artifactId": "generated-b",
        "filename": "main.yaml",
        "role": "intermediate",
        "supersedesPath": "templates/main.yaml",
        "content": "generated B",
    }
    reviewed_a = _base("evt-reviewed-a", 3, "artifact_created", scope="candidate_step")
    reviewed_a["step"] = parent
    reviewed_a["candidate"] = candidate_a
    reviewed_a["candidateStep"] = review_step_a
    reviewed_a["artifact"] = {
        "artifactId": "reviewed-a",
        "filename": "main.yaml",
        "role": "final",
        "supersedesPath": "templates/main.yaml",
        "content": "reviewed A",
    }

    snapshot = reduce_pipeline_events([generated_a, generated_b, reviewed_a])

    assert len(snapshot["display"]["artifacts"]) == 2
    artifacts = {artifact["candidate"]["runId"]: artifact for artifact in snapshot["display"]["artifacts"]}
    assert set(artifacts) == {"candidate-eval-0-1", "candidate-eval-1-1"}
    assert artifacts["candidate-eval-0-1"]["artifactId"] == "reviewed-a"
    assert artifacts["candidate-eval-0-1"]["role"] == "final"
    assert artifacts["candidate-eval-0-1"]["sequence"] == 3
    assert artifacts["candidate-eval-0-1"]["candidateStep"]["id"] == "reviewing"
    assert artifacts["candidate-eval-1-1"]["artifactId"] == "generated-b"
    assert artifacts["candidate-eval-1-1"]["role"] == "intermediate"


def test_reduce_artifact_replacement_prefers_stable_supersedes_key_over_public_path() -> None:
    candidate = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    key_a = "sha256:1111111111111111"
    key_b = "sha256:2222222222222222"
    generated_a = _base("evt-generated-a", 1, "artifact_created", scope="candidate")
    generated_a["candidate"] = candidate
    generated_a["artifact"] = {
        "artifactId": "generated-a",
        "filename": "a.yaml",
        "role": "intermediate",
        "supersedesPath": "[PATH]",
        "supersedesKey": key_a,
    }
    generated_b = _base("evt-generated-b", 2, "artifact_created", scope="candidate")
    generated_b["candidate"] = candidate
    generated_b["artifact"] = {
        "artifactId": "generated-b",
        "filename": "b.yaml",
        "role": "intermediate",
        "supersedesPath": "[PATH]",
        "supersedesKey": key_b,
    }
    reviewed_a = _base("evt-reviewed-a", 3, "artifact_created", scope="candidate")
    reviewed_a["candidate"] = candidate
    reviewed_a["artifact"] = {
        "artifactId": "reviewed-a",
        "filename": "a.yaml",
        "role": "final",
        "supersedesPath": "[PATH]",
        "supersedesKey": key_a,
    }

    snapshot = reduce_pipeline_events([generated_a, generated_b, reviewed_a])

    artifacts = {artifact["supersedesKey"]: artifact for artifact in snapshot["display"]["artifacts"]}
    assert set(artifacts) == {key_a, key_b}
    assert artifacts[key_a]["artifactId"] == "reviewed-a"
    assert artifacts[key_a]["role"] == "final"
    assert artifacts[key_b]["artifactId"] == "generated-b"
    assert artifacts[key_b]["role"] == "intermediate"


def test_reduce_generated_template_remains_final_when_review_is_disabled() -> None:
    artifact = _base("evt-generated", 1, "artifact_created", scope="candidate")
    artifact["candidate"] = {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1}
    artifact["artifact"] = {
        "artifactId": "generated",
        "filename": "main.yaml",
        "role": "final",
        "supersedesPath": "templates/main.yaml",
    }

    snapshot = reduce_pipeline_events([artifact])

    assert snapshot["display"]["artifacts"] == [
        {
            "artifactId": "generated",
            "filename": "main.yaml",
            "role": "final",
            "supersedesPath": "templates/main.yaml",
            "id": "generated",
            "scope": "candidate",
            "runId": "candidate-eval-0-1",
            "sequence": 1,
            "createdAt": "2026-06-08T10:00:00Z",
            "eventId": "evt-generated",
            "candidate": {"runId": "candidate-eval-0-1", "id": "eval", "index": 0, "attempt": 1},
        }
    ]


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


def test_store_preserves_canonical_cleanup_fields_and_input_prompt(tmp_path) -> None:
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
    assert loaded["control"]["handoffHistory"][0]["data"]["cleanup"]["prompt"] == "hidden cleanup prompt"
    assert loaded["control"]["handoffHistory"][0]["data"]["cleanup"]["lastError"] == raw_error
    assert loaded["normalHandoff"]["data"]["cleanup"]["ledgerPath"] == "/tmp/cleanup.yaml"
    assert loaded["normalHandoff"]["data"]["cleanup"]["lastError"] == raw_error
    assert loaded["cleanup"]["prompt"] == "hidden cleanup prompt"
    assert loaded["cleanup"]["last_error"] == raw_error
    assert loaded["cleanup"]["resources"][0]["lastError"] == raw_error
    assert loaded["cleanup"]["history"][0]["data"]["ledgerPath"] == "/tmp/cleanup.yaml"
    assert loaded["cleanup"]["history"][0]["data"]["lastError"] == raw_error
    rendered = json.dumps(loaded, ensure_ascii=False)
    assert "super-secret" in rendered
    assert "sk-live-1234567890" in rendered
    assert "/Users/alice" in rendered

    store.path.write_text(json.dumps(snapshot), encoding="utf-8")
    loaded = store.load()
    assert loaded is not None
    assert loaded["pendingInput"]["prompt"] == "choose deployment target"
    assert loaded["normalHandoff"]["data"]["cleanup"]["prompt"] == "hidden cleanup prompt"
    assert loaded["cleanup"]["ledgerPath"] == "/tmp/cleanup.yaml"
    rendered = json.dumps(loaded, ensure_ascii=False)
    assert "super-secret" in rendered
    assert "sk-live-1234567890" in rendered
    assert "/Users/alice" in rendered


def test_store_returns_none_for_invalid_utf8_snapshot(tmp_path) -> None:
    store = A2APipelineSnapshotStore(tmp_path / "pipeline")
    store.pipeline_dir.mkdir(parents=True)
    store.path.write_bytes(b"\xff\xfe\x00")

    assert store.load() is None


def test_snapshot_schema_version_is_exported() -> None:
    assert SNAPSHOT_SCHEMA_VERSION == "1.2"
    assert "SNAPSHOT_SCHEMA_VERSION" in pipeline_snapshot.__all__
