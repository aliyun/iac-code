from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from a2a.types import Message, Role, TaskState, TaskStatus, TaskStatusUpdateEvent

from iac_code.a2a.events import make_text_part
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import (
    a2a_pipeline_dir_for_session,
    a2a_pipeline_dir_for_sidecar_dir,
    existing_a2a_pipeline_dir_for_session,
)
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
from iac_code.a2a.types import (
    TASK_STATE_CANCELED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_WORKING,
)
from iac_code.agent.message import Message as AgentMessage
from iac_code.i18n import _
from iac_code.pipeline import create_pipeline, discover_pipelines
from iac_code.pipeline.config import get_pipeline_name
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.handoff import build_handoff_summary, terminal_outcome_from_completed_event
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.public_errors import public_error
from iac_code.pipeline.engine.session import PipelineSession
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.session_storage import SessionStorage
from iac_code.services.telemetry import use_session_id
from iac_code.types.stream_events import AskUserQuestionEvent, SubPipelineStreamEvent

logger = logging.getLogger(__name__)
_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS = 1
_ERROR_TEXT_MAX_CHARS = 1000
_AUTH_ERROR_MARKERS = ("api key", "api_key", "access key", "secret", "credential")
_TERMINAL_SIDECAR_STATUSES = {"completed", "failed", "user_aborted", "discarded", "canceled"}
_TERMINAL_SNAPSHOT_STATUSES = {"completed", "failed", "canceled"}
_TERMINAL_A2A_STATUSES = {"completed", "failed", "canceled"}
_WAITING_A2A_STATUSES = {"waiting_input", "input_required"}
_RUNNING_A2A_STATUSES = {"working"}
_TERMINAL_EVENT_BY_SIDECAR_STATUS = {
    "completed": ("pipeline_completed", "completed"),
    "failed": ("pipeline_failed", "failed"),
    "user_aborted": ("pipeline_canceled", "canceled"),
    "discarded": ("pipeline_canceled", "canceled"),
    "canceled": ("pipeline_canceled", "canceled"),
}
_PENDING_QUESTION_NOT_ROUTED = "not_routed"
_PENDING_QUESTION_ANSWERED = "answered"
_PENDING_QUESTION_STALE_FINISHED = "stale_finished"


def _retry_text() -> str:
    return _("A temporary error occurred. Please retry.")


def _auth_error_text() -> str:
    return _("Authentication required. Configure credentials and retry.")


@dataclass
class A2APipelineRuntime:
    agent_runtime: Any
    pipeline: Any | None = None
    publisher: PipelineA2AEventPublisher | None = None
    current_stream: Any | None = None
    pending_question: "_PendingAskUserQuestion | None" = None
    restart_after_interrupt: bool = False
    pause_after_interrupt: bool = False
    restart_requested: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _StreamConsumeResult:
    had_events: bool
    restart_requested: bool


@dataclass(frozen=True)
class _SelectedPipelineStream:
    pipeline: Any
    stream: AsyncIterator[Any]


@dataclass(frozen=True)
class _TaskContextOwner:
    task_id: str
    context_id: str
    sequence: int
    status: str | None = None


@dataclass(frozen=True)
class _PendingAskUserQuestion:
    event: AskUserQuestionEvent
    envelope: dict[str, Any]


class _SidecarOwnerUnavailableError(RuntimeError):
    pass


class _SidecarStateTerminalError(RuntimeError):
    def __init__(self, status: str) -> None:
        super().__init__("A2A pipeline sidecar owner is already terminal")
        self.status = status


class _SidecarRestoreFailedError(RuntimeError):
    def __init__(self, status: str, reason: str | None) -> None:
        detail = reason or "unknown"
        super().__init__(f"A2A pipeline sidecar restore failed: status={status}, reason={detail}")
        self.status = status
        self.reason = reason


