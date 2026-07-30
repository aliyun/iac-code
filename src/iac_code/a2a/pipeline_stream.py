from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import ParseDict

from iac_code.a2a.events import (
    _artifact_update_event,
    _emit_auto_permission_audit,
    _emit_resolver_permission_audit,
    _extract_artifact_metadata,
)
from iac_code.a2a.exposure import A2AExposureType, normalize_a2a_exposure_types
from iac_code.a2a.pipeline_events import (
    PIPELINE_EVENTS_EXTENSION_URI,
    PIPELINE_METADATA_SCHEMA_VERSION,
    PipelineEventTranslator,
    safe_permission_metadata,
)
from iac_code.a2a.pipeline_journal import A2APipelineJournal, to_json_safe
from iac_code.a2a.pipeline_outbound import OUTBOUND_HARD_MAX_BATCH_BYTES, OUTBOUND_HARD_MAX_BATCH_EVENTS
from iac_code.a2a.pipeline_performance import a2a_extreme_performance_enabled
from iac_code.a2a.pipeline_snapshot import SNAPSHOT_SCHEMA_VERSION, A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.pipeline_transport_delivery import (
    discard_pipeline_transport_delivery,
    mark_pipeline_transport_delivery_enqueued,
    pipeline_transport_delivery_is_required,
    pipeline_transport_delivery_tracking_enabled,
    register_pipeline_transport_delivery,
    routed_pipeline_transport_delivery_tracker,
)
from iac_code.pipeline.constants import (
    PIPELINE_EVENT_CLEANUP_COMPLETED,
    PIPELINE_EVENT_CLEANUP_FAILED,
    PIPELINE_EVENT_CLEANUP_PROGRESS,
    PIPELINE_EVENT_CLEANUP_STARTED,
)
from iac_code.services.permissions.audit import is_aliyun_api_non_read_only_permission_event
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent, ToolResultEvent
from iac_code.utils.public_errors import sanitize_strict_text

PipelinePermissionResolver = Callable[[PermissionRequestEvent], bool | Awaitable[bool]]
PipelineBeforeEnqueueHook = Callable[[dict[str, Any]], bool | Awaitable[bool]]
PipelineAfterBackupCommitHook = Callable[[dict[str, Any]], None | Awaitable[None]]
PipelineBackupCommitGate = Callable[[dict[str, Any]], bool]
logger = logging.getLogger(__name__)
PENDING_BACKUP_VISIBILITY = "pending_backup"
COMMITTED_BACKUP_VISIBILITY = "committed"
BACKUP_COMMITTED_EVENT_TYPE = "backup_committed"
_ARTIFACT_SEMANTIC_METADATA_KEYS = ("role", "supersedesPath", "supersedesKey", "supersedesFingerprint")
_RECOVERY_SEMANTIC_EVENT_TYPES = {
    "pipeline_started",
    "pipeline_resumed",
    "step_started",
    "step_completed",
    "step_failed",
    "candidate_started",
    "candidate_selected",
    "candidate_completed",
    "candidate_failed",
    "candidate_step_started",
    "candidate_step_completed",
    "candidate_step_failed",
    "input_required",
    "input_received",
    "backup_blocked",
    BACKUP_COMMITTED_EVENT_TYPE,
    "pipeline_completed",
    "pipeline_failed",
    "pipeline_canceled",
    "pipeline_handoff_ready",
    "pipeline_warning",
    PIPELINE_EVENT_CLEANUP_STARTED,
    PIPELINE_EVENT_CLEANUP_PROGRESS,
    PIPELINE_EVENT_CLEANUP_COMPLETED,
    PIPELINE_EVENT_CLEANUP_FAILED,
    "artifact_created",
    "rollback_completed",
    "candidate_restart_requested",
}
_DISPLAY_ONLY_EVENT_TYPES = {
    "candidate_detail_shown",
    "diagram_shown",
    "permission_requested",
    "thinking_delta",
    "text_delta",
    "message_tombstone",
    "tool_started",
    "tool_result",
}
_EXTREME_DEFERRED_EVENT_TYPES = {"text_delta", "thinking_delta"}
_EXTREME_JOURNAL_FLUSH_EVENTS = 512
_RECOVERY_STATE_SCOPES = {"step", "candidate", "candidateStep", "candidate_step"}
_RECOVERY_STATE_STATUSES = {"working"}


def backup_committed_delivery_envelope(
    ack_envelope: dict[str, Any],
    committed_envelope: dict[str, Any],
) -> dict[str, Any]:
    delivery_envelope = dict(ack_envelope)
    status = committed_envelope.get("status")
    if isinstance(status, str):
        delivery_envelope["status"] = status
    return delivery_envelope


class _SnapshotCatchUpUnavailableError(Exception):
    pass


class _SequenceHighWaterUnavailableError(Exception):
    pass


class PipelineA2APersistenceError(RuntimeError):
    pass


@dataclass
class PreparedPipelinePermission:
    request: PermissionRequestEvent
    envelopes: list[dict[str, Any]]
    approved: bool
    permission_audit_emitted: bool
    resolver_used: bool


