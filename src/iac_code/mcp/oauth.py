from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import posixpath
import secrets
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, parse_http_list, parse_keqv_list, urlopen

import httpx
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.oauth2 import resource_url_from_server_url
from mcp.shared.auth import OAuthClientMetadata
from mcp.shared.auth import OAuthMetadata as SDKOAuthMetadata

from iac_code.desktop.external_env import (
    is_desktop_runtime,
    open_desktop_browser,
)
from iac_code.i18n import _
from iac_code.mcp.errors import MCPNeedsAuthError
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig
from iac_code.utils.public_errors import sanitize_public_text


@dataclass(frozen=True)
class NeedsAuthEntry:
    server_name: str
    reason: str
    expires_at: float
    auth_error: str | None = None
    required_scopes: tuple[str, ...] = ()
    resource_metadata_url: str | None = None


class MCPNeedsAuthCache:
    def __init__(self, *, ttl_seconds: int = 900, now: Callable[[], float] | None = None) -> None:
        self._ttl_seconds = ttl_seconds
        self._now = now or time.time
        self._entries: dict[str, NeedsAuthEntry] = {}

    def mark(
        self,
        server_name: str,
        reason: str,
        *,
        auth_error: str | None = None,
        required_scopes: tuple[str, ...] | list[str] = (),
        resource_metadata_url: str | None = None,
    ) -> None:
        self._entries[server_name] = NeedsAuthEntry(
            server_name=server_name,
            reason=reason,
            expires_at=self._now() + self._ttl_seconds,
            auth_error=auth_error,
            required_scopes=tuple(required_scopes),
            resource_metadata_url=resource_metadata_url,
        )

    def get(self, server_name: str) -> NeedsAuthEntry | None:
        entry = self._entries.get(server_name)
        if entry is None:
            return None
        if entry.expires_at <= self._now():
            self._entries.pop(server_name, None)
            return None
        return entry

    def clear(self, server_name: str) -> None:
        self._entries.pop(server_name, None)


class TokenRefreshCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, concurrent.futures.Future[Any]] = {}

    async def refresh(self, key: str, refresh_func: Callable[[], Awaitable[Any]]) -> Any:
        owner = False
        with self._lock:
            future = self._inflight.get(key)
            if future is None:
                future = concurrent.futures.Future()
                self._inflight[key] = future
                owner = True
        if not owner:
            return await asyncio.shield(asyncio.wrap_future(future))
        try:
            result = await refresh_func()
        except BaseException as exc:
            if not future.cancelled():
                try:
                    future.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    pass
            raise
        else:
            if not future.cancelled():
                try:
                    future.set_result(result)
                except concurrent.futures.InvalidStateError:
                    pass
            return result
        finally:
            with self._lock:
                if self._inflight.get(key) is future:
                    self._inflight.pop(key, None)


_DEFAULT_REFRESH_COORDINATOR = TokenRefreshCoordinator()
_BROWSER_OPEN_EXIT_TIMEOUT_SECONDS = 1.0
_OAUTH_STORAGE_KINDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_marker",
    "auth_flow_marker",
    "client_id",
    "client_secret",
    "client_auth_method",
)
_OAUTH_STORAGE_KEY_SALT = b"iac-code-mcp-oauth-storage-key-v2"
_OAUTH_SIGNATURE_INDEX_SALT = b"iac-code-mcp-oauth-signature-index-v2"
_OAUTH_KEY_DERIVATION_ITERATIONS = 10_000


@dataclass(frozen=True)
class OAuthMetadata:
    authorization_endpoint: str
    token_endpoint: str
    scopes_supported: list[str] = field(default_factory=list)
    issuer: str | None = None
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    resource: str | None = None
    client_id_metadata_document_supported: bool | None = None
    requires_public_endpoints: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class OAuthFlowResult:
    authorization_url: str
    access_token_key: str
    refresh_token_key: str | None = None


class OAuthTokenError(RuntimeError):
    def __init__(self, error: str, description: str = "", *, status_code: int | None = None) -> None:
        self.error = error
        self.description = description
        self.status_code = status_code
        message = error if not description else "{}: {}".format(error, description)
        super().__init__(message)


@dataclass(frozen=True)
class MCPAuthChallenge:
    status_code: int | None = None
    scheme: str | None = None
    error: str | None = None
    error_description: str | None = None
    required_scopes: tuple[str, ...] = ()
    resource_metadata_url: str | None = None
    raw_www_authenticate: str | None = None


@dataclass(eq=False)
class OAuthPendingFlow:
    config: MCPServerConfig
    storage: MCPSecretStorage
    metadata: OAuthMetadata
    callback: "_LoopbackCallback"
    redirect_uri: str
    authorization_url: str
    verifier: str
    scope: MCPConfigScope | str | None = None
    timeout_seconds: float = 120.0
    browser_opened: bool = False
    result_future: concurrent.futures.Future[OAuthFlowResult] | None = None
    auth_flow_marker: str | None = None

    def close(self) -> None:
        self.callback.close()

    def wait(self) -> OAuthFlowResult:
        if self.result_future is not None:
            try:
                return self.result_future.result(timeout=self.timeout_seconds + 30)
            finally:
                self.close()
        try:
            code = self.callback.wait_for_code(self.timeout_seconds)
        finally:
            self.close()
        return _exchange_authorization_code(
            self.config,
            storage=self.storage,
            scope=self.scope,
            metadata=self.metadata,
            redirect_uri=self.redirect_uri,
            verifier=self.verifier,
            code=code,
            authorization_url=self.authorization_url,
            auth_flow_marker=self.auth_flow_marker,
        )

    def complete_manually(self, callback_or_code: str) -> OAuthFlowResult:
        try:
            code, state = _manual_oauth_code_and_state(callback_or_code)
            if state is None:
                state = self.callback.expected_state or _oauth_state_from_url(self.authorization_url)
            if self.callback.expected_state and state is not None and state != self.callback.expected_state:
                raise RuntimeError(_("OAuth callback state did not match."))
            if self.result_future is not None:
                self.callback.complete_manually(code, state)
                return self.wait()
            return _exchange_authorization_code(
                self.config,
                storage=self.storage,
                scope=self.scope,
                metadata=self.metadata,
                redirect_uri=self.redirect_uri,
                verifier=self.verifier,
                code=code,
                authorization_url=self.authorization_url,
                auth_flow_marker=self.auth_flow_marker,
            )
        finally:
            self.close()

    def submit_manually(self, callback_or_code: str) -> None:
        code, state = _manual_oauth_code_and_state(callback_or_code)
        if state is None:
            state = self.callback.expected_state or _oauth_state_from_url(self.authorization_url)
        if self.callback.expected_state and state is not None and state != self.callback.expected_state:
            raise RuntimeError(_("OAuth callback state did not match."))
        self.callback.complete_manually(code, state)


def build_oauth_discovery_urls(config: MCPServerConfig) -> list[str]:
    if not config.url:
        return []
    parsed = urlparse(config.url)
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    urls: list[str] = []
    if config.oauth and config.oauth.auth_server_metadata_url:
        urls.append(config.oauth.auth_server_metadata_url)
    path = parsed.path.rstrip("/")
    if path:
        urls.append(origin + "/.well-known/oauth-protected-resource" + path)
    urls.append(origin + "/.well-known/oauth-protected-resource")
    if path:
        urls.append(origin + "/.well-known/oauth-authorization-server" + path)
    urls.append(origin + "/.well-known/oauth-authorization-server")
    parent = posixpath.dirname(path)
    if parent and parent != "/":
        urls.append(origin + parent + "/.well-known/oauth-authorization-server")
    return _dedupe(urls)


