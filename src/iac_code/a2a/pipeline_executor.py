from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import yaml
from a2a.types import Message, Role, TaskState, TaskStatus, TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError

from iac_code.a2a.artifacts import artifact_store_for_session
from iac_code.a2a.backup import backup_session_async
from iac_code.a2a.events import make_text_part, publish_mcp_warnings
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_flow_monitor import (
    PipelineA2AFlowIdentity,
    PipelineA2AFlowMonitor,
)
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_outbound import PipelineA2AOutboundQueue
from iac_code.a2a.pipeline_paths import (
    a2a_pipeline_dir_for_sidecar_dir,
    existing_a2a_pipeline_dir_for_session,
)
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.pipeline_stream import (
    BACKUP_COMMITTED_EVENT_TYPE,
    PipelineA2AEventPublisher,
    backup_committed_delivery_envelope,
    committed_backup_publication_envelope,
    pending_backup_publication_envelope,
)
from iac_code.a2a.pipeline_transport_delivery import PipelineTransportDeliveryClosedError
from iac_code.a2a.runtime_overrides import (
    a2a_request_context,
    configure_runtime_model,
    refresh_runtime_cloud_tools,
)
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
from iac_code.pipeline.config import get_pipeline_name, is_selling_review_step_enabled
from iac_code.pipeline.engine.cleanup import CleanupLedger
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.handoff import build_handoff_summary, terminal_outcome_from_completed_event
from iac_code.pipeline.engine.loader import _resolve_feature_flags, load_pipeline_dir
from iac_code.pipeline.engine.prerequisites import inspect_prerequisites
from iac_code.pipeline.engine.public_errors import public_error
from iac_code.pipeline.engine.session import PipelineSession
from iac_code.pipeline.engine.user_input import PipelineUserInput, normalize_pipeline_user_input
from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.session_backup import BackupReason, SessionBackupBlocked, SessionBackupService
from iac_code.services.session_backup_state import NORMAL_HANDOFF_PROOF_KEY, BackupPublicationProof
from iac_code.services.session_layout import SessionPaths
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import AskUserQuestionEvent, SubPipelineStreamEvent, TextDeltaEvent
from iac_code.utils.path_locks import PathLockRegistry

logger = logging.getLogger(__name__)
_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS = 1
_ERROR_TEXT_MAX_CHARS = 1000
_TERMINAL_SIDECAR_STATUSES = {"completed", "failed", "user_aborted", "discarded", "canceled"}
_TERMINAL_SNAPSHOT_STATUSES = {"completed", "failed", "canceled"}
_TERMINAL_A2A_STATUSES = {"completed", "failed", "canceled"}
_WAITING_A2A_STATUSES = {"waiting_input", "input_required"}
_RUNNING_A2A_STATUSES = {"working"}
_PENDING_BACKUP_VISIBILITY = "pending_backup"
_COMMITTED_BACKUP_VISIBILITY = "committed"
_WAITING_INPUT_CANCEL_LOCKS = PathLockRegistry()
_TERMINAL_PUBLICATION_UNAVAILABLE_KIND = "terminal_publication_unavailable"
_HANDOFF_PUBLICATION_UNAVAILABLE_ACTION = "switch_to_normal_unavailable"
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
_ACTIVE_INTERRUPT_TERMINAL_WAIT_TIMEOUT_SECONDS = 30.0


def _new_set_asyncio_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


class WaitingInputCancelResult(str, Enum):
    CANCELED = "canceled"
    NOT_OWNER = "not_owner"
    PERSIST_FAILED = "persist_failed"
    BACKUP_BLOCKED = "backup_blocked"
    BACKUP_BLOCKED_PERSIST_FAILED = "backup_blocked_persist_failed"


_CANCEL_WAITING_INPUT_BACKUP_BLOCKED = WaitingInputCancelResult.BACKUP_BLOCKED


def _retry_text() -> str:
    return _("A temporary error occurred. Please retry.")


class RecoverablePipelineInvalidParamsError(InvalidParamsError):
    code = -32602
    jsonrpc_error_data_passthrough = True


def _active_sidecar_mismatch_error(
    *,
    recoverable_task_id: str,
    context_id: str,
    sidecar_status: str,
) -> RecoverablePipelineInvalidParamsError:
    return RecoverablePipelineInvalidParamsError(
        _("Pipeline already running. Resume task {task_id}.").format(task_id=recoverable_task_id),
        data={
            "recoverableTaskId": recoverable_task_id,
            "contextId": context_id,
            "sidecarStatus": sidecar_status,
        },
    )


@dataclass
class A2APipelineRuntime:
    agent_runtime: Any
    pipeline: Any | None = None
    publisher: PipelineA2AEventPublisher | None = None
    outbound: PipelineA2AOutboundQueue | None = None
    outbound_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_stream: Any | None = None
    pending_question: "_PendingAskUserQuestion | None" = None
    active_owner_task: asyncio.Task[Any] | None = None
    restart_after_interrupt: bool = False
    pause_after_interrupt: bool = False
    active_interrupt_count: int = 0
    terminal_publication_started: bool = False
    restart_requested: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_settled: asyncio.Event = field(default_factory=_new_set_asyncio_event)


@dataclass(frozen=True)
class _StreamConsumeResult:
    had_events: bool
    restart_requested: bool
    terminal_handoff_unavailable: bool = False


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


@dataclass(frozen=True)
class _NormalHandoffPublication:
    status: str
    data: dict[str, Any]
    summary: str


@dataclass(frozen=True)
class _TerminalHandoffPublishResult:
    attempted: bool
    terminal_available: bool


@dataclass(frozen=True)
class _TerminalStreamPublishResult:
    interrupt_action: str | None = None
    handoff: _TerminalHandoffPublishResult | None = None
    text: str | None = None


@dataclass(frozen=True)
class _TerminalSidecarRecoveryResult:
    published: bool
    available: bool


class _SidecarOwnerUnavailableError(RuntimeError):
    pass


class _SidecarStateTerminalError(RuntimeError):
    def __init__(self, status: str) -> None:
        super().__init__("A2A pipeline sidecar owner is already terminal")
        self.status = status


class _SidecarRestoreFailedError(RuntimeError):
    def __init__(self, status: str, reason: str | None) -> None:
        detail = reason or _("unknown")
        super().__init__(
            _("A2A pipeline sidecar restore failed: status={status}, reason={reason}").format(
                status=status,
                reason=detail,
            )
        )
        self.status = status
        self.reason = reason


