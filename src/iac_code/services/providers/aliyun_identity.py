"""Stable Alibaba Cloud caller identity resolution for permission checkpoints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from iac_code.services.providers.aliyun import AliyunCredential

logger = logging.getLogger(__name__)

CallerIdentityKind = Literal["account", "ram_user", "ram_role"]
IdentityRequest = Callable[[AliyunCredential, str], Awaitable[Mapping[str, Any]]]
Sleep = Callable[[float], Awaitable[None]]

_TRANSIENT_CODES = {
    "InternalError",
    "ServiceUnavailable",
    "SystemBusy",
    "Throttling",
    "Throttling.User",
    "Throttling.Api",
}


class AliyunCallerIdentityUnavailableError(RuntimeError):
    """The current cloud caller could not be verified safely."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True)
class AliyunCallerIdentity:
    """Stable, non-secret fields returned by STS GetCallerIdentity."""

    kind: CallerIdentityKind
    account_id: str
    subject_id: str

    def canonical_value(self) -> dict[str, str]:
        return {
            "identityType": self.kind,
            "accountId": self.account_id,
            "subjectId": self.subject_id,
        }


class AliyunCallerIdentityResolver:
    """Resolve a stable caller, retrying only transient STS failures."""

    def __init__(
        self,
        *,
        request: IdentityRequest | None = None,
        sleep: Sleep = asyncio.sleep,
        retry_delays: tuple[float, ...] = (0.1, 0.3),
    ) -> None:
        self._request = request or _call_get_caller_identity
        self._sleep = sleep
        self._retry_delays = retry_delays

    async def resolve(self, credential: AliyunCredential, region_id: str) -> AliyunCallerIdentity:
        attempts = len(self._retry_delays) + 1
        for attempt in range(attempts):
            try:
                response = await self._request(credential, region_id)
                return _parse_caller_identity(response)
            except AliyunCallerIdentityUnavailableError as exc:
                error = exc
            except Exception as exc:  # SDK errors intentionally stay behind the service boundary.
                error = _normalize_identity_error(exc)
            if not error.retryable or attempt >= len(self._retry_delays):
                raise error
            delay = self._retry_delays[attempt]
            logger.warning(
                "GetCallerIdentity transient failure attempt=%s max_attempts=%s retry_delay_seconds=%s reason=%s",
                attempt + 1,
                attempts,
                delay,
                error.reason,
            )
            await self._sleep(delay)
        raise AliyunCallerIdentityUnavailableError("caller_identity_unavailable", retryable=False)


def _parse_caller_identity(response: Mapping[str, Any]) -> AliyunCallerIdentity:
    identity_type = response.get("IdentityType")
    account_id = response.get("AccountId")
    if not isinstance(identity_type, str) or not isinstance(account_id, str) or not account_id:
        raise AliyunCallerIdentityUnavailableError("caller_identity_response_invalid", retryable=False)
    if identity_type == "Account":
        return AliyunCallerIdentity(kind="account", account_id=account_id, subject_id=account_id)
    if identity_type == "RAMUser":
        user_id = response.get("UserId")
        if isinstance(user_id, str) and user_id:
            return AliyunCallerIdentity(kind="ram_user", account_id=account_id, subject_id=user_id)
    elif identity_type == "AssumedRoleUser":
        role_id = response.get("RoleId")
        if isinstance(role_id, str) and role_id:
            return AliyunCallerIdentity(kind="ram_role", account_id=account_id, subject_id=role_id)
    raise AliyunCallerIdentityUnavailableError("caller_identity_response_invalid", retryable=False)


def _normalize_identity_error(exc: Exception) -> AliyunCallerIdentityUnavailableError:
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None) or getattr(exc, "error_code", None)
    if not isinstance(code, str):
        data = getattr(exc, "data", None)
        code = data.get("Code") if isinstance(data, Mapping) else None
    retryable = isinstance(exc, (TimeoutError, ConnectionError, OSError))
    retryable = retryable or status == 429 or (isinstance(status, int) and status >= 500)
    retryable = retryable or (isinstance(code, str) and (code in _TRANSIENT_CODES or code.startswith("Throttling")))
    reason = code if isinstance(code, str) and code else type(exc).__name__
    return AliyunCallerIdentityUnavailableError(reason, retryable=retryable)


async def _call_get_caller_identity(credential: AliyunCredential, region_id: str) -> Mapping[str, Any]:
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_openapi.client import Client as OpenApiClient
    from darabonba.runtime import RuntimeOptions

    from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime

    config_values: dict[str, Any] = {
        "endpoint": "sts.aliyuncs.com",
        "region_id": region_id,
    }
    dynamic_client = aliyun_credential_runtime().sdk_client(credential)
    if dynamic_client is not None:
        config_values["credential"] = dynamic_client
    else:
        config_values["access_key_id"] = credential.access_key_id
        config_values["access_key_secret"] = credential.access_key_secret
        if credential.mode in {"StsToken", "OAuth"}:
            config_values["security_token"] = credential.sts_token
    client = OpenApiClient(open_api_models.Config(**config_values))
    params = open_api_models.Params(
        action="GetCallerIdentity",
        version="2015-04-01",
        protocol="HTTPS",
        pathname="/",
        method="POST",
        auth_type="AK",
        style="RPC",
        body_type="json",
        req_body_type="json",
    )
    runtime = RuntimeOptions(autoretry=False, max_attempts=1)
    result = await client.call_api_async(params, open_api_models.OpenApiRequest(), runtime)
    body = result.get("body", result)
    return body if isinstance(body, Mapping) else {}


__all__ = [
    "AliyunCallerIdentity",
    "AliyunCallerIdentityResolver",
    "AliyunCallerIdentityUnavailableError",
    "CallerIdentityKind",
]
