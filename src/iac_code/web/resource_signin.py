"""Short-lived Alibaba Cloud console sign-in URLs for resource previews.

Long-lived and temporary credential material stays in this server-side module.
The browser-facing API returns a local one-use relay URL, so even Alibaba
Cloud's single-use ``SigninToken`` never enters application JavaScript.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_identity import AliyunCallerIdentity, AliyunCallerIdentityResolver
from iac_code.web.resource_link_catalog import resource_link_catalog

_MAX_DESTINATION_LENGTH = 8192
_MAX_SIGNIN_RESPONSE_BYTES = 65536
_SIGNIN_TOKEN = re.compile(r"[^\x00-\x1f\x7f]{1,8192}\Z")
_TEMPLATE_AUTHORITY = re.compile(r"^(?:https:)?//([^/?#]+)", re.IGNORECASE)
_REGION_PLACEHOLDER = re.compile(r"@?\{RegionId\}", re.IGNORECASE)
_REGION_HOST_VALUE = r"[a-z][a-z0-9-]{1,63}"
_ALIYUN_CONSOLE_SUFFIX = ".console.aliyun.com"
_DOMESTIC_EXTERNAL_ONLY_CONSOLE_HOSTS = frozenset(
    {
        "acmnext.console.aliyun.com",
        "apigateway.console.aliyun.com",
        "appflow.console.aliyun.com",
        "dhsnext.console.aliyun.com",
        "dms.console.aliyun.com",
        "ehpc.console.aliyun.com",
        "emr.console.aliyun.com",
        "emr-next.console.aliyun.com",
        "essnew.console.aliyun.com",
        "gws.console.aliyun.com",
    }
)
_DOMESTIC_EXTRA_CONSOLE_HOSTS = frozenset(
    {
        "cen.console.aliyun.com",
        "cloudmonitor.console.aliyun.com",
        "dnsnext.console.aliyun.com",
        "eci.console.aliyun.com",
        "ecs.console.aliyun.com",
        "essnew.console.aliyun.com",
        "expressconnect.console.aliyun.com",
        "kvstore.console.aliyun.com",
        "lindorm.console.aliyun.com",
        "mns.console.aliyun.com",
        "mongodb.console.aliyun.com",
        "ons.console.aliyun.com",
        "ots.console.aliyun.com",
        "ram.console.aliyun.com",
        "ros.console.aliyun.com",
        "vpc.console.aliyun.com",
    }
)


class ResourceSigninError(ValueError):
    """A finite, non-secret result code suitable for the resource-link UI."""


@dataclass(frozen=True)
class ResourceSigninSite:
    partition: str
    origin: str
    ticket_type: str = "normal"

    @property
    def login_url(self) -> str:
        return self.origin + "/login.htm"

    @property
    def federation_url(self) -> str:
        return self.origin + "/federation"


_SITES = {
    "aliyun": ResourceSigninSite("aliyun", "https://signin.aliyun.com"),
    "alibabacloud": ResourceSigninSite("alibabacloud", "https://signin.alibabacloud.com"),
}

SigninTokenFetcher = Callable[[ResourceSigninSite, AliyunCredential], Awaitable[str]]
CredentialLoader = Callable[[], AliyunCredential | None]
IdentityResolver = Callable[[AliyunCredential, str], Awaitable[AliyunCallerIdentity]]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _template_host(template: str) -> str:
    match = _TEMPLATE_AUTHORITY.match(template)
    if match is None or "{" in match.group(1):
        return ""
    return match.group(1).lower()


def _embedded_console_authority(authority: str, partition: str) -> str:
    """Derive the mini-console authority from a released console authority."""
    if (
        partition != "aliyun"
        or authority in _DOMESTIC_EXTERNAL_ONLY_CONSOLE_HOSTS
        or not authority.endswith(_ALIYUN_CONSOLE_SUFFIX)
    ):
        return ""
    product = authority[: -len(_ALIYUN_CONSOLE_SUFFIX)]
    if product.startswith("elasticsearch-"):
        return "elasticsearch4service-" + product.removeprefix("elasticsearch-") + _ALIYUN_CONSOLE_SUFFIX
    return product + "4service" + _ALIYUN_CONSOLE_SUFFIX


@lru_cache(maxsize=1)
def _resource_console_host_patterns() -> Mapping[str, tuple[re.Pattern[str], ...]]:
    """Constrained host patterns for the catalog's region-qualified hosts."""
    result: dict[str, tuple[re.Pattern[str], ...]] = {}
    for partition, templates in resource_link_catalog().templates.items():
        patterns: list[re.Pattern[str]] = []
        for template in templates.values():
            match = _TEMPLATE_AUTHORITY.match(template)
            authority = match.group(1).lower() if match is not None else ""
            if not authority or not _REGION_PLACEHOLDER.search(authority):
                continue
            for candidate in (authority, _embedded_console_authority(authority, partition)):
                if not candidate:
                    continue
                pieces = _REGION_PLACEHOLDER.split(candidate)
                remainder = _REGION_PLACEHOLDER.sub("", candidate)
                if "{" in remainder or "}" in remainder or "@" in remainder:
                    continue
                expression = _REGION_HOST_VALUE.join(re.escape(piece) for piece in pieces)
                patterns.append(re.compile(rf"{expression}\Z"))
        result[partition] = tuple(patterns)
    return result


