from __future__ import annotations

import json
import re
import time

import pytest

from iac_code.a2a.pipeline_events import PIPELINE_EVENTS_EXTENSION_URI, PipelineA2AContext, PipelineEventTranslator
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import (
    CandidateDetailEvent,
    DiagramEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
)


def _ctx() -> PipelineA2AContext:
    return PipelineA2AContext(
        pipeline_run_id="ctx-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
        parent_step_order=["intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select"],
        candidate_step_order=["template_generating", "cost_estimating", "reviewing"],
    )


def test_pipeline_started_has_stable_envelope() -> None:
    translator = PipelineEventTranslator(_ctx())
    event = PipelineEvent(
        type=PipelineEventType.PIPELINE_STARTED,
        step_id=None,
        timestamp=1717821600.0,
        data={"total_steps": 4, "step_names": ["intent_parsing", "architecture_planning"]},
    )

    envelopes = translator.translate(event)

    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["extensionUri"] == PIPELINE_EVENTS_EXTENSION_URI
    assert re.fullmatch(r"evt-[0-9a-f]{32}", envelope["eventId"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", envelope["createdAt"])
    assert envelope["eventType"] == "pipeline_started"
    assert envelope["scope"] == "pipeline"
    assert envelope["sequence"] == 1
    assert envelope["pipelineRunId"] == "ctx-1"
    assert envelope["taskId"] == "task-1"
    assert envelope["contextId"] == "ctx-1"
    assert envelope["pipelineName"] == "selling"
    assert envelope["status"] == "working"
    assert envelope["data"]["totalSteps"] == 4


def test_manual_cleanup_event_normalizes_cleanup_data_keys() -> None:
    translator = PipelineEventTranslator(_ctx())

    event = translator.manual_event(
        "cleanup_started",
        "cleanup",
        data={
            "resource_count": 1,
            "status_message": "检测到 1 个回滚残留资源，开始清理流程。",
            "resource_id": "stack-123",
            "region_id": "cn-hangzhou",
            "stack_status": "DELETE_IN_PROGRESS",
            "cleanup_tool_use_id": "toolu-get",
            "progress_percentage": 60,
            "last_error": "DELETE_FAILED",
        },
    )

    assert event["eventType"] == "cleanup_started"
    assert event["scope"] == "cleanup"
    assert event["data"]["resourceCount"] == 1
    assert event["data"]["statusMessage"] == "检测到 1 个回滚残留资源，开始清理流程。"
    assert event["data"]["resourceId"] == "stack-123"
    assert event["data"]["regionId"] == "cn-hangzhou"
    assert event["data"]["stackStatus"] == "DELETE_IN_PROGRESS"
    assert event["data"]["cleanupToolUseId"] == "toolu-get"
    assert event["data"]["progressPercentage"] == 60
    assert event["data"]["lastError"] == "DELETE_FAILED"


def test_parent_step_attempt_increments_after_rollback() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.ROLLBACK_TRIGGERED,
            step_id="deploying",
            timestamp=time.time(),
            data={"from_step": "deploying", "to_step": "architecture_planning", "reason": "change", "stale_fields": []},
        )
    )

    envelopes = translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="architecture_planning",
            timestamp=time.time(),
            data={"index": 2, "total": 4, "step_type": "agent", "ui_mode": "default"},
        )
    )

    assert envelopes[0]["step"]["runId"] == "step-architecture_planning-2"
    assert envelopes[0]["step"]["attempt"] == 2


def test_rollback_event_sanitizes_reason_before_a2a_metadata() -> None:
    translator = PipelineEventTranslator(_ctx())
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    [envelope] = translator.translate(
        PipelineEvent(
            type=PipelineEventType.ROLLBACK_TRIGGERED,
            step_id="deploying",
            timestamp=time.time(),
            data={
                "from_step": "deploying",
                "to_step": "architecture_planning",
                "reason": (
                    f"Authorization: Bearer sk-live-secret at /Users/alice/.iac-code/settings.yml and {malformed_uri}"
                ),
                "stale_fields": [],
            },
        )
    )

    rendered = json.dumps(envelope, ensure_ascii=False)
    assert "sk-live-secret" not in rendered
    assert "/Users/alice" not in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


