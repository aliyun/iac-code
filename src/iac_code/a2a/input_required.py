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

from iac_code.a2a.runtime_overrides import get_a2a_preferred_language
from iac_code.i18n import translate_message
from iac_code.services.permissions.audit import (
    build_prompt_tool_input,
    emit_permission_boundary_audit,
    sanitize_prompt_text,
)
from iac_code.types.stream_events import PermissionRequestEvent

PERMISSION_SCHEMA_VERSION = 1
PERMISSION_DECISIONS = frozenset({"allow_once", "deny"})
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
    decoded_parts = [_json_data_part(part) for part in message.parts]
    permission_parts = [
        value for value in decoded_parts if isinstance(value, dict) and value.get("kind") == "permission"
    ]
    if not permission_parts:
        return None
    if message.role != Role.ROLE_USER:
        raise InvalidParamsError("Permission responses must use ROLE_USER.")
    if len(message.parts) != 1 or len(permission_parts) != 1:
        raise InvalidParamsError("Permission responses must contain exactly one application/json DataPart.")
    payload = permission_parts[0]
    expected_keys = {"schemaVersion", "kind", "requestTaskId", "inputId", "toolUseId", "decision"}
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


class PermissionInputRegistry:
    """Coordinate legacy input-required and concurrent Sub Pipeline permissions."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._pending: dict[tuple[str, str], PendingPermission] = {}
        self._closing_tasks: dict[str, _PermissionTaskClosingState] = {}

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
                pending.task_id == task_id and pending.resolution_owner is None for pending in self._pending.values()
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

    async def answer(self, response: PermissionResponse) -> bool:
        pending = await self._lookup(response)
        if pending.resolution_owner is not None:
            return await pending.resolution_owner.resolve_permission(pending, response)

        async with self._condition:
            self._validate_response(pending, response)
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
                if pending.task_id == task_id
                and pending.resolution_owner is owner
                and pending.state in {"pending", "resolving"}
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


def _permission_input_id() -> str:
    return "permission-{}".format(uuid.uuid4().hex)


__all__ = [
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