class IacCodeA2APipelineExecutor:
    def __init__(
        self,
        *,
        task_store: Any,
        model: str,
        metrics: Any,
        artifact_store: Any | None,
        push_notifier: Any | None,
        permission_resolver: Any | None,
        auto_approve_permissions: bool,
        thinking_exposure_types: Any,
    ) -> None:
        self._task_store = task_store
        self._model = model
        self._metrics = metrics
        self._artifact_store = artifact_store
        self._push_notifier = push_notifier
        self._permission_resolver = permission_resolver
        self._auto_approve_permissions = auto_approve_permissions
        self._thinking_exposure_types = thinking_exposure_types

    async def execute(
        self,
        *,
        context: Any,
        event_queue: Any,
        task: Any,
        task_id: str,
        context_id: str,
        cwd: str,
        prompt: str,
    ) -> None:
        session_storage = SessionStorage()

        def runtime_factory(session_id: str) -> Any:
            return create_agent_runtime(AgentFactoryOptions(model=self._model, session_id=session_id, cwd=cwd))

        try:
            ctx = await self._task_store.get_or_create_context(
                context_id=context_id,
                cwd=cwd,
                runtime_factory=runtime_factory,
            )
        except Exception as exc:
            await self._publish_exception_status(
                event_queue,
                task=task,
                task_id=task_id,
                context_id=context_id,
                exc=exc,
            )
            return

        if ctx.lock is None:
            ctx.lock = asyncio.Lock()

        if ctx.active_task_id is not None:
            preserve_active_task = _is_active_task_record(task, ctx.active_task_id)
            if _is_active_task_request(task, task_id, ctx.active_task_id):
                routed = await self._route_active_pipeline_interrupt(
                    event_queue,
                    task=task,
                    ctx=ctx,
                    task_id=task_id,
                    context_id=context_id,
                    cwd=cwd,
                    prompt=prompt,
                    preserve_task_record=preserve_active_task,
                )
                if routed:
                    return
            await self._fail_already_active(
                event_queue,
                task=task,
                task_id=task_id,
                context_id=context_id,
                preserve_task_record=preserve_active_task,
            )
            return

        lock = ctx.lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except TimeoutError:
            await self._fail_already_active(event_queue, task=task, task_id=task_id, context_id=context_id)
            return

        try:
            owner_task = asyncio.current_task()
            ctx.active_task_id = task.task_id
            task.active_task = owner_task
            task.state = TASK_STATE_WORKING
            self._task_store.mirror_task(task)
            self._task_store.mirror_context(ctx)

            pipeline = None
            publisher: PipelineA2AEventPublisher | None = None
            try:
                pipeline_runtime = self._pipeline_runtime_from_context(ctx.runtime, session_id=ctx.session_id, cwd=cwd)
                agent_runtime = pipeline_runtime.agent_runtime
                pipeline = self._create_pipeline(
                    session_id=ctx.session_id,
                    cwd=cwd,
                    runtime=agent_runtime,
                    session_storage=session_storage,
                )
                self._set_pipeline_telemetry_correlation(pipeline, task_id=task_id, context_id=context_id)
                publisher = self._publisher(
                    event_queue=event_queue,
                    pipeline=pipeline,
                    task_id=task_id,
                    context_id=context_id,
                    session_id=ctx.session_id,
                    cwd=cwd,
                )
                pipeline_runtime = A2APipelineRuntime(
                    agent_runtime=agent_runtime,
                    pipeline=pipeline,
                    publisher=publisher,
                )
                ctx.runtime = pipeline_runtime
                self._task_store.mirror_context(ctx)

                def fresh_pipeline_factory() -> Any:
                    fresh_pipeline = self._create_pipeline(
                        session_id=ctx.session_id,
                        cwd=cwd,
                        runtime=agent_runtime,
                        session_storage=session_storage,
                        resume_from_sidecar=False,
                    )
                    self._set_pipeline_telemetry_correlation(
                        fresh_pipeline,
                        task_id=task_id,
                        context_id=context_id,
                    )
                    return fresh_pipeline

                selected = self._select_stream(
                    pipeline,
                    prompt,
                    publisher=publisher,
                    task_id=task_id,
                    context_id=context_id,
                    fresh_pipeline_factory=fresh_pipeline_factory,
                )
                if selected.pipeline is not pipeline:
                    pipeline = selected.pipeline
                    pipeline_runtime.pipeline = pipeline
                    self._task_store.mirror_context(ctx)
                stream = selected.stream
                stream_had_events = False
                with use_session_id(ctx.session_id):
                    while True:
                        stream_result = await self._consume_stream_until_restart(
                            stream=stream,
                            runtime=pipeline_runtime,
                            publisher=publisher,
                            task=task,
                        )
                        stream_had_events = stream_had_events or stream_result.had_events

                        if not stream_result.restart_requested:
                            break

                        stream = self._continue_after_interrupt_stream(pipeline, prompt)

                terminal_status_published = False
                terminal_sidecar = _is_terminal_sidecar_status(getattr(pipeline, "sidecar_status", None))
                if terminal_sidecar and publisher is not None:
                    terminal_status_published = await self._publish_terminal_sidecar_recovery_event(
                        publisher,
                        pipeline,
                        task_id=task_id,
                        context_id=context_id,
                    )

                snapshot = publisher.snapshot_store.load() or {}
                task.state = _task_state_from_pipeline(pipeline, snapshot)
                self._task_store.mirror_task(task)
                if not stream_had_events and terminal_sidecar and not terminal_status_published:
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=_a2a_state_from_task_state(task.state),
                    )
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._record_state(task.state)
            except asyncio.CancelledError:
                task.state = TASK_STATE_CANCELED
                if pipeline is not None:
                    await self._mark_user_aborted(pipeline)
                await self._publish_pipeline_terminal_event(
                    publisher,
                    event_type="pipeline_canceled",
                    status="canceled",
                    data={"source": "executor", "reason": _("Task canceled.")},
                )
                if pipeline is not None and publisher is not None:
                    await self._publish_normal_handoff_ready(
                        pipeline,
                        publisher,
                        {"canceled": True, "reason": _("Task canceled.")},
                    )
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    text=_("Task canceled."),
                )
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_task_canceled()
            except _SidecarStateTerminalError as exc:
                task.state = _task_state_from_a2a_status(exc.status)
                self._task_store.mirror_task(task)
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=_a2a_state_from_task_state(task.state),
                )
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._record_state(task.state)
            except Exception as exc:
                await self._publish_exception_status(
                    event_queue,
                    task=task,
                    task_id=task_id,
                    context_id=context_id,
                    exc=exc,
                    pipeline_publisher=publisher,
                )
            finally:
                if task.active_task is owner_task:
                    task.active_task = None
                    if ctx.active_task_id == task.task_id:
                        ctx.active_task_id = None
                ctx.touch()
                task.touch()
                self._task_store.mirror_task(task)
                self._task_store.mirror_context(ctx)
                await _flush_telemetry_safely()
        finally:
            lock.release()

    def _pipeline_runtime_from_context(self, runtime: Any, *, session_id: str, cwd: str) -> A2APipelineRuntime:
        if isinstance(runtime, A2APipelineRuntime):
            return runtime
        if runtime is not None:
            return A2APipelineRuntime(agent_runtime=runtime)
        return A2APipelineRuntime(
            agent_runtime=create_agent_runtime(AgentFactoryOptions(model=self._model, session_id=session_id, cwd=cwd)),
        )

    async def _route_active_pipeline_interrupt(
        self,
        event_queue: Any,
        *,
        task: Any,
        ctx: Any,
        task_id: str,
        context_id: str,
        cwd: str,
        prompt: str,
        preserve_task_record: bool,
    ) -> bool:
        runtime = ctx.runtime
        pipeline = getattr(runtime, "pipeline", None)
        if pipeline is None:
            return False

        pending_question_route = await self._route_pending_question_answer(runtime, prompt)
        if pending_question_route == _PENDING_QUESTION_ANSWERED:
            task.state = TASK_STATE_WORKING
            self._task_store.mirror_task(task)
            return True
        if pending_question_route == _PENDING_QUESTION_STALE_FINISHED:
            task.state = TASK_STATE_INPUT_REQUIRED
            self._task_store.mirror_task(task)
            return True

        publisher = getattr(runtime, "publisher", None)
        publish_interrupt = getattr(publisher, "publish_interrupt", None)
        if not callable(publish_interrupt):
            try:
                publisher = self._publisher(
                    event_queue=event_queue,
                    pipeline=pipeline,
                    task_id=task_id,
                    context_id=context_id,
                    session_id=ctx.session_id,
                    cwd=cwd,
                )
            except Exception:
                logger.warning("A2A pipeline interrupt publisher creation failed", exc_info=True)
                return False
            if hasattr(runtime, "publisher"):
                runtime.publisher = publisher
                self._task_store.mirror_context(ctx)
        if publisher is None:
            return False

        if _pending_pipeline_pause_input_from_sidecar(publisher, task_id=task_id, context_id=context_id) is not None:
            await self._continue_active_pause_confirmation(
                event_queue,
                task=task,
                ctx=ctx,
                runtime=runtime,
                pipeline=pipeline,
                publisher=publisher,
                task_id=task_id,
                context_id=context_id,
                session_id=ctx.session_id,
                prompt=prompt,
                preserve_task_record=preserve_task_record,
            )
            return True

        handler = getattr(pipeline, "handle_user_interrupt", None)
        if not callable(handler):
            return False

        paused = False
        verdict: Any | None = None
        interrupt_received_published = False
        try:
            publish_interrupt_received = getattr(publisher, "publish_interrupt_received", None)
            if callable(publish_interrupt_received):
                await publish_interrupt_received(prompt=prompt)
                interrupt_received_published = True

            pause_agent_loops = getattr(pipeline, "pause_agent_loops", None)
            if callable(pause_agent_loops):
                await _maybe_await(pause_agent_loops())
                paused = True

            verdict = await _maybe_await(handler(prompt))
            parent_rollback: bool | None = None
            if getattr(verdict, "action", "") == "hard_interrupt":
                apply_hard_interrupt = getattr(pipeline, "apply_hard_interrupt", None)
                if callable(apply_hard_interrupt):
                    parent_rollback = bool(await _maybe_await(apply_hard_interrupt(verdict)))
                    if parent_rollback:
                        runtime.restart_after_interrupt = True
                        _restart_requested_event(runtime).set()

            await publisher.publish_interrupt(
                prompt=prompt,
                verdict=verdict,
                parent_rollback=parent_rollback,
                include_received=not interrupt_received_published,
            )
            if _is_terminal_sidecar_status(getattr(pipeline, "sidecar_status", None)):
                await self._publish_terminal_sidecar_recovery_event(
                    publisher,
                    pipeline,
                    task_id=task_id,
                    context_id=context_id,
                )
                snapshot = publisher.snapshot_store.load() or {}
                task.state = _task_state_from_pipeline(pipeline, snapshot)
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task_id, context_id=context_id, state=task.state)
                self._record_state(task.state)
                runtime.pause_after_interrupt = True
                _restart_requested_event(runtime).set()
                paused = False
                return True
            if bool(getattr(verdict, "paused", False)):
                pause_event = await _save_pipeline_interrupt_pause(pipeline, verdict)
                if pause_event is not None:
                    await publisher.publish(pause_event)
                task.state = TASK_STATE_INPUT_REQUIRED
                self._task_store.mirror_task(task)
                runtime.pause_after_interrupt = True
                _restart_requested_event(runtime).set()
            return True
        except Exception as exc:
            await self._publish_exception_status(
                event_queue,
                task=task,
                task_id=task_id,
                context_id=context_id,
                exc=exc,
                preserve_task_record=preserve_task_record,
            )
            return True
        finally:
            if paused and not bool(getattr(verdict, "paused", False)):
                resume_agent_loops = getattr(pipeline, "resume_agent_loops", None)
                if callable(resume_agent_loops):
                    try:
                        await _maybe_await(resume_agent_loops())
                    except Exception:
                        logger.warning("A2A pipeline interrupt resume failed", exc_info=True)

    async def _continue_active_pause_confirmation(
        self,
        event_queue: Any,
        *,
        task: Any,
        ctx: Any,
        runtime: A2APipelineRuntime,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        task_id: str,
        context_id: str,
        session_id: str,
        prompt: str,
        preserve_task_record: bool,
    ) -> None:
        owner_task = asyncio.current_task()
        task.active_task = owner_task
        ctx.active_task_id = task_id
        restart_event = _restart_requested_event(runtime)
        if runtime.pause_after_interrupt and restart_event.is_set():
            restart_event.clear()
            runtime.pause_after_interrupt = False
        ctx.touch()
        task.touch()
        self._task_store.mirror_task(task)
        self._task_store.mirror_context(ctx)
        try:
            stream = pipeline.continue_from_sidecar(user_input=prompt) if prompt else pipeline.continue_from_sidecar()
            task.state = TASK_STATE_WORKING
            self._task_store.mirror_task(task)
            with use_session_id(session_id):
                while True:
                    stream_result = await self._consume_stream_until_restart(
                        stream=stream,
                        runtime=runtime,
                        publisher=publisher,
                        task=task,
                    )
                    if not stream_result.restart_requested:
                        break
                    stream = self._continue_after_interrupt_stream(pipeline, prompt)

            snapshot = publisher.snapshot_store.load() or {}
            task.state = _task_state_from_pipeline(pipeline, snapshot)
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task_id, context_id=context_id, state=task.state)
            self._record_state(task.state)
        except Exception as exc:
            await self._publish_exception_status(
                event_queue,
                task=task,
                task_id=task_id,
                context_id=context_id,
                exc=exc,
                preserve_task_record=False,
                pipeline_publisher=publisher,
            )
        finally:
            if task.active_task is owner_task:
                task.active_task = None
                if ctx.active_task_id == task_id:
                    ctx.active_task_id = None
                ctx.touch()
                task.touch()
                self._task_store.mirror_task(task)
                self._task_store.mirror_context(ctx)

    def _create_pipeline(
        self,
        *,
        session_id: str,
        cwd: str,
        runtime: Any,
        session_storage: SessionStorage,
        resume_from_sidecar: bool = True,
    ) -> Any:
        return create_pipeline(
            get_pipeline_name(),
            provider_manager=runtime.provider_manager,
            base_tool_registry=runtime.tool_registry,
            session_storage=session_storage,
            session_id=session_id,
            cwd=cwd,
            resume_from_sidecar=resume_from_sidecar,
        )

    def _set_pipeline_telemetry_correlation(self, pipeline: Any, *, task_id: str, context_id: str) -> None:
        set_correlation = getattr(pipeline, "set_telemetry_correlation", None)
        if not callable(set_correlation):
            return
        try:
            set_correlation(task_id=task_id, context_id=context_id, pipeline_run_id=context_id)
        except Exception:
            logger.warning("A2A pipeline telemetry correlation setup failed", exc_info=True)

    def _continue_after_interrupt_stream(self, pipeline: Any, prompt: str) -> AsyncIterator[Any]:
        continue_after_interrupt = getattr(pipeline, "continue_after_interrupt", None)
        if callable(continue_after_interrupt):
            return continue_after_interrupt()
        return pipeline.run(prompt)

    async def _consume_stream_until_restart(
        self,
        *,
        stream: AsyncIterator[Any],
        runtime: A2APipelineRuntime,
        publisher: PipelineA2AEventPublisher,
        task: Any,
    ) -> "_StreamConsumeResult":
        had_events = False
        stream_iter = stream.__aiter__()
        runtime.current_stream = stream_iter
        restart_event = _restart_requested_event(runtime)
        next_task: asyncio.Task[Any] | None = None
        restart_task: asyncio.Task[Any] | None = None
        close_stream_on_exit = False
        try:
            while True:
                if runtime.pause_after_interrupt and restart_event.is_set():
                    restart_event.clear()
                    runtime.pause_after_interrupt = False
                    await _close_stream_safely(stream_iter)
                    return _StreamConsumeResult(had_events=had_events, restart_requested=False)
                if runtime.restart_after_interrupt and restart_event.is_set():
                    restart_event.clear()
                    runtime.restart_after_interrupt = False
                    await _close_stream_safely(stream_iter)
                    return _StreamConsumeResult(had_events=had_events, restart_requested=True)

                next_task = asyncio.create_task(_next_stream_event(stream_iter))
                restart_task = asyncio.create_task(restart_event.wait())
                done, _pending = await asyncio.wait(
                    {next_task, restart_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if restart_task in done and runtime.restart_after_interrupt:
                    restart_event.clear()
                    runtime.restart_after_interrupt = False
                    await _cancel_task_safely(next_task)
                    next_task = None
                    await _cancel_task_safely(restart_task)
                    restart_task = None
                    await _close_stream_safely(stream_iter)
                    return _StreamConsumeResult(had_events=had_events, restart_requested=True)
                if restart_task in done and runtime.pause_after_interrupt:
                    restart_event.clear()
                    runtime.pause_after_interrupt = False
                    await _cancel_task_safely(next_task)
                    next_task = None
                    await _cancel_task_safely(restart_task)
                    restart_task = None
                    await _close_stream_safely(stream_iter)
                    return _StreamConsumeResult(had_events=had_events, restart_requested=False)

                await _cancel_task_safely(restart_task)
                restart_task = None
                try:
                    event = await next_task
                except StopAsyncIteration:
                    next_task = None
                    return _StreamConsumeResult(had_events=had_events, restart_requested=False)
                finally:
                    next_task = None

                had_events = True
                text = await publisher.publish(
                    event,
                    permission_resolver=self._permission_resolver,
                    auto_approve_permissions=self._auto_approve_permissions,
                )
                self._track_pending_question(runtime, publisher, event)
                if text:
                    task.output_text.append(text)
                await self._maybe_publish_normal_handoff_ready(runtime.pipeline, publisher, event)
                if _ask_user_question_from(event) is not None:
                    close_stream_on_exit = True
                    return _StreamConsumeResult(had_events=had_events, restart_requested=False)
        except asyncio.CancelledError:
            close_stream_on_exit = True
            raise
        finally:
            if next_task is not None:
                await _cancel_task_safely(next_task)
            if restart_task is not None:
                await _cancel_task_safely(restart_task)
            if close_stream_on_exit:
                await _close_stream_safely(stream_iter)
            if runtime.current_stream is stream_iter:
                runtime.current_stream = None

    def _publisher(
        self,
        *,
        event_queue: Any,
        pipeline: Any,
        task_id: str,
        context_id: str,
        session_id: str,
        cwd: str,
    ) -> PipelineA2AEventPublisher:
        pipeline_dir = _pipeline_sidecar_dir(pipeline, cwd, session_id)
        context = PipelineA2AContext(
            pipeline_run_id=context_id,
            task_id=task_id,
            context_id=context_id,
            pipeline_name=getattr(pipeline, "pipeline_name", get_pipeline_name()),
            parent_step_order=_pipeline_parent_step_order(pipeline),
            candidate_step_order=_pipeline_candidate_step_order(pipeline),
            emit_stack_events=bool(getattr(pipeline, "emit_stack_events", False)),
            a2a_artifacts_by_step_id=_pipeline_a2a_artifacts_by_step_id(pipeline),
        )
        journal = A2APipelineJournal(pipeline_dir)
        translator = PipelineEventTranslator(context)
        try:
            translator.hydrate_from_events(journal.read_all_repairing_tail())
        except Exception:
            logger.warning("Failed to hydrate A2A pipeline translator from journal", exc_info=True)
        return PipelineA2AEventPublisher(
            event_queue=event_queue,
            translator=translator,
            journal=journal,
            snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
            artifact_store=self._artifact_store,
            exposure_types=self._thinking_exposure_types,
        )

    def _select_stream(
        self,
        pipeline: Any,
        prompt: str,
        *,
        publisher: PipelineA2AEventPublisher,
        task_id: str,
        context_id: str,
        fresh_pipeline_factory: Callable[[], Any],
    ) -> _SelectedPipelineStream:
        status = getattr(pipeline, "sidecar_status", None)
        if status == "waiting_input":
            _raise_if_sidecar_restore_failed(pipeline, status)
            if not _sidecar_matches_task(publisher, task_id=task_id, context_id=context_id, sidecar_status=status):
                pipeline = self._fresh_pipeline_after_sidecar_mismatch(pipeline, fresh_pipeline_factory)
                return _SelectedPipelineStream(pipeline=pipeline, stream=pipeline.run(prompt))
            pending_ask = _pending_ask_input_from_sidecar(
                publisher,
                task_id=task_id,
                context_id=context_id,
            )
            if pending_ask is not None:
                return _SelectedPipelineStream(
                    pipeline=pipeline,
                    stream=_resume_pending_ask_user_question_stream(
                        pipeline=pipeline,
                        publisher=publisher,
                        pending_input=pending_ask,
                        prompt=prompt,
                    ),
                )
            pending_pause = _pending_pipeline_pause_input_from_sidecar(
                publisher,
                task_id=task_id,
                context_id=context_id,
            )
            if pending_pause is not None:
                stream = (
                    pipeline.continue_from_sidecar(user_input=prompt) if prompt else pipeline.continue_from_sidecar()
                )
                return _SelectedPipelineStream(pipeline=pipeline, stream=stream)
            return _SelectedPipelineStream(pipeline=pipeline, stream=pipeline.resume(prompt))
        if status == "running":
            _raise_if_sidecar_restore_failed(pipeline, status)
            if not _sidecar_matches_task(publisher, task_id=task_id, context_id=context_id, sidecar_status=status):
                pipeline = self._fresh_pipeline_after_sidecar_mismatch(pipeline, fresh_pipeline_factory)
                return _SelectedPipelineStream(pipeline=pipeline, stream=pipeline.run(prompt))
            pending_ask = _pending_ask_input_from_sidecar(
                publisher,
                task_id=task_id,
                context_id=context_id,
            )
            if pending_ask is not None:
                return _SelectedPipelineStream(
                    pipeline=pipeline,
                    stream=_resume_pending_ask_user_question_stream(
                        pipeline=pipeline,
                        publisher=publisher,
                        pending_input=pending_ask,
                        prompt=prompt,
                    ),
                )
            pending_pause = _pending_pipeline_pause_input_from_sidecar(
                publisher,
                task_id=task_id,
                context_id=context_id,
            )
            if pending_pause is not None:
                stream = (
                    pipeline.continue_from_sidecar(user_input=prompt) if prompt else pipeline.continue_from_sidecar()
                )
                return _SelectedPipelineStream(pipeline=pipeline, stream=stream)
            if prompt:
                return _SelectedPipelineStream(
                    pipeline=pipeline, stream=pipeline.continue_from_sidecar(user_input=prompt)
                )
            return _SelectedPipelineStream(pipeline=pipeline, stream=pipeline.continue_from_sidecar())
        if status in _TERMINAL_SIDECAR_STATUSES:
            if _terminal_sidecar_matches_task(publisher, status, task_id=task_id, context_id=context_id):
                return _SelectedPipelineStream(pipeline=pipeline, stream=_empty_stream())
            pipeline = self._fresh_pipeline_after_sidecar_mismatch(pipeline, fresh_pipeline_factory)
            return _SelectedPipelineStream(pipeline=pipeline, stream=pipeline.run(prompt))
        return _SelectedPipelineStream(pipeline=pipeline, stream=pipeline.run(prompt))

    def _fresh_pipeline_after_sidecar_mismatch(
        self,
        pipeline: Any,
        fresh_pipeline_factory: Callable[[], Any],
    ) -> Any:
        self._clear_terminal_sidecar(pipeline)
        return fresh_pipeline_factory()

    def _clear_terminal_sidecar(self, pipeline: Any) -> None:
        clear_sidecar = getattr(pipeline, "clear_sidecar", None)
        if not callable(clear_sidecar):
            return
        try:
            clear_sidecar()
        except Exception:
            logger.warning("Pipeline terminal sidecar cleanup failed", exc_info=True)

    async def _publish_terminal_sidecar_recovery_event(
        self,
        publisher: PipelineA2AEventPublisher,
        pipeline: Any,
        *,
        task_id: str,
        context_id: str,
    ) -> bool:
        sidecar_status = getattr(pipeline, "sidecar_status", None)
        terminal_event = _terminal_event_from_sidecar_status(sidecar_status)
        if terminal_event is None:
            return False
        event_type, status = terminal_event
        snapshot = publisher.snapshot_store.load()
        journal_events = _safe_read_pipeline_journal(publisher.journal)
        scoped_journal_events = _events_for_task_context(journal_events, task_id=task_id, context_id=context_id)
        existing_terminal_event = _latest_terminal_a2a_event(scoped_journal_events)
        if existing_terminal_event is not None:
            existing_status = _terminal_status_from_a2a_event(existing_terminal_event)
            if existing_status != status:
                self._rebuild_terminal_recovery_snapshot(publisher, scoped_journal_events)
                return False
            if _terminal_snapshot_needs_journal_rebuild(
                snapshot,
                scoped_journal_events,
                status,
                task_id=task_id,
                context_id=context_id,
            ):
                self._rebuild_terminal_recovery_snapshot(publisher, scoped_journal_events)
            return False
        if _snapshot_has_conflicting_terminal_status(snapshot, status, task_id=task_id, context_id=context_id):
            return False
        if not _terminal_snapshot_needs_recovery_event(snapshot, status, task_id=task_id, context_id=context_id):
            return False

        published = await publisher.publish_manual(
            event_type,
            "pipeline",
            status=status,
            data={
                "sidecarStatus": sidecar_status,
                "recovered": True,
            },
        )
        return published is not None

    def _rebuild_terminal_recovery_snapshot(
        self,
        publisher: PipelineA2AEventPublisher,
        journal_events: list[dict[str, Any]],
    ) -> None:
        try:
            snapshot = reduce_pipeline_events(journal_events)
            publisher.snapshot_store.save(snapshot)
        except Exception:
            logger.warning("Failed to rebuild A2A pipeline terminal recovery snapshot", exc_info=True)

    async def _publish_pipeline_terminal_event(
        self,
        publisher: PipelineA2AEventPublisher | None,
        *,
        event_type: str,
        status: str,
        data: dict[str, Any],
    ) -> bool:
        if publisher is None:
            return False
        try:
            return await publisher.publish_manual(event_type, "pipeline", status=status, data=data) is not None
        except Exception:
            logger.warning("Failed to publish A2A pipeline terminal event", exc_info=True)
            return False

    async def _maybe_publish_normal_handoff_ready(
        self,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        event: Any,
    ) -> None:
        if not isinstance(event, PipelineEvent) or event.type != PipelineEventType.PIPELINE_COMPLETED:
            return

        await self._publish_normal_handoff_ready(pipeline, publisher, event.data or {})

    async def _publish_normal_handoff_ready(
        self,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        event_data: dict[str, Any],
    ) -> None:
        should_switch_to_normal = getattr(pipeline, "should_switch_to_normal", None)
        if not callable(should_switch_to_normal):
            return
        try:
            if not bool(should_switch_to_normal(event_data)):
                return
            summary = pipeline.build_normal_handoff_summary(event_data)
            outcome = terminal_outcome_from_completed_event(event_data)
        except Exception:
            logger.warning("Failed to build A2A pipeline normal handoff event", exc_info=True)
            return

        published = await publisher.publish_manual(
            "pipeline_handoff_ready",
            "pipeline",
            status=_handoff_status_from_outcome(outcome),
            data={
                "action": "switch_to_normal",
                "targetMode": "normal",
                "outcome": outcome,
                "summary": summary,
            },
        )
        if published is not None:
            _persist_normal_handoff_summary(pipeline, summary)

    def _track_pending_question(
        self,
        runtime: A2APipelineRuntime,
        publisher: PipelineA2AEventPublisher,
        event: Any,
    ) -> None:
        question = _ask_user_question_from(event)
        if question is None:
            return
        envelope = publisher.last_envelope
        if not isinstance(envelope, dict) or envelope.get("eventType") != "input_required":
            return
        if question.response_future is None or question.response_future.done():
            return
        runtime.pending_question = _PendingAskUserQuestion(event=question, envelope=dict(envelope))

    async def _route_pending_question_answer(self, runtime: Any, prompt: str) -> str:
        pending = getattr(runtime, "pending_question", None)
        if not isinstance(pending, _PendingAskUserQuestion):
            return _PENDING_QUESTION_NOT_ROUTED

        question = pending.event
        future = question.response_future
        if future is None or future.done():
            runtime.pending_question = None
            return _PENDING_QUESTION_STALE_FINISHED

        publisher = getattr(runtime, "publisher", None)
        if not isinstance(publisher, PipelineA2AEventPublisher):
            return _PENDING_QUESTION_NOT_ROUTED

        answer = _ask_user_question_answer_from_prompt(question, prompt)
        published = await publisher.publish_manual(
            "input_received",
            str(pending.envelope.get("scope") or "pipeline"),
            status="working",
            data={
                "kind": "ask_user_question",
                "inputId": _pending_input_id(pending.envelope, question),
                "toolUseId": question.tool_use_id,
                "answerTextLength": len(prompt),
                "selectedId": answer["selected_id"],
                "selectedLabel": answer["selected_label"],
                "freeTextLength": len(answer["free_text"]),
            },
            coordinates=_coordinates_from_envelope(pending.envelope),
        )
        if published is None:
            return _PENDING_QUESTION_NOT_ROUTED

        future.set_result(answer)
        runtime.pending_question = None
        return _PENDING_QUESTION_ANSWERED

    async def _fail_already_active(
        self,
        event_queue: Any,
        *,
        task: Any,
        task_id: str,
        context_id: str,
        preserve_task_record: bool = False,
    ) -> None:
        if not preserve_task_record:
            task.state = TASK_STATE_FAILED
            self._task_store.mirror_task(task)
        await self._publish_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_FAILED,
            text=_("Task is already working."),
        )
        if not preserve_task_record:
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()

    async def _publish_exception_status(
        self,
        event_queue: Any,
        *,
        task: Any,
        task_id: str,
        context_id: str,
        exc: Exception,
        preserve_task_record: bool = False,
        pipeline_publisher: PipelineA2AEventPublisher | None = None,
    ) -> None:
        retryable = _is_retryable_executor_error(exc)
        task_state = TASK_STATE_INPUT_REQUIRED if retryable else TASK_STATE_FAILED
        text = _retry_text() if retryable else _sanitize_error(exc)
        failure = None if retryable else public_error(message=text, error_type=type(exc).__name__)
        if not retryable and not preserve_task_record:
            await self._publish_pipeline_terminal_event(
                pipeline_publisher,
                event_type="pipeline_failed",
                status="failed",
                data={
                    "source": "executor",
                    "errorSummary": text,
                    "errorDetails": _public_error_details_for_a2a(failure.details) if failure is not None else {},
                },
            )
        await self._publish_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_INPUT_REQUIRED if retryable else TaskState.TASK_STATE_FAILED,
            text=text,
        )
        if not preserve_task_record:
            task.state = task_state
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
        self._metrics.record_executor_error()
        if not retryable and not preserve_task_record:
            self._metrics.record_task_failed()

    async def _publish_status(
        self,
        event_queue: Any,
        *,
        task_id: str,
        context_id: str,
        state: int,
        text: str | None = None,
    ) -> None:
        message = None
        if text:
            message = Message(
                message_id=f"{task_id}-{state}",
                task_id=task_id,
                context_id=context_id,
                role=Role.ROLE_AGENT,
                parts=[make_text_part(text)],
            )
        status = TaskStatus(state=TaskState.Name(state), message=message)
        status.timestamp.GetCurrentTime()
        await event_queue.enqueue_event(TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status))

    async def _notify_terminal_task(self, *, task_id: str, context_id: str, state: str) -> None:
        if self._push_notifier is None:
            return
        try:
            await self._push_notifier.notify_task_state(task_id=task_id, context_id=context_id, state=state)
        except Exception:
            logger.warning("A2A push notification failed", exc_info=True)

    def _record_state(self, state: str) -> None:
        if state == TASK_STATE_FAILED:
            self._metrics.record_task_failed()
        elif state == TASK_STATE_CANCELED:
            self._metrics.record_task_canceled()
        else:
            self._metrics.record_turn_completed()

    async def _mark_user_aborted(self, pipeline: Any) -> None:
        mark_user_aborted = getattr(pipeline, "mark_user_aborted", None)
        if not callable(mark_user_aborted):
            return
        try:
            result = mark_user_aborted("A2A task canceled")
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("Pipeline mark_user_aborted failed", exc_info=True)


