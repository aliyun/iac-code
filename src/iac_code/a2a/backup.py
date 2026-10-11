from __future__ import annotations

import asyncio
import inspect
import json
import logging
import stat
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

from iac_code.i18n import _
from iac_code.services.session_backup import BackupReason, BackupResult, SessionBackupBlocked
from iac_code.services.session_backup_state import BackupPublicationProof, SessionBackupState
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock, fsync_parent_dir

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_T = TypeVar("_T")

NATURAL_HANDOFF_VERSION = "natural-handoff-v2"
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
    requires_local_staging_commit = getattr(backup_service, "staging_root", None) is not None
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
            if critical or requires_local_staging_commit:
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
        if requires_local_staging_commit:
            if isinstance(exc, SessionBackupBlocked):
                raise
            raise SessionBackupBlocked(
                _("Session backup failed. Retry after the backup path is available."),
                retry_count=retry_count,
            ) from exc
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

    def __init__(self, message: str, *, job: SessionBackupJob | None = None) -> None:
        super().__init__(message)
        self.job = job


@dataclass(frozen=True)
class SessionBackupHandoff:
    """Proof that a business boundary durably delegated its backup."""

    business_revision: int
    job_id: str | None = None
    backup_disabled: bool = False
    staged_committed: bool = False
    snapshot_generation: int | None = None
    snapshot_commit_id: str | None = None


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
    capture_commit_id: str | None = None
    capture_started: bool = False
    staged_generation: int | None = None
    staged_commit_id: str | None = None
    callback_required: bool = False
    callback_completed: bool = False
    staged_action: Mapping[str, Any] | None = None
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
            "captureCommitId": self.capture_commit_id,
            "captureStarted": self.capture_started,
            "stagedGeneration": self.staged_generation,
            "stagedCommitId": self.staged_commit_id,
            "callbackRequired": self.callback_required,
            "callbackCompleted": self.callback_completed,
            "stagedAction": None if self.staged_action is None else dict(self.staged_action),
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
            staged_action = document.get("stagedAction")
            if staged_action is not None and not isinstance(staged_action, dict):
                return None
            callback_required = document.get("callbackRequired", False)
            callback_completed = document.get("callbackCompleted", False)
            capture_started = document.get("captureStarted", False)
            if (
                not isinstance(callback_required, bool)
                or not isinstance(callback_completed, bool)
                or not isinstance(capture_started, bool)
            ):
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
                capture_commit_id=(
                    None if document.get("captureCommitId") is None else str(document["captureCommitId"])
                ),
                capture_started=capture_started,
                staged_generation=(
                    None if document.get("stagedGeneration") is None else int(document["stagedGeneration"])
                ),
                staged_commit_id=(None if document.get("stagedCommitId") is None else str(document["stagedCommitId"])),
                callback_required=callback_required,
                callback_completed=callback_completed,
                staged_action=(None if staged_action is None else dict(staged_action)),
                attempts=int(document.get("attempts") or 0),
                error=(None if document.get("error") is None else str(document["error"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def session_key(self) -> tuple[str, str]:
        return self.project, self.session_id


class _LocalJobCommitError(OSError):
    """Only a coordinator persistence mutation authorizes one local repair."""

    def __init__(self, job: SessionBackupJob, operation: str) -> None:
        super().__init__("session backup local {} commit failed".format(operation))
        self.job = job
        self.operation = operation


class SessionBackupCoordinator:
    """Commit business-boundary snapshots locally and recover durable pending jobs."""

    def __init__(
        self,
        backup_service: Any | None,
        *,
        state_root: Path | None,
        metrics: Any | None = None,
        retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
        quiescence_timeout: float = _DEFAULT_CAPTURE_QUIESCENCE_TIMEOUT,
        staged_action_resolver: (Callable[[Mapping[str, Any], int, str], Awaitable[None] | None] | None) = None,
    ) -> None:
        self._backup_service = backup_service
        self._state_root = state_root
        self._metrics = metrics
        # Kept in the constructor for compatibility. A failed business-boundary
        # capture must be retried by its caller while the source is still fenced;
        # a background retry could otherwise snapshot a later turn as the old job.
        del retry_delays
        self._quiescence_timeout = quiescence_timeout
        staging_root = getattr(backup_service, "staging_root", None)
        self._staging_root = Path(staging_root) if staging_root is not None else None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._session_chains: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._staged_callbacks: dict[str, Callable[[int | None, str | None], Awaitable[None] | None]] = {}
        self._staged_action_resolver = staged_action_resolver
        self._committed_revisions: dict[str, int] = {}
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._staging_root is not None and self._state_root is not None and self._backup_service is not None

    @property
    def pending_root(self) -> Path | None:
        return None if self._staging_root is None else self._staging_root / PENDING_JOBS_DIRNAME

    def set_staged_action_resolver(
        self,
        resolver: Callable[[Mapping[str, Any], int, str], Awaitable[None] | None] | None,
    ) -> None:
        """Install the typed callback resolver before startup recovery runs."""

        self._staged_action_resolver = resolver

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
        staged_action: Mapping[str, Any] | None = None,
    ) -> SessionBackupHandoff:
        """Persist a backup to-do job while the caller still owns the session."""

        return await await_fenced(
            self._register_boundary(
                cwd=cwd,
                session_id=session_id,
                context_id=context_id,
                execution_id=execution_id,
                boundary=boundary,
                reason=reason,
                completion_generation=completion_generation,
                publication_proofs=publication_proofs,
                on_staged=on_staged,
                staged_action=staged_action,
            )
        )

    async def _register_boundary(
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
        staged_action: Mapping[str, Any] | None = None,
    ) -> SessionBackupHandoff:
        """Own durable registration through original local capture and receipt."""

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
                on_staged is not None,
                None if staged_action is None else dict(staged_action),
            )
        except Exception as exc:
            raise SessionBackupHandoffError(
                "session backup job could not be persisted: {}".format(type(exc).__name__)
            ) from exc
        if job is None:
            return SessionBackupHandoff(business_revision=0, backup_disabled=True)
        self._committed_revisions[session_id] = job.business_revision
        if on_staged is not None:
            self._staged_callbacks[job.job_id] = on_staged
        return await self._complete_registered_job(job)

    async def resume_natural_completion(
        self,
        job: SessionBackupJob,
        *,
        cwd: str,
        session_id: str,
        context_id: str,
        execution_id: str,
        completion_generation: int | None,
    ) -> SessionBackupHandoff:
        """Settle an already captured natural job without creating another boundary."""

        if self._closed or not self.enabled:
            raise SessionBackupHandoffError("session backup coordinator cannot resume a captured job", job=job)
        if (
            job.cwd != cwd
            or job.session_id != session_id
            or job.context_id != context_id
            or job.execution_id != execution_id
            or job.boundary != "natural_completion"
            or job.reason != BackupReason.TERMINAL.value
            or type(completion_generation) is not int
            or completion_generation <= 0
            or type(job.completion_generation) is not int
            or job.completion_generation != completion_generation
            or job.callback_required
            or job.staged_action is not None
        ):
            raise SessionBackupHandoffError("session backup natural retry identity conflicts", job=job)
        try:
            canonical = await run_sync_fenced(self._captured_natural_job, job)
        except SessionBackupHandoffError:
            raise
        except Exception as exc:
            raise SessionBackupHandoffError(
                "session backup natural retry proof could not be loaded: {}".format(type(exc).__name__), job=job
            ) from exc
        return await self._complete_registered_job(canonical)

    def _captured_natural_job(self, job: SessionBackupJob) -> SessionBackupJob:
        project = self._project_for_session(job.cwd, job.session_id)
        if project != job.project:
            raise SessionBackupHandoffError("session backup natural retry project conflicts", job=job)
        with cross_process_file_lock(self._session_state_dir(job.project, job.session_id) / _COORDINATOR_LOCK_FILENAME):
            canonical = self._canonical_job_locked(job)
            assert self.pending_root is not None
            path = self.pending_root / "{}.json".format(job.job_id)
            if path.exists():
                document = json.loads(path.read_text(encoding="utf-8"))
                for key in ("businessRevision", "fence", "completionGeneration", "stagedGeneration"):
                    value = document.get(key)
                    if (key != "stagedGeneration" or value is not None) and (type(value) is not int or value <= 0):
                        raise SessionBackupHandoffError("session backup natural retry proof is invalid", job=job)
            result = (
                BackupResult(
                    enabled=True,
                    generation=canonical.staged_generation,
                    commit_id=canonical.staged_commit_id,
                    staged_committed=True,
                )
                if canonical.staged_generation is not None
                else self._find_local_capture_locked(canonical)
            )
            if (
                not canonical.capture_started
                or result is None
                or not result.enabled
                or type(result.generation) is not int
                or result.generation <= 0
                or not canonical.capture_commit_id
                or result.commit_id != canonical.capture_commit_id
            ):
                raise SessionBackupHandoffError("session backup natural retry has no exact captured proof", job=job)
            return canonical

    async def _complete_registered_job(self, job: SessionBackupJob) -> SessionBackupHandoff:
        self._committed_revisions[job.session_id] = max(
            job.business_revision, self._committed_revisions.get(job.session_id, 0)
        )
        capture = self._schedule_capture(job)
        try:
            captured = await await_fenced(self._await_capture_with_local_repair(job, capture))
        except Exception as exc:
            raise SessionBackupHandoffError(
                "session backup staged callback could not be committed: {}".format(type(exc).__name__), job=job
            ) from exc
        if captured is None:
            raise SessionBackupHandoffError("session backup snapshot could not be committed locally", job=job)
        return SessionBackupHandoff(
            business_revision=job.business_revision,
            job_id=job.job_id,
            backup_disabled=not captured.enabled,
            staged_committed=bool(captured.staged_committed),
            snapshot_generation=captured.generation,
            snapshot_commit_id=captured.commit_id,
        )

    async def _await_capture_with_local_repair(
        self, job: SessionBackupJob, capture: asyncio.Task[BackupResult | None]
    ) -> BackupResult | None:
        try:
            return await capture
        except _LocalJobCommitError as exc:
            # The original boundary still owns its source. Repair only the
            # already captured job, once; callback failures never enter here.
            staged, result = await run_sync_fenced(self._repair_local_job_commit, job, exc.job)
            return await self._finish_staged_job(staged, result=result)

    def _repair_local_job_commit(
        self, job: SessionBackupJob, failed_commit: SessionBackupJob
    ) -> tuple[SessionBackupJob, BackupResult]:
        """Re-read the original job and adopt exact local proof without recapture."""
        state_dir = self._session_state_dir(job.project, job.session_id)
        with cross_process_file_lock(state_dir / "capture.lock"):
            with cross_process_file_lock(state_dir / _COORDINATOR_LOCK_FILENAME):
                canonical = self._canonical_job_locked(job)
                self._canonical_job_locked(failed_commit)
                if canonical.staged_generation is not None:
                    result = BackupResult(
                        enabled=True,
                        generation=canonical.staged_generation,
                        commit_id=canonical.staged_commit_id,
                        staged_committed=True,
                    )
                else:
                    result = self._find_local_capture_locked(canonical)
                if (
                    not canonical.capture_started
                    or result is None
                    or not result.enabled
                    or not isinstance(result.generation, int)
                    or isinstance(result.generation, bool)
                    or result.generation <= 0
                    or result.commit_id != canonical.capture_commit_id
                    or (
                        failed_commit.staged_generation is not None
                        and (
                            result.generation != failed_commit.staged_generation
                            or result.commit_id != failed_commit.staged_commit_id
                        )
                    )
                ):
                    raise SessionBackupHandoffError("session backup local repair has no exact captured proof")
                staged = replace(
                    canonical,
                    staged_generation=result.generation,
                    staged_commit_id=result.commit_id,
                    callback_completed=canonical.callback_completed or failed_commit.callback_completed,
                    error=None,
                )
                self._write_pending_job_locked(staged)
                return staged, result

    def committed_business_revision(self, session_id: str | None) -> int | None:
        """Return the newest business revision this process durably registered."""

        return None if session_id is None else self._committed_revisions.get(session_id)

    async def wait_for_local_snapshot_quiescence(
        self,
        *,
        cwd: str,
        session_id: str | None,
        timeout: float | None = None,
    ) -> None:
        """Wait for already-started local snapshot work, never remote publication."""

        if not self.enabled or session_id is None:
            return
        project = self._project_for_session(cwd, session_id)
        if project is None:
            return
        capture = self._session_chains.get((project, session_id))
        if capture is None:
            return
        wait_timeout = self._quiescence_timeout if timeout is None else max(timeout, 0.0)
        await asyncio.wait_for(asyncio.shield(capture), timeout=wait_timeout)

    async def recover(self) -> int:
        """Re-arm durable to-do jobs left by a previous process."""

        if not self.enabled:
            return 0
        jobs = await run_sync_fenced(self._load_pending_jobs)
        captures = [self._schedule_capture(job, recovery=True) for job in jobs]
        if captures:
            recovered = await await_fenced(asyncio.gather(*captures))
            if any(result is None for result in recovered):
                raise SessionBackupHandoffError("recovered session backup snapshot could not be committed locally")
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

    def _schedule_capture(
        self, job: SessionBackupJob, *, attempt: int = 0, recovery: bool = False
    ) -> asyncio.Task[BackupResult | None]:
        previous = self._session_chains.get(job.session_key)
        task = asyncio.ensure_future(self._run_capture(job, previous, attempt, recovery=recovery))
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
        return task

    async def _run_capture(
        self,
        job: SessionBackupJob,
        previous: asyncio.Task[Any] | None,
        attempt: int,
        *,
        recovery: bool = False,
    ) -> BackupResult | None:
        if previous is not None:
            if not previous.done():
                await asyncio.shield(previous)
            previous.result()
        canonical, result = await run_sync_fenced(self._capture_local_job, job, attempt, recovery)
        if result is None and recovery:
            canonical, result = await run_sync_fenced(self._recover_shared_job, canonical)
        if result is None:
            return None
        if not result.enabled:
            canonical = replace(canonical, callback_completed=True)
            await run_sync_fenced(self._write_pending_job, canonical)
            await self._clear_covered_jobs(canonical)
            return result
        return await self._finish_staged_job(canonical, result=result)

    def _capture_local_job(
        self, job: SessionBackupJob, attempt: int, recovery: bool
    ) -> tuple[SessionBackupJob, BackupResult | None]:
        """Own capture across processes; never hold the coordinator lock during I/O."""
        state_dir = self._session_state_dir(job.project, job.session_id)
        with cross_process_file_lock(state_dir / "capture.lock"):
            with cross_process_file_lock(state_dir / _COORDINATOR_LOCK_FILENAME):
                canonical = self._canonical_job_locked(job)
                recovered = (
                    self._find_local_capture_locked(canonical) if canonical.capture_started or recovery else None
                )
            if recovered is not None and not recovered.enabled:
                return canonical, recovered
            if canonical.staged_generation is not None:
                return canonical, BackupResult(
                    enabled=True,
                    generation=canonical.staged_generation,
                    commit_id=canonical.staged_commit_id,
                    staged_committed=True,
                )
            if not canonical.capture_commit_id:
                raise SessionBackupHandoffError("session backup job is missing a durable capture identity")
            if canonical.capture_started or recovery:
                # Only startup may consult historical shared proof. New business
                # capture never enters the publisher's shared lock. A historical
                # unstarted job cannot capture later live data as its old boundary.
                if recovered is None and recovery:
                    return canonical, None
                if recovered is None:
                    raise SessionBackupHandoffError(
                        "session backup capture started but has no matching durable lineage proof"
                    )
                adopter = getattr(self._backup_service, "adopt_coordinator_capture", None)
                if callable(adopter):
                    adopter(canonical.cwd, canonical.session_id, recovered)
                staged = replace(
                    canonical,
                    capture_started=True,
                    staged_generation=recovered.generation,
                    staged_commit_id=recovered.commit_id,
                    error=None,
                )
                self._write_pending_job(staged)
                return staged, recovered
            capturing = replace(canonical, capture_started=True)
            self._write_pending_job(capturing)
            assert self._backup_service is not None
            try:
                result = self._backup_service.backup_session(
                    capturing.cwd,
                    capturing.session_id,
                    reason=BackupReason(capturing.reason),
                    critical=False,
                    publication_proofs={
                        key: BackupPublicationProof.from_dict(value)
                        for key, value in capturing.publication_proofs.items()
                    },
                    operation_commit_id=capturing.capture_commit_id,
                )
            except Exception as exc:
                result = None
                error = type(exc).__name__
            else:
                error = str(getattr(result, "error", None) or "session backup did not stage a snapshot")
            if result is not None and not result.enabled:
                return capturing, result
            if result is not None and result.succeeded and result.staged_committed:
                if result.commit_id != capturing.capture_commit_id:
                    raise SessionBackupHandoffError("session backup returned a mismatched capture identity")
                if result.generation is None or result.generation <= 0 or not result.commit_id:
                    raise SessionBackupHandoffError("session backup returned an incomplete staged proof")
                staged = replace(
                    capturing,
                    staged_generation=result.generation,
                    staged_commit_id=result.commit_id,
                    attempts=attempt,
                    error=None,
                )
                self._write_pending_job(staged)
                _record_backup_succeeded(
                    self._metrics, reason=BackupReason(capturing.reason), critical=False, retry_count=result.retry_count
                )
                return staged, result
            _record_backup_failed(
                self._metrics, reason=BackupReason(capturing.reason), critical=False, retry_count=attempt
            )
            logger.warning(
                "A2A session backup job attempt failed job_id=%s attempt=%s error=%s",
                capturing.job_id,
                attempt + 1,
                error,
            )
            self._write_pending_job(replace(capturing, attempts=attempt + 1, error=error))
            return capturing, None

    def _recover_shared_job(self, job: SessionBackupJob) -> tuple[SessionBackupJob, BackupResult]:
        # Historical remote lookup must not own capture.lock: another process
        # may already accept a fresh boundary on this resident session.
        shared_capture = self._find_shared_capture(job)
        shared_result = None if shared_capture is None else shared_capture[0]
        shared_state = None if shared_capture is None else shared_capture[1]
        state_dir = self._session_state_dir(job.project, job.session_id)
        with cross_process_file_lock(state_dir / "capture.lock"):
            with cross_process_file_lock(state_dir / _COORDINATOR_LOCK_FILENAME):
                canonical = self._canonical_job_locked(job)
                local_result = self._find_local_capture_locked(canonical)
                result = local_result or shared_result
            if result is None:
                raise SessionBackupHandoffError(
                    "session backup capture started but has no matching durable lineage proof"
                )
            adopter = getattr(self._backup_service, "adopt_coordinator_capture", None)
            if callable(adopter):
                adopter(
                    canonical.cwd,
                    canonical.session_id,
                    result,
                    committed_state=shared_state if local_result is None else None,
                )
            staged = replace(
                canonical,
                capture_started=True,
                staged_generation=result.generation,
                staged_commit_id=result.commit_id,
                error=None,
            )
            self._write_pending_job(staged)
            return staged, result

    async def _finish_staged_job(
        self,
        job: SessionBackupJob,
        *,
        result: BackupResult | None = None,
    ) -> BackupResult:
        generation = job.staged_generation
        commit_id = job.staged_commit_id
        if generation is None or generation <= 0 or not commit_id or commit_id != job.capture_commit_id:
            raise SessionBackupHandoffError("session backup durable staged proof is invalid")

        completed_job = job
        if not job.callback_completed:
            callback = self._staged_callbacks.get(job.job_id)
            if callback is not None:
                await self._notify_staged(job, generation, commit_id)
            elif job.callback_required:
                action = job.staged_action
                resolver = self._staged_action_resolver
                if action is None:
                    raise SessionBackupHandoffError("session backup staged callback has no durable recovery identity")
                if resolver is None:
                    raise SessionBackupHandoffError("session backup staged callback resolver is unavailable")
                outcome = resolver(action, generation, commit_id)
                if inspect.isawaitable(outcome):
                    await outcome
            completed_job = replace(job, callback_completed=True)
            await run_sync_fenced(self._write_pending_job, completed_job)

        await self._clear_covered_jobs(completed_job, generation=generation, commit_id=commit_id)
        if result is not None:
            return result
        return BackupResult(
            enabled=True,
            succeeded=True,
            generation=generation,
            commit_id=commit_id,
            staged_committed=True,
        )

    async def _notify_staged(self, job: SessionBackupJob, generation: int | None, commit_id: str | None) -> None:
        callback = self._staged_callbacks.get(job.job_id)
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
            raise
        else:
            self._staged_callbacks.pop(job.job_id, None)

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
            raise

    def _commit_receipt_and_clear(
        self,
        job: SessionBackupJob,
        generation: int | None,
        commit_id: str | None,
    ) -> None:
        state_dir = self._session_state_dir(job.project, job.session_id)
        with cross_process_file_lock(state_dir / _COORDINATOR_LOCK_FILENAME):
            canonical = self._canonical_job_locked(job)
            if generation is not None and (
                canonical.staged_generation != generation
                or canonical.staged_commit_id != commit_id
                or commit_id != canonical.capture_commit_id
            ):
                raise SessionBackupHandoffError("session backup receipt does not match durable staged ACK")
            if canonical.callback_required and not canonical.callback_completed:
                raise SessionBackupHandoffError("session backup receipt requires completed callback")
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
            try:
                atomic_write_json(self._receipt_path(job), receipt, durable=True)
            except OSError as exc:
                raise _LocalJobCommitError(canonical, "receipt") from exc
            for path, pending in self._pending_documents():
                if pending.job_id == job.job_id:
                    try:
                        self._remove_pending_marker(path)
                    except OSError as exc:
                        raise _LocalJobCommitError(canonical, "pending removal") from exc

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
        callback_required: bool,
        staged_action: dict[str, Any] | None,
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
                capture_commit_id=str(uuid.uuid4()),
                callback_required=callback_required,
                staged_action=staged_action,
            )
            self._write_pending_job_locked(job, create=True)
        return job

    def _find_committed_capture(self, job: SessionBackupJob) -> BackupResult | None:
        """Read exact local proof, without waiting for remote publication."""
        with cross_process_file_lock(self._session_state_dir(job.project, job.session_id) / _COORDINATOR_LOCK_FILENAME):
            return self._find_local_capture_locked(job)

    def _find_local_capture_locked(self, job: SessionBackupJob) -> BackupResult | None:
        service = self._backup_service
        if service is None or self._staging_root is None or not job.capture_commit_id:
            return None
        receipt = self._read_receipt_locked(job)
        if receipt is not None:
            return BackupResult(
                enabled=receipt["backupGeneration"] is not None,
                generation=receipt["backupGeneration"],
                commit_id=receipt["commitId"],
                staged_committed=True,
            )
        project_dir = self._staging_root / "projects" / job.project
        for path in sorted(project_dir.glob("{}_v*".format(job.session_id))):
            if path.is_symlink() or not path.is_dir() or path.name.endswith(".copying"):
                continue
            state = service._read_state(path, session_id=job.session_id, shared=True)
            if state is not None and state.status == "succeeded" and state.commit_id == job.capture_commit_id:
                return BackupResult(
                    enabled=True,
                    destination=path,
                    generation=state.generation,
                    commit_id=state.commit_id,
                    staged_committed=True,
                )
        source = service._source_for_backup(job.cwd, job.session_id)
        if source is not None:
            state = service._read_state(source, session_id=job.session_id, missing_ok=True)
            if state is not None and state.status == "succeeded" and state.commit_id == job.capture_commit_id:
                return BackupResult(
                    enabled=True,
                    source=source,
                    generation=state.generation,
                    commit_id=state.commit_id,
                    staged_committed=True,
                )
        return None

    def _find_shared_capture(self, job: SessionBackupJob) -> tuple[BackupResult, SessionBackupState] | None:
        service = self._backup_service
        assert service is not None
        resolver = getattr(service, "_backup_root", None)
        root = resolver() if callable(resolver) else None
        if root is None:
            return None
        with service._shared_session_lock(root, project=job.project, session_id=job.session_id):
            destination = Path(root) / "projects" / job.project / job.session_id
            state = service._read_state(destination, session_id=job.session_id, shared=True, missing_ok=True)
            if state is None or state.status != "succeeded" or state.commit_id != job.capture_commit_id:
                return None
            return BackupResult(
                enabled=True,
                destination=destination,
                generation=state.generation,
                commit_id=state.commit_id,
                shared_committed=True,
                staged_committed=True,
            ), state

    def _read_receipt_locked(self, job: SessionBackupJob) -> dict[str, Any] | None:
        try:
            receipt = json.loads(self._receipt_path(job).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(receipt, dict) or any(
            receipt.get(key) != value
            for key, value in {
                "version": _JOB_DOCUMENT_VERSION,
                "jobId": job.job_id,
                "project": job.project,
                "sessionId": job.session_id,
                "boundary": job.boundary,
                "coveredThroughBusinessRevision": job.business_revision,
            }.items()
        ):
            raise SessionBackupHandoffError("session backup completion receipt identity conflicts")
        generation = receipt.get("backupGeneration")
        commit = receipt.get("commitId")
        if generation is None and commit is None:
            return receipt  # Backup disabled, also terminal for stale writers.
        if not isinstance(generation, int) or generation <= 0 or commit != job.capture_commit_id:
            raise SessionBackupHandoffError("session backup completion receipt capture identity conflicts")
        return receipt

    def _canonical_job_locked(self, job: SessionBackupJob) -> SessionBackupJob:
        assert self.pending_root is not None
        path = self.pending_root / "{}.json".format(job.job_id)
        try:
            canonical = SessionBackupJob.from_document(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            receipt = self._read_receipt_locked(job)
            if receipt is None:
                raise SessionBackupHandoffError("session backup pending job disappeared without completion receipt")
            return replace(
                job,
                capture_started=True,
                staged_generation=receipt["backupGeneration"],
                staged_commit_id=receipt["commitId"],
                callback_completed=True,
            )
        if (
            canonical is None
            or replace(
                canonical,
                capture_started=job.capture_started,
                staged_generation=job.staged_generation,
                staged_commit_id=job.staged_commit_id,
                callback_completed=job.callback_completed,
                attempts=job.attempts,
                error=job.error,
            )
            != job
        ):
            raise SessionBackupHandoffError("session backup pending job identity conflicts")
        return canonical

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
        with cross_process_file_lock(self._session_state_dir(job.project, job.session_id) / _COORDINATOR_LOCK_FILENAME):
            self._write_pending_job_locked(job)

    def _write_pending_job_locked(self, job: SessionBackupJob, *, create: bool = False) -> None:
        pending_root = self.pending_root
        assert pending_root is not None
        if self._read_receipt_locked(job) is not None:
            return
        if not create:
            current = self._canonical_job_locked(job)
            if (
                current.staged_commit_id
                and job.staged_commit_id
                and (
                    current.staged_commit_id != job.staged_commit_id
                    or current.staged_generation != job.staged_generation
                )
            ):
                raise SessionBackupHandoffError("session backup durable staged proof conflicts")
            job = replace(
                job,
                capture_started=current.capture_started or job.capture_started,
                staged_generation=current.staged_generation or job.staged_generation,
                staged_commit_id=current.staged_commit_id or job.staged_commit_id,
                callback_completed=current.callback_completed or job.callback_completed,
                attempts=max(current.attempts, job.attempts),
            )
        ensure_private_dir(pending_root)
        try:
            atomic_write_json(pending_root / "{}.json".format(job.job_id), job.to_document(), durable=True)
        except OSError as exc:
            if create:
                raise
            raise _LocalJobCommitError(job, "pending") from exc

    def _pending_documents(self) -> list[tuple[Path, SessionBackupJob]]:
        pending_root = self.pending_root
        if pending_root is None:
            return []
        try:
            pending_mode = pending_root.stat().st_mode
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise SessionBackupHandoffError(
                "session backup pending jobs could not be loaded: {}".format(type(exc).__name__)
            ) from exc
        if not stat.S_ISDIR(pending_mode):
            raise SessionBackupHandoffError("session backup pending jobs could not be loaded: invalid directory")
        jobs: list[tuple[Path, SessionBackupJob]] = []
        for path in sorted(pending_root.glob("*.json")):
            if path.is_symlink():
                continue
            try:
                if not stat.S_ISREG(path.stat().st_mode):
                    continue
                job = SessionBackupJob.from_document(json.loads(path.read_text(encoding="utf-8")))
            except FileNotFoundError:
                continue  # A completed job may have been removed since enumeration.
            except (OSError, ValueError) as exc:
                raise SessionBackupHandoffError(
                    "session backup pending jobs could not be loaded: {}".format(type(exc).__name__)
                ) from exc
            if job is None:
                raise SessionBackupHandoffError("session backup pending jobs could not be loaded: invalid record")
            jobs.append((path, job))
        return jobs

    def _load_pending_jobs(self) -> list[SessionBackupJob]:
        try:
            jobs: list[SessionBackupJob] = []
            for path, job in self._pending_documents():
                with cross_process_file_lock(
                    self._session_state_dir(job.project, job.session_id) / _COORDINATOR_LOCK_FILENAME
                ):
                    if self._read_receipt_locked(job) is not None:
                        self._remove_pending_marker(path)
                        continue
                    jobs.append(self._canonical_job_locked(job))
            return sorted(jobs, key=lambda item: (item.project, item.session_id, item.fence))
        except SessionBackupHandoffError:
            raise
        except Exception as exc:
            raise SessionBackupHandoffError(
                "session backup pending jobs could not be loaded: {}".format(type(exc).__name__)
            ) from exc

    def _remove_pending_marker(self, path: Path) -> None:
        with suppress(FileNotFoundError):
            path.unlink()
            fsync_parent_dir(path)
        parent = path.parent
        with suppress(OSError):
            parent.rmdir()

    def _receipt_path(self, job: SessionBackupJob) -> Path:
        return self._session_state_dir(job.project, job.session_id) / "receipts" / "{}.json".format(job.job_id)

    def _project_for_session(self, cwd: str, session_id: str) -> str | None:
        backup_service = self._backup_service
        if backup_service is None:
            return None
        source = backup_service._source_for_backup(cwd, session_id)
        return None if source is None else Path(source).parent.name

    def _session_state_dir(self, project: str, session_id: str) -> Path:
        assert self._state_root is not None
        return self._state_root / _COORDINATOR_STATE_DIRNAME / project / session_id
