from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolutionError, EndpointResolver
from tests.tools.cloud.aliyun.test_endpoint_resolver import api_contract


class LocationReply:
    def __init__(self, endpoint: str | None | Exception, *, delayed: bool = False) -> None:
        self.endpoint = endpoint
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not delayed:
            self.release.set()

    async def resolve(
        self, product: str, version: str, region_id: str, service_code: str, credential: Any
    ) -> str | None:
        self.started.set()
        await self.release.wait()
        if isinstance(self.endpoint, Exception):
            raise self.endpoint
        return self.endpoint

    async def wait_until_started(self) -> None:
        # Bound fixture failures without asserting how quickly discovery should run.
        await asyncio.wait_for(self.started.wait(), timeout=5.0)


class LocationCacheRuntime:
    """Keep Location cache behavior independent of the shipped endpoint catalog."""

    def __init__(self, directory: Path) -> None:
        self.cache_dir = directory / "cache"
        self.catalog_path = directory / "catalog.json"
        self.unavailable_path = directory / "unavailable.json"
        self.overrides_path = directory / "overrides.yml"
        self.contract = api_contract(product="CacheProbe", version="2026-01-01", action="Read")
        self.catalog_path.write_text(
            json.dumps(
                {
                    "_meta": {"default_versions": {"CacheProbe": "2026-01-01"}},
                    "products": {
                        "CacheProbe": {
                            "2026-01-01": {
                                "location_service_code": "cacheprobe",
                                "regional_endpoints": {},
                                "global_endpoint": None,
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.unavailable_path.write_text('{"products": []}', encoding="utf-8")
        self.overrides_path.write_text("trusted_endpoint_suffixes: [aliyuncs.com]\nproducts: {}\n", encoding="utf-8")

    def create_resolver(
        self,
        location: LocationReply,
        clock: Callable[[], datetime],
        *,
        cache_writer: Callable[[Path, Mapping[str, Any]], None] | None = None,
    ) -> EndpointResolver:
        writer_options: dict[str, Any] = {"cache_writer": cache_writer} if cache_writer is not None else {}
        return EndpointResolver(
            cache_dir=self.cache_dir,
            location=location,
            clock=clock,
            catalog_path=self.catalog_path,
            unavailable_path=self.unavailable_path,
            overrides_path=self.overrides_path,
            **writer_options,
        )

    def read_entries(self) -> list[dict[str, Any]]:
        envelope = json.loads((self.cache_dir / "endpoints/location.json").read_text(encoding="utf-8"))
        return envelope["payload"]["entries"]


@pytest.mark.asyncio
async def test_overlapping_location_writes_from_independent_instances_keep_both_keys(tmp_path: Path) -> None:
    runtime = LocationCacheRuntime(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first_location = LocationReply("sae.cn-hangzhou.aliyuncs.com", delayed=True)
    first = runtime.create_resolver(first_location, lambda: now)
    second = runtime.create_resolver(LocationReply("sae.cn-shanghai.aliyuncs.com"), lambda: now)
    first_task = asyncio.create_task(first.resolve(runtime.contract, "cn-hangzhou", "fake-credential"))
    try:
        await first_location.wait_until_started()
        second_result = await second.resolve(runtime.contract, "cn-shanghai", "fake-credential")
    finally:
        first_location.release.set()
        first_result = await first_task

    assert first_result.endpoint == "sae.cn-hangzhou.aliyuncs.com"
    assert second_result.endpoint == "sae.cn-shanghai.aliyuncs.com"
    assert {entry["key"][2]: entry["endpoint"] for entry in runtime.read_entries()} == {
        "cn-hangzhou": "sae.cn-hangzhou.aliyuncs.com",
        "cn-shanghai": "sae.cn-shanghai.aliyuncs.com",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_endpoint", ["fresh.aliyuncs.com", None])
async def test_late_older_location_result_cannot_overwrite_newer_same_key(
    tmp_path: Path, newer_endpoint: str | None
) -> None:
    runtime = LocationCacheRuntime(tmp_path)
    old_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
    new_time = old_time + timedelta(minutes=1)
    old_location = LocationReply("old.aliyuncs.com", delayed=True)
    old = runtime.create_resolver(old_location, lambda: old_time)
    new = runtime.create_resolver(LocationReply(newer_endpoint), lambda: new_time)
    old_task = asyncio.create_task(old.resolve(runtime.contract, "cn-hangzhou", "fake-credential"))
    try:
        await old_location.wait_until_started()
        if newer_endpoint is None:
            with pytest.raises(EndpointResolutionError, match="endpoint_unavailable"):
                await new.resolve(runtime.contract, "cn-hangzhou", "fake-credential")
        else:
            new_result = await new.resolve(runtime.contract, "cn-hangzhou", "fake-credential")
            assert new_result.endpoint == "fresh.aliyuncs.com"
    finally:
        old_location.release.set()
        old_results = await asyncio.gather(old_task, return_exceptions=True)

    if newer_endpoint is None:
        assert isinstance(old_results[0], EndpointResolutionError)
        assert str(old_results[0]) == "endpoint_unavailable"
    else:
        assert not isinstance(old_results[0], BaseException)
        assert old_results[0].endpoint == "fresh.aliyuncs.com"
    assert runtime.read_entries()[0]["endpoint"] == newer_endpoint
    assert runtime.read_entries()[0]["fetched_at"] == "2026-07-01T00:01:00+00:00"


@pytest.mark.asyncio
async def test_initialized_location_reader_sees_another_instances_fresh_disk_entry(tmp_path: Path) -> None:
    runtime = LocationCacheRuntime(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    writer = runtime.create_resolver(LocationReply("fresh.aliyuncs.com"), lambda: now)
    reader = runtime.create_resolver(LocationReply(RuntimeError("offline")), lambda: now)

    await writer.resolve(runtime.contract, "cn-hangzhou", "fake-credential")
    result = await reader.resolve(runtime.contract, "cn-hangzhou", "fake-credential")

    assert result.endpoint == "fresh.aliyuncs.com"
    assert result.source == "location"


@pytest.mark.asyncio
async def test_location_failure_uses_fresh_entry_written_during_discovery(tmp_path: Path) -> None:
    runtime = LocationCacheRuntime(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    failed_location = LocationReply(RuntimeError("offline"), delayed=True)
    failed = runtime.create_resolver(failed_location, lambda: now)
    writer = runtime.create_resolver(LocationReply("fresh.aliyuncs.com"), lambda: now)
    failed_task = asyncio.create_task(failed.resolve(runtime.contract, "cn-hangzhou", "fake-credential"))
    try:
        await failed_location.wait_until_started()
        await writer.resolve(runtime.contract, "cn-hangzhou", "fake-credential")
    finally:
        failed_location.release.set()
        result = await failed_task

    assert result.endpoint == "fresh.aliyuncs.com"
    assert result.source == "location"


@pytest.mark.asyncio
async def test_old_memory_location_entry_yields_to_another_instances_newer_disk_entry(tmp_path: Path) -> None:
    runtime = LocationCacheRuntime(tmp_path)
    clock = [datetime(2026, 7, 1, tzinfo=timezone.utc)]
    writer = runtime.create_resolver(LocationReply("old.aliyuncs.com"), lambda: clock[0])
    await writer.resolve(runtime.contract, "cn-hangzhou", "fake-credential")
    reader = runtime.create_resolver(LocationReply(RuntimeError("offline")), lambda: clock[0])
    clock[0] += timedelta(days=8)
    writer._location = LocationReply("fresh.aliyuncs.com")
    await writer.resolve(runtime.contract, "cn-hangzhou", "fake-credential")

    result = await reader.resolve(runtime.contract, "cn-hangzhou", "fake-credential")

    assert result.endpoint == "fresh.aliyuncs.com"


@pytest.mark.asyncio
async def test_failed_location_cache_transaction_does_not_publish_new_memory_entry(tmp_path: Path) -> None:
    runtime = LocationCacheRuntime(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    writer = runtime.create_resolver(LocationReply("existing.aliyuncs.com"), lambda: now)
    await writer.resolve(runtime.contract, "cn-hangzhou", "fake-credential")

    def fail_write(path: Path, document: Any) -> None:
        raise OSError("fake disk full")

    failed = runtime.create_resolver(
        LocationReply("uncommitted.aliyuncs.com"),
        lambda: now,
        cache_writer=fail_write,
    )
    with pytest.raises(EndpointResolutionError, match="location_cache_write_failed"):
        await failed.resolve(runtime.contract, "cn-shanghai", "fake-credential")
    failed._location = LocationReply(RuntimeError("offline"))
    with pytest.raises(EndpointResolutionError, match="endpoint_unavailable"):
        await failed.resolve(runtime.contract, "cn-shanghai", "fake-credential")
    assert (
        await failed.resolve(runtime.contract, "cn-hangzhou", "fake-credential")
    ).endpoint == "existing.aliyuncs.com"
    assert [entry["key"][2] for entry in runtime.read_entries()] == ["cn-hangzhou"]
