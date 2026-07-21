"""Unified retry accounting and transport-specific retry mappings."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, NoReturn, TypeVar, cast

import aiohttp
import httpx
from darabonba.exceptions import RetryError, UnretryableException

_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_AFTER = re.compile(r"^(?:0(?:\.\d+)?|1(?:\.\d+)?|2(?:\.0+)?)$")
_T = TypeVar("_T")


@dataclass(frozen=True)
class _TerminalOutcome(Generic[_T]):
    value: _T | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _ProtectedOutcome(Generic[_T]):
    terminal: _TerminalOutcome[_T]
    interruptions: tuple[BaseException, ...] = ()


class RetryExhausted(RuntimeError):  # noqa: N818 - name is part of the reviewed transport contract
    """The shared attempt count or call deadline has been exhausted."""

    def __init__(
        self,
        *,
        outcome: str = "target_transport_failure",
        reason: RetryReason | None = None,
    ) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.reason = reason


class RetryReason(str, Enum):
    POOL_UNAVAILABLE = "pool_unavailable"
    CONNECT_TIMEOUT = "connect_timeout"
    CONNECT_ERROR = "connect_error"
    READ_TIMEOUT = "read_timeout"
    READ_ERROR = "read_error"
    PROTOCOL_ERROR = "protocol_error"
    STREAM_READ_ERROR = "stream_read_error"
    RETRYABLE_STATUS = "retryable_status"


class TransportFailure(RuntimeError):  # noqa: N818 - name describes the reviewed transport contract
    """A mapped transport error with an outcome callers can audit safely."""

    def __init__(self, *, outcome: str, reason: RetryReason | None) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.reason = reason


@dataclass
class RetryBudget:
    deadline: float
    max_attempts: int = 3
    attempts: int = 0
    clock: Callable[[], float] = time.monotonic
    random: Callable[[], float] = lambda: 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def acquire(self, *, reason: RetryReason | None = None) -> int:
        async with self._lock:
            if self.attempts >= self.max_attempts or self.clock() >= self.deadline:
                raise RetryExhausted(
                    outcome=reason.value if reason is not None else "target_transport_failure",
                    reason=reason,
                )
            self.attempts += 1
            return self.attempts

    async def run_attempt(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        retryable_call: bool,
        abandon_result: Callable[[_T], Awaitable[None] | None] | None = None,
    ) -> _T:
        """Run one acquired attempt without allowing in-flight I/O to cross the call deadline."""
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise _deadline_exhausted(retryable_call)
        task = asyncio.ensure_future(operation())
        try:
            done, _pending = await asyncio.wait({task}, timeout=remaining)
        except BaseException as error:
            late_errors = await _cancel_and_reap(task, abandon_result=abandon_result)
            _raise_with_causes(error, late_errors)
        if task in done:
            result = task.result()
            if self.clock() >= self.deadline:
                cleanup = await _abandon_value(result, abandon_result)
                _raise_with_causes(_deadline_exhausted(retryable_call), _protected_errors(cleanup))
            return result
        late_errors = await _cancel_and_reap(task, abandon_result=abandon_result)
        _raise_with_causes(_deadline_exhausted(retryable_call), late_errors)


def _deadline_exhausted(retryable_call: bool) -> RetryExhausted:
    reason = RetryReason.READ_TIMEOUT if retryable_call else None
    return RetryExhausted(
        outcome=reason.value if reason is not None else "unknown_after_transport_error",
        reason=reason,
    )


async def _cancel_and_reap(
    task: asyncio.Future[_T],
    *,
    abandon_result: Callable[[_T], Awaitable[None] | None] | None = None,
) -> tuple[BaseException, ...]:
    if not task.done():
        task.cancel()
    operation = await _protected_terminal_wait(task)
    terminal_errors: tuple[BaseException, ...] = ()
    cleanup: _ProtectedOutcome[None] | None = None
    if operation.terminal.error is not None:
        if not task.cancelled():
            terminal_errors = (operation.terminal.error,)
    else:
        cleanup = await _abandon_value(cast(_T, operation.terminal.value), abandon_result)
        if cleanup.terminal.error is not None:
            terminal_errors = (cleanup.terminal.error,)
    cleanup_interruptions = cleanup.interruptions if cleanup is not None else ()
    return terminal_errors + operation.interruptions + cleanup_interruptions


async def _capture_outcome(awaitable: Awaitable[_T]) -> _TerminalOutcome[_T]:
    try:
        return _TerminalOutcome(value=await awaitable)
    except BaseException as error:
        return _TerminalOutcome(error=error)


async def _protected_terminal_wait(awaitable: Awaitable[_T]) -> _ProtectedOutcome[_T]:
    waiter = asyncio.create_task(_capture_outcome(awaitable))
    interruptions: list[BaseException] = []
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except BaseException as error:
            interruptions.append(error)
    return _ProtectedOutcome(waiter.result(), tuple(interruptions))


async def _abandon_value(
    result: _T,
    abandon_result: Callable[[_T], Awaitable[None] | None] | None,
) -> _ProtectedOutcome[None]:
    if abandon_result is None:
        return _ProtectedOutcome(_TerminalOutcome())

    async def cleanup() -> None:
        value = abandon_result(result)
        if inspect.isawaitable(value):
            await value

    return await _protected_terminal_wait(cleanup())


def _protected_errors(outcome: _ProtectedOutcome[_T]) -> tuple[BaseException, ...]:
    terminal = (outcome.terminal.error,) if outcome.terminal.error is not None else ()
    return terminal + outcome.interruptions


def _raise_with_causes(primary: BaseException, late_errors: tuple[BaseException, ...]) -> NoReturn:
    cause = _stable_cause_chain(late_errors)
    if cause is None:
        raise primary
    raise primary from cause


def _stable_cause_chain(errors: tuple[BaseException, ...]) -> BaseException | None:
    unique: list[BaseException] = []
    seen: set[int] = set()
    for error in errors:
        if id(error) not in seen:
            unique.append(error)
            seen.add(id(error))
    for current, following in zip(unique, unique[1:]):
        tail = current
        chain_ids = {id(tail)}
        while tail.__cause__ is not None and id(tail.__cause__) not in chain_ids:
            tail = tail.__cause__
            chain_ids.add(id(tail))
        if id(following) not in chain_ids:
            tail.__cause__ = following
            tail.__suppress_context__ = True
    return unique[0] if unique else None


def retry_eligible(*, operation_type: str | None, method: str, has_body: bool) -> bool:
    return operation_type == "read" and method.upper() in _RETRYABLE_METHODS and not has_body


def map_httpx_retry_reason(exception: BaseException) -> RetryReason | None:
    mappings: tuple[tuple[type[BaseException], RetryReason], ...] = (
        (httpx.PoolTimeout, RetryReason.POOL_UNAVAILABLE),
        (httpx.ConnectTimeout, RetryReason.CONNECT_TIMEOUT),
        (httpx.ConnectError, RetryReason.CONNECT_ERROR),
        (httpx.ReadTimeout, RetryReason.READ_TIMEOUT),
        (httpx.ReadError, RetryReason.READ_ERROR),
        (httpx.RemoteProtocolError, RetryReason.PROTOCOL_ERROR),
    )
    for exception_type, reason in mappings:
        if type(exception) is exception_type:
            return reason
    return None


def map_retryable_status(status: int) -> RetryReason | None:
    return RetryReason.RETRYABLE_STATUS if status in _RETRYABLE_STATUSES else None


def map_tea_retry_reason(exception: BaseException) -> RetryReason | None:
    inner = _tea_retry_inner(exception)
    return map_aiohttp_retry_reason(inner) if inner is not None else None


def _tea_retry_inner(exception: BaseException) -> BaseException | None:
    if type(exception) is not UnretryableException:
        return None
    inner = exception.inner_exception
    if type(inner) is RetryError:
        nested = inner.__cause__ or inner.__context__
        return nested if isinstance(nested, BaseException) else None
    return inner


def map_aiohttp_retry_reason(exception: BaseException) -> RetryReason | None:
    mappings: dict[type[BaseException], RetryReason] = {
        aiohttp.ConnectionTimeoutError: RetryReason.CONNECT_TIMEOUT,
        aiohttp.SocketTimeoutError: RetryReason.READ_TIMEOUT,
        aiohttp.ServerTimeoutError: RetryReason.READ_TIMEOUT,
        aiohttp.ClientConnectorError: RetryReason.CONNECT_ERROR,
        aiohttp.ClientConnectionError: RetryReason.CONNECT_ERROR,
        aiohttp.ClientPayloadError: RetryReason.STREAM_READ_ERROR,
    }
    return mappings.get(type(exception))


def is_transport_failure(exception: BaseException) -> bool:
    """Return whether an exception is a known transport envelope, independently of retry eligibility."""
    return isinstance(exception, httpx.TransportError) or type(exception) is UnretryableException


def retry_delay(
    budget: RetryBudget,
    *,
    failed_attempt: int,
    retry_after: str | None = None,
    reason: RetryReason | None = None,
) -> float:
    delay: float | None = None
    if retry_after is not None and _RETRY_AFTER.fullmatch(retry_after):
        delay = float(retry_after)
    if delay is None:
        base = (0.2, 0.8)[min(max(failed_attempt - 1, 0), 1)]
        delay = base * (1.0 + 0.2 * min(max(budget.random(), 0.0), 1.0))
    if budget.clock() + delay >= budget.deadline:
        raise RetryExhausted(
            outcome=reason.value if reason is not None else "target_transport_failure",
            reason=reason,
        )
    return delay


def classify_transport_failure(exception: BaseException, *, retryable_call: bool) -> str:
    reason = map_httpx_retry_reason(exception)
    tea_inner = _tea_retry_inner(exception)
    if type(exception) is UnretryableException:
        reason = map_tea_retry_reason(exception)
    if retryable_call:
        return reason.value if reason is not None else "unknown_after_transport_error"
    if tea_inner is not None and type(tea_inner) is aiohttp.ClientConnectorError:
        return "pre_connect_failure"
    if reason in {RetryReason.POOL_UNAVAILABLE, RetryReason.CONNECT_TIMEOUT}:
        return "pre_connect_failure"
    return "unknown_after_transport_error"
