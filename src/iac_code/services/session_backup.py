"""Session directory backup mirroring."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable

from iac_code.i18n import _
from iac_code.services.session_layout import (
    UnsupportedSessionLayoutError,
    is_supported_session_dir_for_id,
    require_supported_session_layout,
)
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SESSION_METADATA_FILENAME
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.path_components import is_unsafe_windows_path_component
from iac_code.utils.project_paths import project_dir_candidates
from iac_code.utils.public_errors import sanitize_public_text
from iac_code.utils.state_io import atomic_write_json, fsync_parent_dir, safe_replace

BACKUP_ENV_VAR = "IAC_CODE_CONFIG_BACKUP_DIR"
BACKUP_STATE_FILENAME = ".backup-state.json"
BACKUP_LOCK_FILENAME = ".backup-lock"
_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_PUBLIC_BACKUP_PATH_PATTERN = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9])
    (?:
        [A-Za-z]:[\\/][^\r\n,;:)\"']*
        |\\\\[^\r\n,;:)\"']*
        |/[^\r\n,;:)\"']*
    )
    """
)


class BackupReason(str, Enum):
    NORMAL_TURN_END = "normal_turn_end"
    PIPELINE_STEP_COMPLETED = "pipeline_step_completed"
    OPTION_SELECTION = "option_selection"
    INPUT_REQUIRED = "input_required"
    WAITING_INPUT = "waiting_input"
    TERMINAL = "terminal"
    HANDOFF_READY = "handoff_ready"


@dataclass(frozen=True)
class BackupResult:
    enabled: bool
    source: Path | None = None
    destination: Path | None = None
    copied_files: int = 0
    deleted_files: int = 0
    succeeded: bool = True
    error: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class SessionRestoreResult:
    enabled: bool
    restored: bool = False
    source: Path | None = None
    destination: Path | None = None
    copied_files: int = 0
    deleted_files: int = 0
    error: str | None = None


class SessionBackupError(Exception):
    """Base session backup error."""


