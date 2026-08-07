"""Desktop-only process creation and frozen-runtime environment isolation."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypedDict, TypeVar, cast

from iac_code.desktop.control import DesktopControlDispatcher, get_control_dispatcher


class SpawnEnvKwargs(TypedDict, total=False):
    env: dict[str, str]


_T = TypeVar("_T")
_WINDOWS_CREATION_LOCK = threading.Lock()
_WINDOWS_PRELOAD_LOCK = threading.Lock()
_WINDOWS_PRELOAD_READY = False
_WINDOWS_DLL_DIRECTORY: str | None = None
_DEFAULT_PRELOAD_MANIFEST = Path(__file__).with_name("native_preload_manifest.json")
_ACL_QUEUE_CAPACITY = 128
_SPAWN_QUEUE_CAPACITY = 32
_CLEANUP_QUEUE_CAPACITY = 8
_ACL_REAPER_CAPACITY = 8
_SYNC_WORKER: _WindowsSyncWorker | None = None
_SYNC_WORKER_LOCK = threading.Lock()
_ASYNC_GUARDIAN_REAPERS: set[asyncio.Task[None]] = set()


def is_desktop_runtime() -> bool:
    return os.environ.get("IAC_CODE_DESKTOP_RUNTIME") == "1"


def _windows_desktop_runtime() -> bool:
    return os.name == "nt" and is_desktop_runtime()


@dataclass
class AclReceipt:
    """One-shot completion for a coalesced Windows Desktop ACL operation."""

    _done: threading.Event
    exit_status: int | None = None
    error: BaseException | None = None

    def wait(self) -> int | None:
        self._done.wait()
        if self.error is not None:
            raise self.error
        return self.exit_status


@dataclass
class SpawnReceipt:
    """One-shot process-handle or cleanup result from the high-priority lane."""

    _done: threading.Event
    handle: Any = None
    error: BaseException | None = None

    def wait(self) -> Any:
        self._done.wait()
        if self.error is not None:
            raise self.error
        return self.handle


class _WindowsSyncWorker:
    """Fixed dual-lane worker: opener/cleanup first, coalesced ACL second."""

    def __init__(self) -> None:
        self._acl_queue: queue.Queue[tuple[tuple[str, bool], Path, bool, str]] = queue.Queue(
            maxsize=_ACL_QUEUE_CAPACITY
        )
        self._spawn_queue: queue.Queue[tuple[SpawnReceipt, Callable[[], Any]]] = queue.Queue(
            maxsize=_SPAWN_QUEUE_CAPACITY
        )
        self._cleanup_queue: queue.Queue[tuple[SpawnReceipt, Callable[[], Any]]] = queue.Queue(
            maxsize=_CLEANUP_QUEUE_CAPACITY
        )
        self._wake = threading.Event()
        self._acl_reapers = threading.BoundedSemaphore(_ACL_REAPER_CAPACITY)
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, bool], list[AclReceipt]] = {}
        self._thread = threading.Thread(target=self._run, name="iac-code-desktop-sync-spawn", daemon=True)
        self._thread.start()

    def submit_acl(self, path: Path, *, directory: bool, username: str) -> AclReceipt:
        normalized = path.resolve(strict=False)
        key = (os.path.normcase(str(normalized)), directory)
        receipt = AclReceipt(threading.Event())
        with self._lock:
            waiters = self._pending.get(key)
            if waiters is not None:
                waiters.append(receipt)
                return receipt
            self._pending[key] = [receipt]
        try:
            # Deliberately apply bounded backpressure instead of dropping a
            # distinct path when the low-priority lane is full.
            self._acl_queue.put((key, normalized, directory, username))
            self._wake.set()
        except BaseException as exc:
            self._finish_acl(key, error=exc)
            raise
        return receipt

    def submit_spawn(self, factory: Callable[[], Any], *, cleanup: bool) -> SpawnReceipt:
        receipt = SpawnReceipt(threading.Event())
        target = self._cleanup_queue if cleanup else self._spawn_queue
        try:
            target.put_nowait((receipt, factory))
        except queue.Full as exc:
            raise BlockingIOError("Desktop synchronous process lane is busy") from exc
        self._wake.set()
        return receipt

    def _run(self) -> None:
        while True:
            spawn_item = self._next_spawn()
            if spawn_item is not None:
                receipt, factory = spawn_item
                try:
                    receipt.handle = factory()
                except BaseException as exc:
                    receipt.error = exc
                receipt._done.set()
                continue
            if not self._acl_reapers.acquire(blocking=False):
                self._wake.wait(0.02)
                self._wake.clear()
                continue
            try:
                key, path, directory, username = self._acl_queue.get_nowait()
            except queue.Empty:
                self._acl_reapers.release()
                self._wake.wait(0.05)
                self._wake.clear()
                continue
            try:
                perm = '"{}":(F)'.format(username) if directory else '"{}":(R,W)'.format(username)
                process = popen_external(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", perm],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except BaseException as exc:
                self._finish_acl(key, error=exc)
                self._acl_reapers.release()
            else:
                reaper = threading.Thread(
                    target=self._reap_acl,
                    args=(key, process),
                    name="iac-code-desktop-acl-reaper",
                    daemon=True,
                )
                try:
                    reaper.start()
                except BaseException:
                    # Thread creation can fail under resource pressure. Reap
                    # synchronously so the receipt and semaphore cannot be
                    # stranded and the sole coordinator worker stays alive.
                    self._reap_acl(key, process)
            self._acl_queue.task_done()

    def _next_spawn(self) -> tuple[SpawnReceipt, Callable[[], Any]] | None:
        for target in (self._cleanup_queue, self._spawn_queue):
            try:
                item = target.get_nowait()
            except queue.Empty:
                continue
            target.task_done()
            return item
        return None

    def _reap_acl(self, key: tuple[str, bool], process: Any) -> None:
        exit_status: int | None = None
        error: BaseException | None = None
        try:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            exit_status = process.returncode
        except BaseException as exc:
            error = exc
        finally:
            self._finish_acl(key, exit_status=exit_status, error=error)
            self._acl_reapers.release()
            self._wake.set()

    def _finish_acl(
        self,
        key: tuple[str, bool],
        *,
        exit_status: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            receipts = self._pending.pop(key, ())
        for receipt in receipts:
            receipt.exit_status = exit_status
            receipt.error = error
            receipt._done.set()


def initialize_windows_creation_coordinator() -> None:
    """Start the Desktop-only fixed high-priority/ACL worker."""
    _windows_sync_worker()


def _windows_sync_worker() -> _WindowsSyncWorker | None:
    global _SYNC_WORKER
    if not _windows_desktop_runtime():
        return None
    _require_windows_preload()
    with _SYNC_WORKER_LOCK:
        if _SYNC_WORKER is None:
            _SYNC_WORKER = _WindowsSyncWorker()
        return _SYNC_WORKER


def submit_windows_acl(path: Path, *, directory: bool, username: str) -> AclReceipt | None:
    """Submit one non-dropping, path-coalesced Desktop ``icacls`` operation."""
    worker = _windows_sync_worker()
    if worker is None:
        return None
    return worker.submit_acl(path, directory=directory, username=username)


def wait_windows_acl(receipt: AclReceipt) -> None:
    """Wait for an ACL receipt unless doing so would block the Desktop event loop.

    The worker still completes event-loop submissions in the background. Waiting
    there can deadlock with an in-flight async process creation because both the
    ACL worker and that creation serialize Windows' process-wide DLL directory.
    """
    if _in_running_event_loop():
        return
    receipt.wait()


def submit_windows_spawn(factory: Callable[[], Any], *, cleanup: bool = False) -> SpawnReceipt | None:
    """Submit one fixed high-priority opener/reveal/cleanup creation."""
    worker = _windows_sync_worker()
    if worker is None:
        return None
    return worker.submit_spawn(factory, cleanup=cleanup)


def spawn_env(existing_env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Return the original value outside Desktop and a clean copy inside Desktop."""
    if not is_desktop_runtime():
        return dict(existing_env) if existing_env is not None else None
    environment = dict(os.environ if existing_env is None else existing_env)
    original_library_path = environment.pop("LD_LIBRARY_PATH_ORIG", None)
    if original_library_path:
        environment["LD_LIBRARY_PATH"] = original_library_path
    else:
        environment.pop("LD_LIBRARY_PATH", None)
    for name in tuple(environment):
        if name.startswith("DYLD_") or name in {"_MEIPASS2", "PYTHONHOME"}:
            environment.pop(name, None)
    return environment