class _PipelineBackupBlockedTransitionError(Exception):
    pass


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
        user_id: str | None = None,
        aliyun_credential: AliyunCredential | None = None,
        model_from_metadata: bool = False,
        metadata_api_key: str | None = None,
        request_policy_override: ProviderRequestPolicy | None = None,
        provider_key_override: str | None = None,
        provider_api_key_override: str | None = None,
        provider_base_url_override: str | None = None,
        provider_config_frozen: bool = False,
        provider_config_override: dict[str, Any] | None = None,
        effort_override: str | None = None,
        backup_service: Any | None = None,
        aliyun_delegated_executor_factory: Any | None = None,
    ) -> None:
        self._task_store = task_store
        self._model = model
        self._metrics = metrics
        self._artifact_store = artifact_store
        self._push_notifier = push_notifier
        self._permission_resolver = permission_resolver
        self._auto_approve_permissions = auto_approve_permissions
        self._thinking_exposure_types = thinking_exposure_types
        self._user_id = user_id
        self._aliyun_credential = aliyun_credential
        self._model_from_metadata = model_from_metadata
        self._metadata_api_key = metadata_api_key
        self._request_policy_override = request_policy_override
        self._provider_key_override = provider_key_override
        self._provider_api_key_override = provider_api_key_override
        self._provider_base_url_override = provider_base_url_override
        self._provider_config_frozen = provider_config_frozen
        self._provider_config_override = provider_config_override
        self._effort_override = effort_override
        self._backup_service = backup_service or SessionBackupService()
        self._aliyun_delegated_executor_factory = aliyun_delegated_executor_factory

    async def execute(
        self,
        *,
        context: Any,
        event_queue: Any,
        task: Any,
        task_id: str,
        context_id: str,
        cwd: str,
        pipeline_input: PipelineUserInput | str | None = None,
        prompt: str | None = None,
        active_followup_only: bool = False,
    ) -> bool | None:
        if pipeline_input is None:
            pipeline_input = prompt or ""
        pipeline_input = normalize_pipeline_user_input(pipeline_input)
        prompt = pipeline_input.display_text
        session_storage = SessionStorage()

        def runtime_factory(session_id: str) -> Any:
            SessionBackupService(session_storage=session_storage).restore_session(cwd, session_id)
            return create_agent_runtime(
                AgentFactoryOptions(
                    model=self._model,
                    session_id=session_id,
                    cwd=cwd,
                    provider_key_override=self._provider_key_override,
                    provider_api_key_override=self._provider_api_key_override,
                    provider_base_url_override=self._provider_base_url_override,
                    provider_config_frozen=self._provider_config_frozen,
                    provider_config_override=self._provider_config_override,
                    effort_override=self._effort_override,
                )
            )

        try:
            with self._request_context():
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
            self._clear_stale_recoverable_active_task(
                task=task,
                ctx=ctx,
                task_id=task_id,
                context_id=context_id,
                cwd=cwd,
            )
            if ctx.active_task_id is not None:
                preserve_active_task = _is_active_task_record(task, ctx.active_task_id)
                if _is_active_task_request(task, task_id, ctx.active_task_id):
                    with self._request_context(session_id=ctx.session_id):
                        self._configure_runtime_for_request(ctx.runtime)
                        routed = await self._route_active_pipeline_interrupt(
                            event_queue,
                            task=task,
                            ctx=ctx,
                            task_id=task_id,
                            context_id=context_id,
                            cwd=cwd,
                            pipeline_input=pipeline_input,
                            preserve_task_record=preserve_active_task,
                        )
                    if routed:
                        return True
                if active_followup_only:
                    await self._fail_already_active(
                        event_queue,
                        task=task,
                        task_id=task_id,
                        context_id=context_id,
                        preserve_task_record=preserve_active_task,
                    )
                    return True
                await self._fail_already_active(
                    event_queue,
                    task=task,
                    task_id=task_id,
                    context_id=context_id,
                    preserve_task_record=preserve_active_task,
                )
                return

        if active_followup_only:
            return False

        lock = ctx.lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except TimeoutError:
            await self._fail_already_active(event_queue, task=task, task_id=task_id, context_id=context_id)
            return

        try:
            owner_task = asyncio.current_task()
            task_persistence_started = False

            pipeline = None
            publisher: PipelineA2AEventPublisher | None = None
            pipeline_runtime: A2APipelineRuntime | None = None
            try:
                with self._request_context(session_id=ctx.session_id):
                    pipeline_runtime = self._pipeline_runtime_from_context(
                        ctx.runtime,
                        session_id=ctx.session_id,
                        cwd=cwd,
                    )
                    agent_runtime = pipeline_runtime.agent_runtime
                    await publish_mcp_warnings(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        runtime=agent_runtime,
                        iac_code_session_id=ctx.session_id,
                    )
                    self._configure_agent_runtime_for_request(agent_runtime)
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
                    self._install_backup_hook(
                        publisher,
                        pipeline=pipeline,
                        cwd=cwd,
                        session_id=ctx.session_id,
                        task=task,
                        ctx=ctx,
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

                    selected = await self._select_stream(
                        pipeline,
                        prompt,
                        pipeline_input=pipeline_input,
                        publisher=publisher,
                        task_id=task_id,
                        context_id=context_id,
                        fresh_pipeline_factory=fresh_pipeline_factory,
                    )
                if selected.pipeline is not pipeline:
                    pipeline = selected.pipeline
                    publisher = self._publisher(
                        event_queue=event_queue,
                        pipeline=pipeline,
                        task_id=task_id,
                        context_id=context_id,
                        session_id=ctx.session_id,
                        cwd=cwd,
                    )
                    pipeline_runtime.pipeline = pipeline
                    pipeline_runtime.publisher = publisher
                    self._task_store.mirror_context(ctx)
                stream = selected.stream
                ctx.active_task_id = task.task_id
                task.active_task = owner_task
                pipeline_runtime.active_owner_task = owner_task
                task.state = TASK_STATE_WORKING
                task_persistence_started = True
                self._task_store.mirror_task(task)
                self._task_store.mirror_context(ctx)
                stream_had_events = False
                terminal_handoff_unavailable = False
                with self._request_context(session_id=ctx.session_id):
                    while True:
                        stream_result = await self._consume_stream_until_restart(
                            stream=stream,
                            runtime=pipeline_runtime,
                            publisher=publisher,
                            task=task,
                        )
                        stream_had_events = stream_had_events or stream_result.had_events
                        terminal_handoff_unavailable = (
                            terminal_handoff_unavailable or stream_result.terminal_handoff_unavailable
                        )

                        if not stream_result.restart_requested:
                            break

                        stream = self._continue_after_interrupt_stream(pipeline, pipeline_input)

                terminal_status_published = False
                terminal_sidecar = _is_terminal_sidecar_status(getattr(pipeline, "sidecar_status", None))
                terminal_sidecar_recovery_allowed = not terminal_handoff_unavailable
                if terminal_sidecar and terminal_sidecar_recovery_allowed and publisher is not None:
                    terminal_status_published = await self._run_terminal_sidecar_recovery_publication(
                        pipeline_runtime,
                        publisher,
                        pipeline,
                        task_id=task_id,
                        context_id=context_id,
                    )
                committed_terminal_status = (
                    _committed_terminal_status_for_task_context(
                        publisher,
                        task_id=task_id,
                        context_id=context_id,
                    )
                    if terminal_sidecar and terminal_sidecar_recovery_allowed and publisher is not None
                    else None
                )
                sidecar_terminal_status = _terminal_status_from_sidecar(getattr(pipeline, "sidecar_status", None))
                terminal_snapshot_available = terminal_status_published or committed_terminal_status is not None
                sidecar_terminal_fallback_available = terminal_status_published or (
                    committed_terminal_status is not None and committed_terminal_status == sidecar_terminal_status
                )

                snapshot = publisher.snapshot_store.load() or {}
                task.state = _task_state_from_pipeline(
                    pipeline,
                    snapshot,
                    allow_terminal_snapshot=not terminal_sidecar or terminal_snapshot_available,
                    allow_sidecar_terminal_fallback=sidecar_terminal_fallback_available,
                )
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
                try:
                    task.state = TASK_STATE_CANCELED
                    cancel_data = {"source": "executor", "reason": _("Task canceled.")}
                    cancel_handoff_data = {"canceled": True, "reason": _("Task canceled.")}

                    async def publish_cancel_terminal() -> bool:
                        if pipeline is not None:
                            await self._mark_user_aborted(pipeline)
                        cancel_transaction_result = _TerminalHandoffPublishResult(
                            attempted=False,
                            terminal_available=False,
                        )
                        if pipeline is not None and publisher is not None:
                            cancel_transaction_result = await self._publish_manual_terminal_with_normal_handoff(
                                pipeline,
                                publisher,
                                event_type="pipeline_canceled",
                                status="canceled",
                                terminal_data=cancel_data,
                                handoff_data=cancel_handoff_data,
                            )
                        cancel_terminal_available = cancel_transaction_result.terminal_available
                        if not cancel_transaction_result.attempted:
                            cancel_terminal_available = await self._publish_pipeline_terminal_event(
                                publisher,
                                event_type="pipeline_canceled",
                                status="canceled",
                                data=cancel_data,
                            )
                            if cancel_terminal_available and pipeline is not None and publisher is not None:
                                await self._publish_normal_handoff_ready(pipeline, publisher, cancel_handoff_data)
                        return cancel_terminal_available

                    if pipeline_runtime is not None and publisher is not None:
                        cancel_terminal_available = await self._run_external_terminal_publication(
                            pipeline_runtime,
                            publish_cancel_terminal,
                        )
                    else:
                        cancel_terminal_available = await publish_cancel_terminal()
                    if not cancel_terminal_available:
                        task.state = TASK_STATE_INPUT_REQUIRED
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=_a2a_state_from_task_state(task.state),
                        text=_("Task canceled."),
                    )
                    self._task_store.mirror_task(task)
                    await self._notify_terminal_task(
                        task_id=task.task_id,
                        context_id=task.context_id,
                        state=task.state,
                    )
                    self._record_state(task.state)
                except _PipelineBackupBlockedTransitionError:
                    task_persistence_started = True
                    await self._complete_backup_blocked_transition(task=task, ctx=ctx)
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
            except RecoverablePipelineInvalidParamsError:
                raise
            except _PipelineBackupBlockedTransitionError:
                task_persistence_started = True
                await self._complete_backup_blocked_transition(task=task, ctx=ctx)
            except Exception as exc:
                task_persistence_started = True
                try:
                    await self._publish_exception_status(
                        event_queue,
                        task=task,
                        task_id=task_id,
                        context_id=context_id,
                        exc=exc,
                        pipeline_publisher=publisher,
                        pipeline_runtime=pipeline_runtime,
                    )
                except _PipelineBackupBlockedTransitionError:
                    await self._complete_backup_blocked_transition(task=task, ctx=ctx)
            finally:
                replacement_owner = getattr(ctx.runtime, "active_owner_task", None)
                if (
                    replacement_owner is not None
                    and replacement_owner is not owner_task
                    and not replacement_owner.done()
                ):
                    task.active_task = replacement_owner
                    ctx.active_task_id = task.task_id
                elif (
                    task.active_task is not None and task.active_task is not owner_task and not task.active_task.done()
                ):
                    ctx.active_task_id = task.task_id
                elif task.active_task is owner_task:
                    task.active_task = None
                    if ctx.active_task_id == task.task_id:
                        ctx.active_task_id = None
                    if replacement_owner is owner_task and hasattr(ctx.runtime, "active_owner_task"):
                        ctx.runtime.active_owner_task = None
                ctx.touch()
                if task_persistence_started:
                    task.touch()
                    self._task_store.mirror_task(task)
                self._task_store.mirror_context(ctx)
                await _flush_telemetry_safely()
        finally:
            lock.release()

    def _request_context(self, *, session_id: str | None = None) -> contextlib.AbstractContextManager[None]:
        return a2a_request_context(
            session_id=session_id,
            user_id=self._user_id,
            aliyun_credential=self._aliyun_credential,
        )

    def _configure_runtime_for_request(self, runtime: Any) -> None:
        agent_runtime = getattr(runtime, "agent_runtime", None)
        if agent_runtime is not None:
            self._configure_agent_runtime_for_request(agent_runtime)

    def _configure_agent_runtime_for_request(self, agent_runtime: Any) -> None:
        configure_runtime_model(
            agent_runtime,
            self._model,
            from_metadata=self._model_from_metadata,
            metadata_api_key=self._metadata_api_key,
            request_policy_override=self._request_policy_override,
            provider_key_override=self._provider_key_override,
            provider_api_key_override=self._provider_api_key_override,
            provider_base_url_override=self._provider_base_url_override,
            provider_config_frozen=self._provider_config_frozen,
            provider_config_override=self._provider_config_override,
            effort_override=self._effort_override,
        )
        if self._aliyun_credential is not None:
            refresh_runtime_cloud_tools(agent_runtime)

    def _pipeline_runtime_from_context(self, runtime: Any, *, session_id: str, cwd: str) -> A2APipelineRuntime:
        if isinstance(runtime, A2APipelineRuntime):
            return runtime
        if runtime is not None:
            return A2APipelineRuntime(agent_runtime=runtime)
        return A2APipelineRuntime(
            agent_runtime=create_agent_runtime(
                AgentFactoryOptions(
                    model=self._model,
                    session_id=session_id,
                    cwd=cwd,
                    provider_key_override=self._provider_key_override,
                    provider_api_key_override=self._provider_api_key_override,
                    provider_base_url_override=self._provider_base_url_override,
                    provider_config_frozen=self._provider_config_frozen,
                    provider_config_override=self._provider_config_override,
                    effort_override=self._effort_override,
                )
            ),
        )

    def _clear_stale_recoverable_active_task(
        self,
        *,
        task: Any,
        ctx: Any,
        task_id: str,
        context_id: str,
        cwd: str,
    ) -> bool:
        if not _is_active_task_request(task, task_id, getattr(ctx, "active_task_id", None)):
            return False
        if _task_has_live_owner(task):
            return False
        try:
            recoverable_task_id = recoverable_task_id_from_sidecar(
                cwd=cwd,
                session_id=ctx.session_id,
                context_id=context_id,
            )
        except Exception:
            return False
        if recoverable_task_id != task_id:
            return False
        ctx.active_task_id = None
        ctx.touch()
        self._task_store.mirror_context(ctx)
        return True

    async def _route_active_pipeline_interrupt(
        self,
        event_queue: Any,
        *,
        task: Any,
        ctx: Any,
        task_id: str,
        context_id: str,
        cwd: str,
        pipeline_input: PipelineUserInput,
        preserve_task_record: bool,
    ) -> bool:
        runtime = ctx.runtime
        if getattr(runtime, "pipeline", None) is None:
            return False
        if not await _register_active_interrupt(runtime):
            logger.info("Ignoring A2A pipeline interrupt after terminal publication started")
            return True

        interrupt_registered = True

        async def settle_interrupt() -> None:
            nonlocal interrupt_registered
            if not interrupt_registered:
                return
            interrupt_registered = False
            await _settle_active_interrupt_safely(runtime)

        try:
            return await self._route_registered_active_pipeline_interrupt(
                event_queue,
                task=task,
                ctx=ctx,
                task_id=task_id,
                context_id=context_id,
                cwd=cwd,
                pipeline_input=pipeline_input,
                preserve_task_record=preserve_task_record,
                settle_interrupt=settle_interrupt,
            )
        finally:
            await settle_interrupt()

    async def _route_registered_active_pipeline_interrupt(
        self,
        event_queue: Any,
        *,
        task: Any,
        ctx: Any,
        task_id: str,
        context_id: str,
        cwd: str,
        pipeline_input: PipelineUserInput,
        preserve_task_record: bool,
        settle_interrupt: Callable[[], Awaitable[None]],
    ) -> bool:
        pipeline_input = normalize_pipeline_user_input(pipeline_input)
        prompt = pipeline_input.display_text
        runtime = ctx.runtime
        pipeline = getattr(runtime, "pipeline", None)
        if pipeline is None:
            return False
        publisher = getattr(runtime, "publisher", None)
        if isinstance(publisher, PipelineA2AEventPublisher):
            self._install_backup_hook(
                publisher,
                pipeline=pipeline,
                cwd=cwd,
                session_id=ctx.session_id,
                task=task,
                ctx=ctx,
            )

        try:
            pending_question_route = await self._route_pending_question_answer(runtime, pipeline_input)
        except Exception as exc:
            try:
                await self._publish_exception_status(
                    event_queue,
                    task=task,
                    task_id=task_id,
                    context_id=context_id,
                    exc=exc,
                    preserve_task_record=preserve_task_record,
                    pipeline_publisher=publisher,
                )
            except _PipelineBackupBlockedTransitionError:
                await self._complete_backup_blocked_transition(task=task, ctx=ctx)
            return True
        if pending_question_route == _PENDING_QUESTION_ANSWERED:
            task.state = TASK_STATE_WORKING
            self._task_store.mirror_task(task)
            return True
        if pending_question_route == _PENDING_QUESTION_STALE_FINISHED:
            task.state = TASK_STATE_INPUT_REQUIRED
            self._task_store.mirror_task(task)
            return True

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
        self._install_backup_hook(
            publisher,
            pipeline=pipeline,
            cwd=cwd,
            session_id=ctx.session_id,
            task=task,
            ctx=ctx,
        )

        if _pending_pipeline_pause_input_from_sidecar(publisher, task_id=task_id, context_id=context_id) is not None:
            await settle_interrupt()
            await self._continue_active_pause_confirmation(
                event_queue,
                task=task,
                ctx=ctx,
                runtime=runtime,
                pipeline=pipeline,
                publisher=publisher,
                task_id=task_id,
                context_id=context_id,
                cwd=cwd,
                session_id=ctx.session_id,
                pipeline_input=pipeline_input,
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
                await self._run_outbound_serialized(
                    runtime,
                    lambda: publish_interrupt_received(prompt=prompt),
                )
                interrupt_received_published = True

            pause_agent_loops = getattr(pipeline, "pause_agent_loops", None)
            if callable(pause_agent_loops):
                await _maybe_await(pause_agent_loops())
                paused = True

            runner_input = _pipeline_runner_input(pipeline_input)
            verdict = await _maybe_await(handler(runner_input))
            async with _outbound_lock(runtime):
                if bool(getattr(runtime, "terminal_publication_started", False)):
                    return True
                parent_rollback: bool | None = None
                if getattr(verdict, "action", "") == "hard_interrupt":
                    apply_hard_interrupt = getattr(pipeline, "apply_hard_interrupt", None)
                    if callable(apply_hard_interrupt):
                        parameters = inspect.signature(apply_hard_interrupt).parameters
                        if "source_input" in parameters or any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                        ):
                            applied = apply_hard_interrupt(verdict, source_input=runner_input)
                        else:
                            applied = apply_hard_interrupt(verdict)
                        parent_rollback = bool(await _maybe_await(applied))
                        if parent_rollback:
                            runtime.restart_after_interrupt = True
                            _restart_requested_event(runtime).set()

                outbound = getattr(runtime, "outbound", None)

                async def publish_classification() -> None:
                    await publisher.publish_interrupt(
                        prompt=prompt,
                        verdict=verdict,
                        parent_rollback=parent_rollback,
                        include_received=not interrupt_received_published,
                    )

                if outbound is None:
                    await publish_classification()
                else:
                    await outbound.run_serialized(publish_classification)
            if _is_terminal_sidecar_status(getattr(pipeline, "sidecar_status", None)):
                await settle_interrupt()
                terminal_status_published = await self._run_terminal_sidecar_recovery_publication(
                    runtime,
                    publisher,
                    pipeline,
                    task_id=task_id,
                    context_id=context_id,
                )
                committed_terminal_status = _committed_terminal_status_for_task_context(
                    publisher,
                    task_id=task_id,
                    context_id=context_id,
                )
                sidecar_terminal_status = _terminal_status_from_sidecar(getattr(pipeline, "sidecar_status", None))
                terminal_snapshot_available = terminal_status_published or committed_terminal_status is not None
                sidecar_terminal_fallback_available = terminal_status_published or (
                    committed_terminal_status is not None and committed_terminal_status == sidecar_terminal_status
                )
                snapshot = publisher.snapshot_store.load() or {}
                task.state = _task_state_from_pipeline(
                    pipeline,
                    snapshot,
                    allow_terminal_snapshot=terminal_snapshot_available,
                    allow_sidecar_terminal_fallback=sidecar_terminal_fallback_available,
                )
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
                    await self._run_outbound_serialized(runtime, lambda: publisher.publish(pause_event))
                task.state = TASK_STATE_INPUT_REQUIRED
                self._task_store.mirror_task(task)
                runtime.pause_after_interrupt = True
                _restart_requested_event(runtime).set()
            return True
        except _PipelineBackupBlockedTransitionError:
            await self._complete_backup_blocked_transition(task=task, ctx=ctx)
            return True
        except Exception as exc:
            try:
                await self._publish_exception_status(
                    event_queue,
                    task=task,
                    task_id=task_id,
                    context_id=context_id,
                    exc=exc,
                    preserve_task_record=preserve_task_record,
                )
            except _PipelineBackupBlockedTransitionError:
                await self._complete_backup_blocked_transition(task=task, ctx=ctx)
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
        cwd: str,
        session_id: str,
        pipeline_input: PipelineUserInput,
        preserve_task_record: bool,
    ) -> None:
        pipeline_input = normalize_pipeline_user_input(pipeline_input)
        prompt = pipeline_input.display_text
        owner_task = asyncio.current_task()
        self._install_backup_hook(
            publisher,
            pipeline=pipeline,
            cwd=cwd,
            session_id=session_id,
            task=task,
            ctx=ctx,
        )
        task.active_task = owner_task
        runtime.active_owner_task = owner_task
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
            if prompt:
                stream = pipeline.continue_from_sidecar(user_input=_pipeline_runner_input(pipeline_input))
            else:
                stream = pipeline.continue_from_sidecar()
            task.state = TASK_STATE_WORKING
            self._task_store.mirror_task(task)
            terminal_handoff_unavailable = False
            with self._request_context(session_id=session_id):
                while True:
                    stream_result = await self._consume_stream_until_restart(
                        stream=stream,
                        runtime=runtime,
                        publisher=publisher,
                        task=task,
                    )
                    terminal_handoff_unavailable = (
                        terminal_handoff_unavailable or stream_result.terminal_handoff_unavailable
                    )
                    if not stream_result.restart_requested:
                        break
                    stream = self._continue_after_interrupt_stream(pipeline, pipeline_input)

            terminal_status_published = False
            terminal_sidecar = _is_terminal_sidecar_status(getattr(pipeline, "sidecar_status", None))
            terminal_sidecar_recovery_allowed = not terminal_handoff_unavailable
            if terminal_sidecar and terminal_sidecar_recovery_allowed:
                terminal_status_published = await self._run_terminal_sidecar_recovery_publication(
                    runtime,
                    publisher,
                    pipeline,
                    task_id=task_id,
                    context_id=context_id,
                )
            committed_terminal_status = (
                _committed_terminal_status_for_task_context(
                    publisher,
                    task_id=task_id,
                    context_id=context_id,
                )
                if terminal_sidecar and terminal_sidecar_recovery_allowed
                else None
            )
            sidecar_terminal_status = _terminal_status_from_sidecar(getattr(pipeline, "sidecar_status", None))
            terminal_snapshot_available = terminal_status_published or committed_terminal_status is not None
            sidecar_terminal_fallback_available = terminal_status_published or (
                committed_terminal_status is not None and committed_terminal_status == sidecar_terminal_status
            )

            snapshot = publisher.snapshot_store.load() or {}
            task.state = _task_state_from_pipeline(
                pipeline,
                snapshot,
                allow_terminal_snapshot=not terminal_sidecar or terminal_snapshot_available,
                allow_sidecar_terminal_fallback=sidecar_terminal_fallback_available,
            )
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task_id, context_id=context_id, state=task.state)
            self._record_state(task.state)
        except _PipelineBackupBlockedTransitionError:
            await self._complete_backup_blocked_transition(task=task, ctx=ctx)
        except Exception as exc:
            try:
                await self._publish_exception_status(
                    event_queue,
                    task=task,
                    task_id=task_id,
                    context_id=context_id,
                    exc=exc,
                    preserve_task_record=False,
                    pipeline_publisher=publisher,
                    pipeline_runtime=runtime,
                )
            except _PipelineBackupBlockedTransitionError:
                await self._complete_backup_blocked_transition(task=task, ctx=ctx)
        finally:
            if task.active_task is owner_task:
                task.active_task = None
                if runtime.active_owner_task is owner_task:
                    runtime.active_owner_task = None
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
        pipeline_name = get_pipeline_name()
        prerequisite_resolution = self._inspect_pipeline_prerequisite_metadata(
            pipeline_name=pipeline_name,
            cwd=cwd,
            session_id=session_id,
            session_storage=session_storage,
            resume_from_sidecar=resume_from_sidecar,
        )
        create_kwargs: dict[str, Any] = {}
        if prerequisite_resolution is not None:
            create_kwargs["prerequisite_resolution"] = prerequisite_resolution
        delegated_factory = self._aliyun_delegated_executor_factory
        if delegated_factory is None:
            services = getattr(runtime, "aliyun_services", None)
            delegated_factory = getattr(services, "delegated_executor_factory", None)
        return create_pipeline(
            pipeline_name,
            provider_manager=runtime.provider_manager,
            base_tool_registry=runtime.tool_registry,
            session_storage=session_storage,
            session_id=session_id,
            cwd=cwd,
            resume_from_sidecar=resume_from_sidecar,
            surface="a2a",
            backup_service=self._backup_service,
            aliyun_delegated_executor_factory=delegated_factory,
            **create_kwargs,
            mcp_manager=getattr(runtime, "mcp_manager", None),
            mcp_config_warnings=getattr(runtime, "mcp_config_warnings", None),
        )

    def _inspect_pipeline_prerequisite_metadata(
        self,
        *,
        pipeline_name: str,
        cwd: str,
        session_id: str,
        session_storage: SessionStorage,
        resume_from_sidecar: bool,
    ) -> dict[str, Any] | None:
        if resume_from_sidecar:
            sidecar_metadata = self._sidecar_prerequisite_metadata(
                cwd=cwd,
                session_id=session_id,
                session_storage=session_storage,
            )
            if sidecar_metadata is not None:
                return sidecar_metadata

        raw = self._load_pipeline_raw_config(pipeline_name)
        raw_prerequisites = raw.get("prerequisites") or {}
        if not isinstance(raw_prerequisites, dict):
            raw_prerequisites = {}
        raw_feature_flags = raw.get("feature_flags")
        feature_flags = _resolve_feature_flags(raw_feature_flags if isinstance(raw_feature_flags, dict) else None)
        self._apply_persisted_feature_flag_overrides(feature_flags, raw_feature_flags)
        resolution = inspect_prerequisites(raw_prerequisites, feature_flags=feature_flags)
        return resolution.to_metadata()

    @staticmethod
    def _apply_persisted_feature_flag_overrides(
        feature_flags: dict[str, bool],
        raw_feature_flags: Any,
    ) -> None:
        """Layer the persisted「设置/常规」review-step choice into fresh feature flags.

        Precedence: pipeline.yaml default < settings.yml toggle < explicit env var.
        Only touches ``enable_reviewing`` and only when its env var is not explicitly
        set, so an env override (e.g. in tests/CI) keeps priority. Runs on fresh runs
        only — resumes reuse the frozen sidecar metadata and never reach here.
        """
        flag_name = "enable_reviewing"
        if flag_name not in feature_flags:
            return
        spec = raw_feature_flags.get(flag_name) if isinstance(raw_feature_flags, dict) else None
        env_var = spec.get("env") if isinstance(spec, dict) else None
        if env_var and os.environ.get(env_var, "").strip().lower() in ("true", "1", "yes", "false", "0", "no"):
            return
        feature_flags[flag_name] = is_selling_review_step_enabled()

    def _sidecar_prerequisite_metadata(
        self,
        *,
        cwd: str,
        session_id: str,
        session_storage: SessionStorage,
    ) -> dict[str, Any] | None:
        try:
            raw_session_dir = session_storage.session_dir(cwd, session_id)
            meta_path = Path(raw_session_dir) / "pipeline" / "meta.yaml"
            if not meta_path.exists():
                return None
            raw = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.debug("Failed to peek A2A pipeline sidecar prerequisites", exc_info=True)
            return None
        if not isinstance(raw, dict):
            return None
        metadata = raw.get("prerequisites")
        return dict(metadata) if isinstance(metadata, dict) else None

    def _load_pipeline_raw_config(self, pipeline_name: str) -> dict[str, Any]:
        pipeline_dir = discover_pipelines().get(pipeline_name)
        if pipeline_dir is None:
            return {}
        raw = yaml.safe_load((pipeline_dir / "pipeline.yaml").read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}

    def _set_pipeline_telemetry_correlation(self, pipeline: Any, *, task_id: str, context_id: str) -> None:
        set_correlation = getattr(pipeline, "set_telemetry_correlation", None)
        if not callable(set_correlation):
            return
        try:
            set_correlation(task_id=task_id, context_id=context_id, pipeline_run_id=context_id)
        except Exception:
            logger.warning("A2A pipeline telemetry correlation setup failed", exc_info=True)

    def _continue_after_interrupt_stream(self, pipeline: Any, pipeline_input: PipelineUserInput) -> AsyncIterator[Any]:
        continue_after_interrupt = getattr(pipeline, "continue_after_interrupt", None)
        if callable(continue_after_interrupt):
            return continue_after_interrupt()
        return pipeline.run(_pipeline_runner_input(pipeline_input))

    async def _run_outbound_serialized(
        self,
        runtime: Any,
        callback: Callable[[], Awaitable[Any]],
    ) -> Any:
        async with _outbound_lock(runtime):
            outbound = getattr(runtime, "outbound", None)
            if outbound is None:
                return await callback()
            return await outbound.run_serialized(callback)

    async def _run_external_terminal_publication(
        self,
        runtime: Any,
        callback: Callable[[], Awaitable[bool]],
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ACTIVE_INTERRUPT_TERMINAL_WAIT_TIMEOUT_SECONDS
        interrupt_settled = _interrupt_settled_event(runtime)

        while True:
            async with _outbound_lock(runtime):
                if interrupt_settled.is_set() or loop.time() >= deadline:
                    if not interrupt_settled.is_set():
                        logger.warning(
                            "Timed out waiting for active A2A pipeline interrupt before publishing terminal status"
                        )
                    runtime.terminal_publication_started = True
                    try:
                        outbound = getattr(runtime, "outbound", None)
                        if outbound is None:
                            committed = await callback()
                        else:
                            committed = await outbound.run_serialized(callback)
                    except BaseException:
                        runtime.terminal_publication_started = False
                        raise
                    if not committed:
                        runtime.terminal_publication_started = False
                    return committed

            remaining = deadline - loop.time()
            if remaining <= 0:
                continue
            try:
                await asyncio.wait_for(interrupt_settled.wait(), timeout=remaining)
            except TimeoutError:
                pass

    async def _run_terminal_sidecar_recovery_publication(
        self,
        runtime: Any,
        publisher: PipelineA2AEventPublisher,
        pipeline: Any,
        *,
        task_id: str,
        context_id: str,
    ) -> bool:
        result = _TerminalSidecarRecoveryResult(published=False, available=False)

        async def publish_or_confirm() -> bool:
            nonlocal result
            result = await self._publish_terminal_sidecar_recovery_event(
                publisher,
                pipeline,
                task_id=task_id,
                context_id=context_id,
            )
            return result.available

        await self._run_external_terminal_publication(runtime, publish_or_confirm)
        return result.published

    async def _publish_pending_mcp_warnings(
        self,
        *,
        runtime: A2APipelineRuntime,
        outbound: PipelineA2AOutboundQueue | None,
        publisher: PipelineA2AEventPublisher,
    ) -> None:
        if outbound is not None and not _has_unpublished_mcp_warnings(runtime.agent_runtime):
            return

        async def publish() -> None:
            await publish_mcp_warnings(
                publisher.event_queue,
                task_id=publisher.translator.context.task_id,
                context_id=publisher.translator.context.context_id,
                runtime=runtime.agent_runtime,
                iac_code_session_id=publisher.translator.context.iac_code_session_id,
            )

        if outbound is None:
            await publish()
        else:
            await outbound.run_serialized(publish)

    async def _finish_runtime_outbound(
        self,
        runtime: Any,
        outbound: PipelineA2AOutboundQueue,
    ) -> None:
        async with _outbound_lock(runtime):
            try:
                await outbound.close()
            except asyncio.CancelledError:
                raise
            except BaseException:
                if runtime.outbound is outbound:
                    runtime.outbound = None
                raise
            if runtime.outbound is outbound:
                runtime.outbound = None

    async def _abort_runtime_outbound(self, runtime: Any, outbound: PipelineA2AOutboundQueue) -> None:
        await _abort_outbound_worker(outbound)
        async with _outbound_lock(runtime):
            if runtime.outbound is outbound:
                runtime.outbound = None

    async def _consume_stream_until_restart(
        self,
        *,
        stream: AsyncIterator[Any],
        runtime: A2APipelineRuntime,
        publisher: PipelineA2AEventPublisher,
        task: Any,
    ) -> "_StreamConsumeResult":
        had_events = False
        outbound = PipelineA2AOutboundQueue(publisher) if publisher.extreme_performance else None
        outbound_registered = False
        stream_iter = stream.__aiter__()
        restart_event = _restart_requested_event(runtime)
        stream_requests: asyncio.Queue[asyncio.Future[Any]] = asyncio.Queue()
        stream_driver = asyncio.create_task(
            _drive_stream_events(stream_iter, stream_requests),
            name="pipeline-a2a-stream-driver",
        )
        next_event: asyncio.Future[Any] | None = None
        restart_task: asyncio.Task[Any] | None = None
        terminal_handoff_unavailable = False
        stream_exception: BaseException | None = None
        try:
            if outbound is not None:
                await outbound.start()
            async with _outbound_lock(runtime):
                runtime.terminal_publication_started = False
                if outbound is not None:
                    runtime.outbound = outbound
                    outbound_registered = True
            owner_task = asyncio.current_task()
            if owner_task is not None:
                runtime.active_owner_task = owner_task
                task.active_task = owner_task
            runtime.current_stream = stream_iter
            while True:
                if runtime.pause_after_interrupt and restart_event.is_set():
                    restart_event.clear()
                    runtime.pause_after_interrupt = False
                    return _StreamConsumeResult(
                        had_events=had_events,
                        restart_requested=False,
                        terminal_handoff_unavailable=terminal_handoff_unavailable,
                    )
                if runtime.restart_after_interrupt and restart_event.is_set():
                    restart_event.clear()
                    runtime.restart_after_interrupt = False
                    return _StreamConsumeResult(
                        had_events=had_events,
                        restart_requested=True,
                        terminal_handoff_unavailable=terminal_handoff_unavailable,
                    )

                next_event = asyncio.get_running_loop().create_future()
                stream_requests.put_nowait(next_event)
                restart_task = asyncio.create_task(restart_event.wait())
                done, _pending = await asyncio.wait(
                    {next_event, restart_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if restart_task in done and runtime.restart_after_interrupt:
                    restart_event.clear()
                    runtime.restart_after_interrupt = False
                    await _cancel_task_safely(stream_driver)
                    next_event = None
                    await _cancel_task_safely(restart_task)
                    restart_task = None
                    return _StreamConsumeResult(
                        had_events=had_events,
                        restart_requested=True,
                        terminal_handoff_unavailable=terminal_handoff_unavailable,
                    )
                if restart_task in done and runtime.pause_after_interrupt:
                    restart_event.clear()
                    runtime.pause_after_interrupt = False
                    await _cancel_task_safely(stream_driver)
                    next_event = None
                    await _cancel_task_safely(restart_task)
                    restart_task = None
                    return _StreamConsumeResult(
                        had_events=had_events,
                        restart_requested=False,
                        terminal_handoff_unavailable=terminal_handoff_unavailable,
                    )

                await _cancel_task_safely(restart_task)
                restart_task = None
                try:
                    event = await next_event
                except StopAsyncIteration:
                    next_event = None
                    await self._publish_pending_mcp_warnings(
                        runtime=runtime,
                        outbound=outbound,
                        publisher=publisher,
                    )
                    return _StreamConsumeResult(
                        had_events=had_events,
                        restart_requested=False,
                        terminal_handoff_unavailable=terminal_handoff_unavailable,
                    )
                finally:
                    next_event = None

                if _is_pipeline_terminal_stream_event(event):
                    terminal_publication = await self._publish_terminal_stream_event(
                        runtime=runtime,
                        outbound=outbound,
                        publisher=publisher,
                        event=event,
                    )
                    if terminal_publication.interrupt_action == "restart":
                        return _StreamConsumeResult(
                            had_events=had_events,
                            restart_requested=True,
                            terminal_handoff_unavailable=terminal_handoff_unavailable,
                        )
                    if terminal_publication.interrupt_action == "pause":
                        return _StreamConsumeResult(
                            had_events=had_events,
                            restart_requested=False,
                            terminal_handoff_unavailable=terminal_handoff_unavailable,
                        )
                    had_events = True
                    terminal_handoff_result = terminal_publication.handoff
                    assert terminal_handoff_result is not None
                    text = terminal_publication.text
                else:
                    had_events = True
                    await self._publish_pending_mcp_warnings(
                        runtime=runtime,
                        outbound=outbound,
                        publisher=publisher,
                    )
                    terminal_handoff_result = await self._maybe_publish_terminal_with_normal_handoff(
                        runtime.pipeline,
                        publisher,
                        event,
                    )
                    if terminal_handoff_result.attempted:
                        text = None
                    else:
                        if outbound is not None:
                            delivery_text = _text_delta_output(event)
                            await outbound.submit(
                                event,
                                permission_resolver=self._permission_resolver,
                                auto_approve_permissions=self._auto_approve_permissions,
                                after_delivery=(
                                    lambda text=delivery_text: (
                                        task.output_text.append(text) if text is not None else None
                                    )
                                ),
                            )
                            if _ask_user_question_from(event) is not None:
                                await outbound.flush()
                            text = None
                        else:
                            text = await publisher.publish(
                                event,
                                permission_resolver=self._permission_resolver,
                                auto_approve_permissions=self._auto_approve_permissions,
                            )
                        self._track_pending_question(runtime, publisher, event)
                        await self._maybe_publish_normal_handoff_ready(runtime.pipeline, publisher, event)
                if terminal_handoff_result.attempted and not terminal_handoff_result.terminal_available:
                    terminal_handoff_unavailable = True
                if text:
                    task.output_text.append(text)
                if _ask_user_question_from(event) is not None:
                    return _StreamConsumeResult(
                        had_events=had_events,
                        restart_requested=False,
                        terminal_handoff_unavailable=terminal_handoff_unavailable,
                    )
        except asyncio.CancelledError as exc:
            stream_exception = exc
            raise
        except BaseException as exc:
            stream_exception = exc
            raise
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
            await _cancel_task_safely(stream_driver)
            if restart_task is not None:
                await _cancel_task_safely(restart_task)
            if runtime.current_stream is stream_iter:
                runtime.current_stream = None
            if outbound is not None:
                try:
                    if outbound_registered:
                        await self._finish_runtime_outbound(
                            runtime,
                            outbound,
                        )
                    else:
                        await _abort_outbound_worker(outbound)
                except asyncio.CancelledError:
                    await self._abort_runtime_outbound(runtime, outbound)
                    raise
                except BaseException:
                    if stream_exception is None:
                        raise
                    logger.warning("A2A pipeline outbound queue close failed", exc_info=True)

    async def _publish_terminal_stream_event(
        self,
        *,
        runtime: A2APipelineRuntime,
        outbound: PipelineA2AOutboundQueue | None,
        publisher: PipelineA2AEventPublisher,
        event: Any,
    ) -> _TerminalStreamPublishResult:
        async def publish() -> tuple[_TerminalHandoffPublishResult, str | None]:
            await publish_mcp_warnings(
                publisher.event_queue,
                task_id=publisher.translator.context.task_id,
                context_id=publisher.translator.context.context_id,
                runtime=runtime.agent_runtime,
                iac_code_session_id=publisher.translator.context.iac_code_session_id,
            )
            handoff = await self._maybe_publish_terminal_with_normal_handoff(
                runtime.pipeline,
                publisher,
                event,
            )
            if handoff.attempted:
                return handoff, None
            text = await publisher.publish(
                event,
                permission_resolver=self._permission_resolver,
                auto_approve_permissions=self._auto_approve_permissions,
            )
            self._track_pending_question(runtime, publisher, event)
            await self._maybe_publish_normal_handoff_ready(runtime.pipeline, publisher, event)
            return handoff, text

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ACTIVE_INTERRUPT_TERMINAL_WAIT_TIMEOUT_SECONDS
        timed_out = False
        interrupt_settled = _interrupt_settled_event(runtime)
        restart_event = _restart_requested_event(runtime)

        while True:
            interrupt_action = _consume_requested_interrupt_action(runtime)
            if interrupt_action is not None:
                return _TerminalStreamPublishResult(interrupt_action=interrupt_action)

            async with _outbound_lock(runtime):
                interrupt_action = _consume_requested_interrupt_action(runtime)
                if interrupt_action is not None:
                    return _TerminalStreamPublishResult(interrupt_action=interrupt_action)
                if interrupt_settled.is_set() or timed_out:
                    runtime.terminal_publication_started = True
                    if outbound is None:
                        handoff, text = await publish()
                    else:
                        handoff, text = await outbound.run_serialized(publish)
                    return _TerminalStreamPublishResult(handoff=handoff, text=text)

            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "Timed out waiting for active A2A pipeline interrupt before publishing terminal pipeline event"
                )
                timed_out = True
                continue

            settled_task = asyncio.create_task(interrupt_settled.wait())
            restart_task = asyncio.create_task(restart_event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {settled_task, restart_task},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                await _cancel_task_safely(settled_task)
                await _cancel_task_safely(restart_task)
            if not done:
                logger.warning(
                    "Timed out waiting for active A2A pipeline interrupt before publishing terminal pipeline event"
                )
                timed_out = True

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
            iac_code_session_id=session_id,
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
        artifact_store = self._artifact_store_for_session(cwd=cwd, session_id=session_id)
        flow_monitor = _pipeline_flow_monitor_for_session(
            cwd=cwd,
            session_id=session_id,
            context_id=context_id,
            task_id=task_id,
            pipeline_run_id=context.pipeline_run_id,
        )
        return PipelineA2AEventPublisher(
            event_queue=event_queue,
            translator=translator,
            journal=journal,
            snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
            artifact_store=artifact_store,
            exposure_types=self._thinking_exposure_types,
            backup_commit_gate=_requires_backup_committed_publication,
            flow_monitor=flow_monitor,
        )

    def _install_backup_hook(
        self,
        publisher: PipelineA2AEventPublisher,
        *,
        pipeline: Any,
        cwd: str,
        session_id: str,
        task: Any,
        ctx: Any,
    ) -> None:
        async def before_enqueue(envelope: dict[str, Any]) -> bool:
            return await self._backup_before_pipeline_publication(
                envelope,
                publisher=publisher,
                pipeline=pipeline,
                cwd=cwd,
                session_id=session_id,
                task=task,
                ctx=ctx,
            )

        async def after_backup_commit(envelope: dict[str, Any]) -> None:
            self._mirror_a2a_snapshots_for_pipeline_publication(envelope, task=task, ctx=ctx)

        publisher.before_enqueue = before_enqueue
        publisher.after_backup_commit = after_backup_commit

    async def _backup_before_pipeline_publication(
        self,
        envelope: dict[str, Any],
        *,
        publisher: PipelineA2AEventPublisher,
        pipeline: Any,
        cwd: str,
        session_id: str,
        task: Any,
        ctx: Any,
    ) -> bool:
        reason = _backup_reason_for_pipeline_envelope(envelope)
        if reason is None:
            return True
        if reason in {BackupReason.TERMINAL, BackupReason.HANDOFF_READY}:
            if _is_pending_backup_publication_event(envelope):
                return True
        else:
            self._mirror_a2a_snapshots_for_pipeline_publication(envelope, task=task, ctx=ctx)
        publication_proofs = None
        if reason == BackupReason.HANDOFF_READY and envelope.get("visibility") == "committed":
            publication_proofs = {
                NORMAL_HANDOFF_PROOF_KEY: BackupPublicationProof.from_envelope(envelope),
            }
        try:
            await backup_session_async(
                self._backup_service,
                cwd,
                session_id,
                reason=reason,
                critical=True,
                metrics=self._metrics,
                publication_proofs=publication_proofs,
            )
        except SessionBackupBlocked as exc:
            sidecar_synced = await _sync_pipeline_backup_blocked_sidecar(
                pipeline,
                reason=reason,
                step_id=_pipeline_step_id_from_envelope(envelope),
            )
            if not sidecar_synced:
                logger.warning("A2A pipeline backup_blocked sidecar state was not durably persisted")
                _record_backup_blocked_metric(self._metrics, reason=reason.value, recoverable=False)
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=envelope,
                    reason="backup_blocked_sidecar_persist_failed",
                )
                task.state = TASK_STATE_INPUT_REQUIRED
                task.touch()
                ctx.touch()
                self._task_store.mirror_task(task)
                self._task_store.mirror_context(ctx)
                raise _PipelineBackupBlockedTransitionError from exc
            backup_blocked_published = await self._publish_backup_blocked(
                publisher,
                reason=reason,
                exc=exc,
            )
            if not backup_blocked_published:
                logger.warning("A2A pipeline backup_blocked event was not durably published")
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=envelope,
                    reason="backup_blocked_publish_failed",
                )
            task.state = TASK_STATE_INPUT_REQUIRED
            task.touch()
            ctx.touch()
            self._task_store.mirror_task(task)
            self._task_store.mirror_context(ctx)
            raise _PipelineBackupBlockedTransitionError from exc
        return True

    def _mirror_a2a_snapshots_for_pipeline_publication(
        self,
        envelope: dict[str, Any],
        *,
        task: Any,
        ctx: Any,
    ) -> None:
        state = _task_state_for_pipeline_publication_envelope(envelope)
        if state == TASK_STATE_INPUT_REQUIRED:
            task_snapshot = copy.copy(task)
            task_snapshot.state = state
            context_snapshot = copy.copy(ctx)
            context_snapshot.active_task_id = None
            self._task_store.mirror_task(task_snapshot)
            self._task_store.mirror_context(context_snapshot)
            return

        task.state = state
        if task.state in {TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_CANCELED}:
            ctx.active_task_id = None
        task.touch()
        ctx.touch()
        self._task_store.mirror_task(task)
        self._task_store.mirror_context(ctx)

    async def _publish_backup_blocked(
        self,
        publisher: PipelineA2AEventPublisher,
        *,
        reason: BackupReason,
        exc: SessionBackupBlocked,
    ) -> bool:
        try:
            published = await publisher.publish_manual(
                "backup_blocked",
                "pipeline",
                status="input_required",
                data={
                    "reason": reason.value,
                    "error": _format_exception(exc),
                    "recoverable": True,
                },
                require_durable_metadata=True,
                require_journal_metadata=True,
            )
            if published is not None:
                _record_backup_blocked_metric(self._metrics, reason=reason.value, recoverable=True)
                return True
            _record_backup_blocked_metric(self._metrics, reason=reason.value, recoverable=False)
            return False
        except Exception as publish_exc:
            logger.warning(
                "Failed to publish A2A pipeline backup_blocked event error_type=%s",
                type(publish_exc).__name__,
            )
            _record_backup_blocked_metric(self._metrics, reason=reason.value, recoverable=False)
            return False

    async def _complete_backup_blocked_transition(self, *, task: Any, ctx: Any) -> None:
        task.state = TASK_STATE_INPUT_REQUIRED
        task.touch()
        ctx.touch()
        self._task_store.mirror_task(task)
        self._task_store.mirror_context(ctx)
        await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
        self._metrics.record_executor_error()

    def _artifact_store_for_session(self, *, cwd: str, session_id: str) -> Any | None:
        session_dir = SessionStorage().v2_session_dir(cwd, session_id)
        if session_dir is None:
            return self._artifact_store
        return artifact_store_for_session(session_dir)

    async def _select_stream(
        self,
        pipeline: Any,
        prompt: str,
        *,
        pipeline_input: PipelineUserInput,
        publisher: PipelineA2AEventPublisher,
        task_id: str,
        context_id: str,
        fresh_pipeline_factory: Callable[[], Any],
    ) -> _SelectedPipelineStream:
        status = getattr(pipeline, "sidecar_status", None)
        pending_backup_blocked = _pending_backup_blocked_input_from_sidecar(
            publisher,
            task_id=task_id,
            context_id=context_id,
        )
        if pending_backup_blocked is not None and status != "backup_blocked":
            sidecar_synced = await _sync_pipeline_backup_blocked_sidecar(
                pipeline,
                reason=_backup_reason_from_pending_backup_blocked_input(pending_backup_blocked),
                step_id=_pipeline_step_id_from_pending_input(pending_backup_blocked),
            )
            if not sidecar_synced:
                raise _active_sidecar_mismatch_error_from_publisher(
                    publisher,
                    context_id=context_id,
                    sidecar_status="backup_blocked",
                )
            status = getattr(pipeline, "sidecar_status", status)
        if status == "waiting_input":
            _raise_if_sidecar_restore_failed(pipeline, status)
            if not _sidecar_matches_task(publisher, task_id=task_id, context_id=context_id, sidecar_status=status):
                raise _active_sidecar_mismatch_error_from_publisher(
                    publisher,
                    context_id=context_id,
                    sidecar_status=status,
                )
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
                        pipeline_input=pipeline_input,
                    ),
                )
            pending_pause = _pending_pipeline_pause_input_from_sidecar(
                publisher,
                task_id=task_id,
                context_id=context_id,
            )
            if pending_pause is not None:
                stream = (
                    pipeline.continue_from_sidecar(user_input=_pipeline_runner_input(pipeline_input))
                    if prompt
                    else pipeline.continue_from_sidecar()
                )
                return _SelectedPipelineStream(pipeline=pipeline, stream=stream)
            return _SelectedPipelineStream(
                pipeline=pipeline,
                stream=pipeline.resume(_pipeline_runner_input(pipeline_input)),
            )
        if status == "running":
            _raise_if_sidecar_restore_failed(pipeline, status)
            if not _sidecar_matches_task(publisher, task_id=task_id, context_id=context_id, sidecar_status=status):
                raise _active_sidecar_mismatch_error_from_publisher(
                    publisher,
                    context_id=context_id,
                    sidecar_status=status,
                )
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
                        pipeline_input=pipeline_input,
                    ),
                )
            pending_pause = _pending_pipeline_pause_input_from_sidecar(
                publisher,
                task_id=task_id,
                context_id=context_id,
            )
            if pending_pause is not None:
                stream = (
                    pipeline.continue_from_sidecar(user_input=_pipeline_runner_input(pipeline_input))
                    if prompt
                    else pipeline.continue_from_sidecar()
                )
                return _SelectedPipelineStream(pipeline=pipeline, stream=stream)
            if prompt:
                return _SelectedPipelineStream(
                    pipeline=pipeline,
                    stream=pipeline.continue_from_sidecar(user_input=_pipeline_runner_input(pipeline_input)),
                )
            return _SelectedPipelineStream(pipeline=pipeline, stream=pipeline.continue_from_sidecar())
        if status == "backup_blocked":
            _raise_if_sidecar_restore_failed(pipeline, status)
            if not _sidecar_matches_task(publisher, task_id=task_id, context_id=context_id, sidecar_status=status):
                raise _active_sidecar_mismatch_error_from_publisher(
                    publisher,
                    context_id=context_id,
                    sidecar_status=status,
                )
            stream = (
                pipeline.continue_from_sidecar(user_input=_pipeline_runner_input(pipeline_input))
                if prompt
                else pipeline.continue_from_sidecar()
            )
            return _SelectedPipelineStream(pipeline=pipeline, stream=stream)
        if status in _TERMINAL_SIDECAR_STATUSES:
            if _terminal_sidecar_matches_task(publisher, status, task_id=task_id, context_id=context_id):
                return _SelectedPipelineStream(pipeline=pipeline, stream=_empty_stream())
            pipeline = self._fresh_pipeline_after_sidecar_mismatch(pipeline, fresh_pipeline_factory)
            return _SelectedPipelineStream(
                pipeline=pipeline,
                stream=pipeline.run(_pipeline_runner_input(pipeline_input)),
            )
        return _SelectedPipelineStream(
            pipeline=pipeline,
            stream=pipeline.run(_pipeline_runner_input(pipeline_input)),
        )

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
    ) -> _TerminalSidecarRecoveryResult:
        sidecar_status = getattr(pipeline, "sidecar_status", None)
        terminal_event = _terminal_event_from_sidecar_status(sidecar_status)
        if terminal_event is None:
            return _TerminalSidecarRecoveryResult(published=False, available=False)
        event_type, status = terminal_event
        snapshot = publisher.snapshot_store.load()
        journal_events = _safe_read_pipeline_journal(publisher.journal)
        scoped_journal_events = _events_for_task_context(journal_events, task_id=task_id, context_id=context_id)
        if _terminal_publication_unavailable_blocks_recovery(scoped_journal_events):
            return _TerminalSidecarRecoveryResult(published=False, available=False)
        existing_terminal_event = _latest_terminal_a2a_event(scoped_journal_events)
        if existing_terminal_event is not None:
            existing_status = _terminal_status_from_a2a_event(existing_terminal_event)
            if existing_status != status:
                self._rebuild_terminal_recovery_snapshot(publisher, scoped_journal_events)
                return _TerminalSidecarRecoveryResult(published=False, available=False)
            if _terminal_snapshot_needs_journal_rebuild(
                snapshot,
                scoped_journal_events,
                status,
                task_id=task_id,
                context_id=context_id,
            ):
                self._rebuild_terminal_recovery_snapshot(publisher, scoped_journal_events)
            return _TerminalSidecarRecoveryResult(published=False, available=True)
        if _snapshot_has_conflicting_terminal_status(snapshot, status, task_id=task_id, context_id=context_id):
            return _TerminalSidecarRecoveryResult(published=False, available=False)
        if not _terminal_snapshot_needs_recovery_event(
            snapshot,
            status,
            task_id=task_id,
            context_id=context_id,
        ) and not _has_unacknowledged_committed_terminal_event(scoped_journal_events):
            return _TerminalSidecarRecoveryResult(published=False, available=True)

        published = await publisher.publish_manual(
            event_type,
            "pipeline",
            status=status,
            data={
                "sidecarStatus": sidecar_status,
                "recovered": True,
            },
        )
        if published is None:
            await self._persist_terminal_publication_unavailable(
                publisher,
                terminal_envelope={
                    "eventType": event_type,
                    "status": status,
                    "visibility": _COMMITTED_BACKUP_VISIBILITY,
                },
                reason="terminal_recovery_publication_failed",
            )
            return _TerminalSidecarRecoveryResult(published=False, available=False)
        # 正常完成路径会在补发 terminal 的同时补发 normal handoff(驱动「↪ 普通对话」边界标记);
        # 重启恢复路径此前只补 terminal,导致交接标记缺失(Issue 4)。这里同样补发,幂等:仅当
        # 作用域内尚无 pipeline_handoff_ready 时才发,是否真正切普通对话仍由 pipeline 的
        # on_complete_policy 决定(cancel 等不在 apply_on 的终态自然不发)。
        if not _handoff_ready_present(scoped_journal_events):
            await self._publish_normal_handoff_ready(
                pipeline,
                publisher,
                _completed_event_data_from_sidecar_status(sidecar_status),
            )
        return _TerminalSidecarRecoveryResult(published=True, available=True)

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
            published = await publisher.publish_manual(event_type, "pipeline", status=status, data=data)
            if published is None:
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope={
                        "eventType": event_type,
                        "status": status,
                        "visibility": _COMMITTED_BACKUP_VISIBILITY,
                    },
                    reason="terminal_publication_failed",
                )
                return False
            return True
        except _PipelineBackupBlockedTransitionError:
            raise
        except Exception:
            logger.warning("Failed to publish A2A pipeline terminal event", exc_info=True)
            return False

    async def _maybe_publish_terminal_with_normal_handoff(
        self,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        event: Any,
    ) -> _TerminalHandoffPublishResult:
        if not isinstance(event, PipelineEvent) or event.type != PipelineEventType.PIPELINE_COMPLETED:
            return _TerminalHandoffPublishResult(attempted=False, terminal_available=False)

        publication = self._normal_handoff_publication(pipeline, event.data or {})
        if publication is None:
            return _TerminalHandoffPublishResult(attempted=False, terminal_available=False)

        terminal_envelopes = publisher.translator.translate(event)
        terminal_envelope = next(
            (
                envelope
                for envelope in terminal_envelopes
                if _backup_reason_for_pipeline_envelope(envelope) == BackupReason.TERMINAL
            ),
            None,
        )
        if terminal_envelope is None:
            logger.warning("Skipping A2A pipeline handoff transaction because terminal envelope was not translated")
            return _TerminalHandoffPublishResult(attempted=True, terminal_available=False)

        terminal_available = await self._publish_terminal_handoff_transaction(
            pipeline,
            publisher,
            terminal_envelope=terminal_envelope,
            publication=publication,
        )
        return _TerminalHandoffPublishResult(attempted=True, terminal_available=terminal_available)

    async def _publish_manual_terminal_with_normal_handoff(
        self,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        *,
        event_type: str,
        status: str,
        terminal_data: dict[str, Any],
        handoff_data: dict[str, Any],
    ) -> _TerminalHandoffPublishResult:
        publication = self._normal_handoff_publication(pipeline, handoff_data)
        if publication is None:
            return _TerminalHandoffPublishResult(attempted=False, terminal_available=False)
        terminal_envelope = publisher.translator.manual_event(
            event_type,
            "pipeline",
            status=status,
            data=terminal_data,
        )
        terminal_available = await self._publish_terminal_handoff_transaction(
            pipeline,
            publisher,
            terminal_envelope=terminal_envelope,
            publication=publication,
        )
        return _TerminalHandoffPublishResult(attempted=True, terminal_available=terminal_available)

    async def _publish_terminal_handoff_transaction(
        self,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        *,
        terminal_envelope: dict[str, Any],
        publication: _NormalHandoffPublication,
    ) -> bool:
        handoff_envelope = publisher.translator.manual_event(
            "pipeline_handoff_ready",
            "pipeline",
            status=publication.status,
            data=publication.data,
        )
        pending_terminal_envelope = _pending_backup_publication_envelope(terminal_envelope)
        pending_handoff_envelope = _pending_backup_publication_envelope(handoff_envelope)
        pending_terminal_safe_envelope = await publisher.persist_envelope(
            pending_terminal_envelope,
            require_journal_metadata=True,
        )
        if pending_terminal_safe_envelope is None:
            await self._persist_terminal_publication_unavailable(
                publisher,
                terminal_envelope=terminal_envelope,
                reason="pending_terminal_persist_failed",
            )
            return False
        pending_handoff_safe_envelope = await publisher.persist_envelope(
            pending_handoff_envelope,
            require_journal_metadata=True,
        )
        if pending_handoff_safe_envelope is None:
            await self._persist_terminal_publication_unavailable(
                publisher,
                terminal_envelope=pending_terminal_safe_envelope,
                reason="pending_handoff_persist_failed",
            )
            return False

        async with publisher.delivery_transaction():
            committed_terminal_envelope = _committed_backup_publication_envelope(
                publisher,
                pending_terminal_safe_envelope,
            )
            committed_handoff_envelope = _committed_backup_publication_envelope(
                publisher,
                pending_handoff_safe_envelope,
            )
            terminal_safe_envelope = await publisher.persist_envelope(
                committed_terminal_envelope,
                require_journal_metadata=True,
            )
            if terminal_safe_envelope is None:
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=committed_terminal_envelope,
                    reason="committed_terminal_persist_failed",
                )
                return False
            handoff_safe_envelope = await publisher.persist_envelope(
                committed_handoff_envelope,
                require_journal_metadata=True,
            )
            if handoff_safe_envelope is None:
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=terminal_safe_envelope,
                    reason="committed_handoff_persist_failed",
                )
                return False
            if not await self._run_before_enqueue_hook(publisher, terminal_safe_envelope):
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=terminal_safe_envelope,
                    reason="committed_terminal_before_enqueue_failed",
                )
                return False
            if not await self._run_before_enqueue_hook(publisher, handoff_safe_envelope):
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=terminal_safe_envelope,
                    reason="committed_handoff_before_enqueue_failed",
                )
                return False
            terminal_ack_envelope = await publisher.persist_backup_committed_ack(terminal_safe_envelope)
            if terminal_ack_envelope is None:
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=terminal_safe_envelope,
                    reason="committed_terminal_backup_ack_failed",
                )
                return False
            handoff_ack_envelope = await publisher.persist_backup_committed_ack(handoff_safe_envelope)
            if not await publisher.enqueue_persisted(terminal_safe_envelope, run_before_enqueue=False):
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=terminal_safe_envelope,
                    reason="committed_terminal_enqueue_failed",
                )
                return False
            if not await publisher.enqueue_persisted(
                backup_committed_delivery_envelope(terminal_ack_envelope, terminal_safe_envelope),
                run_before_enqueue=False,
            ):
                await self._persist_terminal_publication_unavailable(
                    publisher,
                    terminal_envelope=terminal_safe_envelope,
                    reason="committed_terminal_backup_ack_enqueue_failed",
                )
                return False
            await publisher._run_after_backup_commit_hook(terminal_safe_envelope)
            if handoff_ack_envelope is None:
                await self._persist_and_enqueue_handoff_publication_unavailable(
                    publisher,
                    handoff_envelope=handoff_safe_envelope,
                    reason="committed_handoff_backup_ack_failed",
                )
                return True
            if await publisher.enqueue_persisted(
                handoff_safe_envelope,
                run_before_enqueue=False,
                # loopback web 主转录 translator 只从 enqueue_local_pipeline_envelope 收信封;
                # 缺了这行 handoff 就只落远程/journal,实时跑看不到「↪ 普通对话」/结局彩条,
                # 只有 reload 从 journal 重建才出现。传 committed(非 safe/脱敏)信封,与
                # ``_persist_backup_gated_publication`` 的直通语义一致(未脱敏本地路径供 web 用)。
                local_envelope=committed_handoff_envelope,
            ):
                if not await publisher.enqueue_persisted(
                    backup_committed_delivery_envelope(handoff_ack_envelope, handoff_safe_envelope),
                    run_before_enqueue=False,
                ):
                    await self._persist_and_enqueue_handoff_publication_unavailable(
                        publisher,
                        handoff_envelope=handoff_safe_envelope,
                        reason="committed_handoff_backup_ack_enqueue_failed",
                    )
                    return True
                await publisher._run_after_backup_commit_hook(handoff_safe_envelope)
                _persist_normal_handoff_summary(pipeline, publication.summary)
            else:
                await self._persist_handoff_publication_unavailable(
                    publisher,
                    handoff_envelope=handoff_safe_envelope,
                    reason="committed_handoff_enqueue_failed",
                )
                return True
        return True

    async def _persist_terminal_publication_unavailable(
        self,
        publisher: PipelineA2AEventPublisher,
        *,
        terminal_envelope: dict[str, Any],
        reason: str,
    ) -> None:
        marker = publisher.translator.manual_event(
            "input_required",
            "pipeline",
            status="input_required",
            data={
                "kind": _TERMINAL_PUBLICATION_UNAVAILABLE_KIND,
                "reason": reason,
                "terminalEventId": terminal_envelope.get("eventId"),
                "terminalEventType": terminal_envelope.get("eventType"),
                "terminalVisibility": _publication_visibility_from_event(terminal_envelope),
            },
        )
        persisted = await publisher.persist_envelope(
            marker,
            require_durable_metadata=True,
            require_journal_metadata=True,
        )
        if persisted is None:
            logger.warning("Failed to persist A2A pipeline terminal publication unavailable marker")

    async def _persist_and_enqueue_handoff_publication_unavailable(
        self,
        publisher: PipelineA2AEventPublisher,
        *,
        handoff_envelope: dict[str, Any],
        reason: str,
    ) -> None:
        persisted = await self._persist_handoff_publication_unavailable(
            publisher,
            handoff_envelope=handoff_envelope,
            reason=reason,
        )
        if persisted is None:
            return
        if not await publisher.enqueue_persisted(persisted, run_before_enqueue=False):
            logger.warning("Failed to enqueue A2A pipeline handoff publication unavailable marker")

    async def _persist_handoff_publication_unavailable(
        self,
        publisher: PipelineA2AEventPublisher,
        *,
        handoff_envelope: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        raw_data = handoff_envelope.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        marker = publisher.translator.manual_event(
            "pipeline_handoff_ready",
            "pipeline",
            status=str(handoff_envelope.get("status") or "input_required"),
            data={
                "action": _HANDOFF_PUBLICATION_UNAVAILABLE_ACTION,
                "targetMode": "pipeline",
                "outcome": data.get("outcome"),
                "summary": data.get("summary"),
                "unavailable": True,
                "reason": reason,
                "handoffEventId": handoff_envelope.get("eventId"),
            },
        )
        persisted = await publisher.persist_envelope(
            marker,
            require_durable_metadata=True,
            require_journal_metadata=True,
        )
        if persisted is None:
            logger.warning("Failed to persist A2A pipeline handoff publication unavailable marker")
        return persisted

    async def _run_before_enqueue_hook(
        self,
        publisher: PipelineA2AEventPublisher,
        envelope: dict[str, Any],
    ) -> bool:
        if publisher.before_enqueue is None:
            return True
        should_enqueue = publisher.before_enqueue(envelope)
        if inspect.isawaitable(should_enqueue):
            should_enqueue = await should_enqueue
        return should_enqueue is not False

    async def _maybe_publish_normal_handoff_ready(
        self,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        event: Any,
    ) -> None:
        if not isinstance(event, PipelineEvent) or event.type != PipelineEventType.PIPELINE_COMPLETED:
            return

        await self._publish_normal_handoff_ready(pipeline, publisher, event.data or {})

    def _normal_handoff_publication(
        self,
        pipeline: Any,
        event_data: dict[str, Any],
    ) -> _NormalHandoffPublication | None:
        should_switch_to_normal = getattr(pipeline, "should_switch_to_normal", None)
        if not callable(should_switch_to_normal):
            return None
        try:
            if not bool(should_switch_to_normal(event_data)):
                return None
            summary = pipeline.build_normal_handoff_summary(event_data)
            outcome = terminal_outcome_from_completed_event(event_data)
        except _PipelineBackupBlockedTransitionError:
            raise
        except Exception:
            logger.warning("Failed to build A2A pipeline normal handoff event", exc_info=True)
            return None

        data = {
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": outcome,
            "summary": summary,
        }
        cleanup = _pipeline_cleanup_handoff_data(pipeline)
        if cleanup is not None:
            data["cleanup"] = cleanup
        return _NormalHandoffPublication(
            status=_handoff_status_from_outcome(outcome),
            data=data,
            summary=summary,
        )

    async def _publish_normal_handoff_ready(
        self,
        pipeline: Any,
        publisher: PipelineA2AEventPublisher,
        event_data: dict[str, Any],
    ) -> None:
        publication = self._normal_handoff_publication(pipeline, event_data)
        if publication is None:
            return

        try:
            published = await publisher.publish_manual(
                "pipeline_handoff_ready",
                "pipeline",
                status=publication.status,
                data=publication.data,
            )
        except _PipelineBackupBlockedTransitionError:
            raise
        if published is not None:
            _persist_normal_handoff_summary(pipeline, publication.summary)

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

    async def _route_pending_question_answer(self, runtime: Any, pipeline_input: PipelineUserInput) -> str:
        pipeline_input = normalize_pipeline_user_input(pipeline_input)
        prompt = pipeline_input.display_text
        pending = getattr(runtime, "pending_question", None)
        if not isinstance(pending, _PendingAskUserQuestion):
            return _PENDING_QUESTION_NOT_ROUTED

        question = pending.event
        future = question.response_future
        if future is None or future.done():
            runtime.pending_question = None
            return _PENDING_QUESTION_STALE_FINISHED

        publisher = getattr(runtime, "publisher", None)
        publish_manual = getattr(publisher, "publish_manual", None)
        if not callable(publish_manual):
            return _PENDING_QUESTION_NOT_ROUTED

        answer = _ask_user_question_answer_from_prompt(question, prompt)
        published = await publish_manual(
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
                **_ask_user_question_echo(pending.envelope.get("data")),
            },
            coordinates=_coordinates_from_envelope(pending.envelope),
        )
        if published is None:
            return _PENDING_QUESTION_NOT_ROUTED

        if pipeline_input.has_images:
            inject_pending_question_supplement = getattr(
                getattr(runtime, "pipeline", None),
                "inject_pending_question_supplement",
                None,
            )
            if callable(inject_pending_question_supplement):
                try:
                    injected = inject_pending_question_supplement(pipeline_input.content, envelope=pending.envelope)
                    if inspect.isawaitable(injected):
                        injected = await injected
                except Exception:
                    await self._restore_pending_question_input_required(runtime, pending)
                    raise
                if injected is False:
                    await self._restore_pending_question_input_required(runtime, pending)
                    raise RuntimeError("A2A ask_user_question image supplement could not be delivered.")
            else:
                await self._restore_pending_question_input_required(runtime, pending)
                raise RuntimeError("A2A pipeline cannot accept ask_user_question image supplement.")
        future.set_result(answer)
        runtime.pending_question = None
        return _PENDING_QUESTION_ANSWERED

    async def _restore_pending_question_input_required(self, runtime: Any, pending: "_PendingAskUserQuestion") -> None:
        publisher = getattr(runtime, "publisher", None)
        publish_manual = getattr(publisher, "publish_manual", None)
        if not callable(publish_manual):
            return
        question = pending.event
        envelope = pending.envelope if isinstance(pending.envelope, dict) else {}
        data = {
            "kind": "ask_user_question",
            "inputId": _pending_input_id(envelope, question),
            "toolUseId": question.tool_use_id,
            "question": question.question,
            "prompt": question.question,
            "options": question.options if isinstance(question.options, list) else [],
            "allowFreeText": question.allow_free_text,
            "freeTextPrompt": question.free_text_prompt,
            "required": True,
        }
        await publish_manual(
            "input_required",
            str(envelope.get("scope") or "pipeline"),
            status="input_required",
            data=data,
            coordinates=_coordinates_from_envelope(envelope),
        )

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
        pipeline_runtime: A2APipelineRuntime | None = None,
    ) -> None:
        if isinstance(exc, PipelineTransportDeliveryClosedError) and pipeline_publisher is not None:
            committed_status = _committed_terminal_status_for_task_context(
                pipeline_publisher,
                task_id=task_id,
                context_id=context_id,
            )
            if committed_status is not None:
                logger.info(
                    "Preserving committed A2A pipeline terminal after transport closed status=%s",
                    committed_status,
                )
                if not preserve_task_record:
                    task.state = _task_state_from_a2a_status(committed_status)
                    self._task_store.mirror_task(task)
                    await self._notify_terminal_task(
                        task_id=task.task_id,
                        context_id=task.context_id,
                        state=task.state,
                    )
                    self._record_state(task.state)
                return

        retryable = _is_retryable_executor_error(exc)
        task_state = TASK_STATE_INPUT_REQUIRED if retryable else TASK_STATE_FAILED
        text = _retry_text() if retryable else _format_exception(exc)
        failure = None if retryable else public_error(message=text, error_type=type(exc).__name__)
        terminal_status_available = retryable or preserve_task_record
        if not retryable and not preserve_task_record:

            async def publish_failed_terminal() -> bool:
                return await self._publish_pipeline_terminal_event(
                    pipeline_publisher,
                    event_type="pipeline_failed",
                    status="failed",
                    data={
                        "source": "executor",
                        "errorSummary": text,
                        "errorDetails": _public_error_details_for_a2a(failure.details) if failure is not None else {},
                    },
                )

            if pipeline_runtime is not None and pipeline_publisher is not None:
                terminal_status_available = await self._run_external_terminal_publication(
                    pipeline_runtime,
                    publish_failed_terminal,
                )
            else:
                terminal_status_available = await publish_failed_terminal()
        if not terminal_status_available:
            task_state = TASK_STATE_INPUT_REQUIRED
        await self._publish_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=_a2a_state_from_task_state(task_state),
            text=text,
        )
        if not preserve_task_record:
            task.state = task_state
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
        self._metrics.record_executor_error()
        if not retryable and not preserve_task_record and terminal_status_available:
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


