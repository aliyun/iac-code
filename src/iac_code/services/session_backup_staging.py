"""Local staging backend and asynchronous publisher for A2A session backups."""

from __future__ import annotations

import multiprocessing
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from loguru import logger

from iac_code.services.session_backup import (
    BACKUP_ENV_VAR,
    BackupReason,
    BackupResult,
    SessionBackupBlocked,
    SessionBackupConflict,
    SessionBackupError,
    SessionBackupService,
    SessionReconcileResult,
    _log_backup_session_elapsed,
)
from iac_code.services.session_backup_state import BackupPublicationProof, SessionBackupState
from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.file_security import ensure_private_dir

BACKUP_TMP_ENV_VAR = "IAC_CODE_CONFIG_BACKUP_TMP_DIR"
_COPYING_SUFFIX = ".copying"
_DEFAULT_MAX_CONCURRENT_SESSIONS = 4
_SNAPSHOT_VERSION_PATTERN = re.compile(r"^(?P<session_id>.+)_v(?P<generation>[1-9][0-9]*)$")


@dataclass(frozen=True)
class StagedSessionSnapshot:
    path: Path
    project: str
    session_id: str
    generation: int


@dataclass(frozen=True)
class A2ASessionBackupRuntime:
    service: SessionBackupService
    staging_process: SessionBackupStagingProcess | None


