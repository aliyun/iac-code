"""Dedicated RAM role support for embedded Alibaba Cloud console sign-in.

Only non-secret role settings and the supplemental policy revision are persisted. Assumed-role credentials are cached in
process memory, keyed by the source credential and refreshed before expiration.
Cloud-side role creation is reachable only from the explicit settings action.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote
from weakref import WeakKeyDictionary

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.web.console_readonly_policy import (
    POLICY_DESCRIPTION,
    POLICY_NAME,
    REVIEWED_ACTIONS,
    managed_actions,
    policy_json,
    policy_revision,
)

_DEFAULT_ROLE_NAME = "IacCodeConsoleSigninRole"
_DEFAULT_ROLE_SESSION_NAME = "iac-code-console"
_READ_ONLY_POLICY_NAME = "ReadOnlyAccess"
_ROLE_DESCRIPTION = "Read-only role used by iac-code embedded console previews."
_CACHE_REFRESH_SKEW_SECONDS = 120
_CACHE_MAX_ENTRIES = 8
_ROLE_ARN = re.compile(r"acs:ram::(?P<account_id>[0-9]{1,32}):role/(?P<role_name>[A-Za-z0-9.-]{1,64})\Z")
_RAM_USER_ARN = re.compile(r"acs:ram::[0-9]{1,32}:user/[A-Za-z0-9.@_-]{1,128}\Z")
_ASSUMED_ROLE_ARN = re.compile(r"(?P<role>acs:ram::[0-9]{1,32}:role/[A-Za-z0-9.-]{1,64})/[A-Za-z0-9.@_-]{2,64}\Z")

RpcCall = Callable[
    [AliyunCredential, str, str, str, Mapping[str, Any]],
    Awaitable[Mapping[str, Any]],
]
Clock = Callable[[], float]
_setup_locks: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()


def _setup_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _setup_locks:
        _setup_locks[loop] = asyncio.Lock()
    return _setup_locks[loop]


def console_signin_policy_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Local revision hint, deliberately not a cloud effective-permission test."""
    return {
        **settings,
        "supplementPolicy": {
            "name": POLICY_NAME,
            "revision": policy_revision(),
            "actions": sorted(REVIEWED_ACTIONS),
            "needsRepair": bool(settings.get("configured")) and settings.get("supplementRevision") != policy_revision(),
        },
    }


