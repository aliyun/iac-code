"""Read-only bash command classification for permission auto-allow."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from iac_code.tools.bash.argv_safety import basename, dangerous_readonly_argument, pip_like_base, sed_inplace_edit

if TYPE_CHECKING:
    from iac_code.tools.bash.command_parser import SimpleCommand

_READONLY_BASE_COMMANDS = frozenset(
    {
        "ls",
        "ll",
        "la",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "file",
        "stat",
        "du",
        "df",
        "tree",
        "realpath",
        "readlink",
        "basename",
        "dirname",
        "md5sum",
        "sha256sum",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "find",
        "fd",
        "ag",
        "ack",
        "locate",
        "which",
        "whereis",
        "type",
        "pwd",
        "env",
        "printenv",
        "echo",
        "printf",
        "whoami",
        "id",
        "hostname",
        "uname",
        "date",
        "uptime",
        "free",
        "ps",
        "top",
        "lsof",
        "netstat",
        "ss",
        "sort",
        "uniq",
        "cut",
        "tr",
        "diff",
        "comm",
        "jq",
        "yq",
        "true",
        "false",
        "test",
        "sed",
    }
)

_GIT_READONLY_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "tag",
        "remote",
        "describe",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "cat-file",
        "blame",
        "shortlog",
    }
)

_PIP_READONLY_VERBS = frozenset({"list", "show", "freeze"})
_NPM_READONLY_VERBS = frozenset({"list", "ls", "info", "view", "outdated"})
_YARN_READONLY_VERBS = frozenset({"list", "info", "why"})
_PNPM_READONLY_VERBS = frozenset({"list", "ls"})
_BREW_READONLY_VERBS = frozenset({"list", "info"})

_RE_GIT_STASH_LIST = re.compile(r"\bgit\b.*\bstash\b.*\blist\b")
_RE_GIT_CONFIG_GET = re.compile(r"\bgit\b.*\bconfig\b.*(?:--get\b|--list\b|\s-l\b)")


def _skip_git_global_options(argv: list[str], start: int) -> int:
    i = start
    while i < len(argv):
        arg = argv[i]
        if arg in {"-C", "--git-dir", "--work-tree"} and i + 1 < len(argv):
            i += 2
            continue
        if arg == "-c" and i + 1 < len(argv):
            i += 2
            continue
        if arg.startswith("-") and arg != "--":
            i += 1
            continue
        break
    return i


def _git_remaining_argv(argv: list[str]) -> list[str]:
    start = _skip_git_global_options(argv, 1)
    return argv[start:]


def _is_git_readonly(argv: list[str]) -> bool:
    remaining = _git_remaining_argv(argv)
    if not remaining:
        return False
    sub = remaining[0]
    if sub == "stash" and len(remaining) > 1 and remaining[1] == "list":
        return True
    if sub == "config":
        rest = remaining[1:]
        if "--get" in rest or "--list" in rest or "-l" in rest:
            return True
        return False
    return sub in _GIT_READONLY_SUBCOMMANDS


def _is_package_manager_readonly(argv: list[str]) -> bool:
    if len(argv) < 2:
        return False
    base = basename(argv[0])
    verb = argv[1]

    if pip_like_base(base) and verb in _PIP_READONLY_VERBS:
        return True

    if base == "uv" and len(argv) >= 3 and argv[1] == "pip" and argv[2] in _PIP_READONLY_VERBS:
        return True

    if base == "npm" and verb in _NPM_READONLY_VERBS:
        return True

    if base == "yarn" and verb in _YARN_READONLY_VERBS:
        return True

    if base == "pnpm" and verb in _PNPM_READONLY_VERBS:
        return True

    if base == "cargo" and verb == "metadata":
        return True

    if base == "go" and verb == "list":
        return True

    if base == "gem" and verb == "list":
        return True

    if base == "brew" and verb in _BREW_READONLY_VERBS:
        return True

    return False


def _regex_git_readonly(text: str) -> bool:
    return bool(_RE_GIT_STASH_LIST.search(text) or _RE_GIT_CONFIG_GET.search(text))


def is_command_readonly(cmd: SimpleCommand) -> bool:
    """Return True when ``cmd`` is classified as read-only (safe to auto-allow)."""
    if cmd.redirects:
        return False
    argv = cmd.argv
    if not argv:
        return False

    if argv[-1] in {"--version", "-V"}:
        return True

    base = basename(argv[0])

    if base == "command" and "-v" in argv[1:]:
        return True

    if _regex_git_readonly(cmd.text):
        return True

    if base == "sed" and sed_inplace_edit(argv):
        return False

    if dangerous_readonly_argument(argv) is not None:
        return False

    if base == "git" and _is_git_readonly(argv):
        return True

    if _is_package_manager_readonly(argv):
        return True

    return base in _READONLY_BASE_COMMANDS
