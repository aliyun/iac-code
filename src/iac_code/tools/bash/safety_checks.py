"""Sensitive path detection and lightweight command-string safety checks."""

from __future__ import annotations

import os
import re
import sys
from typing import TYPE_CHECKING

from iac_code.i18n import _
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult

if TYPE_CHECKING:
    from iac_code.tools.bash.command_parser import SimpleCommand

SENSITIVE_PATHS = [
    ".git/",
    ".git",
    ".iac-code/",
    ".iac-code",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".ssh/",
    ".ssh",
    ".env",
    ".aliyun/",
    ".aliyun",
    ".alibabacloud/",
    ".alibabacloud",
    ".aws/credentials",
]

if sys.platform == "win32":
    SENSITIVE_PATHS.extend(
        [
            "AppData/Roaming/Microsoft/Windows/PowerShell",
            "AppData/Local/Microsoft/Credentials",
            "ntuser.dat",
        ]
    )

_WRITE_COMMANDS = frozenset({"rm", "mv", "cp", "mkdir", "rmdir", "touch", "chmod", "chown", "ln"})
_REDIRECT_TARGET = re.compile(r"^(?:>>|>)\s*(.+)$")


def _build_sensitive_lookups() -> tuple[frozenset[str], tuple[str, ...]]:
    single: set[str] = set()
    multi: list[str] = []
    for entry in SENSITIVE_PATHS:
        cleaned = entry.rstrip("/")
        if not cleaned:
            continue
        if "/" in cleaned:
            multi.append(cleaned)
        else:
            single.add(cleaned)
    return frozenset(single), tuple(multi)


_SENSITIVE_SINGLE, _SENSITIVE_MULTI = _build_sensitive_lookups()


def _path_hits_sensitive(abs_norm: str) -> bool:
    normalized = abs_norm.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in _SENSITIVE_SINGLE for part in parts):
        return True
    return any(sub in normalized for sub in _SENSITIVE_MULTI)


def _resolve_for_check(token: str, cwd: str) -> str:
    if os.path.isabs(token):
        return os.path.realpath(token)
    return os.path.realpath(os.path.join(cwd, token))


def _strip_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _redirect_targets(redirects: list[str]) -> list[str]:
    out: list[str] = []
    for raw in redirects:
        m = _REDIRECT_TARGET.match(raw.strip())
        if m:
            out.append(_strip_outer_quotes(m.group(1).strip()))
    return out


def _positional_paths_after_flags(argv: list[str]) -> list[str]:
    """Collect argv tokens that are likely paths (positional args), skipping flags until `--`."""
    if len(argv) < 2:
        return []
    paths: list[str] = []
    args = argv[1:]
    seen_dd = False
    i = 0
    while i < len(args):
        arg = args[i]
        if seen_dd:
            paths.append(arg)
            i += 1
            continue
        if arg == "--":
            seen_dd = True
            i += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            i += 1
            continue
        paths.append(arg)
        i += 1
    return paths


def _chmod_chown_paths(argv: list[str]) -> list[str]:
    """Paths for chmod/chown: non-flag args excluding obvious mode/user tokens."""
    raw = _positional_paths_after_flags(argv)
    paths: list[str] = []
    for tok in raw:
        if tok.isdigit() and len(tok) <= 4:
            continue
        if ":" in tok and "/" not in tok and not tok.startswith("."):
            continue
        paths.append(tok)
    return paths


def _argv_paths_for_safety(argv: list[str]) -> list[str]:
    if not argv:
        return []
    base = os.path.basename(argv[0])
    if base not in _WRITE_COMMANDS:
        return []
    if base in {"chmod", "chown"}:
        return _chmod_chown_paths(argv)
    return _positional_paths_after_flags(argv)


def check_safety(cmd: SimpleCommand, cwd: str) -> PermissionResult:
    """Check for writes to sensitive paths. Returns passthrough if safe, ask if sensitive."""
    tokens = list(dict.fromkeys(_argv_paths_for_safety(cmd.argv) + _redirect_targets(cmd.redirects)))
    for tok in tokens:
        resolved = _resolve_for_check(tok, cwd)
        norm = resolved.replace("\\", "/")
        if _path_hits_sensitive(norm):
            detail = _("operation touches a sensitive path: {}").format(tok)
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="safety_check", detail=detail),
            )
    return PermissionResult(behavior="passthrough")


def _quotes_balanced(command: str) -> bool:
    i = 0
    in_single = False
    in_double = False
    escape = False
    while i < len(command):
        c = command[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == "\\":
            escape = True
            i += 1
            continue
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
        elif c == '"':
            in_double = True
        i += 1
    return not in_single and not in_double


def _has_disallowed_control_chars(command: str) -> bool:
    for c in command:
        o = ord(c)
        if o == 0:
            return True
        if o <= 8 or (14 <= o <= 31) or o == 127:
            return True
    return False


def check_command_safety(command: str) -> bool:
    """Basic injection detection. Returns True if command looks safe, False if suspicious."""
    if "\x00" in command:
        return False
    if _has_disallowed_control_chars(command):
        return False
    if not _quotes_balanced(command):
        return False
    return True
