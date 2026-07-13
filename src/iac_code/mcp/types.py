from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence, cast
from urllib.parse import urlparse

from iac_code.i18n import _
from iac_code.mcp.redaction import sanitize_mcp_public_text
from iac_code.utils.public_errors import sanitize_public_text

MCP_INITIALIZE_INSTRUCTIONS_MAX_CHARS = 4000
MCP_INSTRUCTIONS_TRUNCATION_MARKER = "[truncated]"


class MCPConfigError(ValueError):
    """Raised when an MCP server config cannot be normalized."""


class MCPTransport(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"
    WS = "ws"

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
    client_metadata_url: str | None = None

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

        supported = {"clientId", "clientSecretEnv", "callbackPort", "authServerMetadataUrl", "clientMetadataUrl"}
        unknown = sorted(str(key) for key in data if key not in supported)
        if unknown:
            raise MCPConfigError(
                _("MCP server {server!r} has unsupported oauth fields: {fields}.").format(
                    server=server_name,
                    fields=", ".join(unknown),
                )
            )

        callback_port = data.get("callbackPort")
        if callback_port is not None and type(callback_port) is not int:
            raise MCPConfigError(
                _("MCP server {server!r} oauth.callbackPort must be an integer.").format(server=server_name)
            )
        if callback_port is not None and not 0 <= callback_port <= 65535:
            raise MCPConfigError(
                _("MCP server {server!r} oauth.callbackPort must be between 0 and 65535.").format(server=server_name)
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
            client_metadata_url=_optional_str(data.get("clientMetadataUrl"), "oauth.clientMetadataUrl", server_name),
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
    headers_helper: str | None = None
    oauth: MCPOAuthConfig | None = None
    source_dir: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        name: str,
        value: Mapping[str, Any],
        *,
        validate_headers_helper_plaintext: bool = True,
    ) -> "MCPServerConfig":
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
        headers_helper = _optional_str(value.get("headersHelper"), "headersHelper", name)
        if validate_headers_helper_plaintext:
            validate_headers_helper_no_plaintext_secret(headers_helper)
        oauth = None
        if "oauth" in value:
            oauth = MCPOAuthConfig.from_mapping(name, value.get("oauth"))

        if transport is MCPTransport.STDIO:
            if headers_helper is not None:
                raise MCPConfigError(
                    _(
                        "MCP server {server!r} field headersHelper is only supported for http and sse transports."
                    ).format(server=name)
                )
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
        if transport is MCPTransport.WS:
            _validate_websocket_url(name, url)
            _reject_websocket_unsupported_options(name, value)
        return cls(
            name=name,
            transport=transport,
            url=url,
            headers=headers,
            headers_helper=headers_helper,
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
                "clientMetadataUrl": self.oauth.client_metadata_url,
            }
        material = {
            "transport": self.transport.value,
            "command": self.command,
            "args": list(self.args),
            "env": _signature_mapping(self.env),
            "url": self.url,
            "headers": _signature_mapping(self.headers),
            "headersHelper": self.headers_helper,
            "oauth": oauth,
        }
        if self.raw and self.command is None and self.url is None:
            material["raw"] = self.raw
        if self.transport is MCPTransport.STDIO:
            prefix = "stdio"
        else:
            prefix = "url"
        import hashlib
        import json

        data = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        digest = hashlib.pbkdf2_hmac("sha256", data, b"iac-code-mcp-config-signature-v1", 100_000).hex()
        return "{}:{}".format(prefix, digest)


@dataclass(frozen=True)
class ScopedMCPServerConfig:
    config: MCPServerConfig
    scope: MCPConfigScope
    source_path: str | None = None
    approved: bool = True
    disabled: bool = False
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
    original_server_name: str | None = None
    original_tool_name: str | None = None
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
    original_server_name: str | None = None
    original_resource_name: str | None = None
    original_skill_name: str | None = None
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
    original_server_name: str | None = None
    original_prompt_name: str | None = None
    description: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPSkillRecord:
    server_name: str
    name: str
    public_name: str
    resource_uri: str
    original_server_name: str | None = None
    original_skill_name: str | None = None
    description: str | None = None
    mime_type: str | None = "text/markdown"
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPConnectionMetadata:
    state: MCPConnectionState
    server_name: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    server_info: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str | None = None
    instructions: str | None = None
    stderr_tail: str | None = None
    retry_count: int = 0
    config_signature: str | None = None


def normalize_initialize_metadata(
    server_name: str,
    initialize_result: Any,
    *,
    state: MCPConnectionState = MCPConnectionState.CONNECTED,
    stderr_tail: str | None = None,
    retry_count: int = 0,
    config_signature: str | None = None,
) -> MCPConnectionMetadata:
    """Normalize MCP initialize metadata from SDK objects, dicts, or model dumps."""
    root = _object_mapping(initialize_result)
    instructions = _normalize_instruction_text(_get_any(root, initialize_result, "instructions"))
    capabilities = _plain_mapping(_get_any(root, initialize_result, "capabilities"))
    server_info = _plain_mapping(_get_any(root, initialize_result, "serverInfo", "server_info"))
    protocol_version = _normalize_protocol_version(
        _get_any(root, initialize_result, "protocolVersion", "protocol_version")
    )
    return MCPConnectionMetadata(
        state=state,
        server_name=server_name,
        capabilities=capabilities,
        server_info=server_info,
        protocol_version=protocol_version,
        instructions=instructions,
        stderr_tail=stderr_tail,
        retry_count=retry_count,
        config_signature=config_signature,
    )


def _normalize_protocol_version(value: object) -> str | None:
    if value is None:
        return None
    text = _plain_metadata_string(str(value))
    return text or None


def bounded_public_instruction_text(
    value: object,
    *,
    max_chars: int = MCP_INITIALIZE_INSTRUCTIONS_MAX_CHARS,
) -> str | None:
    return _normalize_instruction_text(value, max_chars=max_chars)


def _normalize_instruction_text(
    value: object,
    *,
    max_chars: int = MCP_INITIALIZE_INSTRUCTIONS_MAX_CHARS,
) -> str | None:
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    text = sanitize_mcp_public_text(value, fallback_summary="").strip()
    if not text:
        return None
    return _truncate_text(text, max_chars=max_chars)


def _truncate_text(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = MCP_INSTRUCTIONS_TRUNCATION_MARKER
    if max_chars <= len(marker):
        return marker[:max_chars]
    return text[: max_chars - len(marker)].rstrip() + marker


def _plain_mapping(value: object) -> dict[str, Any]:
    plain = _plain_metadata_value(value)
    return plain if isinstance(plain, dict) else {}


def _plain_metadata_value(value: object, *, depth: int = 0) -> Any:
    if depth > 12:
        return "[truncated-depth]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _plain_metadata_string(value)
    if isinstance(value, Mapping):
        return {
            str(key): _plain_metadata_value(item, depth=depth + 1)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_plain_metadata_value(item, depth=depth + 1) for item in value]
    mapped = _object_mapping(value)
    if mapped:
        return _plain_metadata_value(mapped, depth=depth + 1)
    return _plain_metadata_string(str(value))


def _plain_metadata_string(value: str) -> str:
    if not value.strip():
        return ""
    return _truncate_text(sanitize_public_text(value, fallback_summary=""), max_chars=1000)


def _object_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        for kwargs in ({"by_alias": True, "mode": "json"}, {"by_alias": True}, {}):
            try:
                dumped = model_dump(**kwargs)
            except TypeError:
                continue
            except Exception:
                break
            if isinstance(dumped, Mapping):
                return cast(Mapping[str, Any], dumped)
    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        try:
            dumped = dict_method()
        except Exception:
            dumped = None
        if isinstance(dumped, Mapping):
            return cast(Mapping[str, Any], dumped)
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, Mapping):
        return cast(Mapping[str, Any], attrs)
    return {}


def _get_any(mapping: Mapping[str, Any], source: object, *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    for key in keys:
        attr = getattr(source, key, None)
        if attr is not None:
            return attr
    return None


def _reject_unsupported_fields(server_name: str, value: Mapping[str, Any]) -> None:
    supported = {"type", "command", "args", "env", "url", "headers", "headersHelper", "oauth"}
    unsupported = sorted(str(key) for key in value if key not in supported)
    if not unsupported:
        return

    raise MCPConfigError(
        _("MCP server {server!r} has unsupported config fields: {fields}.").format(
            server=server_name,
            fields=", ".join(unsupported),
        )
    )


def _validate_websocket_url(server_name: str, url: str) -> None:
    try:
        parsed = urlparse(url)
        valid = parsed.scheme in {"ws", "wss"} and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise MCPConfigError(
            _("MCP server {server!r} WebSocket transport url must be a ws:// or wss:// URL with a host.").format(
                server=server_name
            )
        )


def _reject_websocket_unsupported_options(server_name: str, value: Mapping[str, Any]) -> None:
    for field_name in ("headers", "headersHelper", "oauth"):
        if field_name not in value:
            continue
        raise MCPConfigError(
            _(
                "MCP server {server!r} WebSocket transport field {field} is not supported because the installed "
                "MCP SDK websocket_client accepts only a URL."
            ).format(server=server_name, field=field_name)
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
_HELPER_ENV_REFERENCE_FULL_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-(?P<default>[^}]*))?\}$")
_HELPER_AUTH_VALUE_RE = re.compile(r"\b(?:bearer|basic)\s+([^\s,;'\"`]+)", re.IGNORECASE)
_HELPER_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;\s&,])(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|authorization|password|secret|"
    r"session|sid|jwt)=([^;\s&,'\"`]+)",
    re.IGNORECASE,
)
_HELPER_SECRET_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_CONFIG_ENV_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")
_CONFIG_ENV_REFERENCE_WITH_DEFAULT_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-(?P<default>[^}]*)\}")
_CONFIG_SENSITIVE_NAME_MARKERS = (
    "authorization",
    "auth",
    "api-key",
    "api_key",
    "apikey",
    "accesskeysecret",
    "access_key_secret",
    "client_secret",
    "cookie",
    "password",
    "secret",
    "session",
    "set-cookie",
    "token",
)


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


