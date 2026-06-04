"""Bash tool permission engine — combines parsing, rules, paths, safety, and modes."""

from __future__ import annotations

import os

from iac_code.i18n import _
from iac_code.tools.bash.argv_safety import dangerous_readonly_argument
from iac_code.tools.bash.command_parser import ParseResult, SimpleCommand, parse_command
from iac_code.tools.bash.mode_validation import check_permission_mode
from iac_code.tools.bash.path_validation import check_path_constraints, check_read_path_constraints
from iac_code.tools.bash.readonly_commands import is_command_readonly
from iac_code.tools.bash.rule_matching import find_matching_rules, normalize_command
from iac_code.tools.bash.safety_checks import check_command_safety, check_safety
from iac_code.types.permissions import (
    PermissionDecisionReason,
    PermissionResult,
    PermissionRuleValue,
    ToolPermissionContext,
)

_MAX_SUBCOMMANDS = 10

_BEHAVIOR_ORDER = {"deny": 0, "ask": 1, "passthrough": 2, "allow": 3}


def _collect_all_rules(rules_by_source: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for _source, rules in rules_by_source.items():
        out.extend(rules)
    return out


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
    return PermissionResult(
        behavior=best.behavior,
        message=best.message,
        reason=best.reason,
        suggestions=best.suggestions,
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
    )


def _command_base(cmd: SimpleCommand) -> str | None:
    if not cmd.argv:
        return None
    return os.path.basename(cmd.argv[0])


def bash_tool_check_permission(
    cmd: SimpleCommand,
    context: ToolPermissionContext,
    compound_has_cd: bool = False,
) -> PermissionResult:
    if not cmd.argv:
        return PermissionResult(behavior="passthrough")

    allow_flat = _collect_all_rules(context.allow_rules)
    deny_flat = _collect_all_rules(context.deny_rules)
    ask_flat = _collect_all_rules(context.ask_rules)

    matched = find_matching_rules(cmd.text, allow_flat, deny_flat, ask_flat)
    if matched["deny"]:
        detail = _("matched deny rule(s): {}").format(", ".join(matched["deny"]))
        return PermissionResult(
            behavior="deny",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
        )
    if matched["ask"]:
        detail = _("matched ask rule(s): {}").format(", ".join(matched["ask"]))
        return PermissionResult(
            behavior="ask",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
        )

    path_res = check_path_constraints(cmd, context.cwd, context.additional_directories)
    if path_res.behavior != "passthrough":
        return path_res

    dangerous_arg = dangerous_readonly_argument(cmd.argv)
    if dangerous_arg is not None:
        detail = _("dangerous readonly argument requires confirmation: {}").format(dangerous_arg)
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
        compound_has_cd=compound_has_cd,
    )
    if read_path_res.behavior != "passthrough":
        return read_path_res

    if cmd.is_complex:
        detail = _("complex command requires confirmation")
        return PermissionResult(
            behavior="ask",
            message=detail,
            reason=PermissionDecisionReason(type="complex_command", detail=detail),
        )

    if matched["allow"]:
        detail = _("matched allow rule(s): {}").format(", ".join(matched["allow"]))
        return PermissionResult(
            behavior="allow",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
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

    allow_flat = _collect_all_rules(context.allow_rules)
    deny_flat = _collect_all_rules(context.deny_rules)
    ask_flat = _collect_all_rules(context.ask_rules)

    full_matches = find_matching_rules(command, allow_flat, deny_flat, ask_flat)
    if full_matches["deny"]:
        detail = _("matched deny rule(s) on full command: {}").format(", ".join(full_matches["deny"]))
        return PermissionResult(
            behavior="deny",
            message=detail,
            reason=PermissionDecisionReason(type="rule", detail=detail),
        )

    parsed: ParseResult = parse_command(command)
    if parsed.kind in ("too_complex", "parse_error"):
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
        return _with_suggestions_if_needed(PermissionResult(behavior="passthrough"), command)

    if len(subcommands) > _MAX_SUBCOMMANDS:
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
    if len(cd_bases) > 1:
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
    if cd_bases and has_git:
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
    sub_results = [bash_tool_check_permission(sc, context, compound_has_cd=compound_has_cd) for sc in subcommands]
    merged = _merge_results(sub_results)
    return _with_suggestions_if_needed(merged, command, commands=subcommands, sub_results=sub_results)
