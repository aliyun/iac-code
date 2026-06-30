"""Durable state-file I/O helpers for recovery-critical files."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Iterator

from iac_code.utils.path_locks import PathLockRegistry

logger = logging.getLogger(__name__)
_PATH_LOCKS = PathLockRegistry()


def _path_lock(path: Path):
    return _PATH_LOCKS.lock_for(path)


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


def atomic_write_bytes(
    path: str | Path,
    content: bytes,
    *,
    durable: bool = True,
    replace_attempts: int = 3,
    _safe_replace: Callable[[str | Path, str | Path], None] | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        if _safe_replace is None:
            safe_replace(tmp_path, target, attempts=replace_attempts)
        else:
            _safe_replace(tmp_path, target)
        if durable:
            fsync_parent_dir(target)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    durable: bool = True,
    replace_attempts: int = 3,
    _safe_replace: Callable[[str | Path, str | Path], None] | None = None,
) -> None:
    atomic_write_bytes(
        path,
        content.encode(encoding),
        durable=durable,
        replace_attempts=replace_attempts,
        _safe_replace=_safe_replace,
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


@contextmanager
def _cross_process_append_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
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
        with _cross_process_append_lock(target):
            created = not target.exists()
            with target.open("ab") as handle:
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
        with _cross_process_append_lock(target):
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


def _open_append_binary(path: Path, *, create_mode: int | None = None):
    if create_mode is None:
        return path.open("ab")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, create_mode)
    return os.fdopen(fd, "ab")