def _pipeline_runner_input(pipeline_input: PipelineUserInput) -> PipelineUserInput | str:
    return pipeline_input if pipeline_input.has_images else pipeline_input.display_text


def _backup_reason_for_pipeline_envelope(envelope: dict[str, Any]) -> BackupReason | None:
    event_type = envelope.get("eventType")
    status = envelope.get("status")
    if event_type == "backup_blocked":
        return None
    if event_type == "pipeline_handoff_ready":
        return BackupReason.HANDOFF_READY
    if event_type in {"pipeline_completed", "pipeline_failed", "pipeline_canceled"}:
        return BackupReason.TERMINAL
    if status in _TERMINAL_A2A_STATUSES:
        return BackupReason.TERMINAL
    if event_type == "input_required" or status in _WAITING_A2A_STATUSES:
        return BackupReason.WAITING_INPUT if status == "waiting_input" else BackupReason.INPUT_REQUIRED
    return None


def _backup_reason_from_pending_backup_blocked_input(pending_input: dict[str, Any]) -> BackupReason:
    backup_blocked = pending_input.get("backupBlocked")
    raw_reason = backup_blocked.get("reason") if isinstance(backup_blocked, dict) else pending_input.get("reason")
    if isinstance(raw_reason, BackupReason):
        return raw_reason
    try:
        return BackupReason(str(raw_reason))
    except ValueError:
        return BackupReason.PIPELINE_STEP_COMPLETED


