from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

import httpx
import pytest
from loguru import logger
from mcp.client.auth.oauth2 import resource_url_from_server_url
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from pydantic import AnyUrl

import iac_code.mcp.oauth as oauth_module
from iac_code.mcp.env_expansion import expand_env
from iac_code.mcp.errors import MCPNeedsAuthError
from iac_code.mcp.oauth import (
    MCPNeedsAuthCache,
    OAuthMetadata,
    OAuthPendingFlow,
    TokenRefreshCoordinator,
    build_authorization_url,
    build_oauth_client_metadata,
    build_oauth_discovery_urls,
    build_oauth_token_storage,
    clear_oauth_state,
    get_oauth_access_token_async,
    get_oauth_client_information,
    oauth_scope_identity,
    oauth_storage_key,
    refresh_oauth_access_token,
    run_oauth_loopback_flow,
)
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig
from iac_code.utils.log import setup_logging


def _loopback_redirect_uris() -> list[AnyUrl]:
    return [AnyUrl("http://127.0.0.1:3123/callback")]


def _legacy_oauth_storage_key(
    config: MCPServerConfig,
    kind: str,
    *,
    scope: MCPConfigScope | str | None = None,
) -> str:
    scope_value = scope.value if isinstance(scope, MCPConfigScope) else scope or "unspecified"
    material = "\0".join([config.name.strip().lower(), scope_value, config.content_signature(), kind])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "mcp:{}:{}".format(kind, digest)


class _JsonResponse:
    def __init__(self, payload: dict[str, object], *, final_url: str | None = None) -> None:
        self._payload = payload
        self._final_url = final_url

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def geturl(self) -> str:
        return self._final_url or ""


class _PeerSocket:
    def __init__(self, address: str) -> None:
        self._address = address

    def getpeername(self) -> tuple[str, int]:
        return (self._address, 443)


class _PeerJsonResponse(_JsonResponse):
    def __init__(self, payload: dict[str, object], *, peer_address: str, final_url: str | None = None) -> None:
        super().__init__(payload, final_url=final_url)
        self.fp = type("FakeFp", (), {"raw": type("FakeRaw", (), {"_sock": _PeerSocket(peer_address)})()})()


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
        "https://example.com/.well-known/oauth-protected-resource/path/mcp",
        "https://example.com/.well-known/oauth-protected-resource",
        "https://example.com/.well-known/oauth-authorization-server/path/mcp",
        "https://example.com/.well-known/oauth-authorization-server",
        "https://example.com/path/.well-known/oauth-authorization-server",
    ]


def test_oauth_discovery_follows_protected_resource_authorization_servers() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://mcp.example.com/path/mcp", "oauth": {"clientId": "client-id"}},
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://mcp.example.com/.well-known/oauth-protected-resource":
            return {
                "resource": "https://mcp.example.com",
                "authorization_servers": ["https://mcp.example.com"],
            }
        if url == "https://mcp.example.com/.well-known/oauth-authorization-server":
            return {
                "issuer": "https://mcp.example.com",
                "authorization_endpoint": "https://mcp.example.com/oauth/authorize",
                "token_endpoint": "https://mcp.example.com/oauth/token",
                "registration_endpoint": "https://mcp.example.com/oauth/register",
                "scopes_supported": ["mcp"],
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert metadata == OAuthMetadata(
        issuer="https://mcp.example.com",
        authorization_endpoint="https://mcp.example.com/oauth/authorize",
        token_endpoint="https://mcp.example.com/oauth/token",
        registration_endpoint="https://mcp.example.com/oauth/register",
        resource="https://mcp.example.com",
        scopes_supported=["mcp"],
    )


def test_oauth_discovery_preserves_revocation_endpoint() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/path/mcp",
            "oauth": {"authServerMetadataUrl": "https://auth.example/.well-known/oauth-authorization-server"},
        },
    )

    def get_json(url: str) -> dict[str, object]:
        assert url == "https://auth.example/.well-known/oauth-authorization-server"
        return {
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/oauth/authorize",
            "token_endpoint": "https://auth.example/oauth/token",
            "revocation_endpoint": "https://auth.example/oauth/revoke",
            "scopes_supported": ["mcp"],
        }

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert metadata.revocation_endpoint == "https://auth.example/oauth/revoke"


def test_oauth_discovery_rejects_untrusted_protected_resource_local_auth_server() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://mcp.evil/mcp", "oauth": {"clientId": "client-id"}},
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://mcp.evil/.well-known/oauth-protected-resource/mcp":
            return {
                "resource": "https://mcp.evil/mcp",
                "authorization_servers": ["http://127.0.0.1:9"],
            }
        if url == "http://127.0.0.1:9/.well-known/oauth-authorization-server":
            return {
                "issuer": "http://127.0.0.1:9",
                "authorization_endpoint": "http://127.0.0.1:9/authorize",
                "token_endpoint": "http://127.0.0.1:9/token",
            }
        raise RuntimeError(url)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(config, http_get_json=get_json)


@pytest.mark.parametrize(
    "endpoint_field",
    ["authorization_endpoint", "token_endpoint", "registration_endpoint"],
)
def test_oauth_discovery_rejects_untrusted_protected_resource_private_oauth_endpoints(
    endpoint_field: str,
) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://mcp.evil/mcp", "oauth": {"clientId": "client-id"}},
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://mcp.evil/.well-known/oauth-protected-resource/mcp":
            return {
                "resource": "https://mcp.evil/mcp",
                "authorization_servers": ["https://auth.example"],
            }
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return {
                "issuer": "https://auth.example",
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
                "registration_endpoint": "https://auth.example/register",
                endpoint_field: "https://127.0.0.1/oauth",
            }
        raise RuntimeError(url)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(config, http_get_json=get_json)


def test_oauth_discovery_prefers_configured_metadata_url() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://resource.example/path/mcp",
            "oauth": {
                "clientId": "client-id",
                "authServerMetadataUrl": "https://auth.example/custom-metadata",
            },
        },
    )
    requested: list[str] = []

    def get_json(url: str) -> dict[str, object]:
        requested.append(url)
        if url == "https://auth.example/custom-metadata":
            return {
                "issuer": "https://auth.example",
                "authorization_endpoint": "https://auth.example/oauth/authorize",
                "token_endpoint": "https://auth.example/oauth/token",
                "registration_endpoint": "https://auth.example/oauth/register",
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert requested == ["https://auth.example/custom-metadata"]
    assert metadata.registration_endpoint == "https://auth.example/oauth/register"
    assert metadata.issuer == "https://auth.example"


def test_configured_oauth_metadata_url_must_be_public_even_with_custom_getter() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example/mcp",
            "oauth": {"authServerMetadataUrl": "http://127.0.0.1/.well-known/oauth-authorization-server"},
        },
    )

    def get_json(url: str) -> dict[str, object]:
        raise AssertionError("unsafe metadata URL should not be fetched")

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(config, http_get_json=get_json)


def test_configured_oauth_metadata_rejects_private_token_endpoint() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example/mcp",
            "oauth": {"authServerMetadataUrl": "https://auth.example/.well-known/oauth-authorization-server"},
        },
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return {
                "issuer": "https://auth.example",
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "http://169.254.169.254/token",
            }
        raise RuntimeError(url)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(config, http_get_json=get_json)


def test_oauth_resource_metadata_discovery_rejects_configured_loopback_metadata_fallback() -> None:
    configured_metadata_url = "http://127.0.0.1:9999/.well-known/oauth-authorization-server"
    resource_metadata_url = "https://metadata.example/.well-known/oauth-protected-resource/mcp"
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example/mcp",
            "oauth": {"authServerMetadataUrl": configured_metadata_url},
        },
    )
    requested: list[str] = []

    def get_json(url: str) -> dict[str, object]:
        requested.append(url)
        if url == resource_metadata_url:
            return {
                "resource": "https://mcp.example/mcp",
                "authorization_servers": ["https://127.0.0.1"],
            }
        if url == configured_metadata_url:
            return {
                "issuer": "http://127.0.0.1:9999",
                "authorization_endpoint": "http://127.0.0.1:9999/oauth/authorize",
                "token_endpoint": "http://127.0.0.1:9999/oauth/token",
            }
        raise RuntimeError(url)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(
            config,
            http_get_json=get_json,
            resource_metadata_url=resource_metadata_url,
        )

    assert requested[0] == resource_metadata_url
    assert configured_metadata_url not in requested