@lru_cache(maxsize=1)
def resource_console_hosts() -> Mapping[str, frozenset[str]]:
    """Exact hosts released by the checked-in ROS link catalog."""
    catalog = resource_link_catalog()
    result: dict[str, frozenset[str]] = {}
    extras = {
        "aliyun": set(_DOMESTIC_EXTRA_CONSOLE_HOSTS),
        "alibabacloud": {
            "ros-intl.console.alibabacloud.com",
            "ros4service.console.alibabacloud.com",
        },
    }
    for partition in _SITES:
        hosts = {_template_host(template) for template in catalog.templates[partition].values()}
        hosts = (hosts - {""}) | extras[partition]
        embedded = {_embedded_console_authority(host, partition) for host in hosts}
        result[partition] = frozenset(hosts | (embedded - {""}))
    return result


def validate_console_destination(value: Any, partition: str) -> str:
    if partition not in _SITES or not isinstance(value, str) or not 0 < len(value) <= _MAX_DESTINATION_LENGTH:
        raise ResourceSigninError("resource_signin_destination_invalid")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ResourceSigninError("resource_signin_destination_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ResourceSigninError("resource_signin_destination_invalid") from None
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.netloc != hostname
        or (
            hostname not in resource_console_hosts()[partition]
            and not any(pattern.fullmatch(hostname) for pattern in _resource_console_host_patterns()[partition])
        )
    ):
        raise ResourceSigninError("resource_signin_destination_invalid")
    return value


def _signin_site_for_destination(partition: str, destination: str) -> ResourceSigninSite:
    destination = validate_console_destination(destination, partition)
    hostname = urlsplit(destination).hostname or ""
    embedded = hostname == "ros4service.console.alibabacloud.com" or (
        partition == "aliyun"
        and (
            hostname.endswith("4service.console.aliyun.com")
            or (hostname.startswith("elasticsearch4service-") and hostname.endswith(_ALIYUN_CONSOLE_SUFFIX))
        )
    )
    return replace(_SITES[partition], ticket_type="mini" if embedded else "normal")


