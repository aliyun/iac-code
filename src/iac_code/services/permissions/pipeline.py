"""Global permission pipeline wrapping per-tool permission checks."""

from __future__ import annotations

from dataclasses import replace

from iac_code.i18n import _
from iac_code.services.permissions.rule_scope import scope_for_rule_source
from iac_code.tools.base import Tool
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionDecisionReason,
    PermissionMode,
    PermissionResult,
    ToolPermissionContext,
)

_STICKY_ASK_REASONS = frozenset(
    {
        "safety_check",
        "path_constraint",
        "dangerous_readonly_argument",
        "complex_command",
        "compound_cd",
        "compound_cd_git",
        "compound_limit",
        "parse_error",
        "too_complex",
    }
)


def _is_explicit_operation_write_allow(result: PermissionResult, tool: Tool) -> bool:
    return (
        _uses_operation_scoped_permissions(tool)
        and result.behavior == "allow"
        and result.audit is not None
        and result.audit.is_read_only is not True
        and result.audit.reason_type == "rule"
    )


def _is_explicit_operation_ask(result: PermissionResult, tool: Tool) -> bool:
    """Keep an operation-scoped ask rule authoritative in bypass mode."""

    return (
        _uses_operation_scoped_permissions(tool)
        and result.behavior == "ask"
        and result.audit is not None
        and result.audit.reason_type == "rule"
    )


def _uses_operation_scoped_permissions(tool: Tool) -> bool:
    """Read the optional capability without breaking legacy duck-typed tools."""

    return bool(getattr(tool, "uses_operation_scoped_permissions", False))


def _is_read_only_operation_allow(result: PermissionResult, tool: Tool) -> bool:
    """Keep metadata-confirmed operation reads automatic despite a bare ask rule."""

    return (
        _uses_operation_scoped_permissions(tool)
        and result.behavior == "allow"
        and result.audit is not None
        and result.audit.is_read_only is True
    )


def _get_tool_rule(tool_name: str, rules_by_source: dict[str, list[str]]) -> tuple[str, str] | None:
    """Check if there's a bare tool-name rule (e.g. 'write_file' without parens)."""
    for source, rules in rules_by_source.items():
        for rule in rules:
            if rule == tool_name:
                return source, rule
    return None


def _audit(
    *,
    scope: str,
    rule_source: str | None = None,
    rule: str | None = None,
    reason_type: str | None = None,
    reason_detail: str | None = None,
    is_read_only: bool | None = None,
    operation: dict[str, object] | None = None,
    inherit: PermissionAuditMetadata | None = None,
) -> PermissionAuditMetadata:
    final_operation = dict(operation or {})
    if inherit is not None:
        final_operation.update(inherit.operation)
    return PermissionAuditMetadata(
        scope=scope,
        source="permission_pipeline",
        rule_source=rule_source,
        rule=rule,
        reason_type=reason_type,
        reason_detail=reason_detail,
        is_read_only=inherit.is_read_only if inherit is not None and inherit.is_read_only is not None else is_read_only,
        operation=final_operation,
    )


def _is_safety_check_ask(result: PermissionResult) -> bool:
    return result.behavior == "ask" and "safety_check" in _structured_reason_types(result)


def _is_sticky_ask(result: PermissionResult) -> bool:
    return result.behavior == "ask" and not _STICKY_ASK_REASONS.isdisjoint(_structured_reason_types(result))


def _structured_reason_types(result: PermissionResult) -> set[str]:
    reasons = [result.reason, *(result.reasons or [])]
    return {reason.type for reason in reasons if reason is not None}


def _with_prompt_audit(tool: Tool, input: dict, result: PermissionResult) -> PermissionResult:
    if result.audit is not None or result.behavior != "ask":
        return result
    reason_type = result.reason.type if result.reason is not None else "prompt_required"
    return replace(
        result,
        audit=_audit(
            scope="once",
            reason_type=reason_type,
            reason_detail=reason_type,
            is_read_only=tool.is_read_only(input),
            operation=tool.permission_audit_operation(input),
        ),
    )