def test_oauth_resource_metadata_discovery_uses_safe_fetch_for_configured_metadata_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_metadata_url = "https://auth.example/.well-known/oauth-authorization-server"
    resource_metadata_url = "https://metadata.example/.well-known/oauth-protected-resource/mcp"
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example/mcp",
            "oauth": {"authServerMetadataUrl": configured_metadata_url},
        },
    )
    safe_opened: list[str] = []
    plain_opened: list[str] = []

    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))]

    def open_safe_metadata(url: str, *args: object, **kwargs: object) -> object:
        safe_opened.append(str(url))
        if url == resource_metadata_url:
            return _JsonResponse(
                {
                    "resource": "https://mcp.example/mcp",
                    "authorization_servers": ["https://127.0.0.1"],
                }
            )
        if url == configured_metadata_url:
            return _JsonResponse(
                {
                    "issuer": "https://auth.example",
                    "authorization_endpoint": "https://auth.example/oauth/authorize",
                    "token_endpoint": "https://auth.example/oauth/token",
                }
            )
        raise RuntimeError(url)

    def open_plain_metadata(url: str, *args: object, **kwargs: object) -> object:
        plain_opened.append(str(url))
        raise RuntimeError(url)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "_open_oauth_metadata_url", open_safe_metadata, raising=False)
    monkeypatch.setattr(oauth_module, "urlopen", open_plain_metadata)

    metadata = oauth_module.discover_oauth_metadata(config, resource_metadata_url=resource_metadata_url)

    assert safe_opened == [resource_metadata_url, configured_metadata_url]
    assert plain_opened == []
    assert metadata.issuer == "https://auth.example"
    assert metadata.authorization_endpoint == "https://auth.example/oauth/authorize"
    assert metadata.token_endpoint == "https://auth.example/oauth/token"


def test_cimd_oauth_discovery_stores_client_metadata_document_support_flag() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://resource.example/mcp",
            "oauth": {"authServerMetadataUrl": "https://auth.example/custom-metadata"},
        },
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://auth.example/custom-metadata":
            return {
                "issuer": "https://auth.example",
                "authorization_endpoint": "https://auth.example/oauth/authorize",
                "token_endpoint": "https://auth.example/oauth/token",
                "client_id_metadata_document_supported": True,
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert metadata.client_id_metadata_document_supported is True


def test_oauth_discovery_prefers_path_aware_protected_resource_metadata() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://resource.example/mcp/v1", "oauth": {"clientId": "client-id"}},
    )
    requested: list[str] = []

    def get_json(url: str) -> dict[str, object]:
        requested.append(url)
        if url == "https://resource.example/.well-known/oauth-protected-resource/mcp/v1":
            return {
                "resource": "https://resource.example/mcp/v1",
                "authorization_servers": ["https://auth.example"],
            }
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return {
                "issuer": "https://auth.example",
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert requested == [
        "https://resource.example/.well-known/oauth-protected-resource/mcp/v1",
        "https://auth.example/.well-known/oauth-authorization-server",
    ]
    assert metadata.resource == "https://resource.example/mcp/v1"
    assert metadata.authorization_endpoint == "https://auth.example/authorize"


def test_oauth_discovery_uses_path_aware_authorization_server_before_legacy_fallback() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://resource.example/mcp/v1", "oauth": {"clientId": "client-id"}},
    )
    requested: list[str] = []

    def get_json(url: str) -> dict[str, object]:
        requested.append(url)
        if url == "https://resource.example/.well-known/oauth-authorization-server/mcp/v1":
            return {
                "issuer": "https://resource.example/mcp/v1",
                "authorization_endpoint": "https://resource.example/mcp/v1/authorize",
                "token_endpoint": "https://resource.example/mcp/v1/token",
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert "https://resource.example/.well-known/oauth-authorization-server/mcp/v1" in requested
    assert "https://resource.example/mcp/.well-known/oauth-authorization-server" not in requested
    assert metadata.issuer == "https://resource.example/mcp/v1"
    assert metadata.token_endpoint == "https://resource.example/mcp/v1/token"


def test_oauth_discovery_infers_issuer_from_path_aware_authorization_server_metadata_url() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://resource.example/mcp/v1", "oauth": {"clientId": "client-id"}},
    )

    def get_json(url: str) -> dict[str, object]:
        if url == "https://resource.example/.well-known/oauth-authorization-server/mcp/v1":
            return {
                "authorization_endpoint": "https://resource.example/mcp/v1/authorize",
                "token_endpoint": "https://resource.example/mcp/v1/token",
            }
        raise RuntimeError(url)

    metadata = oauth_module.discover_oauth_metadata(config, http_get_json=get_json)

    assert metadata.issuer == "https://resource.example/mcp/v1"


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
    assert metadata.issuer == "https://resource.example/mcp"


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


def test_build_oauth_client_metadata_uses_loopback_redirect_and_supported_mcp_scope() -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        registration_endpoint="https://auth.example/register",
        scopes_supported=["profile", "mcp"],
    )

    client_metadata = build_oauth_client_metadata(
        config,
        metadata,
        redirect_uri="http://127.0.0.1:3123/callback",
    )

    assert client_metadata.model_dump(mode="json", exclude_none=True) == {
        "client_name": "IaC Code",
        "redirect_uris": ["http://127.0.0.1:3123/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp",
    }


def test_selected_oauth_scopes_does_not_request_all_advertised_non_mcp_scopes() -> None:
    metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        scopes_supported=["openid", "profile", "email", "offline_access", "admin"],
    )

    assert oauth_module._selected_oauth_scopes(metadata) == []
    assert oauth_module._selected_oauth_scopes(metadata, required_scopes=["doc:read"]) == ["doc:read"]
    single_scope_metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        scopes_supported=["doc:read"],
    )
    assert oauth_module._selected_oauth_scopes(single_scope_metadata) == ["doc:read"]


def test_cimd_transport_auth_provider_accepts_expanded_https_client_metadata_url_without_discovery(
    monkeypatch,
) -> None:
    expanded, warnings = expand_env(
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientMetadataUrl": "${IAC_CODE_MCP_CLIENT_METADATA_URL}"},
        },
        env={"IAC_CODE_MCP_CLIENT_METADATA_URL": "https://metadata.example.com/client.json"},
        source="settings.yml",
        server_name="remote",
    )
    config = MCPServerConfig.from_mapping("remote", expanded)
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    def discover_oauth_metadata(config: MCPServerConfig) -> OAuthMetadata:
        raise AssertionError("metadata discovery should be deferred until OAuth is needed")

    monkeypatch.setattr(oauth_module, "discover_oauth_metadata", discover_oauth_metadata)

    assert warnings == []
    provider = oauth_module.build_oauth_transport_auth_provider(config, storage, "user")
    assert provider is not None


def test_cimd_client_metadata_url_requires_https_when_supported(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientMetadataUrl": "http://example.com/clients/iac-code.json"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            client_id_metadata_document_supported=True,
        ),
    )

    with pytest.raises(RuntimeError, match="oauth.clientMetadataUrl.*HTTPS"):
        oauth_module.build_oauth_transport_auth_provider(config, storage, "user")


def test_cimd_build_oauth_client_provider_rejects_root_https_client_metadata_url() -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
    )

    with pytest.raises(ValueError, match="non-root pathname"):
        oauth_module.build_oauth_client_provider(
            "https://example.com/mcp",
            build_oauth_client_metadata(config, metadata),
            build_oauth_token_storage(config, MCPSecretStorage(keyring_backend=FakeKeyring()), "user"),
            redirect_handler=None,
            callback_handler=None,
            client_metadata_url="https://metadata.example.com/",
        )


def test_client_metadata_url_validated_by_transport_provider_before_metadata_discovery(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientMetadataUrl": "https://example.com/clients/iac-code.json"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    def discover_oauth_metadata(config: MCPServerConfig) -> OAuthMetadata:
        raise AssertionError("metadata discovery should be deferred until OAuth is needed")

    monkeypatch.setattr(oauth_module, "discover_oauth_metadata", discover_oauth_metadata)

    provider = oauth_module.build_oauth_transport_auth_provider(config, storage, "user")
    assert provider is not None


@pytest.mark.asyncio
async def test_transport_auth_provider_is_httpx_auth_and_sends_bearer_token() -> None:
    seen_authorization: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen_authorization.append(self.headers.get("Authorization"))
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import httpx

        config = MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "http://127.0.0.1:{}/mcp".format(server.server_address[1]),
                "oauth": {"clientId": "client-id"},
            },
        )
        storage = MCPSecretStorage(keyring_backend=FakeKeyring())
        storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "access-token")

        provider = oauth_module.build_oauth_transport_auth_provider(config, storage, MCPConfigScope.USER)

        assert isinstance(provider, httpx.Auth)
        async with httpx.AsyncClient(auth=provider, timeout=5) as client:
            response = await client.get(config.url or "")

        assert response.status_code == 200
        assert seen_authorization == ["Bearer access-token"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_invalid_client_metadata_url_fails_even_when_metadata_does_not_support_cimd(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientMetadataUrl": "http://example.com/clients/iac-code.json"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            client_id_metadata_document_supported=False,
        ),
    )

    with pytest.raises(RuntimeError, match="oauth.clientMetadataUrl.*HTTPS"):
        oauth_module.build_oauth_transport_auth_provider(config, storage, "user")