async def _empty_stream() -> AsyncIterator[Any]:
    if False:
        yield None


async def _resume_pending_ask_user_question_stream(
    *,
    pipeline: Any,
    publisher: PipelineA2AEventPublisher,
    pending_input: dict[str, Any],
    prompt: str,
) -> AsyncIterator[Any]:
    resume_ask_user_question = getattr(pipeline, "resume_ask_user_question", None)
    if not callable(resume_ask_user_question):
        raise RuntimeError("Pipeline cannot resume pending ask_user_question input.")

    answer = _ask_user_question_answer_from_pending_input(pending_input, prompt)
    tool_use_id = _string_value(pending_input.get("toolUseId") or pending_input.get("tool_use_id"))
    if not tool_use_id:
        raise RuntimeError("Pending ask_user_question input is missing toolUseId.")

    published = await publisher.publish_manual(
        "input_received",
        _string_value(pending_input.get("scope")) or "pipeline",
        status="working",
        data={
            "kind": "ask_user_question",
            "inputId": _string_value(pending_input.get("inputId") or pending_input.get("input_id"))
            or f"ask-{tool_use_id}",
            "toolUseId": tool_use_id,
            "answerTextLength": len(prompt),
            "selectedId": answer["selected_id"],
            "selectedLabel": answer["selected_label"],
            "freeTextLength": len(answer["free_text"]),
        },
        coordinates=_coordinates_from_pending_input(pending_input),
    )
    if published is None:
        raise RuntimeError("Failed to persist pending ask_user_question answer.")

    parameters = inspect.signature(resume_ask_user_question).parameters
    resume_kwargs: dict[str, Any] = {"tool_use_id": tool_use_id}
    if "pending_input" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    ):
        resume_kwargs["pending_input"] = pending_input

    async for event in resume_ask_user_question(answer, **resume_kwargs):
        yield event


