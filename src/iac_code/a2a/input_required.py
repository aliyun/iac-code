"""Process-local A2A input-required coordination for permission prompts."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from a2a.types import Message, Part, Role
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.runtime_overrides import PermissionReplyExecutionContext, get_a2a_preferred_language
from iac_code.i18n import translate_message
from iac_code.services.permission_wait import (
    PermissionExecutionIdentity,
    PermissionWaitCheckpointStore,
    PermissionWaitCoordinator,
    PermissionWaitPolicy,
    build_permission_checkpoint,
    canonicalize_permission_continuation_frame,
)
from iac_code.services.permissions.audit import (
    build_prompt_tool_input,
    emit_permission_boundary_audit,
    sanitize_prompt_text,
)
from iac_code.types.stream_events import PermissionRequestEvent

PERMISSION_SCHEMA_VERSION = 1
PERMISSION_DECISIONS = frozenset({"allow_once", "deny"})
PERMISSION_QUERY_PREFIX = "IAC_CODE_PERMISSION:"
_SAFE_SUMMARY_MAX_CHARS = 1200
_DISPLAY_FIELD_MAX_CHARS = 500


@dataclass(frozen=True)
class PermissionResponse:
    task_id: str
    context_id: str
    request_task_id: str
    input_id: str
    tool_use_id: str
    decision: str


@dataclass
class PendingPermission:
    task_id: str
    context_id: str
    input_id: str
    request: PermissionRequestEvent
    language: str = field(default_factory=lambda: get_a2a_preferred_language() or "en")
    resolution_owner: PermissionResolutionOwner | None = None
    scope: str = "pipeline"
    coordinates: dict[str, Any] | None = None
    state: str = "pending"
    boundary_id: str | None = None
    checkpoint_store: PermissionWaitCheckpointStore | None = field(default=None, repr=False)
    claim_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    continuation: Any | None = field(default=None, repr=False)
    continuation_claimed: bool = field(default=False, repr=False)
    suspend_callback: Any | None = field(default=None, repr=False)
    backup_cwd: str | None = field(default=None, repr=False)
    backup_session_id: str | None = field(default=None, repr=False)
    backup_service: Any | None = field(default=None, repr=False)
    backup_metrics: Any | None = field(default=None, repr=False)

    def envelope(self) -> dict[str, Any]:
        return permission_input_envelope(
            self.request,
            task_id=self.task_id,
            context_id=self.context_id,
            input_id=self.input_id,
            language=self.language,
        )


class PermissionResolutionOwner(Protocol):
    async def resolve_permission(self, pending: PendingPermission, response: PermissionResponse) -> bool: ...

    async def fail_permission(self, pending: PendingPermission) -> None: ...

    async def cancel_permissions(self, task_id: str) -> None: ...


@dataclass(frozen=True)
class PermissionTaskClosingToken:
    task_id: str
    token_id: str


@dataclass
class _PermissionTaskClosingState:
    permanent: bool = False
    reversible_tokens: set[str] = field(default_factory=set)


def permission_input_envelope(
    request: PermissionRequestEvent,
    *,
    task_id: str,
    context_id: str,
    input_id: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    language = language or get_a2a_preferred_language() or "en"
    display = permission_display_fields(request, language=language)
    envelope: dict[str, Any] = {
        "schemaVersion": PERMISSION_SCHEMA_VERSION,
        "kind": "permission",
        "requestTaskId": task_id,
        "contextId": context_id,
        "inputId": input_id or _permission_input_id(),
        "toolUseId": request.tool_use_id,
        "toolName": request.tool_name,
        "title": display["title"],
        "purpose": display["purpose"],
        "effect": display["effect"],
        "target": display["target"],
        "isReadOnly": display["isReadOnly"],
        "prompt": permission_prompt(display["title"], language=language),
        "safeSummary": permission_safe_summary(request),
        "options": permission_options(language=language),
        "language": language,
        "required": True,
    }
    if deployment_summary := display.get("deploymentSummary"):
        envelope["deploymentSummary"] = deployment_summary
    return envelope


def permission_prompt(title: str, *, language: str | None = None) -> str:
    return translate_message("{title} Allow once?", language=language or "en").format(title=title)


def permission_options(*, language: str | None = None) -> list[dict[str, str]]:
    resolved = language or "en"
    return [
        {"id": "allow_once", "label": translate_message("Allow once", language=resolved)},
        {"id": "deny", "label": translate_message("Deny", language=resolved)},
    ]


def permission_display_fields(request: PermissionRequestEvent, *, language: str | None = None) -> dict[str, Any]:
    """Project deterministic permission semantics for an external decision maker."""

    permission_result = request.permission_result
    audit = getattr(permission_result, "audit", None)
    operation = getattr(audit, "operation", None)
    operation = operation if isinstance(operation, dict) else {}
    read_only_value = getattr(audit, "is_read_only", None)
    is_read_only = read_only_value is True
    read_only_known = isinstance(read_only_value, bool)
    tool_name = request.tool_name
    public_tool = _safe_scalar(operation.get("public_tool")) or tool_name
    product = _safe_scalar(operation.get("product"))
    action = _safe_scalar(operation.get("action"))
    region = _safe_scalar(operation.get("region"))
    stack_name = _safe_scalar(operation.get("stackName"))
    stack_id = _safe_scalar(operation.get("stackId"))
    safe_input = build_prompt_tool_input(request.tool_input)
    language = language or get_a2a_preferred_language() or "en"
    deployment_summary = _safe_deployment_summary(operation.get("deploymentSummary"))

    if product or action:
        operation_name = " ".join(value for value in (product, action) if value)
        title = _cloud_operation_title(product, action, is_read_only=is_read_only, language=language)
        purpose = translate_message(
            "Call {operation} for the requested Alibaba Cloud infrastructure task.", language=language
        ).format(operation=operation_name)
        target = operation_name
        if region:
            target += translate_message(" in {region}", language=language).format(region=region)
        if stack_name:
            target += translate_message("; stack {stack}", language=language).format(stack=stack_name)
        elif stack_id:
            target += translate_message("; stack {stack}", language=language).format(stack=stack_id)
        effect = "read" if is_read_only else ("cloud_change" if read_only_known else "unknown")
    elif tool_name == "bash":
        if is_read_only:
            title = translate_message("Read local workspace data", language=language)
        else:
            title = translate_message("Run a local shell command", language=language)
        if is_read_only:
            purpose = translate_message(
                "Read local data needed for the requested infrastructure task.", language=language
            )
        else:
            purpose = translate_message(
                "Execute a local command needed for the requested infrastructure task.", language=language
            )
        command = None
        if isinstance(safe_input, dict):
            command = safe_input.get("command") or safe_input.get("cmd")
        if isinstance(command, str) and command.strip():
            command_fallback = translate_message("shell command", language=language)
            target = translate_message("the current local workspace; command: {command}", language=language).format(
                command=_display_text(command, fallback=command_fallback, maximum=240)
            )
        else:
            target = translate_message("the current local workspace", language=language)
        effect = "read" if is_read_only else ("local_execution" if read_only_known else "unknown")
    elif tool_name in {"write_file", "edit_file"}:
        title = translate_message("Change a workspace file", language=language)
        purpose = translate_message("Write a file needed for the requested infrastructure task.", language=language)
        target = _safe_input_target(safe_input, language=language) or translate_message(
            "a file in the current workspace", language=language
        )
        effect = "file_change"
    elif tool_name in {"read_file", "glob", "grep"} or is_read_only:
        title = translate_message("Read workspace data with {tool}", language=language).format(tool=public_tool)
        purpose = translate_message("Read local data needed for the requested infrastructure task.", language=language)
        target = _safe_input_target(safe_input, language=language) or translate_message(
            "the current local workspace", language=language
        )
        effect = "read"
    else:
        title = translate_message("Run {tool}", language=language).format(tool=public_tool)
        purpose = translate_message("Run this operation for the requested infrastructure task.", language=language)
        target = _safe_input_target(safe_input, language=language) or translate_message(
            "the current task workspace or cloud account", language=language
        )
        effect = "local_or_remote_change" if read_only_known else "unknown"

    display: dict[str, Any] = {
        "title": _display_text(title, fallback=translate_message("Permission required", language=language)),
        "purpose": _display_text(
            purpose,
            fallback=translate_message("Complete the requested infrastructure task.", language=language),
        ),
        "effect": _display_text(effect, fallback="unknown", maximum=80),
        "target": _display_text(target, fallback=translate_message("the current task scope", language=language)),
        "isReadOnly": is_read_only,
    }
    if deployment_summary:
        display["deploymentSummary"] = deployment_summary
    return display


def permission_safe_summary(request: PermissionRequestEvent) -> str:
    audit = getattr(request.permission_result, "audit", None)
    operation = getattr(audit, "operation", None)
    operation = operation if isinstance(operation, dict) else {}
    if deployment_summary := _safe_deployment_summary(operation.get("deploymentSummary")):
        return _deployment_summary_text(
            deployment_summary,
            language=get_a2a_preferred_language() or "en",
        )
    projected = build_prompt_tool_input(request.tool_input)
    rendered = json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    summary = "{}: {}".format(request.tool_name, rendered)
    return (
        sanitize_prompt_text(summary, max_chars=_SAFE_SUMMARY_MAX_CHARS) or request.tool_name[:_SAFE_SUMMARY_MAX_CHARS]
    )


def _safe_deployment_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary: dict[str, Any] = {}
    for key in ("candidateName", "action", "region", "stackName", "template", "totalMonthlyCost"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            summary[key] = _display_text(raw, fallback="", maximum=300)
    resources: list[dict[str, str]] = []
    raw_resources = value.get("resources")
    if isinstance(raw_resources, list):
        for raw_resource in raw_resources[:12]:
            if not isinstance(raw_resource, dict):
                continue
            item: dict[str, str] = {}
            for source, target in (("name", "name"), ("spec", "spec"), ("monthlyCost", "monthlyCost")):
                raw = raw_resource.get(source)
                if isinstance(raw, str) and raw.strip():
                    item[target] = _display_text(raw, fallback="", maximum=200)
            if item:
                resources.append(item)
    if resources:
        summary["resources"] = resources
    return summary or None


def _deployment_summary_text(summary: dict[str, Any], *, language: str) -> str:
    candidate = _safe_scalar(summary.get("candidateName"))
    region = _safe_scalar(summary.get("region"))
    stack_name = _safe_scalar(summary.get("stackName"))
    template = _safe_scalar(summary.get("template"))
    total = _safe_scalar(summary.get("totalMonthlyCost"))
    resources = summary.get("resources")
    resource_parts: list[str] = []
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            name = _safe_scalar(resource.get("name"))
            spec = _safe_scalar(resource.get("spec"))
            cost = _safe_scalar(resource.get("monthlyCost"))
            label = " / ".join(value for value in (name, spec, cost) if value)
            if label:
                resource_parts.append(label)
    parts = [translate_message("Deploy a ROS stack", language=language)]
    if candidate:
        parts.append(translate_message("plan: {value}", language=language).format(value=candidate))
    if region:
        parts.append(translate_message("region: {value}", language=language).format(value=region))
    if stack_name:
        parts.append(translate_message("stack: {value}", language=language).format(value=stack_name))
    if template:
        parts.append(translate_message("template: {value}", language=language).format(value=template))
    if total:
        parts.append(translate_message("estimated monthly cost: {value}", language=language).format(value=total))
    if resource_parts:
        resource_separator = "；" if language == "zh" else "; "
        parts.append(
            translate_message("resource costs: {value}", language=language).format(
                value=resource_separator.join(resource_parts)
            )
        )
    rendered = "；".join(parts) if language == "zh" else "; ".join(parts)
    return sanitize_prompt_text(rendered, max_chars=_SAFE_SUMMARY_MAX_CHARS) or "ros_deploy"


def _safe_scalar(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else ""


def _cloud_operation_title(product: str, action: str, *, is_read_only: bool, language: str = "en") -> str:
    product_label = product.upper() if product.lower() == "ros" else product
    if action == "CreateStack":
        return translate_message("Create {product} stack", language=language).format(product=product_label)
    if action == "ContinueCreateStack":
        return translate_message("Continue creating {product} stack", language=language).format(product=product_label)
    if action == "UpdateStack":
        return translate_message("Update {product} stack", language=language).format(product=product_label)
    if action == "DeleteStack":
        return translate_message("Delete {product} stack", language=language).format(product=product_label)
    operation_name = " ".join(value for value in (product, action) if value)
    if is_read_only:
        return translate_message("Read Alibaba Cloud data with {operation}", language=language).format(
            operation=operation_name
        )
    return translate_message("Run {operation}", language=language).format(operation=operation_name)


def _display_text(value: str, *, fallback: str, maximum: int = _DISPLAY_FIELD_MAX_CHARS) -> str:
    return sanitize_prompt_text(value, max_chars=maximum) or fallback


def _safe_input_target(value: Any, *, language: str) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("file_path", "filePath", "path", "region_id", "regionId", "resource_id", "resourceId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return _display_text(candidate, fallback=translate_message("the current task scope", language=language))
    return ""


def parse_permission_response(message: Message | None) -> PermissionResponse | None:
    if not isinstance(message, Message):
        return None
    data_permission_parts = [
        value
        for part in message.parts
        if (value := _json_data_part(part)) is not None and value.get("kind") == "permission"
    ]
    text_permission_parts = [
        value
        for part in message.parts
        if (value := _json_text_part(part)) is not None and value.get("kind") == "permission"
    ]
    if not data_permission_parts and not text_permission_parts:
        return None
    if message.role != Role.ROLE_USER:
        raise InvalidParamsError("Permission responses must use ROLE_USER.")

    text_transport = bool(text_permission_parts)
    if data_permission_parts and text_permission_parts:
        raise InvalidParamsError("Permission responses must use exactly one supported JSON transport.")
    if data_permission_parts and (len(message.parts) != 1 or len(data_permission_parts) != 1):
        raise InvalidParamsError("Permission responses must contain exactly one application/json DataPart.")
    if text_permission_parts and (len(message.parts) != 1 or len(text_permission_parts) != 1):
        raise InvalidParamsError("Permission responses must contain exactly one JSON TextPart.")

    payload = text_permission_parts[0] if text_transport else data_permission_parts[0]
    expected_keys = {"schemaVersion", "kind", "requestTaskId", "inputId", "toolUseId", "decision"}
    if text_transport:
        # Some gateways, including ROS StartChat, can only forward user input as
        # an A2A TextPart. A fixed prefix keeps ordinary chat on the fast path;
        # the exact JSON object after that prefix carries the full correlation.
        expected_keys.add("contextId")
    if set(payload) != expected_keys:
        raise InvalidParamsError("Permission response payload fields do not match schemaVersion 1.")
    if payload.get("schemaVersion") != PERMISSION_SCHEMA_VERSION:
        raise InvalidParamsError("Permission response schemaVersion must be 1.")
    decision = payload.get("decision")
    if decision not in PERMISSION_DECISIONS:
        raise InvalidParamsError("Permission decision must be allow_once or deny.")
    outer_task_id = message.task_id
    outer_context_id = message.context_id
    request_task_id = payload.get("requestTaskId")
    input_id = payload.get("inputId")
    tool_use_id = payload.get("toolUseId")
    if text_transport and not outer_task_id and isinstance(request_task_id, str):
        # Text-only gateways may preserve the A2A context while omitting taskId
        # on follow-up messages. The registry still validates every opaque
        # correlation field against an active pending permission.
        outer_task_id = request_task_id
    values = (outer_task_id, outer_context_id, request_task_id, input_id, tool_use_id)
    if not all(isinstance(value, str) and value for value in values):
        raise InvalidParamsError("Permission response correlation fields are required.")
    assert isinstance(outer_task_id, str)
    assert isinstance(outer_context_id, str)
    assert isinstance(request_task_id, str)
    assert isinstance(input_id, str)
    assert isinstance(tool_use_id, str)
    if outer_task_id != request_task_id:
        raise InvalidParamsError("Permission response taskId does not match requestTaskId.")
    if text_transport and payload.get("contextId") != outer_context_id:
        raise InvalidParamsError("Permission response contextId does not match message contextId.")
    return PermissionResponse(
        task_id=outer_task_id,
        context_id=outer_context_id,
        request_task_id=request_task_id,
        input_id=input_id,
        tool_use_id=tool_use_id,
        decision=decision,
    )


def permission_ack_message(response: PermissionResponse, *, approved: bool) -> Message:
    decision = "allow_once" if approved else "deny"
    part = Part(media_type="application/json")
    part.data.struct_value.update(
        {
            "schemaVersion": PERMISSION_SCHEMA_VERSION,
            "kind": "permission_ack",
            "inputId": response.input_id,
            "toolUseId": response.tool_use_id,
            "decision": decision,
            "accepted": True,
        }
    )
    return Message(
        message_id="permission-ack-{}".format(uuid.uuid4().hex),
        task_id=response.task_id,
        context_id=response.context_id,
        role=Role.ROLE_AGENT,
        parts=[part],
    )


async def backup_permission_wait_checkpoint(
    *,
    store: PermissionWaitCheckpointStore,
    boundary_id: str,
    cwd: str,
    session_id: str,
    backup_service: Any,
    metrics: Any | None = None,
) -> Any | None:
    """Commit one permission checkpoint under the permission/backup lock order."""

    from iac_code.a2a.backup import backup_session_async
    from iac_code.services.session_backup import BackupReason, SessionBackupBlocked

    record = store.load(boundary_id)
    if record is None:
        raise RuntimeError("permission checkpoint is unavailable for critical backup")
    generation = int(record["generation"])

    def fenced_backup(_cwd: str, _session_id: str, **kwargs: Any) -> Any:
        return store.run_generation_fenced(
            boundary_id,
            expected_generation=generation,
            operation=lambda: backup_service.backup_session(_cwd, _session_id, **kwargs),
        )

    try:
        result = await backup_session_async(
            backup_service,
            cwd,
            session_id,
            reason=BackupReason.INPUT_REQUIRED,
            critical=True,
            metrics=metrics,
            backup_call=fenced_backup,
        )
    except ValueError as exc:
        raise SessionBackupBlocked("Permission checkpoint changed during critical backup.") from exc
    if getattr(result, "enabled", False) and not getattr(result, "shared_committed", False):
        raise SessionBackupBlocked("Critical permission backup did not reach the shared target.")
    return result


class PermissionInputRegistry:
    """Coordinate legacy input-required and concurrent Sub Pipeline permissions."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._pending: dict[tuple[str, str], PendingPermission] = {}
        self._closing_tasks: dict[str, _PermissionTaskClosingState] = {}
        self._permission_wait_coordinator: PermissionWaitCoordinator | None = None

    def set_permission_wait_coordinator(self, coordinator: PermissionWaitCoordinator | None) -> None:
        self._permission_wait_coordinator = coordinator

    @property
    def durable_permission_wait_enabled(self) -> bool:
        return self._permission_wait_coordinator is not None

    @property
    def permission_wait_policy(self) -> PermissionWaitPolicy:
        coordinator = self._permission_wait_coordinator
        return coordinator.policy if coordinator is not None else PermissionWaitPolicy()

    async def open_durable_boundary(
        self,
        pending: PendingPermission,
        *,
        cwd: str,
        session_id: str,
        permission_class: str,
        backup_service: Any,
        metrics: Any | None = None,
        pipeline_coordinates: dict[str, Any] | None = None,
        perform_backup: bool = True,
    ) -> dict[str, Any]:
        """Persist and critically back up a real external wait before publication."""

        coordinator = self._permission_wait_coordinator
        if coordinator is None:
            raise RuntimeError("permission wait coordinator is unavailable")
        source_frame = pending.request.continuation_frame
        if not isinstance(source_frame, dict):
            raise RuntimeError("permission_resume_invalid: continuation frame is unavailable")
        store = PermissionWaitCheckpointStore(cwd, session_id)
        audit_context = pending.request.audit_context if isinstance(pending.request.audit_context, dict) else {}
        try:
            frame = canonicalize_permission_continuation_frame(source_frame, audit_context=audit_context)
        except ValueError as exc:
            raise RuntimeError(f"permission_resume_invalid: {exc}") from exc
        principal_ref = audit_context.get("principal_ref")
        principal_kind = audit_context.get("principal_kind")
        region = audit_context.get("region")
        record = build_permission_checkpoint(
            session_id=session_id,
            task_id=pending.task_id,
            context_id=pending.context_id,
            input_id=pending.input_id,
            tool_use_id=pending.request.tool_use_id,
            tool_name=pending.request.tool_name,
            tool_input=pending.request.tool_input,
            permission_class="pipeline" if permission_class == "pipeline" else "normal",
            continuation_frame=frame,
            policy=coordinator.policy,
            principal_ref=principal_ref if isinstance(principal_ref, str) else None,
            principal_kind=principal_kind if principal_kind in {"a2a_user", "credential"} else None,
            region=region if isinstance(region, str) else None,
            pipeline_coordinates=pipeline_coordinates,
        )
        previous_boundary_id = frame.get("previousBoundaryId")
        if isinstance(previous_boundary_id, str) and previous_boundary_id:
            store.create_successor(record, previous_boundary_id=previous_boundary_id)
            await self._release_replaced_durable_boundary(previous_boundary_id)
        else:
            store.create(record)
        pending.boundary_id = record["boundaryId"]
        pending.checkpoint_store = store
        pending.request.boundary_id = pending.boundary_id
        pending.backup_cwd = cwd
        pending.backup_session_id = session_id
        pending.backup_service = backup_service
        pending.backup_metrics = metrics

        if perform_backup:
            await self.backup_durable_boundary(
                pending,
                cwd,
                session_id,
                backup_service=backup_service,
                metrics=metrics,
            )
            current = store.load(pending.boundary_id)
            if current is None:
                raise RuntimeError("permission checkpoint is unavailable after critical backup")
            self.activate_durable_boundary(pending, current)
        return record

    async def _release_replaced_durable_boundary(self, boundary_id: str) -> None:
        """Mirror a successor checkpoint swap in registry/coordinator state."""

        replaced: list[PendingPermission] = []
        async with self._condition:
            for key, pending in list(self._pending.items()):
                if pending.boundary_id != boundary_id:
                    continue
                self._pending.pop(key, None)
                pending.state = "completed"
                replaced.append(pending)
            if replaced:
                self._condition.notify_all()
        for pending in replaced:
            if pending.timeout_task is not None and pending.timeout_task is not asyncio.current_task():
                pending.timeout_task.cancel()
        if self._permission_wait_coordinator is not None:
            self._permission_wait_coordinator.unregister_live(boundary_id)

    async def backup_durable_boundary(
        self,
        pending: PendingPermission,
        cwd: str,
        session_id: str,
        *,
        backup_service: Any,
        metrics: Any | None = None,
    ) -> Any | None:
        """Commit the critical shared copy under the documented lock order."""

        store = pending.checkpoint_store
        boundary_id = pending.boundary_id
        if store is None or boundary_id is None:
            raise RuntimeError("permission checkpoint is unavailable for critical backup")
        pending.backup_cwd = cwd
        pending.backup_session_id = session_id
        pending.backup_service = backup_service
        pending.backup_metrics = metrics
        return await backup_permission_wait_checkpoint(
            store=store,
            boundary_id=boundary_id,
            cwd=cwd,
            session_id=session_id,
            backup_service=backup_service,
            metrics=metrics,
        )

    def activate_durable_boundary(
        self,
        pending: PendingPermission,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        coordinator = self._permission_wait_coordinator
        store = pending.checkpoint_store
        if coordinator is None or store is None or pending.boundary_id is None:
            raise RuntimeError("permission checkpoint is unavailable")
        current = record or store.load(pending.boundary_id)
        if current is None:
            raise RuntimeError("permission checkpoint is unavailable")
        future = pending.request.response_future
        if future is None or future.done():
            raise RuntimeError("permission wait point is unavailable after critical backup")

        async def on_suspend() -> None:
            callback = pending.suspend_callback
            if callback is not None:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result

        coordinator.register_live(record=current, store=store, future=future, on_suspend=on_suspend)
        return current

    async def pending_for_response(self, response: PermissionResponse) -> PendingPermission:
        return await self._lookup(response)

    async def register(
        self,
        request: PermissionRequestEvent,
        *,
        task_id: str,
        context_id: str,
        resolution_owner: PermissionResolutionOwner | None = None,
        scope: str = "pipeline",
        coordinates: dict[str, Any] | None = None,
    ) -> PendingPermission:
        if request.response_future is None or request.response_future.done():
            raise InvalidParamsError("Permission wait point is no longer active.")
        async with self._condition:
            if task_id in self._closing_tasks:
                emit_permission_boundary_audit(
                    request,
                    decision="deny",
                    scope="a2a_sub_pipeline_permission" if resolution_owner is not None else "a2a_input_required",
                    source="a2a_task_cancel",
                    reason_type="task_canceled",
                    reason_detail="task cancellation already started before permission registration",
                )
                request.response_future.set_result(False)
                raise InvalidParamsError("permission_resume_invalid: task cancellation is already in progress.")
            while resolution_owner is None and any(
                pending.task_id == task_id
                and pending.resolution_owner is None
                and pending.request.response_future is not None
                and not pending.request.response_future.done()
                for pending in self._pending.values()
            ):
                await self._condition.wait()
            input_id = _permission_input_id()
            while (task_id, input_id) in self._pending:
                input_id = _permission_input_id()
            pending = PendingPermission(
                task_id=task_id,
                context_id=context_id,
                input_id=input_id,
                request=request,
                resolution_owner=resolution_owner,
                scope=scope,
                coordinates=dict(coordinates) if coordinates is not None else None,
            )
            self._pending[(task_id, input_id)] = pending
            if resolution_owner is not None:
                request.resolution_owner_managed = True
            return pending

    async def answer(
        self,
        response: PermissionResponse,
        *,
        execution_context: PermissionReplyExecutionContext | None = None,
    ) -> bool:
        pending = await self._lookup(response)
        if pending.resolution_owner is not None:
            pending.request.permission_execution_context = execution_context
            return await pending.resolution_owner.resolve_permission(pending, response)

        coordinator = self._permission_wait_coordinator
        if coordinator is not None and pending.boundary_id is not None:
            self._validate_live_execution_identity(pending)
            pending.request.permission_execution_context = execution_context

            def audit_new_claim(value: str) -> bool:
                return emit_permission_boundary_audit(
                    pending.request,
                    decision="allow" if value == "allow_once" else "deny",
                    scope="a2a_input_required",
                    source="a2a_user_permission",
                    reason_type="user_decision",
                    reason_detail=value,
                )

            try:
                record, _created = await coordinator.claim_live(
                    boundary_id=pending.boundary_id,
                    value="allow_once" if response.decision == "allow_once" else "deny",
                    source="user",
                    on_new_claim=audit_new_claim,
                    before_delivery=lambda _record: self._backup_claim_before_delivery(pending),
                )
            except (LookupError, ValueError) as exc:
                raise InvalidParamsError(f"permission_resume_invalid: {exc}") from exc
            decision = record.get("decision")
            approved = isinstance(decision, dict) and decision.get("value") == "allow_once"
            if record.get("phase") in {"SUSPENDING", "SUSPENDED", "RESTORING"}:
                pending.state = "suspended_decision_claimed"
            return approved

        async with self._condition:
            self._validate_response(pending, response)
            pending.request.permission_execution_context = execution_context
            future = pending.request.response_future
            if future is None or future.done():
                raise InvalidParamsError("permission_resume_invalid: permission wait point is unavailable.")
            approved = response.decision == "allow_once"
            audit_ok = emit_permission_boundary_audit(
                pending.request,
                decision="allow" if approved else "deny",
                scope="a2a_input_required",
                source="a2a_user_permission",
                reason_type="user_decision",
                reason_detail=response.decision,
            )
            if approved and not audit_ok:
                approved = False
            future.set_result(approved)
            return approved

    @staticmethod
    def _validate_live_execution_identity(pending: PendingPermission) -> None:
        store = pending.checkpoint_store
        boundary_id = pending.boundary_id
        if store is None or boundary_id is None:
            return
        record = store.load(boundary_id)
        if record is None:
            raise InvalidParamsError("permission_resume_invalid: permission checkpoint is unavailable.")
        permission_audit = getattr(pending.request.permission_result, "audit", None)
        principal_kind = record.get("principalKind")
        if principal_kind is None and record.get("principalRef") is not None:
            principal_kind = "credential"
        identity = PermissionExecutionIdentity.resolve(
            tool_name=pending.request.tool_name,
            tool_input=pending.request.tool_input,
            permission_audit=permission_audit,
            principal_kind=principal_kind,
        )
        if identity.principal_ref != record.get("principalRef") or identity.region != record.get("region"):
            raise InvalidParamsError("permission_resume_invalid: cloud execution identity changed.")

    async def _backup_claim_before_delivery(self, pending: PendingPermission) -> None:
        store = pending.checkpoint_store
        boundary_id = pending.boundary_id
        cwd = pending.backup_cwd
        session_id = pending.backup_session_id
        backup_service = pending.backup_service
        if store is None or boundary_id is None or cwd is None or session_id is None or backup_service is None:
            return
        await backup_permission_wait_checkpoint(
            store=store,
            boundary_id=boundary_id,
            cwd=cwd,
            session_id=session_id,
            backup_service=backup_service,
            metrics=pending.backup_metrics,
        )

    async def is_sideband_response(self, response: PermissionResponse) -> bool:
        try:
            pending = await self._lookup(response)
        except InvalidParamsError:
            return False
        return pending.resolution_owner is not None

    async def claim(self, pending: PendingPermission, response: PermissionResponse) -> None:
        async with self._condition:
            if pending.task_id in self._closing_tasks:
                raise InvalidParamsError("permission_resume_invalid: task cancellation is already in progress.")
            current = self._pending.get((pending.task_id, pending.input_id))
            if current is not pending or pending.state != "pending":
                raise InvalidParamsError("permission_resume_invalid: pending permission is not active.")
            self._validate_response(pending, response)
            future = pending.request.response_future
            if future is None or future.done():
                raise InvalidParamsError("permission_resume_invalid: permission wait point is unavailable.")
            pending.state = "resolving"

    async def claim_for_cancel(self, task_id: str, owner: PermissionResolutionOwner) -> list[PendingPermission]:
        async with self._condition:
            claimed = [
                pending
                for pending in self._pending.values()
                if pending.task_id == task_id and pending.resolution_owner is owner and pending.state == "pending"
            ]
            for pending in claimed:
                pending.state = "canceling"
            return claimed

    async def pending_envelopes(
        self,
        task_id: str,
        *,
        excluding: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        excluded = excluding or set()
        async with self._condition:
            return [
                pending.envelope()
                for pending in self._pending.values()
                if pending.task_id == task_id
                and pending.resolution_owner is not None
                and pending.input_id not in excluded
            ]

    async def cancel_task(
        self,
        task_id: str,
        *,
        reversible: bool = False,
    ) -> PermissionTaskClosingToken | None:
        async with self._condition:
            # Closing starts atomically before pending owners are inspected, so
            # registration and unclaimed replies cannot race ahead of draining.
            closing = self._closing_tasks.setdefault(task_id, _PermissionTaskClosingState())
            token = None
            if reversible and not closing.permanent:
                token = PermissionTaskClosingToken(task_id=task_id, token_id=uuid.uuid4().hex)
                closing.reversible_tokens.add(token.token_id)
            else:
                closing.permanent = True
                closing.reversible_tokens.clear()
            owners: list[PermissionResolutionOwner] = []
            legacy: list[PendingPermission] = []
            for pending in self._pending.values():
                if pending.task_id != task_id:
                    continue
                owner = pending.resolution_owner
                if owner is None:
                    legacy.append(pending)
                elif all(existing is not owner for existing in owners):
                    owners.append(owner)
        for owner in owners:
            await owner.cancel_permissions(task_id)
        for pending in legacy:
            coordinator = self._permission_wait_coordinator
            canceled = bool(
                coordinator is not None
                and pending.boundary_id is not None
                and await coordinator.cancel_live(pending.boundary_id)
            )
            if not canceled:
                await self.fail(pending)
            await self.complete(pending)
        return token

    async def reopen_task(self, token: PermissionTaskClosingToken | None) -> None:
        """Undo only the reversible close represented by ``token``."""

        if token is None:
            return
        async with self._condition:
            closing = self._closing_tasks.get(token.task_id)
            if closing is None or closing.permanent:
                return
            closing.reversible_tokens.discard(token.token_id)
            if not closing.reversible_tokens:
                self._closing_tasks.pop(token.task_id, None)
                self._condition.notify_all()

    async def fail(self, pending: PendingPermission) -> None:
        if pending.resolution_owner is not None:
            await pending.resolution_owner.fail_permission(pending)
            return
        # This cleanup is serialized by the same per-boundary resolution lock
        # used for answers and cancellation.  It prevents a failed critical
        # backup/publication from leaving a recoverable checkpoint for an
        # INPUT_REQUIRED boundary that was never externally visible.
        coordinator = self._permission_wait_coordinator
        if pending.boundary_id is not None and pending.checkpoint_store is not None:
            canceled = bool(coordinator is not None and await coordinator.cancel_live(pending.boundary_id))
            if not canceled:
                try:
                    pending.checkpoint_store.cancel(pending.boundary_id)
                except ValueError:
                    # A concurrently claimed decision remains authoritative;
                    # recovery will finish applying that decision.
                    pass
        future = pending.request.response_future
        if future is not None and not future.done():
            emit_permission_boundary_audit(
                pending.request,
                decision="deny",
                scope="a2a_input_required",
                source="a2a_permission_resume_invalid",
                reason_type="permission_resume_invalid",
                reason_detail="permission input could not be published or resumed",
            )
            future.set_result(False)

    async def complete(self, pending: PendingPermission) -> None:
        async with self._condition:
            key = (pending.task_id, pending.input_id)
            if self._pending.get(key) is pending:
                self._pending.pop(key, None)
                pending.state = "completed"
                self._condition.notify_all()
        if pending.timeout_task is not None and pending.timeout_task is not asyncio.current_task():
            pending.timeout_task.cancel()
        if pending.boundary_id is not None and self._permission_wait_coordinator is not None:
            self._permission_wait_coordinator.unregister_live(pending.boundary_id)

    async def claim_continuation(self, pending: PendingPermission) -> Any | None:
        """Claim a detached serial permission continuation exactly once.

        Durable decision claiming is idempotent, but invoking the live
        continuation is not.  Keep this one-shot ownership under the registry
        lock so concurrent/retried permission answers can only return the
        existing acknowledgement.
        """

        async with self._condition:
            key = (pending.task_id, pending.input_id)
            if self._pending.get(key) is not pending:
                return None
            if pending.continuation is None or pending.continuation_claimed:
                return None
            pending.continuation_claimed = True
            return pending.continuation

    async def _lookup(self, response: PermissionResponse) -> PendingPermission:
        async with self._condition:
            pending = self._pending.get((response.task_id, response.input_id))
            if pending is None:
                raise InvalidParamsError("permission_resume_invalid: pending permission is not active.")
            self._validate_response(pending, response)
            return pending

    @staticmethod
    def _validate_response(pending: PendingPermission, response: PermissionResponse) -> None:
        if (
            pending.context_id != response.context_id
            or pending.input_id != response.input_id
            or pending.request.tool_use_id != response.tool_use_id
            or pending.task_id != response.request_task_id
        ):
            raise InvalidParamsError("input_response_mismatch: permission correlation does not match.")


def _json_data_part(part: Any) -> dict[str, Any] | None:
    try:
        has_data = part.HasField("data")
    except (AttributeError, ValueError):
        has_data = False
    if not has_data or getattr(part, "media_type", "") != "application/json":
        return None
    value = MessageToDict(part.data, preserving_proto_field_name=False)
    return value if isinstance(value, dict) else None


def _json_text_part(part: Any) -> dict[str, Any] | None:
    try:
        has_text = part.HasField("text")
    except (AttributeError, ValueError):
        has_text = False
    text = getattr(part, "text", None)
    if not has_text or not isinstance(text, str) or not text.startswith(PERMISSION_QUERY_PREFIX):
        return None
    payload_text = text[len(PERMISSION_QUERY_PREFIX) :].lstrip()
    if not payload_text:
        return None
    try:
        value = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _permission_input_id() -> str:
    return "permission-{}".format(uuid.uuid4().hex)


__all__ = [
    "PERMISSION_QUERY_PREFIX",
    "PendingPermission",
    "PermissionInputRegistry",
    "PermissionResolutionOwner",
    "PermissionResponse",
    "PermissionTaskClosingToken",
    "parse_permission_response",
    "permission_ack_message",
    "permission_display_fields",
    "permission_input_envelope",
    "permission_safe_summary",
]
