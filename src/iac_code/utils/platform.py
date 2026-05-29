# src/iac_code/utils/platform.py
"""Platform detection and Git Bash discovery."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from iac_code.i18n import _

_cached_platform: PlatformInfo | None = None


class GitBashNotFoundError(RuntimeError):
    """Raised when Git for Windows bash.exe cannot be found."""


_WINGET_CMD = "    winget install --id Git.Git -e --source winget"

_NPMMIRROR_CMD = (
    "    $v=((Invoke-RestMethod https://registry.npmmirror.com/-/binary/git-for-windows/).name"
    "|?{$_ -match '^v\\d' -and $_ -notmatch 'rc'}|Sort -Desc)[0]; "
    "$f=((Invoke-RestMethod https://registry.npmmirror.com/-/binary/git-for-windows/$v).name"
    "|?{$_ -match '64-bit\\.exe$' -and $_ -notmatch 'Portable'}); "
    '$u="https://registry.npmmirror.com/-/binary/git-for-windows/$v$f"; '
    "Invoke-WebRequest $u -OutFile $env:TEMP\\$f; "
    "Start-Process $env:TEMP\\$f -ArgumentList '/SILENT /NORESTART' -Wait"
)


def _git_bash_hint() -> str:
    return (
        _("iac-code on Windows requires Git for Windows.")
        + "\n"
        + _("If installed but not on PATH, set IAC_CODE_GIT_BASH_PATH environment variable.")
        + "\n"
        + "\n"
        + _("To install:")
        + "\n"
        + "\n"
        + _("  Option 1 - winget (requires access to github.com):")
        + "\n"
        + _WINGET_CMD
        + "\n"
        + "\n"
        + _("  Option 2 - if you cannot reach github.com, run this to install via npmmirror:")
        + "\n"
        + "    iac-code install-git-bash"
    )


@dataclass(frozen=True)
class PlatformInfo:
    os_kind: Literal["Windows", "Linux", "macOS"] | str
    shell_path: str
    shell_name: Literal["bash", "sh"]

    @staticmethod
    def detect() -> PlatformInfo:
        global _cached_platform
        if _cached_platform is not None:
            return _cached_platform

        if sys.platform == "win32":
            shell_path = _find_git_bash_path()
            result = PlatformInfo(os_kind="Windows", shell_path=shell_path, shell_name="bash")
        else:
            os_kind = "macOS" if sys.platform == "darwin" else sys.platform.capitalize()
            shell_path = _find_unix_shell()
            shell_name: Literal["bash", "sh"] = "bash" if "bash" in shell_path else "sh"
            result = PlatformInfo(os_kind=os_kind, shell_path=shell_path, shell_name=shell_name)

        _cached_platform = result
        return result


def _find_git_bash_path() -> str:
    override = os.environ.get("IAC_CODE_GIT_BASH_PATH")
    if override and os.path.isfile(override):
        return override

    try:
        result = subprocess.run(
            ["where.exe", "git"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                git_path = line.strip()
                if git_path:
                    candidate = str(Path(git_path).parent.parent / "bin" / "bash.exe")
                    if os.path.isfile(candidate):
                        return candidate
    except (OSError, subprocess.TimeoutExpired):
        pass

    for candidate in [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate

    raise GitBashNotFoundError(_git_bash_hint())


def _find_unix_shell() -> str:
    for path in ["/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]:
        if os.path.isfile(path):
            return path
    return "/bin/sh"


def _clear_cache() -> None:
    """Reset cached platform info.

    Called by `install_git_bash` after running the installer to force
    a fresh detection on the subsequent `_find_git_bash_path()` call.
    Also used by tests to isolate detect() runs.
    """
    global _cached_platform
    _cached_platform = None


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and all its descendants.

    Windows: uses ``taskkill /PID /T /F`` to kill the process tree.
    Unix: sends SIGKILL to the process group (caller must have created the
    process with ``start_new_session=True``).
    Falls back to ``proc.kill()`` on any failure.
    """
    pid = proc.pid
    if pid is None:
        return

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if result.returncode != 0:
                proc.kill()
        else:
            os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def normalize_user_path(raw: str) -> str:
    """Normalize a model-supplied path string to a native form.

    On Windows, convert MSYS/Cygwin POSIX-style paths (/c/..., /cygdrive/...,
    //server/share) to Windows native form via posix_path_to_windows. Pure
    relative paths and already-Windows paths pass through unchanged. On
    non-Windows, always passthrough.
    """
    from iac_code.utils.windows_paths import posix_path_to_windows

    if sys.platform != "win32":
        return raw
    if raw.startswith("//"):
        return posix_path_to_windows(raw)
    if raw.startswith("/cygdrive/"):
        return posix_path_to_windows(raw)
    if len(raw) >= 2 and raw[0] == "/" and raw[1].isalpha() and (len(raw) == 2 or raw[2] == "/"):
        return posix_path_to_windows(raw)
    return raw
