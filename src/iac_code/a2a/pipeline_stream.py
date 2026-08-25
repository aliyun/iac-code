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
from iac_code.a2a.input_required import PendingPermission, PermissionResponse
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
from iac_code.a2a.runtime_overrides import get_a2a_preferred_language
from iac_code.i18n import translate_message
from iac_code.pipeline.constants import (
    PIPELINE_EVENT_CLEANUP_COMPLETED,
    PIPELINE_EVENT_CLEANUP_FAILED,
    PIPELINE_EVENT_CLEANUP_PROGRESS,
    PIPELINE_EVENT_CLEANUP_STARTED,
)
from iac_code.services.permissions.audit import (
    emit_permission_boundary_audit,
    is_aliyun_api_non_read_only_permission_event,
)
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent, ToolResultEvent
from iac_code.utils.public_errors import sanitize_strict_text

PipelinePermissionResolver = Callable[[PermissionRequestEvent], bool | Awaitable[bool]]
PipelineBeforeEnqueueHook = Callable[[dict[str, Any]], bool | Awaitable[bool]]
PipelineAfterEnqueueHook = Callable[[dict[str, Any]], None | Awaitable[None]]
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
    "permission_resolved",
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
        permission_input_registry: Any | None = None,
        task_store: Any | None = None,
        delivery_task_id: str | None = None,
        delivery_context_id: str | None = None,
        before_enqueue: PipelineBeforeEnqueueHook | None = None,
        after_enqueue: PipelineAfterEnqueueHook | None = None,
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
        self.permission_input_registry = permission_input_registry
        self.task_store = task_store
        self.delivery_task_id = delivery_task_id
        self.delivery_context_id = delivery_context_id
        self.before_enqueue = before_enqueue
        self.after_enqueue = after_enqueue
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
        self.permission_resolution_owner = _PipelinePermissionResolutionOwner(self)

    async def publish_sub_pipeline_permission(self, event: Any) -> str | None:
        """Publish only a wrapped Sub Pipeline permission without waiting for its Future."""

        permission_request = _sub_pipeline_permission_request_from(event)
        if permission_request is None:
            raise TypeError("Expected a Sub Pipeline permission request event")
        if self.permission_input_registry is None:
            raise PipelineA2APersistenceError("Sub Pipeline permission registry is unavailable")

        envelopes = self.translator.translate(event)
        permission_envelope = next(
            (envelope for envelope in envelopes if envelope.get("eventType") == "permission_requested"),
            None,
        )
        if permission_envelope is None:
            raise PipelineA2APersistenceError("Sub Pipeline permission could not be translated")
        coordinates = {
            key: dict(value)
            for key in ("step", "candidate", "candidateStep")
            if isinstance((value := permission_envelope.get(key)), dict)
        }
        pending = await self.permission_input_registry.register(
            permission_request,
            task_id=self.translator.context.task_id,
            context_id=self.translator.context.context_id,
            resolution_owner=self.permission_resolution_owner,
            scope=str(permission_envelope.get("scope") or "candidate"),
            coordinates=coordinates,
        )
        permission = permission_envelope.setdefault("permission", {})
        permission.clear()
        permission.update(safe_permission_metadata(permission_request, include_tool_input=False))
        permission.update({"permissionId": pending.input_id, "pending": True, "inputId": pending.input_id})
        permission_envelope["status"] = "working"

        try:
            if not await self._commit_permission_control_event(permission_envelope):
                raise PipelineA2APersistenceError("Failed to publish Sub Pipeline permission request")
        except BaseException:
            await self.permission_resolution_owner.fail_permission(pending)
            raise
        return None

    async def _commit_permission_control_event(
        self,
        envelope: dict[str, Any],
        *,
        excluding_pending: set[str] | None = None,
    ) -> bool:
        """Commit a permission frame and its public pending projection in one delivery order."""

        async with self.delivery_transaction():
            safe_envelope = await self.persist_envelope(envelope, require_durable_metadata=True)
            if safe_envelope is None:
                return False
            if not await self._run_before_enqueue_hook(safe_envelope):
                return False
            if self.task_store is not None and self.permission_input_registry is not None:
                pending = await self.permission_input_registry.pending_envelopes(
                    self.translator.context.task_id,
                    excluding=excluding_pending,
                )
                await self.task_store.set_pending_permissions(self.translator.context.task_id, pending)
            return await self.enqueue_persisted(
                safe_envelope,
                run_before_enqueue=False,
                local_envelope=envelope,
            )

    async def publish_permission_resolution(
        self,
        pending: PendingPermission,
        *,
        decision: str,
        canceled: bool = False,
    ) -> bool:
        permission = {
            "permissionId": pending.input_id,
            "inputId": pending.input_id,
            "toolUseId": pending.request.tool_use_id,
            "toolName": pending.request.tool_name,
            "decision": decision,
            "pending": False,
        }
        if canceled:
            permission["canceled"] = True
        envelope = self.translator.manual_event(
            "permission_resolved",
            pending.scope,
            status="working",
            data={"kind": "permission", **permission},
        )
        envelope["permission"] = permission
        for key, value in (pending.coordinates or {}).items():
            if key in {"step", "candidate", "candidateStep"} and isinstance(value, dict):
                envelope[key] = dict(value)
        return await self._commit_permission_control_event(envelope, excluding_pending={pending.input_id})

    async def publish(
        self,
        event: Any,
        *,
        permission_resolver: PipelinePermissionResolver | None = None,
        auto_approve_permissions: bool = False,
    ) -> str | None:
        if (
            _sub_pipeline_permission_request_from(event) is not None
            and self.permission_input_registry is not None
            and permission_resolver is None
            and not auto_approve_permissions
        ):
            return await self.publish_sub_pipeline_permission(event)
        envelopes = self.translator.translate(event)
        permission_request = _permission_request_from(event)
        tool_result = _tool_result_from(event)
        text_parts: list[str] = []
        interactive_permission = (
            permission_request is not None
            and self.permission_input_registry is not None
            and permission_resolver is None
            and not auto_approve_permissions
        )
        pending_permission = None
        if interactive_permission:
            pending_permission = await self.permission_input_registry.register(
                permission_request,
                task_id=self.translator.context.task_id,
                context_id=self.translator.context.context_id,
            )

        try:
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

                if permission_request is not None and interactive_permission:
                    assert pending_permission is not None
                    permission = envelope.setdefault("permission", {})
                    permission.clear()
                    permission.update(safe_permission_metadata(permission_request, include_tool_input=False))
                    permission.update({"pending": True, "inputId": pending_permission.input_id})
                    envelope["status"] = "input_required"
                    approved = None
                    permission_audit_emitted = False
                elif permission_request is not None:
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
                if permission_request is not None and interactive_permission:
                    if not persisted:
                        assert pending_permission is not None
                        await self.permission_input_registry.fail(pending_permission)
                elif permission_request is not None:
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

            if interactive_permission:
                assert permission_request is not None
                future = permission_request.response_future
                if future is None:
                    assert pending_permission is not None
                    await self.permission_input_registry.fail(pending_permission)
                else:
                    await asyncio.shield(future)
        except BaseException:
            if pending_permission is not None:
                assert self.permission_input_registry is not None
                await self.permission_input_registry.fail(pending_permission)
            raise
        finally:
            if pending_permission is not None:
                assert self.permission_input_registry is not None
                await self.permission_input_registry.complete(pending_permission)

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
            await self._run_after_enqueue_hook(envelope)
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
            for envelope in deliverable:
                await self._run_after_enqueue_hook(envelope)
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

    async def _run_after_enqueue_hook(self, envelope: dict[str, Any]) -> None:
        if self.after_enqueue is None:
            return
        result = self.after_enqueue(envelope)
        if inspect.isawaitable(result):
            await result

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
        iac_code_metadata: dict[str, Any] = {"pipeline": envelope}
        input_projection = self._unified_input_projection(envelope)
        if input_projection is not None:
            iac_code_metadata["input"] = input_projection
        ParseDict({"iac_code": iac_code_metadata}, update.metadata)
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
                    **(
                        {"input": input_projection}
                        if (input_projection := self._unified_input_projection(final_envelope)) is not None
                        else {}
                    ),
                }
            },
            update.metadata,
        )
        await self._enqueue_transport_event(update, wait_for_transport=wait_for_transport)

    def _unified_input_projection(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        step = envelope.get("step")
        step_id = step.get("id") if isinstance(step, dict) else None
        kind_hint = self.translator.context.parent_step_ui_modes.get(step_id) if isinstance(step_id, str) else None
        return _unified_input_projection(envelope, kind_hint=kind_hint)

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


class _PipelinePermissionResolutionOwner:
    """Serialize replies, cancellation, journal order, and Future completion for one Task."""

    def __init__(self, publisher: PipelineA2AEventPublisher) -> None:
        self.publisher = publisher
        self._lock = asyncio.Lock()

    async def resolve_permission(self, pending: PendingPermission, response: PermissionResponse) -> bool:
        registry = self.publisher.permission_input_registry
        if registry is None:
            raise PipelineA2APersistenceError("Sub Pipeline permission registry is unavailable")
        async with self._lock:
            await registry.claim(pending, response)
            approved = response.decision == "allow_once"
            audit_ok = emit_permission_boundary_audit(
                pending.request,
                decision="allow" if approved else "deny",
                scope="a2a_sub_pipeline_permission",
                source="a2a_user_permission",
                reason_type="user_decision",
                reason_detail=response.decision,
            )
            if approved and not audit_ok:
                approved = False
            decision = "allow_once" if approved else "deny"
            try:
                committed = await self.publisher.publish_permission_resolution(pending, decision=decision)
            except BaseException:
                await self._finish_failed(pending)
                raise
            if not committed:
                await self._finish_failed(pending)
                raise PipelineA2APersistenceError("Failed to publish Sub Pipeline permission resolution")
            future = pending.request.response_future
            if future is None or future.done():
                await registry.complete(pending)
                raise PipelineA2APersistenceError("Sub Pipeline permission wait point is unavailable")
            future.set_result(approved)
            await registry.complete(pending)
            return approved

    async def fail_permission(self, pending: PendingPermission) -> None:
        async with self._lock:
            await self._finish_failed(pending)

    async def cancel_permissions(self, task_id: str) -> None:
        registry = self.publisher.permission_input_registry
        if registry is None:
            return
        async with self._lock:
            pending_permissions = await registry.claim_for_cancel(task_id, self)
            for pending in pending_permissions:
                emit_permission_boundary_audit(
                    pending.request,
                    decision="deny",
                    scope="a2a_sub_pipeline_permission",
                    source="a2a_task_cancel",
                    reason_type="task_canceled",
                    reason_detail="task canceled while permission was pending",
                )
                try:
                    await self.publisher.publish_permission_resolution(pending, decision="deny", canceled=True)
                except Exception:
                    logger.warning("Failed to publish canceled Sub Pipeline permission", exc_info=True)
                finally:
                    future = pending.request.response_future
                    if future is not None and not future.done():
                        future.set_result(False)
                    await registry.complete(pending)
            if self.publisher.task_store is not None:
                remaining = await registry.pending_envelopes(task_id)
                await self.publisher.task_store.set_pending_permissions(task_id, remaining)

    async def _finish_failed(self, pending: PendingPermission) -> None:
        registry = self.publisher.permission_input_registry
        if registry is None:
            return
        emit_permission_boundary_audit(
            pending.request,
            decision="deny",
            scope="a2a_sub_pipeline_permission",
            source="a2a_permission_resume_invalid",
            reason_type="permission_resume_invalid",
            reason_detail="permission control event could not be committed",
        )
        future = pending.request.response_future
        if future is not None and not future.done():
            future.set_result(False)
        await registry.complete(pending)
        if self.publisher.task_store is not None:
            remaining = await registry.pending_envelopes(pending.task_id)
            await self.publisher.task_store.set_pending_permissions(pending.task_id, remaining)


def _permission_request_from(event: Any) -> PermissionRequestEvent | None:
    inner = _unwrap_stream_event(event)
    return inner if isinstance(inner, PermissionRequestEvent) else None


def _sub_pipeline_permission_request_from(event: Any) -> PermissionRequestEvent | None:
    if not isinstance(event, SubPipelineStreamEvent):
        return None
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


def _unified_input_projection(
    envelope: dict[str, Any],
    *,
    kind_hint: str | None = None,
) -> dict[str, Any] | None:
    task_id = envelope.get("taskId")
    context_id = envelope.get("contextId")
    if not isinstance(task_id, str) or not isinstance(context_id, str):
        return None
    permission = envelope.get("permission")
    if isinstance(permission, dict) and permission.get("pending") is True:
        input_id = permission.get("inputId")
        tool_use_id = permission.get("toolUseId")
        tool_name = permission.get("toolName")
        safe_summary = permission.get("safeSummary")
        title = permission.get("title")
        purpose = permission.get("purpose")
        effect = permission.get("effect")
        target = permission.get("target")
        is_read_only = permission.get("isReadOnly")
        prompt = permission.get("prompt")
        options = permission.get("options")
        language = permission.get("language")
        deployment_summary = permission.get("deploymentSummary")
        if not all(isinstance(value, str) and value for value in (input_id, tool_use_id, tool_name, safe_summary)):
            return None
        fallback_language = language if isinstance(language, str) and language else "en"
        if not isinstance(title, str) or not title:
            title = translate_message("Run {tool}", language=fallback_language).format(tool=tool_name)
        if not isinstance(purpose, str) or not purpose:
            purpose = translate_message(
                "Run this operation for the requested infrastructure task.", language=fallback_language
            )
        if not isinstance(effect, str) or not effect:
            effect = "unknown"
        if not isinstance(target, str) or not target:
            target = translate_message("the current task scope", language=fallback_language)
        if not isinstance(is_read_only, bool):
            is_read_only = False
        if not isinstance(prompt, str) or not prompt:
            prompt = translate_message("{title} Allow once?", language=fallback_language).format(title=title)
        if not isinstance(options, list) or not options:
            options = [
                {"id": "allow_once", "label": translate_message("Allow once", language=fallback_language)},
                {"id": "deny", "label": translate_message("Deny", language=fallback_language)},
            ]
        projection = {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": task_id,
            "contextId": context_id,
            "inputId": input_id,
            "toolUseId": tool_use_id,
            "toolName": tool_name,
            "title": title,
            "purpose": purpose,
            "effect": effect,
            "target": target,
            "isReadOnly": is_read_only,
            "prompt": prompt,
            "safeSummary": safe_summary,
            "options": options,
            "required": True,
        }
        if isinstance(language, str) and language:
            projection["language"] = language
        if isinstance(deployment_summary, dict):
            projection["deploymentSummary"] = deployment_summary
        return projection
    raw_input = envelope.get("input")
    if not isinstance(raw_input, dict):
        raw_input = envelope.get("data") if envelope.get("eventType") == "input_required" else None
    if not isinstance(raw_input, dict):
        return None
    kind = raw_input.get("kind") or kind_hint
    if kind not in {"ask_user_question", "candidate_selection"}:
        return None
    projected: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": kind,
        "requestTaskId": task_id,
        "contextId": context_id,
        "inputId": str(raw_input.get("inputId") or "input-{}".format(envelope.get("eventId") or task_id)),
        "prompt": str(
            raw_input.get("prompt")
            or raw_input.get("question")
            or translate_message("Input required", language=get_a2a_preferred_language() or "en")
        )[:1000],
        "required": True,
    }
    if kind == "ask_user_question":
        projected["allowFreeText"] = bool(raw_input.get("allowFreeText"))
        free_text_prompt = raw_input.get("freeTextPrompt")
        if isinstance(free_text_prompt, str) and free_text_prompt:
            projected["freeTextPrompt"] = free_text_prompt[:500]
    raw_options = raw_input.get("options")
    options: list[dict[str, Any]] = []
    if isinstance(raw_options, list):
        for index, option in enumerate(raw_options[:50]):
            if isinstance(option, str):
                options.append({"id": option[:200], "label": option[:300]})
                continue
            if not isinstance(option, dict):
                continue
            option_id = option.get("id")
            if option_id is None:
                option_id = option.get("candidate_index", option.get("index", index))
            label = option.get("label") or option.get("name") or option.get("candidate_name") or option_id
            projected_option: dict[str, Any] = {"id": str(option_id)[:200], "label": str(label)[:300]}
            if kind == "candidate_selection":
                summary = option.get("summary")
                architecture_diagram = option.get("architecture_diagram") or option.get("architectureDiagram")
                total_monthly_cost = option.get("total_monthly_cost") or option.get("totalMonthlyCost")
                if isinstance(summary, str) and summary:
                    projected_option["summary"] = summary[:600]
                if isinstance(architecture_diagram, str) and architecture_diagram:
                    projected_option["architectureDiagram"] = architecture_diagram[:1600]
                if isinstance(total_monthly_cost, str) and total_monthly_cost:
                    projected_option["totalMonthlyCost"] = total_monthly_cost[:300]
                planning_estimate = option.get("planning_monthly_estimate") or option.get("planningMonthlyEstimate")
                if isinstance(planning_estimate, str) and planning_estimate:
                    projected_option["planningMonthlyEstimate"] = planning_estimate[:300]
                caliber_note = option.get("cost_caliber_note") or option.get("costCaliberNote")
                if isinstance(caliber_note, str) and caliber_note:
                    projected_option["costCaliberNote"] = caliber_note[:600]
                raw_cost_items = option.get("cost_items") or option.get("costItems")
                cost_items: list[dict[str, str]] = []
                if isinstance(raw_cost_items, list):
                    for raw_cost_item in raw_cost_items[:12]:
                        if not isinstance(raw_cost_item, dict):
                            continue
                        name = raw_cost_item.get("name")
                        spec = raw_cost_item.get("spec")
                        monthly_cost = raw_cost_item.get("monthly_cost") or raw_cost_item.get("monthlyCost")
                        item: dict[str, str] = {}
                        if isinstance(name, str) and name:
                            item["name"] = name[:200]
                        if isinstance(spec, str) and spec:
                            item["spec"] = spec[:300]
                        if isinstance(monthly_cost, str) and monthly_cost:
                            item["monthlyCost"] = monthly_cost[:300]
                        if item:
                            cost_items.append(item)
                if cost_items:
                    projected_option["costItems"] = cost_items
            options.append(projected_option)
    projected["options"] = options
    return projected


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