def _pipeline_step_id_from_envelope(envelope: dict[str, Any]) -> str | None:
    step = envelope.get("step")
    if not isinstance(step, dict):
        return None
    step_id = _string_value(step.get("id") or step.get("stepId") or step.get("step_id"))
    return step_id or None


def _pipeline_step_id_from_pending_input(pending_input: dict[str, Any]) -> str | None:
    step = pending_input.get("step")
    if not isinstance(step, dict):
        return None
    step_id = _string_value(step.get("id") or step.get("stepId") or step.get("step_id"))
    return step_id or None


async def _sync_pipeline_backup_blocked_sidecar(
    pipeline: Any,
    *,
    reason: BackupReason,
    step_id: str | None,
) -> bool:
    save_backup_blocked_sidecar = getattr(pipeline, "_save_backup_blocked_sidecar", None)
    if callable(save_backup_blocked_sidecar):
        try:
            result = save_backup_blocked_sidecar(step_id, reason)
            if inspect.isawaitable(result):
                result = await result
            return result is not False
        except Exception as exc:
            logger.warning(
                "Failed to sync pipeline backup_blocked sidecar state error_type=%s",
                type(exc).__name__,
            )
            return False
    try:
        setattr(pipeline, "sidecar_status", "backup_blocked")
        return True
    except Exception as exc:
        logger.warning(
            "Failed to mark pipeline backup_blocked sidecar status error_type=%s",
            type(exc).__name__,
        )
        return False