def test_authorization_code_exchange_includes_allowed_protected_resource(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/path/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        resource="https://mcp.example.com",
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    captured: dict[str, str] = {}

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        captured.update(data)
        return {"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    oauth_module._exchange_authorization_code(
        config,
        storage=storage,
        scope="user",
        metadata=metadata,
        redirect_uri="http://127.0.0.1:3123/callback",
        verifier="verifier",
        code="code-1",
        authorization_url="https://auth.example/authorize",
    )

    assert captured["resource"] == "https://mcp.example.com"


def test_authorization_code_exchange_fallback_resource_matches_mcp_sdk(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/path/mcp?version=1#fragment",
            "oauth": {"clientId": "client-id"},
        },
    )
    metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        resource="https://mcp.example.com/other",
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    captured: dict[str, str] = {}

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        captured.update(data)
        return {"access_token": "access-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    oauth_module._exchange_authorization_code(
        config,
        storage=storage,
        scope="user",
        metadata=metadata,
        redirect_uri="http://127.0.0.1:3123/callback",
        verifier="verifier",
        code="code-1",
        authorization_url="https://auth.example/authorize",
    )

    assert config.url is not None
    assert captured["resource"] == resource_url_from_server_url(config.url)


def test_authorization_code_exchange_uses_configured_url_when_metadata_resource_missing(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://MCP.ALIBABA-INC.COM/path/mcp?version=1#fragment",
            "oauth": {"clientId": "client-id"},
        },
    )
    metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    captured: dict[str, str] = {}

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        captured.update(data)
        return {"access_token": "access-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    oauth_module._exchange_authorization_code(
        config,
        storage=storage,
        scope="user",
        metadata=metadata,
        redirect_uri="http://127.0.0.1:3123/callback",
        verifier="verifier",
        code="code-1",
        authorization_url="https://auth.example/authorize",
    )

    assert config.url is not None
    assert captured["resource"] == resource_url_from_server_url(config.url)


def test_refresh_includes_allowed_protected_resource(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/path/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            resource="https://mcp.example.com",
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        captured.update(data)
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    assert (
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="refresh-token") == "new-token"
    )

    assert captured["resource"] == "https://mcp.example.com"


def test_refresh_uses_configured_url_when_metadata_resource_missing(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/path/mcp?version=1#fragment",
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        captured.update(data)
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    assert (
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="refresh-token") == "new-token"
    )

    assert config.url is not None
    assert captured["resource"] == resource_url_from_server_url(config.url)


def test_needs_auth_cache_expires_and_can_be_cleared() -> None:
    now = 1000.0
    cache = MCPNeedsAuthCache(ttl_seconds=60, now=lambda: now)

    cache.mark("remote", "401")
    entry = cache.get("remote")
    assert entry is not None
    assert entry.reason == "401"

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


def test_token_refresh_coordinator_deduplicates_across_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    calls_lock = threading.Lock()
    owner_started = threading.Event()
    release = threading.Event()
    waiter_registered = threading.Event()
    coordinator = TokenRefreshCoordinator()
    wrap_future = asyncio.wrap_future

    def observed_wrap_future(future, *, loop=None):
        waiter_registered.set()
        return wrap_future(future, loop=loop)

    monkeypatch.setattr(oauth_module.asyncio, "wrap_future", observed_wrap_future)

    async def refresh():
        nonlocal calls
        with calls_lock:
            calls += 1
        owner_started.set()
        await asyncio.to_thread(release.wait)
        return "new-token"

    def run_refresh() -> str:
        return asyncio.run(coordinator.refresh("remote", refresh))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run_refresh)
        second = executor.submit(run_refresh)
        try:
            assert owner_started.wait(1)
            assert waiter_registered.wait(1)
        finally:
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
        lambda _config, **_kwargs: OAuthMetadata(
            issuer="https://auth.example",
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


@pytest.mark.asyncio
async def test_get_oauth_access_token_async_refreshes_once_across_storage_instances(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    first_storage = MCPSecretStorage()
    second_storage = MCPSecretStorage()
    first_storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-token")
    first_storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "refresh-token")
    first_storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "100")
    calls = 0

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        threading.Event().wait(0.05)
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    first, second = await asyncio.gather(
        get_oauth_access_token_async(
            config,
            storage=first_storage,
            scope="user",
            now=lambda: 200,
            refresh_coordinator=TokenRefreshCoordinator(),
        ),
        get_oauth_access_token_async(
            config,
            storage=second_storage,
            scope="user",
            now=lambda: 200,
            refresh_coordinator=TokenRefreshCoordinator(),
        ),
    )

    assert first == "new-token"
    assert second == "new-token"
    assert calls == 1


@pytest.mark.asyncio
async def test_transport_token_storage_refreshes_once_across_storage_instances(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    first_storage = MCPSecretStorage()
    second_storage = MCPSecretStorage()
    first_storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-token")
    first_storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "refresh-token")
    first_storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "100")
    calls = 0

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(
        url: str,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        threading.Event().wait(0.05)
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)
    first_token_storage = oauth_module.build_oauth_token_storage(config, first_storage, "user")
    second_token_storage = oauth_module.build_oauth_token_storage(config, second_storage, "user")

    first, second = await asyncio.gather(first_token_storage.get_tokens(), second_token_storage.get_tokens())

    assert first.access_token == "new-token"
    assert second.access_token == "new-token"
    assert calls == 1


@pytest.mark.asyncio
async def test_get_oauth_access_token_async_refreshes_once_across_storage_instances_without_expires_in(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    first_storage = MCPSecretStorage()
    second_storage = MCPSecretStorage()
    first_storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-token")
    first_storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "refresh-token")
    first_storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "100")
    calls = 0

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        threading.Event().wait(0.05)
        return {"access_token": "new-token"}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    first, second = await asyncio.gather(
        get_oauth_access_token_async(
            config,
            storage=first_storage,
            scope="user",
            now=lambda: 200,
            refresh_coordinator=TokenRefreshCoordinator(),
        ),
        get_oauth_access_token_async(
            config,
            storage=second_storage,
            scope="user",
            now=lambda: 200,
            refresh_coordinator=TokenRefreshCoordinator(),
        ),
    )

    assert first == "new-token"
    assert second == "new-token"
    assert calls == 1
    assert first_storage.get_secret(oauth_storage_key(config, "expires_at", scope="user")) is None


