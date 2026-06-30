from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import typer

from iac_code.i18n import _
from iac_code.mcp.config import (
    approve_project_mcp_server,
    find_project_mcp_server_file,
    load_mcp_configs,
    read_mcp_server_config,
    reject_project_mcp_server,
    remove_mcp_server_config,
    reset_project_mcp_server_choices,
    resolve_mcp_workspace_root,
    write_mcp_server_config,
)
from iac_code.mcp.oauth import clear_oauth_state, oauth_scope_identity, oauth_storage_key, run_oauth_loopback_flow
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigError, MCPConfigScope

app = typer.Typer(help=_("Manage MCP servers."), context_settings={"help_option_names": ["-h", "--help"]})


@app.command("add")
def add_server(
    name: str,
    command: str | None = typer.Option(None, "--command", help=_("Command for stdio MCP server.")),
    arg: list[str] | None = typer.Option(None, "--arg", help=_("Command argument. Can be repeated.")),
    env: list[str] | None = typer.Option(None, "--env", help=_("Environment variable KEY=VALUE. Can be repeated.")),
    transport_type: str = typer.Option("stdio", "--type", help=_("Transport type: stdio, http, sse.")),
    url: str | None = typer.Option(None, "--url", help=_("Remote MCP URL for http/sse.")),
    header: list[str] | None = typer.Option(None, "--header", help=_("HTTP header KEY=VALUE. Can be repeated.")),
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
    config: dict[str, Any] = {}
    if transport_type == "stdio":
        if not command:
            _fail(_("--command is required for stdio MCP servers."))
        config["command"] = command
        if sys.platform == "win32" and command == "npx":
            typer.echo(
                _("Warning: on Windows, bare npx may need to be configured as: cmd /c npx"),
                err=True,
            )
        if arg:
            config["args"] = arg
        if env:
            config["env"] = _parse_key_values(env, "--env")
    else:
        config["type"] = transport_type
        if not url:
            _fail(_("--url is required for remote MCP servers."))
        config["url"] = url
        if header:
            config["headers"] = _parse_key_values(header, "--header")

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
        normalized = _server_config_from_mapping(name, stored)
        MCPSecretStorage().set_secret(
            oauth_storage_key(normalized, "client_secret", scope=_oauth_scope_for_cli(name, resolved_scope)),
            client_secret,
        )
    typer.echo(_("Added MCP server {name!r} to {scope} config.").format(name=name, scope=resolved_scope.value))


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
def list_servers() -> None:
    result = load_mcp_configs(cwd=Path.cwd(), include_pending_project=True)
    if not result.servers:
        typer.echo(_("No MCP servers configured."))
        return
    for server in result.servers:
        status = _("approved") if server.approved else _("pending")
        typer.echo("{}\t{}\t{}\t{}".format(server.name, server.scope.value, server.transport.value, status))


@app.command("get")
def get_server(name: str, scope: str = typer.Option("local", "--scope")) -> None:
    config = read_mcp_server_config(name, scope=_parse_scope(scope), cwd=Path.cwd())
    if config is None:
        _fail(_("MCP server {name!r} not found in {scope} config.").format(name=name, scope=scope))
    assert config is not None
    typer.echo(json.dumps(_redact_config(config), ensure_ascii=False, indent=2, sort_keys=True))


@app.command("remove")
def remove_server(name: str, scope: str = typer.Option("local", "--scope")) -> None:
    path = remove_mcp_server_config(name, scope=_parse_scope(scope), cwd=Path.cwd())
    if path is None:
        _fail(_("MCP server {name!r} not found in {scope} config.").format(name=name, scope=scope))
    typer.echo(_("Removed MCP server {name!r} from {path}.").format(name=name, path=path))


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
def auth_server(name: str, scope: str = typer.Option("local", "--scope")) -> None:
    parsed_scope = _parse_scope(scope)
    config = _server_config_from_mapping(name, _read_config_or_fail(name, scope=parsed_scope))
    try:
        run_oauth_loopback_flow(config, storage=MCPSecretStorage(), scope=_oauth_scope_for_cli(name, parsed_scope))
    except Exception as exc:
        _fail(_("MCP auth failed for {name!r}: {error}").format(name=name, error=exc))
    typer.echo(_("Authenticated MCP server {name!r}.").format(name=name))


@app.command("reset-auth")
def reset_auth(name: str, scope: str = typer.Option("local", "--scope")) -> None:
    parsed_scope = _parse_scope(scope)
    config = _server_config_from_mapping(name, _read_config_or_fail(name, scope=parsed_scope))
    clear_oauth_state(config, storage=MCPSecretStorage(), scope=_oauth_scope_for_cli(name, parsed_scope))
    typer.echo(_("Reset stored MCP auth state for {name!r}.").format(name=name))


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


def _oauth_scope_for_cli(name: str, scope: MCPConfigScope) -> MCPConfigScope | str | None:
    source_path: Path | None = None
    if scope is MCPConfigScope.LOCAL:
        source_path = resolve_mcp_workspace_root(Path.cwd()) / ".iac-code" / "settings.local.yml"
    elif scope is MCPConfigScope.PROJECT:
        source_path = _project_file_for(name)
    return oauth_scope_identity(scope, source_path=source_path)


def _server_config_from_mapping(name: str, config: dict[str, Any]):
    from iac_code.mcp.types import MCPServerConfig

    try:
        return MCPServerConfig.from_mapping(name, config)
    except MCPConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    sensitive = {"authorization", "api-key", "apikey", "token", "secret", "password", "accesskeysecret"}

    def redact(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {item_key: redact(item, str(item_key)) for item_key, item in value.items()}
        if any(marker in key.lower() for marker in sensitive):
            return "[redacted]"
        return value

    return redact(config)


_ENV_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")
_SENSITIVE_NAME_MARKERS = (
    "authorization",
    "api-key",
    "api_key",
    "apikey",
    "accesskeysecret",
    "access_key_secret",
    "client_secret",
    "password",
    "secret",
    "token",
)


def _validate_no_plaintext_secrets(config: dict[str, Any]) -> None:
    for section in ("headers", "env"):
        values = config.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if _is_sensitive_key(key) and _ENV_REFERENCE_RE.search(value) is None:
                section_label = "header" if section == "headers" else "env"
                raise MCPConfigError(
                    _(
                        "MCP {section} {key!r} may contain a secret; use an environment variable reference "
                        "like ${{VAR}} instead of storing plaintext."
                    ).format(section=section_label, key=key)
                )


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace(" ", "").replace("_", "-")
    alternate = key.lower().replace(" ", "")
    return any(marker in normalized or marker in alternate for marker in _SENSITIVE_NAME_MARKERS)


def _parse_key_values(values: list[str], option_name: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            _fail(_("{option} expects KEY=VALUE, got {value!r}.").format(option=option_name, value=value))
        key, item = value.split("=", 1)
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


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)
