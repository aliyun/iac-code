from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from darabonba.exceptions import RetryError, UnretryableException
from darabonba.policy.retry import RetryPolicyContext
from Tea.exceptions import UnretryableException as LegacyTeaUnretryableException
from Tea.request import TeaRequest

from iac_code.tools.cloud.aliyun.retry_policy import (
    RetryBudget,
    RetryExhausted,
    RetryReason,
    classify_transport_failure,
    map_aiohttp_retry_reason,
    map_httpx_retry_reason,
    map_retryable_status,
    map_tea_retry_reason,
    retry_delay,
    retry_eligible,
)


class FakeClock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def explicit_causes(error: BaseException) -> list[BaseException]:
    causes: list[BaseException] = []
    current = error.__cause__
    while current is not None:
        causes.append(current)
        current = current.__cause__
    return causes


@pytest.mark.asyncio
async def test_retry_budget_is_atomic_and_shared_across_callers() -> None:
    budget = RetryBudget(deadline=20.0, clock=FakeClock())

    attempts = await asyncio.gather(*(budget.acquire() for _ in range(3)))

    assert attempts == [1, 2, 3]
    assert budget.attempts == 3
    with pytest.raises(RetryExhausted):
        await budget.acquire()


@pytest.mark.asyncio
async def test_retry_budget_rejects_attempt_at_deadline() -> None:
    budget = RetryBudget(deadline=10.0, clock=FakeClock())

    with pytest.raises(RetryExhausted):
        await budget.acquire()
    assert budget.attempts == 0


@pytest.mark.asyncio
async def test_retry_budget_preserves_previous_reason_when_sleep_crosses_deadline() -> None:
    budget = RetryBudget(deadline=10.0, clock=FakeClock())

    with pytest.raises(RetryExhausted) as raised:
        await budget.acquire(reason=RetryReason.READ_TIMEOUT)

    assert raised.value.outcome == "read_timeout"
    assert raised.value.reason is RetryReason.READ_TIMEOUT
    assert budget.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retryable_call", "expected_outcome", "expected_reason"),
    [
        (True, "read_timeout", RetryReason.READ_TIMEOUT),
        (False, "unknown_after_transport_error", None),
    ],
)
async def test_retry_budget_deadline_cancels_attempt_and_waits_for_cleanup(
    retryable_call: bool,
    expected_outcome: str,
    expected_reason: RetryReason | None,
) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def blocked_attempt() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    budget = RetryBudget(deadline=time.monotonic() + 0.05)

    with pytest.raises(RetryExhausted) as raised:
        await budget.run_attempt(blocked_attempt, retryable_call=retryable_call)

    assert started.is_set()
    assert cleaned.is_set()
    assert raised.value.outcome == expected_outcome
    assert raised.value.reason is expected_reason


@pytest.mark.asyncio
async def test_retry_budget_repeated_cancellation_still_waits_for_attempt_cleanup() -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def blocked_attempt() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            finally:
                cleanup_finished.set()

    budget = RetryBudget(deadline=time.monotonic() + 60.0)
    runner = asyncio.create_task(budget.run_attempt(blocked_attempt, retryable_call=True))
    await started.wait()

    runner.cancel()
    await cleanup_started.wait()
    runner.cancel()
    await asyncio.sleep(0)

    assert not runner.done()
    assert not cleanup_finished.is_set()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_retry_budget_preserves_first_cancellation_and_chains_later_cancellations() -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    propagated: list[asyncio.CancelledError] = []

    async def blocked_attempt() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    budget = RetryBudget(deadline=time.monotonic() + 60.0)

    async def run() -> None:
        try:
            await budget.run_attempt(blocked_attempt, retryable_call=True)
        except asyncio.CancelledError as error:
            propagated.append(error)
            raise

    runner = asyncio.create_task(run())
    await started.wait()

    try:
        runner.cancel("first cancellation")
        await cleanup_started.wait()
        runner.cancel("second cancellation")
        await asyncio.sleep(0)
        runner.cancel("third cancellation")
        await asyncio.sleep(0)

        assert not runner.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert propagated[0].args == ("first cancellation",)
        assert [cause.args for cause in explicit_causes(propagated[0])] == [
            ("second cancellation",),
            ("third cancellation",),
        ]
    finally:
        release_cleanup.set()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_retry_budget_chains_late_operation_error_to_first_cancellation() -> None:
    started = asyncio.Event()
    late_error = OSError("late operation failure")
    propagated: list[asyncio.CancelledError] = []

    async def blocked_attempt() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise late_error

    budget = RetryBudget(deadline=time.monotonic() + 60.0)

    async def run() -> None:
        try:
            await budget.run_attempt(blocked_attempt, retryable_call=True)
        except asyncio.CancelledError as error:
            propagated.append(error)
            raise

    runner = asyncio.create_task(run())
    await started.wait()
    runner.cancel("caller cancellation")

    with pytest.raises(asyncio.CancelledError):
        await runner

    assert propagated[0].args == ("caller cancellation",)
    assert propagated[0].__cause__ is late_error


