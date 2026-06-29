from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import posixpath
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from iac_code.i18n import _
from iac_code.mcp.errors import MCPNeedsAuthError
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig


@dataclass(frozen=True)
class NeedsAuthEntry:
    server_name: str
    reason: str
    expires_at: float


class MCPNeedsAuthCache:
    def __init__(self, *, ttl_seconds: int = 900, now: Callable[[], float] | None = None) -> None:
        self._ttl_seconds = ttl_seconds
        self._now = now or time.time
        self._entries: dict[str, NeedsAuthEntry] = {}

    def mark(self, server_name: str, reason: str) -> None:
        self._entries[server_name] = NeedsAuthEntry(
            server_name=server_name,
            reason=reason,
            expires_at=self._now() + self._ttl_seconds,
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


@dataclass(frozen=True)
class OAuthMetadata:
    authorization_endpoint: str
    token_endpoint: str
    scopes_supported: list[str]


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


@dataclass
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

    def wait(self) -> OAuthFlowResult:
        try:
            code = self.callback.wait_for_code(self.timeout_seconds)
        finally:
            self.callback.close()
        return _exchange_authorization_code(
            self.config,
            storage=self.storage,
            scope=self.scope,
            metadata=self.metadata,
            redirect_uri=self.redirect_uri,
            verifier=self.verifier,
            code=code,
            authorization_url=self.authorization_url,
        )


def build_oauth_discovery_urls(config: MCPServerConfig) -> list[str]:
    if not config.url:
        return []
    parsed = urlparse(config.url)
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    urls: list[str] = []
    if config.oauth and config.oauth.auth_server_metadata_url:
        urls.append(config.oauth.auth_server_metadata_url)
    urls.append(origin + "/.well-known/oauth-protected-resource")
    urls.append(origin + "/.well-known/oauth-authorization-server")
    parent = posixpath.dirname(parsed.path.rstrip("/"))
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
    separator = "&" if "?" in authorization_endpoint else "?"
    return authorization_endpoint + separator + urlencode(query)


def discover_oauth_metadata(
    config: MCPServerConfig,
    *,
    http_get_json: Callable[[str], dict[str, Any]] | None = None,
) -> OAuthMetadata:
    getter = http_get_json or _get_json
    protected_resource_authorization_servers: list[str] = []
    for url in build_oauth_discovery_urls(config):
        try:
            data = getter(url)
        except Exception:
            continue
        authorization_endpoint = data.get("authorization_endpoint")
        token_endpoint = data.get("token_endpoint")
        if isinstance(authorization_endpoint, str) and isinstance(token_endpoint, str):
            scopes = data.get("scopes_supported", [])
            return OAuthMetadata(
                authorization_endpoint=authorization_endpoint,
                token_endpoint=token_endpoint,
                scopes_supported=[str(scope) for scope in scopes] if isinstance(scopes, list) else [],
            )
        authorization_servers = data.get("authorization_servers")
        if isinstance(authorization_servers, list):
            protected_resource_authorization_servers.extend(
                server for server in authorization_servers if isinstance(server, str)
            )

    for auth_server in protected_resource_authorization_servers:
        metadata_url = auth_server.rstrip("/") + "/.well-known/oauth-authorization-server"
        try:
            data = getter(metadata_url)
        except Exception:
            continue
        authorization_endpoint = data.get("authorization_endpoint")
        token_endpoint = data.get("token_endpoint")
        if isinstance(authorization_endpoint, str) and isinstance(token_endpoint, str):
            scopes = data.get("scopes_supported", [])
            return OAuthMetadata(
                authorization_endpoint=authorization_endpoint,
                token_endpoint=token_endpoint,
                scopes_supported=[str(scope) for scope in scopes] if isinstance(scopes, list) else [],
            )

    raise RuntimeError(_("Could not discover OAuth metadata for MCP server {server!r}.").format(server=config.name))


def create_oauth_authorization_url(
    config: MCPServerConfig,
    *,
    redirect_uri: str,
    state: str | None = None,
    code_verifier: str | None = None,
    metadata: OAuthMetadata | None = None,
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
        scopes=["mcp"] if "mcp" in metadata_value.scopes_supported else None,
    )
    return url, state_value, verifier


def run_oauth_loopback_flow(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    open_browser: Callable[[str], bool] | None = None,
    timeout_seconds: float = 120.0,
) -> OAuthFlowResult:
    return start_oauth_loopback_flow(
        config,
        storage=storage,
        scope=scope,
        open_browser=open_browser,
        timeout_seconds=timeout_seconds,
    ).wait()


def start_oauth_loopback_flow(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    open_browser: Callable[[str], bool] | None = None,
    timeout_seconds: float = 120.0,
) -> OAuthPendingFlow:
    metadata = discover_oauth_metadata(config)
    callback_port = config.oauth.callback_port if config.oauth and config.oauth.callback_port else 0
    callback = _LoopbackCallback(callback_port)
    redirect_uri = callback.redirect_uri
    auth_url, state, verifier = create_oauth_authorization_url(
        config,
        redirect_uri=redirect_uri,
        metadata=metadata,
    )
    callback.expected_state = state
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
    )


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
) -> OAuthFlowResult:
    token_response = _post_token(
        metadata.token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.oauth.client_id if config.oauth and config.oauth.client_id else "",
            "code_verifier": verifier,
            **_client_secret_payload(config, storage, scope=scope),
        },
    )
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError(
            _("OAuth token response for MCP server {server!r} did not include an access token.").format(
                server=config.name
            )
        )

    access_key = oauth_storage_key(config, "access_token", scope=scope)
    storage.set_secret(access_key, access_token)
    refresh_key = None
    refresh_token = token_response.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        refresh_key = oauth_storage_key(config, "refresh_token", scope=scope)
        storage.set_secret(refresh_key, refresh_token)
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, int | float):
        storage.set_secret(oauth_storage_key(config, "expires_at", scope=scope), str(time.time() + float(expires_in)))

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
    access_key = oauth_storage_key(config, "access_token", scope=scope)
    access_token = storage.get_secret(access_key)
    if not access_token:
        return None

    expires_at = _parse_expires_at(storage.get_secret(oauth_storage_key(config, "expires_at", scope=scope)))
    refresh_token = storage.get_secret(oauth_storage_key(config, "refresh_token", scope=scope))
    clock = now or time.time
    if refresh_token and expires_at is not None and expires_at <= clock() + refresh_margin_seconds:
        return refresh_oauth_access_token(config, storage=storage, scope=scope, refresh_token=refresh_token)
    return access_token


