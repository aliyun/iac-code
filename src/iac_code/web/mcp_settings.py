"""Web API helpers for MCP server management.

This module is a thin adapter over the REPL/CLI MCP service layer
(:mod:`iac_code.mcp`). It intentionally reuses the existing config read/write,
health-check, capability discovery, and OAuth flows rather than re-implementing
them, so the web console stays in lock-step with the ``/mcp`` command and the
``iac-code mcp`` CLI.
"""

from __future__ import annotations

import io
import threading
import uuid
from contextlib import redirect_stderr, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from iac_code.mcp.cli import (
    _checked_server_payload,
    _health_diagnostic_for_persisted_warning,
    _health_warning_for_entry,
    _persisted_entry_listing_identity,
    _redact_public_config,
    _run_reconnect_checks,
    _scoped_config_for_health_check,
    _scoped_server_listing_identity,
    cancel_pending_mcp_oauth_flow,
    reauthenticate_mcp_server,
    remove_mcp_server_command,
    reset_mcp_auth_server_command,
    start_mcp_oauth_flow,
)
from iac_code.mcp.config import (
    approve_project_mcp_server,
    disable_mcp_server,
    enable_mcp_server,
    find_project_mcp_server_file,
    list_persisted_mcp_server_entries,
    load_all_persisted_mcp_configs,
    load_exact_mcp_config,
    reject_project_mcp_server,
    resolve_mcp_workspace_root,
    write_mcp_server_config,
)
from iac_code.mcp.manager import (
    MCPHealthDiagnostic,
    MCPManager,
    health_diagnostic_for_config,
    health_diagnostic_for_record,
)
from iac_code.mcp.types import (
    MCPConfigError,
    MCPConfigScope,
    MCPPromptRecord,
    MCPResourceRecord,
    MCPServerConfig,
    MCPToolRecord,
    validate_mcp_config_no_plaintext_secrets,
)

_PERSISTABLE_SCOPES = {
    MCPConfigScope.USER: "user",
    MCPConfigScope.LOCAL: "local",
    MCPConfigScope.PROJECT: "project",
}