def build_authorization_url(
    config: MCPServerConfig,
    *,
    authorization_endpoint: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: list[str] | None = None,
    resource: str | None = None,
) -> str:
    client_id = config.oauth.client_id if config.oauth else None
    query = {
        "response_type": "code",
        "client_id": client_id or "",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        query["scope"] = " ".join(scopes)
    if resource:
        query["resource"] = resource
    separator = "&" if "?" in authorization_endpoint else "?"
    return authorization_endpoint + separator + urlencode(query)


def discover_oauth_metadata(
    config: MCPServerConfig,
    *,
    http_get_json: Callable[[str], dict[str, Any]] | None = None,
    resource_metadata_url: str | None = None,
) -> OAuthMetadata:
    safe_getter = http_get_json or _get_safe_oauth_metadata_json
    urls = build_oauth_discovery_urls(config)
    if resource_metadata_url:
        urls = _dedupe([resource_metadata_url, *urls])
    for url in urls:
        require_public_endpoints = True
        resolve_endpoint_hosts = http_get_json is None
        if not _is_safe_discovered_oauth_endpoint(url, resolve_host=resolve_endpoint_hosts):
            continue
        try:
            data = safe_getter(url)
        except Exception:
            continue
        metadata = _oauth_metadata_from_mapping(
            data,
            metadata_url=url,
            require_public_endpoints=require_public_endpoints,
            resolve_endpoint_hosts=resolve_endpoint_hosts,
        )
        if metadata is not None:
            return metadata
        authorization_servers = data.get("authorization_servers")
        if isinstance(authorization_servers, list):
            resource = data.get("resource")
            for auth_server in authorization_servers:
                if not isinstance(auth_server, str):
                    continue
                if not _is_safe_discovered_oauth_endpoint(
                    auth_server,
                    resolve_host=resolve_endpoint_hosts,
                ):
                    continue
                metadata_url = auth_server.rstrip("/") + "/.well-known/oauth-authorization-server"
                try:
                    auth_data = safe_getter(metadata_url)
                except Exception:
                    continue
                metadata = _oauth_metadata_from_mapping(
                    auth_data,
                    metadata_url=metadata_url,
                    resource=resource if isinstance(resource, str) else None,
                    issuer_fallback=auth_server,
                    require_public_endpoints=require_public_endpoints,
                    resolve_endpoint_hosts=resolve_endpoint_hosts,
                )
                if metadata is not None:
                    return metadata

    raise RuntimeError(_("Could not discover OAuth metadata for MCP server {server!r}.").format(server=config.name))


def _oauth_metadata_from_mapping(
    data: dict[str, Any],
    *,
    metadata_url: str,
    resource: str | None = None,
    issuer_fallback: str | None = None,
    require_public_endpoints: bool = False,
    resolve_endpoint_hosts: bool = False,
) -> OAuthMetadata | None:
    authorization_endpoint = data.get("authorization_endpoint")
    token_endpoint = data.get("token_endpoint")
    if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
        return None
    scopes = data.get("scopes_supported", [])
    registration_endpoint = data.get("registration_endpoint")
    revocation_endpoint = data.get("revocation_endpoint")
    endpoint_values = [authorization_endpoint, token_endpoint]
    if isinstance(registration_endpoint, str):
        endpoint_values.append(registration_endpoint)
    if isinstance(revocation_endpoint, str):
        endpoint_values.append(revocation_endpoint)
    if require_public_endpoints and any(
        not _is_safe_discovered_oauth_endpoint(endpoint, resolve_host=resolve_endpoint_hosts)
        for endpoint in endpoint_values
    ):
        return None
    issuer = data.get("issuer")
    client_id_metadata_document_supported = data.get("client_id_metadata_document_supported")
    return OAuthMetadata(
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        scopes_supported=[str(scope) for scope in scopes] if isinstance(scopes, list) else [],
        issuer=issuer if isinstance(issuer, str) else issuer_fallback or _issuer_from_metadata_url(metadata_url),
        registration_endpoint=registration_endpoint if isinstance(registration_endpoint, str) else None,
        revocation_endpoint=revocation_endpoint if isinstance(revocation_endpoint, str) else None,
        resource=resource,
        client_id_metadata_document_supported=(
            client_id_metadata_document_supported if isinstance(client_id_metadata_document_supported, bool) else None
        ),
        requires_public_endpoints=require_public_endpoints,
    )


def _is_safe_discovered_oauth_endpoint(value: str, *, resolve_host: bool) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        port = parsed.port or 443
    except ValueError:
        return False
    normalized = host.lower().strip(".")
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(".localhost"):
        return False
    literal_address = _ip_address_or_none(normalized)
    if literal_address is not None:
        try:
            _reject_unsafe_oauth_metadata_address(literal_address)
        except RuntimeError:
            return False
        return True
    if resolve_host:
        try:
            _validate_public_oauth_metadata_host(host, port)
        except RuntimeError:
            return False
    return True


def _issuer_from_metadata_url(url: str) -> str:
    parsed = urlparse(url)
    marker = "/.well-known/oauth-authorization-server"
    if parsed.path.startswith(marker + "/"):
        issuer_path = parsed.path[len(marker) :]
        return urlunparse((parsed.scheme, parsed.netloc, issuer_path, "", "", "")).rstrip("/")
    if marker in parsed.path:
        issuer_path = parsed.path.split(marker, 1)[0]
        return urlunparse((parsed.scheme, parsed.netloc, issuer_path, "", "", "")).rstrip("/") or urlunparse(
            (parsed.scheme, parsed.netloc, "", "", "", "")
        )
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def create_oauth_authorization_url(
    config: MCPServerConfig,
    *,
    redirect_uri: str,
    state: str | None = None,
    code_verifier: str | None = None,
    metadata: OAuthMetadata | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, str, str]:
    state_value = state or secrets.token_urlsafe(24)
    verifier = code_verifier or secrets.token_urlsafe(48)
    metadata_value = metadata or discover_oauth_metadata(config)
    url = build_authorization_url(
        config,
        authorization_endpoint=metadata_value.authorization_endpoint,
        redirect_uri=redirect_uri,
        state=state_value,
        code_challenge=_code_challenge(verifier),
        scopes=_selected_oauth_scopes(metadata_value, required_scopes=required_scopes),
        resource=_oauth_resource_url(config, metadata_value),
    )
    return url, state_value, verifier


def get_oauth_client_information(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
):
    from mcp.shared.auth import OAuthClientInformationFull

    client_id = config.oauth.client_id if config.oauth and config.oauth.client_id else None
    client_secret = None
    auth_method = None
    if client_id:
        client_secret = _client_secret_payload(config, storage, scope=scope).get("client_secret")
        auth_method = "client_secret_post" if client_secret else "none"
    else:
        client_id = get_oauth_storage_secret(config, storage, "client_id", scope=scope)
        client_secret = get_oauth_storage_secret(config, storage, "client_secret", scope=scope)
        auth_method = get_oauth_storage_secret(config, storage, "client_auth_method", scope=scope) or "none"
    if not client_id:
        return None
    return OAuthClientInformationFull.model_validate(
        {
            "redirect_uris": None,
            "token_endpoint_auth_method": _validated_client_auth_method(auth_method),
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )


def _validated_client_auth_method(value: str | None) -> str | None:
    if value in {"none", "client_secret_post", "client_secret_basic", "private_key_jwt"}:
        return value
    return "none" if value else None


def save_oauth_client_information(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    client_id: str,
    client_secret: str | None,
) -> None:
    _save_oauth_client_information(config, storage, scope, client_id, client_secret)


def _save_oauth_client_information(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    client_id: str,
    client_secret: str | None,
) -> None:
    remember_oauth_storage_signature(config, storage=storage, scope=scope)
    set_oauth_storage_secret(config, storage, "client_id", client_id, scope=scope)
    if client_secret:
        set_oauth_storage_secret(config, storage, "client_secret", client_secret, scope=scope)
    else:
        delete_oauth_storage_secret(config, storage, "client_secret", scope=scope)


def _delete_oauth_client_information(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
) -> None:
    for kind in ("client_id", "client_secret", "client_auth_method"):
        delete_oauth_storage_secret(config, storage, kind, scope=scope)


def build_oauth_token_storage(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
) -> TokenStorage:
    return _MCPOAuthTokenStorage(config, storage, scope)


def build_oauth_client_metadata(
    config: MCPServerConfig,
    metadata: OAuthMetadata,
    *,
    redirect_uri: str | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
) -> OAuthClientMetadata:
    return _build_oauth_client_metadata(
        config,
        scopes=_selected_oauth_scopes(metadata, required_scopes=required_scopes),
        redirect_uri=redirect_uri,
    )


def _build_oauth_client_metadata(
    config: MCPServerConfig,
    *,
    scopes: list[str] | None = None,
    redirect_uri: str | None = None,
) -> OAuthClientMetadata:
    token_endpoint_auth_method = "client_secret_post" if config.oauth and config.oauth.client_secret_env else "none"
    return OAuthClientMetadata.model_validate(
        {
            "client_name": "IaC Code",
            "redirect_uris": [redirect_uri] if redirect_uri else None,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "scope": " ".join(scopes) if scopes else None,
        }
    )


def _oauth_client_metadata_url(config: MCPServerConfig, metadata: OAuthMetadata) -> str | None:
    configured_url = config.oauth.client_metadata_url if config.oauth else None
    if not configured_url:
        return None
    if not _is_valid_oauth_client_metadata_url(configured_url):
        raise RuntimeError(
            _("MCP server {server!r} oauth.clientMetadataUrl must be an HTTPS URL with a non-root pathname.").format(
                server=config.name
            )
        )
    if metadata.client_id_metadata_document_supported is not True:
        return None
    return configured_url


def _validated_configured_oauth_client_metadata_url(config: MCPServerConfig) -> str | None:
    configured_url = config.oauth.client_metadata_url if config.oauth else None
    if not configured_url:
        return None
    if not _is_valid_oauth_client_metadata_url(configured_url):
        raise RuntimeError(
            _("MCP server {server!r} oauth.clientMetadataUrl must be an HTTPS URL with a non-root pathname.").format(
                server=config.name
            )
        )
    return configured_url


def _is_valid_oauth_client_metadata_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.path not in {"", "/"}


def build_oauth_client_provider(
    server_url: str,
    client_metadata: OAuthClientMetadata,
    token_storage: TokenStorage,
    redirect_handler: Callable[[str], Awaitable[None]] | None,
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None,
    client_metadata_url: str | None = None,
    metadata_loader: Callable[[], OAuthMetadata] | None = None,
) -> OAuthClientProvider:
    import inspect

    kwargs: dict[str, Any] = {
        "server_url": server_url,
        "client_metadata": client_metadata,
        "storage": token_storage,
        "redirect_handler": redirect_handler,
        "callback_handler": callback_handler,
    }
    supports_client_metadata_url = "client_metadata_url" in inspect.signature(OAuthClientProvider).parameters
    if supports_client_metadata_url:
        kwargs["client_metadata_url"] = client_metadata_url
    elif client_metadata_url is not None:
        raise RuntimeError(_("The installed MCP SDK does not support OAuth client metadata URLs."))
    provider = _MCPResourceAwareOAuthClientProvider(**kwargs, metadata_loader=metadata_loader)
    return provider


def build_oauth_transport_auth_provider(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
) -> Any | None:
    if not config.url:
        return None
    _validated_configured_oauth_client_metadata_url(config)
    return _NonInteractiveOAuthTransportAuth(config, storage, scope)


class _NonInteractiveOAuthTransportAuth(httpx.Auth):
    """Bearer-token auth for MCP transports; challenge handling stays in the caller."""

    def __init__(
        self,
        config: MCPServerConfig,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None,
    ) -> None:
        self._config = config
        self._storage = storage
        self._scope = scope

    def auth_flow(self, request: Any):
        token = get_oauth_access_token(self._config, storage=self._storage, scope=self._scope)
        if token:
            request.headers["Authorization"] = "Bearer {}".format(token)
        yield request

    async def async_auth_flow(self, request: Any):
        token = await get_oauth_access_token_async(self._config, storage=self._storage, scope=self._scope)
        if token:
            request.headers["Authorization"] = "Bearer {}".format(token)
        yield request


def has_oauth_state(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> bool:
    for kind in ("access_token", "refresh_token", "client_id"):
        if get_oauth_storage_secret(config, storage, kind, scope=scope):
            return True
    return False


def auth_challenge_from_exception(exc: BaseException) -> MCPAuthChallenge | None:
    for candidate in _exception_tree(exc):
        challenge = _auth_challenge_from_single_exception(candidate)
        if challenge is not None:
            return challenge
    return None


def _auth_challenge_from_single_exception(exc: BaseException) -> MCPAuthChallenge | None:
    response = getattr(exc, "response", None)
    header = _www_authenticate_header(response)
    payload = _auth_payload_from_response(response)
    attrs = _auth_payload_from_exception(exc)
    status_code = _response_status_code(response)
    if status_code is None and isinstance(attrs.get("status_code"), int):
        status_code = attrs["status_code"]
    parsed = parse_www_authenticate(header, status_code=status_code)

    error = _first_text(
        parsed.error if parsed else None,
        payload.get("error"),
        attrs.get("error"),
    )
    description = _first_text(
        parsed.error_description if parsed else None,
        payload.get("error_description"),
        payload.get("message"),
        attrs.get("error_description"),
        attrs.get("description"),
    )
    required_scopes = _dedupe(
        [
            *(parsed.required_scopes if parsed else ()),
            *_scope_tokens(payload.get("scope")),
            *_scope_tokens(payload.get("required_scope")),
            *_scope_tokens(payload.get("required_scopes")),
            *_scope_tokens(attrs.get("scope")),
            *_scope_tokens(attrs.get("required_scope")),
            *_scope_tokens(attrs.get("required_scopes")),
        ]
    )
    resource_metadata_url = _first_text(
        parsed.resource_metadata_url if parsed else None,
        payload.get("resource_metadata"),
        payload.get("resource_metadata_uri"),
        attrs.get("resource_metadata"),
        attrs.get("resource_metadata_uri"),
    )
    scheme = parsed.scheme if parsed else None
    raw_www_authenticate = parsed.raw_www_authenticate if parsed else header

    if status_code not in {401, 403} and error not in {"invalid_token", "invalid_client", "insufficient_scope"}:
        return None

    return MCPAuthChallenge(
        status_code=status_code,
        scheme=scheme,
        error=error,
        error_description=description,
        required_scopes=tuple(required_scopes),
        resource_metadata_url=resource_metadata_url,
        raw_www_authenticate=raw_www_authenticate,
    )


def _exception_tree(exc: BaseException):
    stack = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        yield current

        related: list[BaseException] = []
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, list | tuple):
            related.extend(item for item in nested if isinstance(item, BaseException))
        for item in (current.__cause__, current.__context__):
            if isinstance(item, BaseException):
                related.append(item)
        stack.extend(reversed(related))


def parse_www_authenticate(value: str | None, *, status_code: int | None = None) -> MCPAuthChallenge | None:
    if not value:
        return None
    challenge = value.strip()
    if not challenge:
        return None
    scheme, separator, rest = challenge.partition(" ")
    if not separator:
        rest = ""
    params = parse_keqv_list(parse_http_list(rest)) if rest else {}
    error = _first_text(params.get("error"))
    return MCPAuthChallenge(
        status_code=status_code,
        scheme=scheme or None,
        error=error,
        error_description=_first_text(params.get("error_description")),
        required_scopes=tuple(
            _dedupe(
                [
                    *_scope_tokens(params.get("scope")),
                    *_scope_tokens(params.get("required_scope")),
                    *_scope_tokens(params.get("required_scopes")),
                ]
            )
        ),
        resource_metadata_url=_first_text(params.get("resource_metadata"), params.get("resource_metadata_uri")),
        raw_www_authenticate=challenge,
    )


def needs_auth_error_from_exception(
    server_name: str,
    exc: BaseException,
    *,
    config: MCPServerConfig | None = None,
    storage: MCPSecretStorage | None = None,
    scope: MCPConfigScope | str | None = None,
) -> MCPNeedsAuthError | None:
    for candidate in _exception_tree(exc):
        if isinstance(candidate, MCPNeedsAuthError):
            return candidate
    challenge = auth_challenge_from_exception(exc)
    if challenge is None:
        return None
    if config is not None and storage is not None:
        clear_oauth_state_for_auth_challenge(config, storage=storage, scope=scope, challenge=challenge)
    return needs_auth_error_from_challenge(server_name, challenge)


def needs_auth_error_from_challenge(server_name: str, challenge: MCPAuthChallenge) -> MCPNeedsAuthError:
    error = MCPNeedsAuthError(needs_auth_reason(server_name, challenge))
    enriched_error = cast(Any, error)
    enriched_error.auth_error = challenge.error
    enriched_error.auth_status_code = challenge.status_code
    enriched_error.required_scopes = challenge.required_scopes
    enriched_error.auth_resource_metadata_url = challenge.resource_metadata_url
    enriched_error.auth_challenge = challenge
    return error


def needs_auth_reason(server_name: str, challenge: MCPAuthChallenge | None = None) -> str:
    base = _("MCP server {server!r} requires authentication.").format(server=server_name)
    if challenge is None:
        return base
    details: list[str] = []
    if challenge.error:
        details.append(challenge.error)
    if challenge.required_scopes:
        details.append(_("required scopes: {scopes}").format(scopes=" ".join(challenge.required_scopes)))
    if challenge.error_description:
        details.append(sanitize_public_text(challenge.error_description))
    if not details:
        return base
    return "{} {}".format(base, "; ".join(details))


def clear_oauth_state_for_auth_challenge(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    challenge: MCPAuthChallenge,
) -> None:
    if challenge.error == "invalid_client":
        clear_oauth_state(config, storage=storage, scope=scope)
    elif challenge.error in {"invalid_token", "insufficient_scope"}:
        _clear_oauth_tokens(config, storage=storage, scope=scope)


def safe_oauth_resource_metadata_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except Exception:
        return None
    if parsed.scheme != "https" or not parsed.netloc or parsed.path in {"", "/"}:
        return None
    host = parsed.hostname
    if not host:
        return None
    lowered = host.lower().strip(".")
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        return None
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return value
    if not address.is_global:
        return None
    return value


def _response_status_code(response: Any) -> int | None:
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _www_authenticate_header(response: Any) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("WWW-Authenticate")
    except AttributeError:
        value = None
    if isinstance(value, str) and value:
        return value
    try:
        items = headers.items()
    except AttributeError:
        return None
    for key, item_value in items:
        if str(key).lower() == "www-authenticate" and isinstance(item_value, str) and item_value:
            return item_value
    return None


def _auth_payload_from_response(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _auth_payload_from_exception(exc: BaseException) -> dict[str, Any]:
    data: dict[str, Any] = {}
    _merge_sdk_error_payload(data, getattr(exc, "error", None))
    for source, names in {
        "error": ("error", "error_code", "oauth_error"),
        "error_description": ("error_description", "description"),
        "scope": ("scope", "scopes"),
        "required_scope": ("required_scope",),
        "required_scopes": ("required_scopes",),
        "resource_metadata": ("resource_metadata", "resource_metadata_url"),
        "resource_metadata_uri": ("resource_metadata_uri",),
    }.items():
        for name in names:
            value = getattr(exc, name, None)
            if value:
                if source == "error" and not isinstance(value, str):
                    _merge_sdk_error_payload(data, value)
                else:
                    data.setdefault(source, value)
                break
    return data


def _merge_sdk_error_payload(data: dict[str, Any], error: Any) -> None:
    if error is None:
        return
    if isinstance(error, dict):
        _merge_auth_payload_mapping(data, error)
        return
    code = getattr(error, "code", None)
    if isinstance(code, int):
        data.setdefault("status_code", code)
    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        data.setdefault("error_description", message)
    payload = getattr(error, "data", None)
    if isinstance(payload, dict):
        _merge_auth_payload_mapping(data, payload)


def _merge_auth_payload_mapping(data: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in (
        "error",
        "error_description",
        "message",
        "scope",
        "required_scope",
        "required_scopes",
        "resource_metadata",
        "resource_metadata_uri",
    ):
        value = payload.get(key)
        if value:
            data.setdefault(key, value)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _scope_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [scope for scope in value.split() if scope]
    if isinstance(value, list | tuple | set):
        return [str(scope) for scope in value if str(scope)]
    return []


class _MCPResourceAwareOAuthClientProvider(OAuthClientProvider):
    def __init__(
        self,
        *args: Any,
        metadata_loader: Callable[[], OAuthMetadata] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._metadata_loader = metadata_loader
        self._metadata_loaded = False

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)

    async def _refresh_token(self):
        await self._ensure_metadata_loaded()
        return await super()._refresh_token()

    async def _perform_authorization(self):
        await self._ensure_metadata_loaded()
        return await super()._perform_authorization()

    async def _ensure_metadata_loaded(self) -> None:
        if self._metadata_loaded or self._metadata_loader is None:
            return
        metadata = await asyncio.to_thread(self._metadata_loader)
        _seed_oauth_client_provider_context(self, metadata)
        self._metadata_loaded = True


@dataclass(frozen=True)
class _OAuthTokenStorageSnapshot:
    access_token: str
    refresh_token: str | None
    refresh_marker: str | None


class _MCPOAuthTokenStorage:
    def __init__(
        self,
        config: MCPServerConfig,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None,
    ) -> None:
        self._config = config
        self._storage = storage
        self._scope = scope
        self._token_snapshot: _OAuthTokenStorageSnapshot | None = None
        self._fresh_flow_marker: str | None = None

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        access_key = oauth_storage_key(self._config, scope=self._scope)
        access_token, refresh_token, refresh_marker, expires_at = self._read_token_state(access_key)
        if not access_token:
            self._token_snapshot = None
            self._fresh_flow_marker = self._begin_fresh_flow()
            return None
        if refresh_token and expires_at is not None and expires_at <= time.time() + 60:
            refreshed_access_token = await get_oauth_access_token_async(
                self._config,
                storage=self._storage,
                scope=self._scope,
            )
            if refreshed_access_token:
                access_token, refresh_token, refresh_marker, expires_at = self._read_token_state(access_key)
        if not access_token:
            self._token_snapshot = None
            self._fresh_flow_marker = self._begin_fresh_flow()
            return None
        self._token_snapshot = _OAuthTokenStorageSnapshot(
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_marker=refresh_marker,
        )
        self._fresh_flow_marker = None
        expires_in = None
        if expires_at is not None:
            expires_in = max(0, int(expires_at - time.time()))
        return OAuthToken(access_token=access_token, refresh_token=refresh_token, expires_in=expires_in)

    def _read_token_state(self, access_key: str) -> tuple[str | None, str | None, str | None, float | None]:
        with self._storage.lock(access_key):
            access_token = get_oauth_storage_secret(self._config, self._storage, "access_token", scope=self._scope)
            refresh_token = get_oauth_storage_secret(self._config, self._storage, "refresh_token", scope=self._scope)
            refresh_marker = get_oauth_storage_secret(
                self._config,
                self._storage,
                "refresh_marker",
                scope=self._scope,
            )
            expires_at = _parse_expires_at(
                get_oauth_storage_secret(self._config, self._storage, "expires_at", scope=self._scope)
            )
        return access_token, refresh_token, refresh_marker, expires_at

    async def set_tokens(self, tokens: Any) -> None:
        access_token = str(tokens.access_token)
        access_key = oauth_storage_key(self._config, scope=self._scope)
        expires_in = getattr(tokens, "expires_in", None)
        refresh_token = getattr(tokens, "refresh_token", None)
        with self._storage.lock(access_key):
            if not self._captured_token_state_is_current():
                return
            refresh_marker = _persist_oauth_tokens(
                self._config,
                self._storage,
                self._scope,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=expires_in,
            )
            self._token_snapshot = _OAuthTokenStorageSnapshot(
                access_token=access_token,
                refresh_token=get_oauth_storage_secret(
                    self._config,
                    self._storage,
                    "refresh_token",
                    scope=self._scope,
                ),
                refresh_marker=refresh_marker,
            )
            self._clear_fresh_flow()

    async def get_client_info(self):
        return get_oauth_client_information(self._config, self._storage, self._scope)

    async def set_client_info(self, client_info: Any) -> None:
        access_key = oauth_storage_key(self._config, scope=self._scope)
        client_id = getattr(client_info, "client_id", None)
        with self._storage.lock(access_key):
            if not self._captured_token_state_is_current():
                return
            if not client_id:
                _delete_oauth_client_information(self._config, self._storage, self._scope)
                return
            if self._token_snapshot is None and self._fresh_flow_marker is None:
                self._fresh_flow_marker = self._begin_fresh_flow()
            _save_oauth_client_information(
                self._config,
                self._storage,
                self._scope,
                str(client_id),
                getattr(client_info, "client_secret", None),
            )
            auth_method = getattr(client_info, "token_endpoint_auth_method", None)
            if auth_method:
                set_oauth_storage_secret(
                    self._config,
                    self._storage,
                    "client_auth_method",
                    str(auth_method),
                    scope=self._scope,
                )
            else:
                delete_oauth_storage_secret(self._config, self._storage, "client_auth_method", scope=self._scope)

    def _captured_token_state_is_current(self) -> bool:
        snapshot = self._token_snapshot
        if snapshot is None:
            if self._fresh_flow_marker is None:
                return True
            return (
                get_oauth_storage_secret(self._config, self._storage, "auth_flow_marker", scope=self._scope)
                == self._fresh_flow_marker
            )
        access_token = get_oauth_storage_secret(self._config, self._storage, "access_token", scope=self._scope)
        refresh_token = get_oauth_storage_secret(self._config, self._storage, "refresh_token", scope=self._scope)
        refresh_marker = get_oauth_storage_secret(self._config, self._storage, "refresh_marker", scope=self._scope)
        return (
            access_token == snapshot.access_token
            and refresh_token == snapshot.refresh_token
            and refresh_marker == snapshot.refresh_marker
        )

    def _begin_fresh_flow(self) -> str:
        marker = secrets.token_urlsafe(16)
        remember_oauth_storage_signature(self._config, storage=self._storage, scope=self._scope)
        set_oauth_storage_secret(self._config, self._storage, "auth_flow_marker", marker, scope=self._scope)
        return marker

    def _clear_fresh_flow(self) -> None:
        delete_oauth_storage_secret(self._config, self._storage, "auth_flow_marker", scope=self._scope)
        self._fresh_flow_marker = None


class _NoopOAuthTokenStorage:
    async def get_tokens(self):
        return None

    async def set_tokens(self, tokens: Any) -> None:
        _ = tokens

    async def get_client_info(self):
        return None

    async def set_client_info(self, client_info: Any) -> None:
        _ = client_info


_NOOP_OAUTH_TOKEN_STORAGE = _NoopOAuthTokenStorage()


@dataclass(frozen=True)
class _ProtectedResourceMetadataContext:
    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str] | None = None


def _oauth_resource_url(config: MCPServerConfig, metadata: OAuthMetadata) -> str | None:
    if not config.url:
        return None
    client_metadata = build_oauth_client_metadata(config, metadata)
    provider = build_oauth_client_provider(
        config.url,
        client_metadata,
        _NOOP_OAUTH_TOKEN_STORAGE,
        redirect_handler=None,
        callback_handler=None,
        client_metadata_url=None,
    )
    _seed_oauth_client_provider_context(
        provider,
        metadata,
        selected_scopes=_scope_tokens(client_metadata.scope),
    )
    if not metadata.resource:
        cast(Any, provider.context).protected_resource_metadata = _configured_resource_metadata(config, metadata)
    if not provider.context.should_include_resource_param(None):
        return None
    return provider.context.get_resource_url()


def _configured_resource_metadata(
    config: MCPServerConfig,
    metadata: OAuthMetadata,
) -> _ProtectedResourceMetadataContext:
    assert config.url is not None
    authorization_server = metadata.issuer or _issuer_from_metadata_url(metadata.authorization_endpoint)
    return _ProtectedResourceMetadataContext(
        resource=resource_url_from_server_url(config.url),
        authorization_servers=[authorization_server],
        scopes_supported=None,
    )


def _seed_oauth_client_provider_context(
    provider: OAuthClientProvider,
    metadata: OAuthMetadata,
    *,
    selected_scopes: list[str] | tuple[str, ...] | None = None,
) -> None:
    scopes = list(selected_scopes or [])
    try:
        provider.context.oauth_metadata = _sdk_oauth_metadata(metadata, selected_scopes=scopes)
    except Exception:
        provider.context.oauth_metadata = None
    if metadata.issuer:
        provider.context.auth_server_url = metadata.issuer
    if metadata.resource:
        protected_resource_metadata = _sdk_protected_resource_metadata(metadata, selected_scopes=scopes)
        if protected_resource_metadata is not None:
            cast(Any, provider.context).protected_resource_metadata = protected_resource_metadata
            provider.context.auth_server_url = str(protected_resource_metadata.authorization_servers[0])


def _sdk_oauth_metadata(
    metadata: OAuthMetadata,
    *,
    selected_scopes: list[str] | tuple[str, ...] | None = None,
) -> SDKOAuthMetadata:
    return SDKOAuthMetadata.model_validate(
        {
            "issuer": metadata.issuer or _issuer_from_metadata_url(metadata.authorization_endpoint),
            "authorization_endpoint": metadata.authorization_endpoint,
            "token_endpoint": metadata.token_endpoint,
            "registration_endpoint": metadata.registration_endpoint,
            "scopes_supported": list(selected_scopes or []) or None,
            "client_id_metadata_document_supported": metadata.client_id_metadata_document_supported,
        }
    )


def _sdk_protected_resource_metadata(
    metadata: OAuthMetadata,
    *,
    selected_scopes: list[str] | tuple[str, ...] | None = None,
) -> _ProtectedResourceMetadataContext | None:
    if not metadata.resource:
        return None
    authorization_server = metadata.issuer or _issuer_from_metadata_url(metadata.authorization_endpoint)
    return _ProtectedResourceMetadataContext(
        resource=metadata.resource,
        authorization_servers=[authorization_server],
        scopes_supported=list(selected_scopes or []) or None,
    )


def run_oauth_loopback_flow(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
    open_browser: Callable[[str], bool] | None = None,
    timeout_seconds: float = 120.0,
) -> OAuthFlowResult:
    return start_oauth_loopback_flow(
        config,
        storage=storage,
        scope=scope,
        required_scopes=required_scopes,
        open_browser=open_browser,
        timeout_seconds=timeout_seconds,
    ).wait()


def start_oauth_loopback_flow(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
    resource_metadata_url: str | None = None,
    open_browser: Callable[[str], bool] | None = None,
    timeout_seconds: float = 120.0,
) -> OAuthPendingFlow:
    metadata = discover_oauth_metadata(config, resource_metadata_url=resource_metadata_url)
    callback_port = config.oauth.callback_port if config.oauth and config.oauth.callback_port else 0
    callback = _LoopbackCallback(callback_port)
    redirect_uri = callback.redirect_uri
    if not (config.oauth and config.oauth.client_id):
        return _start_dynamic_oauth_loopback_flow(
            config,
            storage=storage,
            scope=scope,
            metadata=metadata,
            callback=callback,
            redirect_uri=redirect_uri,
            required_scopes=required_scopes,
            open_browser=open_browser,
            timeout_seconds=timeout_seconds,
        )
    try:
        auth_url, state, verifier = create_oauth_authorization_url(
            config,
            redirect_uri=redirect_uri,
            metadata=metadata,
            required_scopes=required_scopes,
        )
        callback.expected_state = state
        auth_flow_marker = _begin_oauth_auth_flow_marker(config, storage, scope)
        try:
            opener = open_browser or _open_browser
            browser_opened = bool(opener(auth_url))
        except Exception:
            browser_opened = False
        return OAuthPendingFlow(
            config=config,
            storage=storage,
            metadata=metadata,
            callback=callback,
            redirect_uri=redirect_uri,
            authorization_url=auth_url,
            verifier=verifier,
            scope=scope,
            timeout_seconds=timeout_seconds,
            browser_opened=browser_opened,
            auth_flow_marker=auth_flow_marker,
        )
    except BaseException:
        callback.close()
        raise


def _start_dynamic_oauth_loopback_flow(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    metadata: OAuthMetadata,
    callback: "_LoopbackCallback",
    redirect_uri: str,
    required_scopes: list[str] | tuple[str, ...] | None,
    open_browser: Callable[[str], bool] | None,
    timeout_seconds: float,
) -> OAuthPendingFlow:
    if not config.url:
        callback.close()
        raise RuntimeError(_("OAuth dynamic client registration requires a remote MCP server URL."))
    token_storage = build_oauth_token_storage(config, storage, scope)
    client_metadata = build_oauth_client_metadata(
        config,
        metadata,
        redirect_uri=redirect_uri,
        required_scopes=required_scopes,
    )
    future: concurrent.futures.Future[OAuthFlowResult] = concurrent.futures.Future()
    redirect_ready = threading.Event()
    redirect_state: dict[str, str | bool] = {}

    async def redirect_handler(url: str) -> None:
        opener = open_browser or _open_browser
        redirect_state["authorization_url"] = url
        try:
            redirect_state["browser_opened"] = bool(await asyncio.to_thread(opener, url))
        except Exception:
            redirect_state["browser_opened"] = False
        finally:
            redirect_ready.set()

    async def callback_handler() -> tuple[str, str | None]:
        return await asyncio.to_thread(callback.wait_for_code_and_state, timeout_seconds)

    def worker() -> None:
        try:
            result = asyncio.run(
                _run_sdk_oauth_flow(
                    config,
                    metadata=metadata,
                    client_metadata=client_metadata,
                    token_storage=token_storage,
                    redirect_handler=redirect_handler,
                    callback_handler=callback_handler,
                )
            )
            if redirect_state.get("authorization_url"):
                result = OAuthFlowResult(
                    authorization_url=str(redirect_state["authorization_url"]),
                    access_token_key=result.access_token_key,
                    refresh_token_key=result.refresh_token_key,
                )
        except BaseException as exc:
            future.set_exception(exc)
            redirect_ready.set()
        else:
            future.set_result(result)
        finally:
            callback.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    if not redirect_ready.wait(timeout_seconds):
        callback.close()
        raise TimeoutError(_("Timed out waiting for MCP OAuth authorization URL."))
    if future.done() and "authorization_url" not in redirect_state:
        future.result()
    authorization_url = str(redirect_state.get("authorization_url") or "")
    if not authorization_url:
        callback.close()
        raise RuntimeError(_("OAuth provider did not produce an authorization URL."))
    return OAuthPendingFlow(
        config=config,
        storage=storage,
        metadata=metadata,
        callback=callback,
        redirect_uri=redirect_uri,
        authorization_url=authorization_url,
        verifier="",
        scope=scope,
        timeout_seconds=timeout_seconds,
        browser_opened=bool(redirect_state.get("browser_opened")),
        result_future=future,
    )


async def _run_sdk_oauth_flow(
    config: MCPServerConfig,
    *,
    metadata: OAuthMetadata,
    client_metadata: OAuthClientMetadata,
    token_storage: TokenStorage,
    redirect_handler: Callable[[str], Awaitable[None]],
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]],
) -> OAuthFlowResult:
    import httpx

    assert config.url is not None
    provider = build_oauth_client_provider(
        config.url,
        client_metadata,
        token_storage,
        redirect_handler,
        callback_handler,
        client_metadata_url=_oauth_client_metadata_url(config, metadata),
    )
    _seed_oauth_client_provider_context(provider, metadata)
    if not metadata.resource:
        cast(Any, provider.context).protected_resource_metadata = _configured_resource_metadata(config, metadata)
    request = httpx.Request("GET", config.url)
    flow = provider.async_auth_flow(request)
    yielded_request = await flow.__anext__()
    response = httpx.Response(401, request=yielded_request, headers={"WWW-Authenticate": "Bearer"})
    while True:
        try:
            yielded_request = await flow.asend(response)
        except StopAsyncIteration:
            break
        response = await _execute_sdk_oauth_request(
            yielded_request,
            config=config,
            metadata=metadata,
            client_metadata=client_metadata,
        )
    scope = getattr(token_storage, "_scope", None)
    refresh_key = oauth_storage_key(config, scope=scope)
    secret_storage = getattr(token_storage, "_storage", None)
    if secret_storage is not None and not get_oauth_storage_secret(
        config, secret_storage, "refresh_token", scope=scope
    ):
        refresh_key = None
    return OAuthFlowResult(
        authorization_url="",
        access_token_key=oauth_storage_key(config, scope=scope),
        refresh_token_key=refresh_key,
    )


async def _execute_sdk_oauth_request(
    request: Any,
    *,
    config: MCPServerConfig,
    metadata: OAuthMetadata,
    client_metadata: OAuthClientMetadata,
):
    import httpx

    url = str(request.url)
    path = urlparse(url).path
    if request.method == "GET" and "oauth-protected-resource" in path:
        return httpx.Response(404, request=request)
    if request.method == "GET" and ("oauth-authorization-server" in path or "openid-configuration" in path):
        return _httpx_json_response(
            _sdk_oauth_metadata_payload(metadata, selected_scope=client_metadata.scope),
            request=request,
        )
    if request.method == "GET" and config.url and url == config.url:
        return httpx.Response(200, request=request, content=b"{}")

    if metadata.requires_public_endpoints:
        _validate_public_oauth_endpoint_url(url)
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
        response = await client.send(request)
        content = await response.aread()
    if metadata.requires_public_endpoints:
        _validate_httpx_response_peer(response)
    if request.method == "POST" and _is_registration_url(url, metadata) and response.status_code in {200, 201}:
        content = _augment_registration_response(content, client_metadata)
    return httpx.Response(
        response.status_code,
        request=request,
        headers=response.headers,
        content=content,
    )


def _httpx_json_response(payload: dict[str, Any], *, request: Any):
    import httpx

    return httpx.Response(
        200,
        request=request,
        headers={"Content-Type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
    )


def _sdk_oauth_metadata_payload(metadata: OAuthMetadata, *, selected_scope: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "issuer": metadata.issuer or _issuer_from_metadata_url(metadata.authorization_endpoint),
        "authorization_endpoint": metadata.authorization_endpoint,
        "token_endpoint": metadata.token_endpoint,
    }
    if metadata.registration_endpoint:
        payload["registration_endpoint"] = metadata.registration_endpoint
    if selected_scope:
        payload["scopes_supported"] = selected_scope.split()
    if metadata.client_id_metadata_document_supported is not None:
        payload["client_id_metadata_document_supported"] = metadata.client_id_metadata_document_supported
    return payload


def _is_registration_url(url: str, metadata: OAuthMetadata) -> bool:
    if metadata.registration_endpoint and url == metadata.registration_endpoint:
        return True
    return urlparse(url).path.rstrip("/").endswith("/register")


def _augment_registration_response(content: bytes, client_metadata: OAuthClientMetadata) -> bytes:
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception:
        return content
    if not isinstance(data, dict):
        return content
    if "client_id" not in data:
        return content
    defaults = client_metadata.model_dump(by_alias=True, mode="json", exclude_none=True)
    return json.dumps({**defaults, **data}).encode("utf-8")


def _exchange_authorization_code(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    metadata: OAuthMetadata,
    redirect_uri: str,
    verifier: str,
    code: str,
    authorization_url: str,
    auth_flow_marker: str | None = None,
) -> OAuthFlowResult:
    client_info = get_oauth_client_information(config, storage, scope)
    client_id = client_info.client_id if client_info and client_info.client_id else ""
    client_secret_payload, auth_headers = _client_auth_for_token_request(
        config,
        storage,
        scope=scope,
        client_id=client_id,
    )
    token_request = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        **client_secret_payload,
    }
    resource = _oauth_resource_url(config, metadata)
    if resource:
        token_request["resource"] = resource
    token_response = _post_token_request(
        metadata.token_endpoint,
        token_request,
        headers=auth_headers,
        validate_public_endpoint=metadata.requires_public_endpoints,
    )
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError(
            _("OAuth token response for MCP server {server!r} did not include an access token.").format(
                server=config.name
            )
        )

    access_key = oauth_storage_key(config, scope=scope)
    refresh_key = None
    refresh_token_to_store = None
    refresh_token = token_response.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        refresh_key = oauth_storage_key(config, scope=scope)
        refresh_token_to_store = refresh_token
    expires_in = token_response.get("expires_in")
    with storage.lock(access_key):
        if auth_flow_marker is not None and not _oauth_auth_flow_marker_is_current(
            config,
            storage,
            scope,
            auth_flow_marker,
        ):
            return OAuthFlowResult(
                authorization_url=authorization_url,
                access_token_key=access_key,
                refresh_token_key=refresh_key,
            )
        remember_oauth_storage_signature(config, storage=storage, scope=scope)
        set_oauth_storage_secret(config, storage, "access_token", access_token, scope=scope)
        if refresh_key is not None:
            assert refresh_token_to_store is not None
            set_oauth_storage_secret(config, storage, "refresh_token", refresh_token_to_store, scope=scope)
        if isinstance(expires_in, int | float):
            set_oauth_storage_secret(
                config,
                storage,
                "expires_at",
                str(time.time() + float(expires_in)),
                scope=scope,
            )
        if auth_flow_marker is not None:
            delete_oauth_storage_secret(config, storage, "auth_flow_marker", scope=scope)

    return OAuthFlowResult(
        authorization_url=authorization_url,
        access_token_key=access_key,
        refresh_token_key=refresh_key,
    )


def get_oauth_access_token(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    now: Callable[[], float] | None = None,
    refresh_margin_seconds: float = 60.0,
) -> str | None:
    access_token = get_oauth_storage_secret(config, storage, "access_token", scope=scope)
    if not access_token:
        return None

    expires_at = _parse_expires_at(get_oauth_storage_secret(config, storage, "expires_at", scope=scope))
    refresh_token = get_oauth_storage_secret(config, storage, "refresh_token", scope=scope)
    refresh_marker = get_oauth_storage_secret(config, storage, "refresh_marker", scope=scope)
    clock = now or time.time
    if refresh_token and expires_at is not None and expires_at <= clock() + refresh_margin_seconds:
        return _refresh_oauth_access_token_with_lock(
            config,
            storage=storage,
            scope=scope,
            refresh_token=refresh_token,
            refresh_marker=refresh_marker,
            now=clock,
            refresh_margin_seconds=refresh_margin_seconds,
        )
    # Another storage instance may have refreshed between the blob reads above.
    return get_oauth_storage_secret(config, storage, "access_token", scope=scope)


async def get_oauth_access_token_async(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    now: Callable[[], float] | None = None,
    refresh_margin_seconds: float = 60.0,
    refresh_coordinator: TokenRefreshCoordinator | None = None,
) -> str | None:
    access_key = oauth_storage_key(config, scope=scope)
    access_token = get_oauth_storage_secret(config, storage, "access_token", scope=scope)
    if not access_token:
        return None

    expires_at = _parse_expires_at(get_oauth_storage_secret(config, storage, "expires_at", scope=scope))
    refresh_token = get_oauth_storage_secret(config, storage, "refresh_token", scope=scope)
    refresh_marker = get_oauth_storage_secret(config, storage, "refresh_marker", scope=scope)
    clock = now or time.time
    if not refresh_token or expires_at is None or expires_at > clock() + refresh_margin_seconds:
        # Another storage instance may have refreshed between the blob reads above.
        return get_oauth_storage_secret(config, storage, "access_token", scope=scope)

    coordinator = refresh_coordinator or _DEFAULT_REFRESH_COORDINATOR

    async def refresh() -> str | None:
        return await asyncio.to_thread(
            _refresh_oauth_access_token_with_lock,
            config,
            storage=storage,
            scope=scope,
            refresh_token=refresh_token,
            refresh_marker=refresh_marker,
            now=clock,
            refresh_margin_seconds=refresh_margin_seconds,
        )

    return await coordinator.refresh(access_key, refresh)


def _refresh_oauth_access_token_with_lock(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    refresh_token: str,
    refresh_marker: str | None,
    now: Callable[[], float],
    refresh_margin_seconds: float,
) -> str | None:
    access_key = oauth_storage_key(config, scope=scope)
    with storage.lock(access_key):
        access_token = get_oauth_storage_secret(config, storage, "access_token", scope=scope)
        stored_refresh_token = get_oauth_storage_secret(config, storage, "refresh_token", scope=scope)
        if not stored_refresh_token or stored_refresh_token != refresh_token:
            return access_token
        expires_at = _parse_expires_at(get_oauth_storage_secret(config, storage, "expires_at", scope=scope))
        if access_token and expires_at is not None and expires_at > now() + refresh_margin_seconds:
            return access_token
        current_refresh_marker = get_oauth_storage_secret(config, storage, "refresh_marker", scope=scope)
        if access_token and current_refresh_marker is not None and current_refresh_marker != refresh_marker:
            return access_token
        return refresh_oauth_access_token(
            config,
            storage=storage,
            scope=scope,
            refresh_token=refresh_token,
            _cleanup_lock_held=True,
        )


def refresh_oauth_access_token(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    refresh_token: str | None = None,
    _cleanup_lock_held: bool = False,
) -> str:
    token = refresh_token or get_oauth_storage_secret(config, storage, "refresh_token", scope=scope)
    if not token:
        raise RuntimeError(_("No refresh token is available for MCP server {server!r}.").format(server=config.name))
    metadata = discover_oauth_metadata(config)
    client_info = get_oauth_client_information(config, storage, scope)
    client_id = client_info.client_id if client_info and client_info.client_id else ""
    client_secret_payload, auth_headers = _client_auth_for_token_request(
        config,
        storage,
        scope=scope,
        client_id=client_id,
    )
    token_request = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": client_id,
        **client_secret_payload,
    }
    resource = _oauth_resource_url(config, metadata)
    if resource:
        token_request["resource"] = resource
    try:
        token_response = _post_token_request(
            metadata.token_endpoint,
            token_request,
            headers=auth_headers,
            validate_public_endpoint=metadata.requires_public_endpoints,
        )
    except Exception as exc:
        reauth_error = _reauth_error_code(exc)
        if reauth_error:
            _clear_oauth_state_after_refresh_error(
                config,
                storage=storage,
                scope=scope,
                lock_held=_cleanup_lock_held,
                auth_error=reauth_error,
            )
            needs_auth_error = MCPNeedsAuthError(
                _("MCP server {server!r} requires authentication: {error}").format(
                    server=config.name,
                    error=reauth_error,
                )
            )
            enriched_error = cast(Any, needs_auth_error)
            enriched_error.auth_error = reauth_error
            if isinstance(exc, OAuthTokenError):
                enriched_error.auth_status_code = exc.status_code
            raise needs_auth_error from exc
        raise
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError(
            _("OAuth refresh response for MCP server {server!r} did not include an access token.").format(
                server=config.name
            )
        )
    if _cleanup_lock_held:
        _persist_oauth_tokens(
            config,
            storage,
            scope,
            access_token=access_token,
            refresh_token=token_response.get("refresh_token"),
            expires_in=token_response.get("expires_in"),
        )
        return access_token
    access_key = oauth_storage_key(config, scope=scope)
    with storage.lock(access_key):
        stored_refresh_token = get_oauth_storage_secret(config, storage, "refresh_token", scope=scope)
        if stored_refresh_token != token:
            return access_token
        _persist_oauth_tokens(
            config,
            storage,
            scope,
            access_token=access_token,
            refresh_token=token_response.get("refresh_token"),
            expires_in=token_response.get("expires_in"),
        )
    return access_token