def _record_backup_blocked_metric(metrics: Any, *, reason: str, recoverable: bool) -> None:
    record_backup_blocked = getattr(metrics, "record_backup_blocked", None)
    if not callable(record_backup_blocked):
        return
    try:
        record_backup_blocked(reason=reason, recoverable=recoverable)
    except Exception as exc:
        logger.debug("Failed to record A2A backup_blocked metric error_type=%s", type(exc).__name__)


def _record_backup_succeeded_metric(metrics: Any, *, reason: str, critical: bool, retry_count: int) -> None:
    record_backup_succeeded = getattr(metrics, "record_backup_succeeded", None)
    if not callable(record_backup_succeeded):
        return
    try:
        record_backup_succeeded(reason=reason, critical=critical, retry_count=retry_count)
    except Exception as exc:
        logger.debug("Failed to record A2A backup_succeeded metric error_type=%s", type(exc).__name__)


def _record_backup_failed_metric(metrics: Any, *, reason: str, critical: bool, retry_count: int) -> None:
    record_backup_failed = getattr(metrics, "record_backup_failed", None)
    if not callable(record_backup_failed):
        return
    try:
        record_backup_failed(reason=reason, critical=critical, retry_count=retry_count)
    except Exception as exc:
        logger.debug("Failed to record A2A backup_failed metric error_type=%s", type(exc).__name__)


