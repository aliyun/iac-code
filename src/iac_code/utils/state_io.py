"""Durable state-file I/O helpers for recovery-critical files."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Iterator, TextIO, cast

from iac_code.i18n import _
from iac_code.services.first_token_timing import current_timing, timing_phase
from iac_code.utils.path_locks import PathLockRegistry

logger = logging.getLogger(__name__)
_PATH_LOCKS = PathLockRegistry()
_FILE_OPEN = os.open
_SNAPSHOT_WRITES: OrderedDict[Path, tuple[bytes, tuple[int, ...]]] = OrderedDict()
_SNAPSHOT_GUARD = threading.Lock()


def _path_lock(path: Path):
    return _PATH_LOCKS.lock_for(path)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_symlink_or_reparse_leaf(path: Path) -> None:
    if path.is_symlink() or _is_reparse_point(path):
        raise OSError(_("refusing to follow symlink or reparse point: {path}").format(path=path))


def _open_no_follow(path: Path, flags: int, mode: int):
    _reject_symlink_or_reparse_leaf(path)
    if os.name == "nt":
        return _open_windows_no_follow(path, flags, mode)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = _FILE_OPEN(path, flags | nofollow, mode)
    try:
        fd_stat = os.fstat(fd)
        if not stat.S_ISREG(fd_stat.st_mode):
            raise OSError(_("refusing to open non-regular file: {path}").format(path=path))
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_windows_no_follow(path: Path, flags: int, _mode: int):
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
    except ImportError as exc:
        raise OSError(_("could not open file without following reparse point: {path}").format(path=path)) from exc

    windll_factory: Any = getattr(ctypes, "WinDLL", None)
    if windll_factory is None:
        raise OSError(_("could not open file without following reparse point: {path}").format(path=path))
    kernel32 = windll_factory("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_share_all = 0x00000001 | 0x00000002 | 0x00000004
    create_new = 1
    open_existing = 3
    open_always = 4
    file_attribute_normal = 0x00000080
    file_attribute_reparse_point = 0x00000400
    file_flag_open_reparse_point = 0x00200000

    if flags & os.O_RDWR:
        desired_access = generic_read | generic_write
        fd_flags = os.O_RDWR
    elif flags & os.O_WRONLY:
        desired_access = generic_write
        fd_flags = os.O_WRONLY
    else:
        desired_access = generic_read
        fd_flags = os.O_RDONLY
    if flags & os.O_APPEND:
        fd_flags |= os.O_APPEND
    fd_flags |= getattr(os, "O_BINARY", 0)
    creation_disposition = open_existing
    if flags & os.O_CREAT:
        creation_disposition = create_new if flags & os.O_EXCL else open_always

    handle = kernel32.CreateFileW(
        str(path),
        desired_access,
        file_share_all,
        None,
        creation_disposition,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(_("could not open file without following reparse point: {path}").format(path=path))
    try:
        file_info = _ByHandleFileInformation()
        if (
            not kernel32.GetFileInformationByHandle(handle, ctypes.byref(file_info))
            or file_info.dwFileAttributes & file_attribute_reparse_point
        ):
            raise OSError(_("refusing to follow symlink or reparse point: {path}").format(path=path))
        open_osfhandle: Any = getattr(msvcrt, "open_osfhandle", None)
        if open_osfhandle is None:
            raise OSError(_("could not open file without following reparse point: {path}").format(path=path))
        fd = open_osfhandle(handle, fd_flags)
        handle = None
        try:
            fd_stat = os.fstat(fd)
            if not stat.S_ISREG(fd_stat.st_mode):
                raise OSError(_("refusing to open non-regular file: {path}").format(path=path))
            if flags & os.O_TRUNC:
                os.ftruncate(fd, 0)
            return fd
        except Exception:
            os.close(fd)
            raise
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def _open_append_binary(path: Path, *, create_mode: int | None = None):
    mode = 0o666 if create_mode is None else create_mode
    fd = _open_no_follow(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    return os.fdopen(fd, "ab")


@contextmanager
def open_text_no_follow(
    path: str | Path,
    mode: str,
    *,
    encoding: str = "utf-8",
    create_mode: int = 0o600,
    newline: str | None = None,
) -> Iterator[TextIO]:
    target = Path(path)
    if mode == "r":
        flags = os.O_RDONLY
    elif mode == "a":
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        target.parent.mkdir(parents=True, exist_ok=True)
    elif mode == "w":
        flags = os.O_WRONLY | os.O_TRUNC | os.O_CREAT
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        raise ValueError("mode must be one of 'r', 'a', or 'w'")

    fd = _open_no_follow(target, flags, create_mode)
    try:
        handle = cast(TextIO, os.fdopen(fd, mode, encoding=encoding, newline=newline))
    except Exception:
        os.close(fd)
        raise
    with handle:
        yield handle


def write_text_no_follow(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    create_mode: int = 0o600,
    newline: str | None = None,
) -> None:
    with _path_lock(Path(path)):
        with open_text_no_follow(path, "w", encoding=encoding, create_mode=create_mode, newline=newline) as handle:
            handle.write(content)


def _open_lock_binary(path: Path):
    _reject_symlink_or_reparse_leaf(path)
    fd = _open_no_follow(path, os.O_RDWR | os.O_CREAT, 0o600)
    return os.fdopen(fd, "a+b")


def safe_replace(src: str | Path, dst: str | Path, *, attempts: int = 3, delay: float = 0.05) -> None:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))
        except OSError as exc:
            if exc.errno != getattr(os, "EXDEV", 18):
                raise
            _copy_replace_across_devices(Path(src), Path(dst), attempts=attempts, delay=delay)
            return


def _copy_replace_across_devices(src: Path, dst: Path, *, attempts: int, delay: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{dst.name}.",
        suffix=".tmp",
        dir=dst.parent,
        delete=False,
    )
    tmp_path = Path(handle.name)
    handle.close()
    try:
        shutil.copy2(src, tmp_path)
        try:
            with tmp_path.open("rb") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass
        safe_replace(tmp_path, dst, attempts=attempts, delay=delay)
        fsync_parent_dir(dst)
        src.unlink()
    except Exception:
        with suppress(OSError):
            tmp_path.unlink()
        raise


def fsync_parent_dir(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            return
    finally:
        os.close(fd)


def _state_file_kind(path: Path) -> str:
    parts = set(path.parts)
    if "execution-control" in parts:
        return "execution_control"
    if path.name in {".backup-state.json", "boundary.json"} or parts & {"session-backup-coordinator", ".pending"}:
        return "backup"
    if parts & {"permission-waits", "permission-checkpoints"}:
        return "permission"
    if path.name == "task.json" or path.parent.name == "tasks":
        return "task"
    if path.name == "context.json" or path.parent.name == "contexts":
        return "context"
    if path.name in {"snapshot.json", "a2a-snapshot.json"} and "a2a" in parts:
        return "pipeline_snapshot"
    if path.name == "metadata.json":
        return "session_metadata"
    return "other"


def atomic_write_bytes(
    path: str | Path,
    content: bytes,
    *,
    durable: bool = True,
    replace_attempts: int = 3,
    _safe_replace: Callable[[str | Path, str | Path], None] | None = None,
    _published_signature: list[tuple[int, ...]] | None = None,
) -> None:
    kind = _state_file_kind(Path(path))
    timing = current_timing()
    started = time.perf_counter_ns() if timing is not None and timing.active else None
    fsync_ms = 0.0
    succeeded = False
    with timing_phase("state_file_write", file_kind=kind):
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    if durable:
                        sync_started = time.perf_counter_ns() if started is not None else None
                        try:
                            os.fsync(handle.fileno())
                        finally:
                            if sync_started is not None:
                                fsync_ms += (time.perf_counter_ns() - sync_started) / 1_000_000
                    written = os.fstat(handle.fileno()) if _published_signature is not None else None
                if _safe_replace is None:
                    safe_replace(tmp_path, target, attempts=replace_attempts)
                else:
                    _safe_replace(tmp_path, target)
                if durable:
                    sync_started = time.perf_counter_ns() if started is not None else None
                    try:
                        fsync_parent_dir(target)
                    finally:
                        if sync_started is not None:
                            fsync_ms += (time.perf_counter_ns() - sync_started) / 1_000_000
                if written is not None and _published_signature is not None:
                    try:
                        published = target.lstat()

                        # Atomic concurrent replacements have a different inode. An
                        # edited/deleted file must not inherit this write's cache proof.
                        def identity(info: os.stat_result) -> tuple[int, ...]:
                            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

                        if identity(published) == identity(written):
                            _published_signature.append(_snapshot_signature(published))
                    except OSError:
                        pass
            except Exception:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                raise

            succeeded = True
        finally:
            if timing is not None and started is not None:
                timing.record_state_io(
                    kind,
                    elapsed_ms=(time.perf_counter_ns() - started) / 1_000_000,
                    fsync_ms=fsync_ms,
                    succeeded=succeeded,
                )


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    durable: bool = True,
    replace_attempts: int = 3,
    _safe_replace: Callable[[str | Path, str | Path], None] | None = None,
    _published_signature: list[tuple[int, ...]] | None = None,
) -> None:
    atomic_write_bytes(
        path,
        content.encode(encoding),
        durable=durable,
        replace_attempts=replace_attempts,
        _safe_replace=_safe_replace,
        _published_signature=_published_signature,
    )


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    durable: bool = True,
    replace_attempts: int = 3,
) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    atomic_write_text(path, content, durable=durable, replace_attempts=replace_attempts)


def _snapshot_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _remember_snapshot(target: Path, digest: bytes, signature: tuple[int, ...]) -> None:
    with _SNAPSHOT_GUARD:
        _SNAPSHOT_WRITES[target] = (digest, signature)
        while len(_SNAPSHOT_WRITES) > 256:
            _SNAPSHOT_WRITES.popitem(last=False)


def write_snapshot_text(path: str | Path, content: str) -> None:
    """Skip only an unchanged file this process has already durably written.

    For task/context mirrors, not execution fences, permissions or commit markers.
    Replacements, external edits, deletion and process restarts force a fresh write.
    Retain digests and file identities only, bounded independently of session size.
    """
    target = Path(path).absolute()
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    with _path_lock(target):
        with _SNAPSHOT_GUARD:
            previous = _SNAPSHOT_WRITES.pop(target, None)
        if previous is not None and previous[0] == digest:
            try:
                info = target.lstat()
                signature = _snapshot_signature(info) if stat.S_ISREG(info.st_mode) else None
            except OSError:
                signature = None
            if signature is not None and previous[1] == signature:
                _remember_snapshot(target, digest, signature)
                if timing := current_timing():
                    timing.record_state_io(_state_file_kind(target), skipped=True)
                return
        published: list[tuple[int, ...]] = []
        atomic_write_text(target, content, durable=True, _published_signature=published)
        if not published:
            return
        _remember_snapshot(target, digest, published[0])


@contextmanager
def cross_process_append_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_lock_binary(lock_path) as lock_file:
        if sys.platform == "win32":
            import msvcrt

            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise RuntimeError(f"could not acquire append lock for {path}") from exc
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise RuntimeError(f"could not acquire append lock for {path}") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def cross_process_file_lock(lock_path: Path) -> Iterator[None]:
    """Acquire the exact cross-process lock file supplied by the caller.

    Unlike :func:`cross_process_append_lock`, this helper does not derive a
    sibling name.  Recovery protocols that define a stable lock path can use
    it without accidentally creating ``..lock.lock`` files.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_lock_binary(lock_path) as lock_file:
        if sys.platform == "win32":
            import msvcrt

            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise RuntimeError(f"could not acquire file lock {lock_path}") from exc
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise RuntimeError(f"could not acquire file lock {lock_path}") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