def _persist_oauth_tokens(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    *,
    access_token: str,
    refresh_token: Any,
    expires_in: Any,
) -> str:
    remember_oauth_storage_signature(config, storage=storage, scope=scope)
    set_oauth_storage_secret(config, storage, "access_token", access_token, scope=scope)
    if isinstance(refresh_token, str) and refresh_token:
        set_oauth_storage_secret(config, storage, "refresh_token", refresh_token, scope=scope)
    if isinstance(expires_in, int | float):
        set_oauth_storage_secret(config, storage, "expires_at", str(time.time() + float(expires_in)), scope=scope)
    else:
        delete_oauth_storage_secret(config, storage, "expires_at", scope=scope)
    refresh_marker = secrets.token_urlsafe(16)
    set_oauth_storage_secret(config, storage, "refresh_marker", refresh_marker, scope=scope)
    return refresh_marker


def _begin_oauth_auth_flow_marker(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
) -> str:
    access_key = oauth_storage_key(config, scope=scope)
    marker = secrets.token_urlsafe(16)
    with storage.lock(access_key):
        remember_oauth_storage_signature(config, storage=storage, scope=scope)
        set_oauth_storage_secret(config, storage, "auth_flow_marker", marker, scope=scope)
    return marker


def _oauth_auth_flow_marker_is_current(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    marker: str,
) -> bool:
    return get_oauth_storage_secret(config, storage, "auth_flow_marker", scope=scope) == marker