def _ask_user_question_from(event: Any) -> AskUserQuestionEvent | None:
    inner = event.inner if isinstance(event, SubPipelineStreamEvent) else event
    return inner if isinstance(inner, AskUserQuestionEvent) else None


def _ask_user_question_answer_from_prompt(event: AskUserQuestionEvent, prompt: str) -> dict[str, str]:
    return _ask_user_question_answer_from_options(
        event.options,
        prompt,
        allow_free_text=event.allow_free_text,
    )


def _ask_user_question_answer_from_pending_input(pending_input: dict[str, Any], prompt: str) -> dict[str, str]:
    options = pending_input.get("options")
    allow_free_text = pending_input.get("allowFreeText")
    if not isinstance(allow_free_text, bool):
        allow_free_text = pending_input.get("allow_free_text")
    return _ask_user_question_answer_from_options(
        options if isinstance(options, list) else [],
        prompt,
        allow_free_text=True if not isinstance(allow_free_text, bool) else allow_free_text,
    )


def _ask_user_question_answer_from_options(
    options: list[Any],
    prompt: str,
    *,
    allow_free_text: bool,
) -> dict[str, str]:
    prompt_text = prompt.strip()
    option_index = _one_based_option_index(prompt_text, len(options))
    if option_index is not None:
        option = options[option_index]
        if isinstance(option, dict):
            return {
                "selected_id": _string_value(option.get("id")),
                "selected_label": _string_value(option.get("label")),
                "free_text": "",
            }

    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = _string_value(option.get("id"))
        option_label = _string_value(option.get("label"))
        if prompt_text and prompt_text in {option_id, option_label}:
            return {
                "selected_id": option_id,
                "selected_label": option_label,
                "free_text": "",
            }

    if allow_free_text:
        return {
            "selected_id": "",
            "selected_label": "",
            "free_text": prompt,
        }

    return {
        "selected_id": "",
        "selected_label": prompt,
        "free_text": "",
    }


