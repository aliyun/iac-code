from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence, cast

from iac_code.i18n import _


class MCPConfigError(ValueError):
    """Raised when an MCP server config cannot be normalized."""


class MCPTransport(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def from_value(cls, value: str, *, server_name: str) -> "MCPTransport":
        try:
            return cls(value)
        except ValueError as exc:
            supported = ", ".join(transport.value for transport in cls)
            raise MCPConfigError(
                _(
                    "Unsupported MCP transport {transport!r} for server {server!r}. Supported transports: {supported}."
                ).format(transport=value, server=server_name, supported=supported)
            ) from exc


class MCPConfigScope(str, Enum):
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    SESSION = "session"
    DYNAMIC = "dynamic"

    @property
    def precedence(self) -> int:
        return {
            "user": 10,
            "project": 20,
            "local": 30,
            "session": 40,
            "dynamic": 40,
        }[self.value]


class MCPConnectionState(str, Enum):
    CONNECTED = "connected"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    PENDING = "pending"
    DISABLED = "disabled"


@dataclass(frozen=True)
class MCPConfigWarning:
    source: str
    message: str
    server_name: str | None = None
    code: str = "warning"


@dataclass(frozen=True)
class MCPOAuthConfig:
    client_id: str | None = None
    client_secret_env: str | None = None
    callback_port: int | None = None
    auth_server_metadata_url: str | None = None

    @classmethod
    def from_mapping(cls, server_name: str, value: object) -> "MCPOAuthConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise MCPConfigError(_("MCP server {server!r} oauth config must be an object.").format(server=server_name))
        data = cast(Mapping[str, Any], value)

        if "clientSecret" in data:
            raise MCPConfigError(
                _(
                    "MCP server {server!r} uses oauth.clientSecret, but plaintext client secrets are not supported. "
                    "Use oauth.clientSecretEnv instead."
                ).format(server=server_name)
            )

        supported = {"clientId", "clientSecretEnv", "callbackPort", "authServerMetadataUrl"}
        unknown = sorted(str(key) for key in data if key not in supported)
        if unknown:
            raise MCPConfigError(
                _("MCP server {server!r} has unsupported oauth fields: {fields}.").format(
                    server=server_name,
                    fields=", ".join(unknown),
                )
            )

        callback_port = data.get("callbackPort")
        if callback_port is not None and not isinstance(callback_port, int):
            raise MCPConfigError(
                _("MCP server {server!r} oauth.callbackPort must be an integer.").format(server=server_name)
            )

        return cls(
            client_id=_optional_str(data.get("clientId"), "oauth.clientId", server_name),
            client_secret_env=_optional_str(data.get("clientSecretEnv"), "oauth.clientSecretEnv", server_name),
            callback_port=callback_port,
            auth_server_metadata_url=_optional_str(
                data.get("authServerMetadataUrl"),
                "oauth.authServerMetadataUrl",
                server_name,
            ),
        )


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: MCPTransport
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    oauth: MCPOAuthConfig | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, name: str, value: Mapping[str, Any]) -> "MCPServerConfig":
        if not isinstance(value, Mapping):
            raise MCPConfigError(_("MCP server {server!r} config must be an object.").format(server=name))

        _reject_unsupported_fields(name, value)
        type_value = value.get("type")
        if type_value is None:
            if "command" in value:
                type_value = MCPTransport.STDIO.value
            else:
                raise MCPConfigError(
                    _("MCP server {server!r} requires a type unless a stdio command is provided.").format(server=name)
                )
        if not isinstance(type_value, str):
            raise MCPConfigError(_("MCP server {server!r} type must be a string.").format(server=name))

        transport = MCPTransport.from_value(type_value, server_name=name)
        env = _string_mapping(value.get("env", {}), "env", name)
        headers = _string_mapping(value.get("headers", {}), "headers", name)
        oauth = None
        if "oauth" in value:
            oauth = MCPOAuthConfig.from_mapping(name, value.get("oauth"))

        if transport is MCPTransport.STDIO:
            command = _required_str(value.get("command"), "command", name)
            return cls(
                name=name,
                transport=transport,
                command=command,
                args=_string_sequence(value.get("args", ()), "args", name),
                env=env,
                oauth=oauth,
                raw=dict(value),
            )

        url = _required_str(value.get("url"), "url", name)
        return cls(
            name=name,
            transport=transport,
            url=url,
            headers=headers,
            oauth=oauth,
            raw=dict(value),
        )

    def content_signature(self) -> str:
        oauth = None
        if self.oauth is not None:
            oauth = {
                "clientId": self.oauth.client_id,
                "clientCredentialEnvConfigured": self.oauth.client_secret_env is not None,
                "callbackPort": self.oauth.callback_port,
                "authServerMetadataUrl": self.oauth.auth_server_metadata_url,
            }
        material = {
            "transport": self.transport.value,
            "command": self.command,
            "args": list(self.args),
            "env": _signature_mapping(self.env),
            "url": self.url,
            "headers": _signature_mapping(self.headers),
            "oauth": oauth,
        }
        if self.transport is MCPTransport.STDIO:
            prefix = "stdio"
        else:
            prefix = "url"
        import hashlib
        import json

        data = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.pbkdf2_hmac("sha256", data, b"iac-code-mcp-config-signature-v1", 100_000).hex()
        return "{}:{}".format(prefix, digest)