@pytest.mark.timeout(60)
def test_sync_expired_oauth_refresh_without_expires_in_deduplicates_across_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    counter_path = tmp_path / "refresh-count.txt"
    worker_path = tmp_path / "refresh_worker.py"
    worker_path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import os
            import sys
            import time
            from pathlib import Path

            import iac_code.mcp.oauth as oauth_module
            from iac_code.mcp.oauth import get_oauth_access_token
            from iac_code.mcp.storage import MCPSecretStorage
            from iac_code.mcp.types import MCPConfigScope, MCPServerConfig

            counter_path = Path(sys.argv[1])
            barrier_dir = Path(sys.argv[2])
            config = MCPServerConfig.from_mapping(
                "remote",
                {"type": "http", "url": "https://example.com/mcp", "oauth": {"clientId": "client-id"}},
            )

            def wait_for_barrier(name: str) -> None:
                barrier_dir.mkdir(parents=True, exist_ok=True)
                (barrier_dir / f"{name}-{os.getpid()}.ready").write_text("ready", encoding="utf-8")
                deadline = time.monotonic() + 20
                while len(list(barrier_dir.glob(f"{name}-*.ready"))) < 2:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Timed out waiting for {name} barrier")
                    time.sleep(0.01)

            class BarrierStorage(MCPSecretStorage):
                def get_secret(self, key: str) -> str | None:
                    value = super().get_secret(key)
                    if key == oauth_module.oauth_storage_key(config, "refresh_token", scope=MCPConfigScope.USER):
                        wait_for_barrier("refresh-token-read")
                    if key == oauth_module.oauth_storage_key(config, "refresh_marker", scope=MCPConfigScope.USER):
                        wait_for_barrier("refresh-marker-read")
                    return value

            oauth_module.discover_oauth_metadata = lambda _config: oauth_module.OAuthMetadata(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
                scopes_supported=[],
            )

            def post_token(url: str, data: dict[str, str], **kwargs: object) -> dict[str, object]:
                with counter_path.open("a", encoding="utf-8") as handle:
                        handle.write("refresh\\n")
                        handle.flush()
                time.sleep(0.25)
                return {"access_token": "new-token"}

            oauth_module._post_token = post_token

            def main() -> None:
                token = get_oauth_access_token(
                    config,
                    storage=BarrierStorage(),
                    scope=MCPConfigScope.USER,
                    now=lambda: 200,
                )
                print(token)

            main()
            """
        ),
        encoding="utf-8",
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://example.com/mcp", "oauth": {"clientId": "client-id"}},
    )
    storage = MCPSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-token")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "refresh-token")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "100")
    storage.set_secret(oauth_storage_key(config, "refresh_marker", scope="user"), "old-marker")
    env = os.environ.copy()
    barrier_dir = tmp_path / "barriers"

    processes = [
        subprocess.Popen(
            [sys.executable, str(worker_path), str(counter_path), str(barrier_dir)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]

    try:
        results = [process.communicate(timeout=20) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, stderr
        assert stdout.strip() == "new-token"
    refresh_count = counter_path.read_text(encoding="utf-8").count("refresh") if counter_path.exists() else 0
    assert refresh_count == 1
    assert storage.get_secret(oauth_storage_key(config, "expires_at", scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "refresh_marker", scope="user")) != "old-marker"


def test_refresh_with_lock_does_not_use_captured_refresh_token_after_cleanup(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "100",
        "refresh_marker": "old-marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    captured_refresh_token = storage.get_secret(oauth_storage_key(config, "refresh_token", scope="user"))
    captured_refresh_marker = storage.get_secret(oauth_storage_key(config, "refresh_marker", scope="user"))
    assert captured_refresh_token == "old-refresh"
    assert captured_refresh_marker == "old-marker"
    calls = 0

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config, **_kwargs: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)
    clear_oauth_state(config, storage=storage, scope="user")

    result = oauth_module._refresh_oauth_access_token_with_lock(
        config,
        storage=storage,
        scope="user",
        refresh_token=captured_refresh_token,
        refresh_marker=captured_refresh_marker,
        now=lambda: 200,
        refresh_margin_seconds=60,
    )

    assert result is None
    assert calls == 0
    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "auth_flow_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


def test_direct_refresh_after_cleanup_does_not_resurrect_tokens(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "100",
        "refresh_marker": "old-marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config, **_kwargs: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        clear_oauth_state(config, storage=storage, scope="user")
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="old-refresh")

    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "auth_flow_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


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


def test_oauth_storage_keys_do_not_use_legacy_plain_sha256() -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})

    assert oauth_storage_key(config, "client_secret", scope="user") != _legacy_oauth_storage_key(
        config,
        "client_secret",
        scope="user",
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


def test_oauth_token_storage_stores_registered_client_information_by_scope() -> None:
    from mcp.shared.auth import OAuthClientInformationFull

    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    token_storage = build_oauth_token_storage(config, storage, "project:/repo/.mcp.json")

    asyncio.run(
        token_storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=_loopback_redirect_uris(),
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="mcp",
                client_name="IaC Code",
                client_id="registered-client",
                client_secret="registered-client-secret",
            )
        )
    )

    assert (
        storage.get_secret(oauth_storage_key(config, "client_id", scope="project:/repo/.mcp.json"))
        == "registered-client"
    )
    assert (
        storage.get_secret(oauth_storage_key(config, "client_secret", scope="project:/repo/.mcp.json"))
        == "registered-client-secret"
    )
    client_info = get_oauth_client_information(config, storage, "project:/repo/.mcp.json")
    assert client_info is not None
    assert client_info.client_id == "registered-client"
    assert client_info.client_secret == "registered-client-secret"


def test_oauth_token_storage_allows_public_registered_client_without_secret() -> None:
    from mcp.shared.auth import OAuthClientInformationFull

    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    token_storage = build_oauth_token_storage(config, storage, "user")

    asyncio.run(
        token_storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=_loopback_redirect_uris(),
                token_endpoint_auth_method="none",
                client_id="registered-client",
            )
        )
    )

    assert storage.get_secret(oauth_storage_key(config, "client_id", scope="user")) == "registered-client"
    assert storage.get_secret(oauth_storage_key(config, "client_secret", scope="user")) is None


def test_oauth_token_storage_fresh_auth_after_cleanup_does_not_resurrect_state() -> None:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    token_storage = build_oauth_token_storage(config, storage, "user")

    assert asyncio.run(token_storage.get_tokens()) is None
    asyncio.run(
        token_storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=_loopback_redirect_uris(),
                token_endpoint_auth_method="client_secret_post",
                client_id="registered-client",
                client_secret="registered-secret",
            )
        )
    )
    clear_oauth_state(config, storage=storage, scope="user")
    asyncio.run(
        token_storage.set_tokens(OAuthToken(access_token="late-access", refresh_token="late-refresh", expires_in=3600))
    )

    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "auth_flow_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


def test_static_oauth_pending_flow_after_cleanup_does_not_resurrect_state(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config, **_kwargs: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )
    monkeypatch.setattr(
        oauth_module,
        "_post_token",
        lambda _url, _data: {"access_token": "late-access", "refresh_token": "late-refresh", "expires_in": 3600},
    )

    pending = oauth_module.start_oauth_loopback_flow(
        config,
        storage=storage,
        scope="user",
        open_browser=lambda _url: False,
        timeout_seconds=1,
    )
    clear_oauth_state(config, storage=storage, scope="user")

    pending.complete_manually("late-code")

    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "auth_flow_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


def test_oauth_token_storage_fresh_auth_persists_client_tokens_and_marker() -> None:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    token_storage = build_oauth_token_storage(config, storage, "user")

    assert asyncio.run(token_storage.get_tokens()) is None
    asyncio.run(
        token_storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=_loopback_redirect_uris(),
                token_endpoint_auth_method="client_secret_post",
                client_id="registered-client",
                client_secret="registered-secret",
            )
        )
    )
    asyncio.run(token_storage.set_tokens(OAuthToken(access_token="new-access", refresh_token="new-refresh")))

    assert storage.get_secret(oauth_storage_key(config, "access_token", scope="user")) == "new-access"
    assert storage.get_secret(oauth_storage_key(config, "refresh_token", scope="user")) == "new-refresh"
    assert storage.get_secret(oauth_storage_key(config, "expires_at", scope="user")) is None
    refresh_marker = storage.get_secret(oauth_storage_key(config, "refresh_marker", scope="user"))
    assert refresh_marker is not None
    assert storage.get_secret(oauth_storage_key(config, "auth_flow_marker", scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope="user")) == "registered-client"
    assert storage.get_secret(oauth_storage_key(config, "client_secret", scope="user")) == "registered-secret"
    assert storage.get_secret(oauth_storage_key(config, "client_auth_method", scope="user")) == "client_secret_post"


def test_oauth_token_storage_refresh_after_cleanup_does_not_resurrect_state() -> None:
    from mcp.shared.auth import OAuthToken

    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "9999999999",
        "refresh_marker": "old-marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    token_storage = build_oauth_token_storage(config, storage, "user")

    captured = asyncio.run(token_storage.get_tokens())
    assert captured is not None
    assert captured.refresh_token == "old-refresh"
    clear_oauth_state(config, storage=storage, scope="user")
    asyncio.run(
        token_storage.set_tokens(OAuthToken(access_token="new-access", refresh_token="new-refresh", expires_in=3600))
    )

    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


def test_oauth_token_storage_refresh_without_expires_in_deletes_stale_expiry_and_updates_marker() -> None:
    from mcp.shared.auth import OAuthToken

    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-access")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "old-refresh")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "9999999999")
    storage.set_secret(oauth_storage_key(config, "refresh_marker", scope="user"), "old-marker")
    token_storage = build_oauth_token_storage(config, storage, "user")

    captured = asyncio.run(token_storage.get_tokens())
    assert captured is not None
    assert captured.refresh_token == "old-refresh"
    asyncio.run(token_storage.set_tokens(OAuthToken(access_token="new-access", refresh_token="new-refresh")))

    assert storage.get_secret(oauth_storage_key(config, "access_token", scope="user")) == "new-access"
    assert storage.get_secret(oauth_storage_key(config, "refresh_token", scope="user")) == "new-refresh"
    assert storage.get_secret(oauth_storage_key(config, "expires_at", scope="user")) is None
    refresh_marker = storage.get_secret(oauth_storage_key(config, "refresh_marker", scope="user"))
    assert refresh_marker is not None
    assert refresh_marker != "old-marker"


def test_oauth_token_storage_client_info_after_refresh_cleanup_does_not_resurrect_state() -> None:
    from mcp.shared.auth import OAuthClientInformationFull

    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "9999999999",
        "refresh_marker": "old-marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    token_storage = build_oauth_token_storage(config, storage, "user")

    captured = asyncio.run(token_storage.get_tokens())
    assert captured is not None
    assert captured.refresh_token == "old-refresh"
    clear_oauth_state(config, storage=storage, scope="user")
    asyncio.run(
        token_storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=_loopback_redirect_uris(),
                token_endpoint_auth_method="client_secret_post",
                client_id="registered-client",
                client_secret="registered-secret",
            )
        )
    )

    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


def test_refresh_uses_basic_auth_for_registered_client_secret_basic(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    storage.set_secret(oauth_storage_key(config, "client_id", scope="user"), "registered-client")
    storage.set_secret(oauth_storage_key(config, "client_secret", scope="user"), "registered-secret")
    storage.set_secret(oauth_storage_key(config, "client_auth_method", scope="user"), "client_secret_basic")
    captured_body: dict[str, list[str]] = {}
    captured_headers: dict[str, str] = {}

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: oauth_module.OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    class TokenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"access_token": "new-token", "expires_in": 3600}).encode("utf-8")

    def open_oauth_token_request(request, *, timeout: int):
        captured_body.update(parse_qs(request.data.decode("utf-8")))
        captured_headers.update(dict(request.header_items()))
        return TokenResponse()

    monkeypatch.setattr(oauth_module, "_open_oauth_token_request", open_oauth_token_request)

    assert (
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="refresh-token") == "new-token"
    )

    expected_auth = "Basic " + base64.b64encode(b"registered-client:registered-secret").decode("ascii")
    assert captured_headers["Authorization"] == expected_auth
    assert captured_body == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-token"],
        "client_id": ["registered-client"],
        "resource": ["https://example.com/mcp"],
    }


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
            issuer="https://auth.example",
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
    storage.set_secret(oauth_storage_key(config, "refresh_marker", scope="user"), "marker")
    storage.set_secret(oauth_storage_key(config, "client_id", scope="user"), "registered-client")
    storage.set_secret(oauth_storage_key(config, "client_secret", scope="user"), "registered-secret")
    storage.set_secret(oauth_storage_key(config, "client_auth_method", scope="user"), "client_secret_post")
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: oauth_module.OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        raise RuntimeError("invalid_grant: refresh token expired")

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    with pytest.raises(MCPNeedsAuthError, match="invalid_grant") as raised:
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="old-refresh")

    assert getattr(raised.value, "auth_error", None) == "invalid_grant"
    for kind in ("access_token", "refresh_token", "expires_at", "refresh_marker", "auth_flow_marker"):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope="user")) == "registered-client"
    assert storage.get_secret(oauth_storage_key(config, "client_secret", scope="user")) == "registered-secret"
    assert storage.get_secret(oauth_storage_key(config, "client_auth_method", scope="user")) == "client_secret_post"


def test_invalid_token_refresh_clears_tokens_but_preserves_dynamic_client_state(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://example.com/mcp", "oauth": {}},
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "100",
        "refresh_marker": "marker",
        "auth_flow_marker": "flow-marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: oauth_module.OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        raise oauth_module.OAuthTokenError("invalid_token", "refresh token expired", status_code=400)

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    with pytest.raises(MCPNeedsAuthError, match="invalid_token") as raised:
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="old-refresh")

    assert getattr(raised.value, "auth_error", None) == "invalid_token"
    assert getattr(raised.value, "auth_status_code", None) == 400
    for kind in ("access_token", "refresh_token", "expires_at", "refresh_marker", "auth_flow_marker"):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope="user")) == "registered-client"
    assert storage.get_secret(oauth_storage_key(config, "client_secret", scope="user")) == "registered-secret"
    assert storage.get_secret(oauth_storage_key(config, "client_auth_method", scope="user")) == "client_secret_post"


def test_sdk_mcp_error_auth_challenge_extracts_insufficient_scope() -> None:
    exc = McpError(
        ErrorData(
            code=403,
            message="Forbidden",
            data={
                "error": "insufficient_scope",
                "required_scopes": ["write:stack", "read:stack"],
                "resource_metadata": "https://resource.example/.well-known/oauth-protected-resource/mcp",
            },
        )
    )

    challenge = oauth_module.auth_challenge_from_exception(exc)

    assert challenge is not None
    assert challenge.status_code == 403
    assert challenge.error == "insufficient_scope"
    assert challenge.error_description == "Forbidden"
    assert challenge.required_scopes == ("write:stack", "read:stack")
    assert challenge.resource_metadata_url == "https://resource.example/.well-known/oauth-protected-resource/mcp"

    error = oauth_module.needs_auth_error_from_challenge("remote", challenge)

    assert getattr(error, "auth_resource_metadata_url", None) == (
        "https://resource.example/.well-known/oauth-protected-resource/mcp"
    )


@pytest.mark.parametrize("address", ["10.0.0.8", "100.64.0.8"])
def test_oauth_resource_metadata_discovery_rejects_hostname_resolving_to_non_global_ip(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://mcp.example/mcp"})
    opened: list[str] = []

    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))]

    def urlopen_should_not_run(url: str, *args: object, **kwargs: object) -> object:
        opened.append(str(url))
        return _JsonResponse(
            {
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
            }
        )

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "urlopen", urlopen_should_not_run)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(
            config,
            resource_metadata_url="https://metadata.example/.well-known/oauth-protected-resource/mcp",
        )

    assert opened == []


def test_oauth_resource_metadata_url_rejects_non_global_literal() -> None:
    assert (
        oauth_module.safe_oauth_resource_metadata_url("https://100.64.0.1/.well-known/oauth-protected-resource/mcp")
        is None
    )


@pytest.mark.parametrize(
    "endpoint_field",
    ["authorization_endpoint", "token_endpoint", "registration_endpoint"],
)
def test_oauth_resource_metadata_discovery_rejects_private_discovered_oauth_endpoints(
    endpoint_field: str,
) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://mcp.example/mcp"})
    resource_metadata_url = "https://metadata.example/.well-known/oauth-protected-resource/mcp"

    def get_json(url: str) -> dict[str, object]:
        if url == resource_metadata_url:
            return {
                "resource": "https://mcp.example/mcp",
                "authorization_servers": ["https://auth.example"],
            }
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return {
                "issuer": "https://auth.example",
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
                "registration_endpoint": "https://auth.example/register",
                endpoint_field: "https://127.0.0.1/oauth",
            }
        raise RuntimeError(url)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(
            config,
            http_get_json=get_json,
            resource_metadata_url=resource_metadata_url,
        )


def test_oauth_resource_metadata_discovery_rejects_redirected_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://mcp.example/mcp"})

    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))]

    def open_redirected_metadata(url: str, *args: object, **kwargs: object) -> object:
        return _JsonResponse(
            {
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
            },
            final_url="https://169.254.169.254/latest/meta-data",
        )

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "_open_oauth_metadata_url", open_redirected_metadata, raising=False)
    monkeypatch.setattr(oauth_module, "urlopen", open_redirected_metadata)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(
            config,
            resource_metadata_url="https://metadata.example/.well-known/oauth-protected-resource/mcp",
        )


def test_oauth_resource_metadata_discovery_rejects_rebound_private_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://mcp.example/mcp"})

    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))]

    def open_rebound_metadata(url: str, *args: object, **kwargs: object) -> object:
        return _PeerJsonResponse(
            {
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
            },
            peer_address="169.254.169.254",
            final_url=str(url),
        )

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "_open_oauth_metadata_url", open_rebound_metadata, raising=False)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(
            config,
            resource_metadata_url="https://metadata.example/.well-known/oauth-protected-resource/mcp",
        )


def test_oauth_resource_metadata_discovery_rejects_http_redirect_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://mcp.example/mcp"})

    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))]

    def open_redirect(url: str, *args: object, **kwargs: object) -> object:
        raise HTTPError(str(url), 302, "Found", {"Location": "https://169.254.169.254/latest/meta-data"}, None)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "_open_oauth_metadata_url", open_redirect, raising=False)
    monkeypatch.setattr(oauth_module, "urlopen", open_redirect)

    with pytest.raises(RuntimeError, match="Could not discover OAuth metadata"):
        oauth_module.discover_oauth_metadata(
            config,
            resource_metadata_url="https://metadata.example/.well-known/oauth-protected-resource/mcp",
        )


def test_oauth_token_post_rejects_redirected_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[object] = []

    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))]

    def open_redirected_token(request: object, *args: object, **kwargs: object) -> object:
        requests.append(request)
        return _PeerJsonResponse(
            {"access_token": "leaked-token"},
            peer_address="8.8.8.8",
            final_url="https://169.254.169.254/latest/meta-data",
        )

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "_open_oauth_token_request", open_redirected_token, raising=False)
    monkeypatch.setattr(oauth_module, "urlopen", open_redirected_token)

    with pytest.raises(RuntimeError, match="OAuth token endpoint redirects are not allowed"):
        oauth_module._post_token(
            "https://auth.example/token",
            {"grant_type": "authorization_code"},
            headers={"Authorization": "Basic secret-client"},
            validate_public_endpoint=True,
        )

    assert requests
    assert requests[0].headers.get("Authorization") == "Basic secret-client"


def test_oauth_token_post_rejects_http_redirect_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))]

    def open_redirect(request: object, *args: object, **kwargs: object) -> object:
        raise HTTPError(
            "https://auth.example/token",
            302,
            "Found",
            {"Location": "https://169.254.169.254/latest/meta-data"},
            None,
        )

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "_open_oauth_token_request", open_redirect, raising=False)
    monkeypatch.setattr(oauth_module, "urlopen", open_redirect)

    with pytest.raises(RuntimeError, match="OAuth token endpoint redirects are not allowed"):
        oauth_module._post_token(
            "https://auth.example/token",
            {"grant_type": "refresh_token"},
            validate_public_endpoint=True,
        )


def test_oauth_token_post_rejects_endpoint_resolving_to_non_global_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[object] = []

    def getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.8", port or 443))]

    def open_token(request: object, *args: object, **kwargs: object) -> object:
        requests.append(request)
        return _JsonResponse({"access_token": "leaked-token"})

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(oauth_module, "_open_oauth_token_request", open_token, raising=False)
    monkeypatch.setattr(oauth_module, "urlopen", open_token)

    with pytest.raises(RuntimeError, match="OAuth endpoint host is not allowed"):
        oauth_module._post_token(
            "https://auth.example/token",
            {"grant_type": "refresh_token"},
            validate_public_endpoint=True,
        )

    assert requests == []


def test_sdk_mcp_error_auth_challenge_extracts_invalid_token() -> None:
    exc = McpError(
        ErrorData(
            code=401,
            message="Unauthorized",
            data={"error": "invalid_token"},
        )
    )

    error = oauth_module.needs_auth_error_from_exception("remote", exc)

    assert isinstance(error, MCPNeedsAuthError)
    assert getattr(error, "auth_error", None) == "invalid_token"
    assert getattr(error, "auth_status_code", None) == 401


def test_nested_transport_error_auth_challenge_extracts_invalid_token() -> None:
    request = httpx.Request("GET", "https://mcp.example.com/mcp")
    response = httpx.Response(
        401,
        request=request,
        headers={"WWW-Authenticate": 'Bearer realm="mcp", error="invalid_token"'},
    )
    transport_error = httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

    class FakeExceptionGroupError(Exception):
        def __init__(self, exceptions: list[BaseException]) -> None:
            self.exceptions = exceptions
            super().__init__("unhandled errors in a TaskGroup")

    error = oauth_module.needs_auth_error_from_exception("remote", FakeExceptionGroupError([transport_error]))

    assert isinstance(error, MCPNeedsAuthError)
    assert getattr(error, "auth_error", None) == "invalid_token"
    assert getattr(error, "auth_status_code", None) == 401


def test_nested_mcp_needs_auth_error_is_preserved() -> None:
    needs_auth = MCPNeedsAuthError("MCP server 'remote' requires authentication: invalid_grant")
    setattr(needs_auth, "auth_error", "invalid_grant")

    class FakeExceptionGroupError(Exception):
        def __init__(self, exceptions: list[BaseException]) -> None:
            self.exceptions = exceptions
            super().__init__("unhandled errors in a TaskGroup")

    error = oauth_module.needs_auth_error_from_exception("remote", FakeExceptionGroupError([needs_auth]))

    assert error is needs_auth
    assert getattr(error, "auth_error", None) == "invalid_grant"


def test_sdk_mcp_error_invalid_client_clears_dynamic_client_state() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://example.com/mcp", "oauth": {}},
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "100",
        "refresh_marker": "marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    exc = McpError(
        ErrorData(
            code=400,
            message="Bad Request",
            data={"error": "invalid_client"},
        )
    )

    error = oauth_module.needs_auth_error_from_exception("remote", exc, config=config, storage=storage, scope="user")

    assert isinstance(error, MCPNeedsAuthError)
    assert getattr(error, "auth_error", None) == "invalid_client"
    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


@pytest.mark.parametrize("challenge_error,status_code", [("invalid_token", 401), ("insufficient_scope", 403)])
def test_auth_challenge_token_errors_preserve_registered_client_state(
    challenge_error: str,
    status_code: int,
) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://example.com/mcp", "oauth": {}},
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "100",
        "refresh_marker": "marker",
        "auth_flow_marker": "flow-marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    exc = McpError(
        ErrorData(
            code=status_code,
            message="auth challenge",
            data={"error": challenge_error},
        )
    )

    error = oauth_module.needs_auth_error_from_exception("remote", exc, config=config, storage=storage, scope="user")

    assert isinstance(error, MCPNeedsAuthError)
    for kind in ("access_token", "refresh_token", "expires_at", "refresh_marker", "auth_flow_marker"):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope="user")) == "registered-client"
    assert storage.get_secret(oauth_storage_key(config, "client_secret", scope="user")) == "registered-secret"
    assert storage.get_secret(oauth_storage_key(config, "client_auth_method", scope="user")) == "client_secret_post"


def test_locked_invalid_token_refresh_preserves_dynamic_client_state(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://example.com/mcp", "oauth": {}},
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": "100",
        "refresh_marker": "marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: oauth_module.OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        raise oauth_module.OAuthTokenError("invalid_token", "refresh token expired", status_code=400)

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    with pytest.raises(MCPNeedsAuthError) as raised:
        oauth_module.get_oauth_access_token(config, storage=storage, scope="user", now=lambda: 200)

    assert getattr(raised.value, "auth_error", None) == "invalid_token"
    for kind in ("access_token", "refresh_token", "expires_at", "refresh_marker", "auth_flow_marker"):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope="user")) == "registered-client"
    assert storage.get_secret(oauth_storage_key(config, "client_secret", scope="user")) == "registered-secret"
    assert storage.get_secret(oauth_storage_key(config, "client_auth_method", scope="user")) == "client_secret_post"


def test_invalid_client_refresh_clears_dynamic_client_state_and_requests_reauth(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {"type": "http", "url": "https://example.com/mcp", "oauth": {}},
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "old-access")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope="user"), "old-refresh")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope="user"), "100")
    storage.set_secret(oauth_storage_key(config, "refresh_marker", scope="user"), "marker")
    storage.set_secret(oauth_storage_key(config, "client_id", scope="user"), "registered-client")
    storage.set_secret(oauth_storage_key(config, "client_secret", scope="user"), "registered-secret")
    storage.set_secret(oauth_storage_key(config, "client_auth_method", scope="user"), "client_secret_post")
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: oauth_module.OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        raise oauth_module.OAuthTokenError("invalid_client", "registered client is stale", status_code=400)

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    with pytest.raises(MCPNeedsAuthError, match="invalid_client"):
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="old-refresh")

    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


def test_refresh_reauth_error_sanitizes_token_endpoint_description(monkeypatch) -> None:
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
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        raise oauth_module.OAuthTokenError("invalid_token", "refresh_token=super-secret-token", status_code=400)

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    with pytest.raises(MCPNeedsAuthError) as raised:
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="old-refresh")

    message = str(raised.value)
    assert "super-secret-token" not in message
    assert "refresh_token=" not in message
    assert "invalid_token" in message


def test_refresh_reauth_error_omits_raw_token_endpoint_description(monkeypatch) -> None:
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
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )

    def post_token(url: str, data: dict[str, str]) -> dict[str, object]:
        raise oauth_module.OAuthTokenError(
            "invalid_grant",
            "MCP_REFRESH_EXCEPTION_SECRET_29173",
            status_code=400,
        )

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    with pytest.raises(MCPNeedsAuthError) as raised:
        refresh_oauth_access_token(config, storage=storage, scope="user", refresh_token="old-refresh")

    message = str(raised.value)
    assert getattr(raised.value, "auth_error", None) == "invalid_grant"
    assert "invalid_grant" in message
    assert "MCP_REFRESH_EXCEPTION_SECRET_29173" not in message


def _fake_oauth_metadata(oauth_server: Any) -> OAuthMetadata:
    return OAuthMetadata(
        issuer=oauth_server.base_url,
        authorization_endpoint=oauth_server.base_url + "/oauth/authorize",
        token_endpoint=oauth_server.base_url + "/oauth/token",
        registration_endpoint=oauth_server.base_url + "/oauth/register",
        scopes_supported=oauth_server.scopes_supported,
        client_id_metadata_document_supported=oauth_server.client_id_metadata_document_supported,
    )


def test_oauth_loopback_flow_reports_failed_browser_env_as_not_opened(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    browser = tmp_path / "fail_browser.py"
    browser.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    monkeypatch.setenv("BROWSER", "{} {}".format(shlex.quote(sys.executable), shlex.quote(str(browser))))
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config, **_kwargs: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/oauth/authorize",
            token_endpoint="https://auth.example/oauth/token",
            scopes_supported=[],
        ),
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "clientId": "client-id",
                "authServerMetadataUrl": "https://auth.example/.well-known/oauth-authorization-server",
            },
        },
    )

    pending = oauth_module.start_oauth_loopback_flow(
        config,
        storage=MCPSecretStorage(keyring_backend=FakeKeyring()),
        scope="user",
        timeout_seconds=1,
    )

    try:
        assert pending.browser_opened is False
    finally:
        pending.close()


def test_open_browser_env_registered_name_uses_webbrowser_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_urls: list[str] = []
    monkeypatch.setenv("BROWSER", "registered-browser")

    def popen(_command, **_kwargs):
        raise OSError("not executable")

    class FakeWebBrowser:
        @staticmethod
        def get(name: str):
            assert name == "registered-browser"

            class Browser:
                @staticmethod
                def open(url: str) -> bool:
                    opened_urls.append(url)
                    return True

            return Browser()

    monkeypatch.setattr(oauth_module.subprocess, "Popen", popen)
    monkeypatch.setitem(sys.modules, "webbrowser", FakeWebBrowser)

    assert oauth_module._open_browser("https://example.test/oauth") is True
    assert opened_urls == ["https://example.test/oauth"]


def test_browser_env_commands_expand_placeholder_and_append_url() -> None:
    append_commands = oauth_module._browser_env_commands("python -m browser", "https://example.test/oauth")
    placeholder_commands = oauth_module._browser_env_commands("custom-browser %s", "https://example.test/oauth")

    assert append_commands == [["python", "-m", "browser", "https://example.test/oauth"]]
    assert placeholder_commands == [["custom-browser", "https://example.test/oauth"]]


def test_browser_env_commands_strip_windows_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_module.os, "name", "nt")
    monkeypatch.setattr(oauth_module.os, "pathsep", ";")

    commands = oauth_module._browser_env_commands(
        '"C:\\Program Files\\Browser\\browser.exe" "%s"',
        "https://example.test/oauth",
    )

    assert commands == [["C:\\Program Files\\Browser\\browser.exe", "https://example.test/oauth"]]


def test_oauth_loopback_flow_registers_client_when_no_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": oauth_server.base_url + "/mcp",
            "oauth": {"authServerMetadataUrl": oauth_server.metadata_url},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    def open_browser(url: str) -> bool:
        oauth_server.open_authorization_url(url)
        return True

    result = run_oauth_loopback_flow(
        config,
        storage=storage,
        scope="user",
        open_browser=open_browser,
        timeout_seconds=5,
    )

    assert result.access_token_key == oauth_storage_key(config, "access_token", scope="user")
    assert oauth_server.last_registration_request is not None
    redirect_uri = oauth_server.last_authorize_query["redirect_uri"][0]
    assert oauth_server.last_registration_request == {
        "client_name": "IaC Code",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp",
    }
    assert oauth_server.last_token_request["client_id"] == ["registered-client"]
    expected_resource = resource_url_from_server_url(config.url)
    assert oauth_server.last_authorize_query["resource"] == [expected_resource]
    assert oauth_server.last_token_request["resource"] == [expected_resource]
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope="user")) == "registered-client"
    assert storage.get_secret(oauth_storage_key(config, "client_secret", scope="user")) == "registered-client-secret"
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope="user")) == "access-token"


def test_client_metadata_url_loopback_flow_falls_back_to_dcr_when_metadata_does_not_support_cimd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_server = FakeOAuthServer(client_id_metadata_document_supported=False)
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": oauth_server.base_url + "/mcp",
            "oauth": {
                "authServerMetadataUrl": oauth_server.metadata_url,
                "clientMetadataUrl": "https://example.com/clients/iac-code.json",
            },
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    def open_browser(url: str) -> bool:
        oauth_server.open_authorization_url(url)
        return True

    run_oauth_loopback_flow(
        config,
        storage=storage,
        scope="user",
        open_browser=open_browser,
        timeout_seconds=5,
    )

    assert oauth_server.last_registration_request is not None
    assert oauth_server.last_registration_request["client_name"] == "IaC Code"
    assert oauth_server.last_token_request["client_id"] == ["registered-client"]


def test_oauth_loopback_flow_registers_client_with_selected_mcp_scope_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_server = FakeOAuthServer(scopes_supported=["profile", "mcp"])
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": oauth_server.base_url + "/mcp",
            "oauth": {"authServerMetadataUrl": oauth_server.metadata_url},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    def open_browser(url: str) -> bool:
        oauth_server.open_authorization_url(url)
        return True

    run_oauth_loopback_flow(
        config,
        storage=storage,
        scope="user",
        open_browser=open_browser,
        timeout_seconds=5,
    )

    assert oauth_server.last_registration_request is not None
    assert oauth_server.last_registration_request["scope"] == "mcp"
    assert oauth_server.last_authorize_query["scope"] == ["mcp"]


def test_oauth_loopback_flow_uses_required_scope_instead_of_advertised_scope_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_server = FakeOAuthServer(scopes_supported=["mcp.read", "mcp.write", "profile"])
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": oauth_server.base_url + "/mcp",
            "oauth": {"authServerMetadataUrl": oauth_server.metadata_url},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    def open_browser(url: str) -> bool:
        oauth_server.open_authorization_url(url)
        return True

    run_oauth_loopback_flow(
        config,
        storage=storage,
        scope="user",
        required_scopes=["mcp.write"],
        open_browser=open_browser,
        timeout_seconds=5,
    )

    assert oauth_server.last_registration_request is not None
    assert oauth_server.last_registration_request["scope"] == "mcp.write"
    assert oauth_server.last_authorize_query["scope"] == ["mcp.write"]


def test_oauth_loopback_flow_omits_advertised_scope_union_without_required_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_server = FakeOAuthServer(scopes_supported=["openid", "profile", "email", "admin"])
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": oauth_server.base_url + "/mcp",
            "oauth": {"authServerMetadataUrl": oauth_server.metadata_url},
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())

    def open_browser(url: str) -> bool:
        oauth_server.open_authorization_url(url)
        return True

    run_oauth_loopback_flow(
        config,
        storage=storage,
        scope="user",
        open_browser=open_browser,
        timeout_seconds=5,
    )

    assert oauth_server.last_registration_request is not None
    assert "scope" not in oauth_server.last_registration_request
    assert "scope" not in oauth_server.last_authorize_query


def test_static_oauth_loopback_flow_closes_callback_when_auth_marker_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = int(port_socket.getsockname()[1])

    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "clientId": "client-id",
                "callbackPort": port,
                "authServerMetadataUrl": "https://auth.example/.well-known/oauth-authorization-server",
            },
        },
    )
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda _config, **_kwargs: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
    )
    monkeypatch.setattr(
        oauth_module,
        "_begin_oauth_auth_flow_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("marker failed")),
    )

    with pytest.raises(RuntimeError, match="marker failed"):
        oauth_module.start_oauth_loopback_flow(config, storage=storage, scope="user", open_browser=lambda _url: False)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_oauth_pending_flow_submit_manually_wakes_existing_waiter_without_closing() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    submitted: list[tuple[str, str | None]] = []
    closed: list[bool] = []

    class Callback:
        expected_state = "expected-state"

        def complete_manually(self, code: str, state: str | None = None) -> None:
            submitted.append((code, state))

        def close(self) -> None:
            closed.append(True)

    pending = OAuthPendingFlow(
        config=config,
        storage=MCPSecretStorage(keyring_backend=FakeKeyring()),
        metadata=OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
        callback=cast(Any, Callback()),
        redirect_uri="http://127.0.0.1:3123/callback",
        authorization_url="https://auth.example/authorize?state=expected-state",
        verifier="verifier",
        scope="user",
    )

    pending.submit_manually("http://127.0.0.1:3123/callback?code=code-1&state=expected-state")

    assert submitted == [("code-1", "expected-state")]
    assert closed == []


def test_oauth_pending_flow_closes_callback_on_manual_state_error() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    closed: list[bool] = []

    class Callback:
        expected_state = "expected-state"

        def close(self) -> None:
            closed.append(True)

    pending = OAuthPendingFlow(
        config=config,
        storage=MCPSecretStorage(keyring_backend=FakeKeyring()),
        metadata=OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            scopes_supported=[],
        ),
        callback=cast(Any, Callback()),
        redirect_uri="http://127.0.0.1:3123/callback",
        authorization_url="https://auth.example/authorize?state=expected-state",
        verifier="verifier",
        scope="user",
    )

    with pytest.raises(RuntimeError, match="state"):
        pending.complete_manually("http://127.0.0.1:3123/callback?code=code-1&state=wrong-state")

    assert closed == [True]


def test_dynamic_oauth_worker_routes_sdk_oauth_flow_traceback_to_log_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()
    setup_logging(session_id="issue62", debug=True)
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://resource.example/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    metadata = OAuthMetadata(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        registration_endpoint="https://auth.example/register",
        scopes_supported=[],
    )
    closed: list[bool] = []

    class Callback:
        expected_state = "expected-state"

        def close(self) -> None:
            closed.append(True)

    async def failing_sdk_flow(*_args: object, redirect_handler, **_kwargs: object) -> object:
        await redirect_handler("https://auth.example/authorize?state=expected-state")
        try:
            raise RuntimeError("sdk exploded")
        except RuntimeError:
            logging.getLogger("mcp.client.auth.oauth2").exception("OAuth flow error")
            raise

    monkeypatch.setattr(oauth_module, "_run_sdk_oauth_flow", failing_sdk_flow)

    pending = oauth_module._start_dynamic_oauth_loopback_flow(
        config,
        storage=storage,
        scope=MCPConfigScope.USER,
        metadata=metadata,
        callback=cast(Any, Callback()),
        redirect_uri="http://127.0.0.1:3123/callback",
        required_scopes=None,
        open_browser=lambda _url: False,
        timeout_seconds=1,
    )

    with pytest.raises(RuntimeError, match="sdk exploded"):
        pending.wait()
    logger.complete()

    captured = capsys.readouterr()
    assert "OAuth flow error" not in captured.err
    assert "Traceback" not in captured.err
    log_text = (tmp_path / "logs" / "issue62.log").read_text(encoding="utf-8")
    assert "OAuth flow error" in log_text
    assert "RuntimeError: sdk exploded" in log_text
    assert closed


def test_clear_oauth_state_deletes_local_state_even_when_revocation_fails() -> None:
    keyring = FakeKeyring()
    storage = MCPSecretStorage(keyring_backend=keyring)
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    for kind, value in {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": "100",
        "refresh_marker": "marker",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)

    def revoke(_token: str) -> None:
        raise RuntimeError("revocation failed")

    clear_oauth_state(config, storage=storage, scope="user", revoke=revoke)

    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope="user")) is None


def test_revoke_oauth_stored_tokens_posts_access_and_refresh_tokens(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    for kind, value in {
        "access_token": "stored-access",
        "refresh_token": "stored-refresh",
        "client_id": "registered-client",
        "client_secret": "registered-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope="user"), value)
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/oauth/authorize",
            token_endpoint="https://auth.example/oauth/token",
            revocation_endpoint="https://auth.example/oauth/revoke",
            requires_public_endpoints=True,
        ),
    )
    requests: list[tuple[str, dict[str, str], dict[str, str] | None, bool]] = []

    def post_revocation_request(
        url: str,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        validate_public_endpoint: bool = False,
    ) -> None:
        requests.append((url, data, headers, validate_public_endpoint))

    monkeypatch.setattr(oauth_module, "_post_revocation_request", post_revocation_request)

    warnings = oauth_module.revoke_oauth_stored_tokens(config, storage=storage, scope="user")

    assert warnings == []
    assert requests == [
        (
            "https://auth.example/oauth/revoke",
            {
                "token": "stored-access",
                "token_type_hint": "access_token",
                "client_id": "registered-client",
                "client_secret": "registered-secret",
            },
            {},
            True,
        ),
        (
            "https://auth.example/oauth/revoke",
            {
                "token": "stored-refresh",
                "token_type_hint": "refresh_token",
                "client_id": "registered-client",
                "client_secret": "registered-secret",
            },
            {},
            True,
        ),
    ]


def test_revoke_oauth_stored_tokens_returns_sanitized_warning_on_failure(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage(keyring_backend=FakeKeyring())
    storage.set_secret(oauth_storage_key(config, "access_token", scope="user"), "stored-access")
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/oauth/authorize",
            token_endpoint="https://auth.example/oauth/token",
            revocation_endpoint="https://auth.example/oauth/revoke",
            requires_public_endpoints=True,
        ),
    )

    def post_revocation_request(
        url: str,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        validate_public_endpoint: bool = False,
    ) -> None:
        raise RuntimeError("revocation failed for access_token=stored-access")

    monkeypatch.setattr(oauth_module, "_post_revocation_request", post_revocation_request)

    warnings = oauth_module.revoke_oauth_stored_tokens(config, storage=storage, scope="user")

    assert len(warnings) == 1
    assert "OAuth token revocation failed for MCP server 'remote'" in warnings[0]
    assert "stored-access" not in warnings[0]


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


class FakeOAuthServer:
    def __init__(
        self,
        *,
        scopes_supported: list[str] | None = None,
        client_id_metadata_document_supported: bool | None = None,
    ) -> None:
        self.last_registration_request: dict[str, object] | None = None
        self.last_authorize_query: dict[str, list[str]] = {}
        self.last_token_request: dict[str, list[str]] = {}
        self.scopes_supported = scopes_supported or ["mcp"]
        self.client_id_metadata_document_supported = client_id_metadata_document_supported
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self._server.server_address[1])
        self.metadata_url = self.base_url + "/.well-known/oauth-authorization-server"

    def open_authorization_url(self, url: str) -> None:
        urlopen(url, timeout=5).read()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path in {
                    "/.well-known/oauth-protected-resource",
                    "/.well-known/oauth-protected-resource/mcp",
                }:
                    self._json(
                        {
                            "resource": outer.base_url,
                            "authorization_servers": [outer.base_url],
                            "scopes_supported": outer.scopes_supported,
                        }
                    )
                    return
                if parsed.path == "/.well-known/oauth-authorization-server":
                    payload: dict[str, object] = {
                        "issuer": outer.base_url,
                        "authorization_endpoint": outer.base_url + "/oauth/authorize",
                        "token_endpoint": outer.base_url + "/oauth/token",
                        "registration_endpoint": outer.base_url + "/oauth/register",
                        "scopes_supported": outer.scopes_supported,
                    }
                    if outer.client_id_metadata_document_supported is not None:
                        payload["client_id_metadata_document_supported"] = outer.client_id_metadata_document_supported
                    self._json(payload)
                    return
                if parsed.path == "/oauth/authorize":
                    query = parse_qs(parsed.query)
                    outer.last_authorize_query = query
                    callback_url = "{}?{}".format(
                        query["redirect_uri"][0],
                        urlencode({"code": "code-1", "state": query["state"][0]}),
                    )
                    urlopen(callback_url, timeout=5).read()
                    self._json({"ok": True})
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                if self.path == "/oauth/register":
                    outer.last_registration_request = json.loads(body)
                    self._json(
                        {
                            "client_id": "registered-client",
                            "client_secret": "registered-client-secret",
                        },
                        status=201,
                    )
                    return
                if self.path == "/oauth/token":
                    outer.last_token_request = parse_qs(body)
                    self._json(
                        {
                            "access_token": "access-token",
                            "refresh_token": "refresh-token",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                        }
                    )
                    return
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _json(self, payload: dict[str, object], *, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