def test_failure_event_sanitizes_nested_error_details_and_normalizes_error_id() -> None:
    translator = PipelineEventTranslator(_ctx())
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    [envelope] = translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_FAILED,
            step_id="deploying",
            timestamp=time.time(),
            data={
                "error": "failed",
                "error_details": {
                    "type": "RuntimeError",
                    "error_id": "err-abc123",
                    "traceback": (
                        "Traceback at /Users/alice/.iac-code/settings.yml with SECRET_TOKEN=tok-secret "
                        f"and {malformed_uri}"
                    ),
                },
            },
        )
    )

    rendered = json.dumps(envelope, ensure_ascii=False)
    assert "/Users/alice" not in rendered
    assert "tok-secret" not in rendered
    assert envelope["data"]["errorDetails"]["errorId"] == "err-abc123"
    assert "error_id" not in envelope["data"]["errorDetails"]
    assert "[PATH]" in envelope["data"]["errorDetails"]["traceback"]
    assert "iac-code-artifac[PATH]" not in rendered


def test_parent_step_coordinate_respects_explicit_attempt_from_pipeline_event() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelope = translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="confirm_and_select",
            timestamp=time.time(),
            data={"index": 4, "total": 5, "attempt": 2},
        )
    )[0]

    assert envelope["step"]["runId"] == "step-confirm_and_select-2"
    assert envelope["step"]["attempt"] == 2


def test_translator_hydrates_parent_step_attempts_from_prior_events() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.hydrate_from_events(
        [
            {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "eventType": "input_required",
                "sequence": 12,
                "step": {"id": "confirm_and_select", "runId": "step-confirm_and_select-2", "attempt": 2},
            }
        ]
    )

    envelope = translator.translate(
        PipelineEvent(
            type=PipelineEventType.USER_INPUT_RECEIVED,
            step_id="confirm_and_select",
            timestamp=time.time(),
            data={"selected_value": "已有VPC下新建VSwitch"},
        )
    )[0]

    assert envelope["step"]["runId"] == "step-confirm_and_select-2"
    assert envelope["step"]["attempt"] == 2


def test_translator_hydrates_active_candidate_step_from_prior_events() -> None:
    first = PipelineEventTranslator(_ctx())
    events: list[dict] = []
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="evaluate_candidates",
                timestamp=time.time(),
                data={"index": 3, "total": 5},
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "candidate_name": "low cost",
                    "sub_pipeline_name": "evaluate_candidate",
                    "total_steps": 3,
                    "parent_step_id": "evaluate_candidates",
                },
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_STEP_STARTED,
                step_id="template_generating",
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "step_id": "template_generating",
                    "step_index": 0,
                    "total_steps": 3,
                },
            )
        )
    )

    restored = PipelineEventTranslator(_ctx())
    restored.hydrate_from_events(events)
    envelope = restored.translate(
        SubPipelineStreamEvent(
            sub_pipeline_id="evaluate_candidate_abcd",
            candidate_index=0,
            inner=TextDeltaEvent(text="after restart"),
        )
    )[0]

    assert envelope["scope"] == "candidate_step"
    assert envelope["candidate"]["runId"] == "candidate-evaluate_candidate_abcd-0-1"
    assert envelope["candidateStep"]["runId"] == "candidate-evaluate_candidate_abcd-0-1-template_generating-1"


def test_translator_hydrates_candidate_attempt_after_restart_request() -> None:
    first = PipelineEventTranslator(_ctx())
    events: list[dict] = []
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="evaluate_candidates",
                timestamp=time.time(),
                data={"index": 3, "total": 5},
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "candidate_name": "low cost",
                    "sub_pipeline_name": "evaluate_candidate",
                    "total_steps": 3,
                    "parent_step_id": "evaluate_candidates",
                },
            )
        )
    )
    events.extend(
        first.candidate_restart_events(
            candidate_scope="candidate:0",
            target_candidate_step_id="template_generating",
            reason="try cheaper",
        )
    )

    restored = PipelineEventTranslator(_ctx())
    restored.hydrate_from_events(events)
    envelope = restored.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "total_steps": 3,
                "parent_step_id": "evaluate_candidates",
            },
        )
    )[0]

    assert envelope["candidate"]["runId"] == "candidate-evaluate_candidate_abcd-0-2"
    assert envelope["candidate"]["attempt"] == 2


