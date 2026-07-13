from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

import typer

from iac_code.i18n import _
from iac_code.mcp.config import (
    MCPConfigLoadResult,
    MCPPersistedServerEntry,
    MCPPersistedServerMatch,
    approve_project_mcp_server,
    disable_mcp_server,
    enable_mcp_server,
    find_persisted_mcp_server_entries,
    find_persisted_mcp_server_matches,
    find_project_mcp_server_file,
    list_persisted_mcp_server_entries,
    load_all_persisted_mcp_configs,
    load_exact_mcp_config,
    load_mcp_configs,
    read_mcp_server_config,
    read_raw_mcp_server_config,
    reject_project_mcp_server,
    remove_mcp_server_config,
    reset_project_mcp_server_choices,
    resolve_mcp_workspace_root,
    write_mcp_server_config,
)
from iac_code.mcp.manager import (
    MCPConnectionRecord,
    MCPHealthDiagnostic,
    MCPManager,
    check_mcp_configs,
    health_diagnostic_for_config,
    health_diagnostic_for_record,
)
from iac_code.mcp.oauth import (
    clear_oauth_state,
    clear_oauth_state_for_signatures,
    clear_oauth_storage_signature_index,
    delete_oauth_storage_secret,
    get_oauth_storage_secret,
    oauth_scope_identity,
    oauth_storage_key,
    oauth_storage_signatures,
    remember_oauth_storage_signature,
    revoke_oauth_stored_tokens,
    set_oauth_storage_secret,
    start_oauth_loopback_flow,
)
from iac_code.mcp.redaction import sanitize_mcp_public_text
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import (
    MCPConfigError,
    MCPConfigScope,
    MCPConnectionState,
    MCPServerConfig,
    MCPTransport,
    ScopedMCPServerConfig,
    validate_mcp_config_no_plaintext_secrets,
)
from iac_code.utils.public_errors import sanitize_public_text

app = typer.Typer(help=_("Manage MCP servers."), context_settings={"help_option_names": ["-h", "--help"]})
_MCP_HEALTH_TIMEOUT_SECONDS = 3.0
_MCP_RECONNECT_ATTEMPTS = 2
_ADD_HEALTH_CHECK_ENV = "IAC_CODE_MCP_ADD_HEALTH_CHECK"
_OAUTH_STATE_KINDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_marker",
    "auth_flow_marker",
    "client_id",
    "client_secret",
    "client_auth_method",
)
_PENDING_OAUTH_SNAPSHOT_ATTR = "_iac_code_oauth_state_snapshot"


