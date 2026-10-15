"""Local kernel process identity for conservative execution-owner liveness checks.

An identity mismatch is never a replacement authorization. In particular the
Linux boot identifier identifies a host kernel boot, not a destroyed container.
Cross-runtime recovery still requires a committed handoff or trusted authority.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _DarwinProcessIdentity:
    """Read the public libproc ABI and kernel boot UUID without shell commands."""

    class BsdInfo(ctypes.Structure):
        # sys/proc_info.h: proc_bsdinfo (MAXCOMLEN = 16).
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    def read(self, pid: int) -> tuple[str, str, str] | None:
        if pid > 0x7FFFFFFF:
            return None
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        sysctl = libc.sysctlbyname
        sysctl.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        sysctl.restype = ctypes.c_int
        boot = ctypes.create_string_buffer(37)
        length = ctypes.c_size_t(ctypes.sizeof(boot))
        # A UUID is stable even when the wall clock (and kern.boottime) changes.
        if sysctl(b"kern.bootsessionuuid", boot, ctypes.byref(length), None, 0) != 0:
            return None
        if length.value != 37 or boot.raw[-1:] != b"\x00":
            return None
        boot_id = str(UUID(boot.value.decode("ascii")))
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        proc_pidinfo.restype = ctypes.c_int
        info = self.BsdInfo()
        size = ctypes.sizeof(info)
        if proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size or info.pbi_pid != pid:
            return None
        if info.pbi_start_tvsec == 0 or info.pbi_start_tvusec >= 1000000:
            return None
        start = info.pbi_start_tvsec * 1000000 + info.pbi_start_tvusec
        return f"darwin-boot:{boot_id}", "darwin-global", str(start)


class _WindowsProcessIdentity:
    """Read boot and process creation times only in the proven host PID domain."""

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        @property
        def value(self) -> int:
            return (self.high << 32) | self.low

    def read(self, pid: int) -> tuple[str, str, str] | None:
        if pid > 0xFFFFFFFF:
            return None
        load = getattr(ctypes, "WinDLL")
        native = load("ntdll", use_last_error=True)
        # https://github.com/winsiderss/phnt/blob/master/ntrtl.h:
        # ULONG NTAPI RtlGetCurrentServiceSessionId(VOID).
        # The Windows loader uses zero for host shared data and nonzero for
        # server-silo shared data. This is an undocumented native contract:
        # missing exports and every nonzero result leave the domain unknown.
        # Ordinary job membership does not create a separate PID namespace.
        session = native.RtlGetCurrentServiceSessionId
        session.argtypes = []
        session.restype = ctypes.c_uint32
        if session() != 0:
            return None
        query = native.NtQuerySystemInformation
        query.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        query.restype = ctypes.c_int32
        # phnt/ntexapi.h SYSTEM_TIMEOFDAY_INFORMATION (native class 3): the
        # first LARGE_INTEGER is BootTime. Require the complete 48-byte layout.
        # Clock corrections can change BootTime, causing conservative mismatch.
        time_info = ctypes.create_string_buffer(48)
        returned = ctypes.c_uint32()
        if query(3, time_info, ctypes.sizeof(time_info), ctypes.byref(returned)) != 0:
            return None
        if returned.value != ctypes.sizeof(time_info):
            return None
        boot = int.from_bytes(time_info.raw[:8], "little", signed=True)
        if boot <= 0:
            return None
        kernel = load("kernel32", use_last_error=True)
        open_process = kernel.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        get_times = kernel.GetProcessTimes
        get_times.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(self.FileTime)] * 4)]
        get_times.restype = ctypes.c_int32
        close_handle = kernel.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int32
        handle = open_process(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = (self.FileTime() for _ in range(4))
            if not get_times(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)
            ):
                return None
            if created.value == 0:
                return None
            return f"windows-boot:{boot}", "windows-host", str(created.value)
        finally:
            close_handle(handle)


class ExecutionOwnerIdentity(BaseModel):
    """Immutable identity whose PID is meaningful only inside its kernel domain.

    Only native kernel information supplies the local process domain. Worker UUIDs,
    hostnames, environment variables and client metadata are not identity sources.
    Unsupported platforms or unavailable kernel APIs leave the domain unproven.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    runtime_incarnation: str | None = Field(default=None, alias="runtimeIncarnation", min_length=1)
    pid_namespace: str | None = Field(default=None, alias="pidNamespace", min_length=1)
    process_start_identity: str | None = Field(default=None, alias="processStartIdentity", pattern=r"^(0|[1-9][0-9]*)$")
    pid: int = Field(strict=True, gt=0)

    _proc_root: ClassVar[Path] = Path("/proc")

    @classmethod
    def capture(cls, pid: int | None = None) -> ExecutionOwnerIdentity:
        """Capture a local PID without interpreting missing information as death."""
        identity = cls(pid=os.getpid() if pid is None else pid)
        if sys.platform in {"darwin", "win32"}:
            try:
                reader = _DarwinProcessIdentity() if sys.platform == "darwin" else _WindowsProcessIdentity()
                native = reader.read(identity.pid)
            except (OSError, ValueError, UnicodeError, AttributeError):
                return identity
            if native is not None:
                return cls(
                    pid=identity.pid,
                    runtimeIncarnation=native[0],
                    pidNamespace=native[1],
                    processStartIdentity=native[2],
                )
            return identity
        if sys.platform != "linux":
            return identity
        try:
            boot_id = str(UUID((cls._proc_root / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()))
            local_namespace = (cls._proc_root / "self/ns/pid").stat()
            if not cls._proc_mount_matches_current_namespace():
                return identity
            target_namespace = (cls._proc_root / str(identity.pid) / "ns/pid").stat()
            namespace_key = (local_namespace.st_dev, local_namespace.st_ino)
            if namespace_key != (target_namespace.st_dev, target_namespace.st_ino):
                return identity
            init_start = cls._read_process_start(1)
            process_start = cls._read_process_start(identity.pid)
            if init_start is None or process_start is None:
                return identity
        except (OSError, ValueError, UnicodeError):
            return identity
        # Namespace inodes may be reused after destruction. Bind the namespace
        # to its init process start tick within this kernel boot as well.
        return cls(
            pid=identity.pid,
            runtimeIncarnation=f"linux-boot:{boot_id}",
            pidNamespace=f"{namespace_key[0]}:{namespace_key[1]}:{init_start}",
            processStartIdentity=process_start,
        )

    @classmethod
    def _proc_mount_matches_current_namespace(cls) -> bool:
        # NSpid lists coordinates from the procfs mount's PID namespace down to
        # this process's current namespace. Exactly one matching PID proves
        # that /proc/1 is our namespace init, even when ptrace rules prevent a
        # non-root worker from dereferencing the root-owned /proc/1/ns/pid.
        # An ancestor procfs mount produces multiple coordinates and must not
        # supply the init start tick used to fence namespace inode reuse.
        status = (cls._proc_root / "self/status").read_text(encoding="ascii")
        coordinates = [line.split() for line in status.splitlines() if line.startswith("NSpid:")]
        return coordinates == [["NSpid:", str(os.getpid())]]

    @classmethod
    def _read_process_start(cls, pid: int) -> str | None:
        try:
            stat = (cls._proc_root / str(pid) / "stat").read_text(encoding="ascii")
            # comm is parenthesized but may contain spaces and ')' itself.
            prefix, separator, suffix = stat.rpartition(")")
            if not separator or not prefix.startswith(f"{pid} ("):
                return None
            start = suffix.split()[19]  # suffix begins at stat field 3; starttime is field 22.
            if not start.isascii() or not start.isdecimal():
                return None
            return str(int(start))
        except (OSError, ValueError, IndexError, UnicodeError):
            return None

    def as_document(self) -> dict[str, Any]:
        """Return the JSON-compatible persisted identity, without worker metadata."""
        return self.model_dump(mode="json", by_alias=True)

    def same_process_domain(self, other: ExecutionOwnerIdentity) -> bool:
        """Whether local PID lookups can be interpreted in the same kernel domain."""
        return bool(
            self.runtime_incarnation
            and self.pid_namespace
            and self.runtime_incarnation == other.runtime_incarnation
            and self.pid_namespace == other.pid_namespace
        )

    def can_prove_dead(self, previous: ExecutionOwnerIdentity, pid_alive: Callable[[int], bool]) -> bool:
        """Prove absence or PID reuse only for an owner in our proven local domain.

        A live sibling worker is never reclaimable merely because it differs
        from this worker. Any failed or ambiguous probe preserves ownership.
        """
        if not self.same_process_domain(previous):
            return False
        if not self.process_start_identity or not previous.process_start_identity:
            return False
        if self.pid == previous.pid and self.process_start_identity == previous.process_start_identity:
            return False
        try:
            alive = pid_alive(previous.pid)
        except (OSError, ValueError):
            return False
        if alive is False:
            return True
        if alive is not True:
            return False
        observed = type(self).capture(previous.pid)
        return bool(
            self.same_process_domain(observed)
            and observed.process_start_identity
            and observed.process_start_identity != previous.process_start_identity
        )
