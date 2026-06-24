from __future__ import annotations

import json
import mimetypes
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iac_code.a2a.artifacts import UnsafeArtifactNameError, artifact_filename_from_path, sanitize_public_artifact_text
from iac_code.a2a.events import _tool_result_metadata, _truncate
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    CandidateDetailEvent,
    DiagramEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
)

PIPELINE_EVENTS_EXTENSION_URI = "urn:iac-code:a2a:pipeline-events:v1"
PIPELINE_METADATA_SCHEMA_VERSION = "1.0"

CandidateLookupKey = tuple[str, int]
CandidateAttemptKey = tuple[str, int, int]

_TOP_LEVEL_DATA_KEY_ALIASES = {
    "candidate_index": "candidateIndex",
    "candidate_name": "candidateName",
    "candidates_count": "candidatesCount",
    "conclusion_field": "conclusionField",
    "duration_s": "durationS",
    "early_exit": "earlyExit",
    "error_details": "errorDetails",
    "error_summary": "errorSummary",
    "from_step": "fromStep",
    "parent_step_id": "parentStepId",
    "pipeline_type": "pipelineType",
    "progress_status": "progressStatus",
    "rollback_target": "rollbackTarget",
    "cleanup_status": "cleanupStatus",
    "cleanup_tool_use_id": "cleanupToolUseId",
    "last_error": "lastError",
    "progress_percentage": "progressPercentage",
    "resource_count": "resourceCount",
    "resource_id": "resourceId",
    "resource_name": "resourceName",
    "resource_type": "resourceType",
    "region_id": "regionId",
    "selected_index": "selectedIndex",
    "selected_option": "selectedOption",
    "selected_value": "selectedValue",
    "source_step_id": "sourceStepId",
    "stale_fields": "staleFields",
    "stack_status": "stackStatus",
    "status_message": "statusMessage",
    "step_id": "stepId",
    "step_index": "stepIndex",
    "step_names": "stepNames",
    "step_type": "stepType",
    "sub_pipeline_id": "subPipelineId",
    "sub_pipeline_name": "subPipelineName",
    "to_step": "toStep",
    "total_steps": "totalSteps",
    "total_sub_steps": "totalSubSteps",
    "ui_mode": "uiMode",
    "user_input_length": "userInputLength",
    "valid_targets": "validTargets",
}
_NESTED_DATA_KEY_ALIASES = {
    "error_id": "errorId",
}
_PERMISSION_METADATA_MAX_CHARS = 4000
_PERMISSION_METADATA_MAX_DEPTH = 32
_PERMISSION_SUMMARY_MAX_FIELDS = 20
_PERMISSION_SUMMARY_MAX_CHARS = 256
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "auth",
    "cookie",
    "passphrase",
    "pwd",
    "secret",
    "session",
    "token",
    "password",
    "passwd",
    "credential",
    "authorization",
    "private_key",
)
_STACK_TOOL_ACTIONS = {"CreateStack", "UpdateStack", "ContinueCreateStack", "DeleteStack"}
_STACK_CLEAR_ACTIONS = {"DeleteStack"}


@dataclass
class PipelineA2AContext:
    pipeline_run_id: str
    task_id: str
    context_id: str
    pipeline_name: str
    iac_code_session_id: str | None = None
    parent_step_order: list[str] = field(default_factory=list)
    candidate_step_order: list[str] = field(default_factory=list)
    emit_stack_events: bool = False
    a2a_artifacts_by_step_id: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class _CandidateState:
    sub_pipeline_id: str
    index: int
    attempt: int
    parent_step_id: str | None = None
    parent_step_attempt: int = 1
    name: str | None = None
    sub_pipeline_name: str | None = None
    total_steps: int | None = None
    current_step_id: str | None = None
    current_step_attempt: int | None = None
    terminal: bool = False
    step_attempts: dict[str, int] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return f"candidate-{self.sub_pipeline_id}-{self.index}-{self.attempt}"