def _pending_ask_input_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    task_id: str,
    context_id: str,
) -> dict[str, Any] | None:
    pending_input = _pending_input_from_snapshot(snapshot, task_id=task_id, context_id=context_id)
    if pending_input is None:
        return None
    kind = pending_input.get("kind")
    if kind != "ask_user_question":
        return None
    return pending_input


def _pending_input_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    task_id: str,
    context_id: str,
) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("taskId") not in (None, task_id) or snapshot.get("contextId") not in (None, context_id):
        return None
    pending_input = snapshot.get("pendingInput")
    if not isinstance(pending_input, dict):
        return None
    return pending_input


def _pending_ask_input_from_sidecar(
    publisher: PipelineA2AEventPublisher,
    *,
    task_id: str,
    context_id: str,
) -> dict[str, Any] | None:
    return _pending_ask_input_from_snapshot(
        _authoritative_snapshot_for_task(
            snapshot_store=publisher.snapshot_store,
            journal=publisher.journal,
            task_id=task_id,
            context_id=context_id,
        ),
        task_id=task_id,
        context_id=context_id,
    )


def _pending_pipeline_pause_input_from_sidecar(
    publisher: PipelineA2AEventPublisher,
    *,
    task_id: str,
    context_id: str,
) -> dict[str, Any] | None:
    pending_input = _pending_input_from_snapshot(
        _authoritative_snapshot_for_task(
            snapshot_store=publisher.snapshot_store,
            journal=publisher.journal,
            task_id=task_id,
            context_id=context_id,
        ),
        task_id=task_id,
        context_id=context_id,
    )
    if pending_input is None:
        return None
    return pending_input if pending_input.get("kind") == "pipeline_pause_confirmation" else None