class MCPWebError(Exception):
    """User-facing MCP web error carrying an HTTP status code."""

    def __init__(self, message: str, *, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def _parse_scope(scope: str | None) -> MCPConfigScope:
    if not scope:
        raise MCPWebError("MCP scope is required.")
    try:
        parsed = MCPConfigScope(scope)
    except ValueError as exc:
        raise MCPWebError(
            "Invalid MCP scope {scope!r}. Valid values: user, local, project.".format(scope=scope)
        ) from exc
    if parsed not in _PERSISTABLE_SCOPES:
        raise MCPWebError("Scope {scope!r} cannot be used for persisted MCP config.".format(scope=scope))
    return parsed


def _resolve_scope_default(cwd: Path, scope: str | None) -> MCPConfigScope:
    if scope:
        return _parse_scope(scope)
    root = resolve_mcp_workspace_root(cwd)
    if (root / ".git").exists() or (root / ".mcp.json").exists() or (root / ".iac-code").exists():
        return MCPConfigScope.LOCAL
    return MCPConfigScope.USER


def _source_path_arg(source_path: str | None) -> str | None:
    value = (source_path or "").strip()
    return value or None


def _run_cli(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a CLI wrapper, converting ``typer.Exit`` into :class:`MCPWebError`.

    The CLI wrappers signal failures with ``_fail`` which echoes the message to
    stderr and raises ``typer.Exit``. We capture stderr to surface a meaningful
    message to the web client.
    """

    buffer = io.StringIO()
    try:
        with redirect_stderr(buffer):
            return func(*args, **kwargs)
    except typer.Exit as exc:
        message = buffer.getvalue().strip() or "MCP operation failed."
        raise MCPWebError(message) from exc
    except MCPConfigError as exc:
        raise MCPWebError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _editable_config(raw_config: Any) -> dict[str, Any]:
    """Return the persisted config for editing, sanitising url/command text.

    Persisted configs never contain detected plaintext secrets (they are
    rejected on write), so env/header ``${VAR}`` references and OAuth env-var
    names are safe to surface for editing.
    """

    if not isinstance(raw_config, dict):
        return {}
    return _redact_public_config(dict(raw_config))


def _offline_diagnostics(
    result: Any,
    raw_entries: list[Any],
) -> list[MCPHealthDiagnostic]:
    """Build per-server diagnostics without connecting (mirrors CLI listing)."""

    loaded = {
        _scoped_server_listing_identity(server): server
        for server in [*result.servers, *result.pending]
    }
    diagnostics: list[MCPHealthDiagnostic] = []
    represented: set[Any] = set()
    for entry in raw_entries:
        identity = _persisted_entry_listing_identity(entry)
        represented.add(identity)
        warning = _health_warning_for_entry(entry, result)
        if warning is not None:
            diagnostics.append(_health_diagnostic_for_persisted_warning(entry, warning))
            continue
        server = loaded.get(identity)
        if server is not None:
            diagnostics.append(health_diagnostic_for_config(server))
    for server in result.servers:
        identity = _scoped_server_listing_identity(server)
        if identity in represented:
            continue
        represented.add(identity)
        diagnostics.append(health_diagnostic_for_config(server))
    return diagnostics


def _raw_config_by_identity(raw_entries: list[Any]) -> dict[Any, Any]:
    return {_persisted_entry_listing_identity(entry): entry.config for entry in raw_entries}


def _server_payload(diagnostic: MCPHealthDiagnostic, raw_config: Any) -> dict[str, Any]:
    payload = _checked_server_payload(dict(raw_config) if isinstance(raw_config, dict) else {}, diagnostic)
    payload["source_path"] = diagnostic.scoped_config.source_path
    payload["disabled"] = diagnostic.scoped_config.disabled
    payload["editable_config"] = _editable_config(raw_config)
    return payload


def list_mcp_servers(cwd: Path) -> dict[str, Any]:
    """List persisted MCP servers across scopes without connecting."""

    result = load_all_persisted_mcp_configs(cwd=cwd, include_pending_project=True)
    raw_entries = list_persisted_mcp_server_entries(cwd=cwd)
    raw_by_identity = _raw_config_by_identity(raw_entries)
    diagnostics = _offline_diagnostics(result, raw_entries)
    servers = []
    for diagnostic in diagnostics:
        identity = _scoped_server_listing_identity(diagnostic.scoped_config)
        servers.append(_server_payload(diagnostic, raw_by_identity.get(identity, diagnostic.scoped_config.config.raw)))
    warnings = [
        {
            "server_name": getattr(warning, "server_name", None),
            "message": str(getattr(warning, "message", "")),
            "code": getattr(warning, "code", None),
            "source": str(getattr(warning, "source", "")) or None,
        }
        for warning in result.warnings
    ]
    return {"servers": servers, "warnings": warnings}


def check_mcp_servers(
    cwd: Path,
    *,
    name: str | None = None,
    scope: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Connect to one or all servers and return live health payloads."""

    source = _source_path_arg(source_path)
    raw_entries = list_persisted_mcp_server_entries(cwd=cwd)
    raw_by_identity = _raw_config_by_identity(raw_entries)
    if name:
        diagnostics = _run_cli(
            _reconnect_single,
            cwd=cwd,
            name=name,
            scope=scope,
            source_path=source,
        )
    else:
        result = load_all_persisted_mcp_configs(cwd=cwd, include_pending_project=True)
        if not result.servers:
            diagnostics = []
        else:
            diagnostics = _run_cli(_run_reconnect_checks, result.servers, cwd=cwd)
    servers = []
    for diagnostic in diagnostics:
        identity = _scoped_server_listing_identity(diagnostic.scoped_config)
        servers.append(_server_payload(diagnostic, raw_by_identity.get(identity, diagnostic.scoped_config.config.raw)))
    return {"servers": servers}


def _reconnect_single(
    *,
    cwd: Path,
    name: str,
    scope: str | None,
    source_path: str | None,
) -> list[MCPHealthDiagnostic]:
    parsed_scope = _parse_scope(scope) if scope else None
    if parsed_scope is None:
        # Resolve via the CLI helper (auto-detects scope/source-path by name).
        from iac_code.mcp.cli import reconnect_mcp_server

        return reconnect_mcp_server(name=name, cwd=cwd)
    scoped_config = _scoped_config_for_health_check(
        name,
        parsed_scope,
        source_path=Path(source_path) if source_path else None,
        cwd=cwd,
    )
    return _run_reconnect_checks([scoped_config], cwd=cwd)


# ---------------------------------------------------------------------------
# Capability discovery (tools / resources / prompts)
# ---------------------------------------------------------------------------


def _tool_payload(tool: MCPToolRecord) -> dict[str, Any]:
    return {
        "name": tool.tool_name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema) if tool.input_schema else {},
        "annotations": dict(tool.annotations) if tool.annotations else {},
    }


def _resource_payload(resource: MCPResourceRecord) -> dict[str, Any]:
    return {
        "uri": resource.uri,
        "name": resource.name,
        "title": resource.title,
        "description": resource.description,
        "mime_type": resource.mime_type,
    }


def _normalize_prompt_arguments(arguments: Any) -> list[dict[str, Any]]:
    """Coerce prompt arguments into JSON-serializable dicts.

    Depending on the server, ``arguments`` may be a list of SDK ``PromptArgument``
    pydantic objects, a list of plain dicts, or a ``name -> schema`` mapping. Any
    of these must survive ``json.dumps`` when returned over the web API.
    """

    def _attr(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    normalized: list[dict[str, Any]] = []
    if isinstance(arguments, dict):
        for arg_name, schema in arguments.items():
            structured = not isinstance(schema, str)
            normalized.append(
                {
                    "name": str(arg_name),
                    "description": _attr(schema, "description") if structured else None,
                    "required": bool(_attr(schema, "required")) if structured else False,
                }
            )
    elif isinstance(arguments, (list, tuple)):
        for argument in arguments:
            arg_name = _attr(argument, "name")
            if not arg_name:
                continue
            normalized.append(
                {
                    "name": str(arg_name),
                    "description": _attr(argument, "description"),
                    "required": bool(_attr(argument, "required")),
                }
            )
    return normalized


def _prompt_payload(prompt: MCPPromptRecord) -> dict[str, Any]:
    return {
        "name": prompt.prompt_name,
        "description": prompt.description,
        "arguments": _normalize_prompt_arguments(prompt.arguments),
    }


def server_capabilities(
    cwd: Path,
    *,
    name: str,
    scope: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Connect briefly and return the server's tools/resources/prompts."""

    source = _source_path_arg(source_path)
    parsed_scope = _parse_scope(scope) if scope else None
    if parsed_scope is not None:
        scoped_config = _run_cli(
            _scoped_config_for_health_check,
            name,
            parsed_scope,
            source_path=Path(source) if source else None,
            cwd=cwd,
        )
    else:
        result = load_all_persisted_mcp_configs(cwd=cwd, include_pending_project=True)
        scoped_config = result.by_name().get(name)
        if scoped_config is None:
            raise MCPWebError("MCP server {name!r} not found.".format(name=name), status_code=404)

    workspace_root = resolve_mcp_workspace_root(cwd)
    snapshot = _run_cli(_connect_and_fetch, scoped_config, workspace_root)
    diagnostic = (
        snapshot.diagnostic if snapshot is not None else health_diagnostic_for_config(scoped_config)
    )
    tools = snapshot.tools if snapshot is not None else []
    resources = snapshot.resources if snapshot is not None else []
    prompts = snapshot.prompts if snapshot is not None else []
    capability_errors = snapshot.capability_errors if snapshot is not None else {}
    return {
        "name": name,
        "scope": scoped_config.scope.value,
        "connection_state": diagnostic.connection_state,
        "auth_state": diagnostic.auth_state,
        "latest_failure": diagnostic.failure_reason,
        "capability_errors": capability_errors,
        "tools": tools,
        "resources": resources,
        "prompts": prompts,
    }


@dataclass
class _CapabilitySnapshot:
    """Capabilities captured while the connection is live.

    ``MCPManager.disconnect_all`` rebinds ``record.tools``/``resources``/``prompts``
    to empty lists and resets the connection state. Because ``_connect_and_fetch``
    disconnects in a ``finally`` before its value reaches the caller, returning the
    live record would hand back an already-wiped object. We therefore snapshot the
    diagnostic and serialised payloads *before* disconnecting.
    """

    diagnostic: MCPHealthDiagnostic
    tools: list[dict[str, Any]]
    resources: list[dict[str, Any]]
    prompts: list[dict[str, Any]]
    capability_errors: dict[str, str]


def _connect_and_fetch(scoped_config: Any, workspace_root: Any) -> _CapabilitySnapshot | None:
    from iac_code.mcp.cli import _MCP_HEALTH_TIMEOUT_SECONDS, _MCP_RECONNECT_ATTEMPTS, _run_async_blocking

    if scoped_config.disabled or not scoped_config.approved:
        return None

    async def runner() -> _CapabilitySnapshot | None:
        manager = MCPManager(
            [scoped_config],
            roots=[workspace_root],
            max_reconnect_attempts=_MCP_RECONNECT_ATTEMPTS,
            connect_timeout_seconds=_MCP_HEALTH_TIMEOUT_SECONDS,
            operation_timeout_seconds=_MCP_HEALTH_TIMEOUT_SECONDS,
        )
        try:
            await manager.connect_all()
            for record in manager.list_connections():
                if record.name == scoped_config.name and record.scoped_config.scope is scoped_config.scope:
                    return _CapabilitySnapshot(
                        diagnostic=health_diagnostic_for_record(record),
                        tools=[_tool_payload(tool) for tool in record.tools],
                        resources=[_resource_payload(resource) for resource in record.resources],
                        prompts=[_prompt_payload(prompt) for prompt in record.prompts],
                        capability_errors={
                            str(key): str(value) for key, value in (record.capability_errors or {}).items()
                        },
                    )
            return None
        finally:
            await manager.disconnect_all()

    return _run_async_blocking(runner())


# ---------------------------------------------------------------------------
# Add / edit / remove
# ---------------------------------------------------------------------------


def _build_config_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    transport = str(fields.get("transport") or "stdio").strip().lower()
    config: dict[str, Any] = {}
    if transport == "stdio":
        command = (fields.get("command") or "").strip()
        if not command:
            raise MCPWebError("command is required for stdio MCP servers.")
        config["command"] = command
        args = [str(item) for item in fields.get("args") or [] if str(item).strip()]
        if args:
            config["args"] = args
        env = _normalise_mapping(fields.get("env"))
        if env:
            config["env"] = env
    elif transport in {"http", "sse"}:
        url = (fields.get("url") or "").strip()
        if not url:
            raise MCPWebError("url is required for remote MCP servers.")
        config["type"] = transport
        config["url"] = url
        headers = _normalise_mapping(fields.get("headers"))
        if headers:
            config["headers"] = headers
    elif transport == "ws":
        url = (fields.get("url") or "").strip()
        if not url:
            raise MCPWebError("url is required for ws MCP servers.")
        config["type"] = transport
        config["url"] = url
    else:
        raise MCPWebError("Invalid transport {transport!r}. Valid values: stdio, http, sse, ws.".format(
            transport=transport
        ))

    oauth = _build_oauth(fields.get("oauth"))
    if oauth:
        config["oauth"] = oauth
    return config


def _normalise_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        result[key_text] = "" if item is None else str(item)
    return result


def _build_oauth(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    oauth: dict[str, Any] = {}
    client_id = (value.get("clientId") or value.get("client_id") or "").strip()
    if client_id:
        oauth["clientId"] = client_id
    client_secret_env = (value.get("clientSecretEnv") or value.get("client_secret_env") or "").strip()
    if client_secret_env:
        oauth["clientSecretEnv"] = client_secret_env
    callback_port = value.get("callbackPort", value.get("callback_port"))
    if callback_port not in (None, ""):
        try:
            oauth["callbackPort"] = int(callback_port)
        except (TypeError, ValueError) as exc:
            raise MCPWebError("OAuth callback port must be an integer.") from exc
    metadata_url = (
        value.get("authServerMetadataUrl") or value.get("auth_server_metadata_url") or ""
    ).strip()
    if metadata_url:
        oauth["authServerMetadataUrl"] = metadata_url
    return oauth


def _write_server(name: str, config: dict[str, Any], *, scope: MCPConfigScope, cwd: Path) -> Path:
    try:
        validate_mcp_config_no_plaintext_secrets(config)
        return write_mcp_server_config(name, config, scope=scope, cwd=cwd)
    except MCPConfigError as exc:
        raise MCPWebError(str(exc)) from exc


def add_mcp_server(
    cwd: Path,
    *,
    name: str,
    fields: dict[str, Any],
    scope: str | None = None,
) -> dict[str, Any]:
    server_name = (name or "").strip()
    if not server_name:
        raise MCPWebError("MCP server name is required.")
    resolved_scope = _resolve_scope_default(cwd, scope)
    existing = load_exact_mcp_config(server_name, scope=resolved_scope, cwd=cwd)
    if existing.servers:
        raise MCPWebError(
            "MCP server {name!r} already exists in {scope} config.".format(
                name=server_name, scope=resolved_scope.value
            ),
            status_code=409,
        )
    config = _build_config_from_fields(fields)
    path = _write_server(server_name, config, scope=resolved_scope, cwd=cwd)
    return {"name": server_name, "scope": resolved_scope.value, "path": str(path)}


def add_mcp_server_json(
    cwd: Path,
    *,
    name: str,
    config: Any,
    scope: str | None = None,
) -> dict[str, Any]:
    server_name = (name or "").strip()
    if not server_name:
        raise MCPWebError("MCP server name is required.")
    if not isinstance(config, dict):
        raise MCPWebError("MCP server JSON must be an object.")
    resolved_scope = _resolve_scope_default(cwd, scope)
    path = _write_server(server_name, dict(config), scope=resolved_scope, cwd=cwd)
    return {"name": server_name, "scope": resolved_scope.value, "path": str(path)}


def update_mcp_server(
    cwd: Path,
    *,
    name: str,
    fields: dict[str, Any] | None = None,
    config: Any = None,
    scope: str,
) -> dict[str, Any]:
    server_name = (name or "").strip()
    if not server_name:
        raise MCPWebError("MCP server name is required.")
    resolved_scope = _parse_scope(scope)
    existing = load_exact_mcp_config(server_name, scope=resolved_scope, cwd=cwd)
    if not existing.servers:
        raise MCPWebError(
            "MCP server {name!r} not found in {scope} config.".format(
                name=server_name, scope=resolved_scope.value
            ),
            status_code=404,
        )
    if config is not None:
        if not isinstance(config, dict):
            raise MCPWebError("MCP server JSON must be an object.")
        payload = dict(config)
    else:
        payload = _build_config_from_fields(fields or {})
    path = _write_server(server_name, payload, scope=resolved_scope, cwd=cwd)
    return {"name": server_name, "scope": resolved_scope.value, "path": str(path)}


def remove_mcp_server(
    cwd: Path,
    *,
    name: str,
    scope: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    source = _source_path_arg(source_path)
    if scope:
        parsed_scope = _parse_scope(scope)
        message = _run_cli(
            remove_mcp_server_command,
            name,
            parsed_scope.value,
            source_path=source,
            cwd=cwd,
        )
    else:
        message = _run_cli(remove_mcp_server_command, name, source_path=source, cwd=cwd)
    return {"message": message}


def set_mcp_enabled(
    cwd: Path,
    *,
    name: str,
    disabled: bool,
    scope: str,
    source_path: str | None = None,
) -> dict[str, Any]:
    parsed_scope = _parse_scope(scope)
    source = _source_path_arg(source_path)
    source_arg = Path(source) if source else None
    try:
        if disabled:
            disable_mcp_server(name, scope=parsed_scope, cwd=cwd, source_path=source_arg)
        else:
            enable_mcp_server(name, scope=parsed_scope, cwd=cwd, source_path=source_arg)
    except MCPConfigError as exc:
        raise MCPWebError(str(exc)) from exc
    return {"name": name, "scope": parsed_scope.value, "disabled": disabled}


# ---------------------------------------------------------------------------
# Project approval
# ---------------------------------------------------------------------------


def _project_file_for(cwd: Path, name: str) -> Path:
    project_file = find_project_mcp_server_file(name, cwd=cwd)
    if project_file is None:
        raise MCPWebError("MCP server {name!r} is not defined in a project config.".format(name=name))
    return project_file


def approve_mcp_server(cwd: Path, *, name: str) -> dict[str, Any]:
    project_file = _project_file_for(cwd, name)
    try:
        approve_project_mcp_server(
            name,
            project_file=project_file,
            workspace_root=resolve_mcp_workspace_root(cwd),
        )
    except MCPConfigError as exc:
        raise MCPWebError(str(exc)) from exc
    return {"name": name, "approval_state": "approved"}


def reject_mcp_server(cwd: Path, *, name: str) -> dict[str, Any]:
    project_file = _project_file_for(cwd, name)
    try:
        reject_project_mcp_server(
            name,
            project_file=project_file,
            workspace_root=resolve_mcp_workspace_root(cwd),
        )
    except MCPConfigError as exc:
        raise MCPWebError(str(exc)) from exc
    return {"name": name, "approval_state": "rejected"}


def reset_mcp_auth(
    cwd: Path,
    *,
    name: str,
    scope: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    source = _source_path_arg(source_path)
    scope_arg = _parse_scope(scope).value if scope else None
    message = _run_cli(
        reset_mcp_auth_server_command,
        name,
        scope_arg,
        source_path=source,
        cwd=cwd,
    )
    return {"message": message}


# ---------------------------------------------------------------------------
# OAuth flows
# ---------------------------------------------------------------------------


class _OAuthFlowRegistry:
    """In-memory registry of pending OAuth loopback flows keyed by id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flows: dict[str, Any] = {}

    def add(self, pending: Any) -> str:
        flow_id = uuid.uuid4().hex
        with self._lock:
            self._flows[flow_id] = pending
        return flow_id

    def get(self, flow_id: str) -> Any:
        with self._lock:
            return self._flows.get(flow_id)

    def pop(self, flow_id: str) -> Any:
        with self._lock:
            return self._flows.pop(flow_id, None)


_OAUTH_FLOWS = _OAuthFlowRegistry()


def start_mcp_auth(
    cwd: Path,
    *,
    name: str,
    scope: str | None = None,
    source_path: str | None = None,
    reauthenticate: bool = False,
) -> dict[str, Any]:
    source = _source_path_arg(source_path)
    starter = reauthenticate_mcp_server if reauthenticate else start_mcp_oauth_flow
    try:
        pending = starter(name, scope, source_path=source, cwd=cwd)
    except MCPConfigError as exc:
        raise MCPWebError(str(exc)) from exc
    except typer.Exit as exc:
        raise MCPWebError("Failed to start MCP OAuth flow.") from exc
    except Exception as exc:  # noqa: BLE001 - surface OAuth setup errors to the client
        raise MCPWebError(str(exc) or exc.__class__.__name__) from exc
    flow_id = _OAUTH_FLOWS.add(pending)
    return {
        "flow_id": flow_id,
        "authorization_url": getattr(pending, "authorization_url", None),
        "redirect_uri": getattr(pending, "redirect_uri", None),
        "timeout_seconds": getattr(pending, "timeout_seconds", None),
    }


def wait_mcp_auth(flow_id: str) -> dict[str, Any]:
    pending = _OAUTH_FLOWS.get(flow_id)
    if pending is None:
        raise MCPWebError("Unknown OAuth flow.", status_code=404)
    try:
        result = pending.wait()
    except Exception as exc:  # noqa: BLE001 - report auth failures to the client
        _OAUTH_FLOWS.pop(flow_id)
        with suppress(Exception):
            cancel_pending_mcp_oauth_flow(pending)
        raise MCPWebError(str(exc) or exc.__class__.__name__) from exc
    _OAUTH_FLOWS.pop(flow_id)
    return {"flow_id": flow_id, "completed": bool(result)}


def complete_mcp_auth(flow_id: str, callback_url: str) -> dict[str, Any]:
    pending = _OAUTH_FLOWS.get(flow_id)
    if pending is None:
        raise MCPWebError("Unknown OAuth flow.", status_code=404)
    url = (callback_url or "").strip()
    if not url:
        raise MCPWebError("callback_url is required.")
    try:
        pending.complete_manually(url)
    except Exception as exc:  # noqa: BLE001 - report auth failures to the client
        _OAUTH_FLOWS.pop(flow_id)
        with suppress(Exception):
            cancel_pending_mcp_oauth_flow(pending)
        raise MCPWebError(str(exc) or exc.__class__.__name__) from exc
    _OAUTH_FLOWS.pop(flow_id)
    return {"flow_id": flow_id, "completed": True}


def cancel_mcp_auth(flow_id: str) -> dict[str, Any]:
    pending = _OAUTH_FLOWS.pop(flow_id)
    if pending is None:
        raise MCPWebError("Unknown OAuth flow.", status_code=404)
    with suppress(Exception):
        cancel_pending_mcp_oauth_flow(pending)
    return {"flow_id": flow_id, "cancelled": True}


# Re-exported for tests / callers that need the parsed config model.
__all__ = [
    "MCPServerConfig",
    "MCPWebError",
    "add_mcp_server",
    "add_mcp_server_json",
    "approve_mcp_server",
    "cancel_mcp_auth",
    "check_mcp_servers",
    "complete_mcp_auth",
    "list_mcp_servers",
    "reject_mcp_server",
    "remove_mcp_server",
    "reset_mcp_auth",
    "server_capabilities",
    "set_mcp_enabled",
    "start_mcp_auth",
    "update_mcp_server",
    "wait_mcp_auth",
]
