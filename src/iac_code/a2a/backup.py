from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar, cast

from iac_code.i18n import _
from iac_code.services.session_backup import BackupReason, SessionBackupBlocked
from iac_code.services.session_backup_state import BackupPublicationProof

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_T = TypeVar("_T")


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