def waiting_input_task_id_from_sidecar(*, cwd: str, session_id: str, context_id: str) -> str | None:
    return recoverable_task_id_from_sidecar(
        cwd=cwd,
        session_id=session_id,
        context_id=context_id,
        include_running=False,
    )


def cancel_waiting_input_task_from_sidecar(
    *,
    cwd: str,
    session_id: str,
    context_id: str,
    task_id: str,
    reason: str | None = None,
) -> bool:
    if reason is None:
        reason = _("Task canceled.")
    if waiting_input_task_id_from_sidecar(cwd=cwd, session_id=session_id, context_id=context_id) != task_id:
        return False

    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    try:
        events = journal.read_all_repairing_tail()
    except Exception:
        logger.warning("Failed to cancel waiting A2A pipeline sidecar", exc_info=True)
        return False

    snapshot = snapshot_store.load()
    pipeline_name = get_pipeline_name()
    if isinstance(snapshot, dict) and isinstance(snapshot.get("pipelineName"), str):
        pipeline_name = snapshot["pipelineName"]
    context = PipelineA2AContext(
        pipeline_run_id=context_id,
        task_id=task_id,
        context_id=context_id,
        pipeline_name=pipeline_name,
    )
    translator = PipelineEventTranslator(context)
    translator.hydrate_from_events(events)
    envelope = translator.manual_event(
        "pipeline_canceled",
        "pipeline",
        status="canceled",
        data={"source": "a2a_cancel", "reason": reason},
    )
    high_water_sequence = max(
        [int(event.get("sequence") or 0) for event in events if isinstance(event, dict)]
        + ([int(snapshot.get("lastSequence") or 0)] if isinstance(snapshot, dict) else [0])
    )
    if int(envelope.get("sequence") or 0) <= high_water_sequence:
        envelope["sequence"] = high_water_sequence + 1
    handoff_envelope = _waiting_input_cancel_handoff_event(
        translator,
        snapshot=snapshot,
        cwd=cwd,
        session_id=session_id,
        pipeline_name=pipeline_name,
        reason=reason,
    )
    if handoff_envelope is not None and int(handoff_envelope.get("sequence") or 0) <= int(
        envelope.get("sequence") or 0
    ):
        handoff_envelope["sequence"] = int(envelope.get("sequence") or 0) + 1
    try:
        journal.append(envelope)
        if handoff_envelope is not None:
            journal.append(handoff_envelope)
        snapshot_store.save(reduce_pipeline_events(journal.read_all_repairing_tail()))
    except Exception:
        logger.warning("Failed to persist waiting A2A pipeline cancellation", exc_info=True)
        return False
    return True


def _waiting_input_cancel_handoff_event(
    translator: PipelineEventTranslator,
    *,
    snapshot: dict[str, Any] | None,
    cwd: str,
    session_id: str,
    pipeline_name: str,
    reason: str,
) -> dict[str, Any] | None:
    loaded_pipeline = _load_pipeline_definition_for_handoff(pipeline_name)
    if loaded_pipeline is None:
        return None
    policy = getattr(loaded_pipeline, "on_complete", None)
    if policy is None or policy.action != "switch_to_normal" or "canceled" not in policy.apply_on:
        return None

    include_fields = getattr(policy.handoff_context, "include", [])
    context_snapshot = _flat_pipeline_context_from_sidecar(cwd=cwd, session_id=session_id)
    if not context_snapshot:
        context_snapshot = _flat_pipeline_context_from_a2a_snapshot(snapshot, loaded_pipeline)
    summary = build_handoff_summary(
        pipeline_name=pipeline_name,
        outcome="canceled",
        context_snapshot=context_snapshot,
        include_fields=include_fields,
    )
    return translator.manual_event(
        "pipeline_handoff_ready",
        "pipeline",
        status="canceled",
        data={
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": summary,
            "reason": reason,
        },
    )


def _load_pipeline_definition_for_handoff(pipeline_name: str) -> Any | None:
    try:
        pipeline_dir = discover_pipelines().get(pipeline_name)
        if pipeline_dir is None:
            return None
        return load_pipeline_dir(pipeline_dir)
    except Exception:
        logger.warning("Failed to load A2A pipeline handoff policy for %s", pipeline_name, exc_info=True)
        return None


def _flat_pipeline_context_from_sidecar(*, cwd: str, session_id: str) -> dict[str, Any]:
    try:
        restored = PipelineSession(SessionStorage().session_dir(cwd, session_id) / "pipeline").restore_sync()
    except Exception:
        logger.warning("Failed to load pipeline context for A2A cancel handoff", exc_info=True)
        return {}
    if not isinstance(restored, dict):
        return {}
    context_snapshot = restored.get("context_snapshot")
    if not isinstance(context_snapshot, dict):
        return {}
    return _flatten_pipeline_context_snapshot(context_snapshot)


