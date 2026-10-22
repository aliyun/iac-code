"""Bounded recovery of transient public metadata failures, without cloud credentials."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
import pytest

from iac_code.tools.cloud.aliyun import openmeta as module
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.contract_store import canonical_input_sha256
from iac_code.tools.cloud.aliyun.openmeta import OpenMetaClient
from iac_code.tools.cloud.aliyun.retry_policy import retry_delay
from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services
from iac_code.types.permissions import InvocationBinding, ToolPermissionContext


@pytest.fixture(autouse=True)
def immediate_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "retry_delay", lambda *_args, **_kwargs: 0.0)


def api_response() -> httpx.Response:
    fixture = Path(__file__).parent / "fixtures/openmeta/ecs_describe_instances.json"
    return httpx.Response(200, json=json.loads(fixture.read_text(encoding="utf-8")))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [429, 500, 502, 503, 504, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError],
)
async def test_transient_metadata_failure_recovers_and_is_cached(tmp_path: Path, failure) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure("temporary failure")
        return api_response()

    client = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    try:
        recovered = await client.get_api("Ecs", "2014-05-26", "DescribeInstances")
        cached = await client.get_api("Ecs", "2014-05-26", "DescribeInstances")
        assert recovered.value is not None
        assert cached.cache_status == "memory_fresh"
        assert calls == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_exhausted_metadata_retries_do_not_negative_cache(tmp_path: Path) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls <= 3 else api_response()

    client = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    try:
        failed = await client.get_api("Ecs", "2014-05-26", "DescribeInstances")
        assert failed.error == "temporarily_unavailable"
        assert calls == 3
        recovered = await client.get_api("Ecs", "2014-05-26", "DescribeInstances")
        assert recovered.value is not None
        assert calls == 4
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 501, 505])
async def test_permanent_metadata_failure_does_not_retry(tmp_path: Path, status: int) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    client = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    try:
        result = await client.get_api("Ecs", "2014-05-26", "DescribeInstances")
        assert result.value is None
        assert calls == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_metadata_deadline_cancels_inflight_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_REQUEST_TIMEOUT_SECONDS", 0.03)
    cancelled = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(client.get_api("Ecs", "2014-05-26", "DescribeInstances"), timeout=1)
        assert result.error == "temporarily_unavailable"
        assert cancelled.is_set()
        assert time.monotonic() - started < 0.5
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_backoff_cannot_cross_metadata_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "retry_delay", retry_delay)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "2"})

    client = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    started = time.monotonic()
    try:
        result = await client.get_api("Ecs", "2014-05-26", "DescribeInstances")
        assert result.error == "temporarily_unavailable"
        assert calls == 1
        assert time.monotonic() - started < 0.5
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_waiters_share_metadata_retries(tmp_path: Path) -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(503) if calls < 3 else api_response()

    client = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    tasks = [asyncio.create_task(client.get_api("Ecs", "2014-05-26", "DescribeInstances")) for _ in range(10)]
    try:
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(result.value is not None for result in results)
        assert calls == 3
    finally:
        await client.aclose()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_retry_diagnostics_exclude_query_and_exception_payload(tmp_path: Path, caplog) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("PRIVATE")

    client = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            result = await client._request_json("https://api.aliyun.com/meta/v1/demo?token=PRIVATE", max_bytes=1024)
        assert result[1] == "temporarily_unavailable"
        assert "ConnectError" in caplog.text
        assert "attempt=3" in caplog.text
        assert "elapsed_ms=" in caplog.text
        assert "/meta/v1/demo" in caplog.text
        assert "PRIVATE" not in caplog.text
        assert "token=" not in caplog.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_final_metadata_failure_tells_model_to_retry_without_using_credentials(tmp_path: Path) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    runtime = create_aliyun_runtime_services(cache_dir=tmp_path, openmeta_transport=httpx.MockTransport(handler))
    runtime.credential_provider = lambda: pytest.fail("metadata failure must precede cloud credential access")
    tool_input = {"product": "SWAS-OPEN", "action": "ListPlans", "region_id": "cn-hangzhou"}
    binding = InvocationBinding("runtime", "session", "call", "aliyun_api", canonical_input_sha256(tool_input))
    try:
        permission = await AliyunApi(services=runtime).check_permissions(
            tool_input,
            ToolPermissionContext(cwd=str(tmp_path), invocation_binding=binding, pipeline_mode=True),
        )
        assert permission.behavior == "deny"
        assert permission.message is not None
        assert "Retry the same tool call once" in permission.message
        assert calls == 3
    finally:
        await runtime.aclose()