@pytest.mark.asyncio
async def test_retry_budget_chains_late_operation_error_to_timeout() -> None:
    started = asyncio.Event()
    late_error = OSError("late operation failure")

    async def blocked_attempt() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise late_error

    budget = RetryBudget(deadline=time.monotonic() + 0.01)

    with pytest.raises(RetryExhausted) as raised:
        await budget.run_attempt(blocked_attempt, retryable_call=True)

    assert started.is_set()
    assert raised.value.__cause__ is late_error


@pytest.mark.asyncio
async def test_retry_budget_preserves_timeout_before_later_parent_cancellation() -> None:
    started = asyncio.Event()
    operation_cancelled = asyncio.Event()
    release_operation = asyncio.Event()

    async def blocked_attempt() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            operation_cancelled.set()
            await release_operation.wait()

    budget = RetryBudget(deadline=time.monotonic() + 0.01)
    runner = asyncio.create_task(budget.run_attempt(blocked_attempt, retryable_call=True))
    await started.wait()

    try:
        await operation_cancelled.wait()
        runner.cancel("late parent cancellation")
        await asyncio.sleep(0)

        assert not runner.done()
        release_operation.set()
        with pytest.raises(RetryExhausted) as raised:
            await runner

        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        assert raised.value.__cause__.args == ("late parent cancellation",)
    finally:
        release_operation.set()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_retry_budget_chains_abandon_error_for_late_result_to_timeout() -> None:
    started = asyncio.Event()
    resource = object()
    abandon_error = OSError("abandon failed")
    abandoned: list[object] = []

    async def blocked_attempt() -> object:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return resource

    async def abandon(value: object) -> None:
        abandoned.append(value)
        raise abandon_error

    budget = RetryBudget(deadline=time.monotonic() + 0.01)

    with pytest.raises(RetryExhausted) as raised:
        await budget.run_attempt(blocked_attempt, retryable_call=True, abandon_result=abandon)

    assert started.is_set()
    assert abandoned == [resource]
    assert raised.value.__cause__ is abandon_error


