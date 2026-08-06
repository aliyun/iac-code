from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from iac_code.desktop.probe_worker import _payload, initialize_windows_probe_job, run_desktop_probe
from iac_code.desktop.runtime import DesktopInstallContext, DesktopRuntimeConfig


def _config(tmp_path: Path) -> DesktopRuntimeConfig:
    return DesktopRuntimeConfig(
        default_project_cwd=tmp_path,
        distribution_channel="development",
        update_mode="external",
        install_context=DesktopInstallContext(
            install_id="test-install",
            runtime_dir=tmp_path,
            host_state_dir=tmp_path / "state",
            install_lock_dir=tmp_path / "locks",
            sidecar_generation=1,
        ),
    )


def test_probe_payload_carries_one_absolute_deadline(tmp_path: Path) -> None:
    payload = _payload(_config(tmp_path), tmp_path, 123.5)

    assert payload["hardDeadlineMonotonic"] == 123.5


def test_windows_probe_job_is_created_once_before_work(monkeypatch) -> None:
    import iac_code.desktop.probe_worker as probe_worker

    created: list[int] = []
    monkeypatch.setattr(probe_worker, "_windows_probe_runtime", lambda: True)
    monkeypatch.setattr(probe_worker, "_WINDOWS_PROBE_JOB", None)
    monkeypatch.setattr(probe_worker, "_create_windows_probe_job", lambda: created.append(91) or 91)

    initialize_windows_probe_job()
    initialize_windows_probe_job()

    assert created == [91]
    assert probe_worker._WINDOWS_PROBE_JOB == 91


def test_windows_probe_job_declares_64_bit_safe_ctypes_signatures(monkeypatch) -> None:
    from ctypes import wintypes

    import iac_code.desktop.probe_worker as probe_worker

    class Function:
        def __init__(self, result) -> None:
            self.result = result
            self.argtypes = None
            self.restype = None
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.result

    create = Function(0x123456789ABC)
    configure = Function(1)
    current = Function(0x23456789ABCD)
    assign = Function(1)
    close = Function(1)

    class Kernel32:
        CreateJobObjectW = create
        SetInformationJobObject = configure
        GetCurrentProcess = current
        AssignProcessToJobObject = assign
        CloseHandle = close

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel32(), raising=False)

    assert probe_worker._create_windows_probe_job() == 0x123456789ABC
    assert create.argtypes == [ctypes.c_void_p, wintypes.LPCWSTR]
    assert create.restype is wintypes.HANDLE
    assert configure.argtypes == [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    assert configure.restype is wintypes.BOOL
    assert current.argtypes == []
    assert current.restype is wintypes.HANDLE
    assert assign.argtypes == [wintypes.HANDLE, wintypes.HANDLE]
    assert assign.restype is wintypes.BOOL
    assert close.argtypes == [wintypes.HANDLE]
    assert close.restype is wintypes.BOOL
    assert assign.calls == [(0x123456789ABC, 0x23456789ABCD)]
    assert close.calls == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are exercised here")
async def test_probe_timeout_reaps_the_worker_process_group(tmp_path: Path, monkeypatch) -> None:
    import iac_code.desktop.probe_worker as probe_worker

    pid_path = tmp_path / "descendant.pid"
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    worker_code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)"
    )
    monkeypatch.delenv("IAC_CODE_DESKTOP_EXEC", raising=False)
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setattr(
        probe_worker,
        "_command",
        lambda kind, context: [sys.executable, "-c", worker_code, str(pid_path)],
    )

    result = await run_desktop_probe("diagnostics", _config(tmp_path), tmp_path, timeout=5.0)
    assert all(tool["status"] == "timeout" for tool in result["tools"].values())

    for _ in range(30):
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text(encoding="utf-8")), 0)
            except ProcessLookupError:
                break
        await asyncio.sleep(0.05)
    else:
        descendant = int(pid_path.read_text(encoding="utf-8"))
        os.kill(descendant, signal.SIGKILL)
        pytest.fail("Desktop probe descendant survived its bounded container cleanup")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX probe frame transport is exercised here")
async def test_diagnostics_timeout_preserves_completed_tool_progress(tmp_path: Path, monkeypatch) -> None:
    import iac_code.desktop.probe_worker as probe_worker

    completed = {
        "status": "available",
        "path": "/test/bin/git",
        "version": "git version test",
    }
    encoded = json.dumps({"type": "progress", "tool": "git", "value": completed}).encode("utf-8")
    worker_code = (
        "import struct,sys,time; "
        f"data={encoded!r}; "
        "sys.stdout.buffer.write(struct.pack('>I',len(data))+data); "
        "sys.stdout.buffer.flush(); time.sleep(30)"
    )
    monkeypatch.delenv("IAC_CODE_DESKTOP_EXEC", raising=False)
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setattr(
        probe_worker,
        "_command",
        lambda kind, context: [sys.executable, "-c", worker_code],
    )

    result = await run_desktop_probe("diagnostics", _config(tmp_path), tmp_path, timeout=1.5)

    assert result["tools"]["git"] == completed
    assert result["tools"]["terraform"]["status"] == "timeout"
