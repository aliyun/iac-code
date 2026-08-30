"""Bash tool permission engine — combines parsing, rules, paths, safety, and modes."""

from __future__ import annotations

import os

from iac_code.i18n import _
from iac_code.services.permissions.rule_scope import scope_for_rule_source
from iac_code.tools.bash.argv_safety import dangerous_readonly_argument
from iac_code.tools.bash.command_parser import ParseResult, SimpleCommand, parse_command
from iac_code.tools.bash.mode_validation import check_permission_mode
from iac_code.tools.bash.path_validation import check_path_constraints, check_read_path_constraints
from iac_code.tools.bash.readonly_commands import is_command_readonly
from iac_code.tools.bash.rule_matching import find_matching_rules, normalize_command
from iac_code.tools.bash.safety_checks import check_command_safety, check_safety
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionDecisionReason,
    PermissionResult,
    PermissionRuleValue,
    ToolPermissionContext,
)

_MAX_SUBCOMMANDS = 10

_BASH_BLANKET_ALLOW_RULE = "bash(**)"
_BASH_BLANKET_ALLOW_SOURCES = frozenset(
    {
        "user_settings",
        "project_settings",
        "local_settings",
        "cli_arg",
    }
)

_BEHAVIOR_ORDER = {"deny": 0, "ask": 1, "passthrough": 2, "allow": 3}