class PipelineA2AEventPublisher:
    def __init__(
        self,
        event_queue: Any,
        translator: PipelineEventTranslator,
        journal: A2APipelineJournal,
        snapshot_store: A2APipelineSnapshotStore,
        artifact_store: Any | None = None,
        exposure_types: Any = None,
        delivery_task_id: str | None = None,
        delivery_context_id: str | None = None,
        before_enqueue: PipelineBeforeEnqueueHook | None = None,
        after_backup_commit: PipelineAfterBackupCommitHook | None = None,
        backup_commit_gate: PipelineBackupCommitGate | None = None,
        extreme_performance: bool | None = None,
        flow_monitor: Any | None = None,
    ) -> None:
        self.event_queue = event_queue
        self.translator = translator
        self.journal = journal
        self.snapshot_store = snapshot_store
        self.artifact_store = artifact_store
        self.exposure_types = normalize_a2a_exposure_types(exposure_types)
        self.delivery_task_id = delivery_task_id
        self.delivery_context_id = delivery_context_id
        self.before_enqueue = before_enqueue
        self.after_backup_commit = after_backup_commit
        self.backup_commit_gate = backup_commit_gate
        self.flow_monitor = flow_monitor
        self._sequence_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._delivery_lock_owner: asyncio.Task[Any] | None = None
        self._delivery_lock_depth = 0
        self._last_sequence = 0
        self.last_envelope: dict[str, Any] | None = None
        self.extreme_performance = (
            a2a_extreme_performance_enabled() if extreme_performance is None else extreme_performance
        )
        self._extreme_snapshot_loaded = False
        self._extreme_snapshot_cache: dict[str, Any] | None = None
        self._extreme_pending_journal_events: list[dict[str, Any]] = []
        self._extreme_pending_snapshot_events: list[dict[str, Any]] = []

    async def publish(
        self,
        event: Any,
        *,
        permission_resolver: PipelinePermissionResolver | None = None,
        auto_approve_permissions: bool = False,
    ) -> str | None:
        envelopes = self.translator.translate(event)
        permission_request = _permission_request_from(event)
        tool_result = _tool_result_from(event)
        text_parts: list[str] = []

        for envelope in envelopes:
            if _should_skip_envelope(envelope, exposure_types=self.exposure_types):
                continue

            artifact_metadata = await self._maybe_externalize_artifact(envelope, tool_result)
            if envelope.get("eventType") == "artifact_created" and artifact_metadata is None:
                continue
            if (
                envelope.get("eventType") in ("tool_started", "tool_result")
                and artifact_metadata is None
                and A2AExposureType.TOOL_TRACE not in self.exposure_types
            ):
                continue

            if permission_request is not None:
                approved = await self._apply_permission_metadata(
                    permission_request,
                    envelope,
                    permission_resolver=permission_resolver,
                    auto_approve_permissions=auto_approve_permissions,
                )
                permission_audit_emitted = False
                if approved and _can_resolve_permission_future(permission_request):
                    audit_ok = (
                        _emit_resolver_permission_audit(permission_request, approved)
                        if permission_resolver is not None
                        else _emit_auto_permission_audit(permission_request, approved)
                    )
                    permission_audit_emitted = True
                    if not audit_ok:
                        approved = False
                        _set_permission_approval(envelope, approved)
            else:
                approved = None
                permission_audit_emitted = False

            persisted = await self._persist_and_enqueue(
                envelope,
                artifact_metadata=artifact_metadata,
                require_durable_metadata=permission_request is not None,
            )
            if permission_request is not None:
                final_approved = bool(approved) if persisted else False
                if _can_resolve_permission_future(permission_request):
                    if permission_audit_emitted and bool(approved) and not persisted:
                        if permission_resolver is not None:
                            _emit_resolver_permission_audit(
                                permission_request,
                                False,
                                persistence_failure=True,
                            )
                        else:
                            _emit_auto_permission_audit(
                                permission_request,
                                False,
                                persistence_failure=True,
                            )
                    elif not permission_audit_emitted:
                        audit_ok = (
                            _emit_resolver_permission_audit(permission_request, final_approved)
                            if permission_resolver is not None
                            else _emit_auto_permission_audit(permission_request, final_approved)
                        )
                        if final_approved and not audit_ok:
                            final_approved = False
                    _resolve_permission_future(permission_request, final_approved)

            if envelope.get("eventType") == "text_delta":
                text_parts.append(_text_from_envelope(envelope))

        return "".join(text_parts) if text_parts else None

    async def publish_batch(self, events: list[Any]) -> None:
        persisted = await self.persist_batch_events(events)
        await self.enqueue_persisted_batch(persisted, local_envelopes=persisted)

    async def persist_batch_events(self, events: list[Any]) -> list[dict[str, Any]]:
        persisted: list[dict[str, Any]] = []
        for event in events:
            tool_result = _tool_result_from(event)
            for envelope in self.translator.translate(event):
                if _should_skip_envelope(envelope, exposure_types=self.exposure_types):
                    continue

                artifact_metadata = await self._maybe_externalize_artifact(envelope, tool_result)
                if envelope.get("eventType") == "artifact_created" and artifact_metadata is None:
                    continue
                if (
                    envelope.get("eventType") == "tool_result"
                    and artifact_metadata is None
                    and A2AExposureType.TOOL_TRACE not in self.exposure_types
                ):
                    continue

                safe_envelope = await self.persist_envelope(envelope, artifact_metadata=artifact_metadata)
                if safe_envelope is None:
                    raise PipelineA2APersistenceError("Failed to persist an outbound A2A pipeline event")
                persisted.append(safe_envelope)
        return persisted

    async def prepare_permission_event(
        self,
        event: Any,
        *,
        approved: bool,
        resolver_used: bool,
    ) -> PreparedPipelinePermission:
        request = _permission_request_from(event)
        if request is None:
            raise TypeError("Expected a permission request event")

        persisted: list[dict[str, Any]] = []
        permission_audit_emitted = False
        try:
            for envelope in self.translator.translate(event):
                if _should_skip_envelope(envelope, exposure_types=self.exposure_types):
                    continue
                self._set_permission_metadata(request, envelope, approved=approved)
                if approved and _can_resolve_permission_future(request):
                    audit_ok = (
                        _emit_resolver_permission_audit(request, approved)
                        if resolver_used
                        else _emit_auto_permission_audit(request, approved)
                    )
                    permission_audit_emitted = True
                    if not audit_ok:
                        approved = False
                        _set_permission_approval(envelope, approved)
                safe_envelope = await self.persist_envelope(envelope, require_durable_metadata=True)
                if safe_envelope is None:
                    raise PipelineA2APersistenceError("Failed to persist an outbound A2A permission event")
                persisted.append(safe_envelope)
        except BaseException:
            _resolve_permission_future(request, False)
            raise

        return PreparedPipelinePermission(
            request=request,
            envelopes=persisted,
            approved=approved,
            permission_audit_emitted=permission_audit_emitted,
            resolver_used=resolver_used,
        )

    async def resolve_permission_event(
        self,
        event: Any,
        *,
        permission_resolver: PipelinePermissionResolver | None = None,
        auto_approve_permissions: bool = False,
    ) -> bool:
        request = _permission_request_from(event)
        if request is None:
            raise TypeError("Expected a permission request event")
        try:
            approved = bool(auto_approve_permissions)
            if permission_resolver is not None:
                result = permission_resolver(request)
                approved = bool(await result) if inspect.isawaitable(result) else bool(result)
            elif is_aliyun_api_non_read_only_permission_event(request):
                approved = False
            return approved
        except BaseException:
            _resolve_permission_future(request, False)
            raise

    def fail_permission_event(self, event: Any) -> None:
        request = _permission_request_from(event)
        if request is not None:
            _resolve_permission_future(request, False)

    async def enqueue_prepared_permission(self, prepared: PreparedPipelinePermission) -> bool:
        delivered = bool(prepared.envelopes)
        for envelope in prepared.envelopes:
            delivered = await self.enqueue_persisted(envelope, wait_for_transport=True) and delivered
        return delivered

    def complete_prepared_permission(self, prepared: PreparedPipelinePermission, *, delivered: bool) -> None:
        request = prepared.request
        if not _can_resolve_permission_future(request):
            return

        final_approved = prepared.approved and delivered and bool(prepared.envelopes)
        if prepared.permission_audit_emitted and prepared.approved and not final_approved:
            if prepared.resolver_used:
                _emit_resolver_permission_audit(request, False, persistence_failure=True)
            else:
                _emit_auto_permission_audit(request, False, persistence_failure=True)
        elif not prepared.permission_audit_emitted:
            audit_ok = (
                _emit_resolver_permission_audit(request, final_approved)
                if prepared.resolver_used
                else _emit_auto_permission_audit(request, final_approved)
            )
            if final_approved and not audit_ok:
                final_approved = False
        _resolve_permission_future(request, final_approved)

    async def publish_interrupt(
        self,
        *,
        prompt: str,
        verdict: Any,
        parent_rollback: bool | None = None,
        include_received: bool = True,
    ) -> None:
        action = getattr(verdict, "action", "")
        rollback_target = getattr(verdict, "rollback_target", None)
        candidate_scope = getattr(verdict, "candidate_scope", None)
        reason = getattr(verdict, "reason", "")
        paused = bool(getattr(verdict, "paused", False))

        envelopes = []
        if include_received:
            envelopes.append(
                self.translator.manual_event(
                    "interrupt_received",
                    "interrupt",
                    data={"messageLength": len(prompt)},
                )
            )
        envelopes.append(
            self.translator.manual_event(
                "interrupt_classified",
                "interrupt",
                data={
                    "action": action,
                    "targetStepId": rollback_target,
                    "candidateScope": candidate_scope,
                    "reason": reason,
                    "paused": paused,
                },
            )
        )
        if action == "hard_interrupt" and parent_rollback is True:
            envelopes.append(
                self.translator.manual_event(
                    "rollback_completed",
                    "interrupt",
                    data={
                        "rollbackScope": "parent",
                        "toStepId": rollback_target,
                        "reason": reason,
                    },
                )
            )
        elif action == "hard_interrupt" and parent_rollback is False:
            if candidate_scope:
                envelopes.extend(
                    self.translator.candidate_restart_events(
                        candidate_scope=candidate_scope,
                        target_candidate_step_id=rollback_target,
                        reason=reason,
                    )
                )
        elif action == "hard_interrupt" and parent_rollback is None and candidate_scope:
            envelopes.extend(
                self.translator.candidate_restart_events(
                    candidate_scope=candidate_scope,
                    target_candidate_step_id=rollback_target,
                    reason=reason,
                )
            )
        elif action == "hard_interrupt" and parent_rollback is None:
            envelopes.append(
                self.translator.manual_event(
                    "rollback_completed",
                    "interrupt",
                    data={
                        "rollbackScope": "parent",
                        "toStepId": rollback_target,
                        "reason": reason,
                    },
                )
            )

        for envelope in envelopes:
            await self._persist_and_enqueue(envelope)

    async def publish_interrupt_received(self, *, prompt: str) -> None:
        await self._persist_and_enqueue(
            self.translator.manual_event(
                "interrupt_received",
                "interrupt",
                data={"messageLength": len(prompt)},
            )
        )

    async def publish_manual(
        self,
        event_type: str,
        scope: str,
        *,
        status: str = "working",
        data: dict[str, Any] | None = None,
        coordinates: dict[str, Any] | None = None,
        require_durable_metadata: bool = False,
        require_journal_metadata: bool = False,
    ) -> dict[str, Any] | None:
        envelope = self.translator.manual_event(event_type, scope, status=status, data=data)
        if coordinates:
            for key in ("step", "candidate", "candidateStep"):
                value = coordinates.get(key)
                if isinstance(value, dict):
                    envelope[key] = dict(value)
        return await self._persist_and_enqueue_envelope(
            envelope,
            require_durable_metadata=require_durable_metadata,
            require_journal_metadata=require_journal_metadata,
        )

    def _next_snapshot(self, envelope: dict[str, Any]) -> dict[str, Any]:
        existing_snapshot = self.snapshot_store.load()
        if existing_snapshot is not None:
            try:
                events = self.journal.read_all_repairing_tail()
            except Exception as exc:
                logger.warning(
                    "Failed to read A2A pipeline journal snapshot catch-up events error_type=%s",
                    type(exc).__name__,
                )
                raise _SnapshotCatchUpUnavailableError from None

            scoped_events = _events_for_envelope_identity(events, envelope)
            if _snapshot_matches_envelope_identity(existing_snapshot, envelope) and _snapshot_schema_is_current(
                existing_snapshot
            ):
                snapshot_sequence = _int_value(existing_snapshot.get("lastSequence"), 0)
                catch_up_events = [
                    event for event in scoped_events if _int_value(event.get("sequence"), 0) > snapshot_sequence
                ]
                catch_up_events = _include_backup_ack_committed_event(catch_up_events, scoped_events, envelope)
                snapshot_base = existing_snapshot
            else:
                catch_up_events = scoped_events
                snapshot_base = None
            return reduce_pipeline_events([*catch_up_events, envelope], existing_snapshot=snapshot_base)

        try:
            journal_events = self.journal.read_all_repairing_tail()
        except Exception as exc:
            logger.warning(
                "Failed to read A2A pipeline journal snapshot events error_type=%s",
                type(exc).__name__,
            )
            raise _SnapshotCatchUpUnavailableError from None
        events = _events_for_envelope_identity(journal_events, envelope)
        return reduce_pipeline_events([*events, envelope])

    async def persist_envelope(
        self,
        envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None = None,
        require_durable_metadata: bool = False,
        require_journal_metadata: bool = False,
    ) -> dict[str, Any] | None:
        async with self._sequence_lock:
            self._annotate_delivery_alias(envelope)
            try:
                if self.extreme_performance:
                    self._ensure_monotonic_sequence_fast(envelope)
                else:
                    self._ensure_monotonic_sequence(envelope)
            except _SequenceHighWaterUnavailableError:
                logger.warning("Skipping A2A pipeline event until journal high-water sequence is readable")
                return None
            safe_envelope = to_json_safe(envelope)
            if not isinstance(safe_envelope, dict):
                logger.warning("Skipping invalid A2A pipeline envelope: %r", envelope)
                return None
            durable_required = require_durable_metadata or is_recovery_semantic_event(safe_envelope)
            if self.extreme_performance:
                return self._persist_envelope_extreme(
                    safe_envelope,
                    artifact_metadata=artifact_metadata,
                    durable_required=durable_required,
                    require_journal_metadata=require_journal_metadata,
                )
            journal_persisted = False
            snapshot_persisted = False
            try:
                self.journal.append(safe_envelope, durable=durable_required)
                journal_persisted = True
            except Exception as exc:
                logger.warning(
                    "Failed to append A2A pipeline journal event error_type=%s",
                    type(exc).__name__,
                )
            try:
                snapshot = self._next_snapshot(safe_envelope)
                snapshot_persisted = self.snapshot_store.save(snapshot)
            except _SnapshotCatchUpUnavailableError:
                logger.warning("Skipping A2A pipeline snapshot save until journal catch-up succeeds")
            except Exception as exc:
                logger.warning("Failed to persist A2A pipeline snapshot: %s", sanitize_strict_text(str(exc)))
            if snapshot_persisted:
                _maybe_inject_test_fault("after_a2a_pipeline_snapshot_saved")
            if require_journal_metadata and not journal_persisted:
                logger.warning("Skipping A2A pipeline status update because journal metadata was not persisted")
                return None
            if durable_required and not (journal_persisted or snapshot_persisted):
                logger.warning("Skipping A2A pipeline status update because durable metadata was not persisted")
                return None
            if artifact_metadata is not None and not (journal_persisted or snapshot_persisted):
                logger.warning("Skipping A2A artifact update because pipeline metadata was not persisted")
                return None
        return safe_envelope

    def _persist_envelope_extreme(
        self,
        safe_envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None,
        durable_required: bool,
        require_journal_metadata: bool,
    ) -> dict[str, Any] | None:
        if self._can_defer_extreme_metadata(
            safe_envelope,
            artifact_metadata=artifact_metadata,
            durable_required=durable_required,
            require_journal_metadata=require_journal_metadata,
        ):
            self._extreme_pending_journal_events.append(safe_envelope)
            self._extreme_pending_snapshot_events.append(safe_envelope)
            if len(self._extreme_pending_journal_events) >= _EXTREME_JOURNAL_FLUSH_EVENTS:
                self._flush_extreme_journal_events()
            return safe_envelope

        journal_persisted = False
        snapshot_persisted = False
        self._flush_extreme_journal_events()
        try:
            self.journal.append(safe_envelope, durable=False)
            journal_persisted = True
        except Exception as exc:
            logger.warning(
                "Failed to append A2A pipeline journal event error_type=%s",
                type(exc).__name__,
            )

        try:
            snapshot_events = [*self._extreme_pending_snapshot_events, safe_envelope]
            if safe_envelope.get("eventType") == BACKUP_COMMITTED_EVENT_TYPE:
                journal_events = self.journal.read_all_repairing_tail()
                scoped_events = _events_for_envelope_identity(journal_events, safe_envelope)
                snapshot_events = _include_backup_ack_committed_event(
                    snapshot_events,
                    scoped_events,
                    safe_envelope,
                )
            snapshot = reduce_pipeline_events(snapshot_events, existing_snapshot=self._extreme_snapshot_base())
            snapshot_persisted = self.snapshot_store.save(snapshot, durable=False, compact=True)
            if snapshot_persisted:
                self._extreme_snapshot_cache = snapshot
                self._extreme_snapshot_loaded = True
                self._extreme_pending_snapshot_events.clear()
                _maybe_inject_test_fault("after_a2a_pipeline_snapshot_saved")
        except Exception as exc:
            logger.warning("Failed to persist A2A pipeline snapshot: %s", sanitize_strict_text(str(exc)))

        if require_journal_metadata and not journal_persisted:
            logger.warning("Skipping A2A pipeline status update because journal metadata was not persisted")
            return None
        if durable_required and not (journal_persisted or snapshot_persisted):
            logger.warning("Skipping A2A pipeline status update because durable metadata was not persisted")
            return None
        if artifact_metadata is not None and not (journal_persisted or snapshot_persisted):
            logger.warning("Skipping A2A artifact update because pipeline metadata was not persisted")
            return None
        return safe_envelope

    def _can_defer_extreme_metadata(
        self,
        envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None,
        durable_required: bool,
        require_journal_metadata: bool,
    ) -> bool:
        if artifact_metadata is not None or durable_required or require_journal_metadata:
            return False
        event_type = envelope.get("eventType")
        return isinstance(event_type, str) and event_type in _EXTREME_DEFERRED_EVENT_TYPES

    def _flush_extreme_journal_events(self) -> bool:
        if not self._extreme_pending_journal_events:
            return True
        events = list(self._extreme_pending_journal_events)
        try:
            self.journal.append_many(events, durable=False)
        except Exception as exc:
            logger.warning(
                "Failed to flush deferred A2A pipeline journal events error_type=%s",
                type(exc).__name__,
            )
            return False
        self._extreme_pending_journal_events.clear()
        return True

    def _extreme_snapshot_base(self) -> dict[str, Any] | None:
        if not self._extreme_snapshot_loaded:
            self._extreme_snapshot_cache = self.snapshot_store.load()
            self._extreme_snapshot_loaded = True
        return self._extreme_snapshot_cache

    async def enqueue_persisted(
        self,
        envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None = None,
        run_before_enqueue: bool = True,
        wait_for_transport: bool = False,
        local_envelope: dict[str, Any] | None = None,
    ) -> bool:
        async with self._delivery_guard():
            if run_before_enqueue:
                if not await self._run_before_enqueue_hook(envelope):
                    return False
            if artifact_metadata is not None:
                await self._enqueue_artifact_update(envelope, artifact_metadata)
            if local_envelope is not None:
                await self._forward_local_pipeline_envelope(local_envelope)
            await self._enqueue_status(envelope, wait_for_transport=wait_for_transport)
            self.last_envelope = envelope
        return True

    async def _forward_local_pipeline_envelope(self, local_envelope: dict[str, Any]) -> None:
        """Forward a single pre-remote-redaction envelope to the loopback Web sink, if any.

        This is the only bridge that streams pipeline progress into a watching Web
        session's live event buffer. The remote A2A transport (``_enqueue_status`` /
        ``_enqueue_status_batch``) does not feed it, so every enqueue path that should
        appear live in the browser must route through here.
        """
        local_enqueue = getattr(self.event_queue, "enqueue_local_pipeline_envelope", None)
        if local_enqueue is not None:
            await local_enqueue(dict(to_json_safe(local_envelope) or {}))

    async def enqueue_persisted_batch(
        self,
        envelopes: list[dict[str, Any]],
        *,
        wait_for_transport: bool = False,
        local_envelopes: list[dict[str, Any]] | None = None,
    ) -> int:
        if not envelopes:
            return 0

        frame_count = 0
        async with self._delivery_guard():
            if local_envelopes:
                for local_envelope in local_envelopes:
                    await self._forward_local_pipeline_envelope(local_envelope)
            deliverable = [envelope for envelope in envelopes if await self._run_before_enqueue_hook(envelope)]
            pending: list[dict[str, Any]] = []
            for envelope in deliverable:
                artifact = envelope.get("artifact")
                if envelope.get("eventType") == "artifact_created" and isinstance(artifact, dict):
                    frame_count += await self._enqueue_persisted_batches(
                        pending,
                        wait_for_transport=wait_for_transport,
                    )
                    pending = []
                    await self._enqueue_artifact_update(envelope, artifact)
                    frame_count += 1
                pending.append(envelope)
            frame_count += await self._enqueue_persisted_batches(
                pending,
                wait_for_transport=wait_for_transport,
            )
        return frame_count

    async def _enqueue_persisted_batches(
        self,
        envelopes: list[dict[str, Any]],
        *,
        wait_for_transport: bool,
    ) -> int:
        frame_count = 0
        for batch in _envelope_batches(envelopes):
            if len(batch) == 1:
                await self._enqueue_status(batch[0], wait_for_transport=wait_for_transport)
            else:
                await self._enqueue_status_batch(batch, wait_for_transport=wait_for_transport)
            self.last_envelope = batch[-1]
            frame_count += 1
        return frame_count

    async def _persist_and_enqueue(
        self,
        envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None = None,
        require_durable_metadata: bool = False,
        require_journal_metadata: bool = False,
    ) -> bool:
        return (
            await self._persist_and_enqueue_envelope(
                envelope,
                artifact_metadata=artifact_metadata,
                require_durable_metadata=require_durable_metadata,
                require_journal_metadata=require_journal_metadata,
            )
            is not None
        )

    async def _persist_and_enqueue_envelope(
        self,
        envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None = None,
        require_durable_metadata: bool = False,
        require_journal_metadata: bool = False,
    ) -> dict[str, Any] | None:
        if self._requires_backup_commit(envelope):
            return await self._persist_backup_gated_publication(
                envelope,
                artifact_metadata=artifact_metadata,
                require_durable_metadata=require_durable_metadata,
                require_journal_metadata=require_journal_metadata,
            )
        safe_envelope = await self.persist_envelope(
            envelope,
            artifact_metadata=artifact_metadata,
            require_durable_metadata=require_durable_metadata,
            require_journal_metadata=require_journal_metadata,
        )
        if safe_envelope is None:
            return None
        if not await self.enqueue_persisted(
            safe_envelope,
            artifact_metadata=artifact_metadata,
            local_envelope=envelope,
        ):
            return None
        return safe_envelope

    async def _persist_backup_gated_publication(
        self,
        envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None = None,
        require_durable_metadata: bool = False,
        require_journal_metadata: bool = False,
    ) -> dict[str, Any] | None:
        pending_envelope = pending_backup_publication_envelope(envelope)
        pending_safe_envelope = await self.persist_envelope(
            pending_envelope,
            artifact_metadata=artifact_metadata,
            require_durable_metadata=require_durable_metadata,
            require_journal_metadata=require_journal_metadata,
        )
        if pending_safe_envelope is None:
            return None
        async with self.delivery_transaction():
            committed_envelope = committed_backup_publication_envelope(
                self.translator,
                pending_envelope,
            )
            committed_safe_envelope = await self.persist_envelope(
                committed_envelope,
                artifact_metadata=artifact_metadata,
                require_durable_metadata=require_durable_metadata,
                require_journal_metadata=True,
            )
            if committed_safe_envelope is None:
                return None
            if not await self._run_before_enqueue_hook(committed_safe_envelope):
                return None
            ack_envelope = await self.persist_backup_committed_ack(committed_safe_envelope)
            if ack_envelope is None:
                return None
            if not await self.enqueue_persisted(
                committed_safe_envelope,
                artifact_metadata=artifact_metadata,
                run_before_enqueue=False,
                local_envelope=committed_envelope,
            ):
                return None
            if not await self.enqueue_persisted(
                backup_committed_delivery_envelope(ack_envelope, committed_safe_envelope),
                run_before_enqueue=False,
            ):
                return None
            await self._run_after_backup_commit_hook(committed_safe_envelope)
            return committed_safe_envelope

    async def persist_backup_committed_ack(self, committed_envelope: dict[str, Any]) -> dict[str, Any] | None:
        ack = self.translator.manual_event(
            BACKUP_COMMITTED_EVENT_TYPE,
            "pipeline",
            data={
                "committedEventId": committed_envelope.get("eventId"),
                "committedEventType": committed_envelope.get("eventType"),
                "committedSequence": committed_envelope.get("sequence"),
            },
        )
        ack.pop("status", None)
        return await self.persist_envelope(ack, require_durable_metadata=True, require_journal_metadata=True)

    async def _run_before_enqueue_hook(self, envelope: dict[str, Any]) -> bool:
        if self.before_enqueue is None:
            return True
        should_enqueue = self.before_enqueue(envelope)
        if inspect.isawaitable(should_enqueue):
            should_enqueue = await should_enqueue
        return should_enqueue is not False

    async def _run_after_backup_commit_hook(self, envelope: dict[str, Any]) -> None:
        if self.after_backup_commit is None:
            return
        result = self.after_backup_commit(envelope)
        if inspect.isawaitable(result):
            await result

    def _requires_backup_commit(self, envelope: dict[str, Any]) -> bool:
        if self.backup_commit_gate is None:
            return False
        return bool(self.backup_commit_gate(envelope))

    def delivery_transaction(self):
        return self._delivery_guard()

    @contextlib.asynccontextmanager
    async def _delivery_guard(self):
        owner = asyncio.current_task()
        if owner is not None and self._delivery_lock_owner is owner:
            self._delivery_lock_depth += 1
            try:
                yield
            finally:
                self._delivery_lock_depth -= 1
            return

        async with self._delivery_lock:
            self._delivery_lock_owner = owner
            self._delivery_lock_depth = 1
            try:
                yield
            finally:
                self._delivery_lock_depth = 0
                self._delivery_lock_owner = None

    def _annotate_delivery_alias(self, envelope: dict[str, Any]) -> None:
        delivery_task_id = self._delivery_task_id(envelope)
        delivery_context_id = self._delivery_context_id(envelope)
        if delivery_task_id != str(envelope.get("taskId")):
            envelope["deliveryTaskId"] = delivery_task_id
        if delivery_context_id != str(envelope.get("contextId")):
            envelope["deliveryContextId"] = delivery_context_id

    def _ensure_monotonic_sequence(self, envelope: dict[str, Any]) -> None:
        current = _int_value(envelope.get("sequence"), 0)
        previous = self._last_persisted_sequence()
        if current <= previous:
            envelope["sequence"] = previous + 1
            current = previous + 1
        self._last_sequence = max(self._last_sequence, current)

    def _ensure_monotonic_sequence_fast(self, envelope: dict[str, Any]) -> None:
        current = _int_value(envelope.get("sequence"), 0)
        if current <= self._last_sequence:
            envelope["sequence"] = self._last_sequence + 1
            current = self._last_sequence + 1
        self._last_sequence = max(self._last_sequence, current)

    def _last_persisted_sequence(self) -> int:
        sequence = self._last_sequence
        snapshot = self.snapshot_store.load()
        if isinstance(snapshot, dict):
            sequence = max(sequence, _int_value(snapshot.get("lastSequence"), 0))
        try:
            journal_sequence = max(
                (_int_value(event.get("sequence"), 0) for event in self.journal.read_all_repairing_tail()),
                default=0,
            )
        except Exception as exc:
            logger.warning(
                "Failed to read A2A pipeline journal high-water sequence error_type=%s",
                type(exc).__name__,
            )
            raise _SequenceHighWaterUnavailableError from None
        return max(sequence, journal_sequence)

    async def _maybe_externalize_artifact(
        self,
        envelope: dict[str, Any],
        tool_result: ToolResultEvent | None,
    ) -> dict[str, Any] | None:
        event_type = envelope.get("eventType")
        if event_type not in {"tool_result", "artifact_created"}:
            return None
        if event_type == "tool_result" and tool_result is None:
            return None

        try:
            if event_type == "artifact_created":
                original_artifact = envelope.get("artifact")
                result = {"artifact": original_artifact}
            elif tool_result is not None:
                original_artifact = None
                result = tool_result.result
            else:
                return None
            artifact_semantic_metadata = _artifact_semantic_metadata(original_artifact, envelope.get("data"))
            artifact_metadata = _extract_artifact_metadata(result, self.artifact_store)
        except Exception as exc:
            logger.warning(
                "Failed to externalize A2A pipeline tool artifact: %s",
                sanitize_strict_text(str(exc)),
            )
            return None
        if artifact_metadata is None:
            return None

        envelope["eventType"] = "artifact_created"
        envelope["scope"] = envelope.get("scope") or "pipeline"
        envelope["status"] = "working"
        envelope["artifact"] = {**artifact_metadata, **artifact_semantic_metadata}
        base_data: dict[str, Any] = {}
        envelope_data = envelope.get("data")
        if event_type == "artifact_created" and isinstance(envelope_data, dict):
            base_data = dict(envelope_data)
        envelope["data"] = {
            **base_data,
            "artifactId": artifact_metadata.get("artifactId"),
            "filename": artifact_metadata.get("filename"),
            "mediaType": artifact_metadata.get("mediaType"),
            "byteSize": artifact_metadata.get("byteSize"),
            "sha256": artifact_metadata.get("sha256"),
            "uri": artifact_metadata.get("uri"),
            **artifact_semantic_metadata,
        }
        if tool_result is not None and A2AExposureType.TOOL_TRACE in self.exposure_types:
            envelope["data"].update(
                {
                    "toolName": tool_result.tool_name,
                    "toolUseId": tool_result.tool_use_id,
                    "isError": tool_result.is_error,
                }
            )
        return artifact_metadata

    async def _enqueue_artifact_update(self, envelope: dict[str, Any], artifact_metadata: dict[str, Any]) -> None:
        event = _artifact_update_event(
            task_id=self._delivery_task_id(envelope),
            context_id=self._delivery_context_id(envelope),
            metadata=artifact_metadata,
        )
        await self.event_queue.enqueue_event(event)

    async def _apply_permission_metadata(
        self,
        request: PermissionRequestEvent,
        envelope: dict[str, Any],
        *,
        permission_resolver: PipelinePermissionResolver | None,
        auto_approve_permissions: bool,
    ) -> bool:
        approved = await self.resolve_permission_event(
            request,
            permission_resolver=permission_resolver,
            auto_approve_permissions=auto_approve_permissions,
        )
        self._set_permission_metadata(request, envelope, approved=approved)
        return approved

    def _set_permission_metadata(
        self,
        request: PermissionRequestEvent,
        envelope: dict[str, Any],
        *,
        approved: bool,
    ) -> None:
        include_tool_input = A2AExposureType.TOOL_TRACE in self.exposure_types
        permission = envelope.setdefault("permission", {})
        permission.clear()
        permission.update(safe_permission_metadata(request, include_tool_input=include_tool_input))
        permission.update(_permission_approval_metadata(approved))

    async def _enqueue_status(self, envelope: dict[str, Any], *, wait_for_transport: bool = False) -> None:
        task_id = self._delivery_task_id(envelope)
        context_id = self._delivery_context_id(envelope)
        update = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=TaskStatus(
                state=_a2a_task_state_name(envelope),
            ),
        )
        ParseDict({"iac_code": {"pipeline": envelope}}, update.metadata)
        await self._enqueue_transport_event(update, wait_for_transport=wait_for_transport)

    async def _enqueue_status_batch(
        self,
        envelopes: list[dict[str, Any]],
        *,
        wait_for_transport: bool = False,
    ) -> None:
        final_envelope = envelopes[-1]
        update = TaskStatusUpdateEvent(
            task_id=self._delivery_task_id(final_envelope),
            context_id=self._delivery_context_id(final_envelope),
            status=TaskStatus(
                state=_a2a_task_state_name(final_envelope),
            ),
        )
        ParseDict(
            {
                "iac_code": {
                    "pipelineBatch": _pipeline_batch_payload(envelopes),
                }
            },
            update.metadata,
        )
        await self._enqueue_transport_event(update, wait_for_transport=wait_for_transport)

    async def _enqueue_transport_event(self, event: Any, *, wait_for_transport: bool) -> None:
        wait_for_transport = wait_for_transport or pipeline_transport_delivery_is_required()
        if not wait_for_transport or not pipeline_transport_delivery_tracking_enabled():
            await self.event_queue.enqueue_event(event)
            return

        stage_observer = self.flow_monitor.transport_stage_changed if self.flow_monitor is not None else None
        fallback_tracker = routed_pipeline_transport_delivery_tracker(
            task_id=self.delivery_task_id or self.translator.context.task_id,
            context_id=self.delivery_context_id or self.translator.context.context_id,
        )
        completion = register_pipeline_transport_delivery(
            event,
            fallback_tracker=fallback_tracker,
            stage_observer=stage_observer,
        )
        try:
            if completion.done():
                await completion
            await self.event_queue.enqueue_event(event)
            mark_pipeline_transport_delivery_enqueued(event)
            await asyncio.shield(completion)
        finally:
            discard_pipeline_transport_delivery(event)

    def _delivery_task_id(self, envelope: dict[str, Any]) -> str:
        return self.delivery_task_id or str(envelope["taskId"])

    def _delivery_context_id(self, envelope: dict[str, Any]) -> str:
        return self.delivery_context_id or str(envelope["contextId"])