def oauth_storage_key(config: MCPServerConfig, *, scope: MCPConfigScope | str | None = None) -> str:
    # 一个 MCP 的完整 OAuth 状态存进单个加密 JSON blob，而不是按字段拆成多条。
    return _oauth_storage_key_for_signature(config.name, config.content_signature(), scope=scope)


def get_oauth_storage_secret(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    kind: str,
    *,
    scope: MCPConfigScope | str | None = None,
) -> str | None:
    # 存储层以文件锁保证单次读取返回完整 blob，不会读到半写状态。
    blob = _read_oauth_blob(storage, oauth_storage_key(config, scope=scope))
    return blob.get(kind)


def set_oauth_storage_secret(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    kind: str,
    value: str,
    *,
    scope: MCPConfigScope | str | None = None,
) -> None:
    blob_key = oauth_storage_key(config, scope=scope)
    with storage.lock(_oauth_blob_rmw_lock_key(blob_key)):
        blob = _read_oauth_blob(storage, blob_key)
        blob[kind] = value
        _write_oauth_blob(storage, blob_key, blob)


def delete_oauth_storage_secret(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    kind: str,
    *,
    scope: MCPConfigScope | str | None = None,
) -> None:
    blob_key = oauth_storage_key(config, scope=scope)
    with storage.lock(_oauth_blob_rmw_lock_key(blob_key)):
        blob = _read_oauth_blob(storage, blob_key)
        if kind in blob:
            del blob[kind]
            _write_oauth_blob(storage, blob_key, blob)


