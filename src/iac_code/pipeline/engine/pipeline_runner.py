"""PipelineRunner — generic config-driven pipeline orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from iac_code.agent.message import ContentBlock, Message, ToolResultBlock
from iac_code.i18n import _
from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource, ObservedResource
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.display_replay import DISPLAY_TRANSCRIPT_FILENAME
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.handoff import build_handoff_summary, terminal_outcome_from_completed_event
from iac_code.pipeline.engine.interrupt import InterruptController, InterruptVerdict
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.observability import PipelineObservability
from iac_code.pipeline.engine.public_errors import public_error, public_error_from_exception
from iac_code.pipeline.engine.resume_recovery import reconcile_resume_messages, user_message_already_in_resume
from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession, RestoreResult
from iac_code.pipeline.engine.state_machine import StateMachine
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import AllowUserEscapes, LoadedPipeline, OnCompletePolicy, StepSpec
from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.pipeline.engine.ui_contract import PipelineStepType, parse_selected_candidate
from iac_code.pipeline.engine.user_input import (
    PipelineInputContent,
    PipelineUserInput,
    normalize_pipeline_user_input,
)
from iac_code.types.stream_events import ResourceObservedEvent, StreamEvent
from iac_code.utils.public_errors import sanitize_public_text

logger = logging.getLogger(__name__)

_TERMINAL_SIDECAR_STATUSES = {"completed", "user_aborted", "failed", "discarded"}
_CURRENT_STEP_USER_INPUT_KEY = "current_step_user_input"
_CURRENT_STEP_USER_INPUT_CONTENT_KEY = "current_step_user_input_content"
_CURRENT_STEP_RESUME_MESSAGES_KEY = "current_step_resume_messages"
_CURRENT_STEP_PRECOMPLETED_TOOLS_KEY = "current_step_precompleted_tools"
_PENDING_ASK_USER_QUESTION_RESUME_KEY = "pending_ask_user_question_resume"
_PENDING_INPUT_KIND_KEY = "pending_input_kind"
_PIPELINE_PAUSE_CONFIRMATION_KIND = "pipeline_pause_confirmation"
_REAL_RESTORE_FAILURE_REASONS = {
    "corrupt_meta",
    "invalid_meta",
    "unknown_status",
    "missing_context",
    "corrupt_context",
    "invalid_context",
}


class PipelineStatePersistenceError(RuntimeError):
    """Raised when recovery-critical pipeline state cannot be persisted."""

    def __init__(self, message: str, *, step_id: str | None = None) -> None:
        super().__init__(message)
        self.step_id = step_id


def _string_answer_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _user_input_received_data(
    user_input: PipelineUserInput,
    *,
    ui_mode: str | None,
    selected_index: int | None,
    waiting_options: list[Any],
) -> dict[str, Any]:
    data: dict[str, Any] = {"user_input_length": len(user_input.display_text)}
    if user_input.has_images:
        data["has_images"] = True
    if ui_mode != "candidate_selection":
        return data
    data.update(
        {
            "kind": "candidate_selection",
            "selected_value": user_input.display_text,
        }
    )
    if selected_index is not None:
        data["selected_index"] = selected_index
        if 0 <= selected_index < len(waiting_options):
            selected_option = waiting_options[selected_index]
            if isinstance(selected_option, dict):
                data["selected_option"] = dict(selected_option)
    return data


def _pipeline_pause_input_received_data(user_input: PipelineUserInput) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": _PIPELINE_PAUSE_CONFIRMATION_KIND,
        "user_input_length": len(user_input.display_text),
    }
    if user_input.has_images:
        data["has_images"] = True
    return data


def _serialize_pipeline_input_content(content: PipelineInputContent) -> str | list[dict[str, Any]]:
    dumped = Message(role="user", content=content).to_dict()["content"]
    return cast(str | list[dict[str, Any]], dumped)


def _deserialize_pipeline_input_content(value: Any) -> PipelineInputContent | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    try:
        content = Message(role="user", content=value).content
    except Exception:
        return None
    return content if isinstance(content, list) else None


def _serialize_pipeline_messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [message.to_dict() for message in messages]


def _deserialize_pipeline_messages(value: Any) -> list[Message] | None:
    if not isinstance(value, list):
        return None
    messages: list[Message] = []
    try:
        for item in value:
            if not isinstance(item, dict):
                return None
            messages.append(Message.from_dict(item))
    except Exception:
        return None
    return messages


def _deserialize_precompleted_tools(value: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    tools: dict[str, dict[str, Any]] = {}
    for name, payload in value.items():
        if isinstance(name, str) and isinstance(payload, dict):
            tools[name] = dict(payload)
    return tools


def _serialize_ask_user_question_resume_state(
    *,
    user_message: PipelineInputContent,
    resume_messages: list[Message] | None,
    precompleted_tools: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"user_message": _serialize_pipeline_input_content(user_message)}
    if resume_messages is not None:
        state["resume_messages"] = _serialize_pipeline_messages(resume_messages)
    if precompleted_tools is not None:
        state["precompleted_tools"] = precompleted_tools
    return state


def _deserialize_ask_user_question_resume_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    user_message = _deserialize_pipeline_input_content(value.get("user_message"))
    if user_message is None:
        return None
    return {
        "user_message": user_message,
        "resume_messages": _deserialize_pipeline_messages(value.get("resume_messages")),
        "precompleted_tools": _deserialize_precompleted_tools(value.get("precompleted_tools")),
    }


def _normalize_failed_sub_pipeline_completed_event(event: PipelineEvent) -> None:
    if event.type != PipelineEventType.SUB_PIPELINE_COMPLETED or not event.data.get("failed", False):
        return

    error = event.data.get("error")
    if error is not None:
        event.data["error"] = sanitize_public_text(str(error))
    if "error_summary" in event.data:
        event.data["error_summary"] = sanitize_public_text(str(event.data["error_summary"]))
    else:
        event.data["error_summary"] = event.data.get("error") or "Unknown error"

    if "error_details" not in event.data:
        failure = public_error(
            message=event.data["error_summary"],
            error_type="SubPipelineFailed",
        )
        event.data["error"] = failure.summary
        event.data["error_summary"] = failure.summary
        event.data["error_details"] = failure.details


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_int_value(value: Any) -> int | None:
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


def _candidate_index_from_pending_input(pending_input: dict[str, Any] | None) -> int | None:
    if not pending_input:
        return None
    candidate = _dict_value(pending_input.get("candidate"))
    return _optional_int_value(candidate.get("index"))


def _pending_ask_tool_use_id(pending_input: dict[str, Any] | None, resume_messages: list[Message]) -> str | None:
    if pending_input:
        tool_use_id = pending_input.get("toolUseId") or pending_input.get("tool_use_id")
        if isinstance(tool_use_id, str) and tool_use_id:
            return tool_use_id
    return _latest_ask_user_question_tool_use_id(resume_messages)


def _latest_ask_user_question_tool_use_id(messages: list[Message]) -> str | None:
    for message in reversed(messages):
        content = message.content
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            name = getattr(block, "name", None)
            block_id = getattr(block, "id", None)
            if isinstance(block, dict):
                name = block.get("name")
                block_id = block.get("id")
            if name == "ask_user_question" and isinstance(block_id, str) and block_id:
                return block_id
    return None


def _initial_prompt_text(initial_prompt: str | list[ContentBlock]) -> str:
    if isinstance(initial_prompt, str):
        return initial_prompt
    parts: list[str] = []
    for block in initial_prompt:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        content = getattr(block, "content", None)
        if isinstance(content, str):
            parts.append(content)
            continue
        thinking = getattr(block, "thinking", None)
        if isinstance(thinking, str):
            parts.append(thinking)
    return "\n".join(parts)


@dataclass
class CandidateSentinel:
    """Signals that a candidate task has finished."""

    candidate_index: int


@dataclass
class RestartInfo:
    """P-I14: structured per-candidate restart info, replacing dict[str, Any].

    Used by _schedule_candidate_restart / _execute_parallel_sub_pipeline /
    run_candidate to convey rollback state without dict-key typos.
    """

    start_from_step: str | None
    preserved_conclusions: dict[str, Any]
    rollback_context: str | None = None
    rollback_input: PipelineInputContent | None = None


@dataclass
class PromptContext:
    """Resolved AgentLoop context that would be used by the next model call."""

    scope: str
    step_id: str
    system_prompt: str
    messages: list[Message]
    agent_loop_session_id: str
    initial_prompt: str = ""
    candidate_index: int | None = None
    candidate_name: str = ""
    sub_pipeline_id: str = ""


class PipelineRunner:
    """Generic pipeline orchestrator driven by pipeline.yaml configuration."""

    def __init__(
        self,
        pipeline_dir: Path,
        provider_manager: Any,
        base_tool_registry: Any,
        session_storage: Any,
        session_id: str,
        cwd: str | None = None,
        permission_context_getter: Callable[[], Any] | None = None,
        memory_content_getter: Callable[[], str] | None = None,
        auto_trigger_skills: list[Any] | None = None,
        resume_from_sidecar: bool = False,
        surface: str = "repl",
    ) -> None:
        self._session_storage = session_storage
        self._session_id = session_id
        self._cwd = cwd or os.getcwd()
        self._permission_context_getter = permission_context_getter
        self._memory_content_getter = memory_content_getter
        self._auto_trigger_skills = auto_trigger_skills or []
        self._surface = surface

        self._pipeline_dir = pipeline_dir
        self._loaded: LoadedPipeline = load_pipeline_dir(pipeline_dir)
        self._pipeline_identity = self._build_pipeline_identity(pipeline_dir)
        self._observability = PipelineObservability(
            pipeline_name=self._loaded.name,
            session_id=session_id,
            cwd=self._cwd,
        )
        self._telemetry_correlation: dict[str, str] = {}
        self.context = PipelineContext(self._loaded.context_dependencies)
        self.state_machine = StateMachine(self._loaded.steps, self._loaded.max_rollbacks)
        self._sidecar_status: str | None = None
        self._sidecar_restore_result: RestoreResult | None = None
        self._current_step_user_input: str | None = None
        self._restored_current_step_user_input: PipelineUserInput | None = None
        self._restored_current_step_resume_messages: list[Message] | None = None
        self._restored_current_step_precompleted_tools: dict[str, dict[str, Any]] | None = None
        self._last_applied_interrupt_verdict: InterruptVerdict | None = None
        self._waiting_input_started_at: dict[str, float] = {}
        self._waiting_input_options_by_step: dict[str, list[Any]] = {}
        self._step_attempts: dict[str, int] = {}

        # Single shared pause event for all AgentLoops spawned by this pipeline.
        # Initially set (= not paused). REPL clears/sets it around interrupt prompt.
        self._agent_pause_event = asyncio.Event()
        self._agent_pause_event.set()

        # Sidecar lives at <projects>/<cwd>/<session_id>/pipeline/ — nested
        # under the session directory (main's directory-format session layout)
        # rather than the legacy sibling <session_id>.pipeline/ that pre-dated
        # the directory format. SessionStorage.session_dir is the canonical
        # accessor for <projects>/<cwd>/<session_id>/.
        if hasattr(session_storage, "session_dir"):
            raw_session_dir = session_storage.session_dir(self._cwd, session_id)
            if isinstance(raw_session_dir, (str, Path)):
                self.session = PipelineSession(Path(raw_session_dir) / "pipeline")
            else:
                self.session = None
        else:
            self.session = None

        self._attempts: dict[str, Any] = {"next_attempt_number": 1, "items": {}}
        self._execution: dict[str, Any] = {}
        self._transcript_storage = None
        if self.session is not None:
            from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage

            self._transcript_storage = PipelineTranscriptStorage(self.session.session_dir)
            self._seed_attempt_counter_from_sidecar()

        self._step_executor = StepExecutor(
            provider_manager=provider_manager,
            base_tool_registry=base_tool_registry,
            pipeline=self._loaded,
            pipeline_dir=pipeline_dir,
            session_storage=self._transcript_storage or session_storage,
            cwd=self._cwd,
            pause_event=self._agent_pause_event,
            permission_context_getter=self._permission_context_getter,
            memory_content_getter=self._memory_content_getter,
            auto_trigger_skills=self._auto_trigger_skills,
            surface=self._surface,
        )
        self._apply_telemetry_correlation(self._step_executor)

        if resume_from_sidecar and self.session and self.session.exists():
            self.restore_from_sidecar_sync()

        self._interrupt_controller = InterruptController(provider_manager, self._get_state_for_judge, pipeline_dir)
        # Maps candidate_index → state dict for currently-running candidates.
        # Entry is added in run_candidate's body and removed in its finally,
        # so absence means the candidate has either not started yet or has
        # completed/cancelled. Read by judge supplement injection and by
        # apply_hard_interrupt's candidate-scope escalation logic.
        self._active_candidates: dict[int, Any] = {}
        self._pending_candidate_restarts: dict[int, RestartInfo] = {}
        self._rollback_context: str | None = None
        self._rollback_input: PipelineInputContent | None = None
        self._current_step_user_input_content: PipelineInputContent | None = None
        self._current_step_resume_messages: list[Message] | None = None
        self._current_step_precompleted_tools: dict[str, dict[str, Any]] | None = None
        self._restored_supplement: dict[str, Any] | None = None
        # Total candidate count for the currently-executing parallel sub-pipeline
        # step. 0 when no parallel step is in flight. Used by apply_hard_interrupt
        # to detect scope="all" with partial completion and escalate to parent
        # rollback (otherwise completed candidates would silently keep stale
        # conclusions while running ones get restarted with new context).
        self._parallel_candidates_total: int = 0
        # Live list of SubPipelineExecutors for the currently-running parallel
        # step. None when no parallel step is in flight. Used by
        # iter_active_agent_loops to surface per-candidate AgentLoops to the
        # /status aggregator (problem 6).
        self._current_sub_executor_list: list[SubPipelineExecutor] | None = None

    @property
    def pipeline_name(self) -> str:
        return self._loaded.name

    @property
    def allow_user_escapes(self) -> AllowUserEscapes:
        """Pipeline-level toggles for $/!/slash user escapes (problem 5)."""
        return self._loaded.allow_user_escapes

    @property
    def on_complete_policy(self) -> OnCompletePolicy | None:
        return self._loaded.on_complete

    @property
    def emit_stack_events(self) -> bool:
        return self._loaded.emit_stack_events

    @property
    def sidecar_status(self) -> str | None:
        return self._sidecar_status

    @property
    def sidecar_restore_result(self) -> RestoreResult | None:
        return self._sidecar_restore_result

    @property
    def last_applied_interrupt_verdict(self) -> InterruptVerdict | None:
        return self._last_applied_interrupt_verdict

    def set_telemetry_correlation(
        self,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        self._telemetry_correlation = {
            key: value
            for key, value in {
                "task_id": task_id,
                "context_id": context_id,
                "pipeline_run_id": pipeline_run_id,
            }.items()
            if value
        }
        self._observability.set_correlation(
            task_id=task_id,
            context_id=context_id,
            pipeline_run_id=pipeline_run_id,
        )
        self._apply_telemetry_correlation(self._step_executor)

    def _apply_telemetry_correlation(self, executor: Any) -> None:
        setter = getattr(executor, "set_telemetry_correlation", None)
        if callable(setter):
            setter(**self._telemetry_correlation)

    @property
    def display_transcript_path(self) -> Path | None:
        if self.session is None:
            return None
        return self.session.session_dir / DISPLAY_TRANSCRIPT_FILENAME

    def cleanup_ledger(self) -> CleanupLedger | None:
        session = getattr(self, "session", None)
        if session is None:
            return None
        session_dir = getattr(session, "session_dir", None)
        if not isinstance(session_dir, (str, Path)):
            return None
        return CleanupLedger(Path(session_dir) / "cleanup.yaml")

    def _handle_resource_observed(
        self,
        step: StepSpec,
        event: ResourceObservedEvent,
        *,
        attempt_id: str | None,
    ) -> None:
        hook = getattr(step, "on_resource_observed", None)
        ledger = self.cleanup_ledger()
        if ledger is None or not callable(hook):
            return
        try:
            result = hook(
                self.context,
                event,
                ledger=ledger,
                step_id=step.step_id,
                attempt_id=attempt_id,
            )
        except Exception:
            logger.warning("Pipeline resource-observed hook failed: step_id=%s", step.step_id, exc_info=True)
            return
        for observed in self._observed_resources_from_hook_result(result):
            try:
                ledger.record_observed(observed)
            except Exception as exc:
                logger.warning(
                    "Failed to persist observed cleanup resource: step_id=%s resource_id=%s error=%s",
                    step.step_id,
                    observed.resource_id,
                    exc,
                    exc_info=True,
                )

    def _mark_rollback_cleanup_required(
        self,
        step: StepSpec,
        to_step: str,
        reason: str,
        *,
        from_attempt_id: str | None,
    ) -> None:
        hook = getattr(step, "on_rollback_cleanup_required", None)
        ledger = self.cleanup_ledger()
        if ledger is None or not callable(hook):
            return
        try:
            result = hook(
                self.context,
                ledger=ledger,
                from_step=step.step_id,
                from_attempt_id=from_attempt_id,
                to_step=to_step,
                reason=reason,
            )
        except Exception:
            logger.warning("Pipeline rollback cleanup hook failed: step_id=%s", step.step_id, exc_info=True)
            return
        resources = self._cleanup_resources_from_hook_result(result)
        if resources:
            ledger.mark_cleanup_required(resources, source_step_id=step.step_id, reason=reason)

    @staticmethod
    def _observed_resources_from_hook_result(result: object) -> list[ObservedResource]:
        if isinstance(result, ObservedResource):
            return [result]
        if isinstance(result, list):
            return [item for item in result if isinstance(item, ObservedResource)]
        return []

    @staticmethod
    def _cleanup_resources_from_hook_result(result: object) -> list[CleanupResource]:
        if isinstance(result, CleanupResource):
            return [result]
        if isinstance(result, list):
            return [item for item in result if isinstance(item, CleanupResource)]
        return []

    def _build_pipeline_identity(self, pipeline_dir: Path) -> PipelineIdentity:
        yaml_path = pipeline_dir / "pipeline.yaml"
        digest = hashlib.sha256(yaml_path.read_bytes()).hexdigest()
        return PipelineIdentity(
            pipeline_name=self._loaded.name,
            step_ids=[step.step_id for step in self._loaded.steps],
            sub_pipeline_step_ids={
                name: [step.step_id for step in sub_pipeline.steps]
                for name, sub_pipeline in sorted(self._loaded.sub_pipelines.items())
            },
            pipeline_fingerprint=digest,
        )

    def should_switch_to_normal(self, completed_event_data: dict) -> bool:
        policy = self.on_complete_policy
        if policy is None or policy.action != "switch_to_normal":
            return False
        outcome = terminal_outcome_from_completed_event(completed_event_data)
        return outcome in policy.apply_on

    def build_normal_handoff_summary(self, completed_event_data: dict) -> str:
        policy = self.on_complete_policy
        include_fields = policy.handoff_context.include if policy is not None else []
        outcome = terminal_outcome_from_completed_event(completed_event_data)
        return build_handoff_summary(
            pipeline_name=self.pipeline_name,
            outcome=outcome,
            context_snapshot=self.context.snapshot(),
            include_fields=include_fields,
        )

    def mark_normal_handoff(self, status: str, failed_reason: str | None = None) -> None:
        """Record terminal pipeline-to-normal handoff metadata without deleting the sidecar."""
        if not self.session:
            self._sidecar_status = "completed"
            return

        session = self.session
        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()
        current_step = self._terminal_current_step_id()
        normal_handoff = {
            "status": status,
            "switched_to_normal": status in {"succeeded", "failed"},
            "root_session_id": self._session_id,
            "summary_message_appended": status == "succeeded",
            "failed_reason": failed_reason,
        }
        metadata_kwargs: dict[str, Any] = {
            "attempts": dict(self._attempts),
            "normal_handoff": normal_handoff,
        }
        if self._execution:
            metadata_kwargs["execution"] = dict(self._execution)

        def save() -> None:
            session.save_completed_sync(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason="normal handoff",
                **metadata_kwargs,
            )

        self._try_save_sidecar_sync("completed", "save_normal_handoff", save)

    def _terminal_current_step_id(self) -> str:
        try:
            if not self.state_machine.is_complete:
                return self.state_machine.current_step.step_id
            order = getattr(self.state_machine, "_order", [])
            return str(order[-1]) if order else ""
        except (AttributeError, IndexError):
            return ""

    def restore_from_sidecar_sync(self) -> RestoreResult:
        if not self.session:
            result = RestoreResult(ok=False, reason="missing_session")
            self._sidecar_restore_result = result
            return result
        result = self.session.restore_sync(self._pipeline_identity)
        self._sidecar_restore_result = result
        self._sidecar_status = result.status
        if not result.ok:
            if self._is_real_sidecar_restore_failure(result):
                failure = public_error(
                    message=result.reason or "restore failed",
                    error_type="RestoreFailed",
                )
                self._observability.sidecar_failed(
                    operation="restore",
                    status=result.status,
                    reason=result.reason,
                    error_type=failure.details["type"],
                    error_summary=failure.summary,
                    error_id=failure.error_id,
                )
            return result
        assert result.state_machine_snapshot is not None
        assert result.context_snapshot is not None
        self.state_machine = StateMachine.from_snapshot(
            result.state_machine_snapshot,
            self._loaded.steps,
            max_rollbacks=self._loaded.max_rollbacks,
        )
        self._step_attempts = self._step_attempts_from_snapshot(result.state_machine_snapshot)
        self._restore_current_step_user_input_from_snapshot(result.state_machine_snapshot)
        self.context = PipelineContext.from_snapshot(result.context_snapshot, self._loaded.context_dependencies)
        if result.execution is not None:
            self._execution = dict(result.execution)
        if result.attempts is not None:
            self._attempts = dict(result.attempts)
            self._attempts.setdefault("next_attempt_number", 1)
            self._attempts.setdefault("items", {})
            self._seed_attempt_counter_from_sidecar()
        self._observability.pipeline_resumed(status=result.status, current_step=result.current_step)
        return result

    def pending_input_kind(self) -> str | None:
        kind = self._execution.get(_PENDING_INPUT_KIND_KEY) if isinstance(self._execution, dict) else None
        return kind if isinstance(kind, str) else None

    def has_pending_pipeline_pause_confirmation(self) -> bool:
        return self.pending_input_kind() == _PIPELINE_PAUSE_CONFIRMATION_KIND

    def _set_pending_input_kind(self, kind: str | None) -> None:
        if kind is None:
            self._execution.pop(_PENDING_INPUT_KIND_KEY, None)
            return
        self._execution[_PENDING_INPUT_KIND_KEY] = kind

    async def restore_from_sidecar(self) -> RestoreResult:
        return self.restore_from_sidecar_sync()

    def continue_from_sidecar(
        self, user_input: str | list[ContentBlock] | PipelineUserInput | None = None
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        if not user_input:
            return self._continue_from_current(resume_running_step=True)
        return self._continue_from_sidecar_with_input(user_input)

    async def _continue_from_sidecar_with_input(
        self, user_input: str | list[ContentBlock] | PipelineUserInput
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        pipeline_input = normalize_pipeline_user_input(user_input)
        user_text = pipeline_input.display_text
        was_pause_confirmation = self.has_pending_pipeline_pause_confirmation()
        if was_pause_confirmation:
            self._set_pending_input_kind(None)
            current_step = getattr(self.state_machine, "current_step", None)
            step_id = getattr(current_step, "step_id", None)
            self._set_current_step_user_input(pipeline_input)
            try:
                await self._save_running(str(step_id or ""), reason="pipeline pause confirmation received")
            except PipelineStatePersistenceError as exc:
                yield self._persistence_failure_event(exc)
                return
            yield PipelineEvent(
                type=PipelineEventType.USER_INPUT_RECEIVED,
                step_id=step_id,
                timestamp=time.time(),
                data=_pipeline_pause_input_received_data(pipeline_input),
            )
            if user_text.strip().lower() == "continue":
                self.resume_agent_loops()
                async for event in self._continue_from_current(user_input=None, resume_running_step=True):
                    yield event
                return
        try:
            judge_input: str | PipelineUserInput = pipeline_input if pipeline_input.has_images else user_text
            verdict = await self._interrupt_controller.judge(judge_input)
        except Exception as exc:
            logger.warning("Interrupt judge failed during sidecar continuation: %s", exc, exc_info=True)
            verdict = self._apply_interrupt_judge_failure_policy(
                InterruptVerdict(action="continue", reason=f"judge failed: {exc}")
            )
            async for event in self._continue_after_sidecar_judgment_failure(verdict, user_input=pipeline_input):
                yield event
            return

        if self._is_judgment_error_verdict(verdict):
            verdict = self._apply_interrupt_judge_failure_policy(verdict)
            async for event in self._continue_after_sidecar_judgment_failure(verdict, user_input=pipeline_input):
                yield event
            return
        if verdict.action == "supplement":
            self.resume_agent_loops()
            if self._current_step_is_parallel_sub_pipeline():
                self._restored_supplement = {
                    "message": pipeline_input.content,
                    "target": verdict.supplement_target,
                }
                try:
                    async for event in self._continue_from_current(user_input=None, resume_running_step=True):
                        yield event
                finally:
                    self._restored_supplement = None
                return
            async for event in self._continue_from_current(
                **self._continue_input_kwargs(pipeline_input),
                resume_running_step=True,
            ):
                yield event
            return
        if verdict.action == "hard_interrupt":
            async for event in self._continue_after_sidecar_hard_interrupt(verdict, source_input=pipeline_input):
                yield event
            return

        self.resume_agent_loops()
        async for event in self._continue_from_current(resume_running_step=True):
            yield event

    async def _continue_after_sidecar_judgment_failure(
        self, verdict: InterruptVerdict, *, user_input: PipelineUserInput
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        if verdict.paused:
            try:
                yield await self._save_and_emit_interrupt_pause(verdict)
            except PipelineStatePersistenceError as exc:
                yield self._persistence_failure_event(exc)
            return
        if verdict.action == "hard_interrupt":
            async for event in self._continue_after_sidecar_hard_interrupt(verdict, source_input=user_input):
                yield event
            return
        self.resume_agent_loops()
        async for event in self._continue_from_current(
            **self._continue_input_kwargs(user_input),
            resume_running_step=True,
        ):
            yield event

    async def _continue_after_sidecar_hard_interrupt(
        self, verdict: InterruptVerdict, *, source_input: PipelineUserInput | None = None
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        try:
            if source_input is not None and source_input.has_images:
                parent_rollback = self.apply_hard_interrupt(verdict, source_input=source_input)
            else:
                parent_rollback = self.apply_hard_interrupt(verdict)
        except PipelineStatePersistenceError as exc:
            yield self._persistence_failure_event(exc)
            return
        if self.sidecar_status == "failed":
            current_step = getattr(self.state_machine, "current_step", None)
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=getattr(current_step, "step_id", None),
                timestamp=time.time(),
                data={"total_steps": self.state_machine.total_steps, "failed": True},
            )
            return
        self.resume_agent_loops()
        if parent_rollback:
            async for event in self.continue_after_interrupt():
                yield event
            return
        async for event in self._continue_from_current(resume_running_step=True):
            yield event

    async def save_interrupt_pause(self, verdict: InterruptVerdict) -> PipelineEvent:
        return await self._save_and_emit_interrupt_pause(verdict)

    async def _save_and_emit_interrupt_pause(self, verdict: InterruptVerdict) -> PipelineEvent:
        current_step = getattr(self.state_machine, "current_step", None)
        step_id = getattr(current_step, "step_id", "") or ""
        step_index = getattr(self.state_machine, "current_step_index", 0) + 1
        step_attempt = self._current_step_attempt(step_id) if step_id else 1
        reason = verdict.reason or "pipeline paused for safety"
        prompt = (
            "Pipeline paused because interrupt classification failed during a side-effect step. "
            "Reply with continue, rollback, or cancel instructions."
        )
        self._set_pending_input_kind(_PIPELINE_PAUSE_CONFIRMATION_KIND)
        await self._save_waiting_input(step_id)
        if step_id:
            self._waiting_input_started_at[step_id] = self._observability.now()
            self._waiting_input_options_by_step[step_id] = []
        self._observability.user_input_required(
            step_id=step_id,
            step_index=step_index,
            step_attempt=step_attempt,
            total_steps=self.state_machine.total_steps,
            step_type=getattr(current_step, "step_type", None),
            ui_mode=getattr(current_step, "ui_mode", None),
            option_count=0,
            prompt=prompt,
        )
        return PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id=step_id,
            timestamp=time.time(),
            data={
                "kind": _PIPELINE_PAUSE_CONFIRMATION_KIND,
                "step_id": step_id,
                "prompt": prompt,
                "reason": reason,
                "paused": True,
                "options": [],
            },
        )

    @staticmethod
    def _is_judgment_error_verdict(verdict: InterruptVerdict) -> bool:
        reason = verdict.reason or ""
        return verdict.action == "continue" and reason.startswith(
            ("judge failed", "parse failed", "fallback parse failed")
        )

    def _current_step_is_parallel_sub_pipeline(self) -> bool:
        current_step = getattr(self.state_machine, "current_step", None)
        return getattr(current_step, "step_type", None) == PipelineStepType.PARALLEL_SUB_PIPELINE.value

    def _execution_matches_current_parallel_step(self) -> bool:
        execution = getattr(self, "_execution", {}) or {}
        current_step = getattr(self.state_machine, "current_step", None)
        return (
            self._current_step_is_parallel_sub_pipeline()
            and execution.get("kind") == "parallel_sub_pipeline"
            and execution.get("step_id") == getattr(current_step, "step_id", None)
        )

    def _persisted_parallel_candidate_states(self) -> dict[int, dict[str, Any]]:
        if not self._execution_matches_current_parallel_step():
            return {}
        candidates = self._execution.get("candidates", {})
        if not isinstance(candidates, dict):
            return {}
        states: dict[int, dict[str, Any]] = {}
        for raw_idx, candidate_state in candidates.items():
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if isinstance(candidate_state, dict):
                states[idx] = candidate_state
        return states

    def _persisted_parallel_candidate_state(self, idx: int) -> dict[str, Any] | None:
        return self._persisted_parallel_candidate_states().get(idx)

    @staticmethod
    def _persisted_candidate_restart_info(state: dict[str, Any]) -> RestartInfo | None:
        restart = state.get("pending_restart")
        if not isinstance(restart, dict):
            return None
        start_from_step = restart.get("start_from_step")
        if start_from_step is not None and not isinstance(start_from_step, str):
            return None
        rollback_context = restart.get("rollback_context")
        if rollback_context is not None and not isinstance(rollback_context, str):
            rollback_context = None
        preserved_conclusions = restart.get("preserved_conclusions")
        if not isinstance(preserved_conclusions, dict):
            preserved_conclusions = {}
        rollback_input = _deserialize_pipeline_input_content(restart.get("rollback_input"))
        return RestartInfo(
            start_from_step=start_from_step,
            preserved_conclusions=preserved_conclusions,
            rollback_context=rollback_context,
            rollback_input=rollback_input,
        )

    def _persisted_parallel_candidate_indices(self) -> list[int]:
        return sorted(self._persisted_parallel_candidate_states())

    def _candidate_is_running_for_interrupt(self, idx: int) -> bool:
        if idx in self._active_candidates:
            return True
        persisted = self._persisted_parallel_candidate_state(idx)
        return bool(persisted and persisted.get("status") == "running")

    def _can_interrupt_rollback_to(self, target: str) -> tuple[bool, str | None]:
        validator = getattr(self.state_machine, "can_interrupt_rollback_to", None)
        if not callable(validator):
            return True, None
        result = validator(target)
        if isinstance(result, tuple) and len(result) == 2:
            ok, reason = result
            return bool(ok), reason
        logger.debug("Interrupt rollback validator returned unexpected result %r; allowing rollback", result)
        return True, None

    def _is_real_sidecar_restore_failure(self, result: RestoreResult) -> bool:
        if result.ok:
            return False
        if result.status in _TERMINAL_SIDECAR_STATUSES:
            return False
        if result.reason in {None, "missing_session", "pipeline_identity_mismatch"}:
            return False
        if result.reason == "missing_meta":
            session_dir = getattr(self.session, "session_dir", None)
            return isinstance(session_dir, Path) and session_dir.exists()
        return result.reason in _REAL_RESTORE_FAILURE_REASONS

    def _state_machine_snapshot_for_sidecar(self) -> dict[str, Any]:
        snapshot = self.state_machine.to_snapshot()
        snapshot["step_attempts"] = dict(getattr(self, "_step_attempts", {}))
        current_step_user_input = getattr(self, "_current_step_user_input", None)
        if current_step_user_input is not None:
            snapshot[_CURRENT_STEP_USER_INPUT_KEY] = current_step_user_input
        current_step_user_input_content = getattr(self, "_current_step_user_input_content", None)
        if current_step_user_input_content is not None:
            snapshot[_CURRENT_STEP_USER_INPUT_CONTENT_KEY] = _serialize_pipeline_input_content(
                current_step_user_input_content
            )
        current_step_resume_messages = getattr(self, "_current_step_resume_messages", None)
        if current_step_resume_messages is not None:
            snapshot[_CURRENT_STEP_RESUME_MESSAGES_KEY] = _serialize_pipeline_messages(current_step_resume_messages)
        current_step_precompleted_tools = getattr(self, "_current_step_precompleted_tools", None)
        if current_step_precompleted_tools is not None:
            snapshot[_CURRENT_STEP_PRECOMPLETED_TOOLS_KEY] = current_step_precompleted_tools
        return snapshot

    def _restore_current_step_user_input_from_snapshot(self, snapshot: dict[str, Any]) -> None:
        restored_display_text = snapshot.get(_CURRENT_STEP_USER_INPUT_KEY)
        if not isinstance(restored_display_text, str):
            restored_display_text = None
        restored_content = _deserialize_pipeline_input_content(snapshot.get(_CURRENT_STEP_USER_INPUT_CONTENT_KEY))
        if restored_content is None:
            restored_content = restored_display_text
        self._restored_current_step_user_input = (
            normalize_pipeline_user_input(restored_content, display_text=restored_display_text)
            if restored_content is not None
            else None
        )
        self._current_step_user_input = restored_display_text
        self._current_step_user_input_content = (
            self._restored_current_step_user_input.content
            if self._restored_current_step_user_input is not None and self._restored_current_step_user_input.has_images
            else None
        )
        self._restored_current_step_resume_messages = _deserialize_pipeline_messages(
            snapshot.get(_CURRENT_STEP_RESUME_MESSAGES_KEY)
        )
        self._restored_current_step_precompleted_tools = _deserialize_precompleted_tools(
            snapshot.get(_CURRENT_STEP_PRECOMPLETED_TOOLS_KEY)
        )
        self._current_step_resume_messages = self._restored_current_step_resume_messages
        self._current_step_precompleted_tools = self._restored_current_step_precompleted_tools

    def _set_current_step_user_input(
        self,
        user_input: str | list[ContentBlock] | PipelineUserInput | None,
        *,
        display_text: str | None = None,
    ) -> None:
        if user_input is None:
            self._current_step_user_input = None
            self._current_step_user_input_content = None
            self._set_current_step_resume_state()
            return
        pipeline_input = normalize_pipeline_user_input(user_input, display_text=display_text)
        self._current_step_user_input = display_text if display_text is not None else pipeline_input.display_text
        self._current_step_user_input_content = pipeline_input.content if pipeline_input.has_images else None

    def _set_current_step_resume_state(
        self,
        *,
        resume_messages: list[Message] | None = None,
        precompleted_tools: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._current_step_resume_messages = list(resume_messages) if resume_messages is not None else None
        self._current_step_precompleted_tools = dict(precompleted_tools) if precompleted_tools is not None else None

    @staticmethod
    def _continue_input_kwargs(user_input: PipelineUserInput) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"user_input": user_input.content}
        if not isinstance(user_input.content, str) or user_input.display_text != user_input.content:
            kwargs["user_input_display_text"] = user_input.display_text
        return kwargs

    def _consume_restored_current_step_user_input(self) -> PipelineUserInput | None:
        user_input = self._restored_current_step_user_input
        self._restored_current_step_user_input = None
        return user_input

    def _consume_restored_current_step_resume_state(
        self,
    ) -> tuple[list[Message] | None, dict[str, dict[str, Any]] | None]:
        resume_messages = self._restored_current_step_resume_messages
        precompleted_tools = self._restored_current_step_precompleted_tools
        self._restored_current_step_resume_messages = None
        self._restored_current_step_precompleted_tools = None
        return resume_messages, precompleted_tools

    def _step_attempts_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, int]:
        attempts = snapshot.get("step_attempts", {})
        if not isinstance(attempts, dict):
            return {}
        valid_step_ids = {step.step_id for step in self._loaded.steps}
        restored: dict[str, int] = {}
        for step_id, attempt in attempts.items():
            if not isinstance(step_id, str) or step_id not in valid_step_ids:
                continue
            if type(attempt) is int and attempt > 0:
                restored[step_id] = attempt
        return restored

    def _next_attempt_id(self) -> str:
        number = int(self._attempts.get("next_attempt_number", 1))
        while True:
            attempt_id = f"att_{number:04d}"
            number += 1
            self._attempts["next_attempt_number"] = number
            if not self._attempt_id_in_use(attempt_id):
                return attempt_id

    @staticmethod
    def _attempt_number_after_id(value: Any) -> int | None:
        if not isinstance(value, str):
            return None
        if value.startswith("transcript_att_"):
            suffix = value.removeprefix("transcript_att_")
        elif value.startswith("att_"):
            suffix = value.removeprefix("att_")
        else:
            return None
        if not suffix.isdigit():
            return None
        return int(suffix) + 1

    def _attempt_id_in_use(self, attempt_id: str) -> bool:
        if attempt_id in self._attempts.setdefault("items", {}):
            return True
        transcript_id = f"transcript_{attempt_id}"
        transcript_storage = getattr(self, "_transcript_storage", None)
        return bool(transcript_storage and transcript_storage.exists(getattr(self, "_cwd", ""), transcript_id))

    def _seed_attempt_counter_from_sidecar(self) -> None:
        next_number = int(self._attempts.get("next_attempt_number", 1))
        attempts = self.session.load_attempts_metadata() if self.session is not None else None
        next_number = max(next_number, self._next_attempt_number_from_attempts(attempts))
        if self._transcript_storage is not None:
            for transcript_id in self._transcript_storage.list_transcript_ids():
                next_number = max(next_number, self._attempt_number_after_id(transcript_id) or 1)
        self._attempts["next_attempt_number"] = next_number

    def _next_attempt_number_from_attempts(self, attempts: dict[str, Any] | None) -> int:
        if not isinstance(attempts, dict):
            return 1
        next_number = 1
        raw_next = attempts.get("next_attempt_number")
        if isinstance(raw_next, int):
            next_number = max(next_number, raw_next)
        elif isinstance(raw_next, str) and raw_next.isdigit():
            next_number = max(next_number, int(raw_next))
        items = attempts.get("items")
        if isinstance(items, dict):
            for attempt_id, attempt in items.items():
                next_number = max(next_number, self._attempt_number_after_id(attempt_id) or 1)
                if isinstance(attempt, dict):
                    next_number = max(next_number, self._attempt_number_after_id(attempt.get("attempt_id")) or 1)
                    next_number = max(next_number, self._attempt_number_after_id(attempt.get("transcript_id")) or 1)
        return next_number

    def _create_parent_attempt_record(self, step_id: str) -> dict[str, Any]:
        attempt_id = self._next_attempt_id()
        transcript_id = f"transcript_{attempt_id}"
        attempt = {
            "attempt_id": attempt_id,
            "scope": "parent",
            "step_id": step_id,
            "status": "running",
            "transcript_id": transcript_id,
        }
        self._attempts.setdefault("items", {})[attempt_id] = attempt
        return attempt

    def _create_parent_attempt(self, step_id: str) -> dict[str, Any]:
        attempt = self._create_parent_attempt_record(step_id)
        self._execution = {
            "kind": "step",
            "step_id": step_id,
            "active_attempt_id": attempt["attempt_id"],
            "transcript_id": attempt["transcript_id"],
        }
        return attempt

    def _create_sub_step_attempt(
        self,
        *,
        parent_step_id: str,
        candidate_index: int,
        sub_pipeline_id: str,
        sub_step_id: str,
    ) -> dict[str, Any]:
        attempt_id = self._next_attempt_id()
        transcript_id = f"transcript_{attempt_id}"
        attempt = {
            "attempt_id": attempt_id,
            "scope": "sub_step",
            "parent_step_id": parent_step_id,
            "candidate_index": candidate_index,
            "sub_pipeline_id": sub_pipeline_id,
            "sub_step_id": sub_step_id,
            "status": "running",
            "transcript_id": transcript_id,
        }
        self._attempts.setdefault("items", {})[attempt_id] = attempt
        return attempt

    def _ensure_sub_step_attempt(
        self,
        *,
        parent_step_id: str,
        candidate_index: int,
        sub_pipeline_id: str,
        sub_step_id: str,
        resume_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        items = self._attempts.setdefault("items", {})
        active_attempt_id = resume_state.get("active_attempt_id") if resume_state else None
        active = items.get(active_attempt_id) if active_attempt_id else None
        if (
            active
            and active.get("scope") == "sub_step"
            and active.get("parent_step_id") == parent_step_id
            and active.get("candidate_index") == candidate_index
            and active.get("sub_pipeline_id") == sub_pipeline_id
            and active.get("sub_step_id") == sub_step_id
            and active.get("status") == "running"
        ):
            return active
        return self._create_sub_step_attempt(
            parent_step_id=parent_step_id,
            candidate_index=candidate_index,
            sub_pipeline_id=sub_pipeline_id,
            sub_step_id=sub_step_id,
        )

    def _ensure_parent_attempt(self, step_id: str) -> dict[str, Any]:
        active_attempt_id = self._execution.get("active_attempt_id")
        items = self._attempts.setdefault("items", {})
        active = items.get(active_attempt_id) if active_attempt_id else None
        if (
            active
            and active.get("scope") == "parent"
            and active.get("step_id") == step_id
            and active.get("status") == "running"
        ):
            return active
        if self._execution.get("kind") == "parallel_sub_pipeline" and self._execution.get("step_id") == step_id:
            attempt = self._create_parent_attempt_record(step_id)
            self._execution["active_attempt_id"] = attempt["attempt_id"]
            self._execution["transcript_id"] = attempt["transcript_id"]
            return attempt
        return self._create_parent_attempt(step_id)

    def _mark_attempt_status(self, attempt_id: str | None, status: str) -> None:
        if not attempt_id:
            return
        attempt = self._attempts.setdefault("items", {}).get(attempt_id)
        if attempt is not None:
            attempt["status"] = status
        if status != "running" and self._execution.get("active_attempt_id") == attempt_id:
            self._execution = {}

    def _mark_active_attempt_failed(self) -> None:
        self._mark_attempt_status(self._execution.get("active_attempt_id"), "failed")

    def _mark_active_attempt_failed_preserve_execution(self) -> None:
        active_attempt_id = self._execution.get("active_attempt_id")
        if not active_attempt_id:
            return
        attempt = self._attempts.setdefault("items", {}).get(active_attempt_id)
        if attempt is not None:
            attempt["status"] = "failed"

    def _load_repaired_resume_messages(self, transcript_id: str | None) -> list | None:
        if self._transcript_storage is None or not transcript_id:
            return None
        loaded = self._transcript_storage.load(self._cwd, transcript_id)
        return self._transcript_storage.repair_interrupted(loaded)

    def _attempt_has_resume_transcript(self, attempt: dict[str, Any] | None) -> bool:
        if self._transcript_storage is None or not attempt:
            return False
        transcript_id = attempt.get("transcript_id")
        if not isinstance(transcript_id, str):
            return False
        return bool(self._transcript_storage.load(self._cwd, transcript_id))

    def _record_sidecar_save_failure(self, status: str, operation: str, exc: Exception) -> None:
        self._sidecar_status = None
        observability = getattr(self, "_observability", None)
        if observability is not None:
            failure = public_error_from_exception(exc)
            observability.sidecar_failed(
                operation=operation,
                status=status,
                error_type=failure.details["type"],
                error_summary=failure.summary,
                error_id=failure.error_id,
            )
        logger.warning(
            "Failed to persist pipeline sidecar during %s (pipeline=%s, session_id=%s, status=%s)",
            operation,
            getattr(getattr(self, "_loaded", None), "name", ""),
            getattr(self, "_session_id", ""),
            status,
            exc_info=True,
        )

    def _persistence_failure_event(self, exc: PipelineStatePersistenceError) -> PipelineEvent:
        step_id = exc.step_id
        try:
            current_step = getattr(self.state_machine, "current_step", None)
            step_id = step_id or getattr(current_step, "step_id", None)
        except (AttributeError, IndexError):
            step_id = step_id or self._terminal_current_step_id() or None
        return PipelineEvent(
            type=PipelineEventType.STEP_FAILED,
            step_id=step_id,
            timestamp=time.time(),
            data={
                "error": _("Pipeline state persistence failed."),
                "error_summary": _("Pipeline state persistence failed."),
                "error_details": {"type": "PipelineStatePersistenceError"},
            },
        )

    async def _try_save_sidecar(
        self,
        status: str,
        operation: str,
        save: Callable[[], Awaitable[None]],
        *,
        step_id: str | None = None,
    ) -> None:
        try:
            await save()
        except Exception as exc:
            self._record_sidecar_save_failure(status, operation, exc)
            raise PipelineStatePersistenceError(
                f"pipeline state persistence failed during {operation}",
                step_id=step_id,
            ) from exc
        self._sidecar_status = status

    def _try_save_sidecar_sync(
        self,
        status: str,
        operation: str,
        save: Callable[[], None],
        *,
        step_id: str | None = None,
    ) -> None:
        try:
            save()
        except Exception as exc:
            self._record_sidecar_save_failure(status, operation, exc)
            raise PipelineStatePersistenceError(
                f"pipeline state persistence failed during {operation}",
                step_id=step_id,
            ) from exc
        self._sidecar_status = status

    async def _save_running(self, current_step: str, reason: str | None = None) -> None:
        if not self.session:
            self._sidecar_status = "running"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        async def save() -> None:
            await session.save_running(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason=reason,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        await self._try_save_sidecar("running", "save_running", save, step_id=current_step)

    def _save_running_sync(self, current_step: str, reason: str | None = None) -> None:
        if not self.session:
            self._sidecar_status = "running"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        def save() -> None:
            session.save_running_sync(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason=reason,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        self._try_save_sidecar_sync("running", "save_running_sync", save, step_id=current_step)

    async def _save_waiting_input(self, current_step: str) -> None:
        if not self.session:
            self._sidecar_status = "waiting_input"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        async def save() -> None:
            await session.save_waiting_input(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason="waiting for user input",
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        await self._try_save_sidecar("waiting_input", "save_waiting_input", save, step_id=current_step)

    async def _save_completed(self, current_step: str, reason: str | None = None) -> None:
        if not self.session:
            self._sidecar_status = "completed"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        async def save() -> None:
            await session.save_completed(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason=reason,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        await self._try_save_sidecar("completed", "save_completed", save, step_id=current_step)

    async def _save_failed(self, current_step: str, reason: str) -> None:
        self._mark_active_attempt_failed()
        if not self.session:
            self._sidecar_status = "failed"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        async def save() -> None:
            await session.save_failed(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason=reason,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        await self._try_save_sidecar("failed", "save_failed", save, step_id=current_step)

    def _save_failed_sync(self, current_step: str, reason: str) -> None:
        self._mark_active_attempt_failed()
        if not self.session:
            self._sidecar_status = "failed"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        def save() -> None:
            session.save_failed_sync(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason=reason,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        self._try_save_sidecar_sync("failed", "save_failed_sync", save, step_id=current_step)

    def mark_user_aborted(self, reason: str) -> None:
        self._mark_active_attempt_failed_preserve_execution()
        current_step = ""
        if not self.state_machine.is_complete:
            try:
                current_step = self.state_machine.current_step.step_id
            except (AttributeError, IndexError):
                current_step = ""
        observability = getattr(self, "_observability", None)
        if observability is not None:
            observability.pipeline_user_aborted(
                total_steps=self.state_machine.total_steps,
                current_step=current_step or None,
                reason=reason,
            )
        if not self.session:
            self._sidecar_status = "user_aborted"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        def save() -> None:
            session.save_user_aborted_sync(
                current_step,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                reason=reason,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        self._try_save_sidecar_sync("user_aborted", "save_user_aborted_sync", save, step_id=current_step or None)

    async def _save_rollback(self, from_step: str, to_step: str, reason: str) -> None:
        if not self.session:
            self._sidecar_status = "running"
            return
        session = self.session

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        async def save() -> None:
            await session.save_rollback(
                from_step,
                to_step,
                reason,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        await self._try_save_sidecar("running", "save_rollback", save, step_id=from_step)

    def _save_rollback_sync(self, from_step: str, to_step: str, reason: str) -> None:
        session = getattr(self, "session", None)
        if not session:
            self._sidecar_status = "running"
            return

        state_machine_snapshot = self._state_machine_snapshot_for_sidecar()
        context_snapshot = self.context.to_snapshot()

        def save() -> None:
            session.save_rollback_sync(
                from_step,
                to_step,
                reason,
                state_machine_snapshot,
                context_snapshot,
                self._pipeline_identity,
                execution=dict(self._execution) if self._execution else None,
                attempts=dict(self._attempts),
            )

        self._try_save_sidecar_sync("running", "save_rollback_sync", save, step_id=from_step)

    async def _save_after_advance(self, completed_step_id: str) -> None:
        self._set_current_step_user_input(None)
        try:
            if self.state_machine.is_complete:
                await self._save_completed(completed_step_id, reason="pipeline completed")
                return
            await self._save_running(
                self.state_machine.current_step.step_id,
                reason=f"advanced from {completed_step_id}",
            )
        except PipelineStatePersistenceError as exc:
            exc.step_id = completed_step_id
            raise

    def iter_active_agent_loops(self):
        """Yield all currently-active AgentLoops (problem 6 — /status aggregation).

        - Normal step: yields step_executor.current_agent_loop (if any)
        - Parallel step: yields each candidate sub-executor's current AgentLoop
        """
        step_loop = self._step_executor.current_agent_loop
        if step_loop is not None:
            yield step_loop
        if self._current_sub_executor_list:
            for sub in self._current_sub_executor_list:
                loop = sub.current_step_executor_agent_loop
                if loop is not None:
                    yield loop

    def get_prompt_contexts(self) -> list[PromptContext]:
        """Return real AgentLoop contexts for active or resumable pipeline work."""
        active = self._active_prompt_contexts()
        if active:
            return active
        return self._restored_prompt_contexts()

    def _active_prompt_contexts(self) -> list[PromptContext]:
        contexts: list[PromptContext] = []
        step_loop = self._step_executor.current_agent_loop
        if step_loop is not None:
            contexts.append(
                self._prompt_context_from_agent_loop(
                    step_loop,
                    scope="parent",
                    step_id=self.state_machine.current_step.step_id,
                )
            )

        active_by_loop_id: dict[int, tuple[int, dict[str, Any]]] = {}
        for raw_idx, state in list(self._active_candidates.items()):
            loop = state.get("agent_loop")
            if loop is not None:
                active_by_loop_id[id(loop)] = (int(raw_idx), state)
        if self._current_sub_executor_list:
            for fallback_idx, sub in enumerate(self._current_sub_executor_list):
                loop = sub.current_step_executor_agent_loop
                if loop is None:
                    continue
                idx, state = active_by_loop_id.get(id(loop), (fallback_idx, {}))
                contexts.append(
                    self._prompt_context_from_agent_loop(
                        loop,
                        scope="candidate",
                        step_id=str(state.get("current_sub_step") or ""),
                        candidate_index=idx,
                        candidate_name=str(state.get("name") or ""),
                        sub_pipeline_id=str(state.get("sub_pipeline_id") or ""),
                    )
                )
        return contexts

    def _restored_prompt_contexts(self) -> list[PromptContext]:
        current_step = self.state_machine.current_step
        if current_step.step_type == PipelineStepType.PARALLEL_SUB_PIPELINE.value:
            return self._restored_parallel_prompt_contexts(current_step)
        attempt = self._current_parent_attempt(current_step.step_id)
        if attempt is None or attempt.get("status") != "running":
            return []
        resume_messages = self._load_repaired_resume_messages(attempt.get("transcript_id"))
        agent_context = self._step_executor.build_agent_loop_context(
            current_step,
            self.context,
            self._session_id,
            attempt_id=attempt.get("attempt_id"),
            transcript_id=attempt.get("transcript_id"),
            resume_messages=resume_messages,
        )
        if agent_context.agent_loop is None:
            return []
        return [
            self._prompt_context_from_agent_loop(
                agent_context.agent_loop,
                scope="parent",
                step_id=current_step.step_id,
                initial_prompt=agent_context.initial_prompt,
            )
        ]

    def _restored_parallel_prompt_contexts(self, current_step: StepSpec) -> list[PromptContext]:
        execution = self._execution if isinstance(self._execution, dict) else {}
        if execution.get("kind") != "parallel_sub_pipeline" or execution.get("step_id") != current_step.step_id:
            return []
        sub_pipeline_name = current_step.sub_pipeline_name or execution.get("sub_pipeline_name")
        if not isinstance(sub_pipeline_name, str):
            return []
        sub_spec = self._loaded.sub_pipelines.get(sub_pipeline_name)
        if sub_spec is None:
            return []
        candidates_state = execution.get("candidates")
        if not isinstance(candidates_state, dict):
            return []
        candidate_states = cast(dict[Any, Any], candidates_state)

        contexts: list[PromptContext] = []
        sub_context_executor = SubPipelineExecutor(
            provider_manager=self._step_executor._provider_manager,
            base_tool_registry=self._step_executor._base_tool_registry,
            pipeline=self._loaded,
            pipeline_dir=self._pipeline_dir,
            session_storage=self._transcript_storage or self._session_storage,
            cwd=self._cwd,
            pause_event=self._agent_pause_event,
            permission_context_getter=self._permission_context_getter,
            memory_content_getter=self._memory_content_getter,
            auto_trigger_skills=self._auto_trigger_skills,
            surface=self._surface,
        )
        self._apply_telemetry_correlation(sub_context_executor)
        sub_context_dependencies = sub_context_executor._sub_context_dependencies(sub_spec)
        for raw_idx, raw_state in sorted(candidate_states.items(), key=lambda item: self._candidate_sort_key(item[0])):
            if not isinstance(raw_state, dict):
                continue
            state = cast(dict[str, Any], raw_state)
            if state.get("status") != "running":
                continue
            try:
                candidate_index = int(raw_idx)
            except (TypeError, ValueError):
                continue
            state_machine_data = state.get("state_machine")
            context_data = state.get("context")
            if not isinstance(state_machine_data, dict) or not isinstance(context_data, dict):
                continue
            state_machine_snapshot = cast(dict[str, Any], state_machine_data)
            context_snapshot = cast(dict[str, Any], context_data)
            transcript_id = state.get("transcript_id")
            transcript_id = transcript_id if isinstance(transcript_id, str) else None
            active_attempt_id = state.get("active_attempt_id")
            active_attempt_id = active_attempt_id if isinstance(active_attempt_id, str) else None
            state_machine = StateMachine.from_snapshot(
                state_machine_snapshot,
                sub_spec.steps,
                max_rollbacks=sub_spec.max_rollbacks,
            )
            sub_context = PipelineContext.from_snapshot(context_snapshot, sub_context_dependencies)
            sub_step = state_machine.current_step
            resume_messages = self._load_repaired_resume_messages(transcript_id)
            step_executor = StepExecutor(
                provider_manager=self._step_executor._provider_manager,
                base_tool_registry=self._step_executor._base_tool_registry,
                pipeline=self._loaded,
                pipeline_dir=self._pipeline_dir,
                session_storage=self._transcript_storage or self._session_storage,
                cwd=self._cwd,
                pause_event=self._agent_pause_event,
                permission_context_getter=self._permission_context_getter,
                memory_content_getter=self._memory_content_getter,
                auto_trigger_skills=self._auto_trigger_skills,
                surface=self._surface,
            )
            self._apply_telemetry_correlation(step_executor)
            agent_context = step_executor.build_agent_loop_context(
                sub_step,
                sub_context,
                self._session_id,
                attempt_id=active_attempt_id,
                transcript_id=transcript_id,
                resume_messages=resume_messages,
            )
            if agent_context.agent_loop is None:
                continue
            contexts.append(
                self._prompt_context_from_agent_loop(
                    agent_context.agent_loop,
                    scope="candidate",
                    step_id=sub_step.step_id,
                    initial_prompt=agent_context.initial_prompt,
                    candidate_index=candidate_index,
                    candidate_name=str(state.get("name") or ""),
                    sub_pipeline_id=str(state.get("sub_pipeline_id") or ""),
                )
            )
        return contexts

    @staticmethod
    def _candidate_sort_key(value: Any) -> tuple[int, int | str]:
        try:
            return (0, int(value))
        except (TypeError, ValueError):
            return (1, str(value))

    @staticmethod
    def _option_display_value(option: Any) -> str | None:
        if isinstance(option, str):
            return option
        if isinstance(option, dict):
            for key in ("name", "value", "label"):
                value = option.get(key)
                if isinstance(value, str):
                    return value
        return None

    def _infer_selected_index(self, selected_value: str, options: list[Any]) -> int | None:
        structured = parse_selected_candidate(selected_value)
        if structured is not None and structured.selected_candidate_index is not None:
            idx = structured.selected_candidate_index
            if 0 <= idx < len(options):
                return idx
        matches = [idx for idx, option in enumerate(options) if self._option_display_value(option) == selected_value]
        if len(matches) == 1:
            return matches[0]
        return None

    def _next_step_attempt(self, step_id: str) -> int:
        attempt = self._step_attempts.get(step_id, 0) + 1
        self._step_attempts[step_id] = attempt
        return attempt

    def _current_step_attempt(self, step_id: str) -> int:
        return self._step_attempts.get(step_id, 1)

    def _current_parent_attempt(self, step_id: str) -> dict[str, Any] | None:
        items = self._attempts.get("items")
        if not isinstance(items, dict):
            return None
        attempt_items = cast(dict[str, Any], items)
        active_attempt_id = self._execution.get("active_attempt_id") if isinstance(self._execution, dict) else None
        if isinstance(active_attempt_id, str):
            active = attempt_items.get(active_attempt_id)
            if isinstance(active, dict):
                active_data = cast(dict[str, Any], active)
                if active_data.get("step_id") == step_id:
                    return active_data
        for attempt in reversed(list(attempt_items.values())):
            if not isinstance(attempt, dict):
                continue
            attempt_data = cast(dict[str, Any], attempt)
            if attempt_data.get("scope") == "parent" and attempt_data.get("step_id") == step_id:
                return attempt_data
        return None

    @staticmethod
    def _prompt_context_from_agent_loop(
        agent_loop: Any,
        *,
        scope: str,
        step_id: str,
        initial_prompt: str | list[ContentBlock] = "",
        candidate_index: int | None = None,
        candidate_name: str = "",
        sub_pipeline_id: str = "",
    ) -> PromptContext:
        return PromptContext(
            scope=scope,
            step_id=step_id,
            system_prompt=agent_loop.system_prompt,
            messages=list(agent_loop.context_manager.get_messages()),
            agent_loop_session_id=str(getattr(agent_loop, "_session_id", "")),
            initial_prompt=_initial_prompt_text(initial_prompt),
            candidate_index=candidate_index,
            candidate_name=candidate_name,
            sub_pipeline_id=sub_pipeline_id,
        )

    async def run(
        self, user_input: str | list[ContentBlock] | PipelineUserInput
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        """Start the pipeline from the first step."""
        pipeline_input = normalize_pipeline_user_input(user_input)
        self._session_storage.append_meta(
            self._cwd,
            self._session_id,
            {"type": "pipeline_init", "pipeline_type": self._loaded.name},
        )
        self._set_current_step_user_input(pipeline_input)
        try:
            await self._save_running(self.state_machine.current_step.step_id, reason="pipeline started")
        except PipelineStatePersistenceError as exc:
            yield self._persistence_failure_event(exc)
            return
        self._observability.pipeline_started(
            total_steps=self.state_machine.total_steps,
            step_names=list(self.state_machine._order),
        )
        yield PipelineEvent(
            type=PipelineEventType.PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "pipeline_type": self._loaded.name,
                "total_steps": self.state_machine.total_steps,
                "step_names": list(self.state_machine._order),
            },
        )
        with self._observability.pipeline_run_span(total_steps=self.state_machine.total_steps):
            async for event in self._continue_from_current(**self._continue_input_kwargs(pipeline_input)):
                yield event

    async def resume(
        self, user_input: str | list[ContentBlock] | PipelineUserInput
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        """Resume after user input at a USER_INPUT_REQUIRED pause."""
        if self.has_pending_pipeline_pause_confirmation():
            async for event in self._continue_from_sidecar_with_input(user_input):
                yield event
            return

        pipeline_input = normalize_pipeline_user_input(user_input)
        user_text = pipeline_input.display_text
        step = self.state_machine.current_step
        step_index = self.state_machine.current_step_index + 1
        step_attempt = self._current_step_attempt(step.step_id)
        wait_started_at = self._waiting_input_started_at.pop(step.step_id, None)
        wait_duration_ms = self._observability.duration_ms(wait_started_at) if wait_started_at is not None else None
        current_conclusion = self.context.get_conclusion(step.conclusion_field) or {}
        if not isinstance(current_conclusion, dict):
            current_conclusion = {}
        waiting_options = self._waiting_input_options_by_step.pop(step.step_id, [])
        if not waiting_options:
            restored_options = current_conclusion.get("options")
            if isinstance(restored_options, list):
                waiting_options = restored_options
        selected_index: int | None = None
        if step.ui_mode == "candidate_selection":
            selected_index = self._infer_selected_index(user_text, waiting_options)
            if selected_index is None:
                logger.debug(
                    "Pipeline candidate selection did not match a unique option: step_id=%s option_count=%d",
                    step.step_id,
                    len(waiting_options),
                )
        current_conclusion["user_input"] = user_text
        self.context.set_conclusion(step.conclusion_field, current_conclusion)
        self._set_current_step_user_input(pipeline_input)
        try:
            await self._save_running(step.step_id, reason="user input received")
        except PipelineStatePersistenceError as exc:
            yield self._persistence_failure_event(exc)
            return
        self._observability.user_input_received(
            step_id=step.step_id,
            step_index=step_index,
            step_attempt=step_attempt,
            total_steps=self.state_machine.total_steps,
            ui_mode=step.ui_mode,
            user_input=user_text,
            wait_duration_ms=wait_duration_ms,
        )
        if step.ui_mode == "candidate_selection" and selected_index is not None:
            self._observability.selection_made(
                step_id=step.step_id,
                step_attempt=step_attempt,
                ui_mode=step.ui_mode,
                option_count=len(waiting_options),
                selected_index=selected_index,
                selected_value=user_text,
            )

        yield PipelineEvent(
            type=PipelineEventType.USER_INPUT_RECEIVED,
            step_id=step.step_id,
            timestamp=time.time(),
            data=_user_input_received_data(
                pipeline_input,
                ui_mode=step.ui_mode,
                selected_index=selected_index,
                waiting_options=waiting_options,
            ),
        )

        async for event in self._continue_from_current(
            **self._continue_input_kwargs(pipeline_input),
            resume_waiting_step=True,
        ):
            yield event

    async def resume_ask_user_question(
        self,
        answer: dict[str, str],
        *,
        tool_use_id: str,
        pending_input: dict[str, Any] | None = None,
        supplemental_input: str | list[ContentBlock] | PipelineUserInput | None = None,
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        """Resume an in-step ask_user_question after process restart."""
        payload = {
            "selected_id": _string_answer_value(answer.get("selected_id")),
            "selected_label": _string_answer_value(answer.get("selected_label")),
            "free_text": _string_answer_value(answer.get("free_text")),
        }
        step = self.state_machine.current_step
        resume_messages = self._resume_messages_for_current_parent_step(step.step_id)
        expected_tool_use_id = _pending_ask_tool_use_id(pending_input, resume_messages)
        if expected_tool_use_id is not None and expected_tool_use_id != tool_use_id:
            raise ValueError(
                f"ask_user_question tool_use_id mismatch: expected {expected_tool_use_id!r}, got {tool_use_id!r}"
            )
        tool_result = ToolResultBlock(
            tool_use_id=tool_use_id,
            content=json.dumps(payload, ensure_ascii=False),
            is_error=False,
        )
        supplemental = normalize_pipeline_user_input(supplemental_input) if supplemental_input is not None else None
        if supplemental is not None and not supplemental.has_images:
            supplemental = None
        tool_result_message = Message(role="user", content=[tool_result])
        user_message: str | list[ContentBlock] | None = (
            supplemental.content if supplemental is not None else [tool_result]
        )
        candidate_index = _candidate_index_from_pending_input(pending_input)
        if candidate_index is not None:
            step = self.state_machine.current_step
            if step.step_type == PipelineStepType.PARALLEL_SUB_PIPELINE.value:
                previous = getattr(self, "_restored_ask_user_question", None)
                candidate_resume_messages = [tool_result_message] if supplemental is not None else None
                candidate_precompleted_tools = {"ask_user_question": payload}
                self._restored_ask_user_question = {
                    "candidate_index": candidate_index,
                    "user_message": user_message,
                    "resume_messages": candidate_resume_messages,
                    "precompleted_tools": candidate_precompleted_tools,
                }
                self._set_candidate_ask_user_question_resume_state(
                    candidate_index,
                    user_message=user_message,
                    resume_messages=candidate_resume_messages,
                    precompleted_tools=candidate_precompleted_tools,
                )
                try:
                    async for event in self._continue_from_current(resume_running_step=True):
                        yield event
                finally:
                    if previous is None:
                        if hasattr(self, "_restored_ask_user_question"):
                            delattr(self, "_restored_ask_user_question")
                    else:
                        self._restored_ask_user_question = previous
                return

        if supplemental is not None:
            async for event in self._continue_from_current(
                **self._continue_input_kwargs(supplemental),
                resume_messages=[*resume_messages, tool_result_message],
                precompleted_tools={"ask_user_question": payload},
                resume_waiting_step=True,
            ):
                yield event
            return

        resume_kwargs: dict[str, Any] = {"user_input": user_message}
        async for event in self._continue_from_current(
            **resume_kwargs,
            resume_messages=resume_messages,
            precompleted_tools={"ask_user_question": payload},
            resume_waiting_step=True,
        ):
            yield event

    def _resume_messages_for_current_parent_step(self, step_id: str) -> list[Message]:
        attempt = self._current_parent_attempt(step_id)
        if attempt is not None:
            resume_messages = self._load_repaired_resume_messages(attempt.get("transcript_id"))
            if resume_messages is not None:
                return list(resume_messages)
        if self._session_storage is None or not hasattr(self._session_storage, "load"):
            return []
        loaded = self._session_storage.load(self._cwd, self._session_id)
        return list(loaded or [])

    def _get_state_for_judge(self) -> dict:
        """Build pipeline state summary for the interrupt judge."""
        steps_info = []
        for i, step in enumerate(self._loaded.steps):
            steps_info.append(
                {
                    "step_id": step.step_id,
                    "description": step.description,
                    "is_current": i == self.state_machine.current_step_index,
                }
            )

        conclusions = {}
        for step in self._loaded.steps:
            value = self.context.get_conclusion(step.conclusion_field)
            if value is not None:
                conclusions[step.conclusion_field] = value

        partial_output = ""
        current_step = self.state_machine.current_step
        active_candidates = getattr(self, "_active_candidates", {})
        if current_step.step_type == PipelineStepType.PARALLEL_SUB_PIPELINE.value and active_candidates:
            # P-I12: parent has no agent_loop during parallel execution; aggregate
            # each candidate's currently-streaming text (already kept in sync via
            # state["agent_loop"] = sub_executors[i].current_step_executor_agent_loop).
            parts = []
            for idx, cs in sorted(active_candidates.items()):
                loop = cs.get("agent_loop")
                if loop is not None and getattr(loop, "current_turn_text", ""):
                    parts.append(f"[Candidate {idx + 1}]: {loop.current_turn_text}")
            partial_output = "\n\n".join(parts)
        elif self._step_executor.current_agent_loop:
            partial_output = self._step_executor.current_agent_loop.current_turn_text

        state: dict = {
            "pipeline_name": self._loaded.name,
            "current_step_id": self.state_machine.current_step.step_id,
            "current_step_index": self.state_machine.current_step_index,
            "steps": steps_info,
            "conclusions": conclusions,
            "partial_output": partial_output,
        }

        candidate_states_by_index: dict[int, dict[str, Any]] = {}
        execution = getattr(self, "_execution", {}) or {}
        current_step_is_parallel = current_step.step_type == PipelineStepType.PARALLEL_SUB_PIPELINE.value
        execution_matches_current_parallel_step = (
            current_step_is_parallel
            and execution.get("kind") == "parallel_sub_pipeline"
            and execution.get("step_id") == current_step.step_id
        )

        if execution_matches_current_parallel_step:
            persisted_candidates = execution.get("candidates", {})
            if isinstance(persisted_candidates, dict):
                for raw_idx, candidate_state in persisted_candidates.items():
                    try:
                        idx = int(raw_idx)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(candidate_state, dict):
                        continue
                    candidate = candidate_state.get("candidate")
                    name = candidate_state.get("name", "")
                    if not name:
                        if isinstance(candidate, dict):
                            name = candidate.get("name", "")
                        elif candidate is not None:
                            name = str(candidate)
                    state_entry = {
                        "index": idx,
                        "name": name or "",
                        "current_sub_step": candidate_state.get("current_sub_step", ""),
                    }
                    if "status" in candidate_state:
                        state_entry["status"] = candidate_state["status"]
                    candidate_states_by_index[idx] = state_entry

        for raw_idx, candidate_state in active_candidates.items():
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            state_entry = {
                "index": idx,
                "name": candidate_state.get("name", ""),
                "current_sub_step": candidate_state.get("current_sub_step", ""),
            }
            if "status" in candidate_state:
                state_entry["status"] = candidate_state["status"]
            candidate_states_by_index[idx] = state_entry

        if candidate_states_by_index:
            state["candidate_states"] = [candidate_states_by_index[idx] for idx in sorted(candidate_states_by_index)]

            sub_pipeline_name = getattr(current_step, "sub_pipeline_name", None)
            if not isinstance(sub_pipeline_name, str) and execution_matches_current_parallel_step:
                sub_pipeline_name = execution.get("sub_pipeline_name")
            if isinstance(sub_pipeline_name, str) and sub_pipeline_name in self._loaded.sub_pipelines:
                sub_spec = self._loaded.sub_pipelines[sub_pipeline_name]
                state["sub_pipeline_steps"] = [
                    {"step_id": s.step_id, "description": s.description} for s in sub_spec.steps
                ]

        return state

    async def handle_user_interrupt(self, message: str | list[ContentBlock] | PipelineUserInput) -> InterruptVerdict:
        """Engine-layer interrupt entry point. All clients call this uniformly."""

        pipeline_input = normalize_pipeline_user_input(message)
        judge_input: str | PipelineUserInput = (
            pipeline_input if pipeline_input.has_images else pipeline_input.display_text
        )
        verdict = await self._interrupt_controller.judge(judge_input)

        if self._is_judgment_error_verdict(verdict):
            return self._apply_interrupt_judge_failure_policy(verdict)

        if verdict.action == "supplement":
            injected = self._inject_supplement(verdict, pipeline_input.content)
            if not injected:
                # Don't silently lose the user's message — flag it via reason
                # prefix so the UI can render a clear "supplement was dropped"
                # warning instead of the misleading "已补充" feedback.
                verdict = replace(
                    verdict,
                    reason=f"supplement_dropped (target={verdict.supplement_target}): {verdict.reason}",
                )

        return verdict

    def _apply_interrupt_judge_failure_policy(self, verdict: InterruptVerdict) -> InterruptVerdict:
        current_step = getattr(self.state_machine, "current_step", None)
        step_id = getattr(current_step, "step_id", "")
        policy = getattr(current_step, "interrupt_judge_failure", "continue")
        if policy == "pause":
            self.pause_agent_loops()
            return replace(
                verdict,
                paused=True,
                reason=(
                    f"judge failed while executing side-effect step {step_id!r}; "
                    f"pipeline paused for safety. {verdict.reason}"
                ),
            )
        if policy == "hard_interrupt":
            return replace(
                verdict,
                action="hard_interrupt",
                rollback_target=step_id or verdict.rollback_target,
                rollback_context=(
                    f"Interrupt judge failed while executing side-effect step {step_id!r}; "
                    "restart this step for safety."
                ),
                reason=f"judge failed; applying hard interrupt for safety. {verdict.reason}",
            )
        return verdict

    def pause_agent_loops(self) -> None:
        """Freeze all AgentLoops in this pipeline at their next turn boundary.

        Idempotent: calling on an already-paused pipeline is a no-op.
        """
        self._agent_pause_event.clear()

    def resume_agent_loops(self) -> None:
        """Release any AgentLoops parked on the pause checkpoint.

        Idempotent: calling on a non-paused pipeline is a no-op.
        """
        self._agent_pause_event.set()

    @staticmethod
    def _input_for_interrupt_verdict(
        verdict: InterruptVerdict,
        source_input: str | list[ContentBlock] | PipelineUserInput | None,
    ) -> PipelineUserInput | None:
        source = normalize_pipeline_user_input(source_input) if source_input is not None else None
        rollback_context = verdict.rollback_context or ""
        if source is None:
            return normalize_pipeline_user_input(rollback_context) if rollback_context else None
        if rollback_context:
            return source.with_prepended_text(rollback_context)
        return source

    def apply_hard_interrupt(
        self,
        verdict: InterruptVerdict,
        *,
        source_input: str | list[ContentBlock] | PipelineUserInput | None = None,
    ) -> bool:
        """Execute state rollback after hard interrupt.

        Returns True if a parent-level rollback was performed (caller should
        restart the pipeline stream), False if a candidate-level restart was
        scheduled (parallel step continues).
        """
        self._last_applied_interrupt_verdict = None
        if getattr(self.state_machine, "is_complete", False) is True:
            logger.warning(
                "Cannot apply hard interrupt to completed pipeline (pipeline=%s, session_id=%s)",
                self._loaded.name,
                getattr(self, "_session_id", ""),
            )
            self._rollback_context = None
            self._rollback_input = None
            return False

        target = verdict.rollback_target or self.state_machine.current_step.step_id
        from_step = self.state_machine.current_step.step_id

        is_candidate_restart = False
        candidate_sub_spec = None
        if verdict.candidate_scope is not None:
            current_step = self.state_machine.current_step
            if current_step.sub_pipeline_name:
                sub_spec = self._loaded.sub_pipelines.get(current_step.sub_pipeline_name)
                if sub_spec:
                    candidate_sub_spec = sub_spec
                    sub_step_ids = {s.step_id for s in sub_spec.steps}
                    if target in sub_step_ids:
                        is_candidate_restart = True

        # A candidate-level restart can only revive candidates that are still
        # running (present in _active_candidates, or persisted as running after
        # sidecar restore). If any requested candidate has already completed —
        # scope="all" with partial completion, or scope="candidate:N" where N finished — candidate restart would
        # silently drop it (the completed one keeps its stale conclusion).
        # Escalate to a parent rollback so the whole parallel step re-runs.
        # Note: there is a benign race — between a cancelled task's finally
        # (which pop()s from _active_candidates) and its replacement's body
        # (which re-adds), a requested index can momentarily look completed.
        # The failure mode is over-escalation (parent rollback ⊇ candidate
        # restart), so correctness is preserved.
        if is_candidate_restart:
            requested = self._requested_candidate_indices(verdict.candidate_scope)
            if not requested or any(not self._candidate_is_running_for_interrupt(idx) for idx in requested):
                is_candidate_restart = False
                target = self.state_machine.current_step.step_id
            elif (
                target
                and candidate_sub_spec
                and any(
                    not self._candidate_target_is_non_future_for_interrupt(idx, candidate_sub_spec.steps, target)
                    for idx in requested
                )
            ):
                is_candidate_restart = False
                target = self.state_machine.current_step.step_id

        if is_candidate_restart:
            if source_input is None:
                self._schedule_candidate_restart(verdict)
            else:
                self._schedule_candidate_restart(verdict, source_input=source_input)
            self._emit_hard_interrupt_telemetry(
                rollback_scope="candidate",
                from_step=from_step,
                to_step=target,
                candidate_scope=verdict.candidate_scope,
                rollback_reason=verdict.reason,
            )
            self._last_applied_interrupt_verdict = verdict
            return False

        # Cancelled tasks are cleaned up by _execute_parallel_sub_pipeline's
        # try/finally when the generator is aclose()'d; no need to await here.
        self._cancel_active_candidates(reason="hard_interrupt_parent_rollback")
        # Drop any candidate restarts staged by an earlier interrupt so they
        # don't leak across this parent rollback into the next parallel step.
        self._pending_candidate_restarts.clear()
        original_target: str | None = None
        original_validation_error: str | None = None
        ok, validation_error = self._can_interrupt_rollback_to(target)
        if not ok:
            logger.warning(
                "Invalid hard interrupt rollback target %r for step %s: %s",
                target,
                self.state_machine.current_step.step_id,
                validation_error,
            )
            original_target = target
            original_validation_error = validation_error
            target = self.state_machine.current_step.step_id
            fallback_reason = (
                f"invalid rollback target {original_target!r}; "
                f"falling back to current step {target!r}: {verdict.reason}"
            )
            ok, validation_error = self._can_interrupt_rollback_to(target)
            if not ok:
                logger.warning("Cannot apply hard interrupt fallback target %r: %s", target, validation_error)
                self._save_failed_sync(from_step, fallback_reason)
                self._rollback_context = None
                self._rollback_input = None
                return False
            verdict = replace(verdict, rollback_target=target, reason=fallback_reason)

        cleanup_from_step = self.state_machine.current_step
        self.state_machine.interrupt_rollback(target, verdict.reason)
        current_attempt_id = self._execution.get("active_attempt_id")
        self._mark_attempt_status(current_attempt_id, "discarded")
        self._create_parent_attempt(target)
        target_field = next((s.conclusion_field for s in self._loaded.steps if s.step_id == target), None)
        if target_field:
            self.context.mark_stale(target_field)
        rollback_input = self._input_for_interrupt_verdict(verdict, source_input)
        self._rollback_context = rollback_input.display_text if rollback_input is not None else None
        self._rollback_input = rollback_input.content if rollback_input is not None else None
        self._set_current_step_user_input(rollback_input)
        self._mark_rollback_cleanup_required(
            cleanup_from_step,
            target,
            verdict.reason,
            from_attempt_id=current_attempt_id if isinstance(current_attempt_id, str) else None,
        )
        self._save_rollback_sync(from_step, target, verdict.reason)
        hard_interrupt_attrs = {
            "rollback_scope": "parent",
            "from_step": from_step,
            "to_step": target,
            "candidate_scope": verdict.candidate_scope,
            "rollback_reason": verdict.reason,
        }
        if original_target is not None:
            hard_interrupt_attrs.update(
                original_target=original_target,
                fallback_target=target,
                validation_error=original_validation_error,
            )
        self._emit_hard_interrupt_telemetry(**hard_interrupt_attrs)
        self._last_applied_interrupt_verdict = verdict
        return True

    def _emit_hard_interrupt_telemetry(self, **attrs: Any) -> None:
        observability = getattr(self, "_observability", None)
        if observability is None:
            return
        observability.hard_interrupt(**attrs)

    def _cancel_candidate_task(
        self,
        idx: int,
        state: dict[str, Any],
        *,
        reason: str,
        parent_step_id: str | None = None,
    ) -> asyncio.Task | None:
        """Cancel one active candidate task and emit cancellation observability once."""
        task = state.get("task")
        if not task or task.done():
            return None

        parent_step_id = parent_step_id or self.state_machine.current_step.step_id
        candidate_name = state.get("name", "")
        if not state.get("_candidate_cancelled_observed", False):
            state["_candidate_cancelled_observed"] = True
            self._observability.candidate_cancelled(
                parent_step_id=parent_step_id,
                candidate_index=idx,
                candidate_name=candidate_name,
                reason=reason,
            )
            logger.info(
                (
                    "Pipeline candidate cancelled: pipeline=%s session_id=%s parent_step_id=%s "
                    "candidate_index=%d candidate_name=%s reason=%s"
                ),
                self._loaded.name,
                self._session_id,
                parent_step_id,
                idx,
                candidate_name,
                reason,
                extra={
                    "pipeline": self._loaded.name,
                    "session_id": self._session_id,
                    "parent_step_id": parent_step_id,
                    "candidate_index": idx,
                    "candidate_name": candidate_name,
                    "reason": reason,
                },
            )
        task.cancel()
        return task

    def _cancel_active_candidates(self, reason: str = "cancelled") -> list[asyncio.Task]:
        """Cancel all running candidate tasks and clear tracking dict.

        Returns the list of cancelled tasks so callers in async context
        can ``await asyncio.gather(*tasks, return_exceptions=True)`` to
        ensure their finally-blocks complete before new work starts.
        """
        cancelled: list[asyncio.Task] = []
        parent_step_id = self.state_machine.current_step.step_id
        for idx, state in list(self._active_candidates.items()):
            task = self._cancel_candidate_task(idx, state, reason=reason, parent_step_id=parent_step_id)
            if task is not None:
                cancelled.append(task)
        # Eagerly clear so callers see an empty dict immediately; the per-task
        # finally-clause pop()s become harmless no-ops via pop(i, None).
        self._active_candidates.clear()
        return cancelled

    def continue_after_interrupt(self) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        """Create a new event stream after interrupt rollback."""
        rollback_input = self._rollback_input
        context = self._rollback_context
        self._rollback_input = None
        self._rollback_context = None
        if rollback_input is not None:
            kwargs: dict[str, Any] = {"user_input": rollback_input}
            if not isinstance(rollback_input, str) or context != rollback_input:
                kwargs["user_input_display_text"] = context
            return self._continue_from_current(**kwargs)
        return self._continue_from_current(user_input=context)

    @staticmethod
    def _try_inject_into_agent_loop(agent_loop: object | None, message: PipelineInputContent) -> bool:
        if agent_loop is None:
            return False

        if inspect.getattr_static(agent_loop, "try_inject_user_message", None) is not None:
            try_inject = getattr(agent_loop, "try_inject_user_message", None)
            if callable(try_inject):
                return try_inject(message) is not False

        can_accept = getattr(agent_loop, "can_accept_injected_user_message", True)
        if can_accept is False:
            return False

        inject = getattr(agent_loop, "inject_user_message", None)
        if not callable(inject):
            return False
        inject(message)
        return True

    def _inject_supplement(self, verdict: InterruptVerdict, message: PipelineInputContent) -> bool:
        """Inject supplement message into the correct AgentLoop.

        Returns True if the message was injected into at least one AgentLoop,
        False if there was no eligible target (e.g. parallel mode with
        supplement_target=None — parent has no AgentLoop while candidates run).
        Caller can use the return value to surface a clear error rather than
        silently dropping the user's input.
        """
        if verdict.supplement_target is None:
            # P-I19: in parallel mode, parent has no agent_loop. Broadcast to all
            # active candidates (equivalent to supplement_target="all").
            is_parallel = (
                self._active_candidates
                and self.state_machine.current_step.step_type == PipelineStepType.PARALLEL_SUB_PIPELINE.value
            )
            if is_parallel:
                injected = False
                for state in list(self._active_candidates.values()):
                    al = state.get("agent_loop")
                    if self._try_inject_into_agent_loop(al, message):
                        injected = True
                return injected
            agent_loop = self._step_executor.current_agent_loop
            return self._try_inject_into_agent_loop(agent_loop, message)
        if verdict.supplement_target == "all":
            injected = False
            for state in list(self._active_candidates.values()):
                al = state.get("agent_loop")
                if self._try_inject_into_agent_loop(al, message):
                    injected = True
            return injected
        target = verdict.supplement_target
        idx = self._candidate_index_from_target(target)
        if idx is not None:
            state = self._active_candidates.get(idx)
            if state:
                al = state.get("agent_loop")
                return self._try_inject_into_agent_loop(al, message)
        return False

    @staticmethod
    def _candidate_target_from_pending_question_envelope(envelope: dict[str, Any] | None) -> str | None:
        if not isinstance(envelope, dict):
            return None
        candidate = envelope.get("candidate")
        if not isinstance(candidate, dict):
            return None
        for key in ("index", "candidateIndex", "candidate_index"):
            value = candidate.get(key)
            if isinstance(value, int):
                return f"candidate:{value}"
            if isinstance(value, str):
                try:
                    return f"candidate:{int(value)}"
                except ValueError:
                    continue
        return None

    def inject_pending_question_supplement(
        self,
        message: PipelineInputContent,
        *,
        envelope: dict[str, Any] | None = None,
    ) -> bool:
        """Inject image/text supplied alongside an active ask_user_question answer."""
        target = self._candidate_target_from_pending_question_envelope(envelope)
        verdict = InterruptVerdict(
            action="supplement",
            reason="ask_user_question supplemental input",
            supplement_target=target,
        )
        return self._inject_supplement(verdict, message)

    @staticmethod
    def _candidate_index_from_target(target: str | None) -> int | None:
        if not target or not (target.startswith("candidate:") or target.startswith("candidate_index:")):
            return None
        try:
            return int(target.split(":", 1)[1])
        except (ValueError, IndexError):
            return None

    def _candidate_execution_state_for_resume(self, candidate_index: int) -> dict[str, Any] | None:
        execution = getattr(self, "_execution", None)
        if not isinstance(execution, dict):
            return None
        candidates = execution.get("candidates")
        if not isinstance(candidates, dict):
            return None
        for key in (str(candidate_index), candidate_index):
            state = candidates.get(key)
            if isinstance(state, dict):
                return state
        return None

    def _set_candidate_ask_user_question_resume_state(
        self,
        candidate_index: int,
        *,
        user_message: PipelineInputContent,
        resume_messages: list[Message] | None,
        precompleted_tools: dict[str, dict[str, Any]] | None,
    ) -> None:
        execution = self._execution if isinstance(self._execution, dict) else {}
        candidates = execution.setdefault("candidates", {})
        if not isinstance(candidates, dict):
            candidates = {}
            execution["candidates"] = candidates
        state = candidates.setdefault(str(candidate_index), {})
        if not isinstance(state, dict):
            state = {}
            candidates[str(candidate_index)] = state
        state[_PENDING_ASK_USER_QUESTION_RESUME_KEY] = _serialize_ask_user_question_resume_state(
            user_message=user_message,
            resume_messages=resume_messages,
            precompleted_tools=precompleted_tools,
        )
        self._execution = execution

    def _candidate_ask_user_question_resume_state(self, candidate_index: int) -> dict[str, Any] | None:
        restored_ask = getattr(self, "_restored_ask_user_question", None)
        if isinstance(restored_ask, dict) and restored_ask.get("candidate_index") == candidate_index:
            return restored_ask
        active_state = getattr(self, "_active_candidates", {}).get(candidate_index)
        if isinstance(active_state, dict):
            restored = _deserialize_ask_user_question_resume_state(
                active_state.get(_PENDING_ASK_USER_QUESTION_RESUME_KEY)
            )
            if restored is not None:
                return restored
        state = self._candidate_execution_state_for_resume(candidate_index)
        if state is None:
            return None
        return _deserialize_ask_user_question_resume_state(state.get(_PENDING_ASK_USER_QUESTION_RESUME_KEY))

    def _candidate_user_message_for_restored_supplement(
        self,
        candidate_index: int,
        default_message: str | list[ContentBlock] | None,
    ) -> str | list[ContentBlock] | None:
        restored_supplement = getattr(self, "_restored_supplement", None)
        if not restored_supplement:
            return default_message
        message = restored_supplement.get("message")
        target = restored_supplement.get("target")
        if target in (None, "", "all"):
            return message
        return message if self._candidate_index_from_target(target) == candidate_index else None

    def _candidate_user_message_for_restored_ask_user_question(
        self,
        candidate_index: int,
    ) -> list[ContentBlock] | None:
        restored_ask = self._candidate_ask_user_question_resume_state(candidate_index)
        user_message = restored_ask.get("user_message") if restored_ask is not None else None
        return user_message if isinstance(user_message, list) else None

    def _candidate_resume_messages_for_restored_ask_user_question(
        self,
        candidate_index: int,
    ) -> list[Message] | None:
        restored_ask = self._candidate_ask_user_question_resume_state(candidate_index)
        resume_messages = restored_ask.get("resume_messages") if restored_ask is not None else None
        return resume_messages if isinstance(resume_messages, list) else None

    def _candidate_precompleted_tools_for_restored_ask_user_question(
        self,
        candidate_index: int,
    ) -> dict[str, dict[str, Any]] | None:
        restored_ask = self._candidate_ask_user_question_resume_state(candidate_index)
        precompleted_tools = restored_ask.get("precompleted_tools") if restored_ask is not None else None
        return precompleted_tools if isinstance(precompleted_tools, dict) else None

    def _requested_candidate_indices(self, scope: str | None) -> list[int]:
        """Resolve a candidate_scope verdict to concrete candidate indices.

        ``"all"`` expands to every candidate of the in-flight parallel step
        (``range(_parallel_candidates_total)``), ``"candidate:N"`` to ``[N]``.
        Returns an empty list for ``None`` or malformed scopes — callers treat
        that as "cannot serve at candidate level".
        """
        if scope == "all":
            if self._parallel_candidates_total:
                return list(range(self._parallel_candidates_total))
            return self._persisted_parallel_candidate_indices()
        idx = self._candidate_index_from_target(scope)
        if idx is not None:
            return [idx]
        return []

    def _candidate_target_is_non_future_for_interrupt(
        self,
        candidate_index: int,
        sub_steps: list[Any],
        target_step_id: str,
    ) -> bool:
        step_ids = [step.step_id for step in sub_steps]
        if target_step_id not in step_ids:
            return False
        state = self._active_candidates.get(candidate_index)
        if state is None:
            state = self._persisted_parallel_candidate_state(candidate_index)
        if not isinstance(state, dict):
            return False
        current_index = self._candidate_current_sub_step_index(state, step_ids)
        if current_index is None:
            return False
        return step_ids.index(target_step_id) <= current_index

    def _candidate_current_sub_step_index(self, state: dict[str, Any], step_ids: list[str]) -> int | None:
        state_machine_snapshot = state.get("state_machine")
        if isinstance(state_machine_snapshot, dict):
            current_index = state_machine_snapshot.get("current_index")
            if isinstance(current_index, int) and 0 <= current_index < len(step_ids):
                return current_index

        current_index = state.get("current_index")
        if isinstance(current_index, int) and 0 <= current_index < len(step_ids):
            return current_index

        current_sub_step = state.get("current_sub_step")
        if isinstance(current_sub_step, str) and current_sub_step in step_ids:
            return step_ids.index(current_sub_step)

        return None

    def _schedule_candidate_restart(
        self,
        verdict: InterruptVerdict,
        *,
        source_input: str | list[ContentBlock] | PipelineUserInput | None = None,
    ) -> None:
        """Cancel specified candidate(s) and schedule restart."""
        target_step = verdict.rollback_target
        indices = self._requested_candidate_indices(verdict.candidate_scope)
        current_step = self.state_machine.current_step
        sub_spec = self._loaded.sub_pipelines.get(current_step.sub_pipeline_name or "")
        rollback_input = self._input_for_interrupt_verdict(verdict, source_input)

        for idx in indices:
            state = self._active_candidates.get(idx)
            if state is None:
                state = self._persisted_parallel_candidate_state(idx)
            if state is None:
                continue
            preserved = {
                k: v for k, v in state.get("conclusions", {}).items() if self._conclusion_is_before_step(k, target_step)
            }
            self._pending_candidate_restarts[idx] = RestartInfo(
                start_from_step=target_step,
                preserved_conclusions=preserved,
                rollback_context=rollback_input.display_text
                if rollback_input is not None
                else verdict.rollback_context,
                rollback_input=rollback_input.content if rollback_input is not None else None,
            )
            if target_step and sub_spec:
                sub_pipeline_id = state.get("sub_pipeline_id") or f"{sub_spec.name}_candidate_{idx}"
                old_attempt_id = state.get("active_attempt_id")
                if old_attempt_id is None:
                    old_attempt_id = self._execution.get("candidates", {}).get(str(idx), {}).get("active_attempt_id")
                self._mark_attempt_status(old_attempt_id, "discarded")
                new_attempt = self._create_sub_step_attempt(
                    parent_step_id=current_step.step_id,
                    candidate_index=idx,
                    sub_pipeline_id=sub_pipeline_id,
                    sub_step_id=target_step,
                )
                state_machine_snapshot = state.get("state_machine")
                if isinstance(state_machine_snapshot, dict):
                    sub_state_machine = StateMachine.from_snapshot(
                        state_machine_snapshot,
                        sub_spec.steps,
                        max_rollbacks=sub_spec.max_rollbacks,
                    )
                else:
                    sub_state_machine = StateMachine(sub_spec.steps, sub_spec.max_rollbacks)
                sub_state_machine.jump_to(target_step)
                context_snapshot = state.get("context")
                if not isinstance(context_snapshot, dict):
                    context_snapshot = {}
                target_index = next((i for i, step in enumerate(sub_spec.steps) if step.step_id == target_step), None)
                if target_index is not None and context_snapshot:
                    field_names = ["candidate"] + [s.conclusion_field for s in sub_spec.steps]
                    for parent_field in sub_spec.context_fields_from_parent:
                        if parent_field not in field_names:
                            field_names.append(parent_field)
                    sub_context = PipelineContext.from_snapshot(
                        context_snapshot,
                        {name: [] for name in field_names},
                    )
                    for stale_step in sub_spec.steps[target_index:]:
                        sub_context.mark_stale(stale_step.conclusion_field)
                    context_snapshot = sub_context.to_snapshot()
                pending_restart: dict[str, Any] = {
                    "start_from_step": target_step,
                    "preserved_conclusions": preserved,
                    "rollback_context": rollback_input.display_text
                    if rollback_input is not None
                    else verdict.rollback_context,
                }
                if rollback_input is not None and rollback_input.has_images:
                    pending_restart["rollback_input"] = _serialize_pipeline_input_content(rollback_input.content)
                entry = {
                    "status": "running",
                    "candidate": state.get("candidate", state.get("raw_candidate", {})),
                    "sub_pipeline_id": sub_pipeline_id,
                    "state_machine": sub_state_machine.to_snapshot(),
                    "context": context_snapshot,
                    "current_sub_step": target_step,
                    "current_index": sub_state_machine.current_step_index,
                    "active_attempt_id": new_attempt["attempt_id"],
                    "transcript_id": new_attempt["transcript_id"],
                    "conclusions": preserved,
                    "pending_restart": pending_restart,
                }
                existing_candidate = self._execution.get("candidates", {}).get(str(idx), {}).get("candidate")
                if existing_candidate is not None:
                    entry["candidate"] = existing_candidate
                self._execution.setdefault("candidates", {})[str(idx)] = entry
                self._save_running_sync(current_step.step_id, reason="candidate restart scheduled")
            task = state.get("task")
            if task and not task.done():
                self._cancel_candidate_task(idx, state, reason="candidate_restart")

    def _conclusion_is_before_step(self, conclusion_field: str, step_id: str | None) -> bool:
        """Check if a conclusion field belongs to a step before the given step_id in sub-pipeline."""
        if step_id is None:
            return False
        current_step = self.state_machine.current_step
        if not current_step.sub_pipeline_name:
            return False
        sub_spec = self._loaded.sub_pipelines.get(current_step.sub_pipeline_name)
        if not sub_spec:
            return False
        step_ids = [s.step_id for s in sub_spec.steps]
        field_to_step = {s.conclusion_field: s.step_id for s in sub_spec.steps}
        owning_step = field_to_step.get(conclusion_field)
        if not owning_step or step_id not in step_ids:
            return False
        return step_ids.index(owning_step) < step_ids.index(step_id)

    async def _continue_from_current(
        self,
        user_input: str | list[ContentBlock] | None = None,
        *,
        user_input_display_text: str | None = None,
        resume_messages: list[Message] | None = None,
        precompleted_tools: dict[str, dict[str, Any]] | None = None,
        resume_waiting_step: bool = False,
        resume_running_step: bool = False,
    ) -> AsyncGenerator[StreamEvent | PipelineEvent | StepResult, None]:
        is_first_step = True
        terminal_pipeline_telemetry_emitted = False
        step_result: StepResult | None = None
        restored_step_user_input = self._consume_restored_current_step_user_input() if user_input is None else None
        restored_resume_messages, restored_precompleted_tools = (
            self._consume_restored_current_step_resume_state() if user_input is None else (None, None)
        )
        first_step_user_input = (
            user_input
            if user_input is not None
            else restored_step_user_input.content
            if restored_step_user_input is not None
            else None
        )
        first_step_user_input_display_text = (
            user_input_display_text
            if user_input is not None
            else restored_step_user_input.display_text
            if restored_step_user_input is not None
            else None
        )
        first_step_user_input_is_restored = user_input is None and restored_step_user_input is not None
        first_step_resume_messages = resume_messages if resume_messages is not None else restored_resume_messages
        first_step_precompleted_tools = (
            precompleted_tools if precompleted_tools is not None else restored_precompleted_tools
        )
        if first_step_resume_messages is not None or first_step_precompleted_tools is not None:
            self._set_current_step_resume_state(
                resume_messages=first_step_resume_messages,
                precompleted_tools=first_step_precompleted_tools,
            )
        elif user_input is not None:
            self._set_current_step_resume_state()

        def emit_pipeline_completed(*, failed: bool, early_exit: bool) -> None:
            nonlocal terminal_pipeline_telemetry_emitted
            if terminal_pipeline_telemetry_emitted:
                return
            self._observability.pipeline_completed(
                total_steps=self.state_machine.total_steps,
                failed=failed,
                early_exit=early_exit,
            )
            terminal_pipeline_telemetry_emitted = True

        while not self.state_machine.is_complete:
            step = self.state_machine.current_step
            step_user_message = first_step_user_input if is_first_step else None
            step_user_display_text = first_step_user_input_display_text if is_first_step else None
            self._set_current_step_user_input(step_user_message, display_text=step_user_display_text)
            step_start = time.time()
            step_started_at = self._observability.now()
            step_index = self.state_machine.current_step_index + 1
            existing_attempt = self._current_parent_attempt(step.step_id)
            attempt = self._ensure_parent_attempt(step.step_id)
            resume_current_step = (
                resume_waiting_step or (resume_running_step and self._attempt_has_resume_transcript(existing_attempt))
            ) and is_first_step

            if resume_current_step:
                step_attempt = self._current_step_attempt(step.step_id)
                try:
                    await self._save_running(step.step_id, reason="resumed from user input")
                except PipelineStatePersistenceError as exc:
                    yield self._persistence_failure_event(exc)
                    return
            else:
                step_attempt = self._next_step_attempt(step.step_id)
                try:
                    await self._save_running(step.step_id, reason="step started")
                except PipelineStatePersistenceError as exc:
                    yield self._persistence_failure_event(exc)
                    return
                self._observability.step_started(
                    step_id=step.step_id,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    step_type=step.step_type,
                    ui_mode=step.ui_mode,
                )
                yield PipelineEvent(
                    type=PipelineEventType.STEP_STARTED,
                    step_id=step.step_id,
                    timestamp=step_start,
                    data={
                        "index": step_index,
                        "attempt": step_attempt,
                        "total": self.state_machine.total_steps,
                        "name": step.step_id,
                        "step_type": step.step_type,
                        "ui_mode": step.ui_mode,
                        "active_attempt_id": attempt["attempt_id"],
                        "transcript_id": attempt["transcript_id"],
                    },
                )

            step_result: StepResult | None = None
            if step.step_type == PipelineStepType.PARALLEL_SUB_PIPELINE.value:
                try:
                    with self._observability.step_span(
                        step_id=step.step_id,
                        step_index=step_index,
                        step_attempt=step_attempt,
                        total_steps=self.state_machine.total_steps,
                        step_type=step.step_type,
                    ):
                        # Forward user_input (rollback_context) to first step's worth
                        # of fresh candidates so they know why they're (re-)running.
                        if first_step_user_input_is_restored and self._execution_matches_current_parallel_step():
                            step_user_message = None
                        is_first_step = False
                        async for event in self._execute_parallel_sub_pipeline(
                            step,
                            user_message=step_user_message,
                            emit_step_completed_event=False,
                        ):
                            yield event
                except Exception as exc:
                    failure = public_error(
                        message=str(exc) or type(exc).__name__,
                        error_type="StepFailed",
                        extra_details={"step_id": step.step_id},
                    )
                    try:
                        await self._save_failed(step.step_id, str(exc) or type(exc).__name__)
                    except PipelineStatePersistenceError as persistence_exc:
                        yield self._persistence_failure_event(persistence_exc)
                        return
                    self._observability.step_failed(
                        step_id=step.step_id,
                        duration_ms=self._observability.duration_ms(step_started_at),
                        step_index=step_index,
                        step_attempt=step_attempt,
                        total_steps=self.state_machine.total_steps,
                        step_type=step.step_type,
                        error_summary=failure.summary,
                        error_type=failure.details["type"],
                        error_id=failure.error_id,
                    )
                    self._observability.funnel_step(
                        step_id=step.step_id,
                        step_index=step_index,
                        step_attempt=step_attempt,
                        total_steps=self.state_machine.total_steps,
                        status="failed",
                        step_type=step.step_type,
                        ui_mode=step.ui_mode,
                        duration_ms=self._observability.duration_ms(step_started_at),
                    )
                    yield PipelineEvent(
                        type=PipelineEventType.STEP_FAILED,
                        step_id=step.step_id,
                        timestamp=time.time(),
                        data={
                            "error": failure.summary,
                            "error_summary": failure.summary,
                            "error_details": failure.details,
                        },
                    )
                    break

                duration_ms = self._observability.duration_ms(step_started_at)
                self._mark_attempt_status(attempt.get("attempt_id"), "completed")
                completed_step_id = step.step_id
                self.state_machine.advance()
                try:
                    await self._save_after_advance(completed_step_id)
                except PipelineStatePersistenceError as exc:
                    yield self._persistence_failure_event(exc)
                    return
                self._observability.step_completed(
                    step_id=step.step_id,
                    duration_ms=duration_ms,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    step_type=step.step_type,
                    ui_mode=step.ui_mode,
                )
                self._observability.funnel_step(
                    step_id=step.step_id,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    status="completed",
                    step_type=step.step_type,
                    ui_mode=step.ui_mode,
                    duration_ms=duration_ms,
                )
                if step.forward is None:
                    emit_pipeline_completed(failed=False, early_exit=False)
                parallel_result = self.context.get_conclusion(step.conclusion_field)
                candidates_count = len(parallel_result) if isinstance(parallel_result, list) else 0
                yield PipelineEvent(
                    type=PipelineEventType.STEP_COMPLETED,
                    step_id=step.step_id,
                    timestamp=time.time(),
                    data={
                        "duration_s": 0,
                        "candidates_count": candidates_count,
                        "conclusion_field": step.conclusion_field,
                        "conclusion": parallel_result,
                    },
                )
                continue

            with self._observability.step_span(
                step_id=step.step_id,
                step_index=step_index,
                step_attempt=step_attempt,
                total_steps=self.state_machine.total_steps,
                step_type=step.step_type,
            ):
                first_step = is_first_step
                is_first_step = False
                step_resume_messages = first_step_resume_messages if first_step else None
                step_precompleted_tools = first_step_precompleted_tools if first_step else None
                if self._transcript_storage is not None and attempt.get("status") == "running":
                    loaded = self._transcript_storage.load(self._cwd, attempt["transcript_id"])
                    repaired_resume_messages = self._transcript_storage.repair_interrupted(loaded)
                    step_resume_messages = reconcile_resume_messages(
                        repaired_resume_messages,
                        step_resume_messages,
                    )
                if (
                    first_step
                    and first_step_user_input_is_restored
                    and step_resume_messages
                    and (
                        isinstance(step_user_message, str)
                        or user_message_already_in_resume(step_user_message, step_resume_messages)
                    )
                ):
                    step_user_message = None
                execute_kwargs: dict[str, Any] = {
                    "user_message": step_user_message,
                    "attempt_id": attempt["attempt_id"],
                    "transcript_id": attempt["transcript_id"],
                    "resume_messages": step_resume_messages,
                    "rollback_targets": self.state_machine.completed_non_future_rollback_targets(),
                    "rollback_count": self.state_machine.rollback_count,
                    "max_rollbacks": self.state_machine.max_rollbacks,
                }
                if step_precompleted_tools is not None:
                    execute_kwargs["precompleted_tools"] = step_precompleted_tools

                try:
                    parameters = inspect.signature(self._step_executor.execute).parameters
                except (TypeError, ValueError):
                    parameters = {}
                if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                    execute_kwargs = {key: value for key, value in execute_kwargs.items() if key in parameters}

                async for event in self._step_executor.execute(
                    step,
                    self.context,
                    self._session_id,
                    **execute_kwargs,
                ):
                    if isinstance(event, StepResult):
                        step_result = event
                    else:
                        if isinstance(event, ResourceObservedEvent):
                            self._handle_resource_observed(
                                step,
                                event,
                                attempt_id=attempt.get("attempt_id"),
                            )
                        yield event

            if (
                step_result is not None
                and step_result.status == StepStatus.COMPLETED
                and not step_result.rollback_request
            ):
                self._mark_attempt_status(attempt.get("attempt_id"), "completed")

            if step_result is None or step_result.status == StepStatus.FAILED:
                reason = (step_result.error if step_result else None) or "No result"
                failure = public_error(
                    message=reason,
                    error_type="StepFailed",
                    extra_details={"step_id": step.step_id},
                )
                error_summary = failure.summary
                try:
                    await self._save_failed(step.step_id, reason)
                except PipelineStatePersistenceError as exc:
                    yield self._persistence_failure_event(exc)
                    return
                self._observability.step_failed(
                    step_id=step.step_id,
                    duration_ms=self._observability.duration_ms(step_started_at),
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    step_type=step.step_type,
                    error_summary=error_summary,
                    error_type=failure.details["type"],
                    error_id=failure.error_id,
                )
                self._observability.funnel_step(
                    step_id=step.step_id,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    status="failed",
                    step_type=step.step_type,
                    ui_mode=step.ui_mode,
                    duration_ms=self._observability.duration_ms(step_started_at),
                )
                emit_pipeline_completed(failed=True, early_exit=False)
                yield PipelineEvent(
                    type=PipelineEventType.STEP_FAILED,
                    step_id=step.step_id,
                    timestamp=time.time(),
                    data={
                        "error": error_summary,
                        "error_summary": error_summary,
                        "error_details": failure.details,
                    },
                )
                break

            duration_ms = self._observability.duration_ms(step_started_at)
            elapsed = duration_ms / 1000
            step_success_observed = False

            def emit_step_success_observability(funnel_status: str | None = "completed") -> None:
                nonlocal step_success_observed
                if step_success_observed:
                    return
                self._observability.step_completed(
                    step_id=step.step_id,
                    duration_ms=duration_ms,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    step_type=step.step_type,
                    ui_mode=step.ui_mode,
                )
                if funnel_status is not None:
                    self._observability.funnel_step(
                        step_id=step.step_id,
                        step_index=step_index,
                        step_attempt=step_attempt,
                        total_steps=self.state_machine.total_steps,
                        status=funnel_status,
                        step_type=step.step_type,
                        ui_mode=step.ui_mode,
                        duration_ms=duration_ms,
                    )
                self._session_storage.append_meta(
                    self._cwd, self._session_id, {"type": "pipeline_step_complete", "step_id": step.step_id}
                )
                step_success_observed = True

            step_completed_event = PipelineEvent(
                type=PipelineEventType.STEP_COMPLETED,
                step_id=step.step_id,
                timestamp=time.time(),
                data={
                    "duration_s": elapsed,
                    "conclusion_field": step.conclusion_field,
                    "conclusion": step_result.conclusion,
                },
            )

            if step.exit_condition and step_result.conclusion:
                ec_field = step.exit_condition.get("field", "")
                ec_value = step.exit_condition.get("value")
                actual = step_result.conclusion.get(ec_field)
                matched = actual is ec_value if isinstance(ec_value, bool) else actual == ec_value
                if matched:
                    logger.info("Exit condition met for step %s: %s=%r", step.step_id, ec_field, ec_value)
                    try:
                        await self._save_completed(step.step_id, reason="exit condition met")
                    except PipelineStatePersistenceError as exc:
                        yield self._persistence_failure_event(exc)
                        return
                    emit_step_success_observability()
                    emit_pipeline_completed(failed=False, early_exit=True)
                    yield step_completed_event
                    yield PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=step.step_id,
                        timestamp=time.time(),
                        data={"total_steps": self.state_machine.total_steps, "early_exit": True},
                    )
                    return

            if step_result.rollback_request:
                target, reason = step_result.rollback_request
                current_attempt_id = attempt.get("attempt_id")
                try:
                    self.state_machine.rollback(target, reason)
                except ValueError as exc:
                    valid_targets = self.state_machine.completed_non_future_rollback_targets()
                    error_message = f"Invalid rollback target {target!r}. Valid targets: {valid_targets}. ({exc})"
                    failure = public_error(
                        message=error_message,
                        error_type="StepFailed",
                        extra_details={
                            "step_id": step.step_id,
                            "target": target,
                            "valid_targets": valid_targets,
                        },
                    )
                    logger.warning(
                        "Invalid rollback target %r requested by step %s. Valid: %s",
                        target,
                        step.step_id,
                        valid_targets,
                    )
                    try:
                        await self._save_failed(step.step_id, str(exc))
                    except PipelineStatePersistenceError as persistence_exc:
                        yield self._persistence_failure_event(persistence_exc)
                        return
                    self._observability.step_failed(
                        step_id=step.step_id,
                        duration_ms=self._observability.duration_ms(step_started_at),
                        step_index=step_index,
                        step_attempt=step_attempt,
                        total_steps=self.state_machine.total_steps,
                        step_type=step.step_type,
                        error_summary=failure.summary,
                        error_type="StepFailed",
                        error_id=failure.error_id,
                    )
                    self._observability.funnel_step(
                        step_id=step.step_id,
                        step_index=step_index,
                        step_attempt=step_attempt,
                        total_steps=self.state_machine.total_steps,
                        status="failed",
                        step_type=step.step_type,
                        ui_mode=step.ui_mode,
                        duration_ms=self._observability.duration_ms(step_started_at),
                    )
                    emit_pipeline_completed(failed=True, early_exit=False)
                    yield step_completed_event
                    yield PipelineEvent(
                        type=PipelineEventType.STEP_FAILED,
                        step_id=step.step_id,
                        timestamp=time.time(),
                        data={
                            "error": failure.summary,
                            "error_summary": failure.summary,
                            "error_details": failure.details,
                        },
                    )
                    # Emit terminal event so consumers (e.g. renderer teardown)
                    # see a clean pipeline end. Mirrors the post-loop
                    # PIPELINE_COMPLETED(failed=True) emission for genuine
                    # step failures, which we can't reuse here because
                    # step_result.status is COMPLETED (the step itself succeeded;
                    # only the rollback target was invalid).
                    yield PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=step.step_id,
                        timestamp=time.time(),
                        data={"total_steps": self.state_machine.total_steps, "failed": True},
                    )
                    return
                self._mark_attempt_status(current_attempt_id, "rolled_back")
                self._create_parent_attempt(target)
                target_field = next((s.conclusion_field for s in self._loaded.steps if s.step_id == target), None)
                stale = self.context.mark_stale(target_field) if target_field else []
                self._set_current_step_user_input(None)
                self._mark_rollback_cleanup_required(
                    step,
                    target,
                    reason,
                    from_attempt_id=current_attempt_id if isinstance(current_attempt_id, str) else None,
                )
                try:
                    await self._save_rollback(step.step_id, target, reason)
                except PipelineStatePersistenceError as exc:
                    yield self._persistence_failure_event(exc)
                    return
                emit_step_success_observability()
                self._observability.rollback(
                    from_step=step.step_id,
                    to_step=target,
                    rollback_reason=reason,
                    rollback_scope="parent",
                    stale_fields=stale,
                )
                self._session_storage.append_meta(
                    self._cwd,
                    self._session_id,
                    {"type": "pipeline_rollback", "from": step.step_id, "to": target, "reason": reason},
                )
                yield step_completed_event
                yield PipelineEvent(
                    type=PipelineEventType.ROLLBACK_TRIGGERED,
                    step_id=step.step_id,
                    timestamp=time.time(),
                    data={
                        "from_step": step.step_id,
                        "to_step": target,
                        "reason": sanitize_public_text(reason),
                        "stale_fields": stale,
                    },
                )
                continue

            if step.auto_advance or resume_current_step:
                completed_step_id = step.step_id
                self.state_machine.advance()
                try:
                    await self._save_after_advance(completed_step_id)
                except PipelineStatePersistenceError as exc:
                    yield self._persistence_failure_event(exc)
                    return
                emit_step_success_observability()
                if step.forward is None:
                    emit_pipeline_completed(failed=False, early_exit=False)
                yield step_completed_event
            else:
                self._set_current_step_user_input(None)
                try:
                    await self._save_waiting_input(step.step_id)
                except PipelineStatePersistenceError as exc:
                    yield self._persistence_failure_event(exc)
                    return
                emit_step_success_observability(funnel_status=None)
                yield step_completed_event
                conclusion = step_result.conclusion or {}
                prompt = conclusion.get("user_prompt", "")
                options = conclusion.get("options", [])
                if not isinstance(options, list):
                    options = []
                self._waiting_input_started_at[step.step_id] = self._observability.now()
                self._waiting_input_options_by_step[step.step_id] = options
                self._observability.user_input_required(
                    step_id=step.step_id,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    step_type=step.step_type,
                    ui_mode=step.ui_mode,
                    option_count=len(options),
                    prompt=prompt if isinstance(prompt, str) else "",
                )
                self._observability.funnel_step(
                    step_id=step.step_id,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=self.state_machine.total_steps,
                    status="waiting_input",
                    step_type=step.step_type,
                    ui_mode=step.ui_mode,
                    duration_ms=duration_ms,
                )
                yield PipelineEvent(
                    type=PipelineEventType.USER_INPUT_REQUIRED,
                    step_id=step.step_id,
                    timestamp=time.time(),
                    data={
                        "step_id": step.step_id,
                        "prompt": prompt if isinstance(prompt, str) else "",
                        "options": options,
                    },
                )
                return

        if self.state_machine.is_complete:
            emit_pipeline_completed(failed=False, early_exit=False)
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=time.time(),
                data={"total_steps": self.state_machine.total_steps},
            )
        elif step_result is None or step_result.status == StepStatus.FAILED:
            emit_pipeline_completed(failed=True, early_exit=False)
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=step.step_id,
                timestamp=time.time(),
                data={"total_steps": self.state_machine.total_steps, "failed": True},
            )

    def clear_sidecar(self) -> None:
        """Mark the sidecar non-resumable while preserving files for debugging."""
        if self.session:
            if not self.session.session_dir.exists():
                return
            try:
                self.session.mark_discarded(reason="clear_sidecar requested; sidecar preserved")
            except Exception as exc:
                failure = public_error_from_exception(exc)
                self._observability.sidecar_failed(
                    operation="mark_discarded",
                    status=self._sidecar_status,
                    error_type=failure.details["type"],
                    error_summary=failure.summary,
                    error_id=failure.error_id,
                )

    async def _execute_parallel_sub_pipeline(
        self,
        step: StepSpec,
        user_message: str | list[ContentBlock] | None = None,
        emit_step_completed_event: bool = True,
    ) -> AsyncGenerator[PipelineEvent | StreamEvent, None]:
        """Execute a parallel_sub_pipeline step with interrupt support."""
        assert step.sub_pipeline_name is not None
        sub_spec = self._loaded.sub_pipelines[step.sub_pipeline_name]
        candidates = self._resolve_iterate_field(sub_spec.iterate_over)

        sub_executors = [
            SubPipelineExecutor(
                provider_manager=self._step_executor._provider_manager,
                base_tool_registry=self._step_executor._base_tool_registry,
                pipeline=self._loaded,
                pipeline_dir=self._step_executor._pipeline_dir,
                session_storage=self._transcript_storage or self._session_storage,
                cwd=self._cwd,
                pause_event=self._agent_pause_event,
                permission_context_getter=self._permission_context_getter,
                memory_content_getter=self._memory_content_getter,
                auto_trigger_skills=self._auto_trigger_skills,
                surface=self._surface,
            )
            for _ in candidates
        ]
        for sub_executor in sub_executors:
            self._apply_telemetry_correlation(sub_executor)

        self._active_candidates.clear()

        event_queue: asyncio.Queue = asyncio.Queue()
        conclusions_by_index: dict[int, dict] = {}
        failed_by_index: dict[int, dict[str, Any]] = {}
        restored_execution = (
            self._execution
            if self._execution.get("kind") == "parallel_sub_pipeline" and self._execution.get("step_id") == step.step_id
            else None
        )
        if restored_execution is None:
            self._pending_candidate_restarts.clear()
        restored_candidates = restored_execution.get("candidates", {}) if restored_execution else {}
        parent_attempt_id = self._execution.get("active_attempt_id") if self._execution else None
        parent_transcript_id = self._execution.get("transcript_id") if self._execution else None
        self._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": step.step_id,
            "sub_pipeline_name": sub_spec.name,
            "candidates": {},
        }
        if parent_attempt_id:
            self._execution["active_attempt_id"] = parent_attempt_id
        if parent_transcript_id:
            self._execution["transcript_id"] = parent_transcript_id

        async def save_candidate_execution_state(
            i: int,
            payload: dict[str, Any],
            *,
            reason: str | None = None,
        ) -> None:
            attempt_status = payload.get("attempt_status")
            if attempt_status in {"completed", "failed", "rolled_back"}:
                self._mark_attempt_status(payload.get("active_attempt_id"), attempt_status)
            active_attempt_id = payload.get("active_attempt_id") if attempt_status in {"running", "failed"} else None
            transcript_id = payload.get("transcript_id") if active_attempt_id else None
            entry: dict[str, Any] = {
                "status": payload.get("status", "running"),
                "candidate": payload.get("candidate", candidates[i]),
                "sub_pipeline_id": payload.get("sub_pipeline_id") or f"{sub_spec.name}_candidate_{i}",
                "state_machine": payload.get("state_machine"),
                "context": payload.get("context"),
                "current_sub_step": payload.get("current_sub_step", ""),
                "current_index": payload.get("current_index"),
                "active_attempt_id": active_attempt_id,
                "transcript_id": transcript_id,
            }
            conclusions = payload.get("conclusions")
            if conclusions is not None:
                entry["conclusions"] = conclusions
            active_state = self._active_candidates.get(i)
            pending_ask_resume = (
                active_state.get(_PENDING_ASK_USER_QUESTION_RESUME_KEY) if isinstance(active_state, dict) else None
            )
            if pending_ask_resume is not None and entry["status"] == "running":
                entry[_PENDING_ASK_USER_QUESTION_RESUME_KEY] = pending_ask_resume
            self._execution.setdefault("candidates", {})[str(i)] = entry
            await self._save_running(step.step_id, reason=reason or "parallel sub-pipeline running")

        async def save_candidate_completed(i: int, state: dict[str, Any], conclusions: dict[str, Any]) -> None:
            entry = {
                "status": "completed",
                "candidate": candidates[i],
                "sub_pipeline_id": state.get("sub_pipeline_id") or f"{sub_spec.name}_candidate_{i}",
                "state_machine": state.get("state_machine"),
                "context": state.get("context"),
                "current_sub_step": state.get("current_sub_step", ""),
                "current_index": state.get("current_index"),
                "active_attempt_id": state.get("active_attempt_id"),
                "transcript_id": state.get("transcript_id"),
                "conclusions": conclusions,
            }
            self._execution.setdefault("candidates", {})[str(i)] = entry
            await self._save_running(step.step_id, reason="parallel candidate completed")

        async def save_candidate_failed(i: int, state: dict[str, Any]) -> None:
            active_attempt_id = state.get("active_attempt_id")
            if active_attempt_id:
                self._mark_attempt_status(active_attempt_id, "failed")
            entry = {
                "status": "failed",
                "candidate": candidates[i],
                "sub_pipeline_id": state.get("sub_pipeline_id") or f"{sub_spec.name}_candidate_{i}",
                "state_machine": state.get("state_machine"),
                "context": state.get("context"),
                "current_sub_step": state.get("current_sub_step", ""),
                "current_index": state.get("current_index"),
                "active_attempt_id": active_attempt_id,
                "transcript_id": state.get("transcript_id"),
                "conclusions": state.get("conclusions", {}),
            }
            if state.get("error") is not None:
                entry["error"] = state["error"]
            if state.get("error_details") is not None:
                entry["error_details"] = state["error_details"]
            self._execution.setdefault("candidates", {})[str(i)] = entry
            failed_by_index[i] = dict(entry)
            await self._save_running(step.step_id, reason="parallel candidate failed")

        async def run_candidate(
            i: int,
            candidate: dict,
            restart_info: RestartInfo | None = None,
            resume_state: dict[str, Any] | None = None,
        ) -> None:
            candidate_started_at = self._observability.now()
            default_sub_pipeline_id = f"{sub_spec.name}_candidate_{i}"
            state = {
                "task": asyncio.current_task(),
                "current_sub_step": "",
                "conclusions": restart_info.preserved_conclusions if restart_info else {},
                "name": candidate.get("name", _("Candidate {index}").format(index=i + 1)),
                "agent_loop": None,
                "sub_pipeline_id": (resume_state or {}).get("sub_pipeline_id") or default_sub_pipeline_id,
                "state_machine": (resume_state or {}).get("state_machine"),
                "context": (resume_state or {}).get("context"),
                "active_attempt_id": (resume_state or {}).get("active_attempt_id"),
                "transcript_id": (resume_state or {}).get("transcript_id"),
            }
            pending_ask_resume = (resume_state or {}).get(_PENDING_ASK_USER_QUESTION_RESUME_KEY)
            if pending_ask_resume is not None:
                state[_PENDING_ASK_USER_QUESTION_RESUME_KEY] = pending_ask_resume
            self._active_candidates[i] = state

            def allocate_sub_step_attempt(request: dict[str, Any]) -> dict[str, Any]:
                request_resume_state = request.get("resume_state")
                attempt = self._ensure_sub_step_attempt(
                    parent_step_id=step.step_id,
                    candidate_index=i,
                    sub_pipeline_id=request["sub_pipeline_id"],
                    sub_step_id=request["sub_step_id"],
                    resume_state=request_resume_state,
                )
                resume_messages = None
                if (
                    request_resume_state
                    and request_resume_state.get("active_attempt_id") == attempt.get("attempt_id")
                    and request_resume_state.get("current_sub_step") == request["sub_step_id"]
                ):
                    resume_messages = self._load_repaired_resume_messages(attempt.get("transcript_id"))
                return {**attempt, "resume_messages": resume_messages}

            async def record_sub_step_state(payload: dict[str, Any]) -> None:
                attempt_status = payload.get("attempt_status")
                state["current_sub_step"] = payload.get("current_sub_step", "")
                state["sub_pipeline_id"] = payload.get("sub_pipeline_id") or state["sub_pipeline_id"]
                state["state_machine"] = payload.get("state_machine")
                state["context"] = payload.get("context")
                if attempt_status in {"running", "failed"}:
                    state["active_attempt_id"] = payload.get("active_attempt_id")
                    state["transcript_id"] = payload.get("transcript_id")
                else:
                    state["active_attempt_id"] = None
                    state["transcript_id"] = None
                state["conclusions"] = payload.get("conclusions", state.get("conclusions", {}))
                await save_candidate_execution_state(
                    i,
                    payload,
                    reason=f"sub-step {payload.get('current_sub_step', '')} {payload.get('attempt_status', '')}",
                )

            try:
                execute_streaming = sub_executors[i].execute_streaming
                start_from_step = restart_info.start_from_step if restart_info else None
                preserved_conclusions = restart_info.preserved_conclusions if restart_info else None
                candidate_user_message = (
                    restart_info.rollback_input
                    if restart_info and restart_info.rollback_input is not None
                    else restart_info.rollback_context
                    if restart_info
                    else self._candidate_user_message_for_restored_supplement(i, user_message)
                )
                ask_user_message = self._candidate_user_message_for_restored_ask_user_question(i)
                candidate_resume_messages = self._candidate_resume_messages_for_restored_ask_user_question(i)
                candidate_precompleted_tools = self._candidate_precompleted_tools_for_restored_ask_user_question(i)
                if ask_user_message is not None:
                    candidate_user_message = ask_user_message
                parameters = inspect.signature(execute_streaming).parameters
                has_var_keyword = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                )
                supports_recovery_kwargs = "resume_state" in parameters or has_var_keyword
                if supports_recovery_kwargs:
                    recovery_kwargs: dict[str, Any] = {
                        "resume_state": resume_state,
                        "sub_step_attempt_allocator": allocate_sub_step_attempt,
                        "sub_step_state_callback": record_sub_step_state,
                    }
                    if "precompleted_tools" in parameters or has_var_keyword:
                        recovery_kwargs["precompleted_tools"] = candidate_precompleted_tools
                    if "resume_messages" in parameters or has_var_keyword:
                        recovery_kwargs["resume_messages"] = candidate_resume_messages
                    event_stream = execute_streaming(
                        sub_spec=sub_spec,
                        candidate=candidate,
                        candidate_index=i,
                        parent_context=self.context,
                        session_id=self._session_id,
                        start_from_step=start_from_step,
                        preserved_conclusions=preserved_conclusions,
                        user_message=candidate_user_message,
                        parent_step_id=step.step_id,
                        **recovery_kwargs,
                    )
                else:
                    event_stream = execute_streaming(
                        sub_spec=sub_spec,
                        candidate=candidate,
                        candidate_index=i,
                        parent_context=self.context,
                        session_id=self._session_id,
                        start_from_step=start_from_step,
                        preserved_conclusions=preserved_conclusions,
                        user_message=candidate_user_message,
                        parent_step_id=step.step_id,
                    )
                async for event in event_stream:
                    if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_PIPELINE_STARTED:
                        state["sub_pipeline_id"] = event.data.get("sub_pipeline_id") or default_sub_pipeline_id
                    if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_STEP_STARTED:
                        state["current_sub_step"] = event.data.get("step_id", "")
                    state["agent_loop"] = sub_executors[i].current_step_executor_agent_loop
                    if isinstance(event, PipelineEvent):
                        _normalize_failed_sub_pipeline_completed_event(event)
                    if (
                        isinstance(event, PipelineEvent)
                        and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
                        and not event.data.get("failed", False)
                    ):
                        conclusions_by_index[i] = event.data.get("conclusions", {})
                        state["conclusions"] = conclusions_by_index[i]
                        await save_candidate_completed(i, state, conclusions_by_index[i])
                    if (
                        isinstance(event, PipelineEvent)
                        and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
                        and event.data.get("failed", False)
                    ):
                        state["error"] = event.data.get("error")
                        state["error_details"] = event.data.get("error_details")
                        await save_candidate_failed(i, state)
                    await event_queue.put(event)
            except asyncio.CancelledError:
                logger.debug("Candidate %d cancelled", i)
            except PipelineStatePersistenceError as exc:
                await event_queue.put(exc)
            except Exception as exc:
                failure = public_error_from_exception(exc)
                error_summary = failure.summary
                error_type = failure.details["type"]
                candidate_name = state.get("name", _("Candidate {index}").format(index=i + 1))
                sub_pipeline_id = state.get("sub_pipeline_id") or default_sub_pipeline_id
                failed_attrs = {
                    "parent_step_id": step.step_id,
                    "sub_pipeline_name": sub_spec.name,
                    "sub_pipeline_id": sub_pipeline_id,
                    "candidate_index": i,
                    "candidate_name": candidate_name,
                    "total_steps": len(sub_spec.steps),
                    "error_summary": error_summary,
                    "error_type": error_type,
                    "error_id": failure.error_id,
                }
                self._observability.sub_pipeline_completed(
                    duration_ms=self._observability.duration_ms(candidate_started_at),
                    failed=True,
                    **failed_attrs,
                )
                log_extra = {
                    "pipeline": self._loaded.name,
                    "session_id": self._session_id,
                    **failed_attrs,
                }
                logger.warning(
                    (
                        "Pipeline candidate failed: pipeline=%s session_id=%s parent_step_id=%s "
                        "sub_pipeline_name=%s sub_pipeline_id=%s candidate_index=%d "
                        "candidate_name=%s error_type=%s error_summary=%s"
                    ),
                    self._loaded.name,
                    self._session_id,
                    step.step_id,
                    sub_spec.name,
                    sub_pipeline_id,
                    i,
                    candidate_name,
                    error_type,
                    error_summary,
                    exc_info=True,
                    extra=log_extra,
                )
                state["error"] = error_summary
                state["error_details"] = failure.details
                try:
                    await save_candidate_failed(i, state)
                except PipelineStatePersistenceError as persistence_exc:
                    await event_queue.put(persistence_exc)
                    return
                await event_queue.put(
                    PipelineEvent(
                        type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=time.time(),
                        data={
                            "sub_pipeline_id": sub_pipeline_id,
                            "candidate_index": i,
                            "candidate_name": candidate_name,
                            "sub_pipeline_name": sub_spec.name,
                            "failed": True,
                            "error": error_summary,
                            "error_summary": error_summary,
                            "error_details": failure.details,
                        },
                    )
                )
            finally:
                self._active_candidates.pop(i, None)
                await event_queue.put(CandidateSentinel(candidate_index=i))

        tasks: list[asyncio.Task | None] = []
        initial_done_count = 0
        for i, candidate in enumerate(candidates):
            restored = restored_candidates.get(str(i), {})
            restart_info = self._pending_candidate_restarts.pop(i, None)
            if restart_info is None and restored.get("status") == "running":
                restart_info = self._persisted_candidate_restart_info(restored)
            if restart_info is not None:
                resume_state = restored if isinstance(restored, dict) and restored.get("status") == "running" else None
                tasks.append(asyncio.create_task(run_candidate(i, candidate, restart_info, resume_state=resume_state)))
            elif restored.get("status") == "completed":
                conclusions_by_index[i] = restored.get("conclusions", {})
                self._execution.setdefault("candidates", {})[str(i)] = dict(restored)
                tasks.append(None)
                initial_done_count += 1
            elif restored.get("status") == "failed":
                failed_by_index[i] = dict(restored)
                self._execution.setdefault("candidates", {})[str(i)] = dict(restored)
                tasks.append(None)
                initial_done_count += 1
            elif restored.get("status") == "running":
                tasks.append(asyncio.create_task(run_candidate(i, candidate, resume_state=restored)))
            else:
                tasks.append(asyncio.create_task(run_candidate(i, candidate)))
        self._parallel_candidates_total = len(candidates)

        try:
            # Expose for iter_active_agent_loops aggregation (problem 6). Set
            # as the first statement inside try so the matching finally always
            # clears it even if something between sub_executors construction
            # and this point raised; cleared below so /status sees an empty
            # list once the parallel step ends.
            self._current_sub_executor_list = sub_executors
            done_count = initial_done_count
            total = len(candidates)
            while done_count < total:
                event = await event_queue.get()
                if isinstance(event, PipelineStatePersistenceError):
                    yield self._persistence_failure_event(event)
                    return
                if isinstance(event, CandidateSentinel):
                    idx = event.candidate_index
                    if idx in self._pending_candidate_restarts:
                        restart_info = self._pending_candidate_restarts.pop(idx)
                        resume_state = self._execution.get("candidates", {}).get(str(idx))
                        if not isinstance(resume_state, dict):
                            resume_state = None
                        new_task = asyncio.create_task(
                            run_candidate(idx, candidates[idx], restart_info, resume_state=resume_state)
                        )
                        tasks[idx] = new_task
                    else:
                        done_count += 1
                else:
                    # P-I22: ensure failed SUB_PIPELINE_COMPLETED events carry the
                    # structured ``error_summary`` + ``error_details`` keys so
                    # downstream consumers (judge state, logging, future UI) can
                    # route on type/traceback instead of regex-matching a string.
                    # Upstream ``execute_streaming`` already populates these; we
                    # backfill from the legacy ``error`` key as a safety net for
                    # any code path that emits the event with only ``error``.
                    if isinstance(event, PipelineEvent):
                        _normalize_failed_sub_pipeline_completed_event(event)
                    yield event
        finally:
            cancelled_task_ids = set()
            for idx, state in list(self._active_candidates.items()):
                cancelled_task = self._cancel_candidate_task(
                    idx,
                    state,
                    reason="parallel_cleanup",
                    parent_step_id=step.step_id,
                )
                if cancelled_task is not None:
                    cancelled_task_ids.add(id(cancelled_task))
            for t in tasks:
                if t is not None and not t.done() and id(t) not in cancelled_task_ids:
                    t.cancel()
            await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)
            self._parallel_candidates_total = 0
            self._current_sub_executor_list = None

        aggregated: Any = []
        for i, candidate in enumerate(candidates):
            if i in conclusions_by_index:
                result = {"candidate": candidate, "failed": False}
                result.update(conclusions_by_index[i])
                aggregated.append(result)
            elif i in failed_by_index:
                restored = failed_by_index[i]
                result = {"candidate": restored.get("candidate", candidate), "failed": True}
                result.update(restored.get("conclusions", {}))
                if restored.get("error") is not None:
                    result["error"] = restored["error"]
                if restored.get("error_details") is not None:
                    result["error_details"] = restored["error_details"]
                aggregated.append(result)
            else:
                aggregated.append({"candidate": candidate, "failed": True})

        self.context.set_conclusion(step.conclusion_field, aggregated)
        candidate_success_count = sum(1 for item in aggregated if isinstance(item, dict) and not item.get("failed"))
        candidate_failed_count = len(candidates) - candidate_success_count
        self._observability.candidates_evaluated(
            parent_step_id=step.step_id,
            sub_pipeline_name=sub_spec.name,
            candidate_count=len(candidates),
            candidate_success_count=candidate_success_count,
            candidate_failed_count=candidate_failed_count,
        )
        self._execution = {}

        if emit_step_completed_event:
            yield PipelineEvent(
                type=PipelineEventType.STEP_COMPLETED,
                step_id=step.step_id,
                timestamp=time.time(),
                data={
                    "duration_s": 0,
                    "candidates_count": len(candidates),
                    "conclusion_field": step.conclusion_field,
                    "conclusion": aggregated,
                },
            )

    def _resolve_iterate_field(self, path: str) -> list[dict]:
        """Resolve a context field path (e.g., 'candidates' or 'plan.options') for iteration.

        Raises:
            ValueError: if the field is missing OR is an empty list. Pipelines that
                legitimately want zero iterations should NOT use parallel_sub_pipeline.
        """
        snapshot = self.context.snapshot()
        parts = path.split(".")
        cur: Any = snapshot
        for p in parts:
            if not isinstance(cur, dict) or p not in cur:
                raise ValueError(
                    f"Iterate field {path!r} not present in context. "
                    f"Available top-level keys: {sorted(snapshot.keys())}"
                )
            cur = cur[p]
        if not isinstance(cur, list):
            raise ValueError(f"Iterate field {path!r} is not a list (got {type(cur).__name__})")
        if not cur:
            raise ValueError(f"Iterate field {path!r} is empty; parallel step requires >=1 item")
        return cur