class StagedSessionBackupService(SessionBackupService):
    """Commit A2A backups to immutable local snapshots instead of the shared root."""

    def __init__(
        self,
        staging_root: str | Path,
        session_storage: SessionStorage | None = None,
        retry_delays: Iterable[float] = (0.25, 1.0),
    ) -> None:
        super().__init__(session_storage=session_storage, retry_delays=retry_delays)
        self._staging_root = Path(staging_root)
        self._validate_backup_root(str(staging_root), self._staging_root)

    @property
    def staging_root(self) -> Path:
        return self._staging_root

    @_log_backup_session_elapsed
    def backup_session(
        self,
        cwd: str,
        session_id: str,
        *,
        reason: BackupReason,
        critical: bool,
        publication_proofs: Mapping[str, BackupPublicationProof] | None = None,
    ) -> BackupResult:
        if not self._backup_enabled():
            return BackupResult(enabled=False)
        self._validate_session_id(session_id)
        requested_proofs = dict(publication_proofs or {})
        operation_commit_id = str(uuid.uuid4())

        try:
            source = self._source_for_backup(cwd, session_id)
        except (UnsupportedSessionLayoutError, SessionBackupError) as exc:
            public_error = self._public_error_text(exc)
            if critical:
                raise SessionBackupBlocked(public_error, retry_count=0) from None
            return BackupResult(enabled=True, succeeded=False, error=public_error)
        if source is None:
            if critical:
                raise SessionBackupBlocked("Session backup requires a supported session layout.", retry_count=0)
            return BackupResult(enabled=False)

        delays = (0, *self._retry_delays)
        last_error: Exception | None = None
        last_public_error: str | None = None
        destination: Path | None = None
        base_state: SessionBackupState | None = None

        for attempt, delay in enumerate(delays):
            if attempt:
                time.sleep(delay)
            failed_marker_written = False
            source_verified = False
            copying: Path | None = None
            effective_reason = reason
            attempt_proofs = requested_proofs
            try:
                self._resolve_real_source(source)
                source_verified = True
                with self._session_backup_lock(source):
                    try:
                        base_state = self._read_state(source, session_id=session_id, missing_ok=True)
                        if base_state is None:
                            base_state = SessionBackupState.bootstrap(session_id, writer_id=self._writer_id)
                            self._write_state(source, base_state)

                        if base_state.status == "failed":
                            if base_state.attempt_commit_id is None:
                                raise SessionBackupError("failed staged backup is missing attempt commit id")
                            operation_commit_id = base_state.attempt_commit_id
                            effective_reason = BackupReason(base_state.reason)
                            attempt_proofs = dict(base_state.attempt_publication_proofs)
                            attempt_proofs = self._merge_publication_proofs(
                                attempt_proofs,
                                requested_proofs,
                            )

                        committed_proofs = self._merge_publication_proofs(
                            base_state.publication_proofs,
                            attempt_proofs,
                        )
                        committed_state = base_state.committed_next(
                            commit_id=operation_commit_id,
                            reason=effective_reason.value,
                            writer_id=self._writer_id,
                            proofs=committed_proofs,
                        )
                        destination = self._snapshot_path(source, session_id, committed_state.generation)
                        copying = destination.with_name(destination.name + _COPYING_SUFFIX)
                        self._validate_mirror_paths(source, destination, self._staging_root)

                        existing = self._read_existing_snapshot_state(destination, session_id)
                        if existing is not None:
                            completed_next = (
                                base_state.status == "succeeded" and existing.parent_generation == base_state.generation
                            )
                            if not completed_next and not existing.same_lineage(committed_state):
                                raise SessionBackupConflict(
                                    "staged session backup generation conflict",
                                    local_generation=base_state.generation,
                                    shared_generation=existing.generation,
                                )
                            self._write_state(source, existing)
                            return BackupResult(
                                enabled=True,
                                source=source,
                                destination=destination,
                                retry_count=attempt,
                                generation=existing.generation,
                                commit_id=existing.commit_id,
                                staged_committed=True,
                            )

                        self._remove_copying_snapshot(copying)
                        result = self._mirror(source, copying)
                        self._write_state(copying, committed_state)
                        os.replace(copying, destination)
                        self._fsync_parent_dir(destination)
                        self._write_state(source, committed_state)
                    except Exception as exc:
                        if copying is not None:
                            self._remove_copying_snapshot(copying)
                        failed_marker_written = True
                        public_error = self._public_error_text(exc)
                        self._try_write_failed_marker(
                            source,
                            reason=effective_reason,
                            error=public_error,
                            attempt=attempt + 1,
                            retry_count=attempt,
                            exhausted=attempt == len(delays) - 1,
                            base_state=base_state,
                            writer_id=self._writer_id,
                            attempt_commit_id=operation_commit_id,
                            attempted_proofs=attempt_proofs,
                        )
                        raise
            except Exception as exc:
                last_error = exc
                last_public_error = self._public_error_text(exc)
                if source_verified and not failed_marker_written:
                    self._try_write_failed_marker(
                        source,
                        reason=effective_reason,
                        error=last_public_error,
                        attempt=attempt + 1,
                        retry_count=attempt,
                        exhausted=attempt == len(delays) - 1,
                        base_state=base_state,
                        writer_id=self._writer_id,
                        attempt_commit_id=operation_commit_id,
                        attempted_proofs=attempt_proofs,
                    )
                continue

            return replace(
                result,
                destination=destination,
                retry_count=attempt,
                generation=committed_state.generation,
                commit_id=committed_state.commit_id,
                staged_committed=True,
                shared_committed=False,
            )

        failure_result = BackupResult(
            enabled=True,
            source=source,
            destination=destination,
            succeeded=False,
            error=last_public_error,
            retry_count=max(0, len(delays) - 1),
            staged_committed=False,
            shared_committed=False,
            requires_reconcile=True,
        )
        if critical and last_error is not None:
            raise SessionBackupBlocked(
                last_public_error or self._public_error_text(last_error),
                retry_count=failure_result.retry_count,
                result=failure_result,
            ) from None
        return failure_result

    def reconcile_session(
        self,
        cwd: str,
        session_id: str,
        *,
        attempted_proof_validator: Callable[[str, BackupPublicationProof], bool] | None = None,
        minimum_generation: int | None = None,
    ) -> SessionReconcileResult:
        if minimum_generation is not None and (
            isinstance(minimum_generation, bool) or not isinstance(minimum_generation, int) or minimum_generation <= 0
        ):
            raise ValueError("minimum_generation must be a positive integer")
        if not self._backup_enabled():
            return SessionReconcileResult(enabled=False, action="disabled")
        self._validate_session_id(session_id)
        local = self._source_for_backup(cwd, session_id)
        if local is None:
            return super().reconcile_session(
                cwd,
                session_id,
                attempted_proof_validator=attempted_proof_validator,
                minimum_generation=minimum_generation,
            )

        local_state = self._read_state(local, session_id=session_id, missing_ok=True)
        if local_state is None:
            raise SessionBackupError("existing session is missing backup state")
        if local_state.status == "failed":
            for key, proof in local_state.attempt_publication_proofs.items():
                if attempted_proof_validator is None or not attempted_proof_validator(key, proof):
                    raise SessionBackupError("failed backup publication proof could not be validated")
            try:
                failed_reason = BackupReason(local_state.reason)
            except ValueError as exc:
                raise SessionBackupError("failed staged backup has an invalid reason") from exc
            result = self.backup_session(
                cwd,
                session_id,
                reason=failed_reason,
                critical=True,
                publication_proofs=local_state.attempt_publication_proofs,
            )
            repaired_state = self._read_state(local, session_id=session_id)
            assert repaired_state is not None
            repaired = SessionReconcileResult(
                enabled=True,
                action="repaired",
                source=result.destination,
                state=repaired_state,
                copied_files=result.copied_files,
                deleted_files=result.deleted_files,
                payload_changed=True,
            )
            if minimum_generation is None or repaired_state.generation >= minimum_generation:
                return repaired
            local_state = repaired_state

        if minimum_generation is not None and local_state.generation >= minimum_generation:
            action = "staged_current" if self._snapshot_entries(local, session_id) else "current"
            return SessionReconcileResult(enabled=True, action=action, source=local, state=local_state)

        adopted = self._adopt_next_snapshot(local, session_id, local_state)
        if adopted is not None:
            if minimum_generation is None or (
                adopted.state is not None and adopted.state.generation >= minimum_generation
            ):
                return adopted
            assert adopted.state is not None
            local_state = adopted.state

        if minimum_generation is not None:
            return super().reconcile_session(
                cwd,
                session_id,
                attempted_proof_validator=attempted_proof_validator,
                minimum_generation=minimum_generation,
            )
        action = "staged_current" if self._snapshot_entries(local, session_id) else "current"
        return SessionReconcileResult(enabled=True, action=action, source=local, state=local_state)

    def _adopt_next_snapshot(
        self,
        local: Path,
        session_id: str,
        local_state: SessionBackupState,
    ) -> SessionReconcileResult | None:
        destination = self._snapshot_path(local, session_id, local_state.generation + 1)
        snapshot_state = self._read_existing_snapshot_state(destination, session_id)
        if snapshot_state is None:
            return None
        if snapshot_state.parent_generation != local_state.generation:
            raise SessionBackupConflict(
                "staged session backup generation conflict",
                local_generation=local_state.generation,
                shared_generation=snapshot_state.generation,
            )
        self._write_state(local, snapshot_state)
        return SessionReconcileResult(
            enabled=True,
            action="staged_current",
            source=destination,
            state=snapshot_state,
            payload_changed=False,
        )

    def _snapshot_path(self, source: Path, session_id: str, generation: int) -> Path:
        return self._staging_root / "projects" / source.parent.name / "{}_v{}".format(session_id, generation)

    def _snapshot_entries(self, source: Path, session_id: str) -> list[StagedSessionSnapshot]:
        project_dir = self._staging_root / "projects" / source.parent.name
        if not project_dir.is_dir():
            return []
        entries: list[StagedSessionSnapshot] = []
        try:
            paths = tuple(project_dir.iterdir())
        except FileNotFoundError:
            return []
        for path in paths:
            parsed = parse_staged_snapshot_name(path.name)
            if parsed is None or parsed[0] != session_id or not path.is_dir() or path.is_symlink():
                continue
            entries.append(
                StagedSessionSnapshot(
                    path=path,
                    project=source.parent.name,
                    session_id=session_id,
                    generation=parsed[1],
                )
            )
        return sorted(entries, key=lambda item: item.generation)

    def _read_existing_snapshot_state(self, destination: Path, session_id: str) -> SessionBackupState | None:
        if not destination.exists():
            return None
        try:
            return self._read_state(destination, session_id=session_id, shared=True)
        except SessionBackupError:
            if not destination.exists():
                return None
            raise

    def _remove_copying_snapshot(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        try:
            path.relative_to(self._staging_root)
        except ValueError as exc:
            raise SessionBackupError("staged backup path is outside the staging root") from exc
        self._forget_prepared_dirs(path)
        if path.is_symlink() or self._is_reparse_point(path) or not path.is_dir():
            self._unlink(path)
        else:
            self._rmtree(path)
        self._fsync_parent_dir(path)


class SessionBackupStagingWorker:
    """Publish complete staged snapshots to the configured shared backup root."""

    def __init__(
        self,
        staging_root: str | Path,
        backup_root: str | Path,
        *,
        max_concurrent_sessions: int = _DEFAULT_MAX_CONCURRENT_SESSIONS,
    ) -> None:
        if max_concurrent_sessions < 1:
            raise ValueError("max_concurrent_sessions must be positive")
        self.staging_root = Path(staging_root)
        self.backup_root = Path(backup_root)
        self.max_concurrent_sessions = max_concurrent_sessions
        self._service_local = threading.local()
        self._cleanup_lock = threading.Lock()
        self._service._validate_backup_root(str(staging_root), self.staging_root)
        self._service._validate_backup_root(str(backup_root), self.backup_root)

    @property
    def _service(self) -> SessionBackupService:
        service = getattr(self._service_local, "service", None)
        if service is None:
            service = SessionBackupService()
            self._service_local.service = service
        return service

    def cleanup_incomplete_snapshots(self) -> int:
        removed = 0
        projects_root = self.staging_root / "projects"
        if not projects_root.is_dir():
            return removed
        for path in sorted(projects_root.glob("*/*{}".format(_COPYING_SUFFIX))):
            if not path.name.endswith(_COPYING_SUFFIX):
                continue
            self._remove_snapshot(path)
            removed += 1
        self._prune_empty_staging_dirs()
        return removed

    def run_once(self) -> int:
        sessions: dict[tuple[str, str], list[StagedSessionSnapshot]] = {}
        for snapshot in self.scan_snapshots():
            session_key = (snapshot.project, snapshot.session_id)
            sessions.setdefault(session_key, []).append(snapshot)
        if not sessions:
            return 0
        worker_count = min(self.max_concurrent_sessions, len(sessions))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="iac-code-backup-publisher",
        ) as executor:
            return sum(executor.map(self._publish_session_snapshots, sessions.values()))

    def _publish_session_snapshots(self, snapshots: list[StagedSessionSnapshot]) -> int:
        published = 0
        for snapshot in snapshots:
            try:
                self.publish_snapshot(snapshot)
            except Exception as exc:
                logger.warning(
                    "Staged session backup publish failed session_id={} generation={} error_type={}",
                    snapshot.session_id,
                    snapshot.generation,
                    type(exc).__name__,
                )
                break
            published += 1
        return published

    def scan_snapshots(self) -> list[StagedSessionSnapshot]:
        projects_root = self.staging_root / "projects"
        if not projects_root.is_dir():
            return []
        snapshots: list[StagedSessionSnapshot] = []
        for project_dir in projects_root.iterdir():
            if project_dir.is_symlink() or not project_dir.is_dir():
                continue
            for path in project_dir.iterdir():
                if path.name.endswith(_COPYING_SUFFIX) or path.is_symlink() or not path.is_dir():
                    continue
                parsed = parse_staged_snapshot_name(path.name)
                if parsed is None:
                    continue
                snapshots.append(
                    StagedSessionSnapshot(
                        path=path,
                        project=project_dir.name,
                        session_id=parsed[0],
                        generation=parsed[1],
                    )
                )
        return sorted(snapshots, key=lambda item: (item.project, item.session_id, item.generation))

    def publish_snapshot(self, snapshot: StagedSessionSnapshot) -> None:
        state = self._service._read_state(snapshot.path, session_id=snapshot.session_id, shared=True)
        if state is None or state.generation != snapshot.generation:
            raise SessionBackupError("staged session backup generation does not match its directory")
        destination = self.backup_root / "projects" / snapshot.project / snapshot.session_id
        self._service._validate_mirror_paths(snapshot.path, destination, self.backup_root)
        shared_state = self._service._read_state(
            destination,
            session_id=snapshot.session_id,
            shared=True,
            missing_ok=True,
        )
        if shared_state is not None:
            if shared_state.generation > state.generation:
                self._remove_snapshot(snapshot.path)
                return
            if shared_state.generation == state.generation:
                if shared_state.commit_id != state.commit_id:
                    raise SessionBackupConflict(
                        "shared session backup commit conflicts with staged snapshot",
                        local_generation=state.generation,
                        shared_generation=shared_state.generation,
                    )
                self._remove_snapshot(snapshot.path)
                return

        logger.info(
            "Publishing staged session backup session_id={} generation={}",
            snapshot.session_id,
            snapshot.generation,
        )
        started_at = time.perf_counter()
        self._service._mirror(snapshot.path, destination)
        self._service._write_state(destination, state)
        committed = self._service._read_state(destination, session_id=snapshot.session_id, shared=True)
        if committed is None or not committed.same_lineage(state):
            raise SessionBackupError("shared session backup state did not commit the staged snapshot")
        self._remove_snapshot(snapshot.path)
        logger.info(
            "Published staged session backup session_id={} generation={} elapsed_ms={:.3f}",
            snapshot.session_id,
            snapshot.generation,
            (time.perf_counter() - started_at) * 1000,
        )

    def _remove_snapshot(self, path: Path) -> None:
        try:
            path.relative_to(self.staging_root)
        except ValueError as exc:
            raise SessionBackupError("staged backup path is outside the staging root") from exc
        if path.is_symlink() or self._service._is_reparse_point(path) or not path.is_dir():
            self._service._unlink(path)
        else:
            self._service._rmtree(path)
        self._service._fsync_parent_dir(path)
        with self._cleanup_lock:
            self._prune_empty_staging_dirs()

    def _prune_empty_staging_dirs(self) -> None:
        projects_root = self.staging_root / "projects"
        if projects_root.is_dir():
            for project_dir in tuple(projects_root.iterdir()):
                if project_dir.is_dir() and not project_dir.is_symlink():
                    with suppress(OSError):
                        project_dir.rmdir()
                        self._service._fsync_parent_dir(project_dir)
            with suppress(OSError):
                projects_root.rmdir()
                self._service._fsync_parent_dir(projects_root)