def headers_helper_contains_plaintext_secret(value: str) -> bool:
    if _helper_secret_like_value(value):
        return True
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    for index, token in enumerate(tokens):
        flag, separator, flag_value = token.partition("=")
        if separator and _helper_sensitive_key(flag.lstrip("-")) and _helper_plaintext_token_value(flag_value):
            return True
        if token.startswith("-") and _helper_sensitive_key(token.lstrip("-")) and index + 1 < len(tokens):
            next_token = tokens[index + 1]
            if not next_token.startswith("-") and _helper_plaintext_token_value(next_token):
                return True
    return False


def validate_headers_helper_no_plaintext_secret(value: str | None) -> None:
    if value is None or not headers_helper_contains_plaintext_secret(value):
        return
    raise MCPConfigError(
        _(
            "MCP {section} {key!r} may contain a secret; use an environment variable reference "
            "like ${{VAR}} instead of storing plaintext."
        ).format(section="headersHelper", key="command")
    )


def validate_mcp_config_no_plaintext_secrets(
    config: Mapping[str, Any],
    *,
    reject_plaintext_values: bool = True,
) -> None:
    for section in ("headers", "env"):
        values = config.get(section)
        if not isinstance(values, Mapping):
            continue
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if _CONFIG_ENV_REFERENCE_RE.search(value) is not None:
                if _config_env_reference_has_plaintext_secret_default(key, value):
                    _raise_plaintext_secret_config_error(section, key)
                continue
            if reject_plaintext_values and (_config_sensitive_key(key) or _config_secret_like_value(value)):
                _raise_plaintext_secret_config_error(section, key)
    headers_helper = config.get("headersHelper")
    if isinstance(headers_helper, str):
        validate_headers_helper_no_plaintext_secret(headers_helper)


