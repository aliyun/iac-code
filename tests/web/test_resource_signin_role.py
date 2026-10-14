from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.web.console_readonly_policy import POLICY_DESCRIPTION, POLICY_NAME, policy_json
from iac_code.web.resource_signin_role import (
    ConsoleSigninRoleCredentialCache,
    ConsoleSigninRoleError,
    _rpc_query_strings,
    assume_console_signin_role,
    create_console_signin_role,
    invalidate_console_signin_role_cache,
)
from iac_code.web.settings import console_signin_role_settings


def _existing_supplement(action):
    if action == "GetPolicy":
        return {
            "Policy": {"PolicyName": POLICY_NAME, "PolicyType": "Custom", "Description": POLICY_DESCRIPTION},
            "DefaultPolicyVersion": {"PolicyDocument": policy_json()},
        }
    if action == "ListPoliciesForRole":
        return {"Policies": {"Policy": []}}
    raise AssertionError(action)


def _expiration(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _assume_payload(epoch: int) -> dict:
    return {
        "Credentials": {
            "AccessKeyId": "sts-fake-id",
            "AccessKeySecret": "sts-fake-secret",
            "SecurityToken": "sts-fake-token",
            "Expiration": _expiration(epoch),
        }
    }


def test_rpc_query_scalars_are_serialized_before_the_generic_openapi_encoder() -> None:
    assert _rpc_query_strings(
        {
            "RoleName": "PreviewRole",
            "DurationSeconds": 3600,
            "AllowConsoleLogin": True,
            "Disabled": False,
        }
    ) == {
        "RoleName": "PreviewRole",
        "DurationSeconds": "3600",
        "AllowConsoleLogin": "true",
        "Disabled": "false",
    }
    with pytest.raises(TypeError, match="strings, booleans, or integers"):
        _rpc_query_strings({"Unsupported": ["value"]})


@pytest.mark.asyncio
async def test_assume_role_returns_expiring_sts_without_exposing_source_fields() -> None:
    calls = []

    async def rpc(credential, endpoint, action, version, query):
        calls.append((credential, endpoint, action, version, query))
        return _assume_payload(4600)

    source = AliyunCredential(mode="AK", access_key_id="source-id", access_key_secret="source-secret")
    result = await assume_console_signin_role(
        source,
        "acs:ram::1001:role/PreviewRole",
        rpc_call=rpc,
        clock=lambda: 1000,
    )

    assert result.mode == "StsToken"
    assert result.sts_expiration == 4600
    assert (result.access_key_id, result.access_key_secret, result.sts_token) == (
        "sts-fake-id",
        "sts-fake-secret",
        "sts-fake-token",
    )
    assert calls[0][1:4] == ("sts.aliyuncs.com", "AssumeRole", "2015-04-01")
    assert calls[0][4] == {
        "RoleArn": "acs:ram::1001:role/PreviewRole",
        "RoleSessionName": "iac-code-console",
        "DurationSeconds": 3600,
    }


@pytest.mark.asyncio
async def test_role_cache_reuses_sts_and_refreshes_before_expiration() -> None:
    now = [1000]
    calls = 0

    async def assume(_source, _role_arn):
        nonlocal calls
        calls += 1
        return AliyunCredential(
            mode="StsToken",
            access_key_id=f"sts-{calls}",
            access_key_secret="secret",
            sts_token="token",
            sts_expiration=now[0] + 3600,
        )

    cache = ConsoleSigninRoleCredentialCache(assume=assume, clock=lambda: now[0])
    source = AliyunCredential(mode="AK", access_key_id="source", access_key_secret="secret")
    first = await cache.get(source, "acs:ram::1001:role/PreviewRole")
    second = await cache.get(source, "acs:ram::1001:role/PreviewRole")
    assert first is second
    assert calls == 1

    now[0] = first.sts_expiration - 119
    refreshed = await cache.get(source, "acs:ram::1001:role/PreviewRole")
    assert refreshed.access_key_id == "sts-2"
    assert calls == 2


@pytest.mark.asyncio
async def test_role_cache_merges_concurrent_refreshes() -> None:
    calls = 0

    async def assume(_source, _role_arn):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return AliyunCredential(
            mode="StsToken",
            access_key_id="sts",
            access_key_secret="secret",
            sts_token="token",
            sts_expiration=4600,
        )

    cache = ConsoleSigninRoleCredentialCache(assume=assume, clock=lambda: 1000)
    source = AliyunCredential(mode="AK", access_key_id="source", access_key_secret="secret")
    results = await asyncio.gather(*(cache.get(source, "acs:ram::1001:role/PreviewRole") for _index in range(8)))
    assert calls == 1
    assert all(result is results[0] for result in results)


@pytest.mark.asyncio
async def test_create_role_trusts_exact_ram_user_attaches_read_only_and_persists_only_arn(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    calls = []

    async def rpc(_credential, endpoint, action, version, query):
        calls.append((endpoint, action, version, query))
        if action == "GetCallerIdentity":
            return {
                "IdentityType": "RAMUser",
                "AccountId": "1001",
                "Arn": "acs:ram::1001:user/alice",
            }
        if action == "CreateRole":
            return {"Role": {"Arn": "acs:ram::1001:role/IacCodeConsoleSigninRole"}}
        if action == "AttachPolicyToRole":
            return {"RequestId": "fake"}
        if action == "AssumeRole":
            return _assume_payload(4_000_000_000)
        return _existing_supplement(action)

    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    result = await create_console_signin_role(source, rpc_call=rpc)
    invalidate_console_signin_role_cache()

    assert result == {
        "status": "ready",
        "roleArn": "acs:ram::1001:role/iaccodeconsolesigninrole",
        "configured": True,
        "policyName": "ReadOnlyAccess",
        "supplementPolicyName": POLICY_NAME,
    }
    create_query = next(query for _endpoint, action, _version, query in calls if action == "CreateRole")
    assert create_query["AllowConsoleLogin"] is True
    assert create_query["MaxSessionDuration"] == 3600
    assert '"RAM":["acs:ram::1001:user/alice"]' in create_query["AssumeRolePolicyDocument"]
    attach_query = next(query for _endpoint, action, _version, query in calls if action == "AttachPolicyToRole")
    assert attach_query == {
        "PolicyType": "System",
        "PolicyName": "ReadOnlyAccess",
        "RoleName": "IacCodeConsoleSigninRole",
    }
    assert console_signin_role_settings()["roleArn"] == "acs:ram::1001:role/iaccodeconsolesigninrole"
    settings_text = (tmp_path / "config" / "settings.yml").read_text(encoding="utf-8")
    assert "sts-fake" not in settings_text
    assert "source-secret" not in settings_text


@pytest.mark.asyncio
async def test_one_click_setup_rejects_root_before_cloud_mutation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    actions = []

    async def rpc(_credential, _endpoint, action, _version, _query):
        actions.append(action)
        return {"IdentityType": "Account", "AccountId": "1001", "Arn": "acs:ram::1001:root"}

    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    with pytest.raises(ConsoleSigninRoleError, match="resource_signin_role_root_unsupported"):
        await create_console_signin_role(source, rpc_call=rpc)
    assert actions == ["GetCallerIdentity"]
    assert console_signin_role_settings()["configured"] is False


@pytest.mark.asyncio
async def test_created_role_is_saved_when_caller_still_lacks_assume_permission(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    class DeniedError(Exception):
        code = "Forbidden.RAM"

    async def rpc(_credential, _endpoint, action, _version, _query):
        if action == "GetCallerIdentity":
            return {
                "IdentityType": "RAMUser",
                "AccountId": "1001",
                "Arn": "acs:ram::1001:user/alice",
            }
        if action == "CreateRole":
            return {"Role": {"Arn": "acs:ram::1001:role/IacCodeConsoleSigninRole"}}
        if action == "AttachPolicyToRole":
            return {}
        if action == "AssumeRole":
            raise DeniedError()
        return _existing_supplement(action)

    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    result = await create_console_signin_role(source, rpc_call=rpc)
    assert result["status"] == "needsPermission"
    assert console_signin_role_settings()["configured"] is True


@pytest.mark.asyncio
async def test_create_role_preserves_safe_nested_provider_error_details(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    class ProviderError(Exception):
        code = "Forbidden.RAM"
        data = {
            "Message": "Access denied for ram:CreateRole; AccessKeySecret=must-not-leak",
            "RequestId": "request-123",
            "AccessDeniedDetail": {
                "AuthAction": "ram:CreateRole",
                "PolicyType": "ExplicitDeny",
                "SensitiveInternalField": "hidden",
            },
        }

    class WrappedError(Exception):
        def __init__(self) -> None:
            self.inner_exception = ProviderError()

    async def rpc(_credential, _endpoint, action, _version, _query):
        if action == "GetCallerIdentity":
            return {
                "IdentityType": "RAMUser",
                "AccountId": "1001",
                "Arn": "acs:ram::1001:user/alice",
            }
        if action == "CreateRole":
            raise WrappedError()
        raise AssertionError(action)

    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    with pytest.raises(ConsoleSigninRoleError) as raised:
        await create_console_signin_role(source, rpc_call=rpc)

    assert raised.value.code == "resource_signin_role_create_denied"
    assert raised.value.diagnostic == {
        "source": "alibabaCloud",
        "type": "ProviderError",
        "code": "Forbidden.RAM",
        "message": "Access denied for ram:CreateRole; AccessKeySecret=[REDACTED]",
        "requestId": "request-123",
        "accessDeniedDetail": {
            "AuthAction": "ram:CreateRole",
            "PolicyType": "ExplicitDeny",
        },
    }


@pytest.mark.asyncio
async def test_non_permission_create_failure_is_not_misreported_as_permission_denial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    async def rpc(_credential, _endpoint, action, _version, _query):
        if action == "GetCallerIdentity":
            return {
                "IdentityType": "RAMUser",
                "AccountId": "1001",
                "Arn": "acs:ram::1001:user/alice",
            }
        if action == "CreateRole":
            raise TypeError("quote() doesn't support 'encoding' for bytes")
        raise AssertionError(action)

    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    with pytest.raises(ConsoleSigninRoleError) as raised:
        await create_console_signin_role(source, rpc_call=rpc)

    assert raised.value.code == "resource_signin_role_create_failed"
    assert raised.value.diagnostic == {
        "source": "client",
        "type": "TypeError",
        "message": "quote() doesn't support 'encoding' for bytes",
    }


@pytest.mark.asyncio
async def test_one_click_setup_recovers_exact_role_left_by_interrupted_create(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    calls = []
    trust_policy = {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"RAM": ["acs:ram::1001:user/alice"]},
            }
        ],
        "Version": "1",
    }

    class ConflictError(Exception):
        code = "EntityAlreadyExists.Role"
        data = {
            "Message": "The role already exists.",
            "RequestId": "request-conflict",
        }

    async def rpc(_credential, _endpoint, action, _version, query):
        calls.append(action)
        if action == "GetCallerIdentity":
            return {
                "IdentityType": "RAMUser",
                "AccountId": "1001",
                "Arn": "acs:ram::1001:user/alice",
            }
        if action == "CreateRole":
            raise ConflictError()
        if action == "GetRole":
            return {
                "Role": {
                    "RoleName": "IacCodeConsoleSigninRole",
                    "Arn": "acs:ram::1001:role/iaccodeconsolesigninrole",
                    "Description": "Read-only role used by iac-code embedded console previews.",
                    "MaxSessionDuration": "3600",
                    "AllowConsoleLogin": "true",
                    "AssumeRolePolicyDocument": quote(json.dumps(trust_policy)),
                }
            }
        if action == "AttachPolicyToRole":
            return {}
        if action == "AssumeRole":
            return _assume_payload(4_000_000_000)
        return _existing_supplement(action)

    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    result = await create_console_signin_role(source, rpc_call=rpc)
    invalidate_console_signin_role_cache()

    assert calls == [
        "GetCallerIdentity",
        "CreateRole",
        "GetRole",
        "ListPoliciesForRole",
        "AttachPolicyToRole",
        "GetPolicy",
        "ListPoliciesForRole",
        "AttachPolicyToRole",
        "AssumeRole",
    ]
    assert result["status"] == "ready"
    assert result["roleArn"] == "acs:ram::1001:role/iaccodeconsolesigninrole"
    assert console_signin_role_settings()["roleArn"] == "acs:ram::1001:role/iaccodeconsolesigninrole"


@pytest.mark.asyncio
async def test_one_click_setup_never_adopts_an_unrelated_existing_role(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    calls = []

    class ConflictError(Exception):
        code = "EntityAlreadyExists.Role"

    async def rpc(_credential, _endpoint, action, _version, _query):
        calls.append(action)
        if action == "GetCallerIdentity":
            return {
                "IdentityType": "RAMUser",
                "AccountId": "1001",
                "Arn": "acs:ram::1001:user/alice",
            }
        if action == "CreateRole":
            raise ConflictError()
        if action == "GetRole":
            return {
                "Role": {
                    "RoleName": "IacCodeConsoleSigninRole",
                    "Arn": "acs:ram::1001:role/iaccodeconsolesigninrole",
                    "Description": "Created by someone else",
                    "MaxSessionDuration": 3600,
                    "AllowConsoleLogin": True,
                    "AssumeRolePolicyDocument": "{}",
                }
            }
        raise AssertionError(action)

    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    with pytest.raises(ConsoleSigninRoleError) as raised:
        await create_console_signin_role(source, rpc_call=rpc)

    assert raised.value.code == "resource_signin_role_name_conflict"
    assert calls == ["GetCallerIdentity", "CreateRole", "GetRole"]
    assert console_signin_role_settings()["configured"] is False
