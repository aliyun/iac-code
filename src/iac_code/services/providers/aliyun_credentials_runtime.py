"""Process-wide runtime for dynamic Alibaba Cloud credential sources.

`AliyunCredential` records which credential *source* the user picked; it does not
promise that a signable AccessKey is stored on the object. For `EcsRamRole` the
signable STS triple only exists in memory and comes from the ECS instance
metadata service (IMDS). This module owns that translation so every Alibaba Cloud
call channel (Tea, ACS1, ACS3 streaming, OSS V4, ROS, endpoint discovery and the
legacy `AliyunApi`) shares one provider cache, one validation path and one set of
stable error codes.

Two access shapes are offered:

* `sdk_client()` returns a Credentials-SDK `Client` for SDKs that accept a dynamic
  credential object and fetch fresh credentials right before signing.
* `resolve()` returns a concrete `AliyunCredential(mode="StsToken", ...)` for
  transports that only accept a fixed AK/STS triple.

Both go through the same `EcsRamRoleProviderAdapter.get_credentials()`, so the
field and real-expiration checks cannot be bypassed by picking a channel.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from alibabacloud_credentials_api import ICredentialsProvider

from iac_code.services.providers.aliyun import AliyunCredential

# Stable, non-sensitive error codes for every ECS RAM Role failure. Nothing else may
# escape the runtime: no SDK exception text, metadata URL, role listing or response body.
ECS_METADATA_UNREACHABLE = "ecs_metadata_unreachable"
ECS_METADATA_DISABLED = "ecs_metadata_disabled"
ECS_IMDSV2_REQUIRED = "ecs_imdsv2_required"
ECS_RAM_ROLE_NOT_FOUND = "ecs_ram_role_not_found"
ECS_RAM_ROLE_RESPONSE_INVALID = "ecs_ram_role_response_invalid"
ECS_RAM_ROLE_REFRESH_FAILED = "ecs_ram_role_refresh_failed"

ECS_CREDENTIAL_ERROR_CODES: frozenset[str] = frozenset(
    {
        ECS_METADATA_UNREACHABLE,
        ECS_METADATA_DISABLED,
        ECS_IMDSV2_REQUIRED,
        ECS_RAM_ROLE_NOT_FOUND,
        ECS_RAM_ROLE_RESPONSE_INVALID,
        ECS_RAM_ROLE_REFRESH_FAILED,
    }
)

ECS_RAM_ROLE_PROVIDER_NAME = "ecs_ram_role"

# Reject anything that could escape the fixed metadata namespace; the role name must
# stay a single path segment under /latest/meta-data/ram/security-credentials/.
_FORBIDDEN_ROLE_NAME_CHARACTERS = frozenset({"/", "?", "#"})

# The SDK reports a failed IMDSv2 token round trip as "Failed to get token from ECS
# Metadata Service. HttpCode=..."; matched case-insensitively as a whole phrase.
_IMDSV2_TOKEN_FAILURE_PHRASE = "token from ecs metadata service"

# Refuse credentials that expire within this window so a request signed now cannot
# land after expiry because of small clock skew between this host and Alibaba Cloud.
_EXPIRATION_SKEW_SECONDS = 60

# Provider cache stays small: the key space is (mode, role name, IMDSv1 policy).
_PROVIDER_CACHE_MAX_ENTRIES = 8

_ENV_ECS_METADATA = "ALIBABA_CLOUD_ECS_METADATA"
_ENV_ECS_METADATA_DISABLED = "ALIBABA_CLOUD_ECS_METADATA_DISABLED"
_ENV_IMDSV1_DISABLED = "ALIBABA_CLOUD_IMDSV1_DISABLED"

_DEFAULT_RAM_ROLE_SESSION_NAME = "iac-code-session"


def is_ecs_credential_error(error: BaseException | str) -> bool:
    """Whether `error` is exactly one of the six stable ECS credential codes."""
    return str(error) in ECS_CREDENTIAL_ERROR_CODES


def normalize_role_name(value: Any) -> str | None:
    """Normalize a configured or environment role name to `str | None`.

    Whitespace-only input becomes `None` ("no explicit role name"). Characters that
    could break out of the metadata namespace are rejected before the SDK is touched.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(ECS_RAM_ROLE_NOT_FOUND)
    role_name = value.strip()
    if not role_name:
        return None
    for character in role_name:
        if character in _FORBIDDEN_ROLE_NAME_CHARACTERS or not character.isprintable():
            raise ValueError(ECS_RAM_ROLE_NOT_FOUND)
    return role_name


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() == "true"


