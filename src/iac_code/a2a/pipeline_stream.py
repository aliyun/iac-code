from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
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
from iac_code.a2a.pipeline_events import PipelineEventTranslator, safe_permission_metadata
from iac_code.a2a.pipeline_journal import A2APipelineJournal, to_json_safe
from iac_code.a2a.pipeline_snapshot import SNAPSHOT_SCHEMA_VERSION, A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.pipeline.constants import (
    PIPELINE_EVENT_CLEANUP_COMPLETED,
    PIPELINE_EVENT_CLEANUP_FAILED,
    PIPELINE_EVENT_CLEANUP_PROGRESS,
    PIPELINE_EVENT_CLEANUP_STARTED,
)
from iac_code.services.permissions.audit import is_aliyun_api_non_read_only_permission_event
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent, ToolResultEvent

PipelinePermissionResolver = Callable[[PermissionRequestEvent], bool | Awaitable[bool]]
logger = logging.getLogger(__name__)
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
    "tool_result",
}
_RECOVERY_STATE_SCOPES = {"step", "candidate", "candidateStep", "candidate_step"}
_RECOVERY_STATE_STATUSES = {"working"}


class _SnapshotCatchUpUnavailableError(Exception):
    pass


class _SequenceHighWaterUnavailableError(Exception):
    pass


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
    ) -> None:
        self.event_queue = event_queue
        self.translator = translator
        self.journal = journal
        self.snapshot_store = snapshot_store
        self.artifact_store = artifact_store
        self.exposure_types = normalize_a2a_exposure_types(exposure_types)
        self.delivery_task_id = delivery_task_id
        self.delivery_context_id = delivery_context_id
        self._sequence_lock = asyncio.Lock()
        self._last_sequence = 0
        self.last_envelope: dict[str, Any] | None = None

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
                envelope.get("eventType") == "tool_result"
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
    ) -> dict[str, Any] | None:
        envelope = self.translator.manual_event(event_type, scope, status=status, data=data)
        if coordinates:
            for key in ("step", "candidate", "candidateStep"):
                value = coordinates.get(key)
                if isinstance(value, dict):
                    envelope[key] = dict(value)
        return (
            envelope
            if await self._persist_and_enqueue(envelope, require_durable_metadata=require_durable_metadata)
            else None
        )

    def _next_snapshot(self, envelope: dict[str, Any]) -> dict[str, Any]:
        existing_snapshot = self.snapshot_store.load()
        if existing_snapshot is not None:
            try:
                events = self.journal.read_all_repairing_tail()
            except Exception:
                logger.warning("Failed to read A2A pipeline journal snapshot catch-up events", exc_info=True)
                raise _SnapshotCatchUpUnavailableError from None

            scoped_events = _events_for_envelope_identity(events, envelope)
            if _snapshot_matches_envelope_identity(existing_snapshot, envelope) and _snapshot_schema_is_current(
                existing_snapshot
            ):
                snapshot_sequence = _int_value(existing_snapshot.get("lastSequence"), 0)
                catch_up_events = [
                    event for event in scoped_events if _int_value(event.get("sequence"), 0) > snapshot_sequence
                ]
                snapshot_base = existing_snapshot
            else:
                catch_up_events = scoped_events
                snapshot_base = None
            return reduce_pipeline_events([*catch_up_events, envelope], existing_snapshot=snapshot_base)

        try:
            journal_events = self.journal.read_all_repairing_tail()
        except Exception:
            logger.warning("Failed to read A2A pipeline journal snapshot events", exc_info=True)
            raise _SnapshotCatchUpUnavailableError from None
        events = _events_for_envelope_identity(journal_events, envelope)
        return reduce_pipeline_events([*events, envelope])

    async def _persist_and_enqueue(
        self,
        envelope: dict[str, Any],
        *,
        artifact_metadata: dict[str, Any] | None = None,
        require_durable_metadata: bool = False,
    ) -> bool:
        async with self._sequence_lock:
            self._annotate_delivery_alias(envelope)
            try:
                self._ensure_monotonic_sequence(envelope)
            except _SequenceHighWaterUnavailableError:
                logger.warning("Skipping A2A pipeline event until journal high-water sequence is readable")
                return False
            safe_envelope = to_json_safe(envelope)
            if not isinstance(safe_envelope, dict):
                logger.warning("Skipping invalid A2A pipeline envelope: %r", envelope)
                return False
            durable_required = require_durable_metadata or is_recovery_semantic_event(safe_envelope)
            journal_persisted = False
            snapshot_persisted = False
            try:
                self.journal.append(safe_envelope, durable=durable_required)
                journal_persisted = True
            except Exception:
                logger.warning("Failed to append A2A pipeline journal event", exc_info=True)
            try:
                snapshot = self._next_snapshot(safe_envelope)
                snapshot_persisted = self.snapshot_store.save(snapshot)
            except _SnapshotCatchUpUnavailableError:
                logger.warning("Skipping A2A pipeline snapshot save until journal catch-up succeeds")
            except Exception:
                logger.warning("Failed to persist A2A pipeline snapshot", exc_info=True)
            if snapshot_persisted:
                _maybe_inject_test_fault("after_a2a_pipeline_snapshot_saved")
            if durable_required and not (journal_persisted or snapshot_persisted):
                logger.warning("Skipping A2A pipeline status update because durable metadata was not persisted")
                return False
            if artifact_metadata is not None and not (journal_persisted or snapshot_persisted):
                logger.warning("Skipping A2A artifact update because pipeline metadata was not persisted")
                return False
            if artifact_metadata is not None:
                await self._enqueue_artifact_update(safe_envelope, artifact_metadata)
            await self._enqueue_status(safe_envelope)
            self.last_envelope = safe_envelope
            return True

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
        except Exception:
            logger.warning("Failed to read A2A pipeline journal high-water sequence", exc_info=True)
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
                result = {"artifact": envelope.get("artifact")}
            elif tool_result is not None:
                result = tool_result.result
            else:
                return None
            artifact_metadata = _extract_artifact_metadata(result, self.artifact_store)
        except Exception:
            logger.warning("Failed to externalize A2A pipeline tool artifact", exc_info=True)
            return None
        if artifact_metadata is None:
            return None

        envelope["eventType"] = "artifact_created"
        envelope["scope"] = envelope.get("scope") or "pipeline"
        envelope["status"] = "working"
        envelope["artifact"] = artifact_metadata
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
        await self.event_queue.enqueue_event(
            _artifact_update_event(
                task_id=self._delivery_task_id(envelope),
                context_id=self._delivery_context_id(envelope),
                metadata=artifact_metadata,
            )
        )

    async def _apply_permission_metadata(
        self,
        request: PermissionRequestEvent,
        envelope: dict[str, Any],
        *,
        permission_resolver: PipelinePermissionResolver | None,
        auto_approve_permissions: bool,
    ) -> bool:
        approved = bool(auto_approve_permissions)
        if permission_resolver is not None:
            result = permission_resolver(request)
            approved = bool(await result) if inspect.isawaitable(result) else bool(result)
        elif is_aliyun_api_non_read_only_permission_event(request):
            approved = False

        include_tool_input = A2AExposureType.TOOL_TRACE in self.exposure_types
        permission = envelope.setdefault("permission", {})
        permission.clear()
        permission.update(safe_permission_metadata(request, include_tool_input=include_tool_input))
        permission.update(_permission_approval_metadata(approved))
        return approved

    async def _enqueue_status(self, envelope: dict[str, Any]) -> None:
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
        await self.event_queue.enqueue_event(update)

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


def _unwrap_stream_event(event: Any) -> Any:
    while isinstance(event, SubPipelineStreamEvent):
        event = event.inner
    return event


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


def is_recovery_semantic_event(envelope: dict[str, Any]) -> bool:
    event_type = envelope.get("eventType")
    event_type = event_type if isinstance(event_type, str) else None
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


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["PipelineA2AEventPublisher", "PipelinePermissionResolver", "is_recovery_semantic_event"]
