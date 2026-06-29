from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from urllib.parse import parse_qs, urlparse

import pytest

import iac_code.mcp.oauth as oauth_module
from iac_code.mcp.errors import MCPNeedsAuthError
from iac_code.mcp.oauth import (
    MCPNeedsAuthCache,
    OAuthMetadata,
    TokenRefreshCoordinator,
    build_authorization_url,
    build_oauth_discovery_urls,
    clear_oauth_state,
    get_oauth_access_token_async,
    oauth_scope_identity,
    oauth_storage_key,
    refresh_oauth_access_token,
)
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig


def test_secret_storage_uses_keyring_first() -> None:
    keyring = FakeKeyring()
    storage = MCPSecretStorage(keyring_backend=keyring)

    storage.set_secret("token-key", "secret-token")

    assert keyring.values[("iac-code:mcp", "token-key")] == "secret-token"
    assert storage.get_secret("token-key") == "secret-token"
    storage.delete_secret("token-key")
    assert storage.get_secret("token-key") is None


def test_secret_storage_falls_back_to_encrypted_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    storage = MCPSecretStorage(keyring_backend=FailingKeyring())

    storage.set_secret("token-key", "secret-token")

    assert storage.get_secret("token-key") == "secret-token"
    stored_bytes = (tmp_path / "config" / "mcp" / "secrets.json.enc").read_bytes()
    assert b"secret-token" not in stored_bytes
    storage.delete_secret("token-key")
    assert storage.get_secret("token-key") is None


def test_oauth_discovery_url_order_prefers_configured_metadata() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/path/mcp",
            "oauth": {
                "clientId": "client-id",
                "authServerMetadataUrl": "https://auth.example/.well-known/oauth-authorization-server",
            },
        },
    )

    assert build_oauth_discovery_urls(config) == [
        "https://auth.example/.well-known/oauth-authorization-server",
        "https://example.com/.well-known/oauth-protected-resource",
        "https://example.com/.well-known/oauth-authorization-server",
        "https://example.com/path/.well-known/oauth-authorization-server",
    ]


def test_oauth_discovery_follows_protected_resource_authorization_servers() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://resource.example/path/mcp", "oauth": {"clientId": "client-id"}},
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://resource.example/.well-known/oauth-protected-resource":
            return {"authorization_servers": ["https://auth.example"]}
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return {
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
                "scopes_supported": ["mcp"],
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert metadata == OAuthMetadata(
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        scopes_supported=["mcp"],
    )


def test_oauth_discovery_uses_path_aware_legacy_fallback() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://resource.example/mcp/v1", "oauth": {"clientId": "client-id"}},
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://resource.example/mcp/.well-known/oauth-authorization-server":
            return {
                "authorization_endpoint": "https://resource.example/mcp/authorize",
                "token_endpoint": "https://resource.example/mcp/token",
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert metadata.authorization_endpoint == "https://resource.example/mcp/authorize"
    assert metadata.token_endpoint == "https://resource.example/mcp/token"


def test_build_authorization_url_includes_pkce_and_loopback_redirect() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )

    url = build_authorization_url(
        config,
        authorization_endpoint="https://auth.example/authorize",
        redirect_uri="http://127.0.0.1:3118/callback",
        state="state-1",
        code_challenge="challenge-1",
        scopes=["mcp"],
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.example"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["http://127.0.0.1:3118/callback"]
    assert query["state"] == ["state-1"]
    assert query["code_challenge"] == ["challenge-1"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["mcp"]


def test_needs_auth_cache_expires_and_can_be_cleared() -> None:
    now = 1000.0
    cache = MCPNeedsAuthCache(ttl_seconds=60, now=lambda: now)

    cache.mark("remote", "401")
    assert cache.get("remote").reason == "401"

    now = 1061.0
    assert cache.get("remote") is None

    cache.mark("remote", "missing-token")
    cache.clear("remote")
    assert cache.get("remote") is None


@pytest.mark.asyncio
async def test_token_refresh_coordinator_deduplicates_concurrent_refreshes() -> None:
    calls = 0
    coordinator = TokenRefreshCoordinator()

    async def refresh():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "new-token"

    first, second = await asyncio.gather(coordinator.refresh("remote", refresh), coordinator.refresh("remote", refresh))

    assert first == "new-token"
    assert second == "new-token"
    assert calls == 1


@pytest.mark.asyncio
async def test_token_refresh_waiter_cancellation_does_not_cancel_shared_refresh() -> None:
    calls = 0
    release = asyncio.Event()
    coordinator = TokenRefreshCoordinator()

    async def refresh():
        nonlocal calls
        calls += 1
        await release.wait()
        return "new-token"

    owner = asyncio.create_task(coordinator.refresh("remote", refresh))
    while calls == 0:
        await asyncio.sleep(0)

    waiter = asyncio.create_task(coordinator.refresh("remote", refresh))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()

    assert await owner == "new-token"
    assert calls == 1


def test_token_refresh_coordinator_deduplicates_across_event_loops() -> None:
    calls = 0
    calls_lock = threading.Lock()
    release = threading.Event()
    coordinator = TokenRefreshCoordinator()

    async def refresh():
        nonlocal calls
        with calls_lock:
            calls += 1
        await asyncio.to_thread(release.wait, 2)
        return "new-token"

    def run_refresh() -> str:
        return asyncio.run(coordinator.refresh("remote", refresh))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run_refresh)
        second = executor.submit(run_refresh)
        while True:
            with calls_lock:
                if calls:
                    break
            release.wait(0.01)
        release.set()

    assert first.result(timeout=1) == "new-token"
    assert second.result(timeout=1) == "new-token"
    assert calls == 1


@pytest.mark.asyncio
async def test_get_oauth_access_token_async_refreshes_once_for_concurrent_callers(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-token")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "refresh-token")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "100")
    calls = 0

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config: OAuthMetadata(
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)
    coordinator = TokenRefreshCoordinator()

    first, second = await asyncio.gather(
        get_oauth_access_token_async(
            config,
            storage=storage,
            scope="user",
            now=lambda: 200,
            refresh_coordinator=coordinator,
        ),
        get_oauth_access_token_async(
            config,
            storage=storage,
            scope="user",
            now=lambda: 200,
            refresh_coordinator=coordinator,
        ),
    )

    assert first == "new-token"
    assert second == "new-token"
    assert calls == 1


def test_oauth_storage_keys_are_isolated_by_scope() -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})

    assert oauth_storage_key(config, "access_token", scope="user") != oauth_storage_key(
        config,
        "access_token",
        scope="local",
    )


def test_oauth_storage_keys_are_isolated_by_scope_identity() -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})

    assert oauth_storage_key(config, "access_token", scope="session:one") != oauth_storage_key(
        config,
        "access_token",
        scope="session:two",
    )
    assert oauth_storage_key(config, "access_token", scope="project:/repo/one/.mcp.json") != oauth_storage_key(
        config,
        "access_token",
        scope="project:/repo/two/.mcp.json",
    )


