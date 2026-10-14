from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.cloud.aliyun.endpoint_resolver import (
    AccountIdentityResolver,
    EndpointResolutionError,
    EndpointResolver,
)
from tests.tools.cloud.aliyun.test_endpoint_resolver import api_contract


class IdentityRequest:
    def __init__(self) -> None:
        self.accounts = {"fake-key-a": "1111111111111111", "fake-key-b": "2222222222222222"}
        self.calls: list[Any] = []

    async def __call__(self, host: str, request: Any) -> dict[str, str]:
        assert host == "sts.aliyuncs.com"
        credential = request["credential"]
        self.calls.append(credential)
        return {"AccountId": self.accounts[credential.access_key_id]}


@pytest.mark.asyncio
async def test_account_scoped_endpoint_changes_when_runtime_credentials_change(tmp_path: Path) -> None:
    identity = AccountIdentityResolver(IdentityRequest())
    resolver = EndpointResolver(cache_dir=tmp_path, account_identity=identity)
    contract = api_contract(product="SMQProxy", version="2026-04-09")
    first = AliyunCredential(access_key_id="fake-key-a", access_key_secret="fake-secret-a")
    second = AliyunCredential(access_key_id="fake-key-b", access_key_secret="fake-secret-b")

    first_endpoint = await resolver.resolve(contract, "cn-hangzhou", first)
    second_endpoint = await resolver.resolve(contract, "cn-hangzhou", second)

    assert first_endpoint.endpoint == "1111111111111111.mns.cn-hangzhou.aliyuncs.com"
    assert second_endpoint.endpoint == "2222222222222222.mns.cn-hangzhou.aliyuncs.com"


@pytest.mark.asyncio
async def test_static_account_identity_reuses_equivalent_credential_snapshot() -> None:
    request = IdentityRequest()
    resolver = AccountIdentityResolver(request)
    first = AliyunCredential(access_key_id="fake-key-a", access_key_secret="fake-secret-a")
    equivalent = AliyunCredential(
        access_key_id="fake-key-a", access_key_secret="fake-secret-a", region_id="cn-shanghai"
    )

    assert await resolver.resolve(first, "cn-hangzhou") == "1111111111111111"
    request.accounts.clear()
    assert await resolver.resolve(equivalent, "cn-shanghai") == "1111111111111111"


@pytest.mark.asyncio
async def test_account_identity_revalidates_after_credential_refresh_in_place() -> None:
    resolver = AccountIdentityResolver(IdentityRequest())
    credential = AliyunCredential(access_key_id="fake-key-a", access_key_secret="fake-secret-a")
    assert await resolver.resolve(credential, "cn-hangzhou") == "1111111111111111"

    credential.refresh_from(AliyunCredential(access_key_id="fake-key-b", access_key_secret="fake-secret-b"))

    assert await resolver.resolve(credential, "cn-hangzhou") == "2222222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["access_key_secret", "sts_token"])
async def test_account_identity_revalidates_rotated_signing_fields(field: str) -> None:
    request = IdentityRequest()
    resolver = AccountIdentityResolver(request)
    credential = AliyunCredential(
        mode="StsToken", access_key_id="fake-key-a", access_key_secret="fake-secret-a", sts_token="fake-token-a"
    )
    assert await resolver.resolve(credential, "cn-hangzhou") == "1111111111111111"
    setattr(credential, field, "fake-rotated-value")
    request.accounts["fake-key-a"] = "2222222222222222"

    assert await resolver.resolve(credential, "cn-hangzhou") == "2222222222222222"


class DelayedIdentityRequest(IdentityRequest):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, host: str, request: Any) -> dict[str, str]:
        if request["credential"].access_key_id == "fake-key-a" and not self.release.is_set():
            self.started.set()
            await self.release.wait()
        return await super().__call__(host, request)


@pytest.mark.asyncio
async def test_concurrent_account_identity_requests_never_reuse_another_caller() -> None:
    request = DelayedIdentityRequest()
    resolver = AccountIdentityResolver(request)
    first = AliyunCredential(access_key_id="fake-key-a", access_key_secret="fake-secret-a")
    second = AliyunCredential(access_key_id="fake-key-b", access_key_secret="fake-secret-b")
    first_task = asyncio.create_task(resolver.resolve(first, "cn-hangzhou"))
    try:
        await request.started.wait()
        assert await resolver.resolve(second, "cn-hangzhou") == "2222222222222222"
    finally:
        request.release.set()
        await first_task

    assert first_task.result() == "1111111111111111"
    assert await resolver.resolve(second, "cn-hangzhou") == "2222222222222222"
    assert await resolver.resolve(first, "cn-hangzhou") == "1111111111111111"


@pytest.mark.asyncio
async def test_account_identity_discovery_uses_snapshot_during_in_place_refresh() -> None:
    request = DelayedIdentityRequest()
    resolver = AccountIdentityResolver(request)
    credential = AliyunCredential(access_key_id="fake-key-a", access_key_secret="fake-secret-a")
    first_task = asyncio.create_task(resolver.resolve(credential, "cn-hangzhou"))
    try:
        await request.started.wait()
        credential.refresh_from(AliyunCredential(access_key_id="fake-key-b", access_key_secret="fake-secret-b"))
    finally:
        request.release.set()
        await first_task

    assert first_task.result() == "1111111111111111"
    assert await resolver.resolve(credential, "cn-hangzhou") == "2222222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["RamRoleArn", "EcsRamRole"])
async def test_dynamic_account_identity_revalidates_underlying_caller(mode: str) -> None:
    request = IdentityRequest()
    resolver = AccountIdentityResolver(request)
    credential = AliyunCredential(mode=mode, access_key_id="fake-key-a", access_key_secret="fake-secret-a")
    assert await resolver.resolve(credential, "cn-hangzhou") == "1111111111111111"
    request.accounts["fake-key-a"] = "2222222222222222"

    assert await resolver.resolve(credential, "cn-hangzhou") == "2222222222222222"


@pytest.mark.asyncio
async def test_invalid_new_caller_identity_does_not_use_previous_account() -> None:
    request = IdentityRequest()
    resolver = AccountIdentityResolver(request)
    first = AliyunCredential(access_key_id="fake-key-a", access_key_secret="fake-secret-a")
    second = AliyunCredential(access_key_id="fake-key-b", access_key_secret="fake-secret-b")
    assert await resolver.resolve(first, "cn-hangzhou") == "1111111111111111"
    request.accounts["fake-key-b"] = "not-an-account"

    with pytest.raises(EndpointResolutionError, match="account_id_discovery_failed"):
        await resolver.resolve(second, "cn-hangzhou")

    assert await resolver.resolve(first, "cn-hangzhou") == "1111111111111111"