async def get_oauth_access_token_async(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    now: Callable[[], float] | None = None,
    refresh_margin_seconds: float = 60.0,
    refresh_coordinator: TokenRefreshCoordinator | None = None,
) -> str | None:
    access_key = oauth_storage_key(config, "access_token", scope=scope)
    access_token = storage.get_secret(access_key)
    if not access_token:
        return None

    expires_at = _parse_expires_at(storage.get_secret(oauth_storage_key(config, "expires_at", scope=scope)))
    refresh_token = storage.get_secret(oauth_storage_key(config, "refresh_token", scope=scope))
    clock = now or time.time
    if not refresh_token or expires_at is None or expires_at > clock() + refresh_margin_seconds:
        return access_token

    coordinator = refresh_coordinator or _DEFAULT_REFRESH_COORDINATOR

    async def refresh() -> str:
        return await asyncio.to_thread(
            _refresh_oauth_access_token_with_lock,
            config,
            storage=storage,
            scope=scope,
            refresh_token=refresh_token,
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
    now: Callable[[], float],
    refresh_margin_seconds: float,
) -> str:
    access_key = oauth_storage_key(config, "access_token", scope=scope)
    with storage.lock(access_key):
        access_token = storage.get_secret(access_key)
        expires_at = _parse_expires_at(storage.get_secret(oauth_storage_key(config, "expires_at", scope=scope)))
        if access_token and expires_at is not None and expires_at > now() + refresh_margin_seconds:
            return access_token
        return refresh_oauth_access_token(config, storage=storage, scope=scope, refresh_token=refresh_token)


