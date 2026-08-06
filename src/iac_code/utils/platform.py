# src/iac_code/utils/platform.py
"""Platform detection and Git Bash discovery."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from iac_code.i18n import _

_cached_platform: PlatformInfo | None = None
_desktop_cleanup_tasks: set[asyncio.Task[bool]] = set()


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
    def detect(*, desktop_deadline_monotonic: float | None = None) -> PlatformInfo:
        global _cached_platform
        if _cached_platform is not None:
            return _cached_platform

        if sys.platform == "win32":
            shell_path = _find_git_bash_path(desktop_deadline_monotonic=desktop_deadline_monotonic)
            result = PlatformInfo(os_kind="Windows", shell_path=shell_path, shell_name="bash")
        else:
            os_kind = "macOS" if sys.platform == "darwin" else sys.platform.capitalize()
            shell_path = _find_unix_shell()
            shell_name: Literal["bash", "sh"] = "bash" if "bash" in shell_path else "sh"
            result = PlatformInfo(os_kind=os_kind, shell_path=shell_path, shell_name=shell_name)

        _cached_platform = result
        return result


def _find_git_bash_path(*, desktop_deadline_monotonic: float | None = None) -> str:
    override = os.environ.get("IAC_CODE_GIT_BASH_PATH")
    if override and os.path.isfile(override):
        return override

    timeout = 5.0
    if desktop_deadline_monotonic is not None:
        remaining = desktop_deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Desktop diagnostics deadline expired")
        timeout = min(timeout, remaining)
    try:
        from iac_code.desktop.external_env import run_external

        result = run_external(
            ["where.exe", "git"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                git_path = line.strip()
                if git_path:
                    candidate = str(Path(git_path).parent.parent / "bin" / "bash.exe")
                    if os.path.isfile(candidate):
                        return candidate
    except subprocess.TimeoutExpired:
        if desktop_deadline_monotonic is not None and time.monotonic() >= desktop_deadline_monotonic:
            raise TimeoutError("Desktop diagnostics deadline expired") from None
    except OSError:
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
            from iac_code.desktop.external_env import is_desktop_runtime

            if is_desktop_runtime():
                try:
                    receipt = _submit_desktop_taskkill(pid)
                except OSError:
                    proc.kill()
                    return
                cleanup = asyncio.create_task(_desktop_taskkill_tree(receipt))
                try:
                    succeeded = await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # The cleanup ticket is already irrevocably submitted. Keep
                    # a strong reference so a second caller cancellation cannot
                    # drop taskkill before it reaps the process tree.
                    _desktop_cleanup_tasks.add(cleanup)
                    cleanup.add_done_callback(_desktop_cleanup_tasks.discard)
                    raise
                if not succeeded:
                    proc.kill()
                return
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=5
            )
            if result.returncode != 0:
                proc.kill()
        else:
            from iac_code.desktop.external_env import is_desktop_runtime

            if is_desktop_runtime():
                # Guardian proxies own DRAIN and Host completion. Never signal a
                # bare target PID/PGID from the Desktop cancellation path.
                proc.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=3)
                except (asyncio.TimeoutError, TimeoutError):
                    proc.kill()
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=2)
                return
            os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired, ProcessLookupError, asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _submit_desktop_taskkill(pid: int):
    from iac_code.desktop.external_env import popen_external, submit_windows_spawn

    return submit_windows_spawn(
        lambda: popen_external(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ),
        cleanup=True,
    )


async def _desktop_taskkill_tree(receipt) -> bool:
    try:
        if receipt is None:
            return False
        process = await asyncio.to_thread(receipt.wait)
        try:
            await asyncio.wait_for(asyncio.to_thread(process.communicate), timeout=5)
        except (asyncio.TimeoutError, TimeoutError, subprocess.TimeoutExpired):
            process.kill()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=0.5)
            except (asyncio.TimeoutError, TimeoutError, subprocess.TimeoutExpired):
                return False
        return process.returncode == 0
    except (OSError, subprocess.SubprocessError, asyncio.TimeoutError, TimeoutError):
        return False


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