def _matching_rules_with_sources(
    command: str,
    rules_by_source: dict[str, list[str]],
    behavior: str,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for source, rules in rules_by_source.items():
        if behavior == "allow":
            matched = find_matching_rules(command, rules, [], [])["allow"]
        elif behavior == "deny":
            matched = find_matching_rules(command, [], rules, [])["deny"]
        else:
            matched = find_matching_rules(command, [], [], rules)["ask"]
        out.extend((source, rule) for rule in matched)
    return out


def _rule_audit(
    matches: list[tuple[str, str]],
    *,
    reason_detail: str,
    is_read_only: bool,
) -> PermissionAuditMetadata | None:
    if not matches:
        return None
    source, rule = matches[0]
    return PermissionAuditMetadata(
        scope=scope_for_rule_source(source),
        source="permission_pipeline",
        rule_source=source,
        rule=rule,
        reason_type="rule",
        reason_detail=reason_detail,
        is_read_only=is_read_only,
        operation={"is_read_only": is_read_only},
    )


def _command_text_is_readonly(command: str) -> bool:
    parsed = parse_command(command)
    if parsed.kind != "simple" or not parsed.commands:
        return False
    return all(is_command_readonly(cmd) for cmd in parsed.commands)


def _configured_bash_blanket_allow(
    rules_by_source: dict[str, list[str]],
) -> tuple[str, str] | None:
    """Return an explicitly configured ``bash(**)`` rule.

    ``bash(**)`` is deliberately recognized by exact spelling instead of the
    wildcard matcher.  Runtime-created session rules therefore cannot turn a
    normal per-command allow suggestion into blanket Bash permission.
    """

    for source, rules in rules_by_source.items():
        if source not in _BASH_BLANKET_ALLOW_SOURCES:
            continue
        for rule in rules:
            if rule.strip() == _BASH_BLANKET_ALLOW_RULE:
                return source, rule
    return None


def _safe_mode_path_policy_active(context: ToolPermissionContext) -> bool:
    """Whether the permission context carries fail-closed safe-mode roots."""

    return bool(context.strict_read_directories) or context.read_path_violation_behavior == "deny"


def _blanket_allow_result(rule_match: tuple[str, str]) -> PermissionResult:
    source, rule = rule_match
    detail = _("matched allow rule(s): {}").format(rule)
    return PermissionResult(
        behavior="allow",
        message=detail,
        reason=PermissionDecisionReason(type="rule", detail=detail),
        audit=PermissionAuditMetadata(
            scope=scope_for_rule_source(source),
            source="permission_pipeline",
            rule_source=source,
            rule=rule,
            reason_type="rule",
            reason_detail=detail,
            is_read_only=False,
            operation={"is_read_only": False, "blanket_bash_allow": True},
        ),
    )


def _generate_suggestions(
    commands: list[SimpleCommand], sub_results: list[PermissionResult] | None = None
) -> list[PermissionRuleValue]:
    """Generate suggestions from sub-commands, skipping dangerous builtins and already-allowed ones."""
    from iac_code.tools.bash.command_parser import DANGEROUS_BUILTINS

    seen: set[str] = set()
    result: list[PermissionRuleValue] = []
    for i, cmd in enumerate(commands):
        if not cmd.argv:
            continue
        if sub_results and i < len(sub_results) and sub_results[i].behavior == "allow":
            continue
        base = os.path.basename(cmd.argv[0])
        if not base:
            continue
        if base in DANGEROUS_BUILTINS:
            continue
        rule = "{}:*".format(base)
        if rule not in seen:
            seen.add(rule)
            result.append(PermissionRuleValue(tool_name="bash", rule_content=rule))
    return result


def _generate_suggestions_from_text(command: str) -> list[PermissionRuleValue]:
    """Fallback: generate suggestions from raw command text."""
    normalized = normalize_command(command.strip())
    first = normalized.split(None, 1)[0] if normalized else ""
    if not first:
        return []
    base = os.path.basename(first)
    return [PermissionRuleValue(tool_name="bash", rule_content="{}:*".format(base))]


def _merge_results(results: list[PermissionResult]) -> PermissionResult:
    if not results:
        return PermissionResult(behavior="passthrough")
    _, best = min(enumerate(results), key=lambda ie: (_BEHAVIOR_ORDER[ie[1].behavior], ie[0]))
    audit = best.audit
    if audit is None and best.behavior == "allow":
        audit = next(
            (result.audit for result in results if result.behavior == "allow" and result.audit is not None),
            None,
        )
    return PermissionResult(
        behavior=best.behavior,
        message=best.message,
        reason=best.reason,
        suggestions=best.suggestions,
        audit=audit,
    )


def _with_suggestions_if_needed(
    result: PermissionResult,
    command: str,
    commands: list[SimpleCommand] | None = None,
    sub_results: list[PermissionResult] | None = None,
) -> PermissionResult:
    if result.suggestions:
        return result
    if result.reason is not None and result.reason.type == "dangerous_readonly_argument":
        return result
    if commands:
        sug = _generate_suggestions(commands, sub_results=sub_results)
    else:
        sug = _generate_suggestions_from_text(command)
    if not sug:
        return result
    return PermissionResult(
        behavior=result.behavior,
        message=result.message,
        reason=result.reason,
        suggestions=sug,
        audit=result.audit,
    )


def _command_base(cmd: SimpleCommand) -> str | None:
    if not cmd.argv:
        return None
    return os.path.basename(cmd.argv[0])


def _dangerous_arg_label(arg: str) -> str:
    if arg == "sed in-place edit":
        return _("sed in-place edit")
    if arg == "sed script file":
        return _("sed script file")
    if arg == "sed shell execution":
        return _("sed shell execution")
    if arg == "sed file write":
        return _("sed file write")
    return arg


def bash_tool_check_permission(
    cmd: SimpleCommand,
    context: ToolPermissionContext,
    compound_has_cd: bool = False,
    blanket_allow: tuple[str, str] | None = None,
) -> PermissionResult:
    if not cmd.argv:
        if blanket_allow is not None and not _safe_mode_path_policy_active(context):
            return _blanket_allow_result(blanket_allow)
        if cmd.is_complex:
            detail = _("complex command requires confirmation")
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="complex_command", detail=detail),
            )
        return PermissionResult(behavior="passthrough")

    matched_by_source = {
        "allow": _matching_rules_with_sources(cmd.text, context.allow_rules, "allow"),
        "deny": _matching_rules_with_sources(cmd.text, context.deny_rules, "deny"),
        "ask": _matching_rules_with_sources(cmd.text, context.ask_rules, "ask"),
    }
    matched = {behavior: [rule for _source, rule in matches] for behavior, matches in matched_by_source.items()}
    if matched["deny"]:
        detail = _("matched deny rule(s): {}").format(", ".join(matched["deny"]))
        return PermissionResult(
            behavior="deny",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
            audit=_rule_audit(matched_by_source["deny"], reason_detail=detail, is_read_only=is_command_readonly(cmd)),
        )
    if matched["ask"]:
        detail = _("matched ask rule(s): {}").format(", ".join(matched["ask"]))
        return PermissionResult(
            behavior="ask",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
            audit=_rule_audit(matched_by_source["ask"], reason_detail=detail, is_read_only=is_command_readonly(cmd)),
        )

    path_res = check_path_constraints(cmd, context.cwd, context.additional_directories)
    if path_res.behavior != "passthrough":
        if blanket_allow is None or path_res.behavior == "deny" or _safe_mode_path_policy_active(context):
            return path_res

    dangerous_arg = dangerous_readonly_argument(cmd.argv)
    if dangerous_arg is not None:
        if blanket_allow is None or _safe_mode_path_policy_active(context):
            detail = _("dangerous readonly argument requires confirmation: {}").format(
                _dangerous_arg_label(dangerous_arg)
            )
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="dangerous_readonly_argument", detail=detail),
            )

    read_path_res = check_read_path_constraints(
        cmd,
        context.cwd,
        context.additional_directories,
        context.trusted_read_directories,
        strict_read_directories=context.strict_read_directories,
        read_path_violation_behavior=context.read_path_violation_behavior,
        compound_has_cd=compound_has_cd,
    )
    if read_path_res.behavior != "passthrough":
        if blanket_allow is None or read_path_res.behavior == "deny" or _safe_mode_path_policy_active(context):
            return read_path_res

    if cmd.is_complex and (blanket_allow is None or _safe_mode_path_policy_active(context)):
        detail = _("complex command requires confirmation")
        return PermissionResult(
            behavior="ask",
            message=detail,
            reason=PermissionDecisionReason(type="complex_command", detail=detail),
        )

    if blanket_allow is not None:
        return _blanket_allow_result(blanket_allow)

    if matched["allow"]:
        detail = _("matched allow rule(s): {}").format(", ".join(matched["allow"]))
        return PermissionResult(
            behavior="allow",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
            audit=_rule_audit(matched_by_source["allow"], reason_detail=detail, is_read_only=is_command_readonly(cmd)),
        )

    mode_res = check_permission_mode(cmd, context.mode)
    if mode_res.behavior != "passthrough":
        return mode_res

    if is_command_readonly(cmd):
        return PermissionResult(behavior="allow")

    safety_res = check_safety(cmd, context.cwd)
    if safety_res.behavior != "passthrough":
        return safety_res

    return PermissionResult(behavior="passthrough")