def test_translator_hydrates_candidate_step_attempts_without_losing_same_attempt_state() -> None:
    first = PipelineEventTranslator(_ctx())
    events: list[dict] = []
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="evaluate_candidates",
                timestamp=time.time(),
                data={"index": 3, "total": 5},
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "candidate_name": "low cost",
                    "sub_pipeline_name": "evaluate_candidate",
                    "total_steps": 3,
                    "parent_step_id": "evaluate_candidates",
                },
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_STEP_STARTED,
                step_id="template_generating",
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "step_id": "template_generating",
                    "step_index": 0,
                    "total_steps": 3,
                },
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_STEP_FAILED,
                step_id="template_generating",
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "step_id": "template_generating",
                    "step_index": 0,
                    "total_steps": 3,
                },
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_STEP_STARTED,
                step_id="template_generating",
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "step_id": "template_generating",
                    "step_index": 0,
                    "total_steps": 3,
                },
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_STEP_COMPLETED,
                step_id="template_generating",
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "step_id": "template_generating",
                    "step_index": 0,
                    "total_steps": 3,
                },
            )
        )
    )
    events.extend(
        first.translate(
            PipelineEvent(
                type=PipelineEventType.SUB_STEP_STARTED,
                step_id="cost_analysis",
                timestamp=time.time(),
                data={
                    "sub_pipeline_id": "evaluate_candidate_abcd",
                    "candidate_index": 0,
                    "step_id": "cost_analysis",
                    "step_index": 1,
                    "total_steps": 3,
                },
            )
        )
    )

    restored = PipelineEventTranslator(_ctx())
    restored.hydrate_from_events(events)
    [restart] = restored.candidate_restart_events(
        candidate_scope="candidate:0",
        target_candidate_step_id="template_generating",
        reason="retry template",
    )

    assert restart["candidate"]["runId"] == "candidate-evaluate_candidate_abcd-0-1"
    assert restart["candidateStep"]["runId"] == "candidate-evaluate_candidate_abcd-0-1-template_generating-2"


def test_candidate_stream_text_has_parent_and_candidate_coordinates() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "total_steps": 3,
                "parent_step_id": "evaluate_candidates",
            },
        )
    )

    envelopes = translator.translate(
        SubPipelineStreamEvent(
            sub_pipeline_id="evaluate_candidate_abcd",
            candidate_index=0,
            inner=TextDeltaEvent(text="hello"),
        )
    )

    assert envelopes[0]["eventType"] == "text_delta"
    assert envelopes[0]["scope"] == "candidate"
    assert envelopes[0]["step"]["id"] == "evaluate_candidates"
    assert envelopes[0]["candidate"]["runId"] == "candidate-evaluate_candidate_abcd-0-1"
    assert envelopes[0]["candidate"]["index"] == 0
    assert envelopes[0]["data"]["text"] == "hello"


def test_candidate_started_includes_candidate_step_skeleton() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelope = translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "total_steps": 3,
                "parent_step_id": "evaluate_candidates",
            },
        )
    )[0]

    assert envelope["data"]["totalSteps"] == 3
    assert envelope["candidate"]["totalSteps"] == 3
    assert envelope["candidate"]["steps"] == [
        {
            "id": "template_generating",
            "name": "template_generating",
            "runId": "candidate-evaluate_candidate_abcd-0-1-template_generating-1",
            "attempt": 1,
            "index": 1,
            "total": 3,
            "status": "pending",
        },
        {
            "id": "cost_estimating",
            "name": "cost_estimating",
            "runId": "candidate-evaluate_candidate_abcd-0-1-cost_estimating-1",
            "attempt": 1,
            "index": 2,
            "total": 3,
            "status": "pending",
        },
        {
            "id": "reviewing",
            "name": "reviewing",
            "runId": "candidate-evaluate_candidate_abcd-0-1-reviewing-1",
            "attempt": 1,
            "index": 3,
            "total": 3,
            "status": "pending",
        },
    ]


def test_step_completed_data_keeps_conclusion_and_conclusion_field() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelope = translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_COMPLETED,
            step_id="intent_parsing",
            timestamp=time.time(),
            data={
                "duration_s": 1.25,
                "conclusion_field": "intent",
                "conclusion": {"is_infra_intent": True},
            },
        )
    )[0]

    assert envelope["data"]["durationS"] == 1.25
    assert envelope["data"]["conclusionField"] == "intent"
    assert envelope["data"]["conclusion"] == {"is_infra_intent": True}