def _config_env_reference_has_plaintext_secret_default(key: str, value: str) -> bool:
    for match in _CONFIG_ENV_REFERENCE_WITH_DEFAULT_RE.finditer(value):
        default = match.group("default")
        if _config_sensitive_key(key) or _config_secret_like_value(default):
            return True
    return False


def _config_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace(" ", "").replace("_", "-")
    alternate = key.lower().replace(" ", "")
    return any(marker in normalized or marker in alternate for marker in _CONFIG_SENSITIVE_NAME_MARKERS)


def _config_secret_like_value(value: str) -> bool:
    if _CONFIG_ENV_REFERENCE_RE.search(value) is not None:
        return False
    return _helper_secret_like_value(value)


def _raise_plaintext_secret_config_error(section: str, key: str) -> None:
    section_label = "header" if section == "headers" else "env"
    raise MCPConfigError(
        _(
            "MCP {section} {key!r} may contain a secret; use an environment variable reference "
            "like ${{VAR}} instead of storing plaintext."
        ).format(section=section_label, key=key)
    )


def _helper_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace(" ", "").replace("_", "-")
    alternate = key.lower().replace(" ", "")
    return any(marker in normalized or marker in alternate for marker in _SENSITIVE_CONFIG_NAME_PARTS)


def _helper_secret_like_value(value: str) -> bool:
    for match in _HELPER_AUTH_VALUE_RE.finditer(value):
        if _helper_plaintext_token_value(match.group(1)):
            return True
    for match in _HELPER_SECRET_ASSIGNMENT_RE.finditer(value):
        if _helper_plaintext_token_value(match.group(1)):
            return True
    return _HELPER_SECRET_KEY_RE.search(value) is not None


def _helper_plaintext_token_value(value: str) -> bool:
    if not value:
        return False
    env_match = _HELPER_ENV_REFERENCE_FULL_RE.fullmatch(value)
    if env_match is not None:
        return bool(env_match.group("default"))
    return True


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