@pytest.mark.asyncio
async def test_retry_budget_reaches_terminal_cleanup_and_preserves_all_late_errors() -> None:
    started = asyncio.Event()
    operation_cancelled = asyncio.Event()
    release_operation = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    resource = object()
    cleanup_error = OSError("cleanup failed")
    terminal_tasks: list[asyncio.Task[object]] = []
    propagated: list[asyncio.CancelledError] = []

    async def blocked_attempt() -> object:
        task = asyncio.current_task()
        assert task is not None
        terminal_tasks.append(task)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            operation_cancelled.set()
            await release_operation.wait()
            return resource

    async def abandon(value: object) -> None:
        assert value is resource
        task = asyncio.current_task()
        assert task is not None
        terminal_tasks.append(task)
        cleanup_started.set()
        try:
            await release_cleanup.wait()
            raise cleanup_error
        finally:
            cleanup_finished.set()

    budget = RetryBudget(deadline=time.monotonic() + 60.0)

    async def run() -> None:
        try:
            await budget.run_attempt(blocked_attempt, retryable_call=True, abandon_result=abandon)
        except asyncio.CancelledError as error:
            propagated.append(error)
            raise

    runner = asyncio.create_task(run())
    await started.wait()

    try:
        runner.cancel("first cancellation")
        await operation_cancelled.wait()
        runner.cancel("second cancellation")
        await asyncio.sleep(0)
        release_operation.set()
        await cleanup_started.wait()
        runner.cancel("third cancellation")
        await asyncio.sleep(0)

        assert not runner.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await runner

        causes = explicit_causes(propagated[0])
        assert propagated[0].args == ("first cancellation",)
        assert causes[0] is cleanup_error
        assert [cause.args for cause in causes[1:]] == [
            ("second cancellation",),
            ("third cancellation",),
        ]
        assert cleanup_finished.is_set()
        assert all(task.done() for task in terminal_tasks)
    finally:
        release_operation.set()
        release_cleanup.set()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_retry_budget_cleans_result_abandoned_during_parent_cancellation() -> None:
    parent = asyncio.current_task()
    assert parent is not None
    abandoned: list[object] = []
    resource = object()

    async def operation() -> object:
        asyncio.get_running_loop().call_soon(parent.cancel)
        return resource

    async def cleanup(value: object) -> None:
        abandoned.append(value)

    budget = RetryBudget(deadline=time.monotonic() + 60.0)

    with pytest.raises(asyncio.CancelledError):
        await budget.run_attempt(
            operation,
            retryable_call=True,
            abandon_result=cleanup,
        )

    assert abandoned == [resource]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_only_bodyless_reads_are_retry_eligible(method: str) -> None:
    assert retry_eligible(operation_type="read", method=method, has_body=False)


@pytest.mark.parametrize(
    ("operation_type", "method", "has_body"),
    [
        ("write", "GET", False),
        ("read", "POST", False),
        ("read", "GET", True),
        (None, "GET", False),
    ],
)
def test_other_call_shapes_are_not_retry_eligible(operation_type: str | None, method: str, has_body: bool) -> None:
    assert not retry_eligible(operation_type=operation_type, method=method, has_body=has_body)


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (httpx.PoolTimeout("pool"), RetryReason.POOL_UNAVAILABLE),
        (httpx.ConnectTimeout("connect"), RetryReason.CONNECT_TIMEOUT),
        (httpx.ConnectError("connect"), RetryReason.CONNECT_ERROR),
        (httpx.ReadTimeout("read"), RetryReason.READ_TIMEOUT),
        (httpx.ReadError("read"), RetryReason.READ_ERROR),
        (httpx.RemoteProtocolError("protocol"), RetryReason.PROTOCOL_ERROR),
        (RuntimeError("ConnectTimeout hidden by text"), None),
    ],
)
def test_httpx_exception_mapping_uses_exact_types(exception: Exception, expected: RetryReason | None) -> None:
    assert map_httpx_retry_reason(exception) is expected


@pytest.mark.parametrize(
    ("inner", "expected"),
    [
        (aiohttp.ServerTimeoutError("timeout"), RetryReason.READ_TIMEOUT),
        (
            aiohttp.ClientConnectorError(SimpleNamespace(ssl=False, host="example.com", port=443), OSError("connect")),
            RetryReason.CONNECT_ERROR,
        ),
        (aiohttp.ClientConnectionError("connection"), RetryReason.CONNECT_ERROR),
        (aiohttp.ClientPayloadError("payload"), RetryReason.STREAM_READ_ERROR),
        (aiohttp.ClientError("unknown"), None),
    ],
)
def test_tea_mapping_unwraps_installed_darabonba_exception_context(
    inner: Exception, expected: RetryReason | None
) -> None:
    wrapper = UnretryableException(RetryPolicyContext(http_request=TeaRequest(), exception=inner))

    assert map_tea_retry_reason(wrapper) is expected


def test_tea_mapping_does_not_unwrap_legacy_base_wrapper() -> None:
    wrapper = LegacyTeaUnretryableException(TeaRequest(), aiohttp.ClientConnectionError("connection"))

    assert map_tea_retry_reason(wrapper) is None


@pytest.mark.parametrize(
    ("inner", "expected"),
    [
        (aiohttp.ServerTimeoutError("timeout"), RetryReason.READ_TIMEOUT),
        (
            aiohttp.ClientConnectorError(SimpleNamespace(ssl=False, host="example.com", port=443), OSError("connect")),
            RetryReason.CONNECT_ERROR,
        ),
        (OSError("unknown io failure"), None),
    ],
)
def test_tea_mapping_unwraps_darabonba_core_retry_error_context(inner: Exception, expected: RetryReason | None) -> None:
    core_error = RetryError(str(inner))
    core_error.__context__ = inner
    wrapper = UnretryableException(RetryPolicyContext(http_request=TeaRequest(), exception=core_error))

    assert map_tea_retry_reason(wrapper) is expected