def test_completion_artifact_windows_path_does_not_leak_in_filename() -> None:
    context = _ctx()
    context.a2a_artifacts_by_step_id = {
        "reviewing": [{"path": "conclusion.file_path", "content": "conclusion.content"}]
    }
    translator = PipelineEventTranslator(context)

    envelopes = translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_COMPLETED,
            step_id="reviewing",
            timestamp=time.time(),
            data={
                "conclusion_field": "review",
                "conclusion": {
                    "file_path": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
                    "content": "ROSTemplate",
                },
            },
        )
    )

    artifact = envelopes[1]["artifact"]
    rendered = str(artifact)
    assert artifact["filename"] == "template.yaml"
    assert r"C:\\" not in rendered
    assert "%5CUsers" not in rendered
    assert ".iac-code" not in rendered


def test_candidate_step_failure_keeps_global_task_status_working() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "parent_step_id": "evaluate_candidates",
            },
        )
    )

    envelope = translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_FAILED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "step_id": "template_generating",
                "error_summary": "template failed",
            },
        )
    )[0]

    assert envelope["eventType"] == "candidate_step_failed"
    assert envelope["status"] == "working"


def test_candidate_failure_keeps_global_task_status_working() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "parent_step_id": "evaluate_candidates",
            },
        )
    )

    envelope = translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data={"sub_pipeline_id": "evaluate_candidate_abcd", "candidate_index": 0, "failed": True},
        )
    )[0]

    assert envelope["eventType"] == "candidate_failed"
    assert envelope["status"] == "working"


def test_candidate_detail_and_diagram_have_distinct_event_types() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "total_steps": 3,
                "parent_step_id": "evaluate_candidates",
            },
        )
    )

    detail = translator.translate(
        SubPipelineStreamEvent(
            sub_pipeline_id="evaluate_candidate_abcd",
            candidate_index=0,
            inner=CandidateDetailEvent(
                tool_use_id="toolu-1",
                candidate_name="low cost",
                summary="single ecs",
                cost_items=[],
                total_monthly_cost="CNY 60",
                candidate_index=0,
            ),
        )
    )[0]
    diagram = translator.translate(
        SubPipelineStreamEvent(
            sub_pipeline_id="evaluate_candidate_abcd",
            candidate_index=0,
            inner=DiagramEvent(
                candidate_name="low cost",
                template_content="ROSTemplateFormatVersion: '2015-09-01'",
                mermaid_source="graph TD",
                candidate_index=0,
            ),
        )
    )[0]

    assert detail["eventType"] == "candidate_detail_shown"
    assert detail["data"]["detailId"] == "detail-toolu-1"
    assert detail["data"]["candidateIndex"] == 0
    assert detail["data"]["detail"] == {
        "candidateName": "low cost",
        "candidateIndex": 0,
        "summary": "single ecs",
        "costItems": [],
        "totalMonthlyCost": "CNY 60",
    }
    assert diagram["eventType"] == "diagram_shown"
    assert diagram["data"]["format"] == "mermaid"
    assert diagram["data"]["candidateIndex"] == 0


def test_top_level_candidate_detail_is_attached_to_current_step() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="confirm_and_select",
            timestamp=time.time(),
            data={"index": 4, "total": 4},
        )
    )

    envelopes = translator.translate(
        CandidateDetailEvent(
            tool_use_id="toolu-detail",
            candidate_name="low cost",
            summary="single ecs",
            cost_items=[{"name": "ecs", "monthly_cost": "CNY 60"}],
            total_monthly_cost="CNY 60",
        )
    )

    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope["eventType"] == "candidate_detail_shown"
    assert envelope["scope"] == "step"
    assert envelope["step"]["id"] == "confirm_and_select"
    assert envelope["data"] == {
        "detailId": "detail-toolu-detail",
        "toolUseId": "toolu-detail",
        "detail": {
            "candidateName": "low cost",
            "summary": "single ecs",
            "costItems": [{"name": "ecs", "monthly_cost": "CNY 60"}],
            "totalMonthlyCost": "CNY 60",
        },
    }


