"""Validate command paths and redirect targets against workspace boundaries."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Literal

from iac_code.i18n import _
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult
from iac_code.utils.platform import normalize_user_path

if TYPE_CHECKING:
    from iac_code.tools.bash.command_parser import SimpleCommand

_PATH_COMMANDS = frozenset({"cp", "mv", "rm", "mkdir", "rmdir", "ln", "install"})
_REDIRECT_TARGET = re.compile(r"^(?:>>|>)\s*(.+)$")
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
    path = normalize_user_path(path)
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path))


def _strip_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _redirect_paths(redirects: list[str]) -> list[str]:
    paths: list[str] = []
    for raw in redirects:
        line = raw.strip()
        m = _REDIRECT_TARGET.match(line)
        if not m:
            continue
        paths.append(_strip_outer_quotes(m.group(1).strip()))
    return paths


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
        if arg.startswith("-") and len(arg) > 1:
            i += 1
            continue
        paths.append(arg)
        i += 1
    return paths


def check_path_constraints(cmd: SimpleCommand, cwd: str, additional_directories: list[str]) -> PermissionResult:
    """Validate paths in redirects and command arguments. Returns passthrough if no paths to check."""
    redirect_paths = set(_redirect_paths(cmd.redirects))
    candidates = list(dict.fromkeys(_redirect_paths(cmd.redirects) + _argv_paths(cmd.argv)))
    if not candidates:
        return PermissionResult(behavior="passthrough")

    for rel_or_abs in candidates:
        if rel_or_abs in _POSIX_PSEUDO_DEVICES and rel_or_abs in redirect_paths:
            continue
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