class ConsoleSigninRoleError(ValueError):
    """A stable UI code plus safe provider diagnostics, never raw requests."""

    def __init__(self, code: str, *, diagnostic: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = dict(diagnostic or {})


_DIAGNOSTIC_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIAGNOSTIC_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_ACCESS_DENIED_FIELDS = (
    "AuthAction",
    "AuthPrincipalType",
    "DeniedAction",
    "NoPermissionType",
    "PolicyType",
)


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    result: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(result) < 8:
        result.append(current)
        seen.add(id(current))
        nested = getattr(current, "inner_exception", None)
        if not isinstance(nested, BaseException):
            nested = current.__cause__ or current.__context__
        current = nested if isinstance(nested, BaseException) else None
    return tuple(result)


def _mapping_value(value: Any, *names: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    folded = {str(key).casefold(): item for key, item in value.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None


def _diagnostic_text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    # Web requests disable the generic display redactor. Provider diagnostics
    # must instead use the strict path so credentials in an SDK message can
    # never be copied into the browser response.
    from iac_code.utils.public_errors import sanitize_strict_text

    sanitized = sanitize_strict_text(value, fallback_summary="")
    return " ".join(sanitized.split())[:limit]


def _error_diagnostic(error: BaseException) -> dict[str, Any]:
    """Extract useful error fields without exposing an SDK request or headers."""
    for candidate in reversed(_exception_chain(error)):
        data = getattr(candidate, "data", None)
        code_value = (
            getattr(candidate, "code", None) or getattr(candidate, "error_code", None) or _mapping_value(data, "Code")
        )
        code = _diagnostic_text(code_value, limit=128)
        if not _DIAGNOSTIC_CODE.fullmatch(code):
            code = ""
        message_value = _mapping_value(data, "Message") or getattr(candidate, "message", None)
        message = _diagnostic_text(message_value, limit=1000)
        request_id_value = (
            getattr(candidate, "request_id", None)
            or getattr(candidate, "requestId", None)
            or _mapping_value(data, "RequestId")
        )
        request_id = _diagnostic_text(request_id_value, limit=256)
        if not _DIAGNOSTIC_REQUEST_ID.fullmatch(request_id):
            request_id = ""
        access_denied_value = (
            getattr(candidate, "access_denied_detail", None)
            or getattr(candidate, "accessDeniedDetail", None)
            or _mapping_value(data, "AccessDeniedDetail")
        )
        access_denied = {
            name: text
            for name in _ACCESS_DENIED_FIELDS
            if (text := _diagnostic_text(_mapping_value(access_denied_value, name), limit=256))
        }
        if code or message or request_id or access_denied:
            diagnostic: dict[str, Any] = {
                "source": "alibabaCloud" if code or request_id or access_denied else "client",
                "type": type(candidate).__name__,
            }
            if code:
                diagnostic["code"] = code
            if message:
                diagnostic["message"] = message
            if request_id:
                diagnostic["requestId"] = request_id
            if access_denied:
                diagnostic["accessDeniedDetail"] = access_denied
            return diagnostic

    message = _diagnostic_text(error, limit=1000)
    return {"source": "client", "type": type(error).__name__, **({"message": message} if message else {})}


def _role_error(code: str, error: BaseException) -> ConsoleSigninRoleError:
    return ConsoleSigninRoleError(code, diagnostic=_error_diagnostic(error))


def _local_role_error(code: str, diagnostic_code: str, message: str) -> ConsoleSigninRoleError:
    return ConsoleSigninRoleError(
        code,
        diagnostic={
            "source": "iacCode",
            "type": "ValidationError",
            "code": diagnostic_code,
            "message": message,
        },
    )


def _cloud_error_code(error: BaseException) -> str:
    code = _error_diagnostic(error).get("code")
    return code if isinstance(code, str) else ""


def _permission_error(code: str) -> bool:
    normalized = re.sub(r"[^a-z]", "", code.casefold())
    return any(
        marker in normalized
        for marker in ("accessdenied", "forbidden", "nopermission", "notauthorized", "permissiondenied", "unauthorized")
    )


def _rpc_query_strings(query: Mapping[str, Any]) -> dict[str, str]:
    """Serialize scalar RPC values before the generic OpenAPI query encoder."""
    result: dict[str, str] = {}
    for key, value in query.items():
        if not isinstance(key, str) or not key:
            raise TypeError("RPC query keys must be non-empty strings")
        if isinstance(value, str):
            result[key] = value
        elif isinstance(value, bool):
            result[key] = "true" if value else "false"
        elif isinstance(value, int):
            result[key] = str(value)
        else:
            raise TypeError("RPC query values must be strings, booleans, or integers")
    return result


def _canonical_role_arn(account_id: str) -> str:
    # RAM preserves RoleName for API input/display but canonicalizes the role
    # path in returned ARNs to lowercase.
    return f"acs:ram::{account_id}:role/{_DEFAULT_ROLE_NAME.lower()}"


def _same_role_arn(left: Any, right: str) -> bool:
    return isinstance(left, str) and left.casefold() == right.casefold()


def _trust_policy(principal: str) -> dict[str, Any]:
    return {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"RAM": [principal]},
            }
        ],
        "Version": "1",
    }