def spawn_env_kwargs(existing_env: Mapping[str, str] | None = None) -> SpawnEnvKwargs:
    environment = spawn_env(existing_env)
    return {} if environment is None else {"env": environment}


def guarded_command(command: list[str], *, kind: str = "bash") -> list[str]:
    """Wrap a POSIX Desktop child in the fixed native guardian helper."""
    helper = os.environ.get("IAC_CODE_DESKTOP_EXEC")
    if (
        not is_desktop_runtime()
        or os.name == "nt"
        or not helper
        or os.environ.get("IAC_CODE_DESKTOP_PROBE_CONTAINER") == "1"
    ):
        return command
    return [helper, "--child-guardian", "--kind", kind, "--", *command]


def guarded_shell_command(command: str, *, kind: str = "bash") -> list[str] | None:
    if not is_desktop_runtime() or os.name == "nt" or not os.environ.get("IAC_CODE_DESKTOP_EXEC"):
        return None
    return guarded_command([os.environ.get("SHELL") or "/bin/sh", "-c", command], kind=kind)


@dataclass
class _GuardianPlan:
    helper: str
    kind: str
    target: list[str]
    control_reader_fd: int
    status_writer_fd: int
    control_writer: BinaryIO
    status_reader: BinaryIO
    registration_dispatcher: DesktopControlDispatcher | None = None
    registration_id: int | None = None

    @property
    def command(self) -> list[str]:
        return [
            self.helper,
            "--child-guardian",
            "--control-fd",
            str(self.control_reader_fd),
            "--status-fd",
            str(self.status_writer_fd),
            "--",
            *self.target,
        ]

    @property
    def pass_fds(self) -> tuple[int, int]:
        return (self.control_reader_fd, self.status_writer_fd)

    def close_child_ends(self) -> None:
        for name in ("control_reader_fd", "status_writer_fd"):
            fd = getattr(self, name)
            if fd < 0:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
            setattr(self, name, -1)

    def close(self) -> None:
        self.close_child_ends()
        for stream in (self.control_writer, self.status_reader):
            try:
                stream.close()
            except OSError:
                pass