@app.command("add", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def add_server(
    ctx: typer.Context,
    name: str,
    command_or_url: str | None = typer.Argument(None, help=_("Command or remote URL.")),
    command: str | None = typer.Option(None, "--command", help=_("Command for stdio MCP server.")),
    arg: list[str] | None = typer.Option(None, "--arg", help=_("Command argument. Can be repeated.")),
    env: list[str] | None = typer.Option(None, "--env", help=_("Environment variable KEY=VALUE. Can be repeated.")),
    transport_type: str = typer.Option(
        "stdio",
        "--type",
        "--transport",
        help=_("Transport type: stdio, http, sse, ws."),
    ),
    url: str | None = typer.Option(None, "--url", help=_("Remote MCP URL for http/sse/ws.")),
    header: list[str] | None = typer.Option(
        None,
        "--header",
        help=_("HTTP header KEY=VALUE or Name: Value. Can be repeated."),
    ),
    scope: str | None = typer.Option(None, "--scope", help=_("Config scope: user, local, project.")),
    client_id: str | None = typer.Option(None, "--client-id", help=_("OAuth client id.")),
    client_secret: str | None = typer.Option(
        None,
        "--client-secret",
        help=_("OAuth client secret. Pass the option without a value to enter it securely."),
        prompt=True,
        prompt_required=False,
        hide_input=True,
    ),
    client_secret_env: str | None = typer.Option(
        None, "--client-secret-env", help=_("OAuth client secret env var name.")
    ),
    callback_port: int | None = typer.Option(None, "--callback-port", help=_("OAuth loopback callback port.")),
    auth_server_metadata_url: str | None = typer.Option(
        None,
        "--auth-server-metadata-url",
        help=_("OAuth authorization server metadata URL."),
    ),
) -> None:
    operands = _positional_operands(command_or_url, ctx.args)
    transport_type = transport_type.lower()
    config: dict[str, Any] = {}
    if transport_type == "stdio":
        if command:
            _reject_option_like_command_args(operands)
            stdio_command = command
            stdio_args = operands + (arg or [])
        else:
            if not operands:
                _fail(_("--command is required for stdio MCP servers."))
            _reject_option_like_command_operand(operands[0])
            stdio_command = operands[0]
            stdio_args = operands[1:] + (arg or [])
        if _looks_like_remote_endpoint(stdio_command) and not _option_was_supplied(ctx, "transport_type"):
            typer.echo(
                _(
                    "Warning: {operand!r} looks like a URL. Use --transport http, --transport sse, or --transport ws "
                    "for remote MCP servers."
                ).format(operand=stdio_command),
                err=True,
            )
        if not stdio_command:
            _fail(_("--command is required for stdio MCP servers."))
        config["command"] = stdio_command
        if sys.platform == "win32" and stdio_command == "npx":
            typer.echo(
                _("Warning: on Windows, bare npx may need to be configured as: cmd /c npx"),
                err=True,
            )
        if stdio_args:
            config["args"] = stdio_args
        if env:
            config["env"] = _parse_key_values(env, "--env")
    else:
        if url and operands:
            _fail(_("Use either --url or a positional URL for remote MCP servers, not both."))
        if not url and len(operands) > 1:
            _fail(_("Remote MCP servers accept one positional URL, not command arguments."))
        remote_url = url or (operands[0] if operands else None)
        config["type"] = transport_type
        if not remote_url:
            _fail(_("--url is required for remote MCP servers."))
        assert remote_url is not None
        _reject_option_like_command_operand(remote_url)
        config["url"] = remote_url
        if header:
            config["headers"] = _parse_headers(header)

    oauth = _oauth_config(
        client_id=client_id,
        client_secret_env=client_secret_env,
        callback_port=callback_port,
        auth_server_metadata_url=auth_server_metadata_url,
    )
    if oauth:
        config["oauth"] = oauth

    resolved_scope = _resolve_scope_option(scope)
    _write_config(name, config, scope=resolved_scope.value)
    if client_secret:
        stored = _read_config_or_fail(name, scope=resolved_scope)
        normalized = _optional_expanded_persisted_config(
            name,
            resolved_scope,
            source_path=None,
            cwd=Path.cwd(),
        ) or _server_config_from_mapping(name, stored)
        storage = MCPSecretStorage()
        oauth_scope = _oauth_scope_for_cli(resolved_scope)
        remember_oauth_storage_signature(normalized, storage=storage, scope=oauth_scope)
        set_oauth_storage_secret(normalized, storage, "client_secret", client_secret, scope=oauth_scope)
    diagnostic = _post_add_health_diagnostic(name, config, scope=resolved_scope)
    _echo_add_success(name, config, scope=resolved_scope.value, diagnostic=diagnostic)


@app.command("add-json")
def add_json(name: str, config_json: str, scope: str | None = typer.Option(None, "--scope")) -> None:
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as exc:
        _fail(_("Invalid JSON: {error}").format(error=exc))
    if not isinstance(config, dict):
        _fail(_("MCP server JSON must be an object."))
    resolved_scope = _resolve_scope_option(scope)
    _write_config(name, config, scope=resolved_scope.value)
    typer.echo(_("Added MCP server {name!r} to {scope} config.").format(name=name, scope=resolved_scope.value))


@app.command("list")
def list_servers(
    check: bool = typer.Option(False, "--check", help=_("Connect briefly and show MCP health.")),
    config_only: bool = typer.Option(
        False,
        "--config-only",
        help=_("Show configured MCP servers without health checks."),
    ),
) -> None:
    if check and config_only:
        _fail(_("Use either --check or --config-only, not both."))
    result = load_mcp_configs(cwd=Path.cwd(), include_pending_project=True)
    raw_entries = list_persisted_mcp_server_entries(cwd=Path.cwd())
    if not result.servers and not raw_entries:
        typer.echo(_("No MCP servers configured."))
        return
    if config_only or not check:
        for name, scope, transport, status in _config_listing_rows(result, raw_entries):
            typer.echo("{}\t{}\t{}\t{}".format(name, scope, transport, status))
        return
    check_result = (
        load_all_persisted_mcp_configs(cwd=Path.cwd(), include_pending_project=True) if raw_entries else result
    )
    _echo_health_list(_run_health_checks_with_config_diagnostics(check_result, raw_entries))


def _config_listing_rows(
    result: MCPConfigLoadResult,
    raw_entries: list[MCPPersistedServerEntry],
) -> list[tuple[str, str, str, str]]:
    if not raw_entries:
        return [
            (
                server.name,
                server.scope.value,
                server.transport.value,
                _config_listing_status_for_scoped_server(server),
            )
            for server in result.servers
        ]

    loaded = {_scoped_server_listing_identity(server): server for server in [*result.servers, *result.pending]}
    rows: list[tuple[str, str, str, str]] = []
    for entry in raw_entries:
        identity = _persisted_entry_listing_identity(entry)
        server = loaded.get(identity)
        if server is not None:
            rows.append(
                (
                    server.name,
                    server.scope.value,
                    server.transport.value,
                    _config_listing_status_for_scoped_server(server),
                )
            )
            continue
        rows.append(
            (
                entry.name,
                entry.scope.value,
                _raw_entry_transport_value(entry),
                _raw_entry_listing_status(entry, result),
            )
        )
    return rows


def _config_listing_status_for_scoped_server(server: ScopedMCPServerConfig) -> str:
    if server.disabled:
        return _("disabled")
    return _("approved") if server.approved else _("pending")


def _raw_entry_listing_status(entry: MCPPersistedServerEntry, result: MCPConfigLoadResult) -> str:
    if any(
        getattr(warning, "code", None) == "missing_env"
        and getattr(warning, "server_name", None) == entry.name
        and str(getattr(warning, "source", "")) == str(entry.source_path)
        for warning in result.warnings
    ):
        return _("missing-env")
    if any(
        getattr(warning, "server_name", None) == entry.name
        and str(getattr(warning, "source", "")) == str(entry.source_path)
        for warning in result.warnings
    ):
        return _("invalid-config")
    return _("configured")


def _raw_entry_transport_value(entry: MCPPersistedServerEntry) -> str:
    if isinstance(entry.config, dict):
        try:
            return MCPServerConfig.from_mapping(entry.name, entry.config).transport.value
        except MCPConfigError:
            raw_transport = entry.config.get("type")
            if isinstance(raw_transport, str) and raw_transport:
                return raw_transport
    return "-"


def _scoped_server_listing_identity(server: ScopedMCPServerConfig) -> tuple[str, MCPConfigScope, str]:
    return (server.name, server.scope, str(server.source_path or ""))


def _persisted_entry_listing_identity(entry: MCPPersistedServerEntry) -> tuple[str, MCPConfigScope, str]:
    return (entry.name, entry.scope, str(entry.source_path or ""))


def _run_health_checks_with_config_diagnostics(
    result: MCPConfigLoadResult,
    raw_entries: list[MCPPersistedServerEntry],
) -> list[MCPHealthDiagnostic]:
    if not raw_entries:
        return _run_health_checks(result.servers)

    loaded = {_scoped_server_listing_identity(server): server for server in [*result.servers, *result.pending]}
    health_winners = _health_check_signature_winners(loaded.values(), raw_entries)
    represented: set[tuple[str, MCPConfigScope, str]] = set()
    health_configs: list[ScopedMCPServerConfig] = []
    ordered_items: list[tuple[str, tuple[str, MCPConfigScope, str]]] = []
    static_diagnostics: dict[tuple[str, MCPConfigScope, str], MCPHealthDiagnostic] = {}

    for entry in raw_entries:
        identity = _persisted_entry_listing_identity(entry)
        represented.add(identity)
        warning = _health_warning_for_entry(entry, result)
        if warning is not None:
            static_diagnostics[identity] = _health_diagnostic_for_persisted_warning(entry, warning)
            ordered_items.append(("static", identity))
            continue
        server = loaded.get(identity)
        if server is not None:
            if identity not in health_winners:
                static_diagnostics[identity] = health_diagnostic_for_config(server)
                ordered_items.append(("static", identity))
                continue
            health_configs.append(server)
            ordered_items.append(("health", identity))

    for server in result.servers:
        identity = _scoped_server_listing_identity(server)
        if identity in represented:
            continue
        represented.add(identity)
        health_configs.append(server)
        ordered_items.append(("health", identity))

    health_diagnostics = {
        _scoped_server_listing_identity(diagnostic.scoped_config): diagnostic
        for diagnostic in (_run_health_checks_batched_by_name(health_configs) if health_configs else [])
    }
    diagnostics: list[MCPHealthDiagnostic] = []
    for kind, identity in ordered_items:
        diagnostic = static_diagnostics.get(identity) if kind == "static" else health_diagnostics.get(identity)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return diagnostics


def _health_check_signature_winners(
    servers: Iterable[ScopedMCPServerConfig],
    raw_entries: list[MCPPersistedServerEntry],
) -> set[tuple[str, MCPConfigScope, str]]:
    source_order = {_persisted_entry_listing_identity(entry): index for index, entry in enumerate(raw_entries)}
    winners: dict[str, tuple[tuple[int, int], tuple[str, MCPConfigScope, str]]] = {}
    for server in servers:
        identity = _scoped_server_listing_identity(server)
        key = (server.precedence, source_order.get(identity, -1))
        signature = server.config.content_signature()
        existing = winners.get(signature)
        if existing is None or key >= existing[0]:
            winners[signature] = (key, identity)
    return {identity for _key, identity in winners.values()}


def _health_warning_for_entry(
    entry: MCPPersistedServerEntry,
    result: MCPConfigLoadResult,
) -> Any | None:
    for warning in result.warnings:
        if getattr(warning, "code", None) not in {"missing_env", "invalid_config"}:
            continue
        if getattr(warning, "server_name", None) != entry.name:
            continue
        if str(getattr(warning, "source", "")) == str(entry.source_path):
            return warning
    return None


def _health_diagnostic_for_persisted_warning(
    entry: MCPPersistedServerEntry,
    warning: Any,
) -> MCPHealthDiagnostic:
    code = str(getattr(warning, "code", "invalid_config"))
    status = "missing-env" if code == "missing_env" else "invalid-config"
    return MCPHealthDiagnostic(
        scoped_config=_scoped_config_for_persisted_warning(entry),
        status=status,
        connection_state=status,
        auth_state="not-configured",
        failure_reason=sanitize_mcp_public_text(str(getattr(warning, "message", "") or status)),
    )


def _scoped_config_for_persisted_warning(entry: MCPPersistedServerEntry) -> ScopedMCPServerConfig:
    config = _optional_server_config_from_mapping(entry.name, entry.config)
    if config is None:
        config = MCPServerConfig(
            name=entry.name,
            transport=MCPTransport.STDIO,
            raw=dict(entry.config) if isinstance(entry.config, dict) else {},
        )
    return ScopedMCPServerConfig(
        config=config,
        scope=entry.scope,
        source_path=str(entry.source_path) if entry.source_path is not None else None,
        approved=entry.scope is not MCPConfigScope.PROJECT,
    )


@app.command("get")
def get_server(
    name: str,
    scope: str | None = typer.Option(None, "--scope"),
    source_path: str | None = typer.Option(None, "--source-path", help=_("Persisted MCP config file path.")),
    check: bool = typer.Option(False, "--check", help=_("Connect briefly and show MCP health.")),
    config_only: bool = typer.Option(False, "--config-only", help=_("Show configured MCP server JSON only.")),
) -> None:
    resolved_scope, config, resolved_source_path = _resolve_persisted_server_for_command(
        name,
        scope=scope,
        command="get",
        source_path=source_path,
    )
    if check and config_only:
        _fail(_("Use either --check or --config-only, not both."))
    if not check:
        typer.echo(json.dumps(_redact_public_config(config), ensure_ascii=False, indent=2, sort_keys=True))
        return

    scoped_config = _scoped_config_for_health_check(name, resolved_scope, source_path=resolved_source_path)
    diagnostics = _run_health_checks([scoped_config])
    diagnostic = diagnostics[0] if diagnostics else None
    if diagnostic is None:
        _fail(_("MCP health check failed: {error}").format(error=_("No diagnostic was returned.")))
    assert diagnostic is not None
    typer.echo(
        json.dumps(
            _checked_server_payload(config, diagnostic),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


@app.command("remove")
def remove_server(
    name: str,
    scope: str | None = typer.Option(None, "--scope"),
    source_path: str | None = typer.Option(None, "--source-path", help=_("Persisted MCP config file path.")),
) -> None:
    typer.echo(remove_mcp_server_command(name, scope=scope, source_path=source_path))


def remove_mcp_server_command(
    name: str,
    scope: str | None = None,
    *,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> str:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    resolved_scope, raw_config, source_path = _resolve_raw_persisted_server_for_command(
        name,
        scope=scope,
        command="remove",
        source_path=source_path,
        cwd=command_cwd,
    )
    warnings = _clear_persisted_oauth_state(
        name,
        resolved_scope,
        raw_config,
        source_path=source_path,
        cwd=command_cwd,
    )
    path = remove_mcp_server_config(name, scope=resolved_scope, cwd=command_cwd, source_path=source_path)
    if path is None:
        _fail(_("MCP server {name!r} not found in {scope} config.").format(name=name, scope=resolved_scope.value))
    return _with_warnings(_("Removed MCP server {name!r} from {path}.").format(name=name, path=path), warnings)


@app.command("reconnect")
def reconnect_server(
    name: str | None = typer.Argument(None, help=_("MCP server name.")),
    all_servers: bool = typer.Option(False, "--all", help=_("Reconnect all persisted MCP servers.")),
    scope: str | None = typer.Option(None, "--scope", help=_("Config scope: user, local, project.")),
    source_path: str | None = typer.Option(None, "--source-path", help=_("Persisted MCP config file path.")),
) -> None:
    diagnostics = reconnect_mcp_server(name=name, all_servers=all_servers, scope=scope, source_path=source_path)
    if not diagnostics and all_servers:
        typer.echo(_("No MCP servers configured."))
        return
    _echo_health_list(diagnostics)


def reconnect_mcp_server(
    *,
    name: str | None = None,
    all_servers: bool = False,
    scope: str | None = None,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> list[MCPHealthDiagnostic]:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    if all_servers:
        if name is not None:
            _fail(_("Use either a server name or --all, not both."))
        if scope is not None:
            _fail(_("--scope cannot be used with mcp reconnect --all."))
        if source_path is not None:
            _fail(_("--source-path cannot be used with mcp reconnect --all."))
        result = load_all_persisted_mcp_configs(cwd=command_cwd, include_pending_project=True)
        if not result.servers:
            return []
        return _run_reconnect_checks(result.servers, cwd=command_cwd)

    if name is None:
        _fail(_("MCP server name is required unless --all is used."))
    assert name is not None
    resolved_scope, _raw_config, source_path = _resolve_persisted_server_for_command(
        name,
        scope=scope,
        command="reconnect",
        source_path=source_path,
        cwd=command_cwd,
    )
    scoped_config = _scoped_config_for_health_check(name, resolved_scope, source_path=source_path, cwd=command_cwd)
    return _run_reconnect_checks([scoped_config], cwd=command_cwd)


@app.command("disable")
def disable_server_command(
    name: str,
    scope: str | None = typer.Option(None, "--scope"),
    source_path: str | None = typer.Option(None, "--source-path", help=_("Persisted MCP config file path.")),
) -> None:
    typer.echo(disable_mcp_server_command(name, scope=scope, source_path=source_path))


def disable_mcp_server_command(
    name: str,
    scope: str | None = None,
    *,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> str:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    resolved_scope, _raw_config, source_path = _resolve_raw_persisted_server_for_command(
        name,
        scope=scope,
        command="disable",
        source_path=source_path,
        cwd=command_cwd,
    )
    try:
        disable_mcp_server(name, scope=resolved_scope, cwd=command_cwd, source_path=source_path)
    except MCPConfigError as exc:
        _fail(str(exc))
    return _("Disabled MCP server {name!r}.").format(name=name)


@app.command("enable")
def enable_server_command(
    name: str,
    scope: str | None = typer.Option(None, "--scope"),
    source_path: str | None = typer.Option(None, "--source-path", help=_("Persisted MCP config file path.")),
) -> None:
    typer.echo(enable_mcp_server_command(name, scope=scope, source_path=source_path))


def enable_mcp_server_command(
    name: str,
    scope: str | None = None,
    *,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> str:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    resolved_scope, _raw_config, source_path = _resolve_raw_persisted_server_for_command(
        name,
        scope=scope,
        command="enable",
        source_path=source_path,
        cwd=command_cwd,
    )
    try:
        enable_mcp_server(name, scope=resolved_scope, cwd=command_cwd, source_path=source_path)
    except MCPConfigError as exc:
        _fail(str(exc))
    return _("Enabled MCP server {name!r}.").format(name=name)


@app.command("approve")
def approve_server(name: str) -> None:
    project_file = _project_file_for(name)
    try:
        approve_project_mcp_server(
            name,
            project_file=project_file,
            workspace_root=resolve_mcp_workspace_root(Path.cwd()),
        )
    except MCPConfigError as exc:
        _fail(str(exc))
    typer.echo(_("Approved MCP server {name!r}.").format(name=name))


@app.command("reject")
def reject_server(name: str) -> None:
    project_file = _project_file_for(name)
    try:
        reject_project_mcp_server(
            name,
            project_file=project_file,
            workspace_root=resolve_mcp_workspace_root(Path.cwd()),
        )
    except MCPConfigError as exc:
        _fail(str(exc))
    typer.echo(_("Rejected MCP server {name!r}.").format(name=name))


@app.command("reset-project-choices")
def reset_project_choices() -> None:
    reset_project_mcp_server_choices()
    typer.echo(_("Reset MCP project approval choices."))


@app.command("auth")
def auth_server(
    name: str,
    scope: str | None = typer.Option(None, "--scope"),
    source_path: str | None = typer.Option(None, "--source-path", help=_("Persisted MCP config file path.")),
) -> None:
    typer.echo(authenticate_mcp_server(name, scope=scope, source_path=source_path))


def authenticate_mcp_server(
    name: str,
    scope: str | None = None,
    *,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
    resource_metadata_url: str | None = None,
) -> str:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    parsed_scope, _raw_config, source_path = _resolve_persisted_server_for_command(
        name,
        scope=scope,
        command="auth",
        source_path=source_path,
        cwd=command_cwd,
    )
    config = _expanded_persisted_config(name, parsed_scope, source_path=source_path, cwd=command_cwd)
    storage = MCPSecretStorage()
    oauth_scope = _oauth_scope_for_cli(parsed_scope, source_path=source_path)
    original_oauth_state = _snapshot_oauth_state(config, storage=storage, scope=oauth_scope)
    try:
        flow_kwargs: dict[str, Any] = {}
        if required_scopes:
            flow_kwargs["required_scopes"] = required_scopes
        if resource_metadata_url:
            flow_kwargs["resource_metadata_url"] = resource_metadata_url
        _run_cli_oauth_flow(
            config,
            storage=storage,
            scope=oauth_scope,
            server_name=name,
            **flow_kwargs,
        )
    except KeyboardInterrupt:
        _restore_oauth_state(config, storage=storage, scope=oauth_scope, snapshot=original_oauth_state)
        _fail(
            _("MCP auth failed for {name!r}: {error}").format(
                name=name,
                error=_("OAuth authorization was cancelled."),
            )
        )
    except Exception as exc:
        _restore_oauth_state(config, storage=storage, scope=oauth_scope, snapshot=original_oauth_state)
        error = sanitize_public_text(str(exc) or exc.__class__.__name__)
        _fail(_("MCP auth failed for {name!r}: {error}").format(name=name, error=error))
    return _("Authenticated MCP server {name!r}.").format(name=name)


def start_mcp_oauth_flow(
    name: str,
    scope: str | None = None,
    *,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
    resource_metadata_url: str | None = None,
):
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    parsed_scope, _raw_config, source_path = _resolve_persisted_server_for_command(
        name,
        scope=scope,
        command="auth",
        source_path=source_path,
        cwd=command_cwd,
    )
    config = _expanded_persisted_config(name, parsed_scope, source_path=source_path, cwd=command_cwd)
    storage = MCPSecretStorage()
    oauth_scope = _oauth_scope_for_cli(parsed_scope, source_path=source_path)
    flow_kwargs: dict[str, Any] = {}
    if required_scopes:
        flow_kwargs["required_scopes"] = required_scopes
    if resource_metadata_url:
        flow_kwargs["resource_metadata_url"] = resource_metadata_url
    original_oauth_state = _snapshot_oauth_state(config, storage=storage, scope=oauth_scope)
    try:
        pending = start_oauth_loopback_flow(
            config,
            storage=storage,
            scope=oauth_scope,
            **flow_kwargs,
        )
        _attach_pending_oauth_snapshot(
            pending,
            config=config,
            storage=storage,
            scope=oauth_scope,
            snapshot=original_oauth_state,
        )
        return pending
    except Exception:
        _restore_oauth_state(config, storage=storage, scope=oauth_scope, snapshot=original_oauth_state)
        raise


def reauthenticate_mcp_server(
    name: str,
    scope: str | None = None,
    *,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
    resource_metadata_url: str | None = None,
):
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    parsed_scope, raw_config, source_path = _resolve_persisted_server_for_command(
        name,
        scope=scope,
        command="auth",
        source_path=source_path,
        cwd=command_cwd,
    )
    config = _expanded_persisted_config(name, parsed_scope, source_path=source_path, cwd=command_cwd)
    storage = MCPSecretStorage()
    oauth_scope = _oauth_scope_for_cli(parsed_scope, source_path=source_path)
    original_oauth_state = _snapshot_oauth_state(config, storage=storage, scope=oauth_scope)
    _clear_oauth_states_for_configs(
        [config, _optional_server_config_from_mapping(name, raw_config)],
        storage=storage,
        scope=oauth_scope,
    )
    try:
        flow_kwargs: dict[str, Any] = {}
        if required_scopes:
            flow_kwargs["required_scopes"] = required_scopes
        if resource_metadata_url:
            flow_kwargs["resource_metadata_url"] = resource_metadata_url
        pending = start_oauth_loopback_flow(
            config,
            storage=storage,
            scope=oauth_scope,
            **flow_kwargs,
        )
        _attach_pending_oauth_snapshot(
            pending,
            config=config,
            storage=storage,
            scope=oauth_scope,
            snapshot=original_oauth_state,
        )
        return pending
    except Exception:
        _restore_oauth_state(config, storage=storage, scope=oauth_scope, snapshot=original_oauth_state)
        raise


def _attach_pending_oauth_snapshot(
    pending: Any,
    *,
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    snapshot: dict[str, str | None],
) -> None:
    with suppress(Exception):
        setattr(pending, "config", getattr(pending, "config", config))
        setattr(pending, "storage", getattr(pending, "storage", storage))
        setattr(pending, "scope", getattr(pending, "scope", scope))
        setattr(pending, _PENDING_OAUTH_SNAPSHOT_ATTR, snapshot)


def cancel_pending_mcp_oauth_flow(pending: Any) -> None:
    config = getattr(pending, "config", None)
    storage = getattr(pending, "storage", None)
    scope = getattr(pending, "scope", None)
    snapshot = getattr(pending, _PENDING_OAUTH_SNAPSHOT_ATTR, None)
    _close_pending_oauth_flow(pending)
    if config is not None and storage is not None:
        if isinstance(snapshot, dict):
            _restore_oauth_state(config, storage=storage, scope=scope, snapshot=snapshot)
        else:
            clear_oauth_state(config, storage=storage, scope=scope)


@app.command("reset-auth")
def reset_auth(
    name: str,
    scope: str | None = typer.Option(None, "--scope"),
    source_path: str | None = typer.Option(None, "--source-path", help=_("Persisted MCP config file path.")),
) -> None:
    typer.echo(reset_mcp_auth_server_command(name, scope=scope, source_path=source_path))


def reset_mcp_auth_server_command(
    name: str,
    scope: str | None = None,
    *,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> str:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    parsed_scope, raw_config, source_path = _resolve_raw_persisted_server_for_command(
        name,
        scope=scope,
        command="reset-auth",
        source_path=source_path,
        cwd=command_cwd,
    )
    warnings = _clear_persisted_oauth_state(
        name,
        parsed_scope,
        raw_config,
        source_path=source_path,
        cwd=command_cwd,
    )
    return _with_warnings(_("Reset stored MCP auth state for {name!r}.").format(name=name), warnings)


def _snapshot_oauth_state(
    config,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
) -> dict[str, str | None]:
    access_key = oauth_storage_key(config, "access_token", scope=scope)
    with storage.lock(access_key):
        return {kind: get_oauth_storage_secret(config, storage, kind, scope=scope) for kind in _OAUTH_STATE_KINDS}


def _restore_oauth_state(
    config,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    snapshot: dict[str, str | None],
) -> None:
    access_key = oauth_storage_key(config, "access_token", scope=scope)
    with storage.lock(access_key):
        for kind in _OAUTH_STATE_KINDS:
            value = snapshot.get(kind)
            if value is None:
                delete_oauth_storage_secret(config, storage, kind, scope=scope)
            else:
                set_oauth_storage_secret(config, storage, kind, value, scope=scope)


def _clear_persisted_oauth_state(
    name: str,
    scope: MCPConfigScope,
    raw_config: Any,
    *,
    source_path: Path | None,
    cwd: str | Path | None = None,
) -> list[str]:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    storage = MCPSecretStorage()
    oauth_scope = _oauth_scope_for_cli(scope, source_path=source_path)
    expanded_config = _optional_expanded_persisted_config(name, scope, source_path=source_path, cwd=command_cwd)
    raw_server_config = _optional_server_config_from_mapping(name, raw_config)
    return _clear_oauth_states_for_configs(
        [expanded_config, raw_server_config],
        storage=storage,
        scope=oauth_scope,
        name=name,
        include_index=True,
    )


def _clear_oauth_states_for_configs(
    configs: list[Any],
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    name: str | None = None,
    include_index: bool = False,
) -> list[str]:
    seen: set[str] = set()
    warnings: list[str] = []
    for config in configs:
        if not isinstance(config, MCPServerConfig):
            continue
        signature = config.content_signature()
        if signature in seen:
            continue
        seen.add(signature)
        warnings.extend(revoke_oauth_stored_tokens(config, storage=storage, scope=scope))
        clear_oauth_state(config, storage=storage, scope=scope)
    if include_index and name:
        signatures = [
            signature for signature in oauth_storage_signatures(name, storage=storage, scope=scope) if signature
        ]
        clear_oauth_state_for_signatures(name, signatures, storage=storage, scope=scope)
        clear_oauth_storage_signature_index(name, storage=storage, scope=scope)
    return list(dict.fromkeys(warnings))


def _with_warnings(message: str, warnings: list[str]) -> str:
    if not warnings:
        return message
    suffix = "\n".join(_("Warning: {warning}").format(warning=warning) for warning in warnings)
    return "{}\n{}".format(message, suffix)


def _run_cli_oauth_flow(
    config,
    *,
    storage: MCPSecretStorage,
    scope: MCPConfigScope | str | None,
    server_name: str,
    required_scopes: list[str] | tuple[str, ...] | None = None,
    resource_metadata_url: str | None = None,
):
    flow_kwargs: dict[str, Any] = {}
    if required_scopes:
        flow_kwargs["required_scopes"] = required_scopes
    if resource_metadata_url:
        flow_kwargs["resource_metadata_url"] = resource_metadata_url
    pending = start_oauth_loopback_flow(config, storage=storage, scope=scope, **flow_kwargs)
    status = _("yes") if pending.browser_opened else _("no")
    typer.echo(_("Browser opened: {status}").format(status=status))
    try:
        if pending.browser_opened:
            return pending.wait()

        typer.echo(_("Authorization URL: {url}").format(url=pending.authorization_url))
        manual_value = _read_manual_oauth_completion(server_name)
        if manual_value:
            return pending.complete_manually(manual_value)
        return pending.wait()
    except BaseException:
        _close_pending_oauth_flow(pending)
        raise


def _close_pending_oauth_flow(pending: Any) -> None:
    close = getattr(pending, "close", None)
    if callable(close):
        with suppress(BaseException):
            close()
        return

    callback = getattr(pending, "callback", None)
    callback_close = getattr(callback, "close", None)
    if callable(callback_close):
        with suppress(BaseException):
            callback_close()


def _read_manual_oauth_completion(server_name: str) -> str | None:
    typer.echo(
        _(
            "Paste the callback URL or authorization code for MCP server {name!r}, "
            "or press Enter to wait for the loopback callback:"
        ).format(name=server_name)
    )
    try:
        value = input()
    except EOFError:
        return None
    except KeyboardInterrupt as exc:
        raise RuntimeError(_("OAuth authorization was cancelled.")) from exc
    value = value.strip()
    return value or None


def _run_health_checks(configs: list[ScopedMCPServerConfig]) -> list[MCPHealthDiagnostic]:
    workspace_root = resolve_mcp_workspace_root(Path.cwd())
    try:
        return _run_async_blocking(
            check_mcp_configs(
                configs,
                manager_factory=lambda checked: _create_health_check_manager(checked, roots=[workspace_root]),
            )
        )
    except Exception as exc:
        error = sanitize_public_text(str(exc) or exc.__class__.__name__)
        _fail(_("MCP health check failed: {error}").format(error=error))
        raise AssertionError("unreachable") from exc


def _run_health_checks_batched_by_name(configs: list[ScopedMCPServerConfig]) -> list[MCPHealthDiagnostic]:
    diagnostics: list[MCPHealthDiagnostic] = []
    for batch in _health_check_batches_by_name(configs):
        diagnostics.extend(_run_health_checks(batch))
    return diagnostics


def _health_check_batches_by_name(configs: list[ScopedMCPServerConfig]) -> list[list[ScopedMCPServerConfig]]:
    grouped: dict[str, list[ScopedMCPServerConfig]] = {}
    for config in configs:
        grouped.setdefault(config.name, []).append(config)

    batches: list[list[ScopedMCPServerConfig]] = []
    current_batch: list[ScopedMCPServerConfig] = []
    for group in grouped.values():
        if len(group) == 1:
            current_batch.append(group[0])
            continue
        if current_batch:
            batches.append(current_batch)
            current_batch = []
        batches.extend([config] for config in group)
    if current_batch:
        batches.append(current_batch)
    return batches


def _create_health_check_manager(
    configs: list[ScopedMCPServerConfig],
    *,
    roots: list[str | Path],
) -> MCPManager:
    return MCPManager(
        configs,
        roots=roots,
        max_reconnect_attempts=0,
        connect_timeout_seconds=_MCP_HEALTH_TIMEOUT_SECONDS,
        operation_timeout_seconds=_MCP_HEALTH_TIMEOUT_SECONDS,
    )


def _create_reconnect_manager(
    configs: list[ScopedMCPServerConfig],
    *,
    roots: list[str | Path],
) -> MCPManager:
    return MCPManager(
        configs,
        roots=roots,
        max_reconnect_attempts=_MCP_RECONNECT_ATTEMPTS,
        connect_timeout_seconds=_MCP_HEALTH_TIMEOUT_SECONDS,
        operation_timeout_seconds=_MCP_HEALTH_TIMEOUT_SECONDS,
    )


def _run_reconnect_checks(
    configs: list[ScopedMCPServerConfig],
    *,
    cwd: str | Path | None = None,
) -> list[MCPHealthDiagnostic]:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    workspace_root = resolve_mcp_workspace_root(command_cwd)
    try:
        return _run_async_blocking(
            _reconnect_mcp_configs(
                configs,
                manager_factory=lambda checked: _create_reconnect_manager(checked, roots=[workspace_root]),
            )
        )
    except Exception as exc:
        error = sanitize_public_text(str(exc) or exc.__class__.__name__)
        _fail(_("MCP reconnect failed: {error}").format(error=error))
        raise AssertionError("unreachable") from exc


async def _reconnect_mcp_configs(
    configs: list[ScopedMCPServerConfig],
    *,
    manager_factory,
) -> list[MCPHealthDiagnostic]:
    diagnostics: list[MCPHealthDiagnostic] = []
    for config in configs:
        if config.disabled or not config.approved:
            diagnostics.append(health_diagnostic_for_config(config))
            continue

        manager = manager_factory([config])
        try:
            await manager.connect_all()
            await _retry_failed_reconnects(manager)
            record = _single_connection_for_config(manager, config)
            diagnostic = (
                health_diagnostic_for_record(record) if record is not None else health_diagnostic_for_config(config)
            )
            diagnostics.append(diagnostic)
        finally:
            await manager.disconnect_all()
    return diagnostics


async def _retry_failed_reconnects(manager) -> None:
    reconnect_failed = getattr(manager, "reconnect_failed", None)
    if not callable(reconnect_failed):
        return

    for _attempt in range(_MCP_RECONNECT_ATTEMPTS):
        records = [
            record
            for record in manager.list_connections()
            if record.state is MCPConnectionState.FAILED
            and record.scoped_config.transport in {MCPTransport.HTTP, MCPTransport.SSE}
            and record.retry_count < _MCP_RECONNECT_ATTEMPTS
        ]
        if not records:
            return
        for record in records:
            await reconnect_failed(record.name)


def _single_connection_for_config(manager, config: ScopedMCPServerConfig) -> MCPConnectionRecord | None:
    for record in manager.list_connections():
        if record.name == config.name and record.scoped_config.scope is config.scope:
            return record
    return None


def _echo_health_list(diagnostics: list[MCPHealthDiagnostic]) -> None:
    typer.echo(
        "name\tscope\ttransport\tapproval_state\tauth_state\tconnection_state\ttools\tresources\tprompts\t"
        "latest_failure\trefresh_kind\trefresh_time\trefresh_failure"
    )
    for diagnostic in diagnostics:
        typer.echo(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
                diagnostic.name,
                diagnostic.scope.value,
                diagnostic.transport.value,
                _approval_state(diagnostic.scoped_config),
                diagnostic.auth_state,
                diagnostic.connection_state,
                _format_count(diagnostic.tools_count),
                _format_count(diagnostic.resources_count),
                _format_count(diagnostic.prompts_count),
                diagnostic.failure_reason or "-",
                diagnostic.latest_refresh_kind or "-",
                _format_refresh_time(diagnostic.latest_refresh_at),
                diagnostic.latest_refresh_failure_reason or "-",
            )
        )


def _checked_server_payload(
    raw_config: dict[str, Any],
    diagnostic: MCPHealthDiagnostic,
) -> dict[str, Any]:
    config = diagnostic.scoped_config.config
    payload = {
        "auth_state": diagnostic.auth_state,
        "command": _sanitize_url_text(config.command),
        "config": _redact_checked_config(raw_config),
        "connection_state": diagnostic.connection_state,
        "approval_state": _approval_state(diagnostic.scoped_config),
        "latest_failure": diagnostic.failure_reason,
        "oauth_client_state": _oauth_client_state(diagnostic),
        **_refresh_payload(diagnostic),
        "name": diagnostic.name,
        "prompts": diagnostic.prompts_count,
        "resources": diagnostic.resources_count,
        "scope": diagnostic.scope.value,
        "tools": diagnostic.tools_count,
        "transport": diagnostic.transport.value,
        "url": _sanitize_url_text(config.url),
    }
    if diagnostic.auth_error:
        payload["auth_error"] = diagnostic.auth_error
    if diagnostic.required_auth_scopes:
        payload["required_auth_scopes"] = list(diagnostic.required_auth_scopes)
    if diagnostic.auth_resource_metadata_url:
        payload["auth_resource_metadata_url"] = diagnostic.auth_resource_metadata_url
    if diagnostic.protocol_version:
        payload["protocol_version"] = diagnostic.protocol_version
    return payload


def _approval_state(config: ScopedMCPServerConfig) -> str:
    if config.disabled:
        return "disabled"
    return "approved" if config.approved else "pending-approval"


def _oauth_client_state(diagnostic: MCPHealthDiagnostic) -> dict[str, Any]:
    config = diagnostic.scoped_config.config
    source_path = Path(diagnostic.scoped_config.source_path) if diagnostic.scoped_config.source_path else None
    scope = _oauth_scope_for_cli(diagnostic.scope, source_path=source_path)
    storage = MCPSecretStorage()
    return {
        "configured_client_id": bool(config.oauth and config.oauth.client_id),
        "stored_client_auth_method": get_oauth_storage_secret(config, storage, "client_auth_method", scope=scope),
        "stored_client_id": bool(get_oauth_storage_secret(config, storage, "client_id", scope=scope)),
        "stored_client_secret": bool(get_oauth_storage_secret(config, storage, "client_secret", scope=scope)),
    }


def _refresh_payload(diagnostic: MCPHealthDiagnostic) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if diagnostic.latest_refresh_kind:
        payload["latest_refresh_kind"] = diagnostic.latest_refresh_kind
    if diagnostic.latest_refresh_at is not None:
        payload["latest_refresh_at"] = diagnostic.latest_refresh_at
    if diagnostic.latest_refresh_failure_reason:
        payload["latest_refresh_failure"] = diagnostic.latest_refresh_failure_reason
    return payload


def _format_count(value: int | None) -> str:
    return "-" if value is None else str(value)


def _format_refresh_time(value: float | None) -> str:
    return "-" if value is None else str(value)


def _redact_checked_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted = _redact_public_config(config)
    for section in ("headers", "env", "oauth"):
        value = redacted.get(section)
        if isinstance(value, dict):
            redacted[section] = _redact_all_values(value)
    return redacted


def _redact_public_config(config: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_url_values(_redact_config(config))


def _sanitize_url_text(value: str | None) -> str | None:
    if value is None:
        return None
    return sanitize_mcp_public_text(value, fallback_summary="")


def _sanitize_url_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {item_key: _sanitize_url_values(item) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_url_values(item) for item in value]
    if isinstance(value, str):
        return _sanitize_url_text(value)
    return value


def _redact_all_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {item_key: _redact_all_values(item) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_all_values(item) for item in value]
    return "[redacted]"


def _scoped_config_for_health_check(
    name: str,
    scope: MCPConfigScope,
    *,
    source_path: Path | None,
    cwd: str | Path | None = None,
) -> ScopedMCPServerConfig:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    load_result = load_exact_mcp_config(
        name,
        scope=scope,
        cwd=command_cwd,
        source_path=source_path,
        include_pending_project=True,
    )
    if load_result.servers:
        return load_result.servers[0]
    if load_result.warnings:
        _fail(load_result.warnings[0].message)
    _fail(_("MCP server {name!r} not found in {scope} config.").format(name=name, scope=scope.value))
    raise AssertionError("unreachable")


def _expanded_persisted_config(
    name: str,
    scope: MCPConfigScope,
    *,
    source_path: Path | None,
    cwd: str | Path | None = None,
) -> MCPServerConfig:
    return _scoped_config_for_health_check(name, scope, source_path=source_path, cwd=cwd).config


def _optional_expanded_persisted_config(
    name: str,
    scope: MCPConfigScope,
    *,
    source_path: Path | None,
    cwd: str | Path | None = None,
) -> MCPServerConfig | None:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    load_result = load_exact_mcp_config(
        name,
        scope=scope,
        cwd=command_cwd,
        source_path=source_path,
        include_pending_project=True,
    )
    if load_result.servers:
        return load_result.servers[0].config
    return None


def _run_async_blocking(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def _write_config(name: str, config: dict[str, Any], *, scope: str) -> Path:
    try:
        _validate_no_plaintext_secrets(config)
        return write_mcp_server_config(name, config, scope=_parse_scope(scope), cwd=Path.cwd())
    except MCPConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc


def _resolve_scope_option(scope: str | None) -> MCPConfigScope:
    if scope is not None:
        return _parse_scope(scope)
    root = resolve_mcp_workspace_root(Path.cwd())
    if (root / ".git").exists() or (root / ".mcp.json").exists() or (root / ".iac-code").exists():
        return MCPConfigScope.LOCAL
    return MCPConfigScope.USER


def _parse_scope(scope: str) -> MCPConfigScope:
    try:
        parsed = MCPConfigScope(scope)
    except ValueError as exc:
        _fail(_("Invalid MCP scope {scope!r}. Valid values: user, local, project.").format(scope=scope))
        raise AssertionError("unreachable") from exc
    if parsed not in {MCPConfigScope.USER, MCPConfigScope.LOCAL, MCPConfigScope.PROJECT}:
        _fail(_("Scope {scope!r} cannot be used for persisted MCP config.").format(scope=scope))
    return parsed


def _read_config_or_fail(name: str, *, scope: MCPConfigScope) -> dict[str, Any]:
    config = read_mcp_server_config(name, scope=scope, cwd=Path.cwd())
    if config is None:
        _fail(_("MCP server {name!r} not found in {scope} config.").format(name=name, scope=scope.value))
    assert config is not None
    return config


def _resolve_persisted_server_for_command(
    name: str,
    *,
    scope: str | None,
    command: str,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> tuple[MCPConfigScope, dict[str, Any], Path | None]:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    nearest_project = _use_nearest_project_lookup(command)
    explicit_source_path = _canonical_source_path(source_path, cwd=command_cwd) if source_path is not None else None
    if explicit_source_path is not None:
        if scope is None:
            _fail(_("--source-path requires --scope."))
        parsed_scope = _parse_scope(scope)
        try:
            config = read_mcp_server_config(
                name,
                scope=parsed_scope,
                cwd=command_cwd,
                source_path=explicit_source_path,
            )
        except MCPConfigError as exc:
            _fail(str(exc))
            raise AssertionError("unreachable") from exc
        if config is None:
            _fail_missing_or_invalid_persisted_server(
                name,
                parsed_scope,
                source_path=explicit_source_path,
                cwd=command_cwd,
            )
            raise AssertionError("unreachable")
        return parsed_scope, config, explicit_source_path
    if scope is not None:
        return _resolve_explicit_persisted_server_for_command(
            name,
            scope=scope,
            nearest_project=nearest_project,
            cwd=command_cwd,
        )

    matches = find_persisted_mcp_server_matches(name, cwd=command_cwd, nearest_project=nearest_project)
    if not matches:
        _fail(_("MCP server {name!r} not found in persisted MCP config.").format(name=name))
    if len(matches) > 1:
        _fail(_ambiguous_scope_message(name, command=command, matches=matches))
    match = matches[0]
    return match.scope, match.config, match.source_path


def _resolve_raw_persisted_server_for_command(
    name: str,
    *,
    scope: str | None,
    command: str,
    source_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> tuple[MCPConfigScope, Any, Path | None]:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    nearest_project = _use_nearest_project_lookup(command)
    explicit_source_path = _canonical_source_path(source_path, cwd=command_cwd) if source_path is not None else None
    if explicit_source_path is not None:
        if scope is None:
            _fail(_("--source-path requires --scope."))
        parsed_scope = _parse_scope(scope)
        try:
            found, raw_config, resolved_source_path = read_raw_mcp_server_config(
                name,
                scope=parsed_scope,
                cwd=command_cwd,
                source_path=explicit_source_path,
            )
        except MCPConfigError as exc:
            _fail(str(exc))
            raise AssertionError("unreachable") from exc
        if found:
            return parsed_scope, raw_config, resolved_source_path
        _fail_missing_or_invalid_persisted_server(
            name,
            parsed_scope,
            source_path=explicit_source_path,
            cwd=command_cwd,
        )
        raise AssertionError("unreachable")
    if scope is not None:
        parsed_scope = _parse_scope(scope)
        source_path = (
            _project_source_path_for_persisted_lookup(name, nearest_project=nearest_project, cwd=command_cwd)
            if parsed_scope is MCPConfigScope.PROJECT
            else None
        )
        for entry in find_persisted_mcp_server_entries(name, cwd=command_cwd, nearest_project=nearest_project):
            if entry.scope is parsed_scope:
                return entry.scope, entry.config, entry.source_path
        _fail_missing_or_invalid_persisted_server(name, parsed_scope, source_path=source_path, cwd=command_cwd)
        raise AssertionError("unreachable")

    entries = find_persisted_mcp_server_entries(name, cwd=command_cwd, nearest_project=nearest_project)
    if not entries:
        _fail(_("MCP server {name!r} not found in persisted MCP config.").format(name=name))
    if len(entries) > 1:
        _fail(_ambiguous_scope_message(name, command=command, matches=_entries_as_matches(entries)))
    entry = entries[0]
    return entry.scope, entry.config, entry.source_path


def _resolve_explicit_persisted_server_for_command(
    name: str,
    *,
    scope: str,
    nearest_project: bool,
    cwd: str | Path | None = None,
) -> tuple[MCPConfigScope, dict[str, Any], Path | None]:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    parsed_scope = _parse_scope(scope)
    source_path = (
        _project_source_path_for_persisted_lookup(name, nearest_project=nearest_project, cwd=command_cwd)
        if parsed_scope is MCPConfigScope.PROJECT
        else None
    )
    for match in find_persisted_mcp_server_matches(name, cwd=command_cwd, nearest_project=nearest_project):
        if match.scope is parsed_scope:
            return match.scope, match.config, match.source_path
    _fail_missing_or_invalid_persisted_server(name, parsed_scope, source_path=source_path, cwd=command_cwd)
    raise AssertionError("unreachable")


def _use_nearest_project_lookup(command: str) -> bool:
    _ = command
    return True


def _project_source_path_for_persisted_lookup(
    name: str,
    *,
    nearest_project: bool,
    cwd: str | Path | None = None,
) -> Path:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    if nearest_project:
        project_file = find_project_mcp_server_file(name, cwd=command_cwd)
        return project_file if project_file is not None else resolve_mcp_workspace_root(command_cwd) / ".mcp.json"
    return resolve_mcp_workspace_root(command_cwd) / ".mcp.json"


def _canonical_source_path(source_path: str | Path | None, *, cwd: str | Path | None = None) -> Path | None:
    if source_path is None:
        return None
    path = Path(source_path).expanduser()
    if not path.is_absolute():
        base = Path(cwd) if cwd is not None else Path.cwd()
        path = base / path
    return path.resolve(strict=False)


def _fail_missing_or_invalid_persisted_server(
    name: str,
    scope: MCPConfigScope,
    *,
    source_path: Path | None,
    cwd: str | Path | None = None,
) -> None:
    command_cwd = Path(cwd) if cwd is not None else Path.cwd()
    load_result = load_exact_mcp_config(
        name,
        scope=scope,
        cwd=command_cwd,
        source_path=source_path,
        include_pending_project=True,
    )
    if load_result.warnings:
        _fail(load_result.warnings[0].message)
    _fail(_("MCP server {name!r} not found in {scope} config.").format(name=name, scope=scope.value))


def _ambiguous_scope_message(name: str, *, command: str, matches: list[MCPPersistedServerMatch]) -> str:
    lines = [
        _("MCP server {name!r} exists in multiple persisted scopes. Re-run with one of:").format(name=name),
    ]
    lines.extend(_ambiguous_scope_command_line(name, command=command, match=match) for match in matches)
    return "\n".join(lines)


def _ambiguous_scope_command_line(name: str, *, command: str, match: MCPPersistedServerMatch) -> str:
    line = "iac-code mcp {command} {name} --scope {scope}".format(
        command=command,
        name=name,
        scope=match.scope.value,
    )
    if match.source_path is not None:
        line = "{line} --source-path {source}".format(line=line, source=_quote_cli_arg(str(match.source_path)))
    return line


def _quote_cli_arg(value: str) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _entries_as_matches(entries: list[MCPPersistedServerEntry]) -> list[MCPPersistedServerMatch]:
    return [
        MCPPersistedServerMatch(
            scope=entry.scope,
            source_path=entry.source_path,
            config=dict(entry.config) if isinstance(entry.config, dict) else {},
        )
        for entry in entries
    ]


def _oauth_scope_for_cli(
    scope: MCPConfigScope,
    *,
    source_path: Path | None = None,
) -> MCPConfigScope | str | None:
    if scope is MCPConfigScope.LOCAL and source_path is None:
        source_path = resolve_mcp_workspace_root(Path.cwd()) / ".iac-code" / "settings.local.yml"
    elif scope is MCPConfigScope.PROJECT and source_path is None:
        source_path = resolve_mcp_workspace_root(Path.cwd()) / ".mcp.json"
    return oauth_scope_identity(scope, source_path=source_path)


def _server_config_from_mapping(name: str, config: dict[str, Any]):
    from iac_code.mcp.types import MCPServerConfig

    try:
        return MCPServerConfig.from_mapping(name, config)
    except MCPConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc


def _optional_server_config_from_mapping(name: str, config: Any):
    from iac_code.mcp.types import MCPServerConfig

    if not isinstance(config, dict):
        return None
    try:
        return MCPServerConfig.from_mapping(name, config)
    except MCPConfigError:
        return None


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    def redact(value: Any, key: str = "", parent_key: str = "") -> Any:
        if isinstance(value, dict):
            return {item_key: redact(item, str(item_key), key) for item_key, item in value.items()}
        if parent_key == "oauth" and key in {"clientId", "clientSecretEnv"}:
            return "[redacted]"
        if key == "headersHelper":
            return "[redacted]"
        if _is_sensitive_key(key) or (isinstance(value, str) and _is_secret_like_value(value)):
            return "[redacted]"
        return value

    return redact(config)


def _positional_operands(command_or_url: str | None, extra_args: list[str]) -> list[str]:
    operands: list[str] = []
    if command_or_url is not None:
        operands.append(command_or_url)
    operands.extend(extra_args)
    return operands


def _option_was_supplied(ctx: typer.Context, parameter_name: str) -> bool:
    get_parameter_source = getattr(ctx, "get_parameter_source", None)
    if get_parameter_source is None:
        return False
    source = get_parameter_source(parameter_name)
    return getattr(source, "name", "") == "COMMANDLINE"


def _is_url_like(value: str) -> bool:
    return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) is not None


def _looks_like_remote_endpoint(value: str) -> bool:
    normalized = value.rstrip("/")
    return (
        _is_url_like(value)
        or value.startswith("localhost")
        or normalized.endswith("/mcp")
        or normalized.endswith("/sse")
    )


def _is_option_like(value: str) -> bool:
    return value.startswith("-") and value != "-"


def _reject_option_like_command_operand(value: str) -> None:
    if not _is_option_like(value):
        return
    _fail(
        _(
            "Unknown MCP option {option!r}. Put subprocess flags after a command, for example: "
            "iac-code mcp add NAME -- npx --yes mcp-server."
        ).format(option=value)
    )


def _reject_option_like_command_args(values: list[str]) -> None:
    for value in values:
        if _is_option_like(value):
            _reject_option_like_command_operand(value)


def _post_add_health_diagnostic(
    name: str,
    config: dict[str, Any],
    *,
    scope: MCPConfigScope,
) -> MCPHealthDiagnostic | None:
    if not _post_add_health_check_enabled():
        return None
    try:
        server_config = MCPServerConfig.from_mapping(name, config)
    except MCPConfigError:
        return None
    if server_config.transport not in {MCPTransport.HTTP, MCPTransport.SSE} or server_config.oauth is not None:
        return None
    scoped_config = ScopedMCPServerConfig(config=server_config, scope=scope)
    try:
        diagnostics = _run_post_add_health_check(scoped_config)
    except Exception:
        return None
    return diagnostics[0] if diagnostics else None


def _post_add_health_check_enabled() -> bool:
    value = os.environ.get(_ADD_HEALTH_CHECK_ENV)
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _run_post_add_health_check(config: ScopedMCPServerConfig) -> list[MCPHealthDiagnostic]:
    workspace_root = resolve_mcp_workspace_root(Path.cwd())
    return _run_async_blocking(
        check_mcp_configs(
            [config],
            manager_factory=lambda checked: _create_health_check_manager(checked, roots=[workspace_root]),
        )
    )


def _echo_add_success(
    name: str,
    config: dict[str, Any],
    *,
    scope: str,
    diagnostic: MCPHealthDiagnostic | None = None,
) -> None:
    typer.echo(_("Added MCP server {name!r} to {scope} config.").format(name=name, scope=scope))
    transport = str(config.get("type", "stdio"))
    if transport not in {"http", "sse", "ws"}:
        return
    health_command = "iac-code mcp get {name} --scope {scope} --check".format(name=name, scope=scope)
    typer.echo(_("Next: run `{command}` to check MCP server health.").format(command=health_command))
    needs_auth = diagnostic is not None and diagnostic.auth_state == "needs-auth"
    if transport in {"http", "sse"} and (config.get("oauth") or needs_auth):
        auth_command = "iac-code mcp auth {name} --scope {scope}".format(name=name, scope=scope)
        typer.echo(_("Next: run `{command}` to authenticate this MCP server.").format(command=auth_command))


_ENV_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")
_SENSITIVE_NAME_MARKERS = (
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
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:bearer|basic)\s+[^\s,;]+", re.IGNORECASE),
    re.compile(
        r"(?:^|[;\s&,])(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|authorization|password|secret|"
        r"session|sid|jwt)=([^;\s&,]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def _validate_no_plaintext_secrets(config: dict[str, Any]) -> None:
    validate_mcp_config_no_plaintext_secrets(config)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace(" ", "").replace("_", "-")
    alternate = key.lower().replace(" ", "")
    return any(marker in normalized or marker in alternate for marker in _SENSITIVE_NAME_MARKERS)


def _is_secret_like_value(value: str) -> bool:
    if _ENV_REFERENCE_RE.search(value) is not None:
        return False
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def _parse_headers(values: list[str]) -> dict[str, str]:
    return _parse_key_values(values, "--header", allow_colon=True)


def _parse_key_values(values: list[str], option_name: str, *, allow_colon: bool = False) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        equals_index = value.find("=")
        colon_index = value.find(":") if allow_colon else -1
        if colon_index >= 0 and (equals_index < 0 or colon_index < equals_index):
            key, item = value.split(":", 1)
            key = key.strip()
            item = item.strip()
        elif equals_index >= 0:
            key, item = value.split("=", 1)
        else:
            expected = "KEY=VALUE or KEY: VALUE" if allow_colon else "KEY=VALUE"
            _fail(
                _("{option} expects {expected}, got {value!r}.").format(
                    option=option_name,
                    expected=expected,
                    value=value,
                )
            )
        if not key:
            _fail(_("{option} expects a non-empty key.").format(option=option_name))
        parsed[key] = item
    return parsed


def _oauth_config(
    *,
    client_id: str | None,
    client_secret_env: str | None,
    callback_port: int | None,
    auth_server_metadata_url: str | None,
) -> dict[str, Any]:
    oauth: dict[str, Any] = {}
    if client_id:
        oauth["clientId"] = client_id
    if client_secret_env:
        oauth["clientSecretEnv"] = client_secret_env
    if callback_port is not None:
        oauth["callbackPort"] = callback_port
    if auth_server_metadata_url:
        oauth["authServerMetadataUrl"] = auth_server_metadata_url
    return oauth


def _project_file_for(name: str) -> Path:
    project_file = find_project_mcp_server_file(name, cwd=Path.cwd())
    if project_file is None:
        _fail(_("Project MCP server {name!r} not found.").format(name=name))
    assert project_file is not None
    return project_file


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)