def ecs_metadata_disabled() -> bool:
    """Read the IMDS kill switch from the live environment, not an import snapshot."""
    return _env_flag(_ENV_ECS_METADATA_DISABLED)


def imds_v1_disabled() -> bool:
    """Read the IMDSv1 fallback policy from the live environment."""
    return _env_flag(_ENV_IMDSV1_DISABLED)


def effective_role_name(credential: AliyunCredential) -> str | None:
    """Merge the configured role name with `ALIBABA_CLOUD_ECS_METADATA`.

    Priority is configuration > environment > IMDS auto-discovery (represented by
    `None` inside the runtime).
    """
    configured = normalize_role_name(getattr(credential, "ram_role_name", ""))
    if configured is not None:
        return configured
    return normalize_role_name(os.environ.get(_ENV_ECS_METADATA))


def _refreshable_credentials_class() -> type:
    """Return the concrete refreshable `Credentials` class the SDK providers hand back.

    Imported lazily like the rest of the credentials SDK surface so importing this module
    stays cheap for call paths that never touch a dynamic credential.
    """
    from alibabacloud_credentials.provider.refreshable import Credentials

    return Credentials


def _tea_retry_error_class() -> type[BaseException]:
    """Return the Tea error type that carries a failed IMDS round trip.

    Imported lazily like the rest of the SDK surface so importing this module stays cheap.
    """
    from Tea.exceptions import RetryError

    return RetryError


def _expiration_epoch_seconds(credentials: Any) -> int:
    """Read the real expiration from the concrete refreshable `Credentials` object.

    Neither the generic `ICredentials` interface nor `CredentialModel` declares
    `get_expiration()`, so a missing/unusable value means the credential cannot be
    trusted rather than "skip the expiry check".
    """
    getter = getattr(credentials, "get_expiration", None)
    if not callable(getter):
        raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID)
    try:
        expiration = getter()
    except Exception as error:  # noqa: BLE001 - any provider failure means "unusable"
        raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID) from error
    if isinstance(expiration, bool) or not isinstance(expiration, int):
        raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID)
    return expiration


def validate_ecs_credentials(credentials: Any, *, now: int | None = None) -> Any:
    """Validate an IMDS credential triple, its provider and its real expiration.

    Raises `ValueError` with a stable code; never echoes credential values.
    """
    if credentials is None:
        raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID)
    if not isinstance(credentials, _refreshable_credentials_class()):
        # Only the concrete refreshable `Credentials` object carries a real expiration, so
        # a duck-typed stand-in must not be signed with instead of being rejected.
        raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID)
    if credentials.get_provider_name() != ECS_RAM_ROLE_PROVIDER_NAME:
        # This validation only covers instance-metadata credentials; anything produced by
        # another provider reached it through the wrong channel.
        raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID)
    for getter_name in ("get_access_key_id", "get_access_key_secret", "get_security_token"):
        getter = getattr(credentials, getter_name, None)
        if not callable(getter):
            raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID)
        try:
            value = getter()
        except Exception as error:  # noqa: BLE001 - any provider failure means "unusable"
            raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID) from error
        if not isinstance(value, str) or not value:
            raise ValueError(ECS_RAM_ROLE_RESPONSE_INVALID)
    expiration = _expiration_epoch_seconds(credentials)
    current = int(time.time()) if now is None else now
    if expiration <= current + _EXPIRATION_SKEW_SECONDS:
        # The SDK's StaleValueBehavior.ALLOW hands back the previous value when a
        # refresh fails; an already-expired cache must fail here instead of being
        # signed and rejected later by the cloud API.
        raise ValueError(ECS_RAM_ROLE_REFRESH_FAILED)
    return credentials


