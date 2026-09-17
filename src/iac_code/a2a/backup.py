from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

from iac_code.i18n import _
from iac_code.services.session_backup import BackupReason, BackupResult, SessionBackupBlocked
from iac_code.services.session_backup_state import BackupPublicationProof
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock, fsync_parent_dir

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_T = TypeVar("_T")

NATURAL_HANDOFF_VERSION = "natural-handoff-v1"
PENDING_JOBS_DIRNAME = ".pending"
_COORDINATOR_STATE_DIRNAME = "session-backup-coordinator"
_COORDINATOR_LOCK_FILENAME = "coordinator.lock"
_COORDINATOR_COUNTER_FILENAME = "boundary.json"
_JOB_DOCUMENT_VERSION = 1
_DEFAULT_RETRY_DELAYS = (1.0, 5.0, 15.0, 60.0)
_DEFAULT_CAPTURE_QUIESCENCE_TIMEOUT = 120.0


async def await_fenced(awaitable: Awaitable[_T]) -> _T:
    """Finish an owned cleanup/commit before propagating cancellation to its caller."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def run_sync_fenced(function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Delay coroutine cancellation until the synchronous mutation has actually stopped."""
    return await _run_sync_fenced(function, None, args, kwargs)


async def run_sync_fenced_with_cancel_completion(
    function: Callable[_P, _T],
    on_cancel_completion: Callable[[_T | None, BaseException | None], Awaitable[None] | None],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    """Fence a synchronous mutation and save its late result before propagating cancellation."""
    return cast(_T, await _run_sync_fenced(function, on_cancel_completion, args, kwargs))


async def _run_sync_fenced(
    function: Callable[..., _T],
    on_cancel_completion: Callable[[_T | None, BaseException | None], Awaitable[None] | None] | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> _T:
    thread_task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(thread_task)
    except asyncio.CancelledError as cancellation:
        while not thread_task.done():
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        result: _T | None = None
        error: BaseException | None = None
        if thread_task.done() and not thread_task.cancelled():
            try:
                result = thread_task.result()
            except BaseException as exc:
                error = exc
        if on_cancel_completion is not None:
            try:
                completion = on_cancel_completion(result, error)
                if inspect.isawaitable(completion):
                    completion_task = asyncio.ensure_future(completion)
                    while not completion_task.done():
                        try:
                            await asyncio.shield(completion_task)
                        except asyncio.CancelledError:
                            continue
                    completion_task.result()
            except BaseException:
                logger.exception("Failed to record a synchronous mutation result returned during cancellation")
        raise cancellation


async def backup_session_async(
    backup_service: Any,
    cwd: str,
    session_id: str,
    *,
    reason: BackupReason,
    critical: bool,
    metrics: Any | None = None,
    publication_proofs: dict[str, BackupPublicationProof] | None = None,
    backup_call: Callable[..., Any] | None = None,
) -> Any | None:
    failed_recorded = False
    try:
        kwargs: dict[str, Any] = {"reason": reason, "critical": critical}
        if publication_proofs is not None:
            kwargs["publication_proofs"] = publication_proofs
        result = await run_sync_fenced(backup_call or backup_service.backup_session, cwd, session_id, **kwargs)
        retry_count = _retry_count(result)
        if getattr(result, "enabled", False) and not getattr(result, "succeeded", True):
            message = str(
                getattr(result, "error", None) or _("Session backup failed. Retry after the backup path is available.")
            )
            _record_backup_failed(metrics, reason=reason, critical=critical, retry_count=retry_count)
            failed_recorded = True
            if critical:
                raise SessionBackupBlocked(message, retry_count=retry_count, result=result)
            logger.warning(
                "A2A session backup failed reason=%s critical=%s retry_count=%s: %s",
                reason.value,
                critical,
                retry_count,
                message,
            )
        elif getattr(result, "enabled", False):
            _record_backup_succeeded(metrics, reason=reason, critical=critical, retry_count=retry_count)
        return result
    except Exception as exc:
        retry_count = _retry_count_from_exception(exc)
        if not failed_recorded:
            _record_backup_failed(metrics, reason=reason, critical=critical, retry_count=retry_count)
        if critical:
            raise
        logger.warning(
            "A2A session backup failed reason=%s critical=%s retry_count=%s error_type=%s",
            reason.value,
            critical,
            retry_count,
            type(exc).__name__,
        )
        return None


def _retry_count(result: Any) -> int:
    value = getattr(result, "retry_count", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _retry_count_from_exception(exc: BaseException) -> int:
    value = getattr(exc, "retry_count", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _record_backup_succeeded(metrics: Any | None, *, reason: BackupReason, critical: bool, retry_count: int) -> None:
    record = getattr(metrics, "record_backup_succeeded", None)
    if callable(record):
        try:
            record(reason=reason.value, critical=critical, retry_count=retry_count)
        except Exception as exc:
            logger.debug("Failed to record A2A backup_succeeded metric: %s", type(exc).__name__)


def _record_backup_failed(metrics: Any | None, *, reason: BackupReason, critical: bool, retry_count: int) -> None:
    record = getattr(metrics, "record_backup_failed", None)
    if callable(record):
        try:
            record(reason=reason.value, critical=critical, retry_count=retry_count)
        except Exception as exc:
            logger.debug("Failed to record A2A backup_failed metric: %s", type(exc).__name__)


class SessionBackupHandoffError(RuntimeError):
    """A local durability failure that must not be reported as a successful handoff."""


@dataclass(frozen=True)
class SessionBackupHandoff:
    """Proof that a business boundary durably delegated its backup."""

    business_revision: int
    job_id: str | None = None
    backup_disabled: bool = False


@dataclass(frozen=True)
class SessionBackupJob:
    job_id: str
    project: str
    session_id: str
    cwd: str
    context_id: str
    execution_id: str
    boundary: str
    reason: str
    business_revision: int
    fence: int
    completion_generation: int | None = None
    publication_proofs: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    attempts: int = 0
    error: str | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "version": _JOB_DOCUMENT_VERSION,
            "jobId": self.job_id,
            "project": self.project,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "contextId": self.context_id,
            "executionId": self.execution_id,
            "boundary": self.boundary,
            "reason": self.reason,
            "businessRevision": self.business_revision,
            "fence": self.fence,
            "completionGeneration": self.completion_generation,
            "publicationProofs": {key: dict(value) for key, value in (self.publication_proofs or {}).items()},
            "attempts": self.attempts,
            "error": self.error,
        }

    @classmethod
    def from_document(cls, document: Any) -> SessionBackupJob | None:
        if not isinstance(document, dict) or document.get("version") != _JOB_DOCUMENT_VERSION:
            return None
        try:
            proofs = document.get("publicationProofs") or {}
            if not isinstance(proofs, dict):
                return None
            return cls(
                job_id=str(document["jobId"]),
                project=str(document["project"]),
                session_id=str(document["sessionId"]),
                cwd=str(document["cwd"]),
                context_id=str(document["contextId"]),
                execution_id=str(document["executionId"]),
                boundary=str(document["boundary"]),
                reason=str(document["reason"]),
                business_revision=int(document["businessRevision"]),
                fence=int(document["fence"]),
                completion_generation=(
                    None if document.get("completionGeneration") is None else int(document["completionGeneration"])
                ),
                publication_proofs={str(key): dict(value) for key, value in proofs.items()},
                attempts=int(document.get("attempts") or 0),
                error=(None if document.get("error") is None else str(document["error"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def session_key(self) -> tuple[str, str]:
        return self.project, self.session_id


class SessionBackupCoordinator:
    """Own durable backup to-do jobs so a business boundary never waits for a copy."""

    def __init__(
        self,
        backup_service: Any | None,
        *,
        state_root: Path | None,
        metrics: Any | None = None,
        retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
        quiescence_timeout: float = _DEFAULT_CAPTURE_QUIESCENCE_TIMEOUT,
    ) -> None:
        self._backup_service = backup_service
        self._state_root = state_root
        self._metrics = metrics
        self._retry_delays = tuple(retry_delays)
        self._quiescence_timeout = quiescence_timeout
        staging_root = getattr(backup_service, "staging_root", None)
        self._staging_root = Path(staging_root) if staging_root is not None else None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._session_chains: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._staged_callbacks: dict[str, Callable[[int | None, str | None], Awaitable[None] | None]] = {}
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._staging_root is not None and self._state_root is not None and self._backup_service is not None

    @property
    def pending_root(self) -> Path | None:
        return None if self._staging_root is None else self._staging_root / PENDING_JOBS_DIRNAME

    async def register_boundary(
        self,
        *,
        cwd: str,
        session_id: str | None,
        context_id: str,
        execution_id: str,
        boundary: str,
        reason: BackupReason,
        completion_generation: int | None = None,
        publication_proofs: Mapping[str, BackupPublicationProof] | None = None,
        on_staged: Callable[[int | None, str | None], Awaitable[None] | None] | None = None,
    ) -> SessionBackupHandoff:
        """Persist a backup to-do job while the caller still owns the session."""

        if self._closed:
            raise SessionBackupHandoffError("session backup coordinator is closed")
        if not self.enabled or session_id is None:
            return SessionBackupHandoff(business_revision=0, backup_disabled=True)
        proofs = {key: proof.to_dict() for key, proof in dict(publication_proofs or {}).items()}
        try:
            job = await run_sync_fenced(
                self._persist_pending_job,
                cwd,
                session_id,
                context_id,
                execution_id,
                boundary,
                reason.value,
                completion_generation,
                proofs,
            )
        except Exception as exc:
            raise SessionBackupHandoffError(
                "session backup job could not be persisted: {}".format(type(exc).__name__)
            ) from exc
        if job is None:
            return SessionBackupHandoff(business_revision=0, backup_disabled=True)
        if on_staged is not None:
            self._staged_callbacks[job.job_id] = on_staged
        self._schedule_capture(job)
        return SessionBackupHandoff(business_revision=job.business_revision, job_id=job.job_id)

    async def wait_for_local_snapshot_quiescence(
        self,
        *,
        cwd: str,
        session_id: str | None,
        timeout: float | None = None,
    ) -> None:
        """Let earlier snapshot attempts for this session finish before new writes start."""

        if not self.enabled or session_id is None:
            return
        try:
            project = await run_sync_fenced(self._project_for_session, cwd, session_id)
        except Exception:
            return
        if project is None:
            return
        capture = self._session_chains.get((project, session_id))
        if capture is None or capture.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(capture), timeout=timeout or self._quiescence_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "A2A session backup snapshot did not quiesce before the next turn session_id=%s",
                session_id,
            )
        except Exception:
            return

    async def recover(self) -> int:
        """Re-arm durable to-do jobs left by a previous process."""

        if not self.enabled:
            return 0
        try:
            jobs = await run_sync_fenced(self._load_pending_jobs)
        except Exception as exc:
            logger.warning("A2A session backup job recovery failed error_type=%s", type(exc).__name__)
            return 0
        for job in jobs:
            self._schedule_capture(job)
        if jobs:
            logger.info("A2A session backup recovered pending jobs count=%s", len(jobs))
        return len(jobs)

    async def aclose(self, *, drain_timeout: float = 5.0) -> None:
        """Drain local snapshot work only; never wait for remote publication."""

        self._closed = True
        tasks = tuple(self._tasks)
        if not tasks:
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True),
                timeout=max(drain_timeout, 0.0),
            )

    def _schedule_capture(self, job: SessionBackupJob, *, attempt: int = 0) -> None:
        previous = self._session_chains.get(job.session_key)
        task = asyncio.ensure_future(self._run_capture(job, previous, attempt))
        task.set_name("a2a-session-backup-{}".format(job.job_id))
        self._session_chains[job.session_key] = task
        self._tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if self._session_chains.get(job.session_key) is done:
                self._session_chains.pop(job.session_key, None)
            if not done.cancelled():
                with suppress(BaseException):
                    done.exception()

        task.add_done_callback(completed)

    async def _run_capture(
        self,
        job: SessionBackupJob,
        previous: asyncio.Task[Any] | None,
        attempt: int,
    ) -> None:
        if previous is not None and not previous.done():
            with suppress(BaseException):
                await asyncio.shield(previous)
        result: BackupResult | None = None
        failure: BaseException | None = None
        try:
            result = cast(
                BackupResult,
                await run_sync_fenced(
                    self._backup_service.backup_session,
                    job.cwd,
                    job.session_id,
                    reason=BackupReason(job.reason),
                    critical=False,
                    publication_proofs={
                        key: BackupPublicationProof.from_dict(value)
                        for key, value in (job.publication_proofs or {}).items()
                    },
                ),
            )
        except Exception as exc:
            failure = exc
        if result is not None and not result.enabled:
            await self._clear_covered_jobs(job)
            return
        if result is not None and result.succeeded and result.staged_committed:
            _record_backup_succeeded(
                self._metrics,
                reason=BackupReason(job.reason),
                critical=False,
                retry_count=result.retry_count,
            )
            await self._clear_covered_jobs(job, generation=result.generation, commit_id=result.commit_id)
            await self._notify_staged(job, result.generation, result.commit_id)
            return
        error_text = (
            str(getattr(result, "error", None) or "session backup did not stage a snapshot")
            if failure is None
            else type(failure).__name__
        )
        _record_backup_failed(self._metrics, reason=BackupReason(job.reason), critical=False, retry_count=attempt)
        logger.warning(
            "A2A session backup job attempt failed job_id=%s attempt=%s error=%s",
            job.job_id,
            attempt + 1,
            error_text,
        )
        retried = replace(job, attempts=attempt + 1, error=error_text)
        with suppress(Exception):
            await run_sync_fenced(self._write_pending_job, retried)
        if not self._closed:
            delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
            self._schedule_retry(retried, attempt + 1, delay)

    def _schedule_retry(self, job: SessionBackupJob, attempt: int, delay: float) -> None:
        async def retry() -> None:
            await asyncio.sleep(delay)
            if self._closed:
                return
            self._schedule_capture(job, attempt=attempt)

        task = asyncio.ensure_future(retry())
        task.set_name("a2a-session-backup-retry-{}".format(job.job_id))
        self._tasks.add(task)
        task.add_done_callback(self._forget_task)

    def _forget_task(self, done: asyncio.Task[Any]) -> None:
        self._tasks.discard(done)
        if not done.cancelled():
            with suppress(BaseException):
                done.exception()

    async def _notify_staged(self, job: SessionBackupJob, generation: int | None, commit_id: str | None) -> None:
        callback = self._staged_callbacks.pop(job.job_id, None)
        if callback is None:
            return
        try:
            outcome = callback(generation, commit_id)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:
            logger.warning(
                "A2A session backup staged callback failed job_id=%s error_type=%s",
                job.job_id,
                type(exc).__name__,
            )

    async def _clear_covered_jobs(
        self,
        job: SessionBackupJob,
        *,
        generation: int | None = None,
        commit_id: str | None = None,
    ) -> None:
        try:
            await run_sync_fenced(self._commit_receipt_and_clear, job, generation, commit_id)
        except Exception as exc:
            logger.warning(
                "A2A session backup receipt could not be committed job_id=%s error_type=%s",
                job.job_id,
                type(exc).__name__,
            )

    def _commit_receipt_and_clear(
        self,
        job: SessionBackupJob,
        generation: int | None,
        commit_id: str | None,
    ) -> None:
        state_dir = self._session_state_dir(job.project, job.session_id)
        with cross_process_file_lock(state_dir / _COORDINATOR_LOCK_FILENAME):
            receipt = {
                "version": _JOB_DOCUMENT_VERSION,
                "jobId": job.job_id,
                "project": job.project,
                "sessionId": job.session_id,
                "boundary": job.boundary,
                "backupGeneration": generation,
                "commitId": commit_id,
                "coveredThroughBusinessRevision": job.business_revision,
            }
            receipts_dir = state_dir / "receipts"
            ensure_private_dir(receipts_dir)
            atomic_write_json(receipts_dir / "{}.json".format(job.job_id), receipt, durable=True)
            for path, pending in self._pending_documents():
                if (
                    pending.project == job.project
                    and pending.session_id == job.session_id
                    and pending.business_revision <= job.business_revision
                ):
                    self._remove_pending_marker(path)

    def _persist_pending_job(
        self,
        cwd: str,
        session_id: str,
        context_id: str,
        execution_id: str,
        boundary: str,
        reason: str,
        completion_generation: int | None,
        publication_proofs: dict[str, dict[str, Any]],
    ) -> SessionBackupJob | None:
        project = self._project_for_session(cwd, session_id)
        if project is None:
            return None
        state_dir = self._session_state_dir(project, session_id)
        ensure_private_dir(state_dir)
        with cross_process_file_lock(state_dir / _COORDINATOR_LOCK_FILENAME):
            business_revision, fence = self._next_counters(state_dir)
            job = SessionBackupJob(
                job_id="job-" + uuid.uuid4().hex,
                project=project,
                session_id=session_id,
                cwd=cwd,
                context_id=context_id,
                execution_id=execution_id,
                boundary=boundary,
                reason=reason,
                business_revision=business_revision,
                fence=fence,
                completion_generation=completion_generation,
                publication_proofs=publication_proofs,
            )
            self._write_pending_job(job)
        return job

    def _next_counters(self, state_dir: Path) -> tuple[int, int]:
        counter_path = state_dir / _COORDINATOR_COUNTER_FILENAME
        business_revision = 0
        fence = 0
        if counter_path.exists():
            document = json.loads(counter_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("version") != _JOB_DOCUMENT_VERSION:
                raise SessionBackupHandoffError("session backup boundary counters are unreadable")
            business_revision = int(document["businessRevision"])
            fence = int(document["fence"])
        business_revision += 1
        fence += 1
        atomic_write_json(
            counter_path,
            {"version": _JOB_DOCUMENT_VERSION, "businessRevision": business_revision, "fence": fence},
            durable=True,
        )
        return business_revision, fence

    def _write_pending_job(self, job: SessionBackupJob) -> None:
        pending_root = self.pending_root
        assert pending_root is not None
        ensure_private_dir(pending_root)
        atomic_write_json(pending_root / "{}.json".format(job.job_id), job.to_document(), durable=True)
        fsync_parent_dir(pending_root / "{}.json".format(job.job_id))

    def _pending_documents(self) -> list[tuple[Path, SessionBackupJob]]:
        pending_root = self.pending_root
        if pending_root is None or not pending_root.is_dir():
            return []
        jobs: list[tuple[Path, SessionBackupJob]] = []
        for path in sorted(pending_root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                job = SessionBackupJob.from_document(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            if job is not None:
                jobs.append((path, job))
        return jobs

    def _load_pending_jobs(self) -> list[SessionBackupJob]:
        jobs = [job for _path, job in self._pending_documents()]
        return sorted(jobs, key=lambda item: (item.project, item.session_id, item.fence))

    def _remove_pending_marker(self, path: Path) -> None:
        with suppress(FileNotFoundError):
            path.unlink()
        parent = path.parent
        with suppress(OSError):
            fsync_parent_dir(path)
        with suppress(OSError):
            parent.rmdir()

    def _project_for_session(self, cwd: str, session_id: str) -> str | None:
        source = self._backup_service._source_for_backup(cwd, session_id)
        return None if source is None else Path(source).parent.name

    def _session_state_dir(self, project: str, session_id: str) -> Path:
        assert self._state_root is not None
        return self._state_root / _COORDINATOR_STATE_DIRNAME / project / session_id