@dataclass(frozen=True)
class ScopedMCPServerConfig:
    config: MCPServerConfig
    scope: MCPConfigScope
    source_path: str | None = None
    approved: bool = True
    warning: MCPConfigWarning | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def transport(self) -> MCPTransport:
        return self.config.transport

    @property
    def precedence(self) -> int:
        return self.scope.precedence


@dataclass(frozen=True)
class MCPToolRecord:
    server_name: str
    tool_name: str
    public_name: str
    description: str | None = None
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    annotations: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPResourceRecord:
    server_name: str
    uri: str
    name: str | None = None
    public_name: str | None = None
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    annotations: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_skill_resource(self) -> bool:
        return self.uri.startswith("skill://")


@dataclass(frozen=True)
class MCPPromptRecord:
    server_name: str
    prompt_name: str
    public_name: str
    description: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPSkillRecord:
    server_name: str
    name: str
    public_name: str
    resource_uri: str
    description: str | None = None
    mime_type: str | None = "text/markdown"
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPConnectionMetadata:
    state: MCPConnectionState
    server_name: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    server_info: Mapping[str, Any] = field(default_factory=dict)
    instructions: str | None = None
    stderr_tail: str | None = None
    retry_count: int = 0
    config_signature: str | None = None


def _reject_unsupported_fields(server_name: str, value: Mapping[str, Any]) -> None:
    supported = {"type", "command", "args", "env", "url", "headers", "oauth"}
    unsupported = sorted(str(key) for key in value if key not in supported)
    if not unsupported:
        return

    if "headersHelper" in unsupported:
        raise MCPConfigError(
            _(
                "MCP server {server!r} uses unsupported field 'headersHelper'. Static headers are supported; "
                "dynamic headers need a later trusted-execution design."
            ).format(server=server_name)
        )

    raise MCPConfigError(
        _("MCP server {server!r} has unsupported config fields: {fields}.").format(
            server=server_name,
            fields=", ".join(unsupported),
        )
    )


_SIGNATURE_REDACTED_VALUE = "[redacted]"
_SENSITIVE_CONFIG_NAME_PARTS = (
    "authorization",
    "api-key",
    "apikey",
    "accesskeysecret",
    "access_key_secret",
    "client_secret",
    "password",
    "secret",
    "token",
)
_SENSITIVE_CONFIG_VALUE_PREFIXES = ("bearer ", "basic ")


def _signature_mapping(values: Mapping[str, str]) -> dict[str, str]:
    return {
        key: _SIGNATURE_REDACTED_VALUE if _is_sensitive_config_entry(key, value) else value
        for key, value in values.items()
    }


def _is_sensitive_config_entry(key: str, value: str) -> bool:
    normalized_key = key.replace("-", "_").lower()
    if any(part in normalized_key for part in _SENSITIVE_CONFIG_NAME_PARTS):
        return True
    normalized_value = value.strip().lower()
    return normalized_value.startswith(_SENSITIVE_CONFIG_VALUE_PREFIXES)


def _required_str(value: object, field_name: str, server_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MCPConfigError(
            _("MCP server {server!r} requires a {field} string.").format(server=server_name, field=field_name)
        )
    return value


def _optional_str(value: object, field_name: str, server_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPConfigError(
            _("MCP server {server!r} field {field} must be a string.").format(
                server=server_name,
                field=field_name,
            )
        )
    return value


def _string_sequence(value: object, field_name: str, server_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise MCPConfigError(
            _("MCP server {server!r} field {field} must be a list of strings.").format(
                server=server_name,
                field=field_name,
            )
        )
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MCPConfigError(
                _("MCP server {server!r} field {field} must be a list of strings.").format(
                    server=server_name,
                    field=field_name,
                )
            )
        result.append(item)
    return tuple(result)


def _string_mapping(value: object, field_name: str, server_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MCPConfigError(
            _("MCP server {server!r} field {field} must be an object of string values.").format(
                server=server_name,
                field=field_name,
            )
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise MCPConfigError(
                _("MCP server {server!r} field {field} must be an object of string values.").format(
                    server=server_name,
                    field=field_name,
                )
            )
        result[key] = item
    return result
