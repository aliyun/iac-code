"""Cross-process lease used only by Desktop prerequisite transactions."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, BinaryIO


class DesktopInstallLease:
    def __init__(self, path: Path, *, timeout: float = 30.0, shared: bool = False) -> None:
        self.path = path
        self.timeout = timeout
        self.shared = shared
        self._file: BinaryIO | None = None
        self._windows_overlapped: Any | None = None

    def __enter__(self) -> DesktopInstallLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    self._lock_windows(file)
                else:
                    import fcntl

                    operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                    fcntl.flock(file.fileno(), operation | fcntl.LOCK_NB)
                self._file = file
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    file.close()
                    raise TimeoutError("Desktop prerequisite install lock timed out")
                time.sleep(0.05)

    def __exit__(self, _type, _value, _traceback) -> None:
        file = self._file
        self._file = None
        if file is None:
            return
        try:
            if os.name == "nt":
                self._unlock_windows(file)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()

    def _lock_windows(self, file: BinaryIO) -> None:
        """Acquire a real Windows shared/exclusive byte-range lease.

        ``msvcrt.locking`` only exposes exclusive DOS locks, so using it for a
        reader would serialize readers and still fail to implement the contract
        expressed by ``shared=True``.  LockFileEx supports both modes and the CRT
        opens Python files with share-deny-none, which lets other Desktop channels
        open the stable lock file before contending on this byte range.
        """
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        overlapped = Overlapped()
        flags = 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
        if not self.shared:
            flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
        handle = wintypes.HANDLE(getattr(msvcrt, "get_osfhandle")(file.fileno()))
        lock_file_ex = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True).LockFileEx
        lock_file_ex.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        lock_file_ex.restype = wintypes.BOOL
        if not lock_file_ex(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            error_code = getattr(ctypes, "get_last_error")()
            raise OSError(error_code, "LockFileEx failed")
        self._windows_overlapped = overlapped

    def _unlock_windows(self, file: BinaryIO) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        overlapped = self._windows_overlapped
        self._windows_overlapped = None
        if overlapped is None:
            return
        handle = wintypes.HANDLE(getattr(msvcrt, "get_osfhandle")(file.fileno()))
        unlock_file_ex = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True).UnlockFileEx
        unlock_file_ex.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        unlock_file_ex.restype = wintypes.BOOL
        if not unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped)):
            error_code = getattr(ctypes, "get_last_error")()
            raise OSError(error_code, "UnlockFileEx failed")