class SessionBackupStagingProcess:
    """Own the single A2A child process that publishes staged snapshots."""

    def __init__(
        self,
        staging_root: str | Path,
        backup_root: str | Path,
        *,
        poll_interval: float = 1.0,
        startup_timeout: float = 5.0,
        process_context: Any | None = None,
    ) -> None:
        self.staging_root = Path(staging_root)
        self.backup_root = Path(backup_root)
        self.poll_interval = poll_interval
        self.startup_timeout = startup_timeout
        self._process_context = process_context
        self._stop_event: Any | None = None
        self._process: Any | None = None

    def start(self) -> None:
        if self._process is not None:
            return
        SessionBackupStagingWorker(self.staging_root, self.backup_root).cleanup_incomplete_snapshots()
        process_context = self._process_context or multiprocessing.get_context("spawn")
        stop_event = process_context.Event()
        ready_event = process_context.Event()
        process = process_context.Process(
            target=run_staging_worker,
            args=(str(self.staging_root), str(self.backup_root), stop_event, ready_event),
            kwargs={"poll_interval": self.poll_interval},
            name="iac-code-session-backup-publisher",
            daemon=True,
        )
        process.start()
        if not ready_event.wait(self.startup_timeout):
            stop_event.set()
            process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
            raise SessionBackupError("staged session backup publisher process did not become ready")
        self._stop_event = stop_event
        self._process = process
        logger.info("Started staged session backup publisher process pid={}", process.pid)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
        logger.info("Stopped staged session backup publisher process")
        self._process = None
        self._stop_event = None