def clean_signin_request(value: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {"accountScope", "preferredPartition", "destinations"}:
        raise ResourceSigninError("resource_signin_request_invalid")
    account_scope = value.get("accountScope")
    preferred = value.get("preferredPartition")
    destinations = value.get("destinations")
    if (
        not isinstance(account_scope, str)
        or not account_scope
        or len(account_scope) > 256
        or preferred not in _SITES
        or not isinstance(destinations, dict)
        or not destinations
        or set(destinations) - set(_SITES)
        or preferred not in destinations
    ):
        raise ResourceSigninError("resource_signin_request_invalid")
    clean = {
        partition: validate_console_destination(destination, partition)
        for partition, destination in destinations.items()
    }
    return account_scope, {
        preferred: clean[preferred],
        **{key: item for key, item in clean.items() if key != preferred},
    }


def _default_credential_loader() -> AliyunCredential | None:
    from iac_code.services.cloud_credentials import CloudCredentials

    return CloudCredentials().get_provider("aliyun")


async def _temporary_signin_credential(credential: AliyunCredential) -> AliyunCredential:
    from iac_code.services.providers.aliyun import AliyunCredentials
    from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime
    from iac_code.web.resource_signin_role import ConsoleSigninRoleError, console_signin_role_credential
    from iac_code.web.settings import console_signin_role_settings

    try:
        role_settings = console_signin_role_settings()
        role_arn = str(role_settings.get("roleArn") or "") if role_settings.get("setupComplete") is not False else ""
        if role_arn:
            return await console_signin_role_credential(credential, role_arn)
        if credential.mode == "OAuth":
            credential = await asyncio.to_thread(AliyunCredentials.refresh_oauth_if_needed, credential)
        if credential.mode in {"RamRoleArn", "EcsRamRole"}:
            credential = await aliyun_credential_runtime().resolve(credential)
    except ConsoleSigninRoleError:
        raise ResourceSigninError("resource_signin_role_assume_denied") from None
    except Exception:
        # Provider/SDK exceptions must not reach the HTTP response because some
        # transports include request or credential details in their messages.
        raise ResourceSigninError("resource_signin_credentials_rejected") from None
    if credential.mode not in {"StsToken", "OAuth"} or not all(
        (credential.access_key_id, credential.access_key_secret, credential.sts_token)
    ):
        raise ResourceSigninError("resource_signin_temporary_credentials_required")
    return credential


def _blocking_signin_token(site: ResourceSigninSite, credential: AliyunCredential) -> str:
    # The official endpoint accepts these temporary fields as URL-encoded RPC
    # parameters. urllib is intentionally used instead of an HTTP client whose
    # INFO request logger could persist the query string.
    query = urlencode(
        {
            "Action": "GetSigninToken",
            "AccessKeyId": credential.access_key_id,
            "AccessKeySecret": credential.access_key_secret,
            "SecurityToken": credential.sts_token,
            "TicketType": site.ticket_type,
        }
    )
    request = Request(site.federation_url + "?" + query, headers={"Accept": "application/json"})
    try:
        with build_opener(_RejectRedirects()).open(request, timeout=10) as response:
            content = response.read(_MAX_SIGNIN_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        raise ResourceSigninError("resource_signin_credentials_rejected") from None
    if len(content) > _MAX_SIGNIN_RESPONSE_BYTES:
        raise ResourceSigninError("resource_signin_response_invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ResourceSigninError("resource_signin_response_invalid") from None
    if isinstance(payload, dict) and isinstance(payload.get("ErrorCode"), str):
        raise ResourceSigninError("resource_signin_credentials_rejected")
    token = payload.get("SigninToken") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not _SIGNIN_TOKEN.fullmatch(token):
        raise ResourceSigninError("resource_signin_response_invalid")
    return token


async def fetch_signin_token(site: ResourceSigninSite, credential: AliyunCredential) -> str:
    return await asyncio.to_thread(_blocking_signin_token, site, credential)


async def resolve_signin_identity(credential: AliyunCredential, region_id: str) -> AliyunCallerIdentity:
    return await AliyunCallerIdentityResolver().resolve(credential, region_id)


async def resource_console_open_capability(
    *,
    credential_loader: CredentialLoader = _default_credential_loader,
    identity_resolver: IdentityResolver = resolve_signin_identity,
) -> dict[str, str]:
    """Choose the default resource-link behavior without exposing credentials.

    A normal browser tab is the fail-safe default. Explicit role modes can be
    recognized locally; a generic STS token is eligible only after Alibaba
    Cloud confirms that it represents an assumed-role session.
    """
    try:
        credential = await asyncio.to_thread(credential_loader)
    except Exception:
        return {"openMode": "external"}
    if credential is None:
        return {"openMode": "external"}
    try:
        from iac_code.web.settings import console_signin_role_settings

        role_settings = console_signin_role_settings()
        if role_settings.get("roleArn") and role_settings.get("setupComplete") is not False:
            await _temporary_signin_credential(credential)
            return {"openMode": "embedded"}
    except Exception:
        return {"openMode": "external"}
    if credential.mode in {"RamRoleArn", "EcsRamRole"}:
        return {"openMode": "embedded"}
    if credential.mode != "StsToken":
        return {"openMode": "external"}
    try:
        temporary = await _temporary_signin_credential(credential)
        identity = await identity_resolver(temporary, temporary.region_id)
    except Exception:
        return {"openMode": "external"}
    return {"openMode": "embedded" if identity.kind == "ram_role" else "external"}


def build_console_signin_url(
    site: ResourceSigninSite,
    destination: str,
    signin_token: str,
    *,
    login_url: str | None = None,
) -> str:
    return (
        site.federation_url
        + "?"
        + urlencode(
            {
                "Action": "Login",
                "LoginUrl": login_url or site.login_url,
                "Destination": destination,
                "SigninToken": signin_token,
            }
        )
    )


async def create_resource_console_signin(
    raw: Any,
    *,
    credential_loader: CredentialLoader = _default_credential_loader,
    token_fetcher: SigninTokenFetcher = fetch_signin_token,
    identity_resolver: IdentityResolver = resolve_signin_identity,
    allow_unknown_account_scope: bool = False,
    login_url: str | None = None,
) -> dict[str, str]:
    """Resolve one credential/site and return a one-time console login URL."""
    from iac_code.tools.cloud.aliyun.resource_reference_extractors import credential_reference_scope_matches

    account_scope, destinations = clean_signin_request(raw)
    credential = await asyncio.to_thread(credential_loader)
    if credential is None:
        raise ResourceSigninError("resource_signin_credentials_required")
    scope_match = credential_reference_scope_matches(credential, account_scope)
    if scope_match is False or (scope_match is None and not allow_unknown_account_scope):
        raise ResourceSigninError("resource_signin_account_changed")
    configured_partition = {
        "CN": "aliyun",
        "INTL": "alibabacloud",
    }.get(credential.oauth_site_type)
    if configured_partition is not None:
        if configured_partition not in destinations:
            raise ResourceSigninError("resource_signin_site_mismatch")
        destinations = {configured_partition: destinations[configured_partition]}
    temporary = await _temporary_signin_credential(credential)
    try:
        identity = await identity_resolver(temporary, temporary.region_id)
    except Exception:
        raise ResourceSigninError("resource_signin_credentials_rejected") from None
    if identity.kind != "ram_role":
        raise ResourceSigninError("resource_signin_role_required")
    rejected = False
    for partition, destination in destinations.items():
        try:
            site = _signin_site_for_destination(partition, destination)
            token = await token_fetcher(site, temporary)
        except ResourceSigninError as error:
            if str(error) == "resource_signin_credentials_rejected":
                rejected = True
                continue
            raise
        return {
            "status": "ready",
            "consolePartition": partition,
            "destination": destination,
            "url": build_console_signin_url(site, destination, token, login_url=login_url),
        }
    if rejected:
        code = (
            "resource_signin_credentials_rejected"
            if configured_partition is not None
            else "resource_signin_site_mismatch"
        )
        raise ResourceSigninError(code)
    raise ResourceSigninError("resource_signin_failed")


__all__ = [
    "ResourceSigninError",
    "ResourceSigninSite",
    "build_console_signin_url",
    "clean_signin_request",
    "create_resource_console_signin",
    "fetch_signin_token",
    "resource_console_open_capability",
    "resource_console_hosts",
    "resolve_signin_identity",
    "validate_console_destination",
]