def test_show_candidate_detail_tool_result_recovers_detail_from_tool_input() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="confirm_and_select",
            timestamp=time.time(),
            data={"index": 4, "total": 4},
        )
    )
    translator.translate(
        ToolUseEndEvent(
            tool_use_id="toolu-detail",
            name="show_candidate_detail",
            input={
                "candidate_name": "low cost",
                "summary": "single ecs",
                "cost_items": [{"name": "ecs", "monthly_cost": "CNY 60"}],
                "total_monthly_cost": "CNY 60",
            },
        )
    )

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-detail",
            tool_name="show_candidate_detail",
            result="已展示「low cost」的方案详情。",
            is_error=False,
        )
    )

    assert [envelope["eventType"] for envelope in envelopes] == ["candidate_detail_shown", "tool_result"]
    detail_event = envelopes[0]
    assert detail_event["scope"] == "step"
    assert detail_event["step"]["id"] == "confirm_and_select"
    assert detail_event["data"]["detail"]["candidateName"] == "low cost"
    assert detail_event["data"]["detail"]["costItems"] == [{"name": "ecs", "monthly_cost": "CNY 60"}]


@pytest.mark.parametrize(
    ("stream_event", "event_type"),
    [
        (TextDeltaEvent(text="开始部署资源"), "text_delta"),
        (
            ToolResultEvent(
                tool_use_id="toolu-read",
                tool_name="read_file",
                result="template content",
                is_error=False,
            ),
            "tool_result",
        ),
        (
            PermissionRequestEvent(
                tool_name="ros_stack",
                tool_input={"action": "CreateStack"},
                tool_use_id="toolu-stack",
            ),
            "permission_requested",
        ),
    ],
)
def test_parent_stream_events_include_current_step_coordinate(stream_event: object, event_type: str) -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="deploying",
            timestamp=time.time(),
            data={"index": 5, "total": 5},
        )
    )

    [envelope] = translator.translate(stream_event)

    assert envelope["eventType"] == event_type
    assert envelope["scope"] == "step"
    assert envelope["step"]["id"] == "deploying"
    assert envelope["step"]["runId"] == "step-deploying-1"


def test_stack_current_changed_is_disabled_by_default() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        ToolUseEndEvent(
            tool_use_id="toolu-stack",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "CreateStack",
                "region_id": "cn-hangzhou",
                "params": {"StackName": "demo"},
            },
        )
    )

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-stack",
            tool_name="aliyun_api",
            result=json.dumps({"StackId": "stack-123", "RequestId": "req-1"}),
            is_error=False,
        )
    )

    assert [envelope["eventType"] for envelope in envelopes] == ["tool_result"]


def test_failed_tool_result_payload_is_sanitized() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-secret",
            tool_name="bash",
            result="Tool failed: DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml",
            is_error=True,
        )
    )

    assert [envelope["eventType"] for envelope in envelopes] == ["tool_result"]
    rendered = str(envelopes[0]["data"]["result"])
    assert "hunter2" not in rendered
    assert "/Users/alice" not in rendered


def test_tool_result_keeps_valid_opaque_artifact_uri() -> None:
    translator = PipelineEventTranslator(_ctx())
    uri = "iac-code-artifact://artifact-1/template.yaml"

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-artifact",
            tool_name="write_file",
            result={"artifact": {"filename": "template.yaml", "uri": uri, "parts": [{"url": uri}]}},
            is_error=False,
        )
    )

    assert [envelope["eventType"] for envelope in envelopes] == ["tool_result"]
    artifact = envelopes[0]["data"]["result"]["artifact"]
    assert artifact["uri"] == uri
    assert artifact["parts"][0]["url"] == uri
    assert "iac-code-artifac[PATH]" not in json.dumps(envelopes[0])


def test_tool_result_redacts_malformed_opaque_uri_outside_artifact_field() -> None:
    translator = PipelineEventTranslator(_ctx())
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-artifact",
            tool_name="write_file",
            result={"note": f"see {malformed_uri}"},
            is_error=False,
        )
    )

    rendered = json.dumps(envelopes[0], ensure_ascii=False)
    assert "[PATH]" in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