def parse_staged_snapshot_name(name: str) -> tuple[str, int] | None:
    match = _SNAPSHOT_VERSION_PATTERN.fullmatch(name)
    if match is None:
        return None
    session_id = match.group("session_id")
    try:
        SessionBackupService._validate_session_id(session_id)
    except SessionBackupError:
        return None
    return session_id, int(match.group("generation"))


def run_staging_worker(
    staging_root: str,
    backup_root: str,
    stop_event: Any,
    ready_event: Any,
    *,
    poll_interval: float = 1.0,
) -> None:
    worker = SessionBackupStagingWorker(staging_root, backup_root)
    logger.info("Staged session backup publisher process started")
    ready_event.set()
    try:
        while not stop_event.is_set():
            worker.run_once()
            stop_event.wait(poll_interval)
        worker.run_once()
    finally:
        logger.info("Staged session backup publisher process stopped")


def create_a2a_session_backup_runtime() -> A2ASessionBackupRuntime:
    """Select direct or staged backup explicitly for an A2A runtime."""

    raw_staging_root = os.environ.get(BACKUP_TMP_ENV_VAR, "").strip()
    if not raw_staging_root:
        return A2ASessionBackupRuntime(service=SessionBackupService(), staging_process=None)

    raw_backup_root = os.environ.get(BACKUP_ENV_VAR, "").strip()
    if not raw_backup_root:
        raise SessionBackupError("{} requires {}".format(BACKUP_TMP_ENV_VAR, BACKUP_ENV_VAR))

    staging_root = _expanded_path(raw_staging_root)
    backup_root = _expanded_path(raw_backup_root)
    validator = SessionBackupService()
    validator._validate_backup_root(raw_staging_root, staging_root)
    validator._validate_backup_root(raw_backup_root, backup_root)
    staging_resolved = staging_root.resolve(strict=False)
    backup_resolved = backup_root.resolve(strict=False)
    if validator._path_equal_or_under(staging_resolved, backup_resolved) or validator._path_equal_or_under(
        backup_resolved, staging_resolved
    ):
        raise SessionBackupError("temporary and final session backup directories must not overlap")

    config_root = _configured_root().resolve(strict=False)
    if validator._path_equal_or_under(staging_resolved, config_root) or validator._path_equal_or_under(
        config_root, staging_resolved
    ):
        raise SessionBackupError("temporary session backup directory must not overlap IAC_CODE_CONFIG_DIR")

    ensure_private_dir(staging_root)
    service = StagedSessionBackupService(staging_root)
    process = SessionBackupStagingProcess(staging_root, backup_root)
    return A2ASessionBackupRuntime(service=service, staging_process=process)


def _expanded_path(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _configured_root() -> Path:
    raw = os.environ.get("IAC_CODE_CONFIG_DIR", "").strip()
    return _expanded_path(raw) if raw else Path.home() / ".iac-code"
