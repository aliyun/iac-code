"""Desktop adapter for the optional Git for Windows installation flow."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from iac_code.services.git_bash import GitBashInstallError, install_git_bash
from iac_code.utils.platform import GitBashNotFoundError, _clear_cache, _find_git_bash_path


def inspect_git_bash() -> dict[str, Any]:
    """Return the narrow startup-prerequisite status used by Desktop UI."""
    if sys.platform != "win32":
        return {"status": "not_required", "path": None}
    try:
        path = _find_git_bash_path()
    except GitBashNotFoundError:
        return {"status": "unavailable", "path": None}
    return {"status": "available", "path": path}


def install_git_bash_for_desktop() -> dict[str, Any]:
    """Install Git Bash after explicit Desktop user consent and re-detect it."""
    if sys.platform != "win32":
        return {"status": "not_required", "path": None}

    from iac_code.desktop.external_env import run_external, spawn_env_kwargs

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return run_external(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            **spawn_env_kwargs(),
            **kwargs,
        )

    path = install_git_bash(run=run, find=_find_git_bash_path, clear_cache=_clear_cache)
    return {"status": "available", "path": path}


__all__ = ["GitBashInstallError", "inspect_git_bash", "install_git_bash_for_desktop"]
