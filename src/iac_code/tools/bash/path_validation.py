"""Validate command paths and redirect targets against workspace boundaries."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Literal

from iac_code.i18n import _
from iac_code.tools.bash.argv_safety import command_implicitly_reads_current_directory, extract_read_paths
from iac_code.tools.path_safety import check_read_path
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult
from iac_code.utils.platform import normalize_user_path

if TYPE_CHECKING:
    from iac_code.tools.bash.command_parser import SimpleCommand

_PATH_COMMANDS = frozenset({"cp", "mv", "rm", "mkdir", "rmdir", "ln", "install"})
_PATH_VALUE_OPTIONS_BY_COMMAND = {
    "cp": frozenset({"-t", "--target-directory"}),
    "mv": frozenset({"-t", "--target-directory"}),
    "ln": frozenset({"-t", "--target-directory"}),
    "install": frozenset({"-t", "--target-directory"}),
}
_WRITE_REDIRECT_TARGET = re.compile(r"^(?:\d*)(?:>>?|>\||<>)\s*(.+)$")
_READ_REDIRECT_TARGET = re.compile(r"^(?:\d*)(?:<>|<)\s*(.+)$")
_POSIX_PSEUDO_DEVICES = frozenset(
    {
        "/dev/null",
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
)


def validate_path(resolved_path: str, cwd: str, additional_directories: list[str]) -> Literal["allow", "deny"]:
    """Check if a path is within cwd or additional allowed directories. Uses os.path.realpath for resolution."""
    path_r = os.path.realpath(resolved_path)
    allowed_roots = [os.path.realpath(cwd), *[os.path.realpath(d) for d in additional_directories]]
    for root in allowed_roots:
        if path_r == root:
            return "allow"
        if path_r.startswith(root + os.sep):
            return "allow"
    return "deny"


def _resolve_candidate(path: str, cwd: str) -> str:
    path = os.path.expanduser(normalize_user_path(path))
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path))


def _strip_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _write_redirect_paths(redirects: list[str]) -> list[str]:
    paths: list[str] = []
    for raw in redirects:
        line = raw.strip()
        m = _WRITE_REDIRECT_TARGET.match(line)
        if not m:
            continue
        paths.append(_strip_outer_quotes(m.group(1).strip()))
    return paths


def _read_redirect_paths(redirects: list[str]) -> list[str]:
    paths: list[str] = []
    for raw in redirects:
        line = raw.strip()
        if re.match(r"^(?:\d*)<<<?", line) or re.match(r"^(?:\d*)<&", line):
            continue
        m = _READ_REDIRECT_TARGET.match(line)
        if not m:
            continue
        paths.append(_strip_outer_quotes(m.group(1).strip()))
    return paths


def _has_unresolved_shell_expansion(path: str) -> bool:
    return "$" in path or "`" in path


def _argv_paths(argv: list[str]) -> list[str]:
    if not argv:
        return []
    base = os.path.basename(argv[0])
    if base not in _PATH_COMMANDS:
        return []
    paths: list[str] = []
    args = argv[1:]
    seen_double_dash = False
    i = 0
    while i < len(args):
        arg = args[i]
        if seen_double_dash:
            paths.append(arg)
            i += 1
            continue
        if arg == "--":
            seen_double_dash = True
            i += 1
            continue
        path_option_value, next_i = _path_value_option(base, arg, args, i)
        if path_option_value is not None:
            paths.append(path_option_value)
            i = next_i
            continue
        if arg.startswith("-") and len(arg) > 1:
            i += 1
            continue
        paths.append(arg)
        i += 1
    return paths


def _path_value_option(base: str, arg: str, args: list[str], index: int) -> tuple[str | None, int]:
    options = _PATH_VALUE_OPTIONS_BY_COMMAND.get(base, frozenset())
    if not options:
        return None, index + 1

    short_options = [option[1:] for option in options if option.startswith("-") and not option.startswith("--")]
    for option in options:
        if option.startswith("--"):
            if arg == option:
                if index + 1 < len(args):
                    return args[index + 1], index + 2
                return None, index + 1
            if arg.startswith("{}=".format(option)):
                return arg.split("=", 1)[1], index + 1
            continue

        if arg == option:
            if index + 1 < len(args):
                return args[index + 1], index + 2
            return None, index + 1
        if arg.startswith(option) and len(arg) > len(option):
            return arg[len(option) :], index + 1

    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
        cluster = arg[1:]
        for option in short_options:
            option_index = cluster.find(option)
            if option_index == -1:
                continue
            attached_value = cluster[option_index + len(option) :]
            if attached_value:
                return attached_value, index + 1
            if index + 1 < len(args):
                return args[index + 1], index + 2
            return None, index + 1

    return None, index + 1


def check_path_constraints(cmd: SimpleCommand, cwd: str, additional_directories: list[str]) -> PermissionResult:
    """Validate paths in redirects and command arguments. Returns passthrough if no paths to check."""
    redirect_paths = set(_write_redirect_paths(cmd.redirects))
    candidates = list(dict.fromkeys(_write_redirect_paths(cmd.redirects) + _argv_paths(cmd.argv)))
    if not candidates:
        return PermissionResult(behavior="passthrough")

    for rel_or_abs in candidates:
        if rel_or_abs in _POSIX_PSEUDO_DEVICES and rel_or_abs in redirect_paths:
            continue
        if _has_unresolved_shell_expansion(rel_or_abs):
            detail = _("path uses shell expansion: {}").format(rel_or_abs)
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="path_constraint", detail=detail),
            )
        resolved = _resolve_candidate(rel_or_abs, cwd)
        decision = validate_path(resolved, cwd, additional_directories)
        if decision == "deny":
            detail = _("path outside allowed directories: {}").format(rel_or_abs)
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="path_constraint", detail=detail),
            )

    return PermissionResult(behavior="passthrough")


def check_read_path_constraints(
    cmd: SimpleCommand,
    cwd: str,
    additional_directories: list[str],
    trusted_read_directories: list[str],
    compound_has_cd: bool = False,
) -> PermissionResult:
    """Validate read path operands for commands that inspect file contents."""
    candidates = list(dict.fromkeys(_read_redirect_paths(cmd.redirects) + extract_read_paths(cmd.argv)))
    if not candidates:
        if compound_has_cd and command_implicitly_reads_current_directory(cmd.argv):
            detail = _("read path after cd requires confirmation: current directory")
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="path_constraint", detail=detail),
            )
        return PermissionResult(behavior="passthrough")

    if compound_has_cd:
        for rel_or_abs in candidates:
            if _is_relative_read_path(rel_or_abs):
                detail = _("read path after cd requires confirmation: {}").format(rel_or_abs)
                return PermissionResult(
                    behavior="ask",
                    message=detail,
                    reason=PermissionDecisionReason(type="path_constraint", detail=detail),
                )

    for rel_or_abs in candidates:
        if _has_unresolved_shell_expansion(rel_or_abs):
            detail = _("read path uses shell expansion: {}").format(rel_or_abs)
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="path_constraint", detail=detail),
            )
        decision = check_read_path(
            rel_or_abs,
            cwd=cwd,
            additional_directories=additional_directories,
            trusted_read_directories=trusted_read_directories,
        )
        if decision.behavior == "ask":
            return decision.to_permission_result()

    return PermissionResult(behavior="passthrough")


def _is_relative_read_path(path: str) -> bool:
    normalized = normalize_user_path(_strip_outer_quotes(path.strip()))
    return not os.path.isabs(normalized)