def test_oauth_scope_identity_preserves_user_and_isolates_session_and_project() -> None:
    assert oauth_scope_identity(MCPConfigScope.USER, session_id="one") is MCPConfigScope.USER
    assert oauth_scope_identity(MCPConfigScope.SESSION, session_id="one") == "session:one"
    assert oauth_scope_identity(MCPConfigScope.SESSION, session_id="two") == "session:two"
    assert oauth_scope_identity(MCPConfigScope.PROJECT, source_path="/repo/.mcp.json") == "project:/repo/.mcp.json"
    assert (
        oauth_scope_identity(MCPConfigScope.LOCAL, source_path="/repo/.iac-code/settings.local.yml")
        == "local:/repo/.iac-code/settings.local.yml"
    )


def test_client_secret_env_is_resolved_only_for_token_requests(monkeypatch) -> None:
    monkeypatch.setenv("MCP_CLIENT_SECRET", "env-secret")
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id", "clientSecretEnv": "MCP_CLIENT_SECRET"},
        },
    )
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: oauth_module.OAuthMetadata(
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        captured.update(data)
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    assert (
        refresh_oauth_access_token(
            config,
            storage=MCPSecretStorage(keyring_backend=FailingKeyring()),
            scope="user",
            refresh_token="refresh-token",
        )
        == "new-token"
    )

    assert captured["client_id"] == "client-id"
    assert captured["client_secret"] == "env-secret"
    assert captured["refresh_token"] == "refresh-token"


def test_invalid_grant_refresh_clears_tokens_and_requests_reauth(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://example.com/mcp", "oauth": {"clientId": "client-id"}},
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-access")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "old-refresh")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "100")
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: oauth_module.OAuthMetadata(
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        raise RuntimeError("invalid_grant: refresh token expired")

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    with pytest.raises(MCPNeedsAuthError, match="invalid_grant"):
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="old-refresh")

    assert storage.get_secret(oauth_storage_key(config, "access_token", scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "refresh_token", scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "expires_at", scope="user")) is None


def test_clear_oauth_state_deletes_local_state_even_when_revocation_fails() -> None:
    keyring = FakeKeyring()
    storage = MCPSecretStorage(keyring_backend=keyring)
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "access")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "refresh")

    def revoke(_token: str) -> None:
        raise RuntimeError("revocation failed")

    clear_oauth_state(config, storage=storage, scope="user", revoke=revoke)

    assert storage.get_secret(oauth_storage_key(config, "access_token", scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "refresh_token", scope="user")) is None


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


class FailingKeyring:
    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise RuntimeError("keyring unavailable")

    def get_password(self, service_name: str, username: str) -> str | None:
        raise RuntimeError("keyring unavailable")

    def delete_password(self, service_name: str, username: str) -> None:
        raise RuntimeError("keyring unavailable")