def _policy_document(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(unquote(value))
    except (json.JSONDecodeError, UnicodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _is_recoverable_created_role(payload: Mapping[str, Any], role_arn: str, principal: str) -> bool:
    """Recognize only the exact role left by our interrupted create sequence."""
    role = payload.get("Role")
    if not isinstance(role, Mapping):
        return False
    max_session_duration = role.get("MaxSessionDuration")
    try:
        max_session_duration = int(max_session_duration)
    except (TypeError, ValueError):
        return False
    allow_console_login = role.get("AllowConsoleLogin")
    if isinstance(allow_console_login, str):
        allow_console_login = allow_console_login.casefold() == "true"
    return (
        role.get("RoleName") == _DEFAULT_ROLE_NAME
        and _same_role_arn(role.get("Arn"), role_arn)
        and role.get("Description") == _ROLE_DESCRIPTION
        and max_session_duration == 3600
        and allow_console_login is True
        and _policy_document(role.get("AssumeRolePolicyDocument")) == _trust_policy(principal)
    )


def _rpc_config_values(credential: AliyunCredential, endpoint: str) -> dict[str, Any]:
    from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime

    values: dict[str, Any] = {
        "endpoint": endpoint,
        "region_id": credential.region_id,
    }
    dynamic_client = aliyun_credential_runtime().sdk_client(credential)
    if dynamic_client is not None:
        values["credential"] = dynamic_client
        return values
    values["access_key_id"] = credential.access_key_id
    values["access_key_secret"] = credential.access_key_secret
    if credential.mode in {"StsToken", "OAuth"}:
        values["security_token"] = credential.sts_token
    return values


async def _call_rpc(
    credential: AliyunCredential,
    endpoint: str,
    action: str,
    version: str,
    query: Mapping[str, Any],
) -> Mapping[str, Any]:
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_openapi.client import Client as OpenApiClient
    from darabonba.runtime import RuntimeOptions

    client = OpenApiClient(open_api_models.Config(**_rpc_config_values(credential, endpoint)))
    params = open_api_models.Params(
        action=action,
        version=version,
        protocol="HTTPS",
        pathname="/",
        method="POST",
        auth_type="AK",
        style="RPC",
        body_type="json",
        req_body_type="json",
    )
    request = open_api_models.OpenApiRequest(query=_rpc_query_strings(query))
    result = await client.call_api_async(params, request, RuntimeOptions(autoretry=False, max_attempts=1))
    body = result.get("body", result)
    return body if isinstance(body, Mapping) else {}


async def _resolve_source_credential(credential: AliyunCredential) -> AliyunCredential:
    from iac_code.services.providers.aliyun import AliyunCredentials
    from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime

    try:
        if credential.mode == "OAuth":
            credential = await asyncio.to_thread(AliyunCredentials.refresh_oauth_if_needed, credential)
        if credential.mode in {"RamRoleArn", "EcsRamRole"}:
            credential = await aliyun_credential_runtime().resolve(credential)
    except Exception as error:
        raise _role_error("resource_signin_role_source_rejected", error) from None
    if credential.mode not in {"AK", "StsToken", "OAuth"} or not all(
        (credential.access_key_id, credential.access_key_secret)
    ):
        raise ConsoleSigninRoleError("resource_signin_role_source_rejected")
    if credential.mode in {"StsToken", "OAuth"} and not credential.sts_token:
        raise ConsoleSigninRoleError("resource_signin_role_source_rejected")
    return credential


def _source_fingerprint(credential: AliyunCredential, role_arn: str) -> str:
    # Hashing prevents credentials from appearing in cache keys, reprs or diagnostics.
    raw = "\0".join(
        (
            credential.mode,
            credential.access_key_id,
            credential.access_key_secret,
            credential.sts_token,
            role_arn,
        )
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _expiration_epoch(value: Any) -> int:
    if not isinstance(value, str) or not value:
        raise ConsoleSigninRoleError("resource_signin_role_response_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ConsoleSigninRoleError("resource_signin_role_response_invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


async def assume_console_signin_role(
    source: AliyunCredential,
    role_arn: str,
    *,
    rpc_call: RpcCall = _call_rpc,
    clock: Clock = time.time,
) -> AliyunCredential:
    if _ROLE_ARN.fullmatch(role_arn) is None:
        raise ConsoleSigninRoleError("resource_signin_role_arn_invalid")
    try:
        payload = await rpc_call(
            source,
            "sts.aliyuncs.com",
            "AssumeRole",
            "2015-04-01",
            {
                "RoleArn": role_arn,
                "RoleSessionName": _DEFAULT_ROLE_SESSION_NAME,
                "DurationSeconds": 3600,
            },
        )
    except Exception as error:
        code = _cloud_error_code(error)
        stable_code = (
            "resource_signin_role_assume_denied" if _permission_error(code) else "resource_signin_role_assume_failed"
        )
        raise _role_error(stable_code, error) from None
    credentials = payload.get("Credentials")
    if not isinstance(credentials, Mapping):
        raise ConsoleSigninRoleError("resource_signin_role_response_invalid")
    access_key_id = credentials.get("AccessKeyId")
    access_key_secret = credentials.get("AccessKeySecret")
    security_token = credentials.get("SecurityToken")
    expiration = _expiration_epoch(credentials.get("Expiration"))
    if not all(isinstance(value, str) and value for value in (access_key_id, access_key_secret, security_token)):
        raise ConsoleSigninRoleError("resource_signin_role_response_invalid")
    if expiration <= int(clock()) + _CACHE_REFRESH_SKEW_SECONDS:
        raise ConsoleSigninRoleError("resource_signin_role_response_invalid")
    return AliyunCredential(
        mode="StsToken",
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        sts_token=security_token,
        sts_expiration=expiration,
        region_id=source.region_id,
    )


class ConsoleSigninRoleCredentialCache:
    """Small process-memory cache with per-source single-flight refresh."""

    def __init__(
        self,
        *,
        assume: Callable[[AliyunCredential, str], Awaitable[AliyunCredential]] = assume_console_signin_role,
        clock: Clock = time.time,
    ) -> None:
        self._assume = assume
        self._clock = clock
        self._entries: OrderedDict[str, AliyunCredential] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def invalidate(self) -> None:
        self._entries.clear()
        self._locks.clear()

    def prime(self, source: AliyunCredential, role_arn: str, credential: AliyunCredential) -> None:
        key = _source_fingerprint(source, role_arn)
        self._entries[key] = credential
        self._entries.move_to_end(key)
        while len(self._entries) > _CACHE_MAX_ENTRIES:
            stale_key, _ = self._entries.popitem(last=False)
            self._locks.pop(stale_key, None)

    def _fresh(self, credential: AliyunCredential | None) -> bool:
        return credential is not None and credential.sts_expiration > int(self._clock()) + _CACHE_REFRESH_SKEW_SECONDS

    async def get(self, credential: AliyunCredential, role_arn: str, *, force: bool = False) -> AliyunCredential:
        source = await _resolve_source_credential(credential)
        key = _source_fingerprint(source, role_arn)
        cached = self._entries.get(key)
        if not force and cached is not None and self._fresh(cached):
            self._entries.move_to_end(key)
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._entries.get(key)
            if not force and cached is not None and self._fresh(cached):
                self._entries.move_to_end(key)
                return cached
            refreshed = await self._assume(source, role_arn)
            self._entries[key] = refreshed
            self._entries.move_to_end(key)
            while len(self._entries) > _CACHE_MAX_ENTRIES:
                stale_key, _ = self._entries.popitem(last=False)
                self._locks.pop(stale_key, None)
            return refreshed


_credential_cache = ConsoleSigninRoleCredentialCache()


def invalidate_console_signin_role_cache() -> None:
    _credential_cache.invalidate()


async def console_signin_role_credential(
    credential: AliyunCredential,
    role_arn: str,
    *,
    force: bool = False,
) -> AliyunCredential:
    return await _credential_cache.get(credential, role_arn, force=force)


def _trusted_principal(identity: Mapping[str, Any]) -> tuple[str, str]:
    identity_type = identity.get("IdentityType")
    account_id = identity.get("AccountId")
    arn = identity.get("Arn")
    if identity_type == "Account":
        raise ConsoleSigninRoleError("resource_signin_role_root_unsupported")
    if not isinstance(account_id, str) or not account_id or not isinstance(arn, str):
        raise ConsoleSigninRoleError("resource_signin_role_identity_invalid")
    if identity_type == "RAMUser" and _RAM_USER_ARN.fullmatch(arn):
        return account_id, arn
    if identity_type == "AssumedRoleUser":
        match = _ASSUMED_ROLE_ARN.fullmatch(arn)
        if match is not None:
            return account_id, match.group("role")
    raise ConsoleSigninRoleError("resource_signin_role_identity_invalid")


async def _ensure_supplement_policy(source: AliyunCredential, rpc_call: RpcCall) -> None:
    query = {"PolicyName": POLICY_NAME, "PolicyType": "Custom"}
    try:
        payload = await rpc_call(source, "ram.aliyuncs.com", "GetPolicy", "2015-05-01", query)
    except Exception as error:
        if _cloud_error_code(error) not in {"EntityNotExist.Policy", "EntityNotExist"}:
            raise
        payload = None
    if payload is None:
        try:
            await rpc_call(
                source,
                "ram.aliyuncs.com",
                "CreatePolicy",
                "2015-05-01",
                {"PolicyName": POLICY_NAME, "Description": POLICY_DESCRIPTION, "PolicyDocument": policy_json()},
            )
        except Exception as create_error:
            # Another local instance may have completed the same explicit setup.
            # Unknown outcomes are not retried: the next click starts with GetPolicy.
            if _cloud_error_code(create_error) not in {"EntityAlreadyExists.Policy", "EntityAlreadyExists"}:
                raise
        payload = await rpc_call(source, "ram.aliyuncs.com", "GetPolicy", "2015-05-01", query)
    policy = payload.get("Policy", {})
    version = payload.get("DefaultPolicyVersion", {})
    if (
        not isinstance(policy, Mapping)
        or policy.get("PolicyName") != POLICY_NAME
        or policy.get("PolicyType") != "Custom"
        or policy.get("Description") != POLICY_DESCRIPTION
        or not isinstance(version, Mapping)
    ):
        raise ConsoleSigninRoleError("resource_signin_role_policy_conflict")
    actions = managed_actions(_policy_document(version.get("PolicyDocument")))
    if actions is None:
        raise ConsoleSigninRoleError("resource_signin_role_policy_conflict")
    if actions != set(REVIEWED_ACTIONS):
        entities = await rpc_call(source, "ram.aliyuncs.com", "ListEntitiesForPolicy", "2015-05-01", query)
        members = {}
        for container_name, member_name in (("Users", "User"), ("Groups", "Group"), ("Roles", "Role")):
            container = entities.get(container_name)
            items = container.get(member_name) if isinstance(container, Mapping) else None
            if not isinstance(items, list):
                raise ConsoleSigninRoleError("resource_signin_role_policy_conflict")
            members[container_name] = items
        roles = members["Roles"]
        if (
            members["Users"]
            or members["Groups"]
            or any(not isinstance(role, Mapping) or role.get("RoleName") != _DEFAULT_ROLE_NAME for role in roles)
        ):
            raise ConsoleSigninRoleError("resource_signin_role_policy_conflict")
        versions = await rpc_call(source, "ram.aliyuncs.com", "ListPolicyVersions", "2015-05-01", query)
        history = versions.get("PolicyVersions", {}).get("PolicyVersion", [])
        if not isinstance(history, list) or not 1 <= len(history) <= 5:
            raise ConsoleSigninRoleError("resource_signin_role_policy_conflict")
        # RAM rotation may delete an old non-default version. Inspect every
        # version first so customer-authored history is never discarded.
        for item in history:
            if not isinstance(item, Mapping) or not re.fullmatch(r"v[0-9]+", str(item.get("VersionId", ""))):
                raise ConsoleSigninRoleError("resource_signin_role_policy_conflict")
            old = await rpc_call(
                source,
                "ram.aliyuncs.com",
                "GetPolicyVersion",
                "2015-05-01",
                {**query, "VersionId": item["VersionId"]},
            )
            old_version = old.get("PolicyVersion", {})
            if (
                not isinstance(old_version, Mapping)
                or managed_actions(_policy_document(old_version.get("PolicyDocument"))) is None
            ):
                raise ConsoleSigninRoleError("resource_signin_role_policy_conflict")
        await rpc_call(
            source,
            "ram.aliyuncs.com",
            "CreatePolicyVersion",
            "2015-05-01",
            {
                "PolicyName": POLICY_NAME,
                "PolicyDocument": policy_json(),
                "SetAsDefault": True,
                "RotateStrategy": "DeleteOldestNonDefaultVersionWhenLimitExceeded",
            },
        )
        verified = await rpc_call(source, "ram.aliyuncs.com", "GetPolicy", "2015-05-01", query)
        current = verified.get("DefaultPolicyVersion", {})
        if not isinstance(current, Mapping) or managed_actions(_policy_document(current.get("PolicyDocument"))) != set(
            REVIEWED_ACTIONS
        ):
            raise ConsoleSigninRoleError("resource_signin_role_policy_verify_failed")
    await _attach_role_policy(source, rpc_call, "Custom", POLICY_NAME)


async def _attach_role_policy(source: AliyunCredential, rpc_call: RpcCall, policy_type: str, name: str) -> None:
    # RAM ListPoliciesForRole is not paginated; its only input is RoleName.
    attached = await rpc_call(
        source, "ram.aliyuncs.com", "ListPoliciesForRole", "2015-05-01", {"RoleName": _DEFAULT_ROLE_NAME}
    )
    container = attached.get("Policies")
    policies = container.get("Policy") if isinstance(container, Mapping) else None
    if not isinstance(policies, list) or any(not isinstance(policy, Mapping) for policy in policies):
        raise ConsoleSigninRoleError("resource_signin_role_policy_verify_failed")
    if any(policy.get("PolicyType") == policy_type and policy.get("PolicyName") == name for policy in policies):
        return
    await rpc_call(
        source,
        "ram.aliyuncs.com",
        "AttachPolicyToRole",
        "2015-05-01",
        {"PolicyType": policy_type, "PolicyName": name, "RoleName": _DEFAULT_ROLE_NAME},
    )


async def create_console_signin_role(credential: AliyunCredential, *, rpc_call: RpcCall = _call_rpc) -> dict[str, Any]:
    async with _setup_lock():
        return await _create_console_signin_role(credential, rpc_call=rpc_call)


async def repair_console_signin_role(credential: AliyunCredential, *, rpc_call: RpcCall = _call_rpc) -> dict[str, Any]:
    """Explicit settings action only; never repair an arbitrary manually saved ARN."""
    async with _setup_lock():
        return await _create_console_signin_role(credential, rpc_call=rpc_call, repair=True)


async def _create_console_signin_role(
    credential: AliyunCredential,
    *,
    rpc_call: RpcCall = _call_rpc,
    repair: bool = False,
) -> dict[str, Any]:
    """Create, authorize and persist the dedicated read-only console role."""
    from iac_code.web.settings import console_signin_role_settings, save_console_signin_role

    source = await _resolve_source_credential(credential)
    try:
        identity = await rpc_call(source, "sts.aliyuncs.com", "GetCallerIdentity", "2015-04-01", {})
    except Exception as error:
        raise _role_error("resource_signin_role_identity_failed", error) from None
    account_id, principal = _trusted_principal(identity)
    role_arn = _canonical_role_arn(account_id)

    # A repeated click for the role we created is a connection test, not a second
    # CreateRole mutation. A role left behind after CreateRole succeeded but local
    # persistence failed is recoverable only when every app-owned field matches.
    saved_settings = console_signin_role_settings()
    existing = saved_settings.get("roleArn")
    needs_upgrade = saved_settings.get("supplementRevision") != policy_revision()
    if repair or (_same_role_arn(existing, role_arn) and needs_upgrade):
        if not _same_role_arn(existing, role_arn):
            raise ConsoleSigninRoleError("resource_signin_role_repair_target_invalid")
        try:
            existing_role = await rpc_call(
                source, "ram.aliyuncs.com", "GetRole", "2015-05-01", {"RoleName": _DEFAULT_ROLE_NAME}
            )
        except Exception as error:
            code = (
                "resource_signin_role_policy_denied"
                if _permission_error(_cloud_error_code(error))
                else "resource_signin_role_repair_failed"
            )
            raise _role_error(code, error) from None
        if not _is_recoverable_created_role(existing_role, role_arn, principal):
            raise ConsoleSigninRoleError("resource_signin_role_repair_target_invalid")
    if not _same_role_arn(existing, role_arn):
        trust_policy = json.dumps(_trust_policy(principal), separators=(",", ":"))
        try:
            created = await rpc_call(
                source,
                "ram.aliyuncs.com",
                "CreateRole",
                "2015-05-01",
                {
                    "RoleName": _DEFAULT_ROLE_NAME,
                    "Description": _ROLE_DESCRIPTION,
                    "AssumeRolePolicyDocument": trust_policy,
                    "MaxSessionDuration": 3600,
                    "AllowConsoleLogin": True,
                },
            )
        except Exception as error:
            cloud_code = _cloud_error_code(error)
            if cloud_code in {"EntityAlreadyExists.Role", "EntityAlreadyExists"}:
                try:
                    existing_role = await rpc_call(
                        source,
                        "ram.aliyuncs.com",
                        "GetRole",
                        "2015-05-01",
                        {"RoleName": _DEFAULT_ROLE_NAME},
                    )
                except Exception:
                    raise _role_error("resource_signin_role_name_conflict", error) from None
                if not _is_recoverable_created_role(existing_role, role_arn, principal):
                    raise _role_error("resource_signin_role_name_conflict", error) from None
                created = existing_role
            elif _permission_error(cloud_code):
                raise _role_error("resource_signin_role_create_denied", error) from None
            else:
                raise _role_error("resource_signin_role_create_failed", error) from None
        created_role = created.get("Role")
        returned_arn = created_role.get("Arn") if isinstance(created_role, Mapping) else None
        if returned_arn is not None and not _same_role_arn(returned_arn, role_arn):
            raise _local_role_error(
                "resource_signin_role_response_invalid",
                "UnexpectedRoleArn",
                "CreateRole returned a role ARN that does not match the requested account and role.",
            )
        # Remember that this exact role was created by the explicit action so a
        # later click can safely finish attaching the policy after a partial failure.
        save_console_signin_role(role_arn, setup_complete=False)
        saved_settings = {"roleArn": role_arn, "setupComplete": False}
    if repair or saved_settings.get("setupComplete") is not True or needs_upgrade:
        # Partial failures remain retryable without recreating the role or
        # trusting the former setupComplete flag as proof of current IAM grants.
        save_console_signin_role(role_arn, setup_complete=False)
        invalidate_console_signin_role_cache()
        try:
            await _attach_role_policy(source, rpc_call, "System", _READ_ONLY_POLICY_NAME)
        except Exception as error:
            cloud_code = _cloud_error_code(error)
            stable_code = (
                "resource_signin_role_attach_denied"
                if _permission_error(cloud_code)
                else "resource_signin_role_attach_failed"
            )
            raise _role_error(stable_code, error) from None
        try:
            await _ensure_supplement_policy(source, rpc_call)
        except ConsoleSigninRoleError:
            raise
        except Exception as error:
            code = (
                "resource_signin_role_policy_denied"
                if _permission_error(_cloud_error_code(error))
                else "resource_signin_role_policy_failed"
            )
            raise _role_error(code, error) from None
        save_console_signin_role(role_arn, setup_complete=True, supplement_revision=policy_revision())
        invalidate_console_signin_role_cache()

    try:
        assumed = await assume_console_signin_role(source, role_arn, rpc_call=rpc_call)
    except ConsoleSigninRoleError as error:
        # The role is useful and remains configured, but caller-side
        # sts:AssumeRole may still need to be granted by an administrator.
        return {
            "status": "needsPermission",
            "roleArn": role_arn,
            "configured": True,
            "policyName": _READ_ONLY_POLICY_NAME,
            "supplementPolicyName": POLICY_NAME,
            "error": {
                "code": error.code,
                **({"diagnostic": error.diagnostic} if error.diagnostic else {}),
            },
        }
    _credential_cache.prime(source, role_arn, assumed)
    return {
        "status": "ready",
        "roleArn": role_arn,
        "configured": True,
        "policyName": _READ_ONLY_POLICY_NAME,
        "supplementPolicyName": POLICY_NAME,
    }


__all__ = [
    "ConsoleSigninRoleCredentialCache",
    "ConsoleSigninRoleError",
    "assume_console_signin_role",
    "console_signin_role_credential",
    "create_console_signin_role",
    "repair_console_signin_role",
    "console_signin_policy_settings",
    "invalidate_console_signin_role_cache",
]
