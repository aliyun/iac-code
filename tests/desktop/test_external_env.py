from __future__ import annotations

import asyncio
import ctypes
import io
import os
import socket
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import iac_code.desktop.external_env as external_env
from iac_code.desktop.control import DesktopControlDispatcher, FramedControl, install_control_dispatcher
from iac_code.desktop.external_env import guarded_command, spawn_env, spawn_env_kwargs


def test_default_runtime_preserves_subprocess_defaults(monkeypatch) -> None:
    monkeypatch.delenv("IAC_CODE_DESKTOP_RUNTIME", raising=False)

    assert spawn_env() is None
    assert spawn_env_kwargs() == {}
    assert guarded_command(["tool", "--version"]) == ["tool", "--version"]


def test_desktop_runtime_cleans_frozen_library_environment(monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    environment = {
        "PATH": "/usr/bin",
        "LD_LIBRARY_PATH": "/bundle",
        "LD_LIBRARY_PATH_ORIG": "/system",
        "DYLD_LIBRARY_PATH": "/bundle",
        "PYTHONHOME": "/bundle/python",
        "_MEIPASS2": "/bundle",
    }

    cleaned = spawn_env(environment)

    assert cleaned == {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/system"}


def test_posix_desktop_command_uses_fixed_guardian(monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setenv("IAC_CODE_DESKTOP_EXEC", "/app/iac-code-desktop-exec")

    command = guarded_command(["git", "status"])

    if os.name == "nt":
        assert command == ["git", "status"]
    else:
        assert command == [
            "/app/iac-code-desktop-exec",
            "--child-guardian",
            "--kind",
            "bash",
            "--",
            "git",
            "status",
        ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX guardian proxy contract")
def test_posix_guardian_proxy_registers_before_start_and_reports_target_status(monkeypatch, tmp_path: Path) -> None:
    helper = tmp_path / "guardian-helper"
    helper.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import subprocess
            import sys

            values = sys.argv[1:]
            control_fd = int(values[values.index("--control-fd") + 1])
            status_fd = int(values[values.index("--status-fd") + 1])
            command = values[values.index("--") + 1:]
            os.set_inheritable(control_fd, False)
            os.set_inheritable(status_fd, False)
            control = os.fdopen(control_fd, "rb", buffering=0)
            status = os.fdopen(status_fd, "wb", buffering=0)
            if control.readline() != b"START\\n":
                raise SystemExit(0)
            target = subprocess.Popen(command)
            status.write(("STARTED " + json.dumps({{"pid": target.pid}}) + "\\n").encode())
            status.flush()
            returncode = target.wait()
            wait_status = returncode << 8 if returncode >= 0 else -returncode
            status.write(("EXIT " + json.dumps({{"waitStatus": wait_status}}) + "\\n").encode())
            status.flush()
            control.readline()
            """
        ),
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setenv("IAC_CODE_DESKTOP_EXEC", str(helper))

    async def exercise() -> None:
        sidecar_socket, host_socket = socket.socketpair()
        sidecar = FramedControl(sidecar_socket.makefile("rwb", buffering=0))
        host = FramedControl(host_socket.makefile("rwb", buffering=0))
        dispatcher = DesktopControlDispatcher(sidecar, asyncio.get_running_loop(), 51)
        install_control_dispatcher(dispatcher)
        dispatcher.start()

        async def host_protocol() -> None:
            register = await asyncio.to_thread(host.read)
            assert register is not None and register["type"] == "register-child-group"
            host.write(
                {
                    "type": "child-group-registered",
                    "sidecarGeneration": 51,
                    "registrationId": register["registrationId"],
                }
            )
            complete = await asyncio.to_thread(host.read)
            assert complete is not None and complete["type"] == "complete-child-group"
            host.write(
                {
                    "type": "child-group-complete",
                    "sidecarGeneration": 51,
                    "registrationId": complete["registrationId"],
                }
            )

        host_task = asyncio.create_task(host_protocol())
        try:
            process = await external_env.create_subprocess_exec(
                *guarded_command(
                    [sys.executable, "-c", "import os,sys; print(os.getpid()); sys.exit(7)"],
                    kind="bash",
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            assert int(stdout) == process.pid
            assert stderr == b""
            assert process.returncode == 7
            await host_task
        finally:
            install_control_dispatcher(None)
            host.close()
            sidecar_socket.close()
            host_socket.close()

    asyncio.run(exercise())


def _mock_windows_runtime(monkeypatch) -> list[str | None]:
    dll_changes: list[str | None] = []
    monkeypatch.setattr(external_env, "_windows_desktop_runtime", lambda: True)
    monkeypatch.setattr(external_env, "_WINDOWS_PRELOAD_READY", False)
    monkeypatch.setattr(external_env, "_WINDOWS_DLL_DIRECTORY", None)
    monkeypatch.setattr(external_env, "_get_windows_dll_directory", lambda: "C:/bundle")
    monkeypatch.setattr(external_env, "_set_windows_dll_directory", dll_changes.append)
    return dll_changes


def test_windows_preload_imports_manifest_before_process_creation(monkeypatch, tmp_path: Path) -> None:
    dll_changes = _mock_windows_runtime(monkeypatch)
    imported: list[str] = []
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"modules":["native.alpha","native.beta","native.alpha"]}', encoding="utf-8")
    monkeypatch.setattr(external_env.importlib, "import_module", imported.append)

    external_env.initialize_windows_native_preload(manifest_path=manifest)
    received: dict[str, object] = {}

    def factory(command: str, **kwargs: object) -> object:
        received.update(command=command, **kwargs)
        return object()

    external_env.create_external_process(factory, "tool", creationflags=4)

    assert imported == ["native.alpha", "native.beta"]
    assert received == {"command": "tool", "creationflags": 4 | 0x08000000}
    assert dll_changes == [None, "C:/bundle"]


def test_windows_dll_directory_ctypes_signatures_preserve_wide_strings(monkeypatch) -> None:
    from ctypes import wintypes

    class Function:
        def __init__(self, implementation) -> None:
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    configured: list[str | None] = []

    def get_directory(size, buffer) -> int:
        value = "C:/frozen/运行时"
        if size == 0:
            return len(value)
        buffer.value = value
        return len(value)

    get = Function(get_directory)
    set_directory = Function(lambda value: configured.append(value) or 1)

    class Kernel32:
        GetDllDirectoryW = get
        SetDllDirectoryW = set_directory

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel32(), raising=False)

    assert external_env._get_windows_dll_directory() == "C:/frozen/运行时"
    external_env._set_windows_dll_directory("C:/frozen/运行时")
    external_env._set_windows_dll_directory(None)

    assert get.argtypes == [wintypes.DWORD, wintypes.LPWSTR]
    assert get.restype is wintypes.DWORD
    assert set_directory.argtypes == [wintypes.LPCWSTR]
    assert set_directory.restype is wintypes.BOOL
    assert configured == ["C:/frozen/运行时", None]


def test_windows_detached_opener_gets_breakaway_flags(monkeypatch) -> None:
    _mock_windows_runtime(monkeypatch)
    external_env.initialize_windows_native_preload(modules=())
    received: dict[str, object] = {}

    def factory(*args: object, **kwargs: object) -> object:
        received.update(kwargs)
        return object()

    monkeypatch.setattr(external_env.subprocess, "Popen", factory)
    external_env.popen_external(["explorer", "project"], detached=True)

    assert received["creationflags"] == 0x08000000 | 0x01000000 | 0x00000200


@pytest.mark.skipif(os.name == "nt", reason="POSIX detached-session behavior")
def test_posix_desktop_opener_starts_a_detached_session(monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    received: dict[str, object] = {}

    def factory(*args: object, **kwargs: object) -> object:
        received.update(kwargs)
        return object()

    monkeypatch.setattr(external_env.subprocess, "Popen", factory)
    external_env.popen_external(["open", "https://example.invalid"], detached=True)

    assert received["start_new_session"] is True


def test_windows_process_creation_is_blocked_until_preload(monkeypatch) -> None:
    _mock_windows_runtime(monkeypatch)

    with pytest.raises(RuntimeError, match="preload has not completed"):
        external_env.create_external_process(lambda: None)


@pytest.mark.asyncio
async def test_windows_async_creation_is_serialized_and_restores_dll_directory(monkeypatch) -> None:
    dll_changes = _mock_windows_runtime(monkeypatch)
    external_env.initialize_windows_native_preload(modules=())
    active = 0
    peak = 0

    async def factory(value: int, **kwargs: object) -> int:
        nonlocal active, peak
        assert kwargs["creationflags"] == 0x08000000
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return value

    assert await asyncio.gather(
        external_env.create_external_process_async(factory, 1),
        external_env.create_external_process_async(factory, 2),
    ) == [1, 2]
    assert peak == 1
    assert dll_changes == [None, "C:/bundle", None, "C:/bundle"]


@pytest.mark.asyncio
async def test_windows_sync_creation_rejects_event_loop_thread(monkeypatch) -> None:
    _mock_windows_runtime(monkeypatch)
    external_env.initialize_windows_native_preload(modules=())

    with pytest.raises(RuntimeError, match="event-loop thread"):
        external_env.create_external_process(lambda: None)


@pytest.mark.asyncio
async def test_windows_cancelled_creation_cleans_unclaimed_handle_after_restoring_dll(monkeypatch) -> None:
    events: list[str] = []
    _mock_windows_runtime(monkeypatch)
    monkeypatch.setattr(
        external_env,
        "_set_windows_dll_directory",
        lambda path: events.append("dll:none" if path is None else "dll:restore"),
    )
    external_env.initialize_windows_native_preload(modules=())
    started = asyncio.Event()
    release = asyncio.Event()

    class Process:
        def terminate(self) -> None:
            events.append("terminate")

        async def wait(self) -> None:
            events.append("wait")

    async def factory(**kwargs: object) -> Process:
        assert kwargs["creationflags"] == 0x08000000
        started.set()
        await release.wait()
        return Process()

    task = asyncio.create_task(external_env.create_external_process_async(factory))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["dll:none", "dll:restore", "terminate", "wait"]


def test_windows_sync_worker_prioritizes_spawn_while_acl_reaper_is_busy(monkeypatch, tmp_path: Path) -> None:
    worker = external_env._WindowsSyncWorker()
    acl_started = threading.Event()
    release_acl = threading.Event()

    class Process:
        returncode = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[bytes, bytes]:
            assert timeout == 5
            acl_started.set()
            assert release_acl.wait(2)
            return b"", b""

        def kill(self) -> None:  # pragma: no cover - timeout fallback
            release_acl.set()

    monkeypatch.setattr(external_env, "popen_external", lambda *args, **kwargs: Process())
    acl_receipt = worker.submit_acl(tmp_path / "settings.yml", directory=False, username="desktop-user")
    assert acl_started.wait(1)

    spawn_receipt = worker.submit_spawn(lambda: "created", cleanup=False)
    assert spawn_receipt._done.wait(1)
    assert spawn_receipt.wait() == "created"

    release_acl.set()
    assert acl_receipt._done.wait(1)
    assert acl_receipt.wait() == 0


def test_windows_sync_worker_coalesces_same_acl_path_without_dropping_receipts(monkeypatch, tmp_path: Path) -> None:
    worker = external_env._WindowsSyncWorker()
    acl_started = threading.Event()
    release_acl = threading.Event()
    creations = 0

    class Process:
        returncode = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[bytes, bytes]:
            acl_started.set()
            assert release_acl.wait(2)
            return b"", b""

        def kill(self) -> None:  # pragma: no cover - timeout fallback
            release_acl.set()

    def create(*args, **kwargs) -> Process:
        nonlocal creations
        creations += 1
        return Process()

    monkeypatch.setattr(external_env, "popen_external", create)
    path = tmp_path / "settings.yml"
    first = worker.submit_acl(path, directory=False, username="desktop-user")
    assert acl_started.wait(1)
    second = worker.submit_acl(path, directory=False, username="desktop-user")
    release_acl.set()

    assert first._done.wait(1)
    assert second._done.wait(1)
    assert first.wait() == second.wait() == 0
    assert creations == 1


def test_windows_sync_worker_reaps_synchronously_when_reaper_thread_cannot_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    worker = external_env._WindowsSyncWorker()
    communicated = threading.Event()

    class Process:
        returncode = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[bytes, bytes]:
            assert timeout == 5
            communicated.set()
            return b"", b""

        def kill(self) -> None:  # pragma: no cover - communicate completes
            raise AssertionError("unexpected kill")

    class ExhaustedThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread resources exhausted")

    monkeypatch.setattr(external_env, "popen_external", lambda *args, **kwargs: Process())
    monkeypatch.setattr(external_env.threading, "Thread", ExhaustedThread)

    receipt = worker.submit_acl(tmp_path / "settings.yml", directory=False, username="desktop-user")
    assert receipt._done.wait(1)
    assert receipt.wait() == 0
    assert communicated.is_set()

    # The sole coordinator worker must remain alive and serve the high lane.
    spawn_receipt = worker.submit_spawn(lambda: "still-alive", cleanup=False)
    assert spawn_receipt._done.wait(1)
    assert spawn_receipt.wait() == "still-alive"


def test_windows_desktop_browser_uses_high_lane_and_clean_environment(monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/system")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/bundle")
    monkeypatch.setattr(external_env.os, "name", "nt")
    received: dict[str, object] = {}

    class Process:
        def wait(self, *, timeout: float | None = None) -> int:
            assert timeout == 1.0
            return 0

    class Receipt:
        def __init__(self, factory) -> None:
            self.factory = factory

        def wait(self):
            return self.factory()

    def popen(command, **kwargs):
        received.update(command=command, **kwargs)
        return Process()

    def submit(factory, *, cleanup=False):
        received["cleanup"] = cleanup
        return Receipt(factory)

    monkeypatch.setattr(external_env, "popen_external", popen)
    monkeypatch.setattr(external_env, "submit_windows_spawn", submit)

    assert external_env.open_desktop_browser("https://example.test/oauth") is True
    assert received["command"] == ["explorer.exe", "https://example.test/oauth"]
    assert received["cleanup"] is False
    assert received["detached"] is True
    environment = received["env"]
    assert isinstance(environment, dict)
    assert environment["LD_LIBRARY_PATH"] == "/system"
    assert "DYLD_LIBRARY_PATH" not in environment


def test_mcp_desktop_browser_paths_use_the_controlled_adapter(monkeypatch) -> None:
    from iac_code.mcp import oauth

    calls: list[tuple[str, list[str] | None]] = []
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setenv("BROWSER", "custom-browser %s")
    monkeypatch.setattr(
        oauth,
        "open_desktop_browser",
        lambda url, command=None: calls.append((url, command)) or True,
    )

    assert oauth._open_browser("https://example.test/explicit") is True
    monkeypatch.delenv("BROWSER")
    assert oauth._open_browser("https://example.test/system") is True

    assert calls == [
        ("https://example.test/explicit", ["custom-browser", "https://example.test/explicit"]),
        ("https://example.test/system", None),
    ]


def test_failed_guardian_activation_reaps_process_before_completing_host_registration(monkeypatch) -> None:
    events: list[str] = []
    outstanding: set[int] = set()
    attached: set[int] = set()

    class Dispatcher:
        def allocate_registration_id(self) -> int:
            return 17

        def register_child_group(self, pgid: int, kind: str, *, registration_id: int | None = None) -> int:
            assert pgid == 4242
            assert kind == "bash"
            assert registration_id == 17
            outstanding.add(registration_id)
            events.append("register")
            raise TimeoutError("registered ACK was lost")

        def attach_guardian_writer(self, registration_id: int, writer) -> None:
            assert registration_id == 17
            attached.add(registration_id)
            events.append("attach")

        def detach_guardian_writer(self, registration_id: int) -> None:
            assert registration_id == 17
            attached.discard(registration_id)
            events.append("detach")

        def complete_child_group(self, registration_id: int, pgid: int, *, timeout: float = 10.0) -> None:
            assert registration_id == 17
            assert pgid == 4242
            assert timeout == 1.0
            outstanding.discard(registration_id)
            events.append("complete")

    class Process:
        pid = 4242

        def kill(self) -> None:
            events.append("kill")

        def wait(self) -> int:
            events.append("wait")
            return 0

    plan = external_env._GuardianPlan(
        helper="guardian",
        kind="bash",
        target=["target"],
        control_reader_fd=-1,
        status_writer_fd=-1,
        control_writer=io.BytesIO(),
        status_reader=io.BytesIO(),
    )
    monkeypatch.setattr(external_env, "_new_guardian_plan", lambda command: plan)
    monkeypatch.setattr(external_env, "get_control_dispatcher", lambda: Dispatcher())
    monkeypatch.setattr(external_env.subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(TimeoutError, match="ACK was lost"):
        external_env._guardian_popen(["guarded"], {})

    assert not outstanding
    assert not attached
    assert events.index("kill") < events.index("wait") < events.index("complete")


def test_guardian_watcher_start_failure_detaches_and_completes_registration(monkeypatch) -> None:
    events: list[str] = []
    attached: set[int] = set()
    outstanding: set[int] = set()

    class Dispatcher:
        def allocate_registration_id(self) -> int:
            return 23

        def register_child_group(self, pgid: int, kind: str, *, registration_id: int | None = None) -> int:
            assert registration_id == 23
            outstanding.add(registration_id)
            return registration_id

        def attach_guardian_writer(self, registration_id: int, writer) -> None:
            attached.add(registration_id)

        def detach_guardian_writer(self, registration_id: int) -> None:
            attached.discard(registration_id)
            events.append("detach")

        def complete_child_group(self, registration_id: int, pgid: int, *, timeout: float = 10.0) -> None:
            outstanding.discard(registration_id)
            events.append("complete")

    class Process:
        pid = 5252
        stdin = stdout = stderr = None

        def kill(self) -> None:
            events.append("kill")

        def wait(self) -> int:
            events.append("wait")
            return 0

    class ExhaustedThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread resources exhausted")

    plan = external_env._GuardianPlan(
        helper="guardian",
        kind="bash",
        target=["target"],
        control_reader_fd=-1,
        status_writer_fd=-1,
        control_writer=io.BytesIO(),
        status_reader=io.BytesIO(),
    )
    monkeypatch.setattr(external_env, "_new_guardian_plan", lambda command: plan)
    monkeypatch.setattr(external_env, "get_control_dispatcher", lambda: Dispatcher())
    monkeypatch.setattr(external_env.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(external_env, "_read_guardian_status", lambda *args, **kwargs: {"pid": 6262})
    monkeypatch.setattr(external_env.threading, "Thread", ExhaustedThread)

    with pytest.raises(RuntimeError, match="thread resources exhausted"):
        external_env._guardian_popen(["guarded"], {})

    assert not outstanding
    assert not attached
    assert events.index("kill") < events.index("wait") < events.index("complete")


def test_sync_guardian_second_wait_timeout_still_completes_registration(monkeypatch) -> None:
    events: list[str] = []

    class Dispatcher:
        def detach_guardian_writer(self, registration_id: int) -> None:
            events.append("detach")

        def complete_child_group(self, registration_id: int, pgid: int) -> None:
            events.append("complete")

    class Process:
        pid = 7171
        stdin = stdout = stderr = None

        def wait(self, *, timeout: float) -> int:
            events.append("wait:{}".format(timeout))
            raise external_env.subprocess.TimeoutExpired("guardian", timeout)

        def kill(self) -> None:
            events.append("kill")

    plan = external_env._GuardianPlan(
        helper="guardian",
        kind="bash",
        target=["target"],
        control_reader_fd=-1,
        status_writer_fd=-1,
        control_writer=io.BytesIO(),
        status_reader=io.BytesIO(),
    )
    process = Process()
    proxy = object.__new__(external_env._GuardianPopenProxy)
    external_env._GuardianProcessBase.__init__(proxy, process, plan, Dispatcher(), 31, 8181)
    proxy._done = threading.Event()
    proxy._cleanup_error = None
    monkeypatch.setattr(external_env, "_read_guardian_status", lambda *args, **kwargs: {"waitStatus": 0})
    monkeypatch.setattr(external_env, "_handoff_sync_guardian_reaper", lambda raw: events.append("handoff"))

    proxy._watch()

    assert proxy._done.is_set()
    assert events[-3:] == ["handoff", "detach", "complete"]


@pytest.mark.asyncio
async def test_async_guardian_second_wait_timeout_still_completes_registration(monkeypatch) -> None:
    events: list[str] = []

    class Dispatcher:
        def detach_guardian_writer(self, registration_id: int) -> None:
            events.append("detach")

        def complete_child_group(self, registration_id: int, pgid: int) -> None:
            events.append("complete")

    class Process:
        pid = 9191
        stdin = stdout = stderr = None

        async def wait(self) -> int:
            events.append("wait")
            raise TimeoutError("guardian did not exit")

        def kill(self) -> None:
            events.append("kill")

    plan = external_env._GuardianPlan(
        helper="guardian",
        kind="bash",
        target=["target"],
        control_reader_fd=-1,
        status_writer_fd=-1,
        control_writer=io.BytesIO(),
        status_reader=io.BytesIO(),
    )
    process = Process()
    proxy = object.__new__(external_env._AsyncGuardianProcessProxy)
    external_env._GuardianProcessBase.__init__(proxy, process, plan, Dispatcher(), 41, 1010)
    monkeypatch.setattr(external_env, "_read_guardian_status", lambda *args, **kwargs: {"waitStatus": 0})
    monkeypatch.setattr(external_env, "_handoff_async_guardian_reaper", lambda raw: events.append("handoff"))

    assert await proxy._watch() == 0
    assert events[-3:] == ["handoff", "detach", "complete"]
