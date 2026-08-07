"""Contained workers for the two Desktop-only read-side probes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import signal
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from iac_code.desktop.external_env import create_subprocess_exec, guarded_command, spawn_env
from iac_code.desktop.runtime import DesktopInstallContext, DesktopRuntimeConfig

_MAX_RESULT_BYTES = 4 * 1024 * 1024
_CLEANUP_RESERVE_SECONDS = 1.0
_BACKGROUND_REAP_SECONDS = 3.0
_REAPERS: set[asyncio.Task[None]] = set()
_WINDOWS_PROBE_JOB: int | None = None
_WINDOWS_PROBE_JOB_LOCK = threading.Lock()


def _windows_probe_runtime() -> bool:
    return os.name == "nt"


def _create_windows_probe_job() -> int:
    """Put this worker in a nested kill-on-close Job before running probes."""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(getattr(ctypes, "get_last_error")(), "CreateJobObjectW failed")
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(information), ctypes.sizeof(information)):
        error = getattr(ctypes, "get_last_error")()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = getattr(ctypes, "get_last_error")()
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return int(job)


def initialize_windows_probe_job() -> None:
    global _WINDOWS_PROBE_JOB

    if not _windows_probe_runtime() or _WINDOWS_PROBE_JOB is not None:
        return
    with _WINDOWS_PROBE_JOB_LOCK:
        if _WINDOWS_PROBE_JOB is None:
            # Deliberately retain the handle for the worker lifetime. Every
            # version child inherits Job membership; process exit closes the
            # final handle and kills any descendant that outlived the probe.
            _WINDOWS_PROBE_JOB = _create_windows_probe_job()


def _config(payload: dict[str, Any]) -> DesktopRuntimeConfig:
    install = payload["installContext"]
    return DesktopRuntimeConfig(
        default_project_cwd=Path(payload["project"]),
        distribution_channel=payload["distributionChannel"],
        update_mode=payload["updateMode"],
        install_context=DesktopInstallContext(
            install_id=install["installId"],
            runtime_dir=Path(install["runtimeDir"]),
            host_state_dir=Path(install["hostStateDir"]),
            install_lock_dir=Path(install["installLockDir"]),
            sidecar_generation=int(install.get("sidecarGeneration") or 0),
            host_capture_path=Path(install["hostCapturePath"]) if install.get("hostCapturePath") else None,
            python_log_path=Path(install["pythonLogPath"]) if install.get("pythonLogPath") else None,
            degraded_prerequisites=tuple(install.get("degradedPrerequisites") or ()),
        ),
    )


def _execute(
    kind: str,
    payload: dict[str, Any],
    *,
    tool_progress=None,
) -> dict[str, Any]:
    if kind == "diagnostics":
        from iac_code.desktop.diagnostics import collect_desktop_diagnostics

        config = _config(payload)
        return collect_desktop_diagnostics(
            config,
            Path(payload["project"]),
            tool_progress=tool_progress,
        )
    if kind == "prerequisite":
        config = _config(payload)
        from iac_code.desktop.download_journal import DesktopTransactionReader

        installed_name = "infraguard.exe" if os.name == "nt" else "infraguard"
        try:
            with DesktopTransactionReader(config.install_context, Path.home() / "bin" / installed_name) as reader:
                if reader.recovery_required():
                    return {
                        "name": "infraguard",
                        "satisfied": False,
                        "status": "recovery_required",
                        "installable": True,
                    }
                from iac_code.web.pipeline_prerequisites import _inspect_review_step_prerequisite

                return _inspect_review_step_prerequisite()
        except TimeoutError:
            return {
                "name": "infraguard",
                "satisfied": False,
                "status": "installing",
                "installable": False,
            }
    raise ValueError("unsupported Desktop probe kind")


def worker_main(kind: str, raw_context: str) -> int:
    try:
        initialize_windows_probe_job()
        payload = json.loads(raw_context)
        os.environ["IAC_CODE_DESKTOP_PROBE_CONTAINER"] = "1"
        deadline = payload.get("hardDeadlineMonotonic")
        if isinstance(deadline, (float, int)):
            os.environ["IAC_CODE_DESKTOP_PROBE_DEADLINE"] = str(float(deadline))

        def write_frame(frame: dict[str, Any]) -> None:
            encoded = json.dumps(frame, ensure_ascii=False).encode("utf-8")
            if len(encoded) > _MAX_RESULT_BYTES:
                raise ValueError("Desktop probe result is too large")
            sys.stdout.buffer.write(struct.pack(">I", len(encoded)))
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()

        def tool_progress(name: str, value: dict[str, Any]) -> None:
            write_frame({"type": "progress", "tool": name, "value": value})

        result = _execute(kind, payload, tool_progress=tool_progress if kind == "diagnostics" else None)
        write_frame({"type": "result", "result": result})
        return 0
    except BaseException as error:
        print(str(error), file=sys.stderr)
        return 1


def _payload(config: DesktopRuntimeConfig, current_project: Path, deadline: float) -> dict[str, Any]:
    context = config.install_context
    return {
        "project": str(current_project),
        "distributionChannel": config.distribution_channel,
        "updateMode": config.update_mode,
        "hardDeadlineMonotonic": deadline,
        "installContext": {
            "installId": context.install_id,
            "runtimeDir": str(context.runtime_dir),
            "hostStateDir": str(context.host_state_dir),
            "installLockDir": str(context.install_lock_dir),
            "sidecarGeneration": context.sidecar_generation,
            "hostCapturePath": str(context.host_capture_path) if context.host_capture_path else None,
            "pythonLogPath": str(context.python_log_path) if context.python_log_path else None,
            "degradedPrerequisites": list(context.degraded_prerequisites),
        },
    }


def _command(kind: str, context: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--desktop-probe-worker", kind, context]
    return [sys.executable, "-m", "iac_code.desktop.probe_worker", "--kind", kind, "--context", context]


async def run_desktop_probe(
    kind: str,
    config: DesktopRuntimeConfig,
    current_project: Path,
    *,
    timeout: float,
) -> dict[str, Any]:
    if timeout <= _CLEANUP_RESERVE_SECONDS:
        raise ValueError("Desktop probe timeout must leave cleanup time")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    context = json.dumps(_payload(config, current_project, deadline), separators=(",", ":"))
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    raw_command = _command(kind, context)
    guardian_kind = "prerequisite-probe" if kind == "prerequisite" else "desktop-diagnostics"
    command = guarded_command(raw_command, kind=guardian_kind)
    guarded = command != raw_command
    environment = spawn_env()
    if environment is not None:
        environment["IAC_CODE_DESKTOP_PROBE_CONTAINER"] = "1"
        environment["IAC_CODE_DESKTOP_PROBE_DEADLINE"] = str(deadline)
    process = await create_subprocess_exec(
        *command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=start_new_session,
        creationflags=creationflags,
        env=environment,
    )
    partial_result = _timeout_result(kind, config, current_project)
    assert process.stdout is not None and process.stderr is not None
    result_task = asyncio.create_task(_read_probe_result(process.stdout, partial_result))
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        execution_budget = max(0.0, deadline - loop.time() - _CLEANUP_RESERVE_SECONDS)
        value = await asyncio.wait_for(asyncio.shield(result_task), timeout=execution_budget)
    except (asyncio.TimeoutError, TimeoutError):
        await _terminate_probe_container(process, deadline=deadline, guarded=guarded)
        result_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(result_task, stderr_task, return_exceptions=True)
        return partial_result
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_terminate_probe_container(process, deadline=deadline, guarded=guarded))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
        result_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(result_task, stderr_task, return_exceptions=True)
        raise
    except BaseException:
        await asyncio.gather(
            _terminate_probe_container(process, deadline=deadline, guarded=guarded),
            return_exceptions=True,
        )
        result_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(result_task, stderr_task, return_exceptions=True)
        raise
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.0, deadline - loop.time()))
    except (asyncio.TimeoutError, TimeoutError):
        await _terminate_probe_container(process, deadline=deadline, guarded=guarded)
    stderr = await stderr_task
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace")[:1000] or "Desktop probe failed")
    return value


async def _read_probe_result(stream: asyncio.StreamReader, partial_result: dict[str, Any]) -> dict[str, Any]:
    while True:
        try:
            header = await stream.readexactly(4)
        except asyncio.IncompleteReadError as exc:
            raise RuntimeError("Desktop probe returned an incomplete frame") from exc
        size = struct.unpack(">I", header)[0]
        if size > _MAX_RESULT_BYTES:
            raise RuntimeError("Desktop probe returned an invalid frame")
        try:
            encoded = await stream.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise RuntimeError("Desktop probe returned an incomplete frame") from exc
        frame = json.loads(encoded)
        if not isinstance(frame, dict):
            raise RuntimeError("Desktop probe frame must be an object")
        if frame.get("type") == "progress":
            tool = frame.get("tool")
            value = frame.get("value")
            if isinstance(tool, str) and isinstance(value, dict):
                tools = partial_result.get("tools")
                if isinstance(tools, dict) and tool in tools:
                    tools[tool] = value
            continue
        if frame.get("type") == "result" and isinstance(frame.get("result"), dict):
            return frame["result"]
        raise RuntimeError("Desktop probe returned an invalid frame")


def _timeout_result(kind: str, config: DesktopRuntimeConfig, current_project: Path) -> dict[str, Any]:
    if kind == "prerequisite":
        return {
            "name": "infraguard",
            "satisfied": False,
            "status": "timeout",
            "installable": True,
        }
    context = config.install_context
    raw_config_dir = os.path.expandvars(os.path.expanduser(os.environ.get("IAC_CODE_CONFIG_DIR", "~/.iac-code")))
    return {
        "runtime": "desktop",
        "distributionChannel": config.distribution_channel,
        "updateMode": config.update_mode,
        "paths": {
            "config": str(Path(raw_config_dir).resolve()),
            "project": str(current_project),
            "runtime": str(context.runtime_dir),
            "hostState": str(context.host_state_dir),
            "installLocks": str(context.install_lock_dir),
            "hostCapture": str(context.host_capture_path) if context.host_capture_path else None,
            "pythonLog": str(context.python_log_path) if context.python_log_path else None,
        },
        "platform": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "shell": None,
            "shellStatus": "timeout",
        },
        "tools": {
            name: {"status": "timeout", "path": None, "version": None}
            for name in ("git", "terraform", "node", "npm", "npx", "infraguard")
        },
        "qwenpaw": {"path": None, "source": None, "status": "timeout"},
        "degradedPrerequisites": list(context.degraded_prerequisites),
    }


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - asyncio.get_running_loop().time())


async def _wait_until(process: asyncio.subprocess.Process, deadline: float, *, cap: float | None = None) -> bool:
    if process.returncode is not None:
        return True
    remaining = _remaining(deadline)
    if cap is not None:
        remaining = min(remaining, cap)
    if remaining <= 0:
        return False
    try:
        await asyncio.wait_for(process.wait(), timeout=remaining)
        return True
    except (asyncio.TimeoutError, TimeoutError):
        return process.returncode is not None


async def _taskkill_tree(pid: int, deadline: float) -> None:
    if _remaining(deadline) <= 0:
        return
    try:
        killer = await create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return
    try:
        await asyncio.wait_for(killer.wait(), timeout=_remaining(deadline))
    except (asyncio.TimeoutError, TimeoutError):
        try:
            killer.kill()
        except (OSError, ProcessLookupError):
            pass


async def _bounded_reap(process: asyncio.subprocess.Process, *, guarded: bool, windows: bool) -> None:
    deadline = asyncio.get_running_loop().time() + _BACKGROUND_REAP_SECONDS
    if windows:
        await _taskkill_tree(process.pid, deadline)
        if process.returncode is None:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
    elif process.returncode is None:
        try:
            if guarded:
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await _wait_until(process, deadline)


def _handoff_reaper(process: asyncio.subprocess.Process, *, guarded: bool, windows: bool) -> None:
    task = asyncio.create_task(_bounded_reap(process, guarded=guarded, windows=windows))
    _REAPERS.add(task)
    task.add_done_callback(_REAPERS.discard)


async def _terminate_probe_container(
    process: asyncio.subprocess.Process,
    *,
    deadline: float,
    guarded: bool,
) -> None:
    windows = os.name == "nt"
    if process.returncode is not None and (windows or guarded):
        return
    if windows:
        await _taskkill_tree(process.pid, deadline)
    else:
        try:
            if guarded:
                # The proxy owns the guardian control writer. Requesting a
                # drain preserves guardian ownership of the whole PGID;
                # signalling ``process.pid`` would only target the proxied
                # worker and can race descendant cleanup.
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    leader_exited = await _wait_until(process, deadline, cap=0.75)
    if not windows and not guarded:
        # The worker can exit on SIGTERM while a version-command descendant
        # deliberately ignores it. The process group remains owned until its
        # final member exits, so always complete the TERM→KILL sequence even
        # when the group leader has already been reaped.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if leader_exited:
            return
    elif leader_exited:
        return
    if windows:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    elif guarded:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    elif not guarded:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not await _wait_until(process, deadline):
        _handoff_reaper(process, guarded=guarded, windows=windows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("diagnostics", "prerequisite"), required=True)
    parser.add_argument("--context", required=True)
    args = parser.parse_args(argv)
    from iac_code.desktop.external_env import initialize_windows_native_preload

    initialize_windows_native_preload()
    return worker_main(args.kind, args.context)


if __name__ == "__main__":
    raise SystemExit(main())