async def bash_tool_has_permission(command: str, context: ToolPermissionContext) -> PermissionResult:
    if not check_command_safety(command):
        detail = _("command failed basic safety checks")
        return _with_suggestions_if_needed(
            PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="safety_check", detail=detail),
            ),
            command,
        )

    blanket_allow = _configured_bash_blanket_allow(context.allow_rules)

    full_deny_matches = _matching_rules_with_sources(command, context.deny_rules, "deny")
    full_ask_matches = _matching_rules_with_sources(command, context.ask_rules, "ask")
    full_matches = {
        "deny": [rule for _source, rule in full_deny_matches],
        "ask": [rule for _source, rule in full_ask_matches],
    }
    if full_matches["deny"]:
        detail = _("matched deny rule(s) on full command: {}").format(", ".join(full_matches["deny"]))
        return PermissionResult(
            behavior="deny",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
            audit=_rule_audit(full_deny_matches, reason_detail=detail, is_read_only=_command_text_is_readonly(command)),
        )

    if blanket_allow is not None and full_matches["ask"]:
        detail = _("matched ask rule(s): {}").format(", ".join(full_matches["ask"]))
        return PermissionResult(
            behavior="ask",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
            audit=_rule_audit(full_ask_matches, reason_detail=detail, is_read_only=_command_text_is_readonly(command)),
        )

    parsed: ParseResult = parse_command(command)
    if parsed.kind in ("too_complex", "parse_error"):
        if blanket_allow is not None and not _safe_mode_path_policy_active(context):
            return _blanket_allow_result(blanket_allow)
        if parsed.kind == "too_complex":
            kind_label = _("command too complex to analyze")
        else:
            kind_label = _("could not parse command")
        detail = parsed.reason or kind_label
        return _with_suggestions_if_needed(
            PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type=parsed.kind, detail=detail),
            ),
            command,
        )

    subcommands = parsed.commands
    if not subcommands:
        if blanket_allow is not None and not _safe_mode_path_policy_active(context):
            return _blanket_allow_result(blanket_allow)
        return _with_suggestions_if_needed(PermissionResult(behavior="passthrough"), command)

    if len(subcommands) > _MAX_SUBCOMMANDS and (blanket_allow is None or _safe_mode_path_policy_active(context)):
        detail = _("too many subcommands (>{})").format(_MAX_SUBCOMMANDS)
        return _with_suggestions_if_needed(
            PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="compound_limit", detail=detail),
            ),
            command,
            commands=subcommands,
        )

    cd_bases = [c for c in subcommands if _command_base(c) == "cd"]
    if len(cd_bases) > 1 and (blanket_allow is None or _safe_mode_path_policy_active(context)):
        detail = _("multiple cd commands in compound command")
        return _with_suggestions_if_needed(
            PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="compound_cd", detail=detail),
            ),
            command,
            commands=subcommands,
        )

    has_git = any(_command_base(c) == "git" for c in subcommands)
    if cd_bases and has_git and (blanket_allow is None or _safe_mode_path_policy_active(context)):
        detail = _("cd combined with git in compound command")
        return _with_suggestions_if_needed(
            PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="compound_cd_git", detail=detail),
            ),
            command,
            commands=subcommands,
        )

    compound_has_cd = bool(cd_bases)
    sub_results = [
        bash_tool_check_permission(
            sc,
            context,
            compound_has_cd=compound_has_cd,
            blanket_allow=blanket_allow,
        )
        for sc in subcommands
    ]
    merged = _merge_results(sub_results)
    return _with_suggestions_if_needed(merged, command, commands=subcommands, sub_results=sub_results)