class PipelineEventTranslator:
    def __init__(self, context: PipelineA2AContext):
        self._context = context
        self._sequence = 0
        self._parent_step_attempts: dict[str, int] = {}
        self._candidate_attempts: dict[CandidateAttemptKey, int] = {}
        self._candidates: dict[CandidateLookupKey, _CandidateState] = {}
        self._current_parent_step_id: str | None = None
        self._tool_inputs: dict[str, dict[str, Any]] = {}
        self._emitted_candidate_detail_tool_ids: set[str] = set()

    @property
    def last_sequence(self) -> int:
        return self._sequence

    def hydrate_from_events(self, events: list[dict[str, Any]]) -> None:
        latest_parent_step_sequence = -1
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("taskId") != self._context.task_id or event.get("contextId") != self._context.context_id:
                continue

            sequence = _int_or_none(event.get("sequence")) or 0
            self._sequence = max(self._sequence, sequence)
            self._hydrate_candidate_state(event)

            step = event.get("step")
            if not isinstance(step, dict):
                continue
            step_id = _string_or_none(step.get("id"))
            attempt = _int_or_none(step.get("attempt"))
            if step_id is None or attempt is None or attempt <= 0:
                continue
            self._parent_step_attempts[step_id] = max(self._parent_step_attempts.get(step_id, 1), attempt)

            if sequence >= latest_parent_step_sequence and event.get("eventType") not in {
                "step_completed",
                "step_failed",
                "pipeline_completed",
                "pipeline_failed",
                "pipeline_canceled",
            }:
                latest_parent_step_sequence = sequence
                self._current_parent_step_id = step_id

    def _hydrate_candidate_state(self, event: dict[str, Any]) -> None:
        candidate = event.get("candidate")
        if not isinstance(candidate, dict):
            return
        sub_pipeline_id = _string_or_none(candidate.get("id"))
        if sub_pipeline_id is None:
            return
        candidate_index = _int_or_none(candidate.get("index"))
        if candidate_index is None:
            candidate_index = 0
        candidate_attempt = _int_or_none(candidate.get("attempt")) or 1

        step = event.get("step")
        parent_step_id = _string_or_none(step.get("id")) if isinstance(step, dict) else None
        parent_step_attempt = _int_or_none(step.get("attempt")) if isinstance(step, dict) else None
        if parent_step_id is not None and parent_step_attempt is not None and parent_step_attempt > 0:
            self._parent_step_attempts[parent_step_id] = max(
                self._parent_step_attempts.get(parent_step_id, 1),
                parent_step_attempt,
            )
        else:
            parent_step_attempt = self._parent_step_attempt(parent_step_id)

        attempt_key = self._candidate_attempt_key(parent_step_id, parent_step_attempt, candidate_index)
        self._candidate_attempts[attempt_key] = max(
            self._candidate_attempts.get(attempt_key, 0),
            candidate_attempt,
        )

        key = (sub_pipeline_id, candidate_index)
        state = self._candidates.get(key)
        if state is None or candidate_attempt > state.attempt:
            state = _CandidateState(
                sub_pipeline_id=sub_pipeline_id,
                index=candidate_index,
                attempt=candidate_attempt,
                parent_step_id=parent_step_id,
                parent_step_attempt=parent_step_attempt,
                name=_string_or_none(candidate.get("name")),
                sub_pipeline_name=_string_or_none(candidate.get("pipelineName")),
                total_steps=_int_or_none(candidate.get("totalSteps")),
            )
            self._candidates[key] = state
        elif candidate_attempt == state.attempt:
            if state.parent_step_id is None:
                state.parent_step_id = parent_step_id
            if state.parent_step_attempt is None:
                state.parent_step_attempt = parent_step_attempt
            if state.name is None:
                state.name = _string_or_none(candidate.get("name"))
            if state.sub_pipeline_name is None:
                state.sub_pipeline_name = _string_or_none(candidate.get("pipelineName"))
            if state.total_steps is None:
                state.total_steps = _int_or_none(candidate.get("totalSteps"))

        candidate_step = event.get("candidateStep")
        event_type = _string_or_none(event.get("eventType"))
        if isinstance(candidate_step, dict):
            step_id = _string_or_none(candidate_step.get("id"))
            step_attempt = _int_or_none(candidate_step.get("attempt")) or 1
            if step_id is not None:
                state.step_attempts[step_id] = max(state.step_attempts.get(step_id, 0), step_attempt)
                if event_type in {"candidate_step_completed", "candidate_step_failed"}:
                    if state.current_step_id == step_id:
                        state.current_step_id = None
                        state.current_step_attempt = None
                else:
                    state.current_step_id = step_id
                    state.current_step_attempt = step_attempt

        if event_type in {"candidate_completed", "candidate_failed"}:
            state.current_step_id = None
            state.current_step_attempt = None
            state.terminal = True

    def translate(self, event: Any) -> list[dict[str, Any]]:
        if isinstance(event, PipelineEvent):
            return self._translate_pipeline_event(event)
        if isinstance(event, TextDeltaEvent):
            return [self._translate_text_delta_event(event)]
        if isinstance(event, AskUserQuestionEvent):
            return [self._translate_ask_user_question_event(event)]
        if isinstance(event, PermissionRequestEvent):
            return [self._translate_permission_request_event(event)]
        if isinstance(event, ToolUseEndEvent):
            self._remember_tool_input(event)
            return []
        if isinstance(event, CandidateDetailEvent):
            envelope = self._translate_candidate_detail_event(event)
            return [] if envelope is None else [envelope]
        if isinstance(event, DiagramEvent):
            return [self._translate_diagram_event(event)]
        if isinstance(event, ToolResultEvent):
            return self._translate_tool_result_event(event)
        if isinstance(event, SubPipelineStreamEvent):
            return self._translate_sub_pipeline_stream_event(event)
        return []

    def manual_event(
        self,
        event_type: str,
        scope: str,
        status: str = "working",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_data = _event_data(data or {})
        envelope = self._envelope(event_type, scope, status, event_data)
        if event_type == "rollback_completed":
            target_step_id = _first_string_value(event_data, ("toStepId", "toStep", "rollbackTarget"))
            if target_step_id is not None:
                self._parent_step_attempts[target_step_id] = self._parent_step_attempts.get(target_step_id, 1) + 1
                self._current_parent_step_id = target_step_id
                envelope["step"] = self._parent_step_coordinate(target_step_id)
        return envelope

    def candidate_restart_events(
        self,
        *,
        candidate_scope: Any,
        target_candidate_step_id: Any,
        reason: Any,
    ) -> list[dict[str, Any]]:
        states = self._candidate_states_for_scope(candidate_scope)
        if not states:
            return [
                self.manual_event(
                    "candidate_restart_requested",
                    "interrupt",
                    data={
                        "candidateScope": candidate_scope,
                        "targetCandidateStepId": target_candidate_step_id,
                        "reason": sanitize_public_artifact_text(str(reason)),
                    },
                )
            ]

        events: list[dict[str, Any]] = []
        target_step_id = _string_or_none(target_candidate_step_id)
        for state in states:
            data = {
                "candidateScope": candidate_scope,
                "targetCandidateStepId": target_candidate_step_id,
                "nextCandidateAttempt": state.attempt + 1,
                "reason": sanitize_public_artifact_text(str(reason)),
            }
            envelope = self._envelope("candidate_restart_requested", "candidate", "working", data)
            self._add_candidate_coordinates(envelope, state)
            if target_step_id is not None:
                envelope["candidateStep"] = self._candidate_step_coordinate(state, target_step_id)
            events.append(envelope)
        return events

    def _translate_pipeline_event(self, event: PipelineEvent) -> list[dict[str, Any]]:
        data = event.data or {}
        created_at = _created_at_from_timestamp(event.timestamp)

        if event.type == PipelineEventType.PIPELINE_STARTED:
            return [self._envelope("pipeline_started", "pipeline", "working", _event_data(data), created_at=created_at)]
        if event.type == PipelineEventType.PIPELINE_RESUMED:
            return [self._envelope("pipeline_resumed", "pipeline", "working", _event_data(data), created_at=created_at)]
        if event.type == PipelineEventType.PIPELINE_WARNING:
            return [
                self._envelope(
                    "pipeline_warning",
                    "pipeline",
                    "working",
                    _warning_event_data(data),
                    created_at=created_at,
                )
            ]
        if event.type == PipelineEventType.PIPELINE_COMPLETED:
            event_type = "pipeline_failed" if data.get("failed") is True else "pipeline_completed"
            status = "failed" if event_type == "pipeline_failed" else "completed"
            return [self._envelope(event_type, "pipeline", status, _event_data(data), created_at=created_at)]
        if event.type == PipelineEventType.STEP_STARTED:
            return [self._translate_parent_step_event(event, "step_started", "working", created_at)]
        if event.type == PipelineEventType.STEP_COMPLETED:
            envelope = self._translate_parent_step_event(event, "step_completed", "working", created_at)
            return [envelope, *self._completion_artifact_events(envelope)]
        if event.type == PipelineEventType.STEP_FAILED:
            return [self._translate_parent_step_event(event, "step_failed", "failed", created_at)]
        if event.type == PipelineEventType.ROLLBACK_TRIGGERED:
            return [self._translate_rollback_event(event, created_at)]
        if event.type == PipelineEventType.USER_INPUT_REQUIRED:
            return [self._translate_input_event(event, "input_required", "input_required", created_at)]
        if event.type == PipelineEventType.USER_INPUT_RECEIVED:
            return [self._translate_input_event(event, "input_received", "working", created_at)]
        if event.type == PipelineEventType.SUB_PIPELINE_STARTED:
            return [self._translate_candidate_started_event(event, created_at)]
        if event.type == PipelineEventType.SUB_STEP_STARTED:
            return [self._translate_candidate_step_event(event, "candidate_step_started", "working", created_at)]
        if event.type == PipelineEventType.SUB_STEP_COMPLETED:
            envelope = self._translate_candidate_step_event(event, "candidate_step_completed", "working", created_at)
            self._clear_current_candidate_step(event)
            return [envelope, *self._completion_artifact_events(envelope)]
        if event.type == PipelineEventType.SUB_STEP_FAILED:
            envelope = self._translate_candidate_step_event(event, "candidate_step_failed", "working", created_at)
            self._clear_current_candidate_step(event)
            return [envelope]
        if event.type == PipelineEventType.SUB_PIPELINE_COMPLETED:
            return [self._translate_candidate_completed_event(event, created_at)]

        return []

    def _translate_parent_step_event(
        self,
        event: PipelineEvent,
        event_type: str,
        status: str,
        created_at: str,
    ) -> dict[str, Any]:
        if event.step_id is not None and event.type == PipelineEventType.STEP_STARTED:
            self._current_parent_step_id = event.step_id

        envelope = self._envelope(event_type, "step", status, _event_data(event.data or {}), created_at=created_at)
        if event.step_id is not None:
            envelope["step"] = self._parent_step_coordinate(event.step_id, event.data or {})
        return envelope

    def _translate_input_event(
        self,
        event: PipelineEvent,
        event_type: str,
        status: str,
        created_at: str,
    ) -> dict[str, Any]:
        scope = "step" if event.step_id is not None else "pipeline"
        envelope = self._envelope(event_type, scope, status, _event_data(event.data or {}), created_at=created_at)
        if event.step_id is not None:
            envelope["step"] = self._parent_step_coordinate(event.step_id, event.data or {})
        return envelope

    def _translate_rollback_event(self, event: PipelineEvent, created_at: str) -> dict[str, Any]:
        data = event.data or {}
        target_step_id = _string_or_none(data.get("to_step"))
        if target_step_id is not None:
            self._parent_step_attempts[target_step_id] = self._parent_step_attempts.get(target_step_id, 1) + 1
            self._current_parent_step_id = target_step_id

        envelope = self._envelope("rollback_completed", "pipeline", "working", _event_data(data), created_at=created_at)
        if target_step_id is not None:
            envelope["step"] = self._parent_step_coordinate(target_step_id)
        return envelope

    def _translate_candidate_started_event(self, event: PipelineEvent, created_at: str) -> dict[str, Any]:
        data = event.data or {}
        state = self._start_candidate(data, event.step_id)
        envelope = self._envelope("candidate_started", "candidate", "working", _event_data(data), created_at=created_at)
        self._add_candidate_coordinates(envelope, state, include_candidate_steps=True)
        return envelope

    def _translate_candidate_step_event(
        self,
        event: PipelineEvent,
        event_type: str,
        status: str,
        created_at: str,
    ) -> dict[str, Any]:
        data = event.data or {}
        state = self._candidate_from_data(data)
        step_id = _string_or_none(data.get("step_id")) or event.step_id
        if step_id is not None:
            if event.type == PipelineEventType.SUB_STEP_STARTED:
                self._start_candidate_step(state, step_id)
            elif state.current_step_id != step_id:
                state.current_step_id = step_id
                state.current_step_attempt = state.step_attempts.setdefault(step_id, 1)

        envelope = self._envelope(event_type, "candidate_step", status, _event_data(data), created_at=created_at)
        self._add_candidate_coordinates(envelope, state)
        if step_id is not None:
            envelope["candidateStep"] = self._candidate_step_coordinate(state, step_id, data)
        return envelope

    def _translate_candidate_completed_event(self, event: PipelineEvent, created_at: str) -> dict[str, Any]:
        data = event.data or {}
        state = self._candidate_from_data(data)
        state.current_step_id = None
        state.current_step_attempt = None
        state.terminal = True
        event_type = "candidate_failed" if data.get("failed") is True else "candidate_completed"
        status = "working"
        envelope = self._envelope(event_type, "candidate", status, _event_data(data), created_at=created_at)
        self._add_candidate_coordinates(envelope, state)
        return envelope

    def _completion_artifact_events(self, completed: dict[str, Any]) -> list[dict[str, Any]]:
        step_id = _completion_step_id(completed)
        if step_id is None:
            return []
        specs = self._context.a2a_artifacts_by_step_id.get(step_id, [])
        if not specs:
            return []

        root = _artifact_expression_root(completed)
        events: list[dict[str, Any]] = []
        for spec in specs:
            artifact = _artifact_from_spec(spec, root)
            if artifact is None:
                continue
            envelope = self._envelope(
                "artifact_created",
                str(completed.get("scope") or "step"),
                "working",
                {"source": "conclusion"},
            )
            for key in ("step", "candidate", "candidateStep"):
                value = completed.get(key)
                if isinstance(value, dict):
                    envelope[key] = dict(value)
            envelope["artifact"] = artifact
            events.append(envelope)
        return events

    def _translate_sub_pipeline_stream_event(self, event: SubPipelineStreamEvent) -> list[dict[str, Any]]:
        state = self._candidate_from_stream_event(event)
        inner = event.inner
        if isinstance(inner, TextDeltaEvent):
            event_type = "text_delta"
            data = {"text": inner.text}
            input_data = None
            permission = None
        elif isinstance(inner, AskUserQuestionEvent):
            event_type = "input_required"
            data = _ask_user_question_data(inner)
            input_data = _ask_user_question_input(inner)
            permission = None
        elif isinstance(inner, CandidateDetailEvent):
            if self._has_emitted_candidate_detail(inner.tool_use_id):
                return []
            event_type = "candidate_detail_shown"
            data = _candidate_detail_data(
                tool_use_id=inner.tool_use_id,
                candidate_name=inner.candidate_name,
                summary=inner.summary,
                cost_items=inner.cost_items,
                total_monthly_cost=inner.total_monthly_cost,
                candidate_index=inner.candidate_index,
            )
            self._mark_candidate_detail_emitted(inner.tool_use_id)
            input_data = None
            permission = None
        elif isinstance(inner, DiagramEvent):
            event_type = "diagram_shown"
            data = _diagram_data(inner)
            input_data = None
            permission = None
        elif isinstance(inner, PermissionRequestEvent):
            event_type = "permission_requested"
            data = _permission_request_data(inner)
            input_data = None
            permission = _permission_request_metadata(inner)
        elif isinstance(inner, ToolUseEndEvent):
            self._remember_tool_input(inner)
            return []
        elif isinstance(inner, ToolResultEvent):
            event_type = "tool_result"
            data = _tool_result_data(inner)
            input_data = None
            permission = None
        else:
            return []

        scope = "candidate_step" if state.current_step_id is not None else "candidate"
        status = "input_required" if event_type == "input_required" else "working"
        envelope = self._envelope(event_type, scope, status, data)
        self._add_candidate_coordinates(envelope, state)
        if state.current_step_id is not None:
            envelope["candidateStep"] = self._candidate_step_coordinate(state, state.current_step_id)
        if permission is not None:
            envelope["permission"] = permission
        if input_data is not None:
            envelope["input"] = input_data
        envelopes: list[dict[str, Any]] = []
        stack_envelope = (
            self._translate_stack_current_changed_event(inner) if isinstance(inner, ToolResultEvent) else None
        )
        if stack_envelope is not None:
            self._add_candidate_coordinates(stack_envelope, state)
            if state.current_step_id is not None:
                stack_envelope["candidateStep"] = self._candidate_step_coordinate(state, state.current_step_id)
            envelopes.append(stack_envelope)
        envelopes.append(envelope)
        return envelopes

    def _translate_text_delta_event(self, event: TextDeltaEvent) -> dict[str, Any]:
        return self._translate_parent_scoped_display_event("text_delta", {"text": event.text})

    def _translate_ask_user_question_event(self, event: AskUserQuestionEvent) -> dict[str, Any]:
        envelope = self._translate_parent_scoped_display_event("input_required", _ask_user_question_data(event))
        envelope["status"] = "input_required"
        envelope["input"] = _ask_user_question_input(event)
        return envelope

    def _translate_permission_request_event(self, event: PermissionRequestEvent) -> dict[str, Any]:
        envelope = self._translate_parent_scoped_display_event("permission_requested", _permission_request_data(event))
        envelope["permission"] = _permission_request_metadata(event)
        return envelope

    def _translate_candidate_detail_event(self, event: CandidateDetailEvent) -> dict[str, Any] | None:
        if self._has_emitted_candidate_detail(event.tool_use_id):
            return None
        data = _candidate_detail_data(
            tool_use_id=event.tool_use_id,
            candidate_name=event.candidate_name,
            summary=event.summary,
            cost_items=event.cost_items,
            total_monthly_cost=event.total_monthly_cost,
            candidate_index=event.candidate_index,
        )
        self._mark_candidate_detail_emitted(event.tool_use_id)
        return self._translate_parent_scoped_display_event("candidate_detail_shown", data)

    def _translate_diagram_event(self, event: DiagramEvent) -> dict[str, Any]:
        return self._translate_parent_scoped_display_event("diagram_shown", _diagram_data(event))

    def _translate_parent_scoped_display_event(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        scope = "step" if self._current_parent_step_id is not None else "pipeline"
        envelope = self._envelope(event_type, scope, "working", data)
        if self._current_parent_step_id is not None:
            envelope["step"] = self._parent_step_coordinate(self._current_parent_step_id)
        return envelope

    def _translate_tool_result_event(self, event: ToolResultEvent) -> list[dict[str, Any]]:
        envelopes: list[dict[str, Any]] = []
        candidate_detail_envelope = self._translate_candidate_detail_from_tool_result(event)
        if candidate_detail_envelope is not None:
            envelopes.append(candidate_detail_envelope)
        stack_envelope = self._translate_stack_current_changed_event(event)
        if stack_envelope is not None:
            envelopes.append(stack_envelope)
        envelopes.append(self._translate_parent_scoped_display_event("tool_result", _tool_result_data(event)))
        return envelopes

    def _remember_tool_input(self, event: ToolUseEndEvent) -> None:
        self._tool_inputs[event.tool_use_id] = {"toolName": event.name, "input": dict(event.input)}

    def _translate_candidate_detail_from_tool_result(self, event: ToolResultEvent) -> dict[str, Any] | None:
        if event.is_error or self._has_emitted_candidate_detail(event.tool_use_id):
            return None

        record = self._tool_inputs.get(event.tool_use_id)
        if not isinstance(record, dict):
            return None
        tool_name = _string_or_none(record.get("toolName")) or event.tool_name
        if tool_name != "show_candidate_detail":
            return None

        tool_input = record.get("input")
        if not isinstance(tool_input, dict):
            return None
        data = _candidate_detail_data_from_tool_input(event.tool_use_id, tool_input)
        if data is None:
            return None

        self._mark_candidate_detail_emitted(event.tool_use_id)
        return self._translate_parent_scoped_display_event("candidate_detail_shown", data)

    def _has_emitted_candidate_detail(self, tool_use_id: str) -> bool:
        return bool(tool_use_id) and tool_use_id in self._emitted_candidate_detail_tool_ids

    def _mark_candidate_detail_emitted(self, tool_use_id: str) -> None:
        if tool_use_id:
            self._emitted_candidate_detail_tool_ids.add(tool_use_id)

    def _translate_stack_current_changed_event(self, event: ToolResultEvent) -> dict[str, Any] | None:
        data = self._stack_current_changed_data(event)
        if data is None:
            return None
        envelope = self._envelope("stack_current_changed", "stack", "working", data)
        if self._current_parent_step_id is not None:
            envelope["step"] = self._parent_step_coordinate(self._current_parent_step_id)
        return envelope

    def _stack_current_changed_data(self, event: ToolResultEvent) -> dict[str, Any] | None:
        if not self._context.emit_stack_events:
            return None

        record = self._tool_inputs.get(event.tool_use_id)
        tool_input = record.get("input") if isinstance(record, dict) else {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_name = _string_or_none(record.get("toolName")) if isinstance(record, dict) else None
        tool_name = tool_name or event.tool_name

        operation = _stack_operation_from_tool_input(tool_name, tool_input)
        if operation is None:
            return None
        action = operation["action"]
        params = operation["params"]

        result = _json_object_from_string(event.result)
        if result is None:
            return None
        is_success = _bool_or_none(result.get("is_success"))
        if is_success is None:
            is_success = _bool_or_none(result.get("isSuccess"))
        if is_success is None:
            is_success = not event.is_error

        stack_id = _first_string_from_sources(
            (result, params),
            ("StackId", "stackId", "stack_id"),
        )
        if stack_id is None:
            return None

        stack_status = _first_string_from_sources((result,), ("StackStatus", "stackStatus", "status"))
        is_delete_complete = action in _STACK_CLEAR_ACTIONS and is_success and stack_status == "DELETE_COMPLETE"
        if action in _STACK_CLEAR_ACTIONS and is_success and stack_status is None:
            stack_status = "DELETE_REQUESTED"

        data: dict[str, Any] = {
            "toolName": event.tool_name,
            "toolUseId": event.tool_use_id,
            "provider": operation["provider"],
            "action": action,
            "regionId": operation["regionId"],
            "stackId": stack_id,
            "stackName": _first_string_from_sources((result, params), ("StackName", "stackName", "stack_name", "name")),
            "stackStatus": stack_status,
            "isSuccess": is_success,
            "current": False if is_delete_complete else True,
        }
        if is_delete_complete:
            data["cleared"] = True
        return {key: value for key, value in data.items() if value is not None}

    def _envelope(
        self,
        event_type: str,
        scope: str,
        status: str,
        data: dict[str, Any],
        *,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        self._sequence += 1
        envelope = {
            "schemaVersion": PIPELINE_METADATA_SCHEMA_VERSION,
            "extensionUri": PIPELINE_EVENTS_EXTENSION_URI,
            "eventId": f"evt-{uuid.uuid4().hex}",
            "sequence": self._sequence,
            "createdAt": created_at or _utc_now(),
            "eventType": event_type,
            "scope": scope,
            "pipelineRunId": self._context.pipeline_run_id,
            "taskId": self._context.task_id,
            "contextId": self._context.context_id,
            "pipelineName": self._context.pipeline_name,
            "status": status,
            "data": data,
        }
        if self._context.iac_code_session_id is not None:
            envelope["iacCodeSessionId"] = self._context.iac_code_session_id
        return envelope

    def _parent_step_coordinate(self, step_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data or {}
        explicit_attempt = _int_or_none(data.get("attempt"))
        attempt = (
            explicit_attempt
            if explicit_attempt is not None and explicit_attempt > 0
            else self._parent_step_attempts.get(step_id, 1)
        )
        self._parent_step_attempts[step_id] = max(self._parent_step_attempts.get(step_id, 1), attempt)
        coordinate: dict[str, Any] = {
            "id": step_id,
            "runId": f"step-{step_id}-{attempt}",
            "attempt": attempt,
        }

        index = data.get("index")
        if index is None and step_id in self._context.parent_step_order:
            index = self._context.parent_step_order.index(step_id) + 1
        if index is not None:
            coordinate["index"] = index

        total = data.get("total") or (len(self._context.parent_step_order) if self._context.parent_step_order else None)
        if total is not None:
            coordinate["total"] = total

        if "step_type" in data:
            coordinate["type"] = data["step_type"]
        if "ui_mode" in data:
            coordinate["uiMode"] = data["ui_mode"]
        return coordinate

    def _candidate_coordinate(self, state: _CandidateState, *, include_steps: bool = False) -> dict[str, Any]:
        coordinate: dict[str, Any] = {
            "id": state.sub_pipeline_id,
            "runId": state.run_id,
            "index": state.index,
            "attempt": state.attempt,
        }
        if state.name is not None:
            coordinate["name"] = state.name
        if state.sub_pipeline_name is not None:
            coordinate["pipelineName"] = state.sub_pipeline_name
        if state.total_steps is not None:
            coordinate["totalSteps"] = state.total_steps
        if include_steps:
            steps = self._candidate_step_skeletons(state)
            if steps:
                coordinate["steps"] = steps
        return coordinate

    def _candidate_step_skeletons(self, state: _CandidateState) -> list[dict[str, Any]]:
        if not self._context.candidate_step_order:
            return []
        total = state.total_steps or len(self._context.candidate_step_order)
        steps: list[dict[str, Any]] = []
        for index, step_id in enumerate(self._context.candidate_step_order[:total], start=1):
            attempt = state.step_attempts.get(step_id, 1)
            steps.append(
                {
                    "id": step_id,
                    "name": step_id,
                    "runId": f"{state.run_id}-{step_id}-{attempt}",
                    "attempt": attempt,
                    "index": index,
                    "total": total,
                    "status": "pending",
                }
            )
        return steps

    def _candidate_step_coordinate(
        self,
        state: _CandidateState,
        step_id: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = data or {}
        attempt = self._candidate_step_attempt(state, step_id)
        coordinate: dict[str, Any] = {
            "id": step_id,
            "runId": f"{state.run_id}-{step_id}-{attempt}",
            "attempt": attempt,
        }

        index = None
        if step_id in self._context.candidate_step_order:
            index = self._context.candidate_step_order.index(step_id) + 1
        elif isinstance(data.get("step_index"), int):
            index = data["step_index"] + 1
        if index is not None:
            coordinate["index"] = index

        total = data.get("total_steps") or state.total_steps
        if total is not None:
            coordinate["total"] = total
        return coordinate

    def _add_candidate_coordinates(
        self,
        envelope: dict[str, Any],
        state: _CandidateState,
        *,
        include_candidate_steps: bool = False,
    ) -> None:
        if state.parent_step_id is not None:
            envelope["step"] = self._parent_step_coordinate(state.parent_step_id)
        envelope["candidate"] = self._candidate_coordinate(state, include_steps=include_candidate_steps)

    def _start_candidate(self, data: dict[str, Any], parent_step_id: str | None) -> _CandidateState:
        sub_pipeline_id = _string_or_none(data.get("sub_pipeline_id")) or "unknown"
        candidate_index = _int_or_zero(data.get("candidate_index"))
        key = (sub_pipeline_id, candidate_index)
        resolved_parent_step_id = (
            _string_or_none(data.get("parent_step_id")) or parent_step_id or self._current_parent_step_id
        )
        parent_step_attempt = self._parent_step_attempt(resolved_parent_step_id)
        attempt_key = self._candidate_attempt_key(resolved_parent_step_id, parent_step_attempt, candidate_index)
        attempt = self._candidate_attempts.get(attempt_key, 0) + 1
        self._candidate_attempts[attempt_key] = attempt
        state = _CandidateState(
            sub_pipeline_id=sub_pipeline_id,
            index=candidate_index,
            attempt=attempt,
            parent_step_id=resolved_parent_step_id,
            parent_step_attempt=parent_step_attempt,
            name=_string_or_none(data.get("candidate_name")),
            sub_pipeline_name=_string_or_none(data.get("sub_pipeline_name")),
            total_steps=_int_or_none(data.get("total_steps")),
        )
        self._candidates[key] = state
        return state

    def _candidate_from_data(self, data: dict[str, Any]) -> _CandidateState:
        sub_pipeline_id = _string_or_none(data.get("sub_pipeline_id")) or "unknown"
        candidate_index = _int_or_zero(data.get("candidate_index"))
        key = (sub_pipeline_id, candidate_index)
        state = self._candidates.get(key)
        if state is not None:
            return state

        resolved_parent_step_id = _string_or_none(data.get("parent_step_id")) or self._current_parent_step_id
        parent_step_attempt = self._parent_step_attempt(resolved_parent_step_id)
        attempt_key = self._candidate_attempt_key(resolved_parent_step_id, parent_step_attempt, candidate_index)
        attempt = self._candidate_attempts.setdefault(attempt_key, 1)
        state = _CandidateState(
            sub_pipeline_id=sub_pipeline_id,
            index=candidate_index,
            attempt=attempt,
            parent_step_id=resolved_parent_step_id,
            parent_step_attempt=parent_step_attempt,
        )
        self._candidates[key] = state
        return state

    def _candidate_from_stream_event(self, event: SubPipelineStreamEvent) -> _CandidateState:
        key = (event.sub_pipeline_id, event.candidate_index)
        state = self._candidates.get(key)
        if state is not None:
            return state

        parent_step_attempt = self._parent_step_attempt(self._current_parent_step_id)
        attempt_key = self._candidate_attempt_key(
            self._current_parent_step_id,
            parent_step_attempt,
            event.candidate_index,
        )
        attempt = self._candidate_attempts.setdefault(attempt_key, 1)
        state = _CandidateState(
            sub_pipeline_id=event.sub_pipeline_id,
            index=event.candidate_index,
            attempt=attempt,
            parent_step_id=self._current_parent_step_id,
            parent_step_attempt=parent_step_attempt,
        )
        self._candidates[key] = state
        return state

    def _clear_current_candidate_step(self, event: PipelineEvent) -> None:
        data = event.data or {}
        state = self._candidate_from_data(data)
        step_id = _string_or_none(data.get("step_id")) or event.step_id
        if step_id is None or state.current_step_id == step_id:
            state.current_step_id = None
            state.current_step_attempt = None

    def _candidate_states_for_scope(self, candidate_scope: Any) -> list[_CandidateState]:
        if candidate_scope in ("all", "*"):
            return _current_active_candidate_states(self._candidates.values(), self._current_parent_step_id)

        target_index = _candidate_scope_index(candidate_scope)
        if target_index is not None:
            return _latest_candidate_state(
                state
                for state in _current_active_candidate_states(self._candidates.values(), self._current_parent_step_id)
                if state.index == target_index
            )

        scope = _string_or_none(candidate_scope)
        if scope is None:
            return []
        return _latest_candidate_state(
            state
            for state in _current_active_candidate_states(self._candidates.values(), self._current_parent_step_id)
            if state.sub_pipeline_id == scope or state.run_id == scope
        )

    def _parent_step_attempt(self, parent_step_id: str | None) -> int:
        if parent_step_id is None:
            return 1
        return self._parent_step_attempts.setdefault(parent_step_id, 1)

    @staticmethod
    def _candidate_attempt_key(
        parent_step_id: str | None,
        parent_step_attempt: int,
        candidate_index: int,
    ) -> CandidateAttemptKey:
        return (parent_step_id or "unknown", parent_step_attempt, candidate_index)

    @staticmethod
    def _start_candidate_step(state: _CandidateState, step_id: str) -> None:
        attempt = state.step_attempts.get(step_id, 0) + 1
        state.step_attempts[step_id] = attempt
        state.current_step_id = step_id
        state.current_step_attempt = attempt

    @staticmethod
    def _candidate_step_attempt(state: _CandidateState, step_id: str) -> int:
        if state.current_step_id == step_id and state.current_step_attempt is not None:
            return state.current_step_attempt
        return state.step_attempts.get(step_id, 1)


def _event_data(data: dict[str, Any]) -> dict[str, Any]:
    return {
        _TOP_LEVEL_DATA_KEY_ALIASES.get(str(key), str(key)): _sanitize_event_value(str(key), value)
        for key, value in data.items()
    }


def _warning_event_data(data: dict[str, Any]) -> dict[str, Any]:
    private_keys = {"ledger_path", "ledgerPath", "load_error", "loadError"}
    return _event_data({key: value for key, value in data.items() if str(key) not in private_keys})


def _sanitize_event_value(key: str, value: Any) -> Any:
    key_lower = key.lower()
    if isinstance(value, str):
        if any(marker in key_lower for marker in ("reason", "error", "message", "traceback")):
            return sanitize_public_artifact_text(value)
        return value
    if isinstance(value, list):
        return [_sanitize_event_value(key, item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_event_value(key, item) for item in value]
    if isinstance(value, dict):
        return {
            _NESTED_DATA_KEY_ALIASES.get(str(child_key), str(child_key)): _sanitize_event_value(
                str(child_key), child_value
            )
            for child_key, child_value in value.items()
        }
    return value


def _created_at_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _first_string_value(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _string_or_none(data.get(key))
        if value is not None:
            return value
    return None


def _first_string_from_sources(sources: tuple[dict[str, Any], ...], keys: tuple[str, ...]) -> str | None:
    for source in sources:
        value = _first_string_value(source, keys)
        if value is not None:
            return value
    return None


def _json_object_from_string(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stack_operation_from_tool_input(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    normalized_tool_name = tool_name.lower()
    params = _dict_or_empty(tool_input.get("params") or tool_input.get("parameters"))
    if normalized_tool_name == "ros_stack":
        provider = "ros"
        action = _first_string_value(tool_input, ("action", "Action"))
    elif normalized_tool_name == "aliyun_api":
        product = _first_string_value(tool_input, ("product", "Product", "service", "Service"))
        if product is None or product.lower() != "ros":
            return None
        provider = "ros"
        action = _first_string_value(tool_input, ("action", "Action"))
    else:
        return None

    if action not in _STACK_TOOL_ACTIONS:
        return None
    return {
        "provider": provider,
        "action": action,
        "params": params,
        "regionId": _first_string_from_sources((tool_input, params), ("region_id", "regionId", "RegionId")),
    }


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _int_or_zero(value: Any) -> int:
    return _int_or_none(value) or 0


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _candidate_scope_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    candidate_prefix, separator, rest = value.partition(":")
    if separator and candidate_prefix == "candidate":
        return _int_or_none(rest)
    return _int_or_none(value)


def _current_active_candidate_states(
    states: Any,
    current_parent_step_id: str | None,
) -> list[_CandidateState]:
    active = [state for state in states if isinstance(state, _CandidateState) and not state.terminal]
    if not active:
        return []
    scoped = [state for state in active if state.parent_step_id == current_parent_step_id]
    if scoped:
        active = scoped
    latest_parent_attempt = max(state.parent_step_attempt for state in active)
    return sorted(
        (state for state in active if state.parent_step_attempt == latest_parent_attempt),
        key=lambda item: (item.index, item.run_id),
    )


def _latest_candidate_state(states: Any) -> list[_CandidateState]:
    candidates = [state for state in states if isinstance(state, _CandidateState)]
    if not candidates:
        return []
    latest = max(candidates, key=lambda item: (item.parent_step_attempt, item.attempt, item.run_id))
    return [latest]


def _permission_request_data(event: PermissionRequestEvent) -> dict[str, Any]:
    return {
        "toolName": event.tool_name,
        "toolUseId": event.tool_use_id,
    }


def _ask_user_question_data(event: AskUserQuestionEvent) -> dict[str, Any]:
    return {
        "kind": "ask_user_question",
        "inputId": _ask_user_question_input_id(event),
        "toolUseId": event.tool_use_id,
        "question": event.question,
        "prompt": event.question,
        "options": event.options,
        "allowFreeText": event.allow_free_text,
        "freeTextPrompt": event.free_text_prompt,
    }


def _ask_user_question_input(event: AskUserQuestionEvent) -> dict[str, Any]:
    return {
        **_ask_user_question_data(event),
        "required": True,
    }


def _ask_user_question_input_id(event: AskUserQuestionEvent) -> str:
    suffix = event.tool_use_id or "unknown"
    return f"ask-{suffix}"


def _permission_request_metadata(event: PermissionRequestEvent) -> dict[str, Any]:
    return safe_permission_metadata(event)


def safe_permission_metadata(
    event: PermissionRequestEvent,
    *,
    include_tool_input: bool = True,
) -> dict[str, Any]:
    metadata = {
        "permissionId": f"perm-{event.tool_use_id}",
        "toolName": event.tool_name,
        "toolUseId": event.tool_use_id,
        "safeSummary": _permission_safe_summary(event),
    }
    if include_tool_input:
        metadata["toolInput"] = _redact_permission_value(event.tool_input)
    return metadata


def _permission_safe_summary(event: PermissionRequestEvent) -> str:
    fields = (
        sorted({_safe_permission_field_name(key) for key in event.tool_input})
        if isinstance(event.tool_input, dict)
        else []
    )
    if not fields:
        return f"{event.tool_name} permission request"
    if len(fields) > _PERMISSION_SUMMARY_MAX_FIELDS:
        remaining_field_count = len(fields) - _PERMISSION_SUMMARY_MAX_FIELDS
        visible_fields = [
            *fields[:_PERMISSION_SUMMARY_MAX_FIELDS],
            f"+{remaining_field_count} more",
        ]
    else:
        visible_fields = fields
    summary = f"{event.tool_name} permission request (fields: {', '.join(visible_fields)})"
    if len(summary) <= _PERMISSION_SUMMARY_MAX_CHARS:
        return summary
    return summary[: _PERMISSION_SUMMARY_MAX_CHARS - 3] + "..."


def _redact_permission_value(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= _PERMISSION_METADATA_MAX_DEPTH:
        return "[truncated-depth]"
    if isinstance(value, str):
        return sanitize_public_artifact_text(value)[:_PERMISSION_METADATA_MAX_CHARS]
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _is_secret_key(key) else _redact_permission_value(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_permission_value(item, _depth=_depth + 1) for item in value]
    return _truncate(value, _depth=_depth)


def _is_secret_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    compact = normalized.replace("_", "")
    return any(fragment in normalized or fragment.replace("_", "") in compact for fragment in _SECRET_KEY_FRAGMENTS)


def _safe_permission_field_name(key: Any) -> str:
    return "[redacted]" if _is_secret_key(key) else str(key)


def _tool_result_data(event: ToolResultEvent) -> dict[str, Any]:
    return {
        "toolName": event.tool_name,
        "toolUseId": event.tool_use_id,
        "isError": event.is_error,
        "result": _tool_result_metadata(event.result, is_error=event.is_error),
    }


def _completion_step_id(envelope: dict[str, Any]) -> str | None:
    candidate_step = envelope.get("candidateStep")
    if isinstance(candidate_step, dict):
        step_id = _string_or_none(candidate_step.get("id"))
        if step_id is not None:
            return step_id
    step = envelope.get("step")
    if isinstance(step, dict):
        return _string_or_none(step.get("id"))
    return None


def _artifact_expression_root(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data")
    data = data if isinstance(data, dict) else {}
    return {"data": data, **data}


def _artifact_from_spec(spec: Any, root: dict[str, Any]) -> dict[str, Any] | None:
    path_expression = _artifact_spec_field(spec, "path") or _artifact_spec_field(spec, "source")
    content_expression = _artifact_spec_field(spec, "content")
    if path_expression is None or content_expression is None:
        return None

    path = _resolve_artifact_expression(root, path_expression)
    content = _resolve_artifact_expression(root, content_expression)
    if not isinstance(path, str) or not isinstance(content, str):
        return None

    media_type = _artifact_spec_field(spec, "media_type") or _artifact_spec_field(spec, "mediaType") or "auto"
    if media_type == "auto":
        media_type = _media_type_for_filename(path)

    try:
        filename = artifact_filename_from_path(path)
    except UnsafeArtifactNameError:
        filename = "artifact.txt"

    return {"filename": filename, "mediaType": media_type, "content": content}


def _artifact_spec_field(spec: Any, field_name: str) -> str | None:
    value = spec.get(field_name) if isinstance(spec, dict) else getattr(spec, field_name, None)
    return value if isinstance(value, str) and value else None


def _resolve_artifact_expression(root: dict[str, Any], expression: str) -> Any:
    value: Any = root
    for part in expression.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if 0 <= index < len(value) else None
        else:
            return None
        if value is None:
            return None
    return value


def _media_type_for_filename(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return "text/yaml"
    if suffix == ".json":
        return "application/json"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    media_type, _encoding = mimetypes.guess_type(filename)
    return media_type or "text/plain"


def _candidate_detail_data(
    *,
    tool_use_id: str,
    candidate_name: str,
    summary: str,
    cost_items: list[dict],
    total_monthly_cost: str,
    candidate_index: int | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "candidateName": candidate_name,
        "summary": summary,
        "costItems": cost_items,
        "totalMonthlyCost": total_monthly_cost,
    }
    data: dict[str, Any] = {
        "detailId": f"detail-{tool_use_id}",
        "toolUseId": tool_use_id,
        "detail": detail,
    }
    if candidate_index is not None:
        data["candidateIndex"] = candidate_index
        detail["candidateIndex"] = candidate_index
    return data


def _candidate_detail_data_from_tool_input(tool_use_id: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    candidate_name = _first_string_value(tool_input, ("candidate_name", "candidateName"))
    summary = _first_string_value(tool_input, ("summary",))
    total_monthly_cost = _first_string_value(tool_input, ("total_monthly_cost", "totalMonthlyCost"))
    candidate_index = _int_or_none(tool_input.get("candidate_index"))
    if candidate_index is None:
        candidate_index = _int_or_none(tool_input.get("candidateIndex"))
    cost_items = tool_input.get("cost_items")
    if not isinstance(cost_items, list):
        cost_items = tool_input.get("costItems")
    if candidate_name is None or summary is None or total_monthly_cost is None or not isinstance(cost_items, list):
        return None
    return _candidate_detail_data(
        tool_use_id=tool_use_id,
        candidate_name=candidate_name,
        summary=summary,
        cost_items=[item for item in cost_items if isinstance(item, dict)],
        total_monthly_cost=total_monthly_cost,
        candidate_index=candidate_index,
    )


def _diagram_data(event: DiagramEvent) -> dict[str, Any]:
    data: dict[str, Any] = {
        "candidateName": event.candidate_name,
        "templateContent": event.template_content,
        "mermaidSource": event.mermaid_source,
        "format": "mermaid",
    }
    if event.candidate_index is not None:
        data["candidateIndex"] = event.candidate_index
    return data


__all__ = [
    "PIPELINE_EVENTS_EXTENSION_URI",
    "PIPELINE_METADATA_SCHEMA_VERSION",
    "PipelineA2AContext",
    "PipelineEventTranslator",
    "safe_permission_metadata",
]
