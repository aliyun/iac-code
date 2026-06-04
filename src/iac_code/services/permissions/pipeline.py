"""Global permission pipeline wrapping per-tool permission checks."""

from __future__ import annotations

from iac_code.i18n import _
from iac_code.tools.base import Tool
from iac_code.types.permissions import PermissionDecisionReason, PermissionMode, PermissionResult, ToolPermissionContext

_STICKY_ASK_REASONS = frozenset(
    {
        "safety_check",
        "path_constraint",
        "dangerous_readonly_argument",
        "complex_command",
        "parse_error",
        "too_complex",
    }
)


def _get_tool_rule(tool_name: str, rules_by_source: dict[str, list[str]]) -> str | None:
    """Check if there's a bare tool-name rule (e.g. 'write_file' without parens)."""
    for source, rules in rules_by_source.items():
        for rule in rules:
            if rule == tool_name:
                return source
    return None


def _is_safety_check_ask(result: PermissionResult) -> bool:
    return result.behavior == "ask" and result.reason is not None and result.reason.type == "safety_check"


def _is_sticky_ask(result: PermissionResult) -> bool:
    return result.behavior == "ask" and result.reason is not None and result.reason.type in _STICKY_ASK_REASONS


async def check_tool_permission(
    tool: Tool,
    input: dict,
    context: ToolPermissionContext,
) -> PermissionResult:
    """Apply tool-level rules, tool-internal checks, mode, and post-processing."""
    tool_level_ask = _get_tool_rule(tool.name, context.ask_rules) is not None

    if _get_tool_rule(tool.name, context.deny_rules) is not None:
        return PermissionResult(behavior="deny")

    result = await tool.check_permissions(input, context)

    if result.behavior == "deny":
        return result

    if _is_safety_check_ask(result):
        return result

    if result.behavior == "ask" and tool_level_ask:
        return result

    if result.behavior == "allow" and tool_level_ask:
        detail = _("matched ask rule(s): {}").format(tool.name)
        return PermissionResult(
            behavior="ask",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
        )

    if context.mode == PermissionMode.BYPASS_PERMISSIONS and not _is_safety_check_ask(result):
        return PermissionResult(behavior="allow")

    if _is_sticky_ask(result):
        return result

    if (
        _get_tool_rule(tool.name, context.allow_rules) is not None
        and result.behavior in ("passthrough", "ask")
        and tool.supports_blanket_allow
    ):
        return PermissionResult(behavior="allow")

    if result.behavior == "passthrough":
        result = PermissionResult(
            behavior="ask",
            message=_("Allow {}?").format(tool.user_facing_name(input)),
            suggestions=result.suggestions,
        )

    if context.mode == PermissionMode.DONT_ASK and result.behavior == "ask":
        return PermissionResult(behavior="deny")

    return result