class SessionBackupBlocked(SessionBackupError):  # noqa: N818 - public API name required by backup callers.
    """Raised when a critical backup cannot complete."""

    def __init__(
        self,
        message: str,
        *,
        retry_count: int = 0,
        result: BackupResult | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_count = retry_count if retry_count >= 0 else 0
        self.result = result


class SessionBackupService:
    def __init__(
        self,
        session_storage: SessionStorage | None = None,
        retry_delays: Iterable[float] = (0.25, 1.0),
    ) -> None:
        self._session_storage = session_storage or SessionStorage()
        self._retry_delays = tuple(retry_delays)

    def backup_session(
        self,
        cwd: str,
        session_id: str,
        *,
        reason: BackupReason,
        critical: bool,
    ) -> BackupResult:
        if not self._backup_enabled():
            return BackupResult(enabled=False)
        self._validate_session_id(session_id)

        try:
            source = self._source_for_backup(cwd, session_id)
        except (UnsupportedSessionLayoutError, SessionBackupError) as exc:
            public_error = self._public_error_text(exc)
            if critical:
                raise SessionBackupBlocked(public_error, retry_count=0) from None
            return BackupResult(enabled=True, succeeded=False, error=public_error)
        if source is None:
            if critical:
                raise SessionBackupBlocked(_("Session backup requires a supported session layout."), retry_count=0)
            return BackupResult(enabled=False)
        delays = (0, *self._retry_delays)
        last_error: Exception | None = None
        last_public_error: str | None = None
        destination: Path | None = None

        for attempt, delay in enumerate(delays):
            if attempt:
                time.sleep(delay)
            failed_marker_written = False
            source_verified = False
            try:
                self._resolve_real_source(source)
                source_verified = True
                with self._session_backup_lock(source):
                    try:
                        backup_root = self._backup_root()
                        if backup_root is None:
                            return BackupResult(enabled=False)
                        destination = backup_root / "projects" / source.parent.name / session_id
                        self._validate_mirror_paths(source, destination, backup_root)
                        self._ensure_private_dir(backup_root)
                        result = self._mirror(source, destination)
                        self._write_marker(source, reason=reason, status="succeeded", error=None)
                    except Exception as exc:
                        failed_marker_written = True
                        public_error = self._public_error_text(exc)
                        self._try_write_failed_marker(
                            source,
                            reason=reason,
                            error=public_error,
                            attempt=attempt + 1,
                            retry_count=attempt,
                            exhausted=attempt == len(delays) - 1,
                        )
                        raise
            except Exception as exc:
                last_error = exc
                last_public_error = self._public_error_text(exc)
                if source_verified and not failed_marker_written:
                    self._try_write_failed_marker(
                        source,
                        reason=reason,
                        error=last_public_error,
                        attempt=attempt + 1,
                        retry_count=attempt,
                        exhausted=attempt == len(delays) - 1,
                    )
                continue
            return replace(result, retry_count=attempt)

        failure_result = BackupResult(
            enabled=True,
            source=source,
            destination=destination,
            succeeded=False,
            error=last_public_error,
            retry_count=max(0, len(delays) - 1),
        )
        if critical and last_error is not None:
            raise SessionBackupBlocked(
                last_public_error or self._public_error_text(last_error),
                retry_count=failure_result.retry_count,
                result=failure_result,
            ) from None
        return failure_result

    def restore_session(self, cwd: str, session_id: str) -> SessionRestoreResult:
        if not self._backup_enabled():
            return SessionRestoreResult(enabled=False)
        self._validate_session_id(session_id)
        backup_root = self._backup_root()
        if backup_root is None:
            return SessionRestoreResult(enabled=False)

        destination = Path(self._session_storage.session_dir(cwd, session_id))
        if self._session_exists(cwd, session_id):
            return SessionRestoreResult(enabled=True, restored=False, destination=destination)

        source = self._source_for_restore(cwd, session_id, backup_root)
        if source is None:
            return SessionRestoreResult(enabled=True, restored=False, destination=destination)

        with self._session_restore_lock(destination):
            destination = Path(self._session_storage.session_dir(cwd, session_id))
            if self._session_exists(cwd, session_id):
                return SessionRestoreResult(enabled=True, restored=False, source=source, destination=destination)
            source = self._source_for_restore(cwd, session_id, backup_root)
            if source is None:
                return SessionRestoreResult(enabled=True, restored=False, destination=destination)
            self._validate_restore_paths(source, destination, backup_root)
            self._reject_existing_restore_destination(destination)
            result = self._mirror(source, destination)
            self._validate_restored_session(destination, session_id)
            return SessionRestoreResult(
                enabled=True,
                restored=True,
                source=source,
                destination=destination,
                copied_files=result.copied_files,
                deleted_files=result.deleted_files,
            )

    def _source_for_backup(self, cwd: str, session_id: str) -> Path | None:
        v2_session_dir = getattr(self._session_storage, "v2_session_dir", None)
        if callable(v2_session_dir):
            explicit_source = v2_session_dir(cwd, session_id)
            if explicit_source is not None:
                return Path(explicit_source)
            fallback_source = Path(self._session_storage.session_dir(cwd, session_id))
            if (
                fallback_source.is_symlink()
                or self._is_reparse_point(fallback_source)
                or (fallback_source.exists() and not fallback_source.is_dir())
            ):
                self._resolve_real_source(fallback_source)
            return None

        legacy_session_path = getattr(self._session_storage, "legacy_session_path", None)
        if callable(legacy_session_path) and Path(legacy_session_path(cwd, session_id)).exists():
            return None

        source = self._session_storage.session_dir(cwd, session_id)
        if (source / SESSION_METADATA_FILENAME).exists():
            return None
        return source

    def _source_for_restore(self, cwd: str, session_id: str, backup_root: Path) -> Path | None:
        projects_root = backup_root / "projects"
        for project_dir in project_dir_candidates(cwd, projects_root):
            candidate = project_dir / session_id
            if not candidate.exists():
                continue
            if not is_supported_session_dir_for_id(candidate, session_id):
                continue
            layout_version = require_supported_session_layout(candidate)
            if layout_version != SESSION_LAYOUT_VERSION_V2:
                raise UnsupportedSessionLayoutError(
                    _("Unsupported session layout version: {version}").format(version=layout_version)
                )
            return candidate
        return None

    def _backup_root(self) -> Path | None:
        raw = os.environ.get(BACKUP_ENV_VAR, "").strip()
        if not raw:
            return None
        backup_root = Path(os.path.expandvars(os.path.expanduser(raw)))
        self._validate_backup_root(raw, backup_root)
        return backup_root

    def _validate_backup_root(self, raw: str, backup_root: Path) -> None:
        if not backup_root.is_absolute():
            raise SessionBackupError(_("backup root must be an absolute directory: {path}").format(path=raw))
        if backup_root == Path(backup_root.anchor):
            raise SessionBackupError(_("backup root must not be a filesystem root: {path}").format(path=raw))
        windows_path = PureWindowsPath(raw)
        if windows_path.drive and not windows_path.is_absolute():
            raise SessionBackupError(_("backup root must not be drive-relative: {path}").format(path=raw))
        if windows_path.anchor and windows_path == PureWindowsPath(windows_path.anchor):
            raise SessionBackupError(_("backup root must not be a filesystem root: {path}").format(path=raw))

    def _mirror(self, source: Path, destination: Path) -> BackupResult:
        self._resolve_real_source(source)
        if (
            destination.is_symlink()
            or self._is_reparse_point(destination)
            or (destination.exists() and not destination.is_dir())
        ):
            self._unlink(destination)
            self._fsync_parent_dir(destination)
        self._ensure_private_dir(destination)
        copied_files = 0
        deleted_files = 0
        included_files: set[Path] = set()

        for path in self._iter_tree(source):
            if path.is_symlink():
                continue
            relative = path.relative_to(source)
            if not self._included(relative):
                continue
            target = destination / relative
            if path.is_dir():
                self._remove_conflicting_file(destination, target)
                self._ensure_private_dir(target)
                continue
            if not self._is_regular_file(path):
                continue
            included_files.add(relative)
            self._remove_conflicting_non_file(destination, target)
            if self._needs_copy(path, target):
                self._copy_file(path, target)
                copied_files += 1

        stale_paths = sorted(
            (
                p
                for p in self._iter_tree(destination, include_reparse=True)
                if p.is_symlink() or self._is_reparse_point(p) or not p.is_dir()
            ),
            reverse=True,
        )
        for path in stale_paths:
            relative = path.relative_to(destination)
            if relative not in included_files:
                self._unlink(path)
                self._fsync_parent_dir(path)
                deleted_files += 1

        self._prune_empty_dirs(destination)
        return BackupResult(
            enabled=True,
            source=source,
            destination=destination,
            copied_files=copied_files,
            deleted_files=deleted_files,
        )

    @staticmethod
    def _backup_enabled() -> bool:
        return bool(os.environ.get(BACKUP_ENV_VAR, "").strip())

    @classmethod
    def is_safe_session_id(cls, session_id: str) -> bool:
        try:
            cls._validate_session_id(session_id)
        except SessionBackupError:
            return False
        return True

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if (
            not session_id
            or session_id in {".", ".."}
            or Path(session_id).is_absolute()
            or "/" in session_id
            or "\\" in session_id
            or ".." in session_id
            or _SAFE_SESSION_ID_PATTERN.fullmatch(session_id) is None
            or is_unsafe_windows_path_component(session_id)
        ):
            raise SessionBackupError(_("unsafe session_id: {session_id!r}").format(session_id=session_id))

    def _validate_mirror_paths(self, source: Path, destination: Path, backup_root: Path) -> None:
        source_resolved = self._resolve_real_source(source)
        backup_root_resolved = self._resolve_backup_root(backup_root)
        self._reject_symlinked_destination_ancestry(backup_root, destination)
        destination_resolved = self._resolve_planned_child(destination.parent, destination.name)

        if (
            self._path_equal_or_under(backup_root_resolved, source_resolved)
            or self._path_equal_or_under(
                source_resolved,
                backup_root_resolved,
            )
            or self._path_equal_or_under(
                destination_resolved,
                source_resolved,
            )
            or self._path_equal_or_under(
                source_resolved,
                destination_resolved,
            )
        ):
            raise SessionBackupError(
                _("backup destination overlaps session source: {destination}").format(destination=destination)
            )
        if not self._path_equal_or_under(destination_resolved, backup_root_resolved):
            raise SessionBackupError(
                _("backup destination resolves outside backup root: {destination}").format(destination=destination)
            )
        self._validate_physical_mirror_paths(source, destination, backup_root)

    def _validate_restore_paths(self, source: Path, destination: Path, backup_root: Path) -> None:
        source_resolved = self._resolve_real_source(source)
        backup_root_resolved = self._resolve_restore_backup_root(backup_root)
        self._reject_existing_symlink_ancestry(destination.parent, label="restore destination", include_leaf=True)
        destination_parent_resolved = self._resolve_real_dir(destination.parent, "restore destination")
        destination_resolved = destination_parent_resolved / destination.name

        if not self._path_equal_or_under(source_resolved, backup_root_resolved):
            raise SessionBackupError("restore source resolves outside backup root: {source}".format(source=source))
        if (
            self._path_equal_or_under(destination_resolved, source_resolved)
            or self._path_equal_or_under(source_resolved, destination_resolved)
            or self._path_equal_or_under(destination_resolved, backup_root_resolved)
            or self._path_equal_or_under(backup_root_resolved, destination_resolved)
        ):
            raise SessionBackupError(
                "restore destination overlaps backup source: {destination}".format(destination=destination)
            )
        self._validate_physical_restore_paths(source, destination, backup_root)

    def _validate_physical_restore_paths(self, source: Path, destination: Path, backup_root: Path) -> None:
        if os.name != "nt":
            return
        source_physical = self._physical_path_text(source)
        backup_root_physical = self._physical_path_text(backup_root)
        destination_physical = self._physical_path_text(destination)
        if not self._path_text_equal_or_under(source_physical, backup_root_physical):
            raise SessionBackupError("restore source resolves outside backup root: {source}".format(source=source))
        if (
            self._path_text_equal_or_under(destination_physical, source_physical)
            or self._path_text_equal_or_under(source_physical, destination_physical)
            or self._path_text_equal_or_under(destination_physical, backup_root_physical)
            or self._path_text_equal_or_under(backup_root_physical, destination_physical)
        ):
            raise SessionBackupError(
                "restore destination overlaps backup source: {destination}".format(destination=destination)
            )

    def _reject_existing_restore_destination(self, destination: Path) -> None:
        if destination.is_symlink() or self._is_reparse_point(destination) or destination.exists():
            raise SessionBackupError(
                "restore destination already exists: {destination}".format(destination=destination)
            )

    def _validate_restored_session(self, destination: Path, session_id: str) -> None:
        layout_version = require_supported_session_layout(destination)
        if layout_version != SESSION_LAYOUT_VERSION_V2:
            raise UnsupportedSessionLayoutError(
                _("Unsupported session layout version: {version}").format(version=layout_version)
            )
        if not is_supported_session_dir_for_id(destination, session_id):
            raise SessionBackupError("restored session metadata does not match session_id")

    def _validate_physical_mirror_paths(self, source: Path, destination: Path, backup_root: Path) -> None:
        if os.name != "nt":
            return
        source_physical = self._physical_path_text(source)
        backup_root_physical = self._physical_path_text(backup_root)
        destination_physical = self._physical_path_text(destination)
        if (
            self._path_text_equal_or_under(backup_root_physical, source_physical)
            or self._path_text_equal_or_under(source_physical, backup_root_physical)
            or self._path_text_equal_or_under(destination_physical, source_physical)
            or self._path_text_equal_or_under(source_physical, destination_physical)
        ):
            raise SessionBackupError(
                _("backup destination overlaps session source: {destination}").format(destination=destination)
            )

    def _physical_path_text(self, path: Path) -> str:
        if os.name != "nt":
            return self._canonical_windows_path_text(path.resolve(strict=False))
        return self._windows_physical_path_text(path)

    def _windows_physical_path_text(self, path: Path) -> str:
        missing_parts: list[str] = []
        current = path
        while not current.exists():
            parent = current.parent
            if parent == current:
                return self._canonical_windows_path_text(path.resolve(strict=False))
            missing_parts.append(current.name)
            current = parent

        base = self._windows_existing_physical_path_text(current)
        for part in reversed(missing_parts):
            base = base.rstrip("\\/") + "\\" + part
        return self._canonical_windows_path_text(base)

    @staticmethod
    def _windows_existing_physical_path_text(path: Path) -> str:
        try:
            import ctypes
        except ImportError:
            return str(path.resolve(strict=False))

        windll_factory: Any = getattr(ctypes, "WinDLL", None)
        if windll_factory is None:
            return str(path.resolve(strict=False))
        kernel32 = windll_factory("kernel32", use_last_error=True)
        from ctypes import wintypes

        file_read_attributes = 0x0080
        file_share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
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
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        invalid_handle = ctypes.c_void_p(-1).value
        handle = kernel32.CreateFileW(
            str(path),
            file_read_attributes,
            file_share_all,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )
        if handle == invalid_handle:
            return str(path.resolve(strict=False))
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if 0 < length < len(buffer):
                return buffer.value
            return str(path.resolve(strict=False))
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _canonical_windows_path_text(path: Path | str) -> str:
        text = str(path).replace("/", "\\").rstrip("\\")
        text_casefold = text.casefold()
        unc_prefix = "\\\\?\\unc\\"
        if text_casefold.startswith(unc_prefix):
            text = "\\\\" + text[len(unc_prefix) :]
        elif text_casefold.startswith("\\\\?\\"):
            text = text[4:]
        return text.casefold()

    @staticmethod
    def _path_text_equal_or_under(path: str, root: str) -> bool:
        if not path or not root:
            return False
        return path == root or path.startswith(root + "\\")

    def _reject_symlinked_destination_ancestry(self, backup_root: Path, destination: Path) -> None:
        try:
            relative_parent = destination.parent.relative_to(backup_root)
        except ValueError as exc:
            raise SessionBackupError(
                _("backup destination is not under backup root: {destination}").format(destination=destination)
            ) from exc

        current = backup_root
        for part in relative_parent.parts:
            current /= part
            if current.is_symlink() or self._is_reparse_point(current):
                raise SessionBackupError(_("backup destination ancestry contains symlink: {path}").format(path=current))

    def _reject_existing_symlink_ancestry(self, path: Path, *, label: str, include_leaf: bool) -> None:
        current = path if include_leaf else path.parent
        candidates: list[Path] = []
        while True:
            candidates.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent

        for candidate in reversed(candidates):
            if candidate.is_symlink() or self._is_reparse_point(candidate):
                raise SessionBackupError(
                    _("{label} ancestry contains symlink: {path}").format(label=label, path=candidate)
                )

    def _resolve_real_source(self, source: Path) -> Path:
        self._reject_session_source_ancestry(source)
        if source.is_symlink() or self._is_reparse_point(source):
            raise SessionBackupError(_("session source is a symlink: {source}").format(source=source))
        if not source.is_dir():
            raise SessionBackupError(_("session source is not a directory: {source}").format(source=source))
        try:
            return source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SessionBackupError(_("session source cannot be resolved: {source}").format(source=source)) from exc

    def _reject_session_source_ancestry(self, source: Path) -> None:
        candidates = [source]
        project_dir = source.parent
        if project_dir != source:
            candidates.append(project_dir)
            projects_dir = project_dir.parent
            if projects_dir != project_dir:
                candidates.append(projects_dir)
        for candidate in reversed(candidates):
            if candidate.is_symlink() or self._is_reparse_point(candidate):
                raise SessionBackupError(
                    _("{label} ancestry contains symlink: {path}").format(label=_("session source"), path=candidate)
                )

    @staticmethod
    def _resolve_real_dir(path: Path, label: str) -> Path:
        if path.is_symlink():
            raise SessionBackupError(_("{label} is a symlink: {path}").format(label=label, path=path))
        if not path.is_dir():
            raise SessionBackupError(_("{label} is not a directory: {path}").format(label=label, path=path))
        try:
            return path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SessionBackupError(_("{label} cannot be resolved: {path}").format(label=label, path=path)) from exc

    @staticmethod
    def _resolve_planned_child(parent: Path, child_name: str) -> Path:
        try:
            return parent.resolve(strict=False) / child_name
        except (OSError, RuntimeError) as exc:
            raise SessionBackupError(
                _("backup destination cannot be resolved: {destination}").format(destination=parent / child_name)
            ) from exc

    @staticmethod
    def _resolve_backup_root(path: Path) -> Path:
        try:
            return path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SessionBackupError(
                _("{label} cannot be resolved: {path}").format(label=_("backup root"), path=path)
            ) from exc

    def _resolve_restore_backup_root(self, path: Path) -> Path:
        resolved = self._resolve_backup_root(path)
        if not resolved.is_dir():
            raise SessionBackupError(_("{label} is not a directory: {path}").format(label=_("backup root"), path=path))
        return resolved

    @staticmethod
    def _path_equal_or_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @contextmanager
    def _session_backup_lock(self, source: Path):
        if source.is_symlink() or self._is_reparse_point(source):
            raise SessionBackupError(_("session source is a symlink: {source}").format(source=source))
        if not source.is_dir():
            yield
            return

        lock_path = source / BACKUP_LOCK_FILENAME
        if lock_path.is_symlink() or self._is_reparse_point(lock_path):
            raise SessionBackupError(_("backup lock is a symlink: {path}").format(path=lock_path))
        if lock_path.exists() and not lock_path.is_file():
            raise SessionBackupError(_("backup lock is not a regular file: {path}").format(path=BACKUP_LOCK_FILENAME))
        with self._open_backup_lock_file(lock_path) as lock_file:
            ensure_private_file(lock_path)
            if os.name == "nt":
                import msvcrt

                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                except OSError as exc:
                    raise SessionBackupError(
                        _("could not acquire backup lock for {source}").format(source=source)
                    ) from exc
                try:
                    yield
                finally:
                    with suppress(OSError):
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    raise SessionBackupError(
                        _("could not acquire backup lock for {source}").format(source=source)
                    ) from exc
                try:
                    yield
                finally:
                    with suppress(OSError):
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _session_restore_lock(self, destination: Path):
        lock_path = destination.parent / ".{}.restore.lock".format(destination.name)
        self._ensure_private_dir(lock_path.parent)
        if lock_path.is_symlink() or self._is_reparse_point(lock_path):
            raise SessionBackupError("restore lock is a symlink: {path}".format(path=lock_path))
        if lock_path.exists() and not lock_path.is_file():
            raise SessionBackupError("restore lock is not a regular file: {path}".format(path=lock_path.name))
        with self._open_restore_lock_file(lock_path) as lock_file:
            ensure_private_file(lock_path)
            if os.name == "nt":
                import msvcrt

                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                except OSError as exc:
                    raise SessionBackupError(
                        "could not acquire restore lock for {destination}".format(destination=destination)
                    ) from exc
                try:
                    yield
                finally:
                    with suppress(OSError):
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    raise SessionBackupError(
                        "could not acquire restore lock for {destination}".format(destination=destination)
                    ) from exc
                try:
                    yield
                finally:
                    with suppress(OSError):
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _open_backup_lock_file(self, lock_path: Path):
        if lock_path.is_symlink() or self._is_reparse_point(lock_path):
            raise SessionBackupError(_("backup lock is a symlink: {path}").format(path=lock_path))
        fd = self._open_no_follow_fd(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        return os.fdopen(fd, "a+b")

    def _open_restore_lock_file(self, lock_path: Path):
        if lock_path.is_symlink() or self._is_reparse_point(lock_path):
            raise SessionBackupError("restore lock is a symlink: {path}".format(path=lock_path))
        fd = self._open_no_follow_fd(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        return os.fdopen(fd, "a+b")

    def _copy_file(self, src: Path, dst: Path) -> None:
        self._ensure_private_dir(dst.parent)
        handle = tempfile.NamedTemporaryFile(
            prefix=".iacbk.",
            suffix=".tmp",
            dir=dst.parent,
            delete=False,
        )
        tmp = Path(handle.name)
        handle.close()
        try:
            self._copy_regular_file_no_follow(src, tmp)
            self._fsync_file(tmp)
            ensure_private_file(tmp)
            self._make_writable(dst)
            safe_replace(tmp, dst)
            self._fsync_parent_dir(dst)
            ensure_private_file(dst)
        finally:
            if tmp.exists():
                self._unlink(tmp)
                self._fsync_parent_dir(tmp)

    def _copy_regular_file_no_follow(self, src: Path, dst: Path) -> None:
        with self._open_source_file_no_follow(src) as source_file:
            with dst.open("wb") as target_file:
                shutil.copyfileobj(source_file, target_file)
                target_file.flush()
        with suppress(OSError):
            shutil.copystat(src, dst, follow_symlinks=False)

    def _open_source_file_no_follow(self, src: Path):
        if src.is_symlink() or self._is_reparse_point(src):
            raise SessionBackupError(_("session source entry is not a regular file: {source}").format(source=src))
        fd = self._open_no_follow_fd(src, os.O_RDONLY, 0)
        return os.fdopen(fd, "rb")

    def _open_no_follow_fd(self, path: Path, flags: int, mode: int) -> int:
        if path.is_symlink() or self._is_reparse_point(path):
            raise SessionBackupError(_("session source entry is not a regular file: {source}").format(source=path))
        if os.name == "nt":
            return self._open_windows_no_follow_fd(path, flags, mode)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags | nofollow, mode)
        except OSError as exc:
            raise SessionBackupError(
                _("session source entry is not a regular file: {source}").format(source=path)
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SessionBackupError(_("session source entry is not a regular file: {source}").format(source=path))
            return fd
        except Exception:
            os.close(fd)
            raise

    def _open_windows_no_follow_fd(self, path: Path, flags: int, _mode: int) -> int:
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes
        except ImportError as exc:
            raise SessionBackupError(
                _("session source entry is not a regular file: {source}").format(source=path)
            ) from exc

        windll_factory: Any = getattr(ctypes, "WinDLL", None)
        if windll_factory is None:
            raise SessionBackupError(_("session source entry is not a regular file: {source}").format(source=path))
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
            raise SessionBackupError(_("session source entry is not a regular file: {source}").format(source=path))
        try:
            file_info = _ByHandleFileInformation()
            if (
                not kernel32.GetFileInformationByHandle(handle, ctypes.byref(file_info))
                or file_info.dwFileAttributes & file_attribute_reparse_point
            ):
                raise SessionBackupError(_("session source entry is not a regular file: {source}").format(source=path))
            open_osfhandle: Any = getattr(msvcrt, "open_osfhandle", None)
            if open_osfhandle is None:
                raise OSError(_("could not open file without following reparse point: {path}").format(path=path))
            fd = open_osfhandle(handle, fd_flags)
            handle = None
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise SessionBackupError(
                        _("session source entry is not a regular file: {source}").format(source=path)
                    )
                return fd
            except Exception:
                os.close(fd)
                raise
        finally:
            if handle is not None:
                kernel32.CloseHandle(handle)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with suppress(OSError):
            with path.open("rb") as handle:
                os.fsync(handle.fileno())

    def _ensure_private_dir(self, path: Path) -> Path:
        missing_dirs = self._missing_directory_chain(path)
        ensure_private_dir(path)
        if missing_dirs:
            for created_dir in reversed(missing_dirs):
                self._fsync_parent_dir(created_dir)
        else:
            self._fsync_parent_dir(path)
        return path

    @staticmethod
    def _missing_directory_chain(path: Path) -> list[Path]:
        missing_dirs: list[Path] = []
        current = path
        while not current.exists():
            missing_dirs.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
        return missing_dirs

    @staticmethod
    def _fsync_parent_dir(path: Path) -> None:
        with suppress(OSError):
            fsync_parent_dir(path)

    def _remove_conflicting_file(self, root: Path, path: Path) -> None:
        self._require_under_root(root, path)
        if path.is_symlink() or self._is_reparse_point(path) or (path.exists() and not path.is_dir()):
            self._unlink(path)
            self._fsync_parent_dir(path)

    def _remove_conflicting_non_file(self, root: Path, path: Path) -> None:
        self._require_under_root(root, path)
        if path.is_symlink() or self._is_reparse_point(path):
            self._unlink(path)
            self._fsync_parent_dir(path)
        elif path.is_dir():
            self._rmtree(path)
            self._fsync_parent_dir(path)
        elif path.exists() and not path.is_file():
            self._unlink(path)
            self._fsync_parent_dir(path)

    @staticmethod
    def _require_under_root(root: Path, path: Path) -> None:
        path.relative_to(root)

    def _included(self, path: Path) -> bool:
        return not any(
            part in {BACKUP_STATE_FILENAME, BACKUP_LOCK_FILENAME} or self._is_hidden_lock_file(part)
            for part in path.parts
        )

    @staticmethod
    def _is_hidden_lock_file(name: str) -> bool:
        return name.startswith(".") and name.endswith(".lock")

    def _try_write_failed_marker(
        self,
        source: Path,
        *,
        reason: BackupReason,
        error: str,
        attempt: int | None = None,
        retry_count: int | None = None,
        exhausted: bool | None = None,
    ) -> None:
        if not self._marker_source_is_safe(source):
            return
        try:
            self._write_marker(
                source,
                reason=reason,
                status="failed",
                error=error,
                attempt=attempt,
                retry_count=retry_count,
                exhausted=exhausted,
            )
        except Exception:
            return

    def _write_marker(
        self,
        source: Path,
        *,
        reason: BackupReason,
        status: str,
        error: str | None,
        attempt: int | None = None,
        retry_count: int | None = None,
        exhausted: bool | None = None,
    ) -> None:
        if not self._marker_source_is_safe(source):
            raise SessionBackupError(_("session source is not a directory: {source}").format(source=source))
        marker = source / BACKUP_STATE_FILENAME
        payload: dict[str, Any] = {
            "reason": reason.value,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if status == "failed":
            if attempt is not None:
                payload["attempt"] = attempt
            if retry_count is not None:
                payload["retry_count"] = retry_count
            if exhausted is not None:
                payload["exhausted"] = exhausted
        if error is not None:
            payload["error"] = sanitize_public_text(error)[:500]
        atomic_write_json(marker, payload, durable=True)
        ensure_private_file(marker)

    @staticmethod
    def _public_error_text(exc: BaseException) -> str:
        return _PUBLIC_BACKUP_PATH_PATTERN.sub("[PATH]", sanitize_public_text(str(exc)))

    def _marker_source_is_safe(self, source: Path) -> bool:
        if source.is_symlink() or self._is_reparse_point(source) or not source.is_dir():
            return False
        try:
            self._reject_session_source_ancestry(source)
        except SessionBackupError:
            return False
        return True

    def _session_exists(self, cwd: str, session_id: str) -> bool:
        exists = getattr(self._session_storage, "exists", None)
        if callable(exists):
            return bool(exists(cwd, session_id))
        session_path = getattr(self._session_storage, "session_path", None)
        if callable(session_path):
            raw_path = session_path(cwd, session_id)
            if isinstance(raw_path, (str, Path)) and Path(raw_path).exists():
                return True
        session_dir = getattr(self._session_storage, "session_dir", None)
        if callable(session_dir):
            raw_dir = session_dir(cwd, session_id)
            if isinstance(raw_dir, (str, Path)) and Path(raw_dir).exists():
                return True
        return False

    def _prune_empty_dirs(self, root: Path) -> None:
        for path in sorted((p for p in self._iter_tree(root, include_reparse=True) if p.is_dir()), reverse=True):
            try:
                if self._is_reparse_point(path):
                    continue
                self._make_writable(path)
                path.rmdir()
            except OSError:
                continue
            self._fsync_parent_dir(path)

    @staticmethod
    def _needs_copy(src: Path, dst: Path) -> bool:
        if not dst.exists():
            return True
        src_stat = src.stat(follow_symlinks=False)
        dst_stat = dst.stat(follow_symlinks=False)
        return src_stat.st_size != dst_stat.st_size or src_stat.st_mtime_ns != dst_stat.st_mtime_ns

    def _is_regular_file(self, path: Path) -> bool:
        if path.is_symlink() or self._is_reparse_point(path):
            return False
        try:
            return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
        except OSError:
            return False

    def _iter_tree(self, root: Path, *, include_reparse: bool = False) -> Iterable[Path]:
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.iterdir():
                if self._is_reparse_point(child):
                    if include_reparse:
                        yield child
                    continue
                if child.is_symlink():
                    yield child
                    continue
                yield child
                if child.is_dir():
                    stack.append(child)

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    def _unlink(self, path: Path) -> None:
        if path.is_symlink():
            if os.name == "nt" and self._is_windows_directory_entry(path):
                path.rmdir()
            else:
                path.unlink()
            return
        if self._is_reparse_point(path):
            if self._is_windows_directory_entry(path):
                path.rmdir()
            else:
                path.unlink()
            return
        self._make_writable(path)
        path.unlink()

    def _rmtree(self, path: Path) -> None:
        shutil.rmtree(path, onerror=self._rmtree_onerror)

    def _rmtree_onerror(self, func, path: str, _exc_info) -> None:
        retry_path = Path(path)
        self._make_writable(retry_path)
        func(path)

    @staticmethod
    def _is_windows_directory_entry(path: Path) -> bool:
        if os.name != "nt":
            return path.is_dir()
        try:
            attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10):
                return True
        except (AttributeError, OSError, TypeError):
            pass
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction):
            with suppress(OSError):
                if is_junction():
                    return True
        return path.is_dir()

    @staticmethod
    def _make_writable(path: Path) -> None:
        if path.is_symlink() or SessionBackupService._is_reparse_point(path):
            return
        if os.name != "nt" or not path.exists():
            return
        with suppress(OSError):
            mode = path.stat().st_mode
            os.chmod(path, mode | stat.S_IWRITE | stat.S_IREAD)
