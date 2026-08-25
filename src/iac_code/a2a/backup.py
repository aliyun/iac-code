from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from iac_code.i18n import _
from iac_code.services.session_backup import BackupReason, SessionBackupBlocked
from iac_code.services.session_backup_state import BackupPublicationProof

logger = logging.getLogger(__name__)

_DEFAULT_SHARED_COMMIT_TIMEOUT_SECONDS = 30.0


async def run_sync_fenced(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Delay coroutine cancellation until the synchronous mutation has actually stopped."""
    thread_task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(thread_task)
    except asyncio.CancelledError:
        while not thread_task.done():
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if thread_task.done() and not thread_task.cancelled():
            with contextlib.suppress(Exception):
                thread_task.result()
        raise


async def backup_session_async(
    backup_service: Any,
    cwd: str,
    session_id: str,
    *,
    reason: BackupReason,
    critical: bool,
    metrics: Any | None = None,
    publication_proofs: dict[str, BackupPublicationProof] | None = None,
    shared_commit_timeout: float = _DEFAULT_SHARED_COMMIT_TIMEOUT_SECONDS,
) -> Any | None:
    failed_recorded = False
    try:
        kwargs: dict[str, Any] = {"reason": reason, "critical": critical}
        if publication_proofs is not None:
            kwargs["publication_proofs"] = publication_proofs
        result = await run_sync_fenced(backup_service.backup_session, cwd, session_id, **kwargs)
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
        elif (
            getattr(result, "enabled", False)
            and critical
            and reason in {BackupReason.TERMINAL, BackupReason.HANDOFF_READY}
        ):
            wait_for_shared_commit = getattr(backup_service, "wait_for_shared_commit", None)
            if callable(wait_for_shared_commit):
                result = await run_sync_fenced(
                    wait_for_shared_commit,
                    result,
                    timeout=shared_commit_timeout,
                )
        if getattr(result, "enabled", False) and getattr(result, "succeeded", True):
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