def refresh_oauth_access_token(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
    refresh_token: str | None = None,
) -> str:
    token = refresh_token or storage.get_secret(oauth_storage_key(config, "refresh_token", scope=scope))
    if not token:
        raise RuntimeError(_("No refresh token is available for MCP server {server!r}.").format(server=config.name))
    metadata = discover_oauth_metadata(config)
    try:
        token_response = _post_token(
            metadata.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": config.oauth.client_id if config.oauth and config.oauth.client_id else "",
                **_client_secret_payload(config, storage, scope=scope),
            },
        )
    except Exception as exc:
        if _requires_reauth(exc):
            _clear_oauth_tokens(config, storage=storage, scope=scope)
            raise MCPNeedsAuthError(
                _("MCP server {server!r} requires authentication: {error}").format(
                    server=config.name,
                    error=exc,
                )
            ) from exc
        raise
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError(
            _("OAuth refresh response for MCP server {server!r} did not include an access token.").format(
                server=config.name
            )
        )
    storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), access_token)
    next_refresh_token = token_response.get("refresh_token")
    if isinstance(next_refresh_token, str) and next_refresh_token:
        storage.set_secret(oauth_storage_key(config, "refresh_token", scope=scope), next_refresh_token)
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, int | float):
        storage.set_secret(oauth_storage_key(config, "expires_at", scope=scope), str(time.time() + float(expires_in)))
    return access_token


def oauth_storage_key(config: MCPServerConfig, kind: str, *, scope: MCPConfigScope | str | None = None) -> str:
    material = "\0".join([_normalized_server_name(config.name), _scope_value(scope), config.content_signature(), kind])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "mcp:{}:{}".format(kind, digest)


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
    access_key = oauth_storage_key(config, "access_token", scope=scope)
    access_token = storage.get_secret(access_key)
    if access_token and revoke is not None:
        try:
            revoke(access_token)
        except Exception:
            pass
    for kind in ("access_token", "refresh_token", "expires_at", "client_secret"):
        storage.delete_secret(oauth_storage_key(config, kind, scope=scope))


def _clear_oauth_tokens(
    config: MCPServerConfig,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None = None,
) -> None:
    for kind in ("access_token", "refresh_token", "expires_at"):
        storage.delete_secret(oauth_storage_key(config, kind, scope=scope))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


class _LoopbackCallback:
    def __init__(self, port: int) -> None:
        self.expected_state: str | None = None
        self._event = threading.Event()
        self._code: str | None = None
        self._error: str | None = None
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

    def close(self) -> None:
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


def _get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(_("OAuth metadata endpoint did not return an object."))
    return data


def _post_token(url: str, data: dict[str, str]) -> dict[str, Any]:
    payload = urlencode(data).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
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


def _requires_reauth(exc: BaseException) -> bool:
    if isinstance(exc, OAuthTokenError):
        if exc.status_code in {401, 403}:
            return True
        if exc.error in {"invalid_grant", "invalid_token"}:
            return True
    text = "{} {}".format(exc.__class__.__name__, str(exc)).lower()
    return "invalid_grant" in text or "invalid_token" in text or "unauthorized" in text or "forbidden" in text


def _client_secret_payload(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    *,
    scope: MCPConfigScope | str | None = None,
) -> dict[str, str]:
    secret = None
    if config.oauth and config.oauth.client_secret_env:
        import os

        secret = os.environ.get(config.oauth.client_secret_env)
    if not secret:
        secret = storage.get_secret(oauth_storage_key(config, "client_secret", scope=scope))
    return {"client_secret": secret} if secret else {}


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
    import webbrowser

    return bool(webbrowser.open(url))