def test_tool_result_sanitizes_root_artifact_list_payloads() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-artifact",
            tool_name="write_file",
            result=[
                {
                    "artifact": {
                        "filename": "template.yaml",
                        "Content": "RAW-TEMPLATE-CONTENT",
                        "metadata": {"token": "plain-token"},
                        "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
                    }
                }
            ],
            is_error=False,
        )
    )

    rendered = json.dumps(envelopes[0], ensure_ascii=False)
    artifact = envelopes[0]["data"]["result"][0]["artifact"]
    assert artifact == {"filename": "template.yaml", "metadata": {"token": "[REDACTED]"}}
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "plain-token" not in rendered
    assert "Alice and Bob" not in rendered
    assert ".iac-code" not in rendered


def test_failed_tool_result_dict_artifact_payload_is_sanitized() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-artifact",
            tool_name="write_file",
            result={
                "Artifact": {
                    "filename": "template.yaml",
                    "Content": "RAW-TEMPLATE-CONTENT",
                    "Raw": "RAW",
                    "metadata": {"api_key": "plain-secret"},
                },
                "api_key": "secret-key",
            },
            is_error=True,
        )
    )

    rendered = json.dumps(envelopes[0], ensure_ascii=False)
    assert envelopes[0]["data"]["result"] == {
        "Artifact": {"filename": "template.yaml", "metadata": {"api_key": "[REDACTED]"}},
        "api_key": "[REDACTED]",
    }
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "plain-secret" not in rendered
    assert "secret-key" not in rendered


def test_stack_current_changed_emits_after_successful_ros_create_stack() -> None:
    ctx = _ctx()
    ctx.emit_stack_events = True
    translator = PipelineEventTranslator(ctx)
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="deploying",
            timestamp=time.time(),
            data={"index": 5, "total": 5},
        )
    )
    translator.translate(
        ToolUseEndEvent(
            tool_use_id="toolu-stack",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "CreateStack",
                "region_id": "cn-hangzhou",
                "params": {"StackName": "demo"},
            },
        )
    )

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-stack",
            tool_name="aliyun_api",
            result=json.dumps({"StackId": "stack-123", "RequestId": "req-1"}),
            is_error=False,
        )
    )

    stack_event = envelopes[0]
    assert [envelope["eventType"] for envelope in envelopes] == ["stack_current_changed", "tool_result"]
    assert stack_event["scope"] == "stack"
    assert stack_event["step"]["id"] == "deploying"
    assert stack_event["data"] == {
        "toolName": "aliyun_api",
        "toolUseId": "toolu-stack",
        "provider": "ros",
        "action": "CreateStack",
        "regionId": "cn-hangzhou",
        "stackId": "stack-123",
        "stackName": "demo",
        "isSuccess": True,
        "current": True,
    }


def test_stack_current_changed_keeps_current_stack_after_statusless_successful_delete() -> None:
    ctx = _ctx()
    ctx.emit_stack_events = True
    translator = PipelineEventTranslator(ctx)
    translator.translate(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123", "StackName": "demo"},
            },
        )
    )

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "stack_name": "demo", "is_success": True}),
            is_error=False,
        )
    )

    stack_event = envelopes[0]
    assert stack_event["eventType"] == "stack_current_changed"
    assert stack_event["data"]["action"] == "DeleteStack"
    assert stack_event["data"]["stackId"] == "stack-123"
    assert stack_event["data"]["stackStatus"] == "DELETE_REQUESTED"
    assert stack_event["data"]["current"] is True
    assert "cleared" not in stack_event["data"]


def test_stack_current_changed_clears_current_stack_after_delete_complete() -> None:
    ctx = _ctx()
    ctx.emit_stack_events = True
    translator = PipelineEventTranslator(ctx)
    translator.translate(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123", "StackName": "demo"},
            },
        )
    )

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps(
                {
                    "stack_id": "stack-123",
                    "stack_name": "demo",
                    "status": "DELETE_COMPLETE",
                    "is_success": True,
                }
            ),
            is_error=False,
        )
    )

    stack_event = envelopes[0]
    assert stack_event["eventType"] == "stack_current_changed"
    assert stack_event["data"]["action"] == "DeleteStack"
    assert stack_event["data"]["stackId"] == "stack-123"
    assert stack_event["data"]["stackStatus"] == "DELETE_COMPLETE"
    assert stack_event["data"]["current"] is False
    assert stack_event["data"]["cleared"] is True