def _permission_request_from(event: Any) -> PermissionRequestEvent | None:
    inner = _unwrap_stream_event(event)
    return inner if isinstance(inner, PermissionRequestEvent) else None


def _tool_result_from(event: Any) -> ToolResultEvent | None:
    inner = _unwrap_stream_event(event)
    return inner if isinstance(inner, ToolResultEvent) else None


def _artifact_semantic_metadata(*sources: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _ARTIFACT_SEMANTIC_METADATA_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                metadata.setdefault(key, value)
    return metadata


def _unwrap_stream_event(event: Any) -> Any:
    while isinstance(event, SubPipelineStreamEvent):
        event = event.inner
    return event


def _envelope_batches(envelopes: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    batch_size = _encoded_pipeline_batch_size([])
    for envelope in envelopes:
        envelope_size = _encoded_pipeline_envelope_size(envelope)
        separator_size = 1 if batch else 0
        candidate_size = batch_size + separator_size + envelope_size
        if batch and (len(batch) >= OUTBOUND_HARD_MAX_BATCH_EVENTS or candidate_size > OUTBOUND_HARD_MAX_BATCH_BYTES):
            yield batch
            batch = []
            batch_size = _encoded_pipeline_batch_size([])
            candidate_size = batch_size + envelope_size
        batch.append(envelope)
        batch_size = candidate_size
    if batch:
        yield batch


def _pipeline_batch_payload(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": PIPELINE_METADATA_SCHEMA_VERSION,
        "extensionUri": PIPELINE_EVENTS_EXTENSION_URI,
        "events": envelopes,
    }


def _encoded_pipeline_batch_size(envelopes: list[dict[str, Any]]) -> int:
    payload = json.dumps(
        _pipeline_batch_payload(envelopes),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload)


def _encoded_pipeline_envelope_size(envelope: dict[str, Any]) -> int:
    payload = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload)


def _resolve_permission_future(request: PermissionRequestEvent, approved: bool) -> bool:
    future = request.response_future
    if future is not None and not future.done():
        future.set_result(approved)
        return True
    return False


def _can_resolve_permission_future(request: PermissionRequestEvent) -> bool:
    return request.response_future is not None and not request.response_future.done()


def _set_permission_approval(envelope: dict[str, Any], approved: bool) -> None:
    permission = envelope.setdefault("permission", {})
    permission.update(_permission_approval_metadata(approved))


def _permission_approval_metadata(approved: bool) -> dict[str, Any]:
    return {"approved": approved, "decision": "allow_once" if approved else "deny"}


def pending_backup_publication_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    pending = dict(to_json_safe(envelope) or {})
    pending["visibility"] = PENDING_BACKUP_VISIBILITY
    return pending


def committed_backup_publication_envelope(
    translator: PipelineEventTranslator,
    pending_envelope: dict[str, Any],
) -> dict[str, Any]:
    pending_data = pending_envelope.get("data")
    data = dict(pending_data) if isinstance(pending_data, dict) else {}
    envelope = translator.manual_event(
        str(pending_envelope.get("eventType") or ""),
        str(pending_envelope.get("scope") or "pipeline"),
        status=str(pending_envelope.get("status") or "working"),
        data=data,
    )
    envelope["visibility"] = COMMITTED_BACKUP_VISIBILITY
    created_at = pending_envelope.get("createdAt")
    if isinstance(created_at, str) and created_at:
        envelope["createdAt"] = created_at
    for key in ("step", "candidate", "candidateStep"):
        value = pending_envelope.get(key)
        if isinstance(value, dict):
            envelope[key] = dict(value)
    return envelope


def is_recovery_semantic_event(envelope: dict[str, Any]) -> bool:
    event_type = envelope.get("eventType")
    event_type = event_type if isinstance(event_type, str) else None
    if event_type == "mcp_status":
        return False
    if event_type in _DISPLAY_ONLY_EVENT_TYPES:
        return False
    if event_type in _RECOVERY_SEMANTIC_EVENT_TYPES:
        return True
    status = envelope.get("status")
    status = status if isinstance(status, str) else None
    if status in {"waiting_input", "input_required", "completed", "failed", "canceled"}:
        return True
    scope = envelope.get("scope")
    scope = scope if isinstance(scope, str) else None
    return scope in _RECOVERY_STATE_SCOPES and status in _RECOVERY_STATE_STATUSES


def _should_skip_envelope(envelope: dict[str, Any], *, exposure_types: frozenset[A2AExposureType]) -> bool:
    event_type = envelope.get("eventType")
    if event_type == "text_delta":
        return _text_from_envelope(envelope) == ""
    if event_type == "thinking_delta":
        return A2AExposureType.RAW_THINKING not in exposure_types
    return False


def _maybe_inject_test_fault(point: str) -> None:
    if os.environ.get("IAC_CODE_TEST_FAULT_INJECTION") != "1":
        return
    if os.environ.get("IAC_CODE_TEST_CRASH_AT") != point:
        return
    mode = os.environ.get("IAC_CODE_TEST_FAULT_INJECTION_MODE", "exit")
    if mode == "raise":
        raise RuntimeError(f"Injected test fault at {point}")
    os._exit(97)


def _text_from_envelope(envelope: dict[str, Any]) -> str:
    data = envelope.get("data")
    text = data.get("text") if isinstance(data, dict) else ""
    return text if isinstance(text, str) else ""


def _a2a_task_state_name(envelope: dict[str, Any]) -> str:
    status = envelope.get("status")
    if status in {"waiting_input", "input_required"}:
        return TaskState.Name(TaskState.TASK_STATE_INPUT_REQUIRED)
    if status == "failed":
        return TaskState.Name(TaskState.TASK_STATE_FAILED)
    if status == "canceled":
        return TaskState.Name(TaskState.TASK_STATE_CANCELED)
    if status == "completed":
        return TaskState.Name(TaskState.TASK_STATE_COMPLETED)
    return TaskState.Name(TaskState.TASK_STATE_WORKING)


def _events_for_envelope_identity(events: list[dict[str, Any]], envelope: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("taskId") == envelope.get("taskId") and event.get("contextId") == envelope.get("contextId")
    ]


def _snapshot_matches_envelope_identity(snapshot: dict[str, Any], envelope: dict[str, Any]) -> bool:
    return snapshot.get("taskId") == envelope.get("taskId") and snapshot.get("contextId") == envelope.get("contextId")


def _snapshot_schema_is_current(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("schemaVersion") == SNAPSHOT_SCHEMA_VERSION


def _include_backup_ack_committed_event(
    catch_up_events: list[dict[str, Any]],
    scoped_events: list[dict[str, Any]],
    envelope: dict[str, Any],
) -> list[dict[str, Any]]:
    if envelope.get("eventType") != BACKUP_COMMITTED_EVENT_TYPE:
        return catch_up_events
    committed_event = _matching_committed_event_for_backup_ack(scoped_events, envelope)
    if committed_event is None:
        return catch_up_events
    committed_event_id = committed_event.get("eventId")
    if committed_event_id is not None and any(event.get("eventId") == committed_event_id for event in catch_up_events):
        return catch_up_events
    return [committed_event, *catch_up_events]


def _matching_committed_event_for_backup_ack(
    events: list[dict[str, Any]],
    ack_envelope: dict[str, Any],
) -> dict[str, Any] | None:
    data = ack_envelope.get("data")
    data = data if isinstance(data, dict) else {}
    committed_event_id = data.get("committedEventId")
    committed_event_type = data.get("committedEventType")
    committed_sequence = _int_value(data.get("committedSequence"), 0)
    for event in events:
        if event.get("visibility") != COMMITTED_BACKUP_VISIBILITY:
            continue
        if committed_event_id is not None and event.get("eventId") == committed_event_id:
            return event
        if (
            _int_value(event.get("sequence"), 0) == committed_sequence
            and event.get("eventType") == committed_event_type
        ):
            return event
    return None


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "COMMITTED_BACKUP_VISIBILITY",
    "PENDING_BACKUP_VISIBILITY",
    "PipelineA2AEventPublisher",
    "PipelineAfterBackupCommitHook",
    "PipelineBackupCommitGate",
    "PipelineBeforeEnqueueHook",
    "PipelinePermissionResolver",
    "committed_backup_publication_envelope",
    "is_recovery_semantic_event",
    "pending_backup_publication_envelope",
]