def _backup_retry_count(result: Any) -> int:
    value = getattr(result, "retry_count", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _backup_retry_count_from_exception(exc: BaseException) -> int:
    value = getattr(exc, "retry_count", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _requires_backup_committed_publication(envelope: dict[str, Any]) -> bool:
    if _publication_visibility_from_event(envelope) in {_PENDING_BACKUP_VISIBILITY, _COMMITTED_BACKUP_VISIBILITY}:
        return False
    return _backup_reason_for_pipeline_envelope(envelope) in {BackupReason.TERMINAL, BackupReason.HANDOFF_READY}


def _pending_backup_publication_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    return pending_backup_publication_envelope(envelope)


def _committed_backup_publication_envelope(
    publisher: PipelineA2AEventPublisher,
    pending_envelope: dict[str, Any],
) -> dict[str, Any]:
    return _committed_backup_publication_envelope_from_translator(publisher.translator, pending_envelope)


def _committed_backup_publication_envelope_from_translator(
    translator: PipelineEventTranslator,
    pending_envelope: dict[str, Any],
) -> dict[str, Any]:
    return committed_backup_publication_envelope(translator, pending_envelope)


def _is_pending_backup_publication_event(event: dict[str, Any]) -> bool:
    raw_data = event.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    return _publication_visibility_from_event(event) == _PENDING_BACKUP_VISIBILITY or data.get("backupPending") is True


def _is_committed_backup_publication_event(event: dict[str, Any]) -> bool:
    return _publication_visibility_from_event(event) == _COMMITTED_BACKUP_VISIBILITY


def _publication_visibility_from_event(event: dict[str, Any]) -> str | None:
    visibility = event.get("visibility")
    if isinstance(visibility, str):
        return visibility
    raw_data = event.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    data_visibility = data.get("visibility")
    return data_visibility if isinstance(data_visibility, str) else None


def _task_state_for_pipeline_publication_envelope(envelope: dict[str, Any]) -> str:
    event_type = envelope.get("eventType")
    if event_type == "pipeline_completed":
        return TASK_STATE_COMPLETED
    if event_type == "pipeline_failed":
        return TASK_STATE_FAILED
    if event_type == "pipeline_canceled":
        return TASK_STATE_CANCELED
    return _task_state_from_a2a_status(envelope.get("status"))


async def _resume_pending_ask_user_question_stream(
    *,
    pipeline: Any,
    publisher: PipelineA2AEventPublisher,
    pending_input: dict[str, Any],
    prompt: str,
    pipeline_input: PipelineUserInput,
) -> AsyncIterator[Any]:
    pipeline_input = normalize_pipeline_user_input(pipeline_input)
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
            **_ask_user_question_echo(pending_input),
        },
        coordinates=_coordinates_from_pending_input(pending_input),
    )
    if published is None:
        raise RuntimeError("Failed to persist pending ask_user_question answer.")

    parameters = inspect.signature(resume_ask_user_question).parameters
    resume_kwargs: dict[str, Any] = {"tool_use_id": tool_use_id}
    if pipeline_input.has_images:
        resume_kwargs["supplemental_input"] = pipeline_input
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


def _ask_user_question_echo(source: Any) -> dict[str, Any]:
    """Non-private fields echoed from an ask_user_question ``input_required``
    onto its ``input_received`` so the answered envelope is self-contained.

    The web transcript rebuilds an answered ask card at ``input_received`` from
    the question + options. On a live *resume* run that is served by a fresh
    translator which never saw the paused run's ``input_required``, those fields
    are otherwise unavailable and the card collapses to ``{"question": ""}``.
    Echoing them here (the assistant's question and predefined options — already
    emitted verbatim one envelope earlier, never the user's free text) makes the
    envelope self-describing for both the resume and reload paths.
    """
    echo: dict[str, Any] = {}
    if not isinstance(source, dict):
        return echo
    question = source.get("question")
    if isinstance(question, str) and question:
        echo["question"] = question
    options = source.get("options")
    if isinstance(options, list):
        echo["options"] = options
    allow_free_text = source.get("allowFreeText")
    if not isinstance(allow_free_text, bool):
        allow_free_text = source.get("allow_free_text")
    if isinstance(allow_free_text, bool):
        echo["allowFreeText"] = allow_free_text
    return echo


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


def _pending_backup_blocked_input_from_sidecar(
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
    return pending_input if pending_input.get("kind") == "backup_blocked" else None


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
    backup_service: Any | None = None,
    task_store: Any | None = None,
    task_record: Any | None = None,
    context_record: Any | None = None,
    metrics: Any | None = None,
) -> WaitingInputCancelResult:
    if reason is None:
        reason = _("Task canceled.")
    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    with _WAITING_INPUT_CANCEL_LOCKS.lock_for(pipeline_dir / ".waiting-input-cancel.lock"):
        return _cancel_waiting_input_task_from_sidecar_locked(
            cwd=cwd,
            session_id=session_id,
            context_id=context_id,
            task_id=task_id,
            reason=reason,
            backup_service=backup_service,
            task_store=task_store,
            task_record=task_record,
            context_record=context_record,
            metrics=metrics,
        )


