"""Descriptor-relative file updates for request-scoped, cwd-only writes."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO, cast

from iac_code.i18n import translate_message

_HAS_SAFE_DIR_FD = (
    os.name == "posix"
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and bool(getattr(os, "O_DIRECTORY", 0))
    and {os.open, os.mkdir, os.stat, os.unlink, os.rename}.issubset(os.supports_dir_fd)
)


def supported() -> bool:
    return _HAS_SAFE_DIR_FD


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


@contextmanager
def _text_stream(descriptor: int, mode: str, *, newline: str | None = None) -> Iterator[TextIO]:
    try:
        stream = cast(TextIO, os.fdopen(descriptor, mode, encoding="utf-8", newline=newline))
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


@dataclass
class SafeWriteTarget:
    directory: int
    name: str
    display_path: str
    language: str
    directories: list[tuple[int, str, int]]
    root: Path
    root_fd: int
    original: os.stat_result | None

    def _unsafe(self) -> ValueError:
        return ValueError(
            translate_message(
                "Safe writes do not support symbolic links, hard links, or non-regular files: {path}",
                language=self.language,
            ).format(path=self.display_path)
        )

    def _changed(self) -> ValueError:
        return ValueError(
            translate_message(
                "The file or its directory changed during the write. Retry the operation.",
                language=self.language,
            )
        )

    def _file_stat(self, name: str) -> os.stat_result | None:
        try:
            value = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise self._unsafe()
        if not value.st_mode & 0o222:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), self.display_path)
        return value

    def _verify(self) -> None:
        try:
            root = os.stat(self.root, follow_symlinks=False)
            opened = os.fstat(self.root_fd)
            if (root.st_dev, root.st_ino) != (opened.st_dev, opened.st_ino):
                raise self._changed()
            for parent, name, child in self.directories:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                opened = os.fstat(child)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise self._changed()
            current = self._file_stat(self.name)
            if (None if current is None else _identity(current)) != (
                None if self.original is None else _identity(self.original)
            ):
                raise self._changed()
        except FileNotFoundError as error:
            raise self._changed() from error

    def read_text(self) -> str:
        self._verify()
        descriptor = os.open(
            self.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=self.directory,
        )
        with _text_stream(descriptor, "r") as stream:
            current = os.fstat(stream.fileno())
            if self.original is None or _identity(current) != _identity(self.original):
                raise self._changed()
            return stream.read()

    def write_text(self, content: str, *, newline: str | None) -> None:
        self._verify()
        temporary = ".iac-write-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory,
        )
        try:
            with _text_stream(descriptor, "w", newline=newline) as stream:
                stream.write(content)
                stream.flush()
                if self.original is not None:
                    os.fchmod(stream.fileno(), stat.S_IMODE(self.original.st_mode) & 0o777)
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
            self._verify()
            current = self._file_stat(temporary)
            if current is None or _identity(current) != _identity(written):
                raise self._changed()
            os.replace(temporary, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self.directory)


@contextmanager
def safe_write_target(
    path: str,
    *,
    cwd: str,
    language: str,
    create_parents: bool = False,
    missing_ok: bool = False,
) -> Iterator[SafeWriteTarget | None]:
    """Keep every parent descriptor open, rejecting links before any file writes."""
    if not supported():
        raise ValueError(translate_message("Safe file writes are not supported on this platform.", language=language))
    root = Path(os.path.realpath(cwd))
    candidate = Path(os.path.expanduser(path))
    relative = candidate
    if candidate.is_absolute():
        for base in (Path(os.path.abspath(cwd)), root):
            try:
                relative = candidate.relative_to(base)
                break
            except ValueError:
                continue
        else:
            raise ValueError(
                translate_message("Files can only be written under {cwd}.", language=language).format(cwd="./")
            )
    parts = relative.parts
    if not parts or ".." in parts:
        raise ValueError(
            translate_message("Files can only be written under {cwd}.", language=language).format(cwd="./")
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        root_fd = os.open(root, flags)
        stack.callback(os.close, root_fd)
        parent = root_fd
        directories = []
        try:
            for name in parts[:-1]:
                try:
                    child = os.open(name, flags, dir_fd=parent)
                except FileNotFoundError:
                    if not create_parents:
                        if missing_ok:
                            yield None
                            return
                        raise
                    try:
                        os.mkdir(name, dir_fd=parent)
                    except FileExistsError:
                        pass
                    child = os.open(name, flags, dir_fd=parent)
                stack.callback(os.close, child)
                directories.append((parent, name, child))
                parent = child
        except OSError as error:
            if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                raise
            raise ValueError(
                translate_message(
                    "Safe writes do not support symbolic links, hard links, or non-regular files: {path}",
                    language=language,
                ).format(path=path)
            ) from error
        target = SafeWriteTarget(parent, parts[-1], path, language, directories, root, root_fd, None)
        target.original = target._file_stat(target.name)
        yield target


def validate_safe_write_path(path: str, *, cwd: str, language: str) -> None:
    with safe_write_target(path, cwd=cwd, language=language, missing_ok=True):
        pass