_cross_process_append_lock = cross_process_append_lock


def append_jsonl_locked(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    durable: bool = False,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for record in records
    ]
    if not lines:
        return
    with _path_lock(target):
        with cross_process_append_lock(target):
            created = not target.exists()
            with _open_append_binary(target) as handle:
                for line in lines:
                    handle.write(line.encode("utf-8"))
                handle.flush()
                if durable:
                    os.fsync(handle.fileno())
            if durable and created:
                fsync_parent_dir(target)


def _rotate_jsonl_files(target: Path, *, max_file_bytes: int, max_files: int, pending_bytes: int = 0) -> None:
    if max_file_bytes <= 0 or max_files <= 0:
        return
    if not target.exists():
        return
    current_size = target.stat().st_size
    if pending_bytes > 0:
        if current_size + pending_bytes <= max_file_bytes:
            return
    elif current_size < max_file_bytes:
        return
    oldest = target.with_name(f"{target.name}.{max_files}")
    if oldest.exists():
        oldest.unlink()
    for index in range(max_files - 1, 0, -1):
        current = target.with_name(f"{target.name}.{index}")
        if current.exists():
            current.replace(target.with_name(f"{target.name}.{index + 1}"))
    target.replace(target.with_name(f"{target.name}.1"))


def append_jsonl_rotating_locked(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    max_file_bytes: int,
    max_files: int,
    durable: bool = False,
    create_mode: int | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for record in records
    ]
    if not lines:
        return
    with _path_lock(target):
        with cross_process_append_lock(target):
            try:
                pending_bytes = sum(len(line.encode("utf-8")) for line in lines)
                _rotate_jsonl_files(
                    target,
                    max_file_bytes=max_file_bytes,
                    max_files=max_files,
                    pending_bytes=pending_bytes,
                )
            except OSError as exc:
                logger.warning("Could not rotate JSONL file %s; appending to active file: %s", target, exc)
            created = not target.exists()
            with _open_append_binary(target, create_mode=create_mode) as handle:
                for line in lines:
                    handle.write(line.encode("utf-8"))
                handle.flush()
                if durable:
                    os.fsync(handle.fileno())
            if durable and created:
                fsync_parent_dir(target)