async def check_tool_permission(
    tool: Tool,
    input: dict,
    context: ToolPermissionContext,
) -> PermissionResult:
    """Apply tool-level rules, tool-internal checks, mode, and post-processing."""
    ask_rule = _get_tool_rule(tool.name, context.ask_rules)

    deny_rule = _get_tool_rule(tool.name, context.deny_rules)
    if deny_rule is not None:
        source, rule = deny_rule
        return PermissionResult(
            behavior="deny",
            audit=_audit(
                scope=scope_for_rule_source(source),
                rule_source=source,
                rule=rule,
                reason_type="rule",
                reason_detail="matched deny rule: {}".format(rule),
                is_read_only=tool.is_read_only(input),
                operation=tool.permission_audit_operation(input),
            ),
        )

    result = await tool.check_permissions(input, context)

    if result.behavior == "deny":
        return result

    if _is_safety_check_ask(result) and context.mode != PermissionMode.BYPASS_PERMISSIONS:
        return _with_prompt_audit(tool, input, result)

    if result.behavior == "ask" and ask_rule is not None:
        source, rule = ask_rule
        detail = _("matched ask rule(s): {}").format(rule)
        return replace(
            result,
            behavior="ask",
            audit=_audit(
                scope=scope_for_rule_source(source),
                rule_source=source,
                rule=rule,
                reason_type="rule",
                reason_detail=detail,
                is_read_only=tool.is_read_only(input),
                operation=tool.permission_audit_operation(input),
                inherit=result.audit,
            ),
        )

    if result.behavior == "allow" and ask_rule is not None and not _is_read_only_operation_allow(result, tool):
        source, rule = ask_rule
        detail = _("matched ask rule(s): {}").format(tool.name)
        return replace(
            result,
            behavior="ask",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
            audit=_audit(
                scope=scope_for_rule_source(source),
                rule_source=source,
                rule=rule,
                reason_type="rule",
                reason_detail=_("matched ask rule(s): {}").format(rule),
                is_read_only=tool.is_read_only(input),
                operation=tool.permission_audit_operation(input),
                inherit=result.audit,
            ),
        )

    if _is_explicit_operation_ask(result, tool) or (
        context.mode != PermissionMode.BYPASS_PERMISSIONS
        and _uses_operation_scoped_permissions(tool)
        and _is_sticky_ask(result)
    ):
        return _with_prompt_audit(tool, input, result)

    if context.mode == PermissionMode.BYPASS_PERMISSIONS and not _is_explicit_operation_write_allow(result, tool):
        return replace(
            result,
            behavior="allow",
            message="",
            reason=None,
            reasons=None,
            suggestions=None,
            audit=_audit(
                scope="mode",
                rule_source="mode",
                reason_type="bypass_permissions",
                reason_detail="bypass_permissions mode",
                is_read_only=tool.is_read_only(input),
                operation=tool.permission_audit_operation(input),
                inherit=result.audit,
            ),
        )

    if _is_sticky_ask(result):
        return _with_prompt_audit(tool, input, result)

    if (
        (allow_rule := _get_tool_rule(tool.name, context.allow_rules)) is not None
        and result.behavior in ("passthrough", "ask")
        and tool.supports_blanket_allow
    ):
        source, rule = allow_rule
        return replace(
            result,
            behavior="allow",
            message="",
            reason=None,
            reasons=None,
            suggestions=None,
            audit=_audit(
                scope=scope_for_rule_source(source),
                rule_source=source,
                rule=rule,
                reason_type="rule",
                reason_detail="matched allow rule: {}".format(rule),
                is_read_only=tool.is_read_only(input),
                operation=tool.permission_audit_operation(input),
                inherit=result.audit,
            ),
        )

    if result.behavior == "passthrough":
        result = replace(
            result,
            behavior="ask",
            message=_("Allow {}?").format(tool.user_facing_name(input)),
            audit=_audit(
                scope="once",
                reason_type="prompt_required",
                reason_detail="prompt_required",
                is_read_only=tool.is_read_only(input),
                operation=tool.permission_audit_operation(input),
            ),
        )

    if context.mode == PermissionMode.DONT_ASK and result.behavior == "ask":
        return replace(
            result,
            behavior="deny",
            message="",
            reason=None,
            reasons=None,
            suggestions=None,
            audit=_audit(
                scope="mode",
                rule_source="mode",
                reason_type="dont_ask",
                reason_detail="dont_ask converted ask to deny",
                is_read_only=tool.is_read_only(input),
                operation=tool.permission_audit_operation(input),
                inherit=result.audit,
            ),
        )

    return result