def _parse_guarded_command(command: Any) -> tuple[str, str, list[str]] | None:
    if os.name == "nt" or not is_desktop_runtime() or not isinstance(command, (list, tuple)):
        return None
    values = [os.fspath(value) for value in command]
    if len(values) < 6 or values[1:3] != ["--child-guardian", "--kind"] or values[4] != "--":
        return None
    helper = os.environ.get("IAC_CODE_DESKTOP_EXEC")
    if not helper or values[0] != helper or not values[3] or not values[5:]:
        return None
    return values[0], values[3], values[5:]


def is_guardian_command(command: Any) -> bool:
    return _parse_guarded_command(command) is not None


def _new_guardian_plan(command: Any) -> _GuardianPlan | None:
    parsed = _parse_guarded_command(command)
    if parsed is None:
        return None
    helper, kind, target = parsed
    control_reader_fd, control_writer_fd = os.pipe()
    status_reader_fd, status_writer_fd = os.pipe()
    os.set_inheritable(control_reader_fd, True)
    os.set_inheritable(status_writer_fd, True)
    return _GuardianPlan(
        helper=helper,
        kind=kind,
        target=target,
        control_reader_fd=control_reader_fd,
        status_writer_fd=status_writer_fd,
        control_writer=os.fdopen(control_writer_fd, "wb", buffering=0),
        status_reader=os.fdopen(status_reader_fd, "rb", buffering=0),
    )


def _read_guardian_status(plan: _GuardianPlan, expected: str, timeout: float | None = None) -> dict[str, Any] | None:
    if timeout is not None:
        readable, _, _ = select.select([plan.status_reader], [], [], timeout)
        if not readable:
            raise TimeoutError("Desktop child guardian status timed out")
    line = plan.status_reader.readline()
    if not line:
        return None
    prefix = (expected + " ").encode()
    if not line.startswith(prefix):
        raise RuntimeError("Desktop child guardian returned an invalid status")
    payload = json.loads(line[len(prefix) :])
    if not isinstance(payload, dict):
        raise RuntimeError("Desktop child guardian status is invalid")
    return payload


def _target_returncode(payload: Mapping[str, Any] | None, fallback: int) -> int:
    if payload is None or not isinstance(payload.get("waitStatus"), int):
        return fallback
    return os.waitstatus_to_exitcode(int(payload["waitStatus"]))