def _flat_pipeline_context_from_a2a_snapshot(snapshot: dict[str, Any] | None, loaded_pipeline: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    field_by_step_id = {
        str(getattr(step, "step_id")): str(getattr(step, "conclusion_field"))
        for step in getattr(loaded_pipeline, "steps", [])
        if getattr(step, "step_id", None) and getattr(step, "conclusion_field", None)
    }
    context: dict[str, Any] = {}
    for step in snapshot.get("steps", []) if isinstance(snapshot.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        field_name = field_by_step_id.get(str(step.get("id") or ""))
        if not field_name:
            continue
        conclusion = step.get("conclusion")
        if conclusion is not None:
            context[field_name] = conclusion
    return context


def _flatten_pipeline_context_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for field_name, field_value in snapshot.items():
        if isinstance(field_value, dict) and "value" in field_value:
            value = field_value.get("value")
            if value is not None:
                flattened[field_name] = value
    return flattened


def terminal_task_state_from_sidecar(*, cwd: str, session_id: str, context_id: str, task_id: str) -> str | None:
    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    try:
        owner = _current_sidecar_owner_from_stores(
            snapshot_store=snapshot_store,
            journal=journal,
            context_id=context_id,
        )
    except _SidecarOwnerUnavailableError:
        return None
    if owner is None or owner.task_id != task_id:
        return None
    status = _normalized_a2a_status(owner.status)
    if status not in _TERMINAL_A2A_STATUSES:
        return None
    return _task_state_from_sidecar_status(status)


def recoverable_task_id_from_sidecar(
    *,
    cwd: str,
    session_id: str,
    context_id: str,
    include_running: bool = True,
) -> str | None:
    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    owner = _current_sidecar_owner_from_stores(
        snapshot_store=snapshot_store,
        journal=journal,
        context_id=context_id,
    )
    if owner is None:
        return None
    status = _normalized_a2a_status(owner.status)
    if status in _TERMINAL_A2A_STATUSES:
        return None
    if include_running and status in _RUNNING_A2A_STATUSES:
        return owner.task_id
    if status not in _WAITING_A2A_STATUSES:
        return None
    pending_input = _pending_input_from_snapshot(
        _authoritative_snapshot_for_task(
            snapshot_store=snapshot_store,
            journal=journal,
            task_id=owner.task_id,
            context_id=context_id,
        ),
        task_id=owner.task_id,
        context_id=context_id,
    )
    return owner.task_id if pending_input is not None else None


def _coordinates_from_pending_input(pending_input: dict[str, Any]) -> dict[str, Any]:
    return {
        key: dict(value)
        for key in ("step", "candidate", "candidateStep")
        if isinstance((value := pending_input.get(key)), dict)
    }


def _one_based_option_index(value: str, option_count: int) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    index = parsed - 1
    return index if 0 <= index < option_count else None


def _coordinates_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        key: dict(value)
        for key in ("step", "candidate", "candidateStep")
        if isinstance((value := envelope.get(key)), dict)
    }


def _pending_input_id(envelope: dict[str, Any], event: AskUserQuestionEvent) -> str:
    input_value = envelope.get("input")
    if isinstance(input_value, dict):
        input_id = _string_value(input_value.get("inputId"))
        if input_id:
            return input_id
    return f"ask-{event.tool_use_id or 'unknown'}"


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _persist_normal_handoff_summary(pipeline: Any, summary: str) -> None:
    session_storage = getattr(pipeline, "_session_storage", None)
    cwd = getattr(pipeline, "_cwd", None)
    session_id = getattr(pipeline, "_session_id", None)
    append = getattr(session_storage, "append", None)
    if not callable(append) or not isinstance(cwd, str) or not isinstance(session_id, str):
        return
    try:
        append(cwd, session_id, AgentMessage(role="user", content=summary))
    except Exception:
        logger.warning("Failed to persist A2A pipeline normal handoff summary", exc_info=True)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _save_pipeline_interrupt_pause(pipeline: Any, verdict: Any) -> PipelineEvent | None:
    save_interrupt_pause = getattr(pipeline, "save_interrupt_pause", None)
    if not callable(save_interrupt_pause):
        return None
    event = await _maybe_await(save_interrupt_pause(verdict))
    return event if isinstance(event, PipelineEvent) else None


async def _next_stream_event(stream: AsyncIterator[Any]) -> Any:
    return await anext(stream)


async def _cancel_task_safely(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    except Exception:
        logger.warning("A2A pipeline stream task cleanup failed", exc_info=True)


async def _close_stream_safely(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if not callable(aclose):
        return
    try:
        await _maybe_await(aclose())
    except Exception:
        logger.warning("A2A pipeline interrupt stream close failed", exc_info=True)


def _restart_requested_event(runtime: Any) -> asyncio.Event:
    restart_requested = getattr(runtime, "restart_requested", None)
    if isinstance(restart_requested, asyncio.Event):
        return restart_requested
    restart_requested = asyncio.Event()
    runtime.restart_requested = restart_requested
    return restart_requested


def _is_active_task_record(task: Any, active_task_id: str | None) -> bool:
    return active_task_id is not None and getattr(task, "task_id", None) == active_task_id


def _is_active_task_request(task: Any, task_id: str, active_task_id: str | None) -> bool:
    return _is_active_task_record(task, active_task_id) and task_id == active_task_id


def _pipeline_sidecar_dir(pipeline: Any, cwd: str, session_id: str) -> Path:
    session = getattr(pipeline, "session", None)
    session_dir = getattr(session, "session_dir", None)
    if isinstance(session_dir, (str, Path)):
        pipeline_dir = a2a_pipeline_dir_for_sidecar_dir(session_dir)
        if pipeline_dir == a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id):
            return existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
        return pipeline_dir
    return existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)


def _pipeline_parent_step_order(pipeline: Any) -> list[str]:
    loaded = getattr(pipeline, "_loaded", None)
    return _step_ids(getattr(loaded, "steps", []))


def _pipeline_candidate_step_order(pipeline: Any) -> list[str]:
    loaded = getattr(pipeline, "_loaded", None)
    if loaded is None:
        return []
    sub_pipelines = getattr(loaded, "sub_pipelines", {}) or {}
    for step in getattr(loaded, "steps", []):
        if getattr(step, "step_type", None) != "parallel_sub_pipeline":
            continue
        sub_pipeline_name = getattr(step, "sub_pipeline_name", None)
        sub_pipeline = sub_pipelines.get(sub_pipeline_name)
        step_order = _step_ids(getattr(sub_pipeline, "steps", []))
        if step_order:
            return step_order
    if len(sub_pipelines) == 1:
        sub_pipeline = next(iter(sub_pipelines.values()))
        return _step_ids(getattr(sub_pipeline, "steps", []))
    return []


def _pipeline_a2a_artifacts_by_step_id(pipeline: Any) -> dict[str, list[Any]]:
    loaded = getattr(pipeline, "_loaded", None)
    if loaded is None:
        return {}

    artifacts_by_step_id: dict[str, list[Any]] = {}
    for step in getattr(loaded, "steps", []) or []:
        step_id = _string_attr(step, "step_id")
        artifacts = getattr(step, "a2a_artifacts", None)
        if step_id is not None and artifacts:
            artifacts_by_step_id[step_id] = list(artifacts)

    for sub_pipeline in (getattr(loaded, "sub_pipelines", {}) or {}).values():
        for step in getattr(sub_pipeline, "steps", []) or []:
            step_id = _string_attr(step, "step_id")
            artifacts = getattr(step, "a2a_artifacts", None)
            if step_id is not None and artifacts:
                artifacts_by_step_id[step_id] = list(artifacts)

    return artifacts_by_step_id


def _step_ids(steps: Any) -> list[str]:
    return [step_id for step_id in (_string_attr(step, "step_id") for step in steps or []) if step_id is not None]


def _string_attr(value: Any, attr: str) -> str | None:
    attr_value = getattr(value, attr, None)
    return attr_value if isinstance(attr_value, str) else None


def _raise_if_sidecar_restore_failed(pipeline: Any, status: str) -> None:
    result = getattr(pipeline, "sidecar_restore_result", None)
    if result is None or getattr(result, "ok", None) is not False:
        return
    result_status = getattr(result, "status", None)
    if result_status == status:
        raise _SidecarRestoreFailedError(status, getattr(result, "reason", None))


def _task_state_from_snapshot(snapshot: dict[str, Any]) -> str:
    status = snapshot.get("status")
    return _task_state_from_a2a_status(status)


def _task_state_from_a2a_status(status: Any) -> str:
    if status == "completed":
        return TASK_STATE_COMPLETED
    if status == "failed":
        return TASK_STATE_FAILED
    if status == "canceled":
        return TASK_STATE_CANCELED
    if status in {"waiting_input", "input_required"}:
        return TASK_STATE_INPUT_REQUIRED
    return TASK_STATE_INPUT_REQUIRED


def _task_state_from_pipeline(pipeline: Any, snapshot: dict[str, Any]) -> str:
    snapshot_status = snapshot.get("status")
    sidecar_status = getattr(pipeline, "sidecar_status", None)
    if _is_terminal_sidecar_status(sidecar_status) and snapshot_status not in _TERMINAL_SNAPSHOT_STATUSES:
        return _task_state_from_sidecar_status(sidecar_status)
    return _task_state_from_snapshot(snapshot)


def _is_terminal_sidecar_status(status: Any) -> bool:
    return isinstance(status, str) and status in _TERMINAL_SIDECAR_STATUSES


def _task_state_from_sidecar_status(status: Any) -> str:
    if status == "completed":
        return TASK_STATE_COMPLETED
    if status == "failed":
        return TASK_STATE_FAILED
    if status in {"user_aborted", "discarded", "canceled"}:
        return TASK_STATE_CANCELED
    return TASK_STATE_INPUT_REQUIRED


def _terminal_event_from_sidecar_status(status: Any) -> tuple[str, str] | None:
    if not isinstance(status, str):
        return None
    return _TERMINAL_EVENT_BY_SIDECAR_STATUS.get(status)


def _handoff_status_from_outcome(outcome: str) -> str:
    if outcome == "failed":
        return "failed"
    if outcome == "canceled":
        return "canceled"
    return "completed"


def _terminal_sidecar_matches_task(
    publisher: PipelineA2AEventPublisher,
    sidecar_status: Any,
    *,
    task_id: str,
    context_id: str,
) -> bool:
    terminal_event = _terminal_event_from_sidecar_status(sidecar_status)
    if terminal_event is None:
        return False
    return _owner_matches_task(
        _current_sidecar_owner(publisher, context_id=context_id),
        task_id=task_id,
        context_id=context_id,
    )


def _sidecar_matches_task(
    publisher: PipelineA2AEventPublisher,
    *,
    task_id: str,
    context_id: str,
    sidecar_status: str,
) -> bool:
    owner = _current_sidecar_owner(publisher, context_id=context_id)
    if owner is None or not _owner_matches_task(owner, task_id=task_id, context_id=context_id):
        return False
    status = _normalized_a2a_status(owner.status)
    if status in _TERMINAL_A2A_STATUSES:
        raise _SidecarStateTerminalError(status)
    if sidecar_status == "waiting_input":
        return status in _WAITING_A2A_STATUSES
    if sidecar_status == "running":
        if status in _WAITING_A2A_STATUSES:
            if _pending_ask_input_from_sidecar(publisher, task_id=task_id, context_id=context_id):
                return True
            if _pending_pipeline_pause_input_from_sidecar(publisher, task_id=task_id, context_id=context_id):
                return True
        return status in _RUNNING_A2A_STATUSES
    return False


def _current_sidecar_owner(publisher: PipelineA2AEventPublisher, *, context_id: str) -> _TaskContextOwner | None:
    return _current_sidecar_owner_from_stores(
        snapshot_store=publisher.snapshot_store,
        journal=publisher.journal,
        context_id=context_id,
    )


def _current_sidecar_owner_from_stores(
    *,
    snapshot_store: A2APipelineSnapshotStore,
    journal: A2APipelineJournal,
    context_id: str,
) -> _TaskContextOwner | None:
    snapshot_owner = _owner_from_snapshot(snapshot_store.load())
    if snapshot_owner is not None and snapshot_owner.context_id != context_id:
        snapshot_owner = None
    try:
        journal_events = journal.read_all_repairing_tail()
    except Exception:
        logger.warning("Failed to inspect A2A pipeline sidecar owner journal", exc_info=True)
        raise _SidecarOwnerUnavailableError("A2A pipeline sidecar owner is unavailable") from None
    journal_owner = _owner_from_journal_events(journal_events, context_id=context_id)
    if snapshot_owner is not None and (journal_owner is None or snapshot_owner.sequence >= journal_owner.sequence):
        return snapshot_owner
    return journal_owner


def _authoritative_snapshot_for_task(
    *,
    snapshot_store: A2APipelineSnapshotStore,
    journal: A2APipelineJournal,
    task_id: str,
    context_id: str,
) -> dict[str, Any] | None:
    snapshot = snapshot_store.load()
    try:
        events = _events_for_task_context(
            journal.read_all_repairing_tail(),
            task_id=task_id,
            context_id=context_id,
        )
    except Exception:
        logger.warning("Failed to build A2A pipeline snapshot from journal", exc_info=True)
        return snapshot
    if not events:
        return snapshot
    try:
        rebuilt = reduce_pipeline_events(events)
    except Exception:
        logger.warning("Failed to reduce A2A pipeline journal events", exc_info=True)
        return snapshot
    if not isinstance(rebuilt, dict):
        return snapshot
    snapshot_sequence = _sequence_number(snapshot.get("lastSequence")) if isinstance(snapshot, dict) else 0
    rebuilt_sequence = _sequence_number(rebuilt.get("lastSequence"))
    if rebuilt_sequence >= snapshot_sequence:
        try:
            snapshot_store.save(rebuilt)
        except Exception:
            logger.debug("Failed to save repaired A2A pipeline snapshot", exc_info=True)
        return rebuilt
    return snapshot


def _owner_matches_task(owner: _TaskContextOwner | None, *, task_id: str, context_id: str) -> bool:
    return owner is not None and owner.task_id == task_id and owner.context_id == context_id


def _owner_from_snapshot(snapshot: dict[str, Any] | None) -> _TaskContextOwner | None:
    if not isinstance(snapshot, dict):
        return None
    return _owner_from_values(
        snapshot.get("taskId"),
        snapshot.get("contextId"),
        _sequence_number(snapshot.get("lastSequence")),
        snapshot.get("status"),
    )


def _owner_from_journal_events(events: list[dict[str, Any]], *, context_id: str) -> _TaskContextOwner | None:
    owner: _TaskContextOwner | None = None
    for event in events:
        if event.get("contextId") != context_id:
            continue
        candidate = _owner_from_values(
            event.get("taskId"),
            event.get("contextId"),
            _sequence_number(event.get("sequence")),
            event.get("status"),
        )
        if candidate is not None and (owner is None or candidate.sequence >= owner.sequence):
            owner = candidate
    return owner


def _owner_from_values(
    task_id: Any,
    context_id: Any,
    sequence: int,
    status: Any = None,
) -> _TaskContextOwner | None:
    if not isinstance(task_id, str) or not task_id:
        return None
    if not isinstance(context_id, str) or not context_id:
        return None
    return _TaskContextOwner(
        task_id=task_id,
        context_id=context_id,
        sequence=sequence,
        status=status if isinstance(status, str) else None,
    )


def _normalized_a2a_status(status: str | None) -> str | None:
    if status == "input_required":
        return "waiting_input"
    return status


def _safe_read_pipeline_journal(journal: A2APipelineJournal) -> list[dict[str, Any]]:
    try:
        return journal.read_all_repairing_tail()
    except Exception:
        logger.warning("Failed to inspect A2A pipeline terminal recovery journal", exc_info=True)
        return []


def _events_for_task_context(
    events: list[dict[str, Any]],
    *,
    task_id: str,
    context_id: str,
) -> list[dict[str, Any]]:
    return [event for event in events if event.get("taskId") == task_id and event.get("contextId") == context_id]


def _latest_terminal_a2a_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    terminal_event: dict[str, Any] | None = None
    for event in events:
        if _terminal_status_from_a2a_event(event) is None:
            continue
        if terminal_event is None or _sequence_number(event.get("sequence")) >= _sequence_number(
            terminal_event.get("sequence")
        ):
            terminal_event = event
    return terminal_event


def _terminal_status_from_a2a_event(event: dict[str, Any]) -> str | None:
    status = _normalized_a2a_status(event.get("status") if isinstance(event.get("status"), str) else None)
    if status in _TERMINAL_A2A_STATUSES:
        return status
    event_type = event.get("eventType")
    if event_type == "pipeline_completed":
        return "completed"
    if event_type == "pipeline_failed":
        return "failed"
    if event_type == "pipeline_canceled":
        return "canceled"
    return None


def _snapshot_has_conflicting_terminal_status(
    snapshot: dict[str, Any] | None,
    status: str,
    *,
    task_id: str,
    context_id: str,
) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("taskId") != task_id or snapshot.get("contextId") != context_id:
        return False
    snapshot_status = _normalized_a2a_status(
        snapshot.get("status") if isinstance(snapshot.get("status"), str) else None
    )
    return snapshot_status in _TERMINAL_A2A_STATUSES and snapshot_status != status


def _terminal_snapshot_needs_journal_rebuild(
    snapshot: dict[str, Any] | None,
    journal_events: list[dict[str, Any]],
    status: str,
    *,
    task_id: str,
    context_id: str,
) -> bool:
    if _terminal_snapshot_needs_recovery_event(snapshot, status, task_id=task_id, context_id=context_id):
        return True
    if not isinstance(snapshot, dict):
        return True
    snapshot_sequence = _sequence_number(snapshot.get("lastSequence"))
    journal_sequence = max((_sequence_number(event.get("sequence")) for event in journal_events), default=0)
    return snapshot_sequence < journal_sequence


def _terminal_snapshot_needs_recovery_event(
    snapshot: dict[str, Any] | None,
    status: str,
    *,
    task_id: str,
    context_id: str,
) -> bool:
    if not isinstance(snapshot, dict):
        return True
    if snapshot.get("status") != status:
        return True
    if snapshot.get("taskId") != task_id:
        return True
    return snapshot.get("contextId") != context_id


def _sequence_number(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _a2a_state_from_task_state(state: str) -> int:
    if state == TASK_STATE_COMPLETED:
        return TaskState.TASK_STATE_COMPLETED
    if state == TASK_STATE_FAILED:
        return TaskState.TASK_STATE_FAILED
    if state == TASK_STATE_CANCELED:
        return TaskState.TASK_STATE_CANCELED
    if state == TASK_STATE_WORKING:
        return TaskState.TASK_STATE_WORKING
    return TaskState.TASK_STATE_INPUT_REQUIRED


def _sanitize_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if any(marker in msg for marker in _AUTH_ERROR_MARKERS):
        return _auth_error_text()
    if type(exc).__name__ == "AuthenticationError":
        return _auth_error_text()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 401:
        return _auth_error_text()
    return public_error(message=_format_exception(exc), error_type=type(exc).__name__).summary


def _public_error_details_for_a2a(details: dict[str, Any]) -> dict[str, Any]:
    payload = dict(details)
    error_id = payload.pop("error_id", None)
    if error_id is not None:
        payload["errorId"] = error_id
    return payload


def _is_retryable_executor_error(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError, ConnectionError))


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message[:_ERROR_TEXT_MAX_CHARS]}"


async def _flush_telemetry_safely() -> None:
    from iac_code.services.telemetry import flush_telemetry

    try:
        await asyncio.to_thread(flush_telemetry)
    except Exception:
        logger.debug("flush_telemetry after pipeline task failed", exc_info=True)