def _classify_provider_failure(error: BaseException, *, disable_imds_v1: bool) -> str:
    """Map an SDK/network failure to a stable code without reusing its message."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__

    text = " ".join(str(item) for item in chain)
    if disable_imds_v1 and _IMDSV2_TOKEN_FAILURE_PHRASE in text.casefold():
        # With IMDSv1 disabled the SDK re-raises the token failure instead of falling
        # back, so this is specifically "IMDSv2 is required but unavailable" — including
        # when the token request itself failed with HttpCode=404, which is why this comes
        # before the role-lookup check below. The phrase is matched in full so an
        # unrelated message mentioning a SecurityToken cannot land here.
        return ECS_IMDSV2_REQUIRED
    if any(isinstance(item, _tea_retry_error_class()) for item in chain):
        # `TeaCore.do_action` turns every IMDS connect/read failure into
        # `Tea.exceptions.RetryError`, a plain `Exception` carrying only the socket message,
        # so neither the OSError check nor the ValueError fallback below recognizes it.
        # Reaching here means no metadata response arrived at all. It stays below the token
        # check because the same type also covers the role-name and credential requests, so
        # a disabled IMDSv1 on its own must not be read as a token-stage failure.
        return ECS_METADATA_UNREACHABLE
    if "HttpCode=404" in text:
        return ECS_RAM_ROLE_NOT_FOUND
    if any(isinstance(item, (json.JSONDecodeError, KeyError, TypeError, AttributeError)) for item in chain):
        # The provider parses the metadata response without validating it: a missing or
        # non-string `Expiration` reaches `time.strptime` as a TypeError, and a JSON body
        # whose top level is not an object fails on `dic.get` with an AttributeError.
        return ECS_RAM_ROLE_RESPONSE_INVALID
    if any(isinstance(item, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)) for item in chain):
        return ECS_METADATA_UNREACHABLE
    if any(isinstance(item, OSError) for item in chain):
        return ECS_METADATA_UNREACHABLE
    if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
        # `time.strptime` on a malformed Expiration and json field extraction both
        # surface as ValueError from the provider's response parsing.
        return ECS_RAM_ROLE_RESPONSE_INVALID
    return ECS_RAM_ROLE_REFRESH_FAILED


def _create_ecs_provider(*, role_name: str, disable_imds_v1: bool) -> Any:
    from alibabacloud_credentials.provider.ecs_ram_role import EcsRamRoleCredentialsProvider

    try:
        return EcsRamRoleCredentialsProvider(
            # Always an explicit str; "" selects the synchronous auto-discovery path
            # instead of letting the SDK fall back to its import-time env snapshot.
            role_name=role_name,
            disable_imds_v1=disable_imds_v1,
            # No background APScheduler and no SIGINT/SIGTERM handlers: iac-code owns
            # the process lifecycle for CLI, Web, ACP, A2A and the Desktop sidecar.
            async_update_enabled=False,
        )
    except ValueError as error:
        # The SDK constructor also checks its import-time snapshot of
        # ALIBABA_CLOUD_ECS_METADATA_DISABLED; normalize that to a stable code.
        raise ValueError(ECS_METADATA_DISABLED) from error


class EcsRamRoleProviderAdapter(ICredentialsProvider):
    """Single validated entry point to an ECS RAM Role provider.

    The raw provider's async interface is never used: it would run the SDK's
    `threading.Lock.acquire(timeout=5)` plus IMDS I/O on the event loop, and its
    async refresh path does not auto-discover the role name for an empty string.
    """

    def __init__(self, provider: Any, *, disable_imds_v1: bool) -> None:
        self._provider = provider
        self._disable_imds_v1 = disable_imds_v1

    def get_credentials(self) -> Any:
        try:
            credentials = self._provider.get_credentials()
        except ValueError as error:
            if is_ecs_credential_error(error):
                raise
            raise ValueError(_classify_provider_failure(error, disable_imds_v1=self._disable_imds_v1)) from error
        except Exception as error:  # noqa: BLE001 - SDK raises CredentialException and Tea errors
            raise ValueError(_classify_provider_failure(error, disable_imds_v1=self._disable_imds_v1)) from error
        return validate_ecs_credentials(credentials)

    async def get_credentials_async(self) -> Any:
        return await asyncio.to_thread(self.get_credentials)

    def get_provider_name(self) -> str:
        return ECS_RAM_ROLE_PROVIDER_NAME


def _ram_role_arn_config(credential: AliyunCredential) -> Any:
    from alibabacloud_credentials import models as credential_models

    # Endpoint discovery passes duck-typed credential objects, so read defensively the
    # same way those call sites did before they shared this helper.
    return credential_models.Config(
        type="ram_role_arn",
        access_key_id=getattr(credential, "access_key_id", ""),
        access_key_secret=getattr(credential, "access_key_secret", ""),
        role_arn=getattr(credential, "ram_role_arn", ""),
        role_session_name=getattr(credential, "ram_session_name", "") or _DEFAULT_RAM_ROLE_SESSION_NAME,
    )


class AliyunCredentialRuntime:
    """Shared provider cache and credential resolver for dynamic credential modes."""

    def __init__(self, *, provider_factory: Callable[..., Any] = _create_ecs_provider) -> None:
        self._provider_factory = provider_factory
        # Guarded by a plain threading lock: the cache is shared across event loops
        # (CLI, Web, ACP, A2A) and worker threads, so no asyncio primitive may own it.
        self._lock = threading.Lock()
        self._adapters: OrderedDict[tuple[str, str | None, bool], EcsRamRoleProviderAdapter] = OrderedDict()

    def invalidate(self) -> None:
        """Drop cached providers after the stored credential configuration changed."""
        with self._lock:
            self._adapters.clear()

    def ecs_adapter(self, credential: AliyunCredential) -> EcsRamRoleProviderAdapter:
        """Return the shared adapter for `credential`'s effective ECS configuration."""
        if ecs_metadata_disabled():
            raise ValueError(ECS_METADATA_DISABLED)
        role_name = effective_role_name(credential)
        disable_imds_v1 = imds_v1_disabled()
        key = (str(getattr(credential, "mode", "")), role_name, disable_imds_v1)
        with self._lock:
            adapter = self._adapters.get(key)
            if adapter is not None:
                self._adapters.move_to_end(key)
                return adapter
            provider = self._provider_factory(role_name=role_name or "", disable_imds_v1=disable_imds_v1)
            adapter = EcsRamRoleProviderAdapter(provider, disable_imds_v1=disable_imds_v1)
            self._adapters[key] = adapter
            while len(self._adapters) > _PROVIDER_CACHE_MAX_ENTRIES:
                self._adapters.popitem(last=False)
            return adapter

    def sdk_client(self, credential: AliyunCredential | None) -> Any | None:
        """Return a shared Credentials-SDK client for dynamic modes, else `None`."""
        if credential is None:
            return None
        from alibabacloud_credentials.client import Client as CredentialClient

        mode = getattr(credential, "mode", "AK")
        if mode == "EcsRamRole":
            return CredentialClient(provider=self.ecs_adapter(credential))
        if mode == "RamRoleArn":
            return CredentialClient(_ram_role_arn_config(credential))
        return None

    async def resolve(
        self,
        credential: AliyunCredential,
        *,
        client_factory: Callable[[Any], Any] | None = None,
    ) -> AliyunCredential:
        """Return a credential that static AK/STS transports can sign with."""
        if credential.mode == "EcsRamRole":
            adapter = self.ecs_adapter(credential)
            credentials = await asyncio.to_thread(adapter.get_credentials)
            return AliyunCredential(
                mode="StsToken",
                access_key_id=credentials.get_access_key_id(),
                access_key_secret=credentials.get_access_key_secret(),
                sts_token=credentials.get_security_token(),
                region_id=credential.region_id,
            )
        if credential.mode == "RamRoleArn":
            factory = client_factory if client_factory is not None else self._default_ram_role_client_factory()
            model = await factory(_ram_role_arn_config(credential)).get_credential_async()
            access_key_id = getattr(model, "access_key_id", None)
            access_key_secret = getattr(model, "access_key_secret", None)
            security_token = getattr(model, "security_token", None)
            if (
                not isinstance(access_key_id, str)
                or not access_key_id
                or not isinstance(access_key_secret, str)
                or not access_key_secret
                or not isinstance(security_token, str)
                or not security_token
            ):
                raise ValueError("ram_role_credential_invalid")
            return AliyunCredential(
                mode="StsToken",
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                sts_token=security_token,
                region_id=credential.region_id,
            )
        return credential

    @staticmethod
    def _default_ram_role_client_factory() -> Callable[[Any], Any]:
        from alibabacloud_credentials.client import Client as CredentialClient

        return CredentialClient


_runtime_lock = threading.Lock()
_runtime: AliyunCredentialRuntime | None = None


def aliyun_credential_runtime() -> AliyunCredentialRuntime:
    """Return the process-wide credential runtime."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = AliyunCredentialRuntime()
        return _runtime


def invalidate_aliyun_credential_runtime() -> None:
    """Invalidate the process-wide provider cache after a configuration change."""
    with _runtime_lock:
        runtime = _runtime
    if runtime is not None:
        runtime.invalidate()