def _activate_guardian(
    plan: _GuardianPlan,
    guardian_pid: int,
) -> tuple[DesktopControlDispatcher, int, int]:
    dispatcher = get_control_dispatcher()
    if dispatcher is None:
        raise RuntimeError("Desktop child guardian requires an active Host control dispatcher")
    registration_id = dispatcher.allocate_registration_id()
    plan.registration_dispatcher = dispatcher
    plan.registration_id = registration_id
    dispatcher.register_child_group(guardian_pid, plan.kind, registration_id=registration_id)
    dispatcher.attach_guardian_writer(registration_id, plan.control_writer)
    try:
        plan.control_writer.write(b"START\n")
        plan.control_writer.flush()
        started = _read_guardian_status(plan, "STARTED", 10.0)
        if started is None or not isinstance(started.get("pid"), int) or int(started["pid"]) <= 0:
            raise RuntimeError("Desktop child guardian did not report its target")
        return dispatcher, registration_id, int(started["pid"])
    except BaseException:
        dispatcher.detach_guardian_writer(registration_id)
        try:
            plan.control_writer.close()
        except OSError:
            pass
        raise


def _complete_failed_guardian_registration(plan: _GuardianPlan, guardian_pid: int) -> None:
    """Best-effort compensation after the failed guardian has been reaped."""
    dispatcher = plan.registration_dispatcher
    registration_id = plan.registration_id
    plan.registration_dispatcher = None
    plan.registration_id = None
    if dispatcher is None or registration_id is None:
        return
    try:
        dispatcher.detach_guardian_writer(registration_id)
    except BaseException:
        pass
    try:
        dispatcher.complete_child_group(registration_id, guardian_pid, timeout=1.0)
    except BaseException:
        pass


async def _cleanup_failed_async_guardian(plan: _GuardianPlan, raw_process: Any) -> None:
    plan.close()
    try:
        try:
            raw_process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            await raw_process.wait()
        except (OSError, ProcessLookupError):
            pass
    finally:
        await asyncio.to_thread(_complete_failed_guardian_registration, plan, raw_process.pid)


class _GuardianProcessBase:
    def __init__(
        self,
        raw_process: Any,
        plan: _GuardianPlan,
        dispatcher: DesktopControlDispatcher,
        registration_id: int,
        target_pid: int,
    ) -> None:
        self._raw_process = raw_process
        self._plan = plan
        self._dispatcher = dispatcher
        self._registration_id = registration_id
        self._target_pid = target_pid
        self._returncode: int | None = None
        self._iac_code_desktop_guardian = True
        self._drain_lock = threading.Lock()
        self._drain: str | None = None
        self.args = plan.target
        self.stdin = raw_process.stdin
        self.stdout = raw_process.stdout
        self.stderr = raw_process.stderr

    @property
    def pid(self) -> int:
        return self._target_pid

    def _request_drain(self, command: str) -> None:
        with self._drain_lock:
            if self._drain == "DRAIN_FORCE" or self._drain == command:
                return
            self._drain = command
            try:
                self._plan.control_writer.write((command + "\n").encode())
                self._plan.control_writer.flush()
            except (OSError, ValueError):
                pass

    def terminate(self) -> None:
        self._request_drain("DRAIN_GRACE")

    def kill(self) -> None:
        self._request_drain("DRAIN_FORCE")

    def _finish_registration(self) -> None:
        error: BaseException | None = None
        try:
            self._dispatcher.detach_guardian_writer(self._registration_id)
        except BaseException as exc:
            error = exc
        try:
            self._plan.control_writer.close()
        except OSError as exc:
            error = error or exc
        try:
            self._dispatcher.complete_child_group(self._registration_id, self._raw_process.pid)
        except BaseException as exc:
            error = error or exc
        finally:
            try:
                self._plan.status_reader.close()
            except OSError as exc:
                error = error or exc
        if error is not None:
            raise error


def _handoff_sync_guardian_reaper(process: Any) -> None:
    def reap() -> None:
        try:
            process.wait(timeout=3)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass

    try:
        threading.Thread(
            target=reap,
            name="iac-code-desktop-guardian-reaper",
            daemon=True,
        ).start()
    except RuntimeError:
        pass


def _handoff_async_guardian_reaper(process: Any) -> None:
    async def reap() -> None:
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except (OSError, ProcessLookupError, asyncio.TimeoutError, TimeoutError):
            pass

    task = asyncio.create_task(reap(), name="desktop-child-guardian-reaper")
    _ASYNC_GUARDIAN_REAPERS.add(task)
    task.add_done_callback(_ASYNC_GUARDIAN_REAPERS.discard)


