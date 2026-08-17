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
    return "是否允许本次操作：{}？".format(title) if language == "zh" else "{} Allow once?".format(title)


def permission_options(*, language: str | None = None) -> list[dict[str, str]]:
    if language == "zh":
        return [
            {"id": "allow_once", "label": "本次允许"},
            {"id": "deny", "label": "拒绝"},
        ]
    return [
        {"id": "allow_once", "label": "Allow once"},
        {"id": "deny", "label": "Deny"},
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
        purpose = (
            "为当前阿里云基础设施任务调用 {}。".format(operation_name)
            if language == "zh"
            else "Call {} for the requested Alibaba Cloud infrastructure task.".format(operation_name)
        )
        target = operation_name
        if region:
            target += ("，地域 " if language == "zh" else " in ") + region
        if stack_name:
            target += ("；资源栈 " if language == "zh" else "; stack ") + stack_name
        elif stack_id:
            target += ("；资源栈 " if language == "zh" else "; stack ") + stack_id
        effect = "read" if is_read_only else ("cloud_change" if read_only_known else "unknown")
    elif tool_name == "bash":
        if language == "zh":
            title = "读取本地工作区数据" if is_read_only else "运行本地 Shell 命令"
        else:
            title = "Read local workspace data" if is_read_only else "Run a local shell command"
        purpose = (
            ("读取当前基础设施任务所需的本地数据。" if is_read_only else "执行当前基础设施任务所需的本地命令。")
            if language == "zh"
            else (
                "Read local data needed for the requested infrastructure task."
                if is_read_only
                else "Execute a local command needed for the requested infrastructure task."
            )
        )
        command = None
        if isinstance(safe_input, dict):
            command = safe_input.get("command") or safe_input.get("cmd")
        target = (
            ("当前本地工作区；命令：{}" if language == "zh" else "the current local workspace; command: {}").format(
                _display_text(command, fallback="shell command", maximum=240)
            )
            if isinstance(command, str) and command.strip()
            else ("当前本地工作区" if language == "zh" else "the current local workspace")
        )
        effect = "read" if is_read_only else ("local_execution" if read_only_known else "unknown")
    elif tool_name in {"write_file", "edit_file"}:
        title = "修改工作区文件" if language == "zh" else "Change a workspace file"
        purpose = (
            "写入当前基础设施任务所需的文件。"
            if language == "zh"
            else "Write a file needed for the requested infrastructure task."
        )
        target = _safe_input_target(safe_input) or (
            "当前工作区中的文件" if language == "zh" else "a file in the current workspace"
        )
        effect = "file_change"
    elif tool_name in {"read_file", "glob", "grep"} or is_read_only:
        title = (
            "使用 {} 读取工作区数据".format(public_tool)
            if language == "zh"
            else "Read workspace data with {}".format(public_tool)
        )
        purpose = (
            "读取当前基础设施任务所需的本地数据。"
            if language == "zh"
            else "Read local data needed for the requested infrastructure task."
        )
        target = _safe_input_target(safe_input) or (
            "当前本地工作区" if language == "zh" else "the current local workspace"
        )
        effect = "read"
    else:
        title = "运行 {}".format(public_tool) if language == "zh" else "Run {}".format(public_tool)
        purpose = (
            "为当前基础设施任务执行此操作。"
            if language == "zh"
            else "Run this operation for the requested infrastructure task."
        )
        target = _safe_input_target(safe_input) or (
            "当前任务工作区或云账号" if language == "zh" else "the current task workspace or cloud account"
        )
        effect = "local_or_remote_change" if read_only_known else "unknown"

    display: dict[str, Any] = {
        "title": _display_text(title, fallback="需要权限确认" if language == "zh" else "Permission required"),
        "purpose": _display_text(
            purpose,
            fallback="完成当前基础设施任务。" if language == "zh" else "Complete the requested infrastructure task.",
        ),
        "effect": _display_text(effect, fallback="unknown", maximum=80),
        "target": _display_text(target, fallback="当前任务范围" if language == "zh" else "the current task scope"),
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
    if language == "zh":
        parts = ["部署 ROS 资源栈"]
        if candidate:
            parts.append("方案：{}".format(candidate))
        if region:
            parts.append("地域：{}".format(region))
        if stack_name:
            parts.append("资源栈：{}".format(stack_name))
        if template:
            parts.append("模板：{}".format(template))
        if total:
            parts.append("预计月费用：{}".format(total))
        if resource_parts:
            parts.append("资源费用：{}".format("；".join(resource_parts)))
    else:
        parts = ["Deploy a ROS stack"]
        if candidate:
            parts.append("plan: {}".format(candidate))
        if region:
            parts.append("region: {}".format(region))
        if stack_name:
            parts.append("stack: {}".format(stack_name))
        if template:
            parts.append("template: {}".format(template))
        if total:
            parts.append("estimated monthly cost: {}".format(total))
        if resource_parts:
            parts.append("resource costs: {}".format("; ".join(resource_parts)))
    rendered = "；".join(parts) if language == "zh" else "; ".join(parts)
    return sanitize_prompt_text(rendered, max_chars=_SAFE_SUMMARY_MAX_CHARS) or "ros_deploy"


def _safe_scalar(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else ""


def _cloud_operation_title(product: str, action: str, *, is_read_only: bool, language: str = "en") -> str:
    product_label = product.upper() if product.lower() == "ros" else product
    if language == "zh":
        stack_actions = {
            "CreateStack": "创建 {} 资源栈",
            "ContinueCreateStack": "继续创建 {} 资源栈",
            "UpdateStack": "更新 {} 资源栈",
            "DeleteStack": "删除 {} 资源栈",
        }
        template = stack_actions.get(action)
        if template:
            return template.format(product_label)
        operation_name = " ".join(value for value in (product, action) if value)
        return "使用 {} 读取阿里云数据".format(operation_name) if is_read_only else "执行 {}".format(operation_name)
    stack_actions = {
        "CreateStack": "Create {} stack",
        "ContinueCreateStack": "Continue creating {} stack",
        "UpdateStack": "Update {} stack",
        "DeleteStack": "Delete {} stack",
    }
    template = stack_actions.get(action)
    if template:
        return template.format(product_label)
    operation_name = " ".join(value for value in (product, action) if value)
    return (
        "Read Alibaba Cloud data with {}".format(operation_name)
        if is_read_only
        else "Run {}".format(operation_name)
    )


def _display_text(value: str, *, fallback: str, maximum: int = _DISPLAY_FIELD_MAX_CHARS) -> str:
    return sanitize_prompt_text(value, max_chars=maximum) or fallback


def _safe_input_target(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("file_path", "filePath", "path", "region_id", "regionId", "resource_id", "resourceId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return _display_text(candidate, fallback="the current task scope")
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