def _read_oauth_blob(storage: MCPSecretStorage, blob_key: str) -> dict[str, str]:
    raw = storage.get_secret(blob_key)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items() if isinstance(key, str) and isinstance(value, str)}


def _write_oauth_blob(storage: MCPSecretStorage, blob_key: str, blob: dict[str, str]) -> None:
    if not blob:
        storage.delete_secret(blob_key)
        return
    storage.set_secret(blob_key, json.dumps(blob, ensure_ascii=False, sort_keys=True))


def _oauth_blob_rmw_lock_key(blob_key: str) -> str:
    # 读改写用独立锁名(与粗粒度 storage.lock(blob_key) 区分),保证单字段写入原子、不丢更新,
    # 且不会与调用方持有的 CAS 粗粒度锁在同进程内自锁(文件锁不可重入)。
    return "{}:rmw".format(blob_key)


def _oauth_storage_key_for_signature(
    name: str,
    content_signature: str,
    *,
    scope: MCPConfigScope | str | None = None,
) -> str:
    material = "\0".join([_normalized_server_name(name), _scope_value(scope), content_signature])
    digest = _derive_oauth_storage_digest(material, salt=_OAUTH_STORAGE_KEY_SALT)
    return "mcp:oauth:{}".format(digest)


def remember_oauth_storage_signature(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    index_key = oauth_storage_signature_index_key(config.name, scope=scope)
    signature = config.content_signature()
    with storage.lock(index_key):
        signatures = oauth_storage_signatures(config.name, storage=storage, scope=scope)
        if signature not in signatures:
            signatures.append(signature)
        storage.set_secret(index_key, json.dumps(signatures, ensure_ascii=False, sort_keys=True))
        for legacy_key in _oauth_storage_signature_index_keys(config.name, scope=scope)[1:]:
            storage.delete_secret(legacy_key)


def oauth_storage_signatures(
    name: str,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> list[str]:
    raw = None
    for key in _oauth_storage_signature_index_keys(name, scope=scope):
        raw = storage.get_secret(key)
        if raw:
            break
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item]


def clear_oauth_state_for_signatures(
    name: str,
    signatures: list[str],
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    for signature in dict.fromkeys(signatures):
        blob_key = _oauth_storage_key_for_signature(name, signature, scope=scope)
        with storage.lock(_oauth_blob_rmw_lock_key(blob_key)):
            storage.delete_secret(blob_key)


def clear_oauth_storage_signature_index(
    name: str,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    for key in _oauth_storage_signature_index_keys(name, scope=scope):
        storage.delete_secret(key)


def oauth_storage_signature_index_key(name: str, *, scope: MCPConfigScope | str | None = None) -> str:
    material = "\0".join([_normalized_server_name(name), _scope_value(scope), "oauth-signature-index"])
    digest = _derive_oauth_storage_digest(material, salt=_OAUTH_SIGNATURE_INDEX_SALT)
    return "mcp:oauth_signatures:{}".format(digest)


def _oauth_storage_signature_index_keys(
    name: str,
    *,
    scope: MCPConfigScope | str | None = None,
) -> tuple[str, ...]:
    return (oauth_storage_signature_index_key(name, scope=scope),)


@lru_cache(maxsize=8192)
def _derive_oauth_storage_digest(material: str, *, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        material.encode("utf-8"),
        salt,
        _OAUTH_KEY_DERIVATION_ITERATIONS,
    ).hex()


def oauth_scope_identity(
    scope: MCPConfigScope | str | None,
    *,
    source_path: str | Path | None = None,
    session_id: str | None = None,
) -> MCPConfigScope | str | None:
    if scope is None:
        return None
    parsed_scope = scope if isinstance(scope, MCPConfigScope) else None
    if parsed_scope is None:
        try:
            parsed_scope = MCPConfigScope(str(scope))
        except ValueError:
            return scope
    if parsed_scope is MCPConfigScope.USER:
        return parsed_scope
    if parsed_scope in {MCPConfigScope.SESSION, MCPConfigScope.DYNAMIC}:
        if session_id is None:
            return parsed_scope
        return "{}:{}".format(parsed_scope.value, session_id)
    if source_path is not None:
        return "{}:{}".format(parsed_scope.value, Path(source_path).expanduser().as_posix())
    return parsed_scope


def clear_oauth_state(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    revoke: Callable[[str], None] | None = None,
) -> None:
    access_token = get_oauth_storage_secret(config, storage, "access_token", scope=scope)
    if access_token and revoke is not None:
        try:
            revoke(access_token)
        except Exception:
            pass
    clear_scoped_oauth_storage(config, storage=storage, scope=scope)


def revoke_oauth_stored_tokens(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> list[str]:
    tokens: list[tuple[str, str]] = []
    access_token = get_oauth_storage_secret(config, storage, "access_token", scope=scope)
    refresh_token = get_oauth_storage_secret(config, storage, "refresh_token", scope=scope)
    if access_token:
        tokens.append((access_token, "access_token"))
    if refresh_token and refresh_token != access_token:
        tokens.append((refresh_token, "refresh_token"))
    if not tokens:
        return []
    try:
        metadata = discover_oauth_metadata(config)
    except Exception as exc:
        return [_oauth_revocation_warning(config, exc)]
    if not metadata.revocation_endpoint:
        return []
    client_info = get_oauth_client_information(config, storage, scope)
    client_id = client_info.client_id if client_info and client_info.client_id else ""
    client_secret_payload, auth_headers = _client_auth_for_token_request(
        config,
        storage,
        scope=scope,
        client_id=client_id,
    )
    warnings: list[str] = []
    for token, token_type_hint in tokens:
        request_data = {
            "token": token,
            "token_type_hint": token_type_hint,
        }
        if client_id:
            request_data["client_id"] = client_id
        request_data.update(client_secret_payload)
        try:
            _post_revocation_request(
                metadata.revocation_endpoint,
                request_data,
                headers=auth_headers,
                validate_public_endpoint=metadata.requires_public_endpoints,
            )
        except Exception as exc:
            warnings.append(_oauth_revocation_warning(config, exc))
    return _dedupe(warnings)


def _oauth_revocation_warning(config: MCPServerConfig, exc: BaseException) -> str:
    detail = sanitize_public_text(str(exc) or exc.__class__.__name__)
    return _("OAuth token revocation failed for MCP server {server!r}: {detail}").format(
        server=config.name,
        detail=detail,
    )


def clear_scoped_oauth_storage(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    access_key = oauth_storage_key(config, scope=scope)
    with storage.lock(access_key):
        _delete_scoped_oauth_storage(config, storage=storage, scope=scope)


def _delete_scoped_oauth_storage(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    for kind in _OAUTH_STORAGE_KINDS:
        delete_oauth_storage_secret(config, storage, kind, scope=scope)


def _clear_oauth_tokens(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    access_key = oauth_storage_key(config, scope=scope)
    with storage.lock(access_key):
        _delete_oauth_tokens(config, storage=storage, scope=scope)


def _delete_oauth_tokens(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    for kind in ("access_token", "refresh_token", "expires_at", "refresh_marker", "auth_flow_marker"):
        delete_oauth_storage_secret(config, storage, kind, scope=scope)


def _clear_oauth_state_after_refresh_error(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    lock_held: bool,
    auth_error: str,
) -> None:
    if auth_error != "invalid_client":
        if lock_held:
            _delete_oauth_tokens(config, storage=storage, scope=scope)
        else:
            _clear_oauth_tokens(config, storage=storage, scope=scope)
        return
    if lock_held:
        _delete_scoped_oauth_storage(config, storage=storage, scope=scope)
    else:
        clear_scoped_oauth_storage(config, storage=storage, scope=scope)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _manual_oauth_code_and_state(callback_or_code: str) -> tuple[str, str | None]:
    value = callback_or_code.strip()
    if not value:
        raise RuntimeError(_("OAuth manual callback input was empty."))
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        query = parse_qs(parsed.query)
        if "error" in query:
            raise RuntimeError(query.get("error_description", query["error"])[0])
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0] or None
    else:
        code = value
        state = None
    if not code:
        raise RuntimeError(_("OAuth callback did not include a code."))
    return code, state


def _oauth_state_from_url(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    return query.get("state", [""])[0] or None


class _LoopbackCallback:
    def __init__(self, port: int) -> None:
        self.expected_state: str | None = None
        self._event = threading.Event()
        self._code: str | None = None
        self._state: str | None = None
        self._error: str | None = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def redirect_uri(self) -> str:
        return "http://127.0.0.1:{}/callback".format(self._server.server_address[1])

    def wait_for_code(self, timeout_seconds: float) -> str:
        if not self._event.wait(timeout_seconds):
            raise TimeoutError(_("Timed out waiting for MCP OAuth callback."))
        if self._error:
            raise RuntimeError(self._error)
        if not self._code:
            raise RuntimeError(_("OAuth callback did not include a code."))
        return self._code

    def wait_for_code_and_state(self, timeout_seconds: float) -> tuple[str, str | None]:
        code = self.wait_for_code(timeout_seconds)
        return code, self._state

    def complete_manually(self, code: str, state: str | None = None) -> None:
        if self._event.is_set():
            return
        resolved_state = self.expected_state if state is None else state
        self._state = resolved_state
        if self.expected_state and resolved_state != self.expected_state:
            self._error = _("OAuth callback state did not match.")
        elif not code:
            self._error = _("OAuth callback did not include a code.")
        else:
            self._code = code
        self._event.set()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if not self._event.is_set():
                self._error = _("OAuth flow closed.")
                self._event.set()
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=1)

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                query = parse_qs(parsed.query)
                state = query.get("state", [""])[0]
                outer._state = state
                if outer.expected_state and state != outer.expected_state:
                    outer._error = _("OAuth callback state did not match.")
                elif "error" in query:
                    outer._error = query.get("error_description", query["error"])[0]
                else:
                    outer._code = query.get("code", [""])[0]
                outer._event.set()
                body = _("MCP authentication complete. You can close this window.").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class _NoOAuthMetadataRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_OAUTH_METADATA_OPENER = build_opener(_NoOAuthMetadataRedirectHandler)


class _NoOAuthTokenRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_OAUTH_TOKEN_OPENER = build_opener(_NoOAuthTokenRedirectHandler)


def _get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(_("OAuth metadata endpoint did not return an object."))
    return data


def _get_safe_oauth_metadata_json(url: str) -> dict[str, Any]:
    safe_url = _safe_oauth_metadata_fetch_url(url)
    try:
        with _open_oauth_metadata_url(safe_url, timeout=10) as response:
            _validate_urllib_response_peer(response)
            final_url = _response_url(response, safe_url)
            if final_url != safe_url:
                raise RuntimeError(_("OAuth metadata redirects are not allowed."))
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError(_("OAuth metadata redirects are not allowed.")) from exc
        raise
    if not isinstance(data, dict):
        raise RuntimeError(_("OAuth metadata endpoint did not return an object."))
    return data


def _open_oauth_metadata_url(url: str, *, timeout: float) -> Any:
    return _OAUTH_METADATA_OPENER.open(url, timeout=timeout)


def _open_oauth_token_request(request: Request, *, timeout: float) -> Any:
    return _OAUTH_TOKEN_OPENER.open(request, timeout=timeout)


def _response_url(response: Any, fallback: str) -> str:
    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return fallback
    value = geturl()
    return value if isinstance(value, str) and value else fallback


def _safe_oauth_metadata_fetch_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(_("OAuth metadata endpoint must be an HTTPS URL."))
    host = parsed.hostname
    if not host:
        raise RuntimeError(_("OAuth metadata endpoint must include a host."))
    port = parsed.port or 443
    _validate_public_oauth_metadata_host(host, port)
    return url


def _validate_public_oauth_endpoint_url(url: str) -> None:
    if not _is_safe_discovered_oauth_endpoint(url, resolve_host=True):
        raise RuntimeError(_("OAuth endpoint host is not allowed."))


def _validate_public_oauth_metadata_host(host: str, port: int) -> None:
    normalized = host.lower().strip(".")
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(".localhost"):
        raise RuntimeError(_("OAuth metadata endpoint host is not allowed."))
    literal_address = _ip_address_or_none(normalized)
    if literal_address is not None:
        _reject_unsafe_oauth_metadata_address(literal_address)
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError(_("OAuth metadata endpoint host could not be resolved.")) from exc
    if not infos:
        raise RuntimeError(_("OAuth metadata endpoint host could not be resolved."))
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = _ip_address_or_none(str(sockaddr[0]).split("%", 1)[0])
        if address is not None:
            _reject_unsafe_oauth_metadata_address(address)


def _ip_address_or_none(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _reject_unsafe_oauth_metadata_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise RuntimeError(_("OAuth metadata endpoint host is not allowed."))


def _validate_urllib_response_peer(response: Any) -> None:
    address = _urllib_response_peer_address(response)
    if address is not None:
        _reject_unsafe_oauth_metadata_address(address)


def _validate_httpx_response_peer(response: Any) -> None:
    extensions = getattr(response, "extensions", None)
    if not isinstance(extensions, dict):
        return
    stream = extensions.get("network_stream")
    get_extra_info = getattr(stream, "get_extra_info", None)
    if not callable(get_extra_info):
        return
    for key in ("server_addr", "peername"):
        address = _address_from_peername(get_extra_info(key))
        if address is not None:
            _reject_unsafe_oauth_metadata_address(address)
            return
    socket_obj = get_extra_info("socket")
    address = _address_from_socket(socket_obj)
    if address is not None:
        _reject_unsafe_oauth_metadata_address(address)


def _urllib_response_peer_address(response: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    for path in (
        ("fp", "raw", "_sock"),
        ("fp", "_sock"),
        ("raw", "_fp", "fp", "raw", "_sock"),
    ):
        value = _nested_attr(response, path)
        address = _address_from_socket(value)
        if address is not None:
            return address
    return None


def _nested_attr(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for name in path:
        current = getattr(current, name, None)
        if current is None:
            return None
    return current


def _address_from_socket(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    getpeername = getattr(value, "getpeername", None)
    if not callable(getpeername):
        return None
    try:
        return _address_from_peername(getpeername())
    except OSError:
        return None


def _address_from_peername(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if isinstance(value, tuple) and value:
        host = str(value[0]).split("%", 1)[0]
        return _ip_address_or_none(host)
    if isinstance(value, str):
        return _ip_address_or_none(value.split("%", 1)[0])
    return None


def _post_token(
    url: str,
    data: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    validate_public_endpoint: bool = False,
) -> dict[str, Any]:
    if validate_public_endpoint:
        _validate_public_oauth_endpoint_url(url)
    payload = urlencode(data).encode("utf-8")
    request_headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(
        url,
        data=payload,
        headers=request_headers,
        method="POST",
    )
    try:
        with _open_oauth_token_request(request, timeout=10) as response:
            if validate_public_endpoint:
                _validate_urllib_response_peer(response)
            final_url = _response_url(response, url)
            if final_url != url:
                raise RuntimeError(_("OAuth token endpoint redirects are not allowed."))
            parsed = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError(_("OAuth token endpoint redirects are not allowed.")) from exc
        body = exc.read().decode("utf-8", errors="replace")
        error = "http_{}".format(exc.code)
        description = body
        try:
            parsed_error = json.loads(body)
        except Exception:
            parsed_error = None
        if isinstance(parsed_error, dict):
            error = str(parsed_error.get("error") or error)
            description = str(parsed_error.get("error_description") or parsed_error.get("message") or description)
        raise OAuthTokenError(error, description, status_code=exc.code) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(_("OAuth token endpoint did not return an object."))
    return parsed


def _post_revocation_request(
    url: str,
    data: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    validate_public_endpoint: bool = False,
) -> None:
    if validate_public_endpoint:
        _validate_public_oauth_endpoint_url(url)
    payload = urlencode(data).encode("utf-8")
    request_headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(
        url,
        data=payload,
        headers=request_headers,
        method="POST",
    )
    try:
        with _open_oauth_token_request(request, timeout=10) as response:
            if validate_public_endpoint:
                _validate_urllib_response_peer(response)
            final_url = _response_url(response, url)
            if final_url != url:
                raise RuntimeError(_("OAuth revocation endpoint redirects are not allowed."))
            response.read()
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError(_("OAuth revocation endpoint redirects are not allowed.")) from exc
        body = exc.read().decode("utf-8", errors="replace")
        error = "http_{}".format(exc.code)
        description = body
        try:
            parsed_error = json.loads(body)
        except Exception:
            parsed_error = None
        if isinstance(parsed_error, dict):
            error = str(parsed_error.get("error") or error)
            description = str(parsed_error.get("error_description") or parsed_error.get("message") or description)
        raise OAuthTokenError(error, description, status_code=exc.code) from exc


def _post_token_request(
    url: str,
    data: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    validate_public_endpoint: bool = False,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if headers:
        kwargs["headers"] = headers
    if validate_public_endpoint:
        kwargs["validate_public_endpoint"] = True
    return _post_token(url, data, **kwargs)


def _requires_reauth(exc: BaseException) -> bool:
    return _reauth_error_code(exc) is not None


def _reauth_error_code(exc: BaseException) -> str | None:
    if isinstance(exc, OAuthTokenError):
        if exc.error in {"invalid_grant", "invalid_token", "invalid_client"}:
            return exc.error
        if exc.status_code == 401:
            return "unauthorized"
        if exc.status_code == 403:
            return "forbidden"
    text = "{} {}".format(exc.__class__.__name__, str(exc)).lower()
    for error in ("invalid_grant", "invalid_token", "invalid_client", "unauthorized", "forbidden"):
        if error in text:
            return error
    return None


def _client_secret_payload(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    *,
    scope: MCPConfigScope | str | None = None,
) -> dict[str, str]:
    payload, _headers = _client_auth_for_token_request(config, storage, scope=scope, client_id=None)
    return payload


def _client_auth_for_token_request(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    *,
    scope: MCPConfigScope | str | None = None,
    client_id: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    auth_method = get_oauth_storage_secret(config, storage, "client_auth_method", scope=scope)
    if auth_method == "none":
        return {}, {}
    secret = _client_secret_value(config, storage, scope=scope)
    if not secret:
        return {}, {}
    if auth_method == "client_secret_basic":
        if not client_id:
            return {}, {}
        encoded_id = quote(client_id, safe="")
        encoded_secret = quote(secret, safe="")
        credentials = "{}:{}".format(encoded_id, encoded_secret)
        token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return {}, {"Authorization": "Basic {}".format(token)}
    return {"client_secret": secret}, {}


def _client_secret_value(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    *,
    scope: MCPConfigScope | str | None = None,
) -> str | None:
    secret = None
    if config.oauth and config.oauth.client_secret_env:
        import os

        secret = os.environ.get(config.oauth.client_secret_env)
    if not secret:
        secret = get_oauth_storage_secret(config, storage, "client_secret", scope=scope)
    return secret


def _selected_oauth_scopes(
    metadata: OAuthMetadata,
    *,
    required_scopes: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    if "mcp" in metadata.scopes_supported:
        selected = ["mcp"]
    elif len(metadata.scopes_supported) == 1:
        selected = [str(metadata.scopes_supported[0])]
    else:
        selected = []
    if required_scopes:
        selected.extend(str(scope) for scope in required_scopes if str(scope))
    return _dedupe(selected)


def _parse_expires_at(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _scope_value(scope: MCPConfigScope | str | None) -> str:
    if isinstance(scope, MCPConfigScope):
        return scope.value
    return scope or "unspecified"


def _normalized_server_name(name: str) -> str:
    return name.strip().lower()


def _open_browser(url: str) -> bool:
    browser_result = _open_browser_from_env(url)
    if browser_result is not None:
        return browser_result

    if is_desktop_runtime():
        return open_desktop_browser(url)

    import webbrowser

    return bool(webbrowser.open(url))


def _open_browser_from_env(url: str) -> bool | None:
    browser_env = os.environ.get("BROWSER")
    if not browser_env:
        return None
    opened = False
    for raw_entry in browser_env.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        command = _browser_env_command(entry, url)
        if not command:
            continue
        try:
            if is_desktop_runtime():
                if open_desktop_browser(url, command=command):
                    return True
                continue
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            if not is_desktop_runtime() and _open_registered_browser_entry(entry, url):
                return True
            continue
        try:
            opened = process.wait(timeout=_BROWSER_OPEN_EXIT_TIMEOUT_SECONDS) == 0
        except subprocess.TimeoutExpired:
            threading.Thread(target=process.wait, daemon=True).start()
            return True
        if opened:
            return True
    return False


def _browser_env_commands(browser_env: str, url: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for raw_entry in browser_env.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        command = _browser_env_command(entry, url)
        if not command:
            continue
        commands.append(command)
    return commands


def _browser_env_command(entry: str, url: str) -> list[str]:
    command = _split_browser_env_entry(entry)
    if not command:
        return []
    if any("%s" in part for part in command):
        return [part.replace("%s", url) for part in command]
    return [*command, url]


def _open_registered_browser_entry(entry: str, url: str) -> bool:
    command = _split_browser_env_entry(entry)
    if len(command) != 1 or "%s" in command[0]:
        return False
    try:
        import webbrowser

        browser = webbrowser.get(command[0])
        return bool(browser.open(url))
    except Exception:
        return False


def _split_browser_env_entry(entry: str) -> list[str]:
    try:
        command = shlex.split(entry, posix=(os.name != "nt"))
    except ValueError:
        return []
    if os.name == "nt":
        return [_strip_matching_quotes(part) for part in command]
    return command


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
