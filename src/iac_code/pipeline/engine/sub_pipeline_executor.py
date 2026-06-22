"""SubPipelineExecutor — runs a sub-pipeline for a single candidate."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iac_code.agent.message import ContentBlock, Message
from iac_code.i18n import _
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.observability import PipelineObservability
from iac_code.pipeline.engine.public_errors import public_error, public_error_from_exception
from iac_code.pipeline.engine.resume_recovery import reconcile_resume_messages, user_message_already_in_resume
from iac_code.pipeline.engine.state_machine import StateMachine
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import LoadedPipeline, SubPipelineSpec
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.types.stream_events import SubPipelineStreamEvent

logger = logging.getLogger(__name__)


@dataclass
class SubPipelineResult:
    """Result of executing a sub-pipeline for one candidate."""

    sub_pipeline_id: str
    candidate_index: int
    candidate: dict
    conclusions: dict[str, Any] = field(default_factory=dict)
    failed: bool = False
    error: str | None = None
    error_details: dict | None = None  # Structured public error info.

    def to_dict(self) -> dict:
        result: dict[str, Any] = {
            "candidate": self.candidate,
            "failed": self.failed,
        }
        if self.error:
            result["error"] = self.error
        result.update(self.conclusions)
        return result


class SubPipelineExecutor:
    """Executes a sub-pipeline (template->review->cost) for a single candidate.

    Each sub-pipeline runs linearly through its steps using an isolated
    PipelineContext. The parent context fields specified in the SubPipelineSpec
    are copied in as read-only seeds, and the candidate dict is injected as
    a pseudo-field named "candidate".
    """

    def __init__(
        self,
        provider_manager: Any,
        base_tool_registry: Any,
        pipeline: LoadedPipeline,
        pipeline_dir: Path,
        session_storage: Any = None,
        cwd: str | None = None,
        pause_event: asyncio.Event | None = None,
        permission_context_getter: Callable[[], Any] | None = None,
        memory_content_getter: Callable[[], str] | None = None,
        auto_trigger_skills: list[Any] | None = None,
    ) -> None:
        self._provider_manager = provider_manager
        self._base_tool_registry = base_tool_registry
        self._pipeline = pipeline
        self._pipeline_dir = pipeline_dir
        self._session_storage = session_storage
        self._cwd = cwd
        self._pause_event = pause_event
        self._permission_context_getter = permission_context_getter
        self._memory_content_getter = memory_content_getter
        self._auto_trigger_skills = auto_trigger_skills or []
        self._active_step_executor = None
        self._telemetry_correlation: dict[str, str] = {}
        pipeline_name = getattr(pipeline, "name", "")
        if not isinstance(pipeline_name, str):
            pipeline_name = ""
        self._observability = PipelineObservability(
            pipeline_name=pipeline_name,
            session_id="",
            cwd=self._cwd or "",
        )

    @property
    def current_step_executor_agent_loop(self):
        """Return the AgentLoop of the currently executing step, if any."""
        if self._active_step_executor:
            return self._active_step_executor.current_agent_loop
        return None

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
        if self._active_step_executor is not None:
            self._apply_telemetry_correlation(self._active_step_executor)

    def _apply_telemetry_correlation(self, executor: Any) -> None:
        setter = getattr(executor, "set_telemetry_correlation", None)
        if callable(setter):
            setter(**self._telemetry_correlation)

    async def execute(
        self,
        sub_spec: SubPipelineSpec,
        candidate: dict,
        candidate_index: int,
        parent_context: PipelineContext,
        session_id: str,
        event_callback: Callable[[PipelineEvent], None] | None = None,
    ) -> SubPipelineResult:
        """Execute all steps in a sub-pipeline for a single candidate.

        Returns a SubPipelineResult containing the accumulated conclusions
        or failure information.
        """
        sub_pipeline_id = f"{sub_spec.name}_{uuid.uuid4().hex[:8]}"

        sub_context = self._build_sub_context(sub_spec, candidate, parent_context)
        state_machine = StateMachine(sub_spec.steps, sub_spec.max_rollbacks)

        step_executor = StepExecutor(
            provider_manager=self._provider_manager,
            base_tool_registry=self._base_tool_registry,
            pipeline=self._pipeline,
            pipeline_dir=self._pipeline_dir,
            session_storage=self._session_storage,
            cwd=self._cwd,
            pause_event=self._pause_event,
            permission_context_getter=self._permission_context_getter,
            memory_content_getter=self._memory_content_getter,
            auto_trigger_skills=self._auto_trigger_skills,
        )
        self._apply_telemetry_correlation(step_executor)

        if event_callback:
            event_callback(
                PipelineEvent(
                    type=PipelineEventType.SUB_PIPELINE_STARTED,
                    step_id=None,
                    timestamp=time.time(),
                    data={
                        "sub_pipeline_id": sub_pipeline_id,
                        "candidate_index": candidate_index,
                        "sub_pipeline_name": sub_spec.name,
                    },
                )
            )

        conclusions: dict[str, Any] = {}

        try:
            while not state_machine.is_complete:
                step = state_machine.current_step

                if event_callback:
                    event_callback(
                        PipelineEvent(
                            type=PipelineEventType.SUB_STEP_STARTED,
                            step_id=step.step_id,
                            timestamp=time.time(),
                            data={
                                "sub_pipeline_id": sub_pipeline_id,
                                "candidate_index": candidate_index,
                                "step_id": step.step_id,
                            },
                        )
                    )

                step_result: StepResult | None = None
                async for event in step_executor.execute(
                    step,
                    sub_context,
                    session_id,
                    rollback_targets=state_machine.completed_non_future_rollback_targets(),
                    rollback_count=state_machine.rollback_count,
                    max_rollbacks=state_machine.max_rollbacks,
                ):
                    if isinstance(event, StepResult):
                        step_result = event

                if step_result is None or step_result.status == StepStatus.FAILED:
                    failure = public_error(
                        message=step_result.error if step_result else "No result from step executor",
                        error_type="StepFailed",
                        extra_details={"step_id": step.step_id},
                    )
                    if event_callback:
                        event_callback(
                            PipelineEvent(
                                type=PipelineEventType.SUB_STEP_FAILED,
                                step_id=step.step_id,
                                timestamp=time.time(),
                                data={
                                    "sub_pipeline_id": sub_pipeline_id,
                                    "candidate_index": candidate_index,
                                    "step_id": step.step_id,
                                    "error": failure.summary,
                                    "error_summary": failure.summary,
                                    "error_details": failure.details,
                                },
                            )
                        )
                    return SubPipelineResult(
                        sub_pipeline_id=sub_pipeline_id,
                        candidate_index=candidate_index,
                        candidate=candidate,
                        conclusions=conclusions,
                        failed=True,
                        error=failure.summary,
                        error_details=failure.details,
                    )

                # Accumulate conclusions
                if step_result.conclusion:
                    conclusions[step.conclusion_field] = step_result.conclusion

                if event_callback:
                    event_callback(
                        PipelineEvent(
                            type=PipelineEventType.SUB_STEP_COMPLETED,
                            step_id=step.step_id,
                            timestamp=time.time(),
                            data={
                                "sub_pipeline_id": sub_pipeline_id,
                                "candidate_index": candidate_index,
                                "step_id": step.step_id,
                            },
                        )
                    )

                # Handle rollback within the sub-pipeline
                if step_result.rollback_request:
                    target, reason = step_result.rollback_request
                    try:
                        state_machine.rollback(target, reason, allow_completed_non_future=True)
                        conclusions = self._conclusions_before_step(sub_spec, target, conclusions)
                        self._mark_rolled_back_fields_stale(sub_context, sub_spec, target)
                    except ValueError as e:
                        valid_targets = [s.step_id for s in sub_spec.steps]
                        failure = public_error(
                            message=str(e),
                            error_type="InvalidRollbackTarget",
                            extra_details={"target": target, "valid_targets": valid_targets},
                        )
                        return SubPipelineResult(
                            sub_pipeline_id=sub_pipeline_id,
                            candidate_index=candidate_index,
                            candidate=candidate,
                            conclusions=conclusions,
                            failed=True,
                            error=failure.summary,
                            error_details=failure.details,
                        )
                    continue

                state_machine.advance()

        except Exception as e:
            failure = public_error_from_exception(e)
            return SubPipelineResult(
                sub_pipeline_id=sub_pipeline_id,
                candidate_index=candidate_index,
                candidate=candidate,
                conclusions=conclusions,
                failed=True,
                error=failure.summary,
                error_details=failure.details,
            )

        if event_callback:
            event_callback(
                PipelineEvent(
                    type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=time.time(),
                    data={
                        "sub_pipeline_id": sub_pipeline_id,
                        "candidate_index": candidate_index,
                        "failed": False,
                    },
                )
            )

        return SubPipelineResult(
            sub_pipeline_id=sub_pipeline_id,
            candidate_index=candidate_index,
            candidate=candidate,
            conclusions=conclusions,
            failed=False,
        )

    def _make_step_executor(self) -> StepExecutor:
        """Create a StepExecutor instance for internal use."""
        executor = StepExecutor(
            provider_manager=self._provider_manager,
            base_tool_registry=self._base_tool_registry,
            pipeline=self._pipeline,
            pipeline_dir=self._pipeline_dir,
            session_storage=self._session_storage,
            cwd=self._cwd,
            pause_event=self._pause_event,
            permission_context_getter=self._permission_context_getter,
            memory_content_getter=self._memory_content_getter,
            auto_trigger_skills=self._auto_trigger_skills,
        )
        self._apply_telemetry_correlation(executor)
        return executor

    async def execute_streaming(
        self,
        sub_spec: SubPipelineSpec,
        candidate: dict,
        candidate_index: int,
        parent_context: PipelineContext,
        session_id: str,
        *,
        start_from_step: str | None = None,
        preserved_conclusions: dict[str, Any] | None = None,
        user_message: str | list[ContentBlock] | None = None,
        resume_messages: list[Message] | None = None,
        parent_step_id: str | None = None,
        resume_state: dict[str, Any] | None = None,
        sub_step_attempt_allocator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        sub_step_state_callback: Callable[[dict[str, Any]], Any] | None = None,
        precompleted_tools: dict[str, dict[str, Any]] | None = None,
    ) -> AsyncGenerator[PipelineEvent | SubPipelineStreamEvent, None]:
        """Execute sub-pipeline yielding all events in real-time for UI rendering."""
        self._observability.session_id = session_id
        sub_pipeline_id = (
            str(resume_state["sub_pipeline_id"])
            if resume_state and resume_state.get("sub_pipeline_id")
            else f"{sub_spec.name}_{uuid.uuid4().hex[:8]}"
        )
        if resume_state and isinstance(resume_state.get("context"), dict):
            sub_context = PipelineContext.from_snapshot(
                resume_state["context"],
                self._sub_context_dependencies(sub_spec),
            )
        else:
            sub_context = self._build_sub_context(sub_spec, candidate, parent_context)
        if preserved_conclusions and not resume_state:
            for field_name, value in preserved_conclusions.items():
                sub_context.set_conclusion(field_name, value)
        if resume_state and isinstance(resume_state.get("state_machine"), dict):
            state_machine = StateMachine.from_snapshot(
                resume_state["state_machine"],
                sub_spec.steps,
                max_rollbacks=sub_spec.max_rollbacks,
            )
        else:
            state_machine = StateMachine(sub_spec.steps, sub_spec.max_rollbacks)
        step_executor = self._make_step_executor()
        self._active_step_executor = step_executor
        candidate_name = candidate.get("name", _("Candidate {index}").format(index=candidate_index + 1))
        sub_pipeline_started_at = self._observability.now()
        sub_pipeline_attrs: dict[str, Any] = {
            "parent_step_id": parent_step_id,
            "sub_pipeline_name": sub_spec.name,
            "sub_pipeline_id": sub_pipeline_id,
            "candidate_index": candidate_index,
            "candidate_name": candidate_name,
            "total_sub_steps": len(sub_spec.steps),
        }
        self._observability.sub_pipeline_started(**sub_pipeline_attrs)

        def sub_pipeline_event_data(extra: dict[str, Any] | None = None) -> dict[str, Any]:
            data: dict[str, Any] = {
                "sub_pipeline_id": sub_pipeline_id,
                "candidate_index": candidate_index,
                "candidate_name": candidate_name,
                "sub_pipeline_name": sub_spec.name,
                "total_steps": len(sub_spec.steps),
            }
            if extra:
                data.update(extra)
            return data

        def sub_step_event_data(step_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
            return sub_pipeline_event_data({"step_id": step_id, **(extra or {})})

        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data=sub_pipeline_event_data(),
        )

        if resume_state and isinstance(resume_state.get("conclusions"), dict):
            conclusions = dict(resume_state["conclusions"])
        else:
            conclusions: dict[str, Any] = {}
            for existing_step in sub_spec.steps:
                field = sub_context.get_field(existing_step.conclusion_field)
                if field.value is not None and not field.stale:
                    conclusions[existing_step.conclusion_field] = field.value
        is_first_step = True
        terminal_event: PipelineEvent | None = None
        current_step_attrs: dict[str, Any] | None = None
        sub_step_started_at: float | None = None
        current_sub_step_id: str | None = None
        attempt_info: dict[str, Any] | None = None

        async def publish_sub_step_state(
            *,
            status: str,
            attempt_status: str,
            current_sub_step: str,
            attempt_info: dict[str, Any],
        ) -> None:
            if sub_step_state_callback is None:
                return
            payload = {
                "status": status,
                "attempt_status": attempt_status,
                "candidate": candidate,
                "candidate_index": candidate_index,
                "sub_pipeline_id": sub_pipeline_id,
                "sub_pipeline_name": sub_spec.name,
                "current_sub_step": current_sub_step,
                "current_index": (
                    state_machine.current_step_index if not state_machine.is_complete else len(sub_spec.steps)
                ),
                "state_machine": state_machine.to_snapshot(),
                "context": sub_context.to_snapshot(),
                "active_attempt_id": attempt_info.get("attempt_id"),
                "transcript_id": attempt_info.get("transcript_id"),
                "conclusions": dict(conclusions),
            }
            result = sub_step_state_callback(payload)
            if asyncio.iscoroutine(result):
                await result

        def allocate_sub_step_attempt(step_id: str) -> dict[str, Any]:
            request = {
                "candidate": candidate,
                "candidate_index": candidate_index,
                "sub_pipeline_id": sub_pipeline_id,
                "sub_pipeline_name": sub_spec.name,
                "parent_step_id": parent_step_id,
                "sub_step_id": step_id,
                "current_index": state_machine.current_step_index,
                "state_machine": state_machine.to_snapshot(),
                "context": sub_context.to_snapshot(),
                "resume_state": resume_state,
            }
            if sub_step_attempt_allocator is not None:
                return sub_step_attempt_allocator(request) or {}
            if resume_state and step_id == resume_state.get("current_sub_step"):
                return {
                    "attempt_id": resume_state.get("active_attempt_id"),
                    "transcript_id": resume_state.get("transcript_id"),
                    "resume_messages": resume_state.get("resume_messages"),
                }
            return {}

        def emit_sub_pipeline_failed(
            error_summary: str,
            error_type: str,
            error_id: str,
            extra_attrs: dict[str, Any] | None = None,
        ) -> None:
            attrs = dict(sub_pipeline_attrs)
            if extra_attrs:
                attrs.update(extra_attrs)
            self._observability.sub_pipeline_completed(
                duration_ms=self._observability.duration_ms(sub_pipeline_started_at),
                failed=True,
                error_summary=error_summary,
                error_type=error_type,
                error_id=error_id,
                **attrs,
            )
            log_extra = {
                "pipeline": self._pipeline.name,
                "session_id": session_id,
                "parent_step_id": parent_step_id,
                "sub_pipeline_name": sub_spec.name,
                "sub_pipeline_id": sub_pipeline_id,
                "candidate_index": candidate_index,
                "candidate_name": candidate_name,
                "error_summary": error_summary,
                "error_type": error_type,
            }
            if extra_attrs:
                log_extra.update(extra_attrs)
            logger.warning(
                (
                    "Sub-pipeline failed: pipeline=%s session_id=%s parent_step_id=%s "
                    "sub_pipeline_name=%s sub_pipeline_id=%s candidate_index=%d "
                    "candidate_name=%s error_type=%s error_summary=%s"
                ),
                self._pipeline.name,
                session_id,
                parent_step_id,
                sub_spec.name,
                sub_pipeline_id,
                candidate_index,
                candidate_name,
                error_type,
                error_summary,
                extra=log_extra,
            )

        def sub_step_attrs_for_current(step, step_index: int) -> dict[str, Any]:
            return {
                **sub_pipeline_attrs,
                "sub_step_id": step.step_id,
                "sub_step_index": step_index + 1,
                "total_sub_steps": len(sub_spec.steps),
            }

        try:
            with self._observability.sub_pipeline_span(**sub_pipeline_attrs):
                try:
                    # P-I4: validate start_from_step INSIDE the try so an invalid
                    # step id (e.g., LLM-hallucinated) is caught by the except
                    # below and surfaced as SUB_PIPELINE_COMPLETED(failed=True)
                    # instead of escaping as a silent ValueError from the generator.
                    if start_from_step:
                        state_machine.jump_to(start_from_step)
                    while not state_machine.is_complete:
                        step = state_machine.current_step
                        attempt_info = None
                        current_sub_step_id = step.step_id
                        current_step_attrs = sub_step_attrs_for_current(step, state_machine.current_step_index)
                        sub_step_started_at = self._observability.now()
                        self._observability.sub_step_started(**current_step_attrs)
                        attempt_info = allocate_sub_step_attempt(step.step_id)
                        await publish_sub_step_state(
                            status="running",
                            attempt_status="running",
                            current_sub_step=step.step_id,
                            attempt_info=attempt_info,
                        )

                        yield PipelineEvent(
                            type=PipelineEventType.SUB_STEP_STARTED,
                            step_id=step.step_id,
                            timestamp=time.time(),
                            data=sub_step_event_data(
                                step.step_id,
                                {
                                    "step_index": state_machine.current_step_index,
                                    "active_attempt_id": attempt_info.get("attempt_id"),
                                    "transcript_id": attempt_info.get("transcript_id"),
                                },
                            ),
                        )

                        step_msg = user_message if is_first_step else None
                        step_precompleted_tools = precompleted_tools if is_first_step else None
                        attempt_resume_messages = attempt_info.get("resume_messages")
                        if not isinstance(attempt_resume_messages, list):
                            attempt_resume_messages = []
                        explicit_resume_messages = (
                            resume_messages if is_first_step and resume_messages is not None else []
                        )
                        step_resume_messages = reconcile_resume_messages(
                            attempt_resume_messages,
                            explicit_resume_messages,
                        )
                        if step_resume_messages and (
                            isinstance(step_msg, str) or user_message_already_in_resume(step_msg, step_resume_messages)
                        ):
                            step_msg = None
                        is_first_step = False
                        step_result: StepResult | None = None
                        with self._observability.sub_step_span(**current_step_attrs):
                            execute_kwargs: dict[str, Any] = {
                                "user_message": step_msg,
                                "attempt_id": attempt_info.get("attempt_id"),
                                "transcript_id": attempt_info.get("transcript_id"),
                                "resume_messages": step_resume_messages,
                                "precompleted_tools": step_precompleted_tools,
                                "rollback_targets": state_machine.completed_non_future_rollback_targets(),
                                "rollback_count": state_machine.rollback_count,
                                "max_rollbacks": state_machine.max_rollbacks,
                            }
                            try:
                                parameters = inspect.signature(step_executor.execute).parameters
                            except (TypeError, ValueError):
                                parameters = {}
                            if not any(
                                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                            ):
                                execute_kwargs = {
                                    key: value for key, value in execute_kwargs.items() if key in parameters
                                }
                            async for event in step_executor.execute(
                                step,
                                sub_context,
                                session_id,
                                **execute_kwargs,
                            ):
                                if isinstance(event, StepResult):
                                    step_result = event
                                elif isinstance(event, PipelineEvent):
                                    # Forward pipeline-level events from the step executor directly
                                    yield event
                                else:
                                    yield SubPipelineStreamEvent(
                                        sub_pipeline_id=sub_pipeline_id,
                                        candidate_index=candidate_index,
                                        inner=event,
                                    )

                        if step_result is None or step_result.status == StepStatus.FAILED:
                            # P-I22: surface structured error info (error_summary + error_details)
                            # alongside the legacy ``error`` key so UI consumers (repl.py) keep working.
                            err_msg = step_result.error if step_result else "No result"
                            failure = public_error(
                                message=err_msg,
                                error_type="StepFailed",
                                extra_details={"step_id": step.step_id},
                            )
                            error_summary = failure.summary
                            self._observability.sub_step_completed(
                                duration_ms=self._observability.duration_ms(sub_step_started_at),
                                failed=True,
                                error_summary=error_summary,
                                error_type="StepFailed",
                                error_id=failure.error_id,
                                **current_step_attrs,
                            )
                            await publish_sub_step_state(
                                status="failed",
                                attempt_status="failed",
                                current_sub_step=step.step_id,
                                attempt_info=attempt_info,
                            )
                            yield PipelineEvent(
                                type=PipelineEventType.SUB_STEP_FAILED,
                                step_id=step.step_id,
                                timestamp=time.time(),
                                data=sub_step_event_data(
                                    step.step_id,
                                    {
                                        "error": error_summary,
                                        "error_summary": error_summary,
                                        "error_details": failure.details,
                                    },
                                ),
                            )
                            terminal_event = PipelineEvent(
                                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                                step_id=None,
                                timestamp=time.time(),
                                data=sub_pipeline_event_data(
                                    {
                                        "failed": True,
                                        "error": error_summary,
                                        "error_summary": error_summary,
                                        "error_details": failure.details,
                                    },
                                ),
                            )
                            emit_sub_pipeline_failed(error_summary, "StepFailed", failure.error_id)
                            break

                        conclusions[step.conclusion_field] = step_result.conclusion

                        if step_result.rollback_request:
                            target, reason = step_result.rollback_request
                            try:
                                state_machine.rollback(target, reason, allow_completed_non_future=True)
                                conclusions = self._conclusions_before_step(sub_spec, target, conclusions)
                                self._observability.sub_step_completed(
                                    duration_ms=self._observability.duration_ms(sub_step_started_at),
                                    **current_step_attrs,
                                )
                                yield PipelineEvent(
                                    type=PipelineEventType.SUB_STEP_COMPLETED,
                                    step_id=step.step_id,
                                    timestamp=time.time(),
                                    data=sub_step_event_data(
                                        step.step_id,
                                        {
                                            "conclusion_field": step.conclusion_field,
                                            "conclusion": step_result.conclusion,
                                        },
                                    ),
                                )
                                self._mark_rolled_back_fields_stale(sub_context, sub_spec, target)
                                await publish_sub_step_state(
                                    status="running",
                                    attempt_status="rolled_back",
                                    current_sub_step=target,
                                    attempt_info=attempt_info,
                                )
                            except ValueError as e:
                                # P-I9: emit SUB_STEP_FAILED for symmetry with parent runner's STEP_FAILED
                                # (P-C3). Provides a structured failure event for logging/judge state;
                                # the user-visible failure still arrives via SUB_PIPELINE_COMPLETED(failed=True).
                                valid_targets = [s.step_id for s in sub_spec.steps]
                                error = f"Invalid rollback target {target!r}. Valid targets: {valid_targets}. ({e})"
                                failure = public_error(
                                    message=error,
                                    error_type="InvalidRollbackTarget",
                                    extra_details={"target": target, "valid_targets": valid_targets},
                                )
                                err_msg = failure.summary
                                self._observability.sub_step_completed(
                                    duration_ms=self._observability.duration_ms(sub_step_started_at),
                                    failed=True,
                                    error_summary=err_msg,
                                    error_type="InvalidRollbackTarget",
                                    error_id=failure.error_id,
                                    rollback_target=target,
                                    valid_targets=valid_targets,
                                    **current_step_attrs,
                                )
                                await publish_sub_step_state(
                                    status="failed",
                                    attempt_status="failed",
                                    current_sub_step=step.step_id,
                                    attempt_info=attempt_info,
                                )
                                yield PipelineEvent(
                                    type=PipelineEventType.SUB_STEP_FAILED,
                                    step_id=step.step_id,
                                    timestamp=time.time(),
                                    data=sub_step_event_data(
                                        step.step_id,
                                        {
                                            "error": err_msg,
                                            "error_summary": err_msg,
                                            "error_details": failure.details,
                                        },
                                    ),
                                )
                                # P-I22: structured error fields for the terminal completion event.
                                terminal_event = PipelineEvent(
                                    type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                                    step_id=None,
                                    timestamp=time.time(),
                                    data=sub_pipeline_event_data(
                                        {
                                            "failed": True,
                                            "error": err_msg,
                                            "error_summary": err_msg,
                                            "error_details": failure.details,
                                        },
                                    ),
                                )
                                emit_sub_pipeline_failed(
                                    err_msg,
                                    "InvalidRollbackTarget",
                                    failure.error_id,
                                    {"rollback_target": target, "valid_targets": valid_targets},
                                )
                                break
                            continue

                        self._observability.sub_step_completed(
                            duration_ms=self._observability.duration_ms(sub_step_started_at),
                            **current_step_attrs,
                        )
                        completed_step_id = step.step_id
                        state_machine.advance()
                        next_sub_step = (
                            state_machine.current_step.step_id if not state_machine.is_complete else completed_step_id
                        )
                        await publish_sub_step_state(
                            status="running",
                            attempt_status="completed",
                            current_sub_step=next_sub_step,
                            attempt_info=attempt_info,
                        )
                        yield PipelineEvent(
                            type=PipelineEventType.SUB_STEP_COMPLETED,
                            step_id=completed_step_id,
                            timestamp=time.time(),
                            data=sub_step_event_data(
                                completed_step_id,
                                {
                                    "conclusion_field": step.conclusion_field,
                                    "conclusion": step_result.conclusion,
                                },
                            ),
                        )

                except Exception as e:
                    # Keep public failure events structured without exposing traceback text.
                    failure = public_error_from_exception(e)
                    err_msg = failure.summary
                    error_summary = failure.summary
                    error_type = failure.details["type"]
                    if current_step_attrs is not None and sub_step_started_at is not None:
                        self._observability.sub_step_completed(
                            duration_ms=self._observability.duration_ms(sub_step_started_at),
                            failed=True,
                            error_summary=error_summary,
                            error_type=error_type,
                            error_id=failure.error_id,
                            **current_step_attrs,
                        )
                    if attempt_info is not None and current_sub_step_id is not None:
                        await publish_sub_step_state(
                            status="failed",
                            attempt_status="failed",
                            current_sub_step=current_sub_step_id,
                            attempt_info=attempt_info,
                        )
                    if current_sub_step_id is not None:
                        yield PipelineEvent(
                            type=PipelineEventType.SUB_STEP_FAILED,
                            step_id=current_sub_step_id,
                            timestamp=time.time(),
                            data=sub_step_event_data(
                                current_sub_step_id,
                                {
                                    "error": error_summary,
                                    "error_summary": error_summary,
                                    "error_details": failure.details,
                                },
                            ),
                        )
                    terminal_event = PipelineEvent(
                        type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=time.time(),
                        data=sub_pipeline_event_data(
                            {
                                "failed": True,
                                "error": err_msg,
                                "error_summary": error_summary,
                                "error_details": failure.details,
                            },
                        ),
                    )
                    self._observability.sub_pipeline_completed(
                        duration_ms=self._observability.duration_ms(sub_pipeline_started_at),
                        failed=True,
                        error_summary=error_summary,
                        error_type=error_type,
                        error_id=failure.error_id,
                        **sub_pipeline_attrs,
                    )
                    log_extra = {
                        "pipeline": self._pipeline.name,
                        "session_id": session_id,
                        "parent_step_id": parent_step_id,
                        "sub_pipeline_name": sub_spec.name,
                        "sub_pipeline_id": sub_pipeline_id,
                        "candidate_index": candidate_index,
                        "candidate_name": candidate_name,
                        "error_summary": error_summary,
                        "error_type": error_type,
                        "error_id": failure.error_id,
                    }
                    logger.exception(
                        (
                            "Sub-pipeline failed: pipeline=%s session_id=%s parent_step_id=%s "
                            "sub_pipeline_name=%s sub_pipeline_id=%s candidate_index=%d "
                            "candidate_name=%s error_type=%s error_summary=%s"
                        ),
                        self._pipeline.name,
                        session_id,
                        parent_step_id,
                        sub_spec.name,
                        sub_pipeline_id,
                        candidate_index,
                        candidate_name,
                        error_type,
                        error_summary,
                        extra=log_extra,
                    )
        finally:
            self._active_step_executor = None

        if terminal_event is not None:
            yield terminal_event
            return

        self._observability.sub_pipeline_completed(
            duration_ms=self._observability.duration_ms(sub_pipeline_started_at),
            **sub_pipeline_attrs,
        )
        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data=sub_pipeline_event_data(
                {
                    "failed": False,
                    "conclusions": conclusions,
                },
            ),
        )

    def _build_sub_context(
        self,
        sub_spec: SubPipelineSpec,
        candidate: dict,
        parent_context: PipelineContext,
    ) -> PipelineContext:
        """Build an isolated context for the sub-pipeline.

        Creates a fresh PipelineContext with:
        - A "candidate" pseudo-field seeded with the candidate dict.
        - Conclusion fields for each sub-pipeline step.
        - Parent fields copied from the parent context (read-only seeds).
        """
        ctx = PipelineContext(self._sub_context_dependencies(sub_spec))

        # Inject candidate
        ctx.set_conclusion("candidate", candidate)

        # Copy specified parent fields
        for parent_field in sub_spec.context_fields_from_parent:
            value = parent_context.get_conclusion(parent_field)
            if value is not None:
                ctx.set_conclusion(parent_field, value)

        return ctx

    def _sub_context_dependencies(self, sub_spec: SubPipelineSpec) -> dict[str, list[str]]:
        """Build the dependency map used by isolated sub-pipeline contexts."""
        field_names = ["candidate"] + [s.conclusion_field for s in sub_spec.steps]
        for parent_field in sub_spec.context_fields_from_parent:
            if parent_field not in field_names:
                field_names.append(parent_field)
        return {name: [] for name in field_names}

    def _conclusions_before_step(
        self,
        sub_spec: SubPipelineSpec,
        target_step_id: str,
        conclusions: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep only conclusions produced before a rollback target."""
        try:
            target_index = [step.step_id for step in sub_spec.steps].index(target_step_id)
        except ValueError:
            return dict(conclusions)
        preserved_fields = {step.conclusion_field for step in sub_spec.steps[:target_index]}
        return {field: value for field, value in conclusions.items() if field in preserved_fields}

    def _mark_rolled_back_fields_stale(
        self,
        context: PipelineContext,
        sub_spec: SubPipelineSpec,
        target_step_id: str,
    ) -> None:
        """Mark the rollback target and later sub-step fields stale in the persisted sub-context."""
        try:
            target_index = [step.step_id for step in sub_spec.steps].index(target_step_id)
        except ValueError:
            return
        for step in sub_spec.steps[target_index:]:
            context.mark_stale(step.conclusion_field)