class _GuardianPopenProxy(_GuardianProcessBase):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._done = threading.Event()
        self._cleanup_error: BaseException | None = None
        self._watcher = threading.Thread(target=self._watch, name="iac-code-desktop-child-guardian", daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        try:
            try:
                payload = _read_guardian_status(self._plan, "EXIT")
                self._returncode = _target_returncode(
                    payload,
                    -signal.SIGKILL if self._drain == "DRAIN_FORCE" else -signal.SIGTERM,
                )
                if payload is not None:
                    # The direct target has already been reaped and its real status
                    # is durable. The still-live guardian pins the PGID, so force
                    # drains inherited descendants without a recycled-group race.
                    # Natural target exit gets the guardian's normal grace;
                    # explicit ``kill()`` has already selected DRAIN_FORCE.
                    self._request_drain("DRAIN_GRACE")
            except BaseException as error:
                self._cleanup_error = error
                self._returncode = -signal.SIGKILL if self._drain == "DRAIN_FORCE" else -signal.SIGTERM
                self._request_drain("DRAIN_FORCE")
            try:
                self._raw_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._raw_process.kill()
                try:
                    self._raw_process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _handoff_sync_guardian_reaper(self._raw_process)
        except BaseException as error:
            self._cleanup_error = self._cleanup_error or error
        finally:
            try:
                self._finish_registration()
            except BaseException as error:
                self._cleanup_error = self._cleanup_error or error
            self._done.set()

    @property
    def returncode(self) -> int | None:
        return self._returncode if self._done.is_set() else None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            assert timeout is not None
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self._cleanup_error is not None:
            raise self._cleanup_error
        return self._returncode if self._returncode is not None else -signal.SIGTERM

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        stdout, stderr = self._raw_process.communicate(input=input, timeout=timeout)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        self.wait(remaining)
        return stdout, stderr

    def __enter__(self) -> _GuardianPopenProxy:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.returncode is None:
            self.terminate()
        self.wait(5)


class _AsyncGuardianProcessProxy(_GuardianProcessBase):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._cleanup_task = asyncio.create_task(self._watch(), name="desktop-child-guardian-cleanup")

    async def _watch(self) -> int:
        try:
            try:
                payload = await asyncio.to_thread(_read_guardian_status, self._plan, "EXIT")
                self._returncode = _target_returncode(
                    payload,
                    -signal.SIGKILL if self._drain == "DRAIN_FORCE" else -signal.SIGTERM,
                )
                if payload is not None:
                    self._request_drain("DRAIN_GRACE")
            except BaseException:
                self._returncode = -signal.SIGKILL if self._drain == "DRAIN_FORCE" else -signal.SIGTERM
                self._request_drain("DRAIN_FORCE")
            try:
                await asyncio.wait_for(self._raw_process.wait(), timeout=5)
            except (asyncio.TimeoutError, TimeoutError):
                self._raw_process.kill()
                try:
                    await asyncio.wait_for(self._raw_process.wait(), timeout=1)
                except (asyncio.TimeoutError, TimeoutError):
                    _handoff_async_guardian_reaper(self._raw_process)
            return self._returncode
        finally:
            try:
                await asyncio.to_thread(self._finish_registration)
            finally:
                try:
                    self._plan.status_reader.close()
                except OSError:
                    pass

    @property
    def returncode(self) -> int | None:
        return self._returncode if self._cleanup_task.done() else None

    async def wait(self) -> int:
        return await asyncio.shield(self._cleanup_task)

    async def communicate(self, input: Any = None) -> tuple[Any, Any]:
        stdout, stderr = await self._raw_process.communicate(input)
        await self.wait()
        return stdout, stderr

    async def aclose(self) -> None:
        if not self._cleanup_task.done():
            self.terminate()
        await self.wait()
        close = getattr(self._raw_process, "aclose", None)
        if callable(close):
            await close()

    async def __aenter__(self) -> _AsyncGuardianProcessProxy:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()


def is_guardian_process(process: Any) -> bool:
    return bool(getattr(process, "_iac_code_desktop_guardian", False))


def _guardian_popen(command: Any, kwargs: dict[str, Any]) -> _GuardianPopenProxy | None:
    plan = _new_guardian_plan(command)
    if plan is None:
        return None
    options = dict(kwargs)
    options["start_new_session"] = True
    options["pass_fds"] = tuple(dict.fromkeys((*options.get("pass_fds", ()), *plan.pass_fds)))
    try:
        raw_process = subprocess.Popen(plan.command, **options)
        plan.close_child_ends()
        dispatcher, registration_id, target_pid = _activate_guardian(plan, raw_process.pid)
        return _GuardianPopenProxy(raw_process, plan, dispatcher, registration_id, target_pid)
    except BaseException:
        plan.close()
        if "raw_process" in locals():
            try:
                try:
                    raw_process.kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    raw_process.wait()
                except (OSError, ProcessLookupError):
                    pass
            finally:
                _complete_failed_guardian_registration(plan, raw_process.pid)
        raise


def windows_creation_flags(existing: int = 0) -> int:
    """Hide console-subsystem children only for the Desktop Windows runtime."""
    if not _windows_desktop_runtime():
        return existing
    return existing | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def windows_detached_creation_flags(existing: int = 0) -> int:
    """Allow an intentional system opener to outlive the sidecar Job."""
    flags = windows_creation_flags(existing)
    if not _windows_desktop_runtime():
        return flags
    return (
        flags
        | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )


def _get_windows_dll_directory() -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    kernel32.GetDllDirectoryW.restype = wintypes.DWORD
    length = kernel32.GetDllDirectoryW(0, None)
    if length == 0:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    copied = kernel32.GetDllDirectoryW(len(buffer), buffer)
    if copied == 0:
        return None
    return buffer.value


def _set_windows_dll_directory(path: str | None) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel32.SetDllDirectoryW.restype = wintypes.BOOL
    if not kernel32.SetDllDirectoryW(path):
        error_code = getattr(ctypes, "get_last_error")()
        raise OSError(error_code, "SetDllDirectoryW failed")


def _manifest_modules(path: Path) -> tuple[str, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    modules = payload.get("modules") if isinstance(payload, dict) else None
    if not isinstance(modules, list) or not all(isinstance(value, str) and value for value in modules):
        raise ValueError("Desktop native preload manifest must contain a modules list")
    return tuple(dict.fromkeys(cast(list[str], modules)))


def initialize_windows_native_preload(
    modules: Iterable[str] | None = None,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Import frozen native users before enabling Windows external process creation."""
    global _WINDOWS_DLL_DIRECTORY, _WINDOWS_PRELOAD_READY

    if not _windows_desktop_runtime():
        return
    with _WINDOWS_PRELOAD_LOCK:
        if _WINDOWS_PRELOAD_READY:
            return
        selected = (
            tuple(modules)
            if modules is not None
            else _manifest_modules(
                manifest_path
                or Path(os.environ.get("IAC_CODE_DESKTOP_NATIVE_PRELOAD_MANIFEST", _DEFAULT_PRELOAD_MANIFEST))
            )
        )
        for module in selected:
            importlib.import_module(module)
        _WINDOWS_DLL_DIRECTORY = _get_windows_dll_directory()
        _WINDOWS_PRELOAD_READY = True


def _require_windows_preload() -> None:
    if _windows_desktop_runtime() and not _WINDOWS_PRELOAD_READY:
        raise RuntimeError("Windows Desktop native preload has not completed")


class _WindowsCreationBoundary:
    def __enter__(self) -> None:
        _require_windows_preload()
        _WINDOWS_CREATION_LOCK.acquire()
        try:
            _set_windows_dll_directory(None)
        except BaseException:
            _WINDOWS_CREATION_LOCK.release()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            _set_windows_dll_directory(_WINDOWS_DLL_DIRECTORY)
        finally:
            _WINDOWS_CREATION_LOCK.release()


def _in_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def create_external_process(
    factory: Callable[..., _T],
    /,
    *args: Any,
    add_creation_flags: bool = True,
    **kwargs: Any,
) -> _T:
    """Call a synchronous process factory inside the Windows creation boundary."""
    if not _windows_desktop_runtime():
        return factory(*args, **kwargs)
    if _windows_desktop_runtime() and _in_running_event_loop():
        raise RuntimeError("synchronous Desktop process creation is not allowed on an event-loop thread")
    if add_creation_flags:
        kwargs["creationflags"] = windows_creation_flags(int(kwargs.get("creationflags", 0)))
    with _WindowsCreationBoundary():
        return factory(*args, **kwargs)


async def _acquire_windows_creation_boundary() -> _WindowsCreationBoundary:
    loop = asyncio.get_running_loop()
    acquire = loop.run_in_executor(None, _WINDOWS_CREATION_LOCK.acquire)
    try:
        await asyncio.shield(acquire)
    except asyncio.CancelledError:
        acquired = await asyncio.shield(acquire)
        if acquired:
            _WINDOWS_CREATION_LOCK.release()
        raise
    try:
        _require_windows_preload()
        _set_windows_dll_directory(None)
    except BaseException:
        _WINDOWS_CREATION_LOCK.release()
        raise
    return _WindowsCreationBoundary()


def _leave_async_windows_creation_boundary() -> None:
    try:
        _set_windows_dll_directory(_WINDOWS_DLL_DIRECTORY)
    finally:
        _WINDOWS_CREATION_LOCK.release()


async def create_external_process_async(
    factory: Callable[..., Awaitable[_T]],
    /,
    *args: Any,
    add_creation_flags: bool = True,
    **kwargs: Any,
) -> _T:
    """Create an async process on its caller loop under the process-wide Windows lock."""
    if not _windows_desktop_runtime():
        return await factory(*args, **kwargs)
    _require_windows_preload()
    if add_creation_flags:
        kwargs["creationflags"] = windows_creation_flags(int(kwargs.get("creationflags", 0)))
    await _acquire_windows_creation_boundary()
    owns_boundary = True
    try:
        # The factory is deliberately awaited on the originating loop. The lock
        # covers handle creation only; callers own the resulting process lifetime.
        creation = asyncio.ensure_future(factory(*args, **kwargs))
        try:
            return await asyncio.shield(creation)
        except asyncio.CancelledError:
            try:
                unclaimed = await asyncio.shield(creation)
            except BaseException:
                raise asyncio.CancelledError from None
            _leave_async_windows_creation_boundary()
            owns_boundary = False
            await asyncio.shield(_dispose_unclaimed_process(unclaimed))
            raise
    finally:
        if owns_boundary:
            _leave_async_windows_creation_boundary()


async def _dispose_unclaimed_process(process: Any) -> None:
    """Best-effort cleanup when cancellation wins before handle handoff."""
    try:
        process.terminate()
    except (AttributeError, OSError, ProcessLookupError):
        pass
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            result = wait()
            if isinstance(result, Awaitable):
                await asyncio.wait_for(result, timeout=1.0)
            return
        except (OSError, ProcessLookupError, asyncio.TimeoutError, TimeoutError):
            pass
    try:
        process.kill()
    except (AttributeError, OSError, ProcessLookupError):
        pass
    if callable(wait):
        try:
            result = wait()
            if isinstance(result, Awaitable):
                await asyncio.wait_for(result, timeout=0.5)
            return
        except (OSError, ProcessLookupError, asyncio.TimeoutError, TimeoutError):
            pass
    close = getattr(process, "aclose", None)
    if callable(close):
        try:
            await close()
        except (OSError, ProcessLookupError):
            pass


async def _create_asyncio_guardian(plan: _GuardianPlan, kwargs: dict[str, Any]) -> _AsyncGuardianProcessProxy:
    options = dict(kwargs)
    options["start_new_session"] = True
    options["pass_fds"] = tuple(dict.fromkeys((*options.get("pass_fds", ()), *plan.pass_fds)))
    raw_process: Any | None = None
    try:
        raw_process = await asyncio.create_subprocess_exec(*plan.command, **options)
        plan.close_child_ends()
        dispatcher, registration_id, target_pid = await asyncio.to_thread(
            _activate_guardian,
            plan,
            raw_process.pid,
        )
        return _AsyncGuardianProcessProxy(raw_process, plan, dispatcher, registration_id, target_pid)
    except BaseException:
        if raw_process is not None:
            completion = asyncio.create_task(_cleanup_failed_async_guardian(plan, raw_process))
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                await asyncio.shield(completion)
        else:
            plan.close()
        raise


async def create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
    plan = _new_guardian_plan(list(args))
    if plan is not None:
        return await _create_asyncio_guardian(plan, kwargs)
    return await create_external_process_async(asyncio.create_subprocess_exec, *args, **kwargs)


async def create_subprocess_shell(command: str, **kwargs: Any) -> asyncio.subprocess.Process:
    return await create_external_process_async(asyncio.create_subprocess_shell, command, **kwargs)


async def create_anyio_process(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
    stderr: Any = None,
    cwd: str | Path | None = None,
) -> Any:
    """Create the one fixed MCP stdio child while preserving guardian fds."""
    import anyio

    plan = _new_guardian_plan(command)
    if plan is None:
        return await anyio.open_process(command, env=env, stderr=stderr, cwd=cwd, start_new_session=True)
    raw_process: Any | None = None
    try:
        raw_process = await anyio.open_process(
            plan.command,
            env=env,
            stderr=stderr,
            cwd=cwd,
            start_new_session=True,
            pass_fds=plan.pass_fds,
        )
        plan.close_child_ends()
        dispatcher, registration_id, target_pid = await asyncio.to_thread(
            _activate_guardian,
            plan,
            raw_process.pid,
        )
        return _AsyncGuardianProcessProxy(raw_process, plan, dispatcher, registration_id, target_pid)
    except BaseException:
        if raw_process is not None:
            completion = asyncio.create_task(_cleanup_failed_async_guardian(plan, raw_process))
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                await asyncio.shield(completion)
        else:
            plan.close()
        raise


def popen_external(*args: Any, detached: bool = False, **kwargs: Any) -> Any:
    if args and not detached:
        guarded = _guardian_popen(args[0], kwargs)
        if guarded is not None:
            return guarded
    if detached and is_desktop_runtime() and os.name != "nt":
        kwargs["start_new_session"] = True
    if detached and _windows_desktop_runtime():
        kwargs["creationflags"] = windows_detached_creation_flags(int(kwargs.get("creationflags", 0)))
    return create_external_process(subprocess.Popen, *args, **kwargs)


def run_external(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """A subprocess.run equivalent whose Windows lock covers only Popen creation."""
    guarded = bool(args and _parse_guarded_command(args[0]) is not None)
    if not _windows_desktop_runtime() and not guarded:
        return subprocess.run(*args, **kwargs)
    if _windows_desktop_runtime() and _in_running_event_loop():
        raise RuntimeError("synchronous Desktop process creation is not allowed on an event-loop thread")

    input_value = kwargs.pop("input", None)
    capture_output = bool(kwargs.pop("capture_output", False))
    timeout = kwargs.pop("timeout", None)
    check = bool(kwargs.pop("check", False))
    if input_value is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    process = popen_external(*args, **kwargs)
    try:
        stdout, stderr = process.communicate(input_value, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(process.args, timeout, output=stdout, stderr=stderr)
    completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed


async def popen_external_async(*args: Any, detached: bool = False, **kwargs: Any) -> Any:
    """Create a synchronous Popen handle from an async Desktop caller."""
    if not _windows_desktop_runtime():
        if args and _parse_guarded_command(args[0]) is not None:
            return await asyncio.to_thread(popen_external, *args, detached=detached, **kwargs)
        if detached and is_desktop_runtime() and os.name != "nt":
            kwargs["start_new_session"] = True
        return subprocess.Popen(*args, **kwargs)
    _require_windows_preload()
    receipt = submit_windows_spawn(lambda: popen_external(*args, detached=detached, **kwargs))
    assert receipt is not None
    delivery = asyncio.create_task(asyncio.to_thread(receipt.wait))
    try:
        return await asyncio.shield(delivery)
    except asyncio.CancelledError:
        process = await asyncio.shield(delivery)
        if not detached:
            await asyncio.shield(_dispose_unclaimed_process(process))
        raise


def open_desktop_browser(url: str, *, command: list[str] | None = None) -> bool:
    """Open one URL without letting a frozen sidecar environment leak into the browser."""
    if not is_desktop_runtime():
        raise RuntimeError("Desktop browser adapter requires the Desktop runtime")
    resolved_command = command
    if resolved_command is None:
        if os.name == "nt":
            resolved_command = ["explorer.exe", url]
        elif sys.platform == "darwin":
            resolved_command = ["open", url]
        else:
            resolved_command = ["xdg-open", url]

    def create() -> Any:
        return popen_external(
            resolved_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            detached=True,
            **spawn_env_kwargs(),
        )

    if os.name == "nt":
        receipt = submit_windows_spawn(create)
        if receipt is None:
            return False
        process = receipt.wait()
    else:
        process = create()
    try:
        return process.wait(timeout=1.0) == 0
    except subprocess.TimeoutExpired:
        try:
            threading.Thread(
                target=process.wait,
                name="iac-code-desktop-browser-reaper",
                daemon=True,
            ).start()
        except RuntimeError:
            pass
        return True