def _cancel_waiting_input_task_from_sidecar_locked(
    *,
    cwd: str,
    session_id: str,
    context_id: str,
    task_id: str,
    reason: str,
    backup_service: Any | None = None,
    task_store: Any | None = None,
    task_record: Any | None = None,
    context_record: Any | None = None,
    metrics: Any | None = None,
) -> WaitingInputCancelResult:
    if waiting_input_task_id_from_sidecar(cwd=cwd, session_id=session_id, context_id=context_id) != task_id:
        return WaitingInputCancelResult.NOT_OWNER

    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    try:
        events = journal.read_all_repairing_tail()
    except Exception:
        logger.warning("Failed to cancel waiting A2A pipeline sidecar", exc_info=True)
        return WaitingInputCancelResult.PERSIST_FAILED

    snapshot = snapshot_store.load()
    pipeline_name = get_pipeline_name()
    if isinstance(snapshot, dict) and isinstance(snapshot.get("pipelineName"), str):
        pipeline_name = snapshot["pipelineName"]
    pending_input = copy.deepcopy(snapshot.get("pendingInput")) if isinstance(snapshot, dict) else None
    if not isinstance(pending_input, dict):
        pending_input = None
    context = PipelineA2AContext(
        pipeline_run_id=context_id,
        task_id=task_id,
        context_id=context_id,
        pipeline_name=pipeline_name,
        iac_code_session_id=session_id,
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
    pending_envelope = _pending_backup_publication_envelope(envelope)
    pending_handoff_envelope = (
        _pending_backup_publication_envelope(handoff_envelope) if handoff_envelope is not None else None
    )
    try:
        events_to_append = [pending_envelope]
        if pending_handoff_envelope is not None:
            events_to_append.append(pending_handoff_envelope)
        journal.append_many(events_to_append, durable=True)
        _save_pipeline_snapshot_or_raise(snapshot_store, reduce_pipeline_events(journal.read_all_repairing_tail()))
    except Exception:
        logger.warning("Failed to persist waiting A2A pipeline cancellation", exc_info=True)
        return WaitingInputCancelResult.PERSIST_FAILED
    _mirror_waiting_input_cancel_a2a_snapshots(
        task_store=task_store,
        task_record=task_record,
        context_record=context_record,
        state=TASK_STATE_INPUT_REQUIRED,
    )
    committed_envelope = _committed_backup_publication_envelope_from_translator(translator, pending_envelope)
    high_water_sequence = max(
        [int(event.get("sequence") or 0) for event in journal.read_all_repairing_tail() if isinstance(event, dict)],
        default=0,
    )
    if int(committed_envelope.get("sequence") or 0) <= high_water_sequence:
        committed_envelope["sequence"] = high_water_sequence + 1
    committed_handoff_envelope = None
    if pending_handoff_envelope is not None:
        committed_handoff_envelope = _committed_backup_publication_envelope_from_translator(
            translator,
            pending_handoff_envelope,
        )
        if int(committed_handoff_envelope.get("sequence") or 0) <= int(committed_envelope.get("sequence") or 0):
            committed_handoff_envelope["sequence"] = int(committed_envelope.get("sequence") or 0) + 1
    try:
        committed_events = [committed_envelope]
        if committed_handoff_envelope is not None:
            committed_events.append(committed_handoff_envelope)
        events_before_commit = journal.read_all_repairing_tail()
        _save_pipeline_snapshot_or_raise(
            snapshot_store,
            reduce_pipeline_events([*events_before_commit, *committed_events]),
        )
        journal.append_many(committed_events, durable=True)
    except Exception as exc:
        logger.warning(
            "Failed to persist committed waiting A2A pipeline cancellation error_type=%s",
            type(exc).__name__,
        )
        try:
            _save_pipeline_snapshot_or_raise(snapshot_store, reduce_pipeline_events(journal.read_all_repairing_tail()))
        except Exception as restore_exc:
            logger.debug(
                "Failed to restore waiting A2A pipeline snapshot after commit failure error_type=%s",
                type(restore_exc).__name__,
            )
        try:
            _persist_waiting_input_terminal_publication_unavailable(
                journal=journal,
                snapshot_store=snapshot_store,
                translator=translator,
                pending_input=pending_input,
                reason="committed_cancel_persist_failed",
            )
        except Exception as marker_exc:
            logger.debug(
                "Failed to persist waiting A2A pipeline publication unavailable marker after commit failure "
                "error_type=%s",
                type(marker_exc).__name__,
            )
        _mirror_waiting_input_cancel_a2a_snapshots(
            task_store=task_store,
            task_record=task_record,
            context_record=context_record,
            state=TASK_STATE_INPUT_REQUIRED,
        )
        return WaitingInputCancelResult.PERSIST_FAILED
    publication_proofs = None
    if committed_handoff_envelope is not None:
        publication_proofs = {
            NORMAL_HANDOFF_PROOF_KEY: BackupPublicationProof.from_envelope(committed_handoff_envelope),
        }
    try:
        backup_result = (backup_service or SessionBackupService()).backup_session(
            cwd,
            session_id,
            reason=BackupReason.TERMINAL,
            critical=True,
            publication_proofs=publication_proofs,
        )
    except SessionBackupBlocked as exc:
        _record_backup_failed_metric(
            metrics,
            reason=BackupReason.TERMINAL.value,
            critical=True,
            retry_count=_backup_retry_count_from_exception(exc),
        )
        _mirror_waiting_input_cancel_a2a_snapshots(
            task_store=task_store,
            task_record=task_record,
            context_record=context_record,
            state=TASK_STATE_INPUT_REQUIRED,
        )
        result = _persist_waiting_input_backup_blocked_event(
            journal=journal,
            snapshot_store=snapshot_store,
            translator=translator,
            pending_input=pending_input,
            error=_format_exception(exc),
        )
        _record_backup_blocked_metric(
            metrics,
            reason=BackupReason.TERMINAL.value,
            recoverable=result == WaitingInputCancelResult.BACKUP_BLOCKED,
        )
        return result
    if getattr(backup_result, "enabled", True) and not getattr(backup_result, "succeeded", True):
        backup_error = getattr(backup_result, "error", None) or type(backup_result).__name__
        blocked_exc = SessionBackupBlocked(
            str(backup_error),
            retry_count=_backup_retry_count(backup_result),
            result=backup_result,
        )
        _record_backup_failed_metric(
            metrics,
            reason=BackupReason.TERMINAL.value,
            critical=True,
            retry_count=_backup_retry_count(backup_result),
        )
        _mirror_waiting_input_cancel_a2a_snapshots(
            task_store=task_store,
            task_record=task_record,
            context_record=context_record,
            state=TASK_STATE_INPUT_REQUIRED,
        )
        result = _persist_waiting_input_backup_blocked_event(
            journal=journal,
            snapshot_store=snapshot_store,
            translator=translator,
            pending_input=pending_input,
            error=_format_exception(blocked_exc),
        )
        _record_backup_blocked_metric(
            metrics,
            reason=BackupReason.TERMINAL.value,
            recoverable=result == WaitingInputCancelResult.BACKUP_BLOCKED,
        )
        return result
    _record_backup_succeeded_metric(
        metrics,
        reason=BackupReason.TERMINAL.value,
        critical=True,
        retry_count=_backup_retry_count(backup_result),
    )
    try:
        _persist_waiting_input_backup_committed_acks(
            journal=journal,
            snapshot_store=snapshot_store,
            translator=translator,
            committed_events=committed_events,
        )
    except Exception as ack_exc:
        logger.warning(
            "Failed to persist waiting A2A pipeline backup committed ack error_type=%s",
            type(ack_exc).__name__,
        )
        _mirror_waiting_input_cancel_a2a_snapshots(
            task_store=task_store,
            task_record=task_record,
            context_record=context_record,
            state=TASK_STATE_INPUT_REQUIRED,
        )
        return WaitingInputCancelResult.PERSIST_FAILED
    _mirror_waiting_input_cancel_a2a_snapshots(
        task_store=task_store,
        task_record=task_record,
        context_record=context_record,
        state=TASK_STATE_CANCELED,
    )
    return WaitingInputCancelResult.CANCELED


def _persist_waiting_input_backup_committed_acks(
    *,
    journal: A2APipelineJournal,
    snapshot_store: A2APipelineSnapshotStore,
    translator: PipelineEventTranslator,
    committed_events: list[dict[str, Any]],
) -> None:
    ack_events: list[dict[str, Any]] = []
    high_water_sequence = max(
        [int(event.get("sequence") or 0) for event in journal.read_all_repairing_tail() if isinstance(event, dict)],
        default=0,
    )
    for committed_event in committed_events:
        ack = translator.manual_event(
            BACKUP_COMMITTED_EVENT_TYPE,
            "pipeline",
            data={
                "committedEventId": committed_event.get("eventId"),
                "committedEventType": committed_event.get("eventType"),
                "committedSequence": committed_event.get("sequence"),
            },
        )
        ack.pop("status", None)
        high_water_sequence += 1
        ack["sequence"] = high_water_sequence
        ack_events.append(ack)
    journal.append_many(ack_events, durable=True)
    _save_pipeline_snapshot_or_raise(snapshot_store, reduce_pipeline_events(journal.read_all_repairing_tail()))


def _persist_waiting_input_backup_blocked_event(
    *,
    journal: A2APipelineJournal,
    snapshot_store: A2APipelineSnapshotStore,
    translator: PipelineEventTranslator,
    pending_input: dict[str, Any] | None,
    error: str,
) -> WaitingInputCancelResult:
    try:
        backup_blocked = translator.manual_event(
            "backup_blocked",
            "pipeline",
            status="input_required",
            data={
                "reason": BackupReason.TERMINAL.value,
                "error": error,
                "recoverable": True,
            },
        )
        if pending_input is not None:
            backup_blocked["input"] = pending_input
        high_water_sequence = max(
            [int(event.get("sequence") or 0) for event in journal.read_all_repairing_tail() if isinstance(event, dict)],
            default=0,
        )
        if int(backup_blocked.get("sequence") or 0) <= high_water_sequence:
            backup_blocked["sequence"] = high_water_sequence + 1
        journal.append(backup_blocked, durable=True)
        _save_pipeline_snapshot_or_raise(snapshot_store, reduce_pipeline_events(journal.read_all_repairing_tail()))
    except Exception as persist_exc:
        logger.warning(
            "Failed to persist waiting A2A pipeline backup_blocked event error_type=%s",
            type(persist_exc).__name__,
        )
        try:
            _persist_waiting_input_terminal_publication_unavailable(
                journal=journal,
                snapshot_store=snapshot_store,
                translator=translator,
                pending_input=pending_input,
                reason="backup_blocked_persist_failed",
            )
        except Exception as marker_exc:
            logger.debug(
                "Failed to persist waiting A2A pipeline publication unavailable marker error_type=%s",
                type(marker_exc).__name__,
            )
        return WaitingInputCancelResult.BACKUP_BLOCKED_PERSIST_FAILED
    return WaitingInputCancelResult.BACKUP_BLOCKED


def _persist_waiting_input_terminal_publication_unavailable(
    *,
    journal: A2APipelineJournal,
    snapshot_store: A2APipelineSnapshotStore,
    translator: PipelineEventTranslator,
    pending_input: dict[str, Any] | None,
    reason: str,
) -> None:
    marker = translator.manual_event(
        "input_required",
        "pipeline",
        status="input_required",
        data={
            "kind": _TERMINAL_PUBLICATION_UNAVAILABLE_KIND,
            "reason": reason,
        },
    )
    if pending_input is not None:
        marker["input"] = pending_input
    high_water_sequence = max(
        [int(event.get("sequence") or 0) for event in journal.read_all_repairing_tail() if isinstance(event, dict)],
        default=0,
    )
    if int(marker.get("sequence") or 0) <= high_water_sequence:
        marker["sequence"] = high_water_sequence + 1
    journal.append(marker, durable=True)
    _save_pipeline_snapshot_or_raise(snapshot_store, reduce_pipeline_events(journal.read_all_repairing_tail()))


def _save_pipeline_snapshot_or_raise(
    snapshot_store: A2APipelineSnapshotStore,
    snapshot: dict[str, Any],
) -> None:
    if not snapshot_store.save(snapshot):
        raise OSError(_("Failed to persist A2A pipeline snapshot"))


def _mirror_waiting_input_cancel_a2a_snapshots(
    *,
    task_store: Any | None,
    task_record: Any | None,
    context_record: Any | None,
    state: str,
) -> None:
    if task_store is None:
        return
    if task_record is not None:
        task_record.state = state
        task_record.active_task = None
        task_record.touch()
        task_store.mirror_task(task_record)
    if context_record is not None:
        if state in {TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_CANCELED}:
            context_record.active_task_id = None
        context_record.touch()
        task_store.mirror_context(context_record)


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
    data: dict[str, Any] = {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "outcome": "canceled",
        "summary": summary,
        "reason": reason,
    }
    cleanup = _pipeline_cleanup_handoff_data_from_session(cwd=cwd, session_id=session_id, public_snapshot=snapshot)
    if cleanup is not None:
        data["cleanup"] = cleanup
    return translator.manual_event(
        "pipeline_handoff_ready",
        "pipeline",
        status="canceled",
        data=data,
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
    try:
        events = _events_for_task_context(
            journal.read_all_repairing_tail(),
            task_id=task_id,
            context_id=context_id,
        )
    except Exception as exc:
        logger.warning(
            "Failed to inspect A2A pipeline terminal task state error_type=%s",
            type(exc).__name__,
        )
        return None
    terminal_event = _latest_terminal_a2a_event(events)
    if terminal_event is None:
        return None
    status = _terminal_status_from_a2a_event(terminal_event)
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


def _pipeline_cleanup_handoff_data(pipeline: Any) -> dict[str, Any] | None:
    cleanup_ledger = getattr(pipeline, "cleanup_ledger", None)
    if not callable(cleanup_ledger):
        return None
    try:
        ledger = cleanup_ledger()
    except Exception:
        logger.warning("Failed to build A2A pipeline cleanup handoff data", exc_info=True)
        return None
    return _pipeline_cleanup_handoff_data_from_ledger(ledger)


def _pipeline_cleanup_handoff_data_from_session(
    *,
    cwd: str,
    session_id: str,
    public_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        ledger_path = SessionStorage().session_dir(cwd, session_id) / "pipeline" / "cleanup.yaml"
    except Exception:
        logger.warning("Failed to locate A2A pipeline cleanup ledger for handoff", exc_info=True)
        return None
    if not ledger_path.exists():
        snapshot_cleanup = public_snapshot.get("cleanup") if isinstance(public_snapshot, dict) else None
        if _public_cleanup_snapshot_has_pending_evidence(snapshot_cleanup):
            return _cleanup_state_unavailable_payload()
        return None
    return _pipeline_cleanup_handoff_data_from_ledger(CleanupLedger(ledger_path))


def _pipeline_cleanup_handoff_data_from_ledger(ledger: Any) -> dict[str, Any] | None:
    try:
        ledger_path = getattr(ledger, "path", None)
        if ledger_path is not None and not Path(ledger_path).exists():
            return _cleanup_state_unavailable_payload()
        load_failed = getattr(ledger, "load_failed", None)
        if callable(load_failed) and load_failed():
            return _cleanup_state_unavailable_payload()
        build_pending_prompt = getattr(ledger, "build_pending_prompt", None)
        if not callable(build_pending_prompt):
            return None
        prompt = build_pending_prompt()
    except Exception:
        logger.warning("Failed to build A2A pipeline cleanup handoff data", exc_info=True)
        return _cleanup_state_unavailable_payload()
    if prompt is None:
        return None

    resources = list(getattr(prompt, "resources", []) or [])
    if not resources:
        return None
    return {
        "status": "pending",
        "resourceCount": len(resources),
        "statusMessage": str(getattr(prompt, "status_message", "") or ""),
        "resources": [_cleanup_resource_handoff_data(resource) for resource in resources],
    }


def _cleanup_state_unavailable_payload() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "statusMessage": _("Cleanup state unavailable. Inspect the session file and cloud resources manually."),
    }


def _public_cleanup_snapshot_has_pending_evidence(cleanup: Any) -> bool:
    if not isinstance(cleanup, dict):
        return False
    resources = cleanup.get("resources")
    if isinstance(resources, list) and len(resources) > 0:
        return True
    resource_count = cleanup.get("resourceCount")
    if isinstance(resource_count, int) and resource_count > 0:
        return True
    status = cleanup.get("status")
    if isinstance(status, str) and status in {"pending", "started", "in_progress", "failed", "unavailable"}:
        return True
    return False


def _cleanup_resource_handoff_data(resource: Any) -> dict[str, Any]:
    return {
        "provider": str(getattr(resource, "provider", "") or ""),
        "resourceType": str(getattr(resource, "resource_type", "") or ""),
        "resourceId": str(getattr(resource, "resource_id", "") or ""),
        "resourceName": str(getattr(resource, "resource_name", "") or ""),
        "regionId": str(getattr(resource, "region_id", "") or ""),
        "sourceStepId": str(getattr(resource, "source_step_id", "") or ""),
        "cleanupStatus": str(getattr(resource, "cleanup_status", "") or ""),
        "progressStatus": getattr(resource, "progress_status", None),
        "lastError": _public_cleanup_error(getattr(resource, "last_error", None)),
    }


def _public_cleanup_error(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:1000] + "..." if len(text) > 1000 else text


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


async def _drive_stream_events(
    stream: AsyncIterator[Any],
    requests: asyncio.Queue[asyncio.Future[Any]],
) -> None:
    try:
        while True:
            completion = await requests.get()
            if completion.cancelled():
                continue
            try:
                event = await anext(stream)
            except asyncio.CancelledError:
                completion.cancel()
                raise
            except BaseException as exc:
                if not completion.done():
                    completion.set_exception(exc)
                return
            if not completion.done():
                completion.set_result(event)
    finally:
        await _close_stream_safely(stream)


async def _cancel_task_safely(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait({task})
    if task.cancelled():
        return
    try:
        task.result()
    except StopAsyncIteration:
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


async def _abort_outbound_worker(outbound: PipelineA2AOutboundQueue) -> None:
    abort_task = asyncio.create_task(outbound.abort())
    while not abort_task.done():
        try:
            await asyncio.shield(abort_task)
        except asyncio.CancelledError:
            continue
    await abort_task


async def _settle_active_interrupt_safely(runtime: Any) -> None:
    async def settle() -> None:
        async with _outbound_lock(runtime):
            runtime.active_interrupt_count = max(0, _active_interrupt_count(runtime) - 1)
            if runtime.active_interrupt_count == 0:
                _interrupt_settled_event(runtime).set()

    cancellation: asyncio.CancelledError | None = None
    settle_task = asyncio.create_task(settle())
    while not settle_task.done():
        try:
            await asyncio.shield(settle_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
    await settle_task
    if cancellation is not None:
        raise cancellation


async def _register_active_interrupt(runtime: Any) -> bool:
    async with _outbound_lock(runtime):
        if bool(getattr(runtime, "terminal_publication_started", False)):
            return False
        runtime.active_interrupt_count = _active_interrupt_count(runtime) + 1
        _interrupt_settled_event(runtime).clear()
        return True


def _outbound_lock(runtime: Any) -> asyncio.Lock:
    lock = getattr(runtime, "outbound_lock", None)
    if isinstance(lock, asyncio.Lock):
        return lock
    lock = asyncio.Lock()
    runtime.outbound_lock = lock
    return lock


def _text_delta_output(event: Any) -> str | None:
    while isinstance(event, SubPipelineStreamEvent):
        event = event.inner
    return event.text if isinstance(event, TextDeltaEvent) else None


def _restart_requested_event(runtime: Any) -> asyncio.Event:
    restart_requested = getattr(runtime, "restart_requested", None)
    if isinstance(restart_requested, asyncio.Event):
        return restart_requested
    restart_requested = asyncio.Event()
    runtime.restart_requested = restart_requested
    return restart_requested


def _interrupt_settled_event(runtime: Any) -> asyncio.Event:
    interrupt_settled = getattr(runtime, "interrupt_settled", None)
    if isinstance(interrupt_settled, asyncio.Event):
        return interrupt_settled
    interrupt_settled = _new_set_asyncio_event()
    runtime.interrupt_settled = interrupt_settled
    return interrupt_settled


def _active_interrupt_count(runtime: Any) -> int:
    count = getattr(runtime, "active_interrupt_count", 0)
    return count if isinstance(count, int) and count > 0 else 0


def _has_unpublished_mcp_warnings(runtime: Any) -> bool:
    warnings = getattr(runtime, "mcp_config_warnings", None) or []
    pushed_count = getattr(runtime, "_a2a_mcp_warnings_pushed_count", 0)
    return isinstance(pushed_count, int) and pushed_count < len(warnings)


def _consume_requested_interrupt_action(runtime: Any) -> str | None:
    restart_event = _restart_requested_event(runtime)
    if bool(getattr(runtime, "restart_after_interrupt", False)) and restart_event.is_set():
        restart_event.clear()
        runtime.restart_after_interrupt = False
        return "restart"
    if bool(getattr(runtime, "pause_after_interrupt", False)) and restart_event.is_set():
        restart_event.clear()
        runtime.pause_after_interrupt = False
        return "pause"
    return None


def _is_pipeline_terminal_stream_event(event: Any) -> bool:
    return isinstance(event, PipelineEvent) and event.type in {
        PipelineEventType.PIPELINE_COMPLETED,
        PipelineEventType.PIPELINE_ERROR,
        PipelineEventType.BACKUP_BLOCKED,
    }


def _is_active_task_record(task: Any, active_task_id: str | None) -> bool:
    return active_task_id is not None and getattr(task, "task_id", None) == active_task_id


def _is_active_task_request(task: Any, task_id: str, active_task_id: str | None) -> bool:
    return _is_active_task_record(task, active_task_id) and task_id == active_task_id


def _task_has_live_owner(task: Any) -> bool:
    active_task = getattr(task, "active_task", None)
    return active_task is not None and not active_task.done()


def _pipeline_sidecar_dir(pipeline: Any, cwd: str, session_id: str) -> Path:
    session = getattr(pipeline, "session", None)
    session_dir = getattr(session, "session_dir", None)
    if isinstance(session_dir, (str, Path)):
        sidecar_dir = Path(session_dir)
        root_session_dir = SessionStorage().session_dir(cwd, session_id)
        if sidecar_dir.name == "pipeline" and sidecar_dir.parent == root_session_dir:
            return existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
        return a2a_pipeline_dir_for_sidecar_dir(sidecar_dir)
    return existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)


def _pipeline_flow_monitor_for_session(
    *,
    cwd: str,
    session_id: str,
    context_id: str,
    task_id: str,
    pipeline_run_id: str,
) -> PipelineA2AFlowMonitor | None:
    try:
        session_paths = SessionPaths.require_supported(SessionStorage().session_dir(cwd, session_id))
    except Exception as exc:
        logger.warning(
            "Failed to initialize session A2A pipeline flow monitor error_type=%s",
            type(exc).__name__,
        )
        return None
    return PipelineA2AFlowMonitor(
        session_paths.a2a_pipeline_flow_log_path,
        PipelineA2AFlowIdentity(
            session_id=session_id,
            context_id=context_id,
            task_id=task_id,
            pipeline_run_id=pipeline_run_id,
        ),
        session_dir=session_paths.session_dir,
    )


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


def _committed_terminal_status_for_task_context(
    publisher: PipelineA2AEventPublisher,
    *,
    task_id: str,
    context_id: str,
) -> str | None:
    events = _safe_read_pipeline_journal(publisher.journal)
    scoped_events = _events_for_task_context(events, task_id=task_id, context_id=context_id)
    terminal_event = _latest_terminal_a2a_event(scoped_events)
    if terminal_event is None:
        return None
    return _terminal_status_from_a2a_event(terminal_event)


def _terminal_status_from_sidecar(status: Any) -> str | None:
    terminal_event = _terminal_event_from_sidecar_status(status)
    if terminal_event is None:
        return None
    return terminal_event[1]


def _completed_event_data_from_sidecar_status(status: Any) -> dict[str, Any]:
    """把 sidecar 终态映射成最小的 completed-event 载荷,只承载 outcome 标志。

    恢复路径没有真正的 PIPELINE_COMPLETED 事件,但 ``terminal_outcome_from_completed_event``
    只看 ``failed`` / ``canceled`` / ``early_exit`` 标志(缺省即 ``completed``),实际交接上下文
    由重建的 ``pipeline.context`` 提供。故此处仅需据 sidecar 终态还原 outcome。
    """
    if status == "failed":
        return {"failed": True}
    if status in {"user_aborted", "discarded", "canceled"}:
        return {"canceled": True}
    return {}


def _handoff_ready_present(events: list[dict[str, Any]]) -> bool:
    """判断作用域内是否已存在 ``pipeline_handoff_ready``,避免恢复补发时重复交接。"""
    return any(event.get("eventType") == "pipeline_handoff_ready" for event in events)


def _task_state_from_pipeline(
    pipeline: Any,
    snapshot: dict[str, Any],
    *,
    allow_terminal_snapshot: bool = True,
    allow_sidecar_terminal_fallback: bool = True,
) -> str:
    snapshot_status = snapshot.get("status")
    snapshot_state = _task_state_from_snapshot(snapshot)
    if snapshot_status in _TERMINAL_SNAPSHOT_STATUSES:
        return snapshot_state if allow_terminal_snapshot else TASK_STATE_INPUT_REQUIRED
    sidecar_status = getattr(pipeline, "sidecar_status", None)
    if allow_sidecar_terminal_fallback and _is_terminal_sidecar_status(sidecar_status):
        return _task_state_from_sidecar_status(sidecar_status)
    return snapshot_state


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
    ) or _unacknowledged_committed_terminal_matches_task(
        publisher,
        sidecar_status,
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
    if sidecar_status == "backup_blocked":
        return (
            status in _WAITING_A2A_STATUSES
            and _pending_backup_blocked_input_from_sidecar(
                publisher,
                task_id=task_id,
                context_id=context_id,
            )
            is not None
        )
    return False


def _active_sidecar_mismatch_error_from_publisher(
    publisher: PipelineA2AEventPublisher,
    *,
    context_id: str,
    sidecar_status: str,
) -> RecoverablePipelineInvalidParamsError:
    owner = _current_sidecar_owner(publisher, context_id=context_id)
    recoverable_task_id = owner.task_id if owner is not None and owner.task_id else "unknown"
    recoverable_context_id = owner.context_id if owner is not None and owner.context_id else context_id
    return _active_sidecar_mismatch_error(
        recoverable_task_id=recoverable_task_id,
        context_id=recoverable_context_id,
        sidecar_status=sidecar_status,
    )


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
    snapshot = snapshot_store.load()
    try:
        journal_events = journal.read_all_repairing_tail()
    except Exception:
        logger.warning("Failed to inspect A2A pipeline sidecar owner journal", exc_info=True)
        raise _SidecarOwnerUnavailableError(_("A2A pipeline sidecar owner is unavailable")) from None
    snapshot = _journal_authoritative_snapshot(
        snapshot_store=snapshot_store,
        snapshot=snapshot,
        journal_events=journal_events,
    )
    snapshot_owner = _owner_from_snapshot(snapshot)
    if snapshot_owner is not None and snapshot_owner.context_id != context_id:
        snapshot_owner = None
    if snapshot_owner is not None and _has_unacknowledged_committed_event_at_or_after(
        journal_events,
        snapshot_owner.sequence,
    ):
        snapshot_owner = None
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
    except Exception as exc:
        logger.warning(
            "Failed to build A2A pipeline snapshot from journal error_type=%s",
            type(exc).__name__,
        )
        return None
    if not events:
        return snapshot
    try:
        rebuilt = reduce_pipeline_events(events)
    except Exception as exc:
        logger.warning(
            "Failed to reduce A2A pipeline journal events error_type=%s",
            type(exc).__name__,
        )
        return None
    if not isinstance(rebuilt, dict):
        return None
    snapshot_sequence = _sequence_number(snapshot.get("lastSequence")) if isinstance(snapshot, dict) else 0
    rebuilt_sequence = _sequence_number(rebuilt.get("lastSequence"))
    if rebuilt_sequence != snapshot_sequence:
        try:
            snapshot_store.save(rebuilt)
        except Exception as exc:
            logger.debug("Failed to save repaired A2A pipeline snapshot error_type=%s", type(exc).__name__)
    return rebuilt


def _journal_authoritative_snapshot(
    *,
    snapshot_store: A2APipelineSnapshotStore,
    snapshot: dict[str, Any] | None,
    journal_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not journal_events:
        return snapshot
    snapshot_sequence = _sequence_number(snapshot.get("lastSequence")) if isinstance(snapshot, dict) else 0
    journal_sequence = max((_sequence_number(event.get("sequence")) for event in journal_events), default=0)
    if isinstance(snapshot, dict) and snapshot_sequence == journal_sequence:
        return snapshot
    try:
        rebuilt = reduce_pipeline_events(journal_events)
    except Exception as exc:
        logger.warning(
            "Failed to reduce A2A pipeline journal events error_type=%s",
            type(exc).__name__,
        )
        return None
    if not isinstance(rebuilt, dict):
        return None
    try:
        snapshot_store.save(rebuilt)
    except Exception as exc:
        logger.debug("Failed to save repaired A2A pipeline snapshot error_type=%s", type(exc).__name__)
    return rebuilt


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
        if _is_pending_backup_publication_event(event):
            continue
        if _requires_backup_committed_ack(event) and not _has_backup_committed_ack(events, event):
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
    unavailable_sequence = _latest_terminal_recovery_blocking_sequence(events)
    for event in events:
        if _sequence_number(event.get("sequence")) <= unavailable_sequence:
            continue
        if _terminal_status_from_a2a_event(event) is None:
            continue
        if _requires_backup_committed_ack(event) and not _has_backup_committed_ack(events, event):
            continue
        if terminal_event is None or _sequence_number(event.get("sequence")) >= _sequence_number(
            terminal_event.get("sequence")
        ):
            terminal_event = event
    return terminal_event


def _terminal_publication_unavailable_blocks_recovery(events: list[dict[str, Any]]) -> bool:
    unavailable_sequence = _latest_terminal_recovery_blocking_sequence(events)
    if unavailable_sequence <= 0:
        return False
    latest_terminal_sequence = max(
        (
            _sequence_number(event.get("sequence"))
            for event in events
            if _terminal_status_from_a2a_event(event) is not None
        ),
        default=0,
    )
    return unavailable_sequence >= latest_terminal_sequence


def _latest_terminal_publication_unavailable_sequence(events: list[dict[str, Any]]) -> int:
    return max(
        (
            _sequence_number(event.get("sequence"))
            for event in events
            if _is_terminal_publication_unavailable_event(event)
        ),
        default=0,
    )


def _latest_terminal_recovery_blocking_sequence(events: list[dict[str, Any]]) -> int:
    return max(
        _latest_terminal_publication_unavailable_sequence(events),
        max(
            (_sequence_number(event.get("sequence")) for event in events if event.get("eventType") == "backup_blocked"),
            default=0,
        ),
    )


def _is_terminal_publication_unavailable_event(event: dict[str, Any]) -> bool:
    if event.get("eventType") != "input_required":
        return False
    raw_data = event.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    return data.get("kind") == _TERMINAL_PUBLICATION_UNAVAILABLE_KIND


def _requires_backup_committed_ack(event: dict[str, Any]) -> bool:
    return event.get("visibility") == _COMMITTED_BACKUP_VISIBILITY


def _has_unacknowledged_committed_terminal_event(events: list[dict[str, Any]]) -> bool:
    return any(
        _terminal_status_from_a2a_event(event) is not None
        and _requires_backup_committed_ack(event)
        and not _has_backup_committed_ack(events, event)
        for event in events
    )


def _unacknowledged_committed_terminal_matches_task(
    publisher: PipelineA2AEventPublisher,
    sidecar_status: Any,
    *,
    task_id: str,
    context_id: str,
) -> bool:
    expected_status = _terminal_status_from_sidecar(sidecar_status)
    if expected_status is None:
        return False
    events = _events_for_task_context(
        _safe_read_pipeline_journal(publisher.journal),
        task_id=task_id,
        context_id=context_id,
    )
    blocking_sequence = _latest_terminal_recovery_blocking_sequence(events)
    for event in events:
        if _sequence_number(event.get("sequence")) <= blocking_sequence:
            continue
        if _terminal_status_from_a2a_event(event) != expected_status:
            continue
        if _requires_backup_committed_ack(event) and not _has_backup_committed_ack(events, event):
            return True
    return False


def _has_unacknowledged_committed_event_at_or_after(events: list[dict[str, Any]], sequence: int) -> bool:
    return any(
        _sequence_number(event.get("sequence")) >= sequence
        and _requires_backup_committed_ack(event)
        and not _has_backup_committed_ack(events, event)
        for event in events
    )


def _has_backup_committed_ack(events: list[dict[str, Any]], terminal_event: dict[str, Any]) -> bool:
    terminal_sequence = _sequence_number(terminal_event.get("sequence"))
    terminal_event_id = terminal_event.get("eventId")
    terminal_event_type = terminal_event.get("eventType")
    for event in events:
        if event.get("eventType") != BACKUP_COMMITTED_EVENT_TYPE:
            continue
        if _sequence_number(event.get("sequence")) <= terminal_sequence:
            continue
        raw_data = event.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        if data.get("committedEventId") == terminal_event_id:
            return True
        if (
            _sequence_number(data.get("committedSequence")) == terminal_sequence
            and data.get("committedEventType") == terminal_event_type
        ):
            return True
    return False


def _terminal_status_from_a2a_event(event: dict[str, Any]) -> str | None:
    if _is_pending_backup_publication_event(event):
        return None
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
    return bool(journal_events) and snapshot_sequence != journal_sequence


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