def test_stack_current_changed_keeps_current_stack_id_from_failed_create_result() -> None:
    ctx = _ctx()
    ctx.emit_stack_events = True
    translator = PipelineEventTranslator(ctx)
    translator.translate(
        ToolUseEndEvent(
            tool_use_id="toolu-stack",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "CreateStack",
                "region_id": "cn-hangzhou",
                "params": {"StackName": "demo"},
            },
        )
    )

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-stack",
            tool_name="aliyun_api",
            result=json.dumps({"StackId": "stack-123", "Message": "validation failed", "is_success": False}),
            is_error=True,
        )
    )

    stack_event = envelopes[0]
    assert [envelope["eventType"] for envelope in envelopes] == ["stack_current_changed", "tool_result"]
    assert stack_event["data"]["action"] == "CreateStack"
    assert stack_event["data"]["stackId"] == "stack-123"
    assert stack_event["data"]["isSuccess"] is False
    assert stack_event["data"]["current"] is True


def test_stack_current_changed_does_not_clear_current_stack_after_failed_delete() -> None:
    ctx = _ctx()
    ctx.emit_stack_events = True
    translator = PipelineEventTranslator(ctx)
    translator.translate(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123", "StackName": "demo"},
            },
        )
    )

    envelopes = translator.translate(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "stack_name": "demo", "is_success": False}),
            is_error=True,
        )
    )

    stack_event = envelopes[0]
    assert stack_event["eventType"] == "stack_current_changed"
    assert stack_event["data"]["action"] == "DeleteStack"
    assert stack_event["data"]["stackId"] == "stack-123"
    assert stack_event["data"]["isSuccess"] is False
    assert stack_event["data"]["current"] is True
    assert "cleared" not in stack_event["data"]


def test_permission_request_metadata_redacts_and_truncates_tool_input_size_and_depth() -> None:
    nested: object = "leaf"
    for _ in range(80):
        nested = {"next": nested}
    translator = PipelineEventTranslator(_ctx())

    envelope = translator.translate(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "x" * 5000, "api_key": "secret-value", "nested": nested},
            tool_use_id="toolu-1",
        )
    )[0]

    assert envelope["permission"]["safeSummary"] == "bash permission request (fields: [redacted], cmd, nested)"
    tool_input = envelope["permission"]["toolInput"]
    assert len(tool_input["cmd"]) == 4000
    assert tool_input["api_key"] == "[redacted]"
    current = tool_input["nested"]
    while isinstance(current, dict):
        current = current["next"]
    assert current == "[truncated-depth]"
    assert "secret-value" not in str(envelope)


def test_permission_request_metadata_redacts_secret_strings_in_safe_keys() -> None:
    translator = PipelineEventTranslator(_ctx())
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"
    encoded_path = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"

    envelope = translator.translate(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={
                "cmd": (
                    f"cat /Users/alice/.iac-code/settings.yml && cat {malformed_uri} && cat {encoded_path} "
                    '&& curl -H "Authorization: Bearer sk-live-secret"'
                )
            },
            tool_use_id="toolu-1",
        )
    )[0]

    tool_input = envelope["permission"]["toolInput"]
    assert "sk-live-secret" not in str(tool_input)
    assert "Authorization: Bearer" not in str(tool_input)
    assert "/Users/alice" not in str(tool_input)
    assert "[REDACTED]" in str(tool_input)
    assert "[PATH]" in str(tool_input)
    assert "iac-code-artifac[PATH]" not in str(tool_input)
    assert "%2FUsers" not in str(tool_input)
    assert "Users" not in str(tool_input)


@pytest.mark.parametrize("sensitive_key", ["pwd", "passphrase", "auth", "cookie", "session", "session_id"])
def test_permission_request_metadata_redacts_common_sensitive_key_aliases(sensitive_key: str) -> None:
    translator = PipelineEventTranslator(_ctx())

    envelope = translator.translate(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={sensitive_key: "secret-value", "nested": [{"Authorization": "Bearer secret-value"}]},
            tool_use_id="toolu-1",
        )
    )[0]

    tool_input = envelope["permission"]["toolInput"]
    assert tool_input[sensitive_key] == "[redacted]"
    assert tool_input["nested"][0]["Authorization"] == "[redacted]"
    assert "secret-value" not in str(envelope)


