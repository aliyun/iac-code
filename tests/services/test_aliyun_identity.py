from __future__ import annotations

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_identity import (
    AliyunCallerIdentityResolver,
    AliyunCallerIdentityUnavailableError,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "kind", "subject_id"),
    [
        ({"IdentityType": "Account", "AccountId": "1001"}, "account", "1001"),
        (
            {"IdentityType": "RAMUser", "AccountId": "1001", "UserId": "2002"},
            "ram_user",
            "2002",
        ),
        (
            {
                "IdentityType": "AssumedRoleUser",
                "AccountId": "1001",
                "RoleId": "3003",
                "PrincipalId": "3003:rotating-session-name",
            },
            "ram_role",
            "3003",
        ),
    ],
)
async def test_resolver_uses_stable_caller_fields(response, kind, subject_id) -> None:
    async def request(_credential, _region_id):
        return response

    identity = await AliyunCallerIdentityResolver(request=request).resolve(AliyunCredential(), "cn-hangzhou")

    assert identity.kind == kind
    assert identity.account_id == "1001"
    assert identity.subject_id == subject_id
    assert "rotating-session-name" not in identity.canonical_value().values()


@pytest.mark.asyncio
async def test_resolver_retries_transient_failure_twice() -> None:
    attempts = 0
    delays: list[float] = []

    class InternalError(Exception):
        status_code = 500
        code = "InternalError"

    async def retrying_request(_credential, _region_id):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise InternalError()
        return {"IdentityType": "RAMUser", "AccountId": "1001", "UserId": "2002"}

    async def sleep(delay):
        delays.append(delay)

    attempts = 0
    identity = await AliyunCallerIdentityResolver(request=retrying_request, sleep=sleep).resolve(
        AliyunCredential(),
        "cn-hangzhou",
    )

    assert identity.subject_id == "2002"
    assert attempts == 3
    assert delays == [0.1, 0.3]


@pytest.mark.asyncio
async def test_resolver_does_not_retry_expired_token() -> None:
    attempts = 0

    class ExpiredTokenError(Exception):
        status_code = 400
        code = "InvalidSecurityToken.Expired"

    async def request(_credential, _region_id):
        nonlocal attempts
        attempts += 1
        raise ExpiredTokenError()

    with pytest.raises(AliyunCallerIdentityUnavailableError) as raised:
        await AliyunCallerIdentityResolver(request=request).resolve(AliyunCredential(), "cn-hangzhou")

    assert raised.value.reason == "InvalidSecurityToken.Expired"
    assert raised.value.retryable is False
    assert attempts == 1


@pytest.mark.asyncio
async def test_resolver_rejects_incomplete_identity_without_retry() -> None:
    attempts = 0

    async def request(_credential, _region_id):
        nonlocal attempts
        attempts += 1
        return {"IdentityType": "AssumedRoleUser", "AccountId": "1001"}

    with pytest.raises(AliyunCallerIdentityUnavailableError, match="caller_identity_response_invalid"):
        await AliyunCallerIdentityResolver(request=request).resolve(AliyunCredential(), "cn-hangzhou")

    assert attempts == 1
