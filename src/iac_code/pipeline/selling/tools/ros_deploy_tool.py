"""Selling pipeline ROS deployment orchestration tool."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

from iac_code.i18n import _
from iac_code.services.permissions.rule_scope import scope_for_rule_source
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.ros_stack import RosStack
from iac_code.tools.cloud.aliyun.template_source import is_remote_template_url
from iac_code.tools.path_safety import check_read_path, resolve_read_path
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionDecisionReason,
    PermissionResult,
    PermissionRuleValue,
    ToolPermissionContext,
)

_OWNED_STACKS_KEY = "ros_deploy_owned_stack_ids"
_ACTIONS = ("create", "continue_create", "delete_and_create", "wait")
_ACTION_ALLOWED_FIELDS = {
    "create": frozenset(("action", "stack_name", "template_url", "parameters", "region_id")),
    "continue_create": frozenset(("action", "stack_id", "template_url", "parameters", "region_id")),
    "delete_and_create": frozenset(("action", "stack_id", "stack_name", "template_url", "parameters", "region_id")),
    "wait": frozenset(("action", "stack_id", "region_id")),
}
_ACTION_REQUIRED_FIELDS = {
    "create": ("stack_name", "template_url"),
    "continue_create": ("stack_id", "template_url"),
    "delete_and_create": ("stack_id", "stack_name", "template_url"),
    "wait": ("stack_id",),
}
_SAFE_RULE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_ROS_ERROR_CODE_RE = re.compile(r"\bcode:\s*([A-Za-z0-9_.-]+)")
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_REASON_CHARS = 80
_RULE_SOURCE_ORDER = {
    "cli_arg": 5,
    "session": 4,
    "local_settings": 3,
    "project_settings": 2,
    "user_settings": 1,
}


def _json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _short_stack_id(stack_id: str) -> str:
    return stack_id[:8] if stack_id else ""


def _friendly_ros_error_code(code: str) -> str:
    if code == "InvalidCidrBlock.Overlapped":
        return _("CIDR block overlapped")
    return code


def _short_status_reason(status_reason: str) -> str:
    match = _ROS_ERROR_CODE_RE.search(status_reason)
    if match is not None:
        return _friendly_ros_error_code(match.group(1))

    reason = _WHITESPACE_RE.sub(" ", status_reason).strip()
    if len(reason) > _MAX_REASON_CHARS:
        return reason[: _MAX_REASON_CHARS - 3].rstrip() + "..."
    return reason


def _safe_rule_segment(value: str) -> bool:
    return bool(_SAFE_RULE_SEGMENT.fullmatch(value))


def _parse_rule(rule: str) -> tuple[str, str] | None:
    prefix = "ros_deploy("
    if not rule.startswith(prefix) or not rule.endswith(")"):
        return None
    inner = rule[len(prefix) : -1]
    if inner.count(":") != 1:
        return None
    action, target = inner.split(":", 1)
    if action not in _ACTIONS:
        return None
    if not _safe_rule_segment(target):
        return None
    return action, target


class RosDeployTool(Tool):
    """Deploy and recover ROS stacks for the selling pipeline."""

    def __init__(self, completion_guard_state: dict[str, Any] | None = None) -> None:
        self._completion_guard_state = completion_guard_state if completion_guard_state is not None else {}

    @property
    def name(self) -> str:
        return "ros_deploy"

    @property
    def timeout(self) -> float | None:
        return 3600.0

    @property
    def description(self) -> str:
        return (
            "Deploy a ROS template in the selling pipeline. Use create for the initial stack, continue_create for "
            "failed stacks created by this step, delete_and_create only after ContinueCreateStackValidationFailed, "
            "and wait to resume polling an already-started stack creation."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "Deployment action to perform.",
                },
                "stack_name": {
                    "type": "string",
                    "description": "StackName for create and delete_and_create.",
                },
                "stack_id": {
                    "type": "string",
                    "description": "Stack ID for wait, or failed stack ID for continue_create/delete_and_create.",
                },
                "template_url": {
                    "type": "string",
                    "description": "Local file path, OSS URL, or HTTP(S) URL for the ROS template.",
                },
                "parameters": {
                    "type": "object",
                    "description": "Template parameters as a plain key/value object.",
                    "propertyNames": {"not": {"pattern": r"^Parameters\."}},
                },
                "region_id": {
                    "type": "string",
                    "description": "Alibaba Cloud region ID. Defaults to the configured region.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def supports_blanket_allow(self) -> bool:
        return False

    def is_read_only(self, input: dict | None = None) -> bool:
        return isinstance(input, dict) and input.get("action") == "wait"

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Deploy")

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        action = _string_value(input.get("action"))
        target = self._rule_target(input)
        return " ".join(part for part in (action, target) if part)

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if verbose:
            return output.strip()

        parsed = _json_object(output)
        if is_error and parsed is not None and parsed.get("error_code"):
            message = _string_value(parsed.get("message"))
            recommended_action = _string_value(parsed.get("recommended_action"))
            detail = _string_value(parsed.get("error_code"))
            if recommended_action:
                detail = _("{error_code}; recommended action: {action}").format(
                    error_code=detail,
                    action=recommended_action,
                )
            if message:
                detail = f"{detail}: {message}"
            return detail

        if parsed is not None:
            if message := self._render_stack_summary(parsed, is_error=is_error):
                return message
        return RosStack().render_tool_result_message(output, is_error=is_error, verbose=verbose)

    def _render_stack_summary(self, parsed: dict[str, Any], *, is_error: bool) -> str | None:
        stack_name = _string_value(parsed.get("stack_name"))
        stack_id = _string_value(parsed.get("stack_id"))
        short_id = _short_stack_id(stack_id)
        name = stack_name or short_id
        if not name:
            return None

        is_success = parsed.get("is_success")
        if is_success is True:
            if short_id and stack_name:
                return _("{name} creation succeeded ({stack_id})").format(name=name, stack_id=short_id)
            return _("{name} creation succeeded").format(name=name)

        if is_success is False or is_error:
            reason = _short_status_reason(_string_value(parsed.get("status_reason")))
            if not reason:
                return None
            if short_id and stack_name:
                return _("{name} creation failed: {reason} ({stack_id})").format(
                    name=name,
                    reason=reason,
                    stack_id=short_id,
                )
            return _("{name} creation failed: {reason}").format(name=name, reason=reason)

        return None

    def needs_event_queue(self) -> bool:
        return True

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return False

    def is_destructive(self, input: dict | None = None) -> bool:
        return not self.is_read_only(input)

    def _new_stack_tool(self) -> RosStack:
        return RosStack(allow_pipeline_deployment_actions=True)

    def validate_input(self, tool_input: dict[str, Any]) -> tuple[bool, str]:
        valid, error = super().validate_input(tool_input)
        if not valid:
            return valid, error
        if action_error := self._action_input_error(tool_input):
            return False, action_error
        return True, ""

    @staticmethod
    def _action_input_error(input: dict[str, Any]) -> str | None:
        action = _string_value(input.get("action"))
        if action not in _ACTIONS:
            return None

        unsupported_fields = sorted(set(input) - _ACTION_ALLOWED_FIELDS[action])
        if unsupported_fields:
            return _("Fields are not supported for action '{action}': {fields}").format(
                action=action,
                fields=", ".join(unsupported_fields),
            )

        missing_fields = [field for field in _ACTION_REQUIRED_FIELDS[action] if not _string_value(input.get(field))]
        if missing_fields:
            return _("Missing required field(s) for action '{action}': {fields}").format(
                action=action,
                fields=", ".join(missing_fields),
            )

        return None

    def _owned_stacks(self) -> dict[str, Any]:
        stacks = self._completion_guard_state.setdefault(_OWNED_STACKS_KEY, {})
        if not isinstance(stacks, dict):
            stacks = {}
            self._completion_guard_state[_OWNED_STACKS_KEY] = stacks
        return stacks

    def _record_owned_stack(self, stack_id: str, *, action: str) -> None:
        if not stack_id:
            return
        self._owned_stacks()[stack_id] = {"action": action}

    def _is_owned_stack(self, stack_id: str) -> bool:
        return bool(stack_id and stack_id in self._owned_stacks())

    def _rule_target(self, input: dict) -> str:
        action = _string_value(input.get("action"))
        if action == "create":
            return _string_value(input.get("stack_name"))
        if action in {"continue_create", "delete_and_create", "wait"}:
            return _string_value(input.get("stack_id"))
        return ""

    def _rule_content(self, input: dict) -> str | None:
        action = _string_value(input.get("action"))
        target = self._rule_target(input)
        if action not in _ACTIONS or not _safe_rule_segment(target):
            return None
        return f"{action}:{target}"

    def _rule_display_text(self, input: dict) -> str | None:
        action = _string_value(input.get("action"))
        target = self._rule_target(input)
        if action not in _ACTIONS or not _safe_rule_segment(target):
            return None
        if action == "create":
            return _("Create ROS stack: {target}").format(target=target)
        if action == "continue_create":
            return _("Continue ROS stack creation: {target}").format(target=target)
        if action == "wait":
            return _("Wait for ROS stack creation: {target}").format(target=target)
        return _("Delete failed ROS stack and create replacement: {target}").format(target=target)

    def _operation_metadata(self, input: dict) -> dict[str, object]:
        operation: dict[str, object] = {}
        action = _string_value(input.get("action"))
        target = self._rule_target(input)
        region = _string_value(input.get("region_id"))
        if action:
            operation["action"] = action
        if target and _safe_rule_segment(target):
            operation["stack_id" if action != "create" else "stack_name"] = target
        if region and _safe_rule_segment(region):
            operation["region"] = region
        return operation

    def _audit(
        self,
        input: dict,
        *,
        scope: str,
        rule_source: str | None = None,
        rule: str | None = None,
        reason: PermissionDecisionReason | None = None,
    ) -> PermissionAuditMetadata:
        return PermissionAuditMetadata(
            scope=scope,
            source="permission_pipeline",
            rule_source=rule_source,
            rule=rule,
            reason_type=reason.type if reason else None,
            reason_detail=reason.detail if reason else None,
            is_read_only=self.is_read_only(input),
            operation=self._operation_metadata(input),
        )

    def _local_template_url_permission_error(
        self,
        input: dict,
        context: ToolPermissionContext,
    ) -> PermissionResult | None:
        template_url = _string_value(input.get("template_url"))
        if not template_url or is_remote_template_url(template_url):
            return None

        decision = check_read_path(
            template_url,
            cwd=context.cwd or ".",
            additional_directories=context.additional_directories,
            trusted_read_directories=context.trusted_read_directories,
            relative_read_directories=context.relative_read_directories,
            strict_read_directories=context.strict_read_directories,
            read_path_violation_behavior=context.read_path_violation_behavior,
        )
        if decision.behavior == "allow":
            return None

        permission = decision.to_permission_result()
        suggestions = None
        if rule_content := self._rule_content(input):
            suggestions = [
                PermissionRuleValue(
                    tool_name=self.name,
                    rule_content=rule_content,
                    display_text=self._rule_display_text(input),
                )
            ]
        return PermissionResult(
            behavior=permission.behavior,
            message=permission.message,
            reason=permission.reason,
            suggestions=suggestions,
            audit=self._audit(input, scope="once", reason=permission.reason),
        )

    def _matching_rule(self, input: dict, rules_by_source: dict[str, list[str]]) -> tuple[str, str] | None:
        action = _string_value(input.get("action"))
        target = self._rule_target(input)
        if action not in _ACTIONS or not _safe_rule_segment(target):
            return None

        best: tuple[tuple[int, int], str, str] | None = None
        for source, rules in rules_by_source.items():
            for index, rule in enumerate(rules):
                parsed = _parse_rule(rule)
                if parsed is None:
                    continue
                action_pattern, target_pattern = parsed
                if action_pattern != action:
                    continue
                if not fnmatch.fnmatchcase(target, target_pattern):
                    continue
                score = (_RULE_SOURCE_ORDER.get(source, 0), -index)
                rule_content = f"{action_pattern}:{target_pattern}"
                if best is None or score > best[0]:
                    best = (score, source, rule_content)
        if best is None:
            return None
        return best[1], best[2]

    def _unowned_stack_permission_error(self, input: dict) -> PermissionResult | None:
        action = _string_value(input.get("action"))
        if action not in {"continue_create", "delete_and_create"}:
            return None
        stack_id = _string_value(input.get("stack_id"))
        if self._is_owned_stack(stack_id):
            return None
        reason = PermissionDecisionReason(
            type="unowned_ros_stack",
            detail="stack was not created by the current selling deployment step",
        )
        message = _("ROS stack {stack_id} was not created by the current selling deployment step.").format(
            stack_id=stack_id or "<missing>"
        )
        return PermissionResult(
            behavior="deny",
            message=message,
            reason=reason,
            audit=self._audit(input, scope="once", reason=reason),
        )

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if not isinstance(context, ToolPermissionContext):
            context = ToolPermissionContext(cwd=context.get("cwd", "") if isinstance(context, dict) else "")

        if unowned := self._unowned_stack_permission_error(input):
            return unowned

        for behavior, rules_by_source in (("deny", context.deny_rules),):
            match = self._matching_rule(input, rules_by_source)
            if match is None:
                continue
            rule_source, rule = match
            detail = _("matched {behavior} rule: {rule}").format(behavior=behavior, rule=rule)
            reason = PermissionDecisionReason(type="rule", detail=detail)
            return PermissionResult(
                behavior=behavior,
                message=detail,
                reason=reason,
                audit=self._audit(
                    input,
                    scope=scope_for_rule_source(rule_source),
                    rule_source=rule_source,
                    rule=rule,
                    reason=reason,
                ),
            )

        if self.is_read_only(input):
            return PermissionResult(behavior="allow", audit=self._audit(input, scope="once"))

        if template_permission := self._local_template_url_permission_error(input, context):
            return template_permission

        for behavior, rules_by_source in (("ask", context.ask_rules), ("allow", context.allow_rules)):
            match = self._matching_rule(input, rules_by_source)
            if match is None:
                continue
            rule_source, rule = match
            detail = _("matched {behavior} rule: {rule}").format(behavior=behavior, rule=rule)
            reason = PermissionDecisionReason(type="rule", detail=detail)
            return PermissionResult(
                behavior=behavior,
                message=detail,
                reason=reason,
                audit=self._audit(
                    input,
                    scope=scope_for_rule_source(rule_source),
                    rule_source=rule_source,
                    rule=rule,
                    reason=reason,
                ),
            )

        reason = PermissionDecisionReason(
            type="untrusted_write",
            detail="ROS deployment operation may modify cloud resources",
        )
        suggestions = None
        if rule_content := self._rule_content(input):
            suggestions = [
                PermissionRuleValue(
                    tool_name=self.name,
                    rule_content=rule_content,
                    display_text=self._rule_display_text(input),
                )
            ]
        return PermissionResult(
            behavior="ask",
            message=_("Allow {}?").format(self.user_facing_name(input)),
            reason=reason,
            suggestions=suggestions,
            audit=self._audit(input, scope="once", reason=reason),
        )

    @staticmethod
    def _require(value: str, field_name: str) -> ToolResult | None:
        if value:
            return None
        return ToolResult.error(_("Missing required field: {}").format(field_name))

    @staticmethod
    def _parameters(input: dict) -> dict[str, Any]:
        parameters = input.get("parameters")
        return dict(parameters) if isinstance(parameters, dict) else {}

    @staticmethod
    def _resolve_template_url(template_url: str, context: ToolContext) -> str:
        if is_remote_template_url(template_url):
            return template_url
        return resolve_read_path(
            template_url,
            context.cwd or ".",
            relative_read_directories=context.relative_read_directories,
        )

    def _create_stack_input(self, input: dict, context: ToolContext) -> ToolResult | dict[str, Any]:
        stack_name = _string_value(input.get("stack_name"))
        template_url = _string_value(input.get("template_url"))
        if error := self._require(stack_name, "stack_name"):
            return error
        if error := self._require(template_url, "template_url"):
            return error
        params: dict[str, Any] = {
            "StackName": stack_name,
            "DisableRollback": True,
            "TemplateURL": self._resolve_template_url(template_url, context),
        }
        if parameters := self._parameters(input):
            params["Parameters"] = parameters
        tool_input: dict[str, Any] = {"action": "CreateStack", "params": params}
        if region_id := _string_value(input.get("region_id")):
            tool_input["region_id"] = region_id
        return tool_input

    def _continue_stack_input(self, input: dict, context: ToolContext) -> ToolResult | dict[str, Any]:
        stack_id = _string_value(input.get("stack_id"))
        template_url = _string_value(input.get("template_url"))
        if error := self._require(stack_id, "stack_id"):
            return error
        if error := self._require(template_url, "template_url"):
            return error
        if not self._is_owned_stack(stack_id):
            return ToolResult.error(
                _("ROS stack {stack_id} was not created by the current selling deployment step.").format(
                    stack_id=stack_id
                )
            )
        params: dict[str, Any] = {
            "StackId": stack_id,
            "Mode": "Recreate",
            "RecreatingOptions": ["AutoRecreatingResources"],
            "TemplateURL": self._resolve_template_url(template_url, context),
        }
        if parameters := self._parameters(input):
            params["Parameters"] = parameters
        tool_input: dict[str, Any] = {"action": "ContinueCreateStack", "params": params}
        if region_id := _string_value(input.get("region_id")):
            tool_input["region_id"] = region_id
        return tool_input

    def _delete_stack_input(self, input: dict) -> ToolResult | dict[str, Any]:
        stack_id = _string_value(input.get("stack_id"))
        if error := self._require(stack_id, "stack_id"):
            return error
        if not self._is_owned_stack(stack_id):
            return ToolResult.error(
                _("ROS stack {stack_id} was not created by the current selling deployment step.").format(
                    stack_id=stack_id
                )
            )
        tool_input: dict[str, Any] = {"action": "DeleteStack", "params": {"StackId": stack_id}}
        if region_id := _string_value(input.get("region_id")):
            tool_input["region_id"] = region_id
        return tool_input

    def _wait_stack_input(self, input: dict) -> ToolResult | dict[str, Any]:
        stack_id = _string_value(input.get("stack_id"))
        if error := self._require(stack_id, "stack_id"):
            return error
        tool_input: dict[str, Any] = {"action": "CreateStack", "params": {"StackId": stack_id}, "stack_id": stack_id}
        if region_id := _string_value(input.get("region_id")):
            tool_input["region_id"] = region_id
        return tool_input

    @staticmethod
    def _preflight_local_template(input: dict, context: ToolContext) -> ToolResult | None:
        template_url = _string_value(input.get("template_url"))
        if not template_url or is_remote_template_url(template_url):
            return None
        try:
            template_path = resolve_read_path(
                template_url,
                context.cwd or ".",
                relative_read_directories=context.relative_read_directories,
            )
            Path(template_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult.error(
                _("Template file is not readable: {template_url}: {error}").format(
                    template_url=template_url,
                    error=str(exc),
                )
            )
        return None

    def _record_result_stack(self, result: ToolResult, *, action: str) -> None:
        parsed = _json_object(result.content)
        stack_id = _string_value(parsed.get("stack_id")) if parsed is not None else ""
        if not stack_id and isinstance(result.metadata, dict):
            stack_id = _string_value(result.metadata.get("stack_id"))
        self._record_owned_stack(stack_id, action=action)

    @staticmethod
    def _continue_validation_failure(result: ToolResult) -> bool:
        return result.is_error and "ContinueCreateStackValidationFailed" in result.content

    @staticmethod
    def _continue_validation_failure_result(stack_id: str, content: str) -> ToolResult:
        return ToolResult.error(
            json.dumps(
                {
                    "stack_id": stack_id,
                    "error_code": "ContinueCreateStackValidationFailed",
                    "recommended_action": "delete_and_create",
                    "message": content,
                },
                ensure_ascii=False,
            )
        )

    async def _call_stack(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._new_stack_tool().execute(tool_input=tool_input, context=context)

    async def _wait_stack(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        stack = self._new_stack_tool()
        region = _string_value(tool_input.get("region_id"))
        if not region:
            region = stack._resolve_region(tool_input)
        return await stack.wait_for_stack_operation(
            tool_input["action"],
            tool_input["params"],
            region,
            tool_input["stack_id"],
            context,
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        action = _string_value(tool_input.get("action"))
        if action not in _ACTIONS:
            return ToolResult.error(_("Invalid action '{}'. Supported actions: {}").format(action, list(_ACTIONS)))
        if action_error := self._action_input_error(tool_input):
            return ToolResult.error(action_error)

        if action == "create":
            create_input = self._create_stack_input(tool_input, context)
            if isinstance(create_input, ToolResult):
                return create_input
            result = await self._call_stack(create_input, context)
            self._record_result_stack(result, action="create")
            return result

        if action == "continue_create":
            continue_input = self._continue_stack_input(tool_input, context)
            if isinstance(continue_input, ToolResult):
                return continue_input
            result = await self._call_stack(continue_input, context)
            if self._continue_validation_failure(result):
                return self._continue_validation_failure_result(
                    _string_value(tool_input.get("stack_id")), result.content
                )
            self._record_result_stack(result, action="continue_create")
            return result

        if action == "wait":
            wait_input = self._wait_stack_input(tool_input)
            if isinstance(wait_input, ToolResult):
                return wait_input
            result = await self._wait_stack(wait_input, context)
            self._record_result_stack(result, action="wait")
            return result

        create_input = self._create_stack_input(tool_input, context)
        if isinstance(create_input, ToolResult):
            return create_input

        delete_input = self._delete_stack_input(tool_input)
        if isinstance(delete_input, ToolResult):
            return delete_input
        if error := self._preflight_local_template(tool_input, context):
            return error
        delete_result = await self._call_stack(delete_input, context)
        if delete_result.is_error:
            return delete_result
        create_result = await self._call_stack(create_input, context)
        self._record_result_stack(create_result, action="delete_and_create")
        return create_result