def test_permission_request_safe_summary_caps_field_names() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelope = translator.translate(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={f"field_{index:02d}": index for index in range(25)},
            tool_use_id="toolu-1",
        )
    )[0]

    assert envelope["permission"]["safeSummary"] == (
        "bash permission request (fields: "
        "field_00, field_01, field_02, field_03, field_04, "
        "field_05, field_06, field_07, field_08, field_09, "
        "field_10, field_11, field_12, field_13, field_14, "
        "field_15, field_16, field_17, field_18, field_19, +5 more)"
    )


def test_permission_request_safe_summary_caps_total_length() -> None:
    translator = PipelineEventTranslator(_ctx())

    envelope = translator.translate(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"field_" + "x" * 400: "value"},
            tool_use_id="toolu-1",
        )
    )[0]

    assert len(envelope["permission"]["safeSummary"]) <= 256


def test_nested_pipeline_data_keys_are_preserved() -> None:
    translator = PipelineEventTranslator(_ctx())
    envelopes = translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "eval",
                "candidate_index": 0,
                "failed": False,
                "conclusions": {"template_content": {"ros_version": "2015-09-01"}},
            },
        )
    )

    assert envelopes[0]["data"]["subPipelineId"] == "eval"
    assert envelopes[0]["data"]["candidateIndex"] == 0
    assert envelopes[0]["data"]["conclusions"] == {"template_content": {"ros_version": "2015-09-01"}}


def test_candidate_attempt_uses_parent_step_not_sub_pipeline_id() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="evaluate_candidates",
            timestamp=time.time(),
            data={"index": 3, "total": 4, "step_type": "parallel_sub_pipeline", "ui_mode": "default"},
        )
    )
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_first",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "parent_step_id": "evaluate_candidates",
            },
        )
    )

    second = translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_second",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "parent_step_id": "evaluate_candidates",
            },
        )
    )[0]

    assert second["candidate"]["attempt"] == 2
    assert second["candidate"]["runId"] == "candidate-evaluate_candidate_second-0-2"


def test_candidate_step_attempt_increments_when_same_step_restarts() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "total_steps": 3,
                "parent_step_id": "evaluate_candidates",
            },
        )
    )
    sub_step_started = {
        "sub_pipeline_id": "evaluate_candidate_abcd",
        "candidate_index": 0,
        "step_id": "template_generating",
        "step_index": 0,
        "total_steps": 3,
    }
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_STARTED,
            step_id="template_generating",
            timestamp=time.time(),
            data=sub_step_started,
        )
    )
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_COMPLETED,
            step_id="template_generating",
            timestamp=time.time(),
            data=sub_step_started,
        )
    )

    second = translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_STARTED,
            step_id="template_generating",
            timestamp=time.time(),
            data=sub_step_started,
        )
    )[0]

    assert second["candidateStep"]["attempt"] == 2
    assert second["candidateStep"]["runId"] == "candidate-evaluate_candidate_abcd-0-1-template_generating-2"


def test_stream_scope_returns_to_candidate_after_candidate_step_completes() -> None:
    translator = PipelineEventTranslator(_ctx())
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "evaluate_candidate_abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "sub_pipeline_name": "evaluate_candidate",
                "total_steps": 3,
                "parent_step_id": "evaluate_candidates",
            },
        )
    )
    sub_step = {
        "sub_pipeline_id": "evaluate_candidate_abcd",
        "candidate_index": 0,
        "step_id": "template_generating",
        "step_index": 0,
        "total_steps": 3,
    }
    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_STARTED,
            step_id="template_generating",
            timestamp=time.time(),
            data=sub_step,
        )
    )

    during_step = translator.translate(
        SubPipelineStreamEvent(
            sub_pipeline_id="evaluate_candidate_abcd",
            candidate_index=0,
            inner=TextDeltaEvent(text="during"),
        )
    )[0]

    translator.translate(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_COMPLETED,
            step_id="template_generating",
            timestamp=time.time(),
            data=sub_step,
        )
    )
    after_step = translator.translate(
        SubPipelineStreamEvent(
            sub_pipeline_id="evaluate_candidate_abcd",
            candidate_index=0,
            inner=TextDeltaEvent(text="after"),
        )
    )[0]

    assert during_step["scope"] == "candidate_step"
    assert during_step["candidateStep"]["id"] == "template_generating"
    assert after_step["scope"] == "candidate"
    assert "candidateStep" not in after_step
