"""Git for Windows installation shared by CLI and Desktop adapters."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from iac_code.utils.platform import _NPMMIRROR_CMD, GitBashNotFoundError, _clear_cache, _find_git_bash_path

RunCommand = Callable[..., subprocess.CompletedProcess[Any]]
FindGitBash = Callable[[], str]


class GitBashInstallError(RuntimeError):
    """Raised when Git for Windows could not be installed or verified."""


def install_git_bash(
    *,
    run: RunCommand = subprocess.run,
    find: FindGitBash = _find_git_bash_path,
    clear_cache: Callable[[], None] = _clear_cache,
    check_existing: bool = True,
) -> str:
    """Install Git for Windows and return the verified ``bash.exe`` path.

    The caller owns presentation and user consent. CLI passes the normal
    ``subprocess.run`` implementation so progress remains attached to the
    terminal; Desktop supplies its external-process adapter.
    """
    if check_existing:
        try:
            existing = find()
        except GitBashNotFoundError:
            existing = None
        if existing:
            return existing

    try:
        result = run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _NPMMIRROR_CMD,
            ],
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitBashInstallError("powershell-not-found") from exc

    if result.returncode != 0:
        raise GitBashInstallError("installer-exit:{}".format(result.returncode))

    clear_cache()
    try:
        return find()
    except GitBashNotFoundError as exc:
        raise GitBashInstallError("installed-but-not-found") from exc
