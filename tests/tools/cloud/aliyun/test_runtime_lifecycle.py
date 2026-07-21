from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services

_CLOSE_STAGES = ("acs3_httpx", "oss_aiohttp", "openmeta_httpx")


def _install_client_close_spies(
    services: Any,
    close_stage: Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]],
) -> tuple[dict[str, Callable[[], Awaitable[None]]], Counter[str]]:
    originals = {
        "acs3_httpx": services.transport_router._transports["acs3_streaming"]._client.aclose,
        "oss_aiohttp": services.oss_http_client.close,
        "openmeta_httpx": services.openmeta._client.aclose,
    }
    calls: Counter[str] = Counter()

    def wrapped(name: str) -> Callable[[], Awaitable[None]]:
        async def close() -> None:
            calls[name] += 1
            await close_stage(name, originals[name])

        return close

    services.transport_router._transports["acs3_streaming"]._client.aclose = wrapped("acs3_httpx")
    services.oss_http_client.close = wrapped("oss_aiohttp")
    services.openmeta._client.aclose = wrapped("openmeta_httpx")
    return originals, calls


async def _close_original_clients(originals: dict[str, Callable[[], Awaitable[None]]]) -> None:
    for close in originals.values():
        try:
            await close()
        except BaseException:
            pass


@pytest.mark.parametrize("blocked_stage", _CLOSE_STAGES)
@pytest.mark.asyncio
async def test_runtime_close_is_shared_and_survives_repeated_caller_cancellation(
    tmp_path: Path,
    blocked_stage: str,
) -> None:
    services = create_aliyun_runtime_services(cache_dir=tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def close_stage(name: str, original: Callable[[], Awaitable[None]]) -> None:
        if name == blocked_stage:
            entered.set()
            await release.wait()
        await original()

    originals, calls = _install_client_close_spies(services, close_stage)
    first = asyncio.create_task(services.aclose())
    second: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        second = asyncio.create_task(services.aclose())
        await asyncio.sleep(0)
        second_shared_pending = not second.done()

        first.cancel("first-close-cancel")
        await asyncio.sleep(0)
        pending_after_first_cancel = not first.done()
        first.cancel("second-close-cancel")
        await asyncio.sleep(0)
        pending_after_second_cancel = not first.done()

        release.set()
        first_result, second_result = await asyncio.gather(first, second, return_exceptions=True)

        assert second_shared_pending
        assert pending_after_first_cancel
        assert pending_after_second_cancel
        assert isinstance(first_result, asyncio.CancelledError)
        assert second_result is None
        assert calls == Counter({stage: 1 for stage in _CLOSE_STAGES})
        assert services._closed is True

        await services.aclose()
        assert calls == Counter({stage: 1 for stage in _CLOSE_STAGES})
    finally:
        release.set()
        pending = [task for task in (first, second) if task is not None and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await _close_original_clients(originals)


@pytest.mark.parametrize("failing_stage", _CLOSE_STAGES)
@pytest.mark.asyncio
async def test_runtime_close_failure_is_retryable_and_marks_closed_only_after_success(
    tmp_path: Path,
    failing_stage: str,
) -> None:
    services = create_aliyun_runtime_services(cache_dir=tmp_path)
    failed = False

    async def close_stage(name: str, original: Callable[[], Awaitable[None]]) -> None:
        nonlocal failed
        if name == failing_stage and not failed:
            failed = True
            raise RuntimeError(f"{name} close failed")
        await original()

    originals, calls = _install_client_close_spies(services, close_stage)
    try:
        with pytest.raises(RuntimeError, match=f"{failing_stage} close failed"):
            await services.aclose()

        assert services._closed is False
        await services.aclose()
        assert services._closed is True
        assert calls[failing_stage] == 2
        assert all(calls[stage] >= 1 for stage in _CLOSE_STAGES)

        successful_counts = calls.copy()
        await services.aclose()
        assert calls == successful_counts
    finally:
        await _close_original_clients(originals)


@pytest.mark.asyncio
async def test_cancelled_runtime_close_retrieves_close_task_failure_and_can_retry(tmp_path: Path) -> None:
    services = create_aliyun_runtime_services(cache_dir=tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    failed = False
    unhandled: list[dict[str, Any]] = []

    async def close_stage(name: str, original: Callable[[], Awaitable[None]]) -> None:
        nonlocal failed
        if name == "openmeta_httpx" and not failed:
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            failed = True
            raise RuntimeError("openmeta close failed after cancellation")
        await original()

    originals, calls = _install_client_close_spies(services, close_stage)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    caller = asyncio.create_task(services.aclose())
    try:
        await entered.wait()
        caller.cancel("first-close-cancel")
        await asyncio.sleep(0)
        caller.cancel("second-close-cancel")
        await asyncio.sleep(0)
        release.set()

        result = (await asyncio.gather(caller, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert services._closed is False
        assert services._close_task is None

        await services.aclose()
        await asyncio.sleep(0)
        assert services._closed is True
        assert calls["openmeta_httpx"] == 2
        assert not unhandled
    finally:
        release.set()
        if not caller.done():
            await asyncio.gather(caller, return_exceptions=True)
        loop.set_exception_handler(previous_handler)
        await _close_original_clients(originals)


@pytest.mark.asyncio
async def test_runtime_close_reaps_openmeta_refresh_after_last_waiter_cancellation(tmp_path: Path) -> None:
    refresh_started = asyncio.Event()
    refresh_cancelled = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        refresh_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refresh_cancelled.set()
            raise

    services = create_aliyun_runtime_services(
        cache_dir=tmp_path,
        openmeta_transport=httpx.MockTransport(handler),
    )
    request_task = asyncio.create_task(services.openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances"))
    try:
        await asyncio.wait_for(refresh_started.wait(), timeout=1)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task

        await services.aclose()

        assert refresh_cancelled.is_set()
        assert services.openmeta.singleflight_size == 0
        assert not services.openmeta._singleflight._notification_tasks
    finally:
        for entry in list(services.openmeta._singleflight._entries.values()):
            entry.task.cancel()
        await asyncio.gather(
            *(entry.task for entry in list(services.openmeta._singleflight._entries.values())),
            return_exceptions=True,
        )
        await services.aclose()