def test_unknown_tea_wrapper_does_not_retry() -> None:
    class UnknownWrapperError(Exception):
        def __init__(self, inner_exception: Exception) -> None:
            self.inner_exception = inner_exception

    assert map_tea_retry_reason(UnknownWrapperError(httpx.ConnectError("hidden"))) is None


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (aiohttp.ConnectionTimeoutError("connect timeout"), RetryReason.CONNECT_TIMEOUT),
        (aiohttp.SocketTimeoutError("read timeout"), RetryReason.READ_TIMEOUT),
        (aiohttp.ServerTimeoutError("timeout"), RetryReason.READ_TIMEOUT),
        (aiohttp.ClientConnectionError("connection"), RetryReason.CONNECT_ERROR),
        (aiohttp.ClientPayloadError("payload"), RetryReason.STREAM_READ_ERROR),
        (
            aiohttp.ClientConnectorError(SimpleNamespace(ssl=False, host="example.com", port=443), OSError("connect")),
            RetryReason.CONNECT_ERROR,
        ),
        (aiohttp.ClientError("unknown"), None),
    ],
)
def test_aiohttp_exception_mapping_uses_explicit_allowlist(exception: Exception, expected: RetryReason | None) -> None:
    assert map_aiohttp_retry_reason(exception) is expected


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_retryable_status_allowlist(status: int) -> None:
    assert map_retryable_status(status) is RetryReason.RETRYABLE_STATUS


@pytest.mark.parametrize("status", [400, 408, 500, 501, 505])
def test_other_statuses_do_not_retry(status: int) -> None:
    assert map_retryable_status(status) is None


def test_backoff_uses_200_and_800_ms_with_bounded_jitter() -> None:
    clock = FakeClock()
    budget = RetryBudget(deadline=20.0, clock=clock, random=lambda: 1.0)

    assert retry_delay(budget, failed_attempt=1) == pytest.approx(0.24)
    assert retry_delay(budget, failed_attempt=2) == pytest.approx(0.96)


@pytest.mark.parametrize(("value", "expected"), [("0", 0.0), ("1.5", 1.5), ("2", 2.0)])
def test_retry_after_accepts_zero_through_two_seconds(value: str, expected: float) -> None:
    budget = RetryBudget(deadline=20.0, clock=FakeClock())

    assert retry_delay(budget, failed_attempt=1, retry_after=value) == expected


@pytest.mark.parametrize("value", ["-1", "2.01", "soon", " 1", "1 "])
def test_invalid_retry_after_falls_back_to_backoff(value: str) -> None:
    budget = RetryBudget(deadline=20.0, clock=FakeClock(), random=lambda: 0.0)

    assert retry_delay(budget, failed_attempt=1, retry_after=value) == pytest.approx(0.2)


def test_delay_that_reaches_deadline_is_rejected() -> None:
    budget = RetryBudget(deadline=10.2, clock=FakeClock(), random=lambda: 0.0)

    with pytest.raises(RetryExhausted) as raised:
        retry_delay(budget, failed_attempt=1, reason=RetryReason.RETRYABLE_STATUS)

    assert raised.value.outcome == "retryable_status"
    assert raised.value.reason is RetryReason.RETRYABLE_STATUS
    assert str(raised.value) == "retryable_status"


def test_write_failure_distinguishes_pre_connect_from_unknown_outcome() -> None:
    assert classify_transport_failure(httpx.PoolTimeout("pool"), retryable_call=False) == "pre_connect_failure"
    assert classify_transport_failure(httpx.ConnectTimeout("connect"), retryable_call=False) == "pre_connect_failure"
    assert (
        classify_transport_failure(httpx.ConnectError("may have connected"), retryable_call=False)
        == "unknown_after_transport_error"
    )
    assert (
        classify_transport_failure(httpx.ReadError("partial response"), retryable_call=False)
        == "unknown_after_transport_error"
    )
