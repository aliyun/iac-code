"""Interactive MCP management command."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iac_code.i18n import _
from iac_code.mcp import cli as mcp_cli
from iac_code.mcp.config import (
    disable_mcp_server,
    enable_mcp_server,
    find_persisted_mcp_server_entries,
    find_project_mcp_server_file,
    list_persisted_mcp_server_entries,
    load_all_persisted_mcp_configs,
    read_raw_mcp_server_config,
)
from iac_code.mcp.types import MCPConfigError, MCPConfigScope, MCPConnectionState


@dataclass(frozen=True)
class _QuickCommandArgs:
    target: str
    scope: str | None = None
    source_path: str | None = None
    error: str | None = None


async def mcp_command(context=None, args: list[str] | None = None, **kwargs: Any) -> str | None:
    if context is None:
        return _("MCP command requires a context.")
    handled = await _handle_quick_command(context, [str(arg) for arg in args or [] if str(arg)])
    if handled is not None:
        return handled
    dialog_class = _mcp_manager_dialog_class()
    dialog = dialog_class(context)
    empty_message = getattr(dialog, "empty_message_if_no_servers", None)
    if callable(empty_message):
        message = empty_message()
        if message is not None:
            return str(message)
    return dialog.run()


async def _handle_quick_command(context: Any, args: list[str]) -> str | None:
    if not args:
        return None
    command = args[0]
    parsed = _parse_quick_command_args(args[1:])
    if parsed.error is not None:
        return parsed.error
    target = parsed.target
    if command == "reconnect" and target:
        return await _reconnect_live_mcp_server(context, target, scope=parsed.scope, source_path=parsed.source_path)
    if command in {"enable", "disable"}:
        target = target or "all"
        if target == "all" and (parsed.scope is not None or parsed.source_path is not None):
            return _("--scope or --source-path cannot be used with /mcp {command} all.").format(command=command)
        if target == "all":
            return await _toggle_all_mcp_servers(context, command)
        return await _toggle_named_mcp_server(
            context,
            command,
            target,
            scope=parsed.scope,
            source_path=parsed.source_path,
        )
    return None


def _parse_quick_command_args(args: list[str]) -> _QuickCommandArgs:
    target_parts: list[str] = []
    scope: str | None = None
    source_path: str | None = None
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--scope":
            if index + 1 >= len(args):
                return _QuickCommandArgs("", error=_("--scope requires a value."))
            scope = args[index + 1]
            index += 2
            continue
        if value.startswith("--scope="):
            scope = value.split("=", 1)[1]
            index += 1
            continue
        if value == "--source-path":
            if index + 1 >= len(args):
                return _QuickCommandArgs("", error=_("--source-path requires a value."))
            source_path = args[index + 1]
            index += 2
            continue
        if value.startswith("--source-path="):
            source_path = value.split("=", 1)[1]
            index += 1
            continue
        target_parts.append(value)
        index += 1
    if source_path is not None and scope is None:
        return _QuickCommandArgs("", error=_("Error: --source-path requires --scope."))
    return _QuickCommandArgs(" ".join(target_parts).strip(), scope=scope, source_path=source_path)


async def _toggle_all_mcp_servers(context: Any, action: str) -> str:
    cwd = _mcp_command_cwd(context)
    result = load_all_persisted_mcp_configs(cwd=cwd, include_pending_project=True)
    is_enable = action == "enable"
    servers = [server for server in result.servers if bool(getattr(server, "disabled", False)) is is_enable]
    raw_servers = _raw_unloaded_warning_servers_for_disable(cwd, result) if action == "disable" else []
    if not servers:
        if raw_servers:
            return await _toggle_raw_mcp_servers(context, action, raw_servers)
        state = _("enabled") if is_enable else _("disabled")
        return _("All MCP servers are already {state}").format(state=state)

    for server in servers:
        _toggle_persisted_mcp_server(cwd, action, server)
    for server in raw_servers:
        _toggle_persisted_mcp_server(cwd, action, server)
    await _refresh_live_runtime(context)
    verb = _("Enabled") if is_enable else _("Disabled")
    return _("{verb} {count} MCP server(s)").format(verb=verb, count=len(servers) + len(raw_servers))


def _toggle_named_message(name: str, action: str) -> str:
    state = _("enabled") if action == "enable" else _("disabled")
    return _('MCP server "{name}" {state}').format(name=name, state=state)


async def _toggle_named_mcp_server(
    context: Any,
    action: str,
    name: str,
    *,
    scope: str | None = None,
    source_path: str | None = None,
) -> str:
    cwd = _mcp_command_cwd(context)
    if source_path is not None and scope is not None:
        return await _toggle_exact_source_mcp_server(context, action, name, scope=scope, source_path=source_path)
    result = load_all_persisted_mcp_configs(cwd=cwd, include_pending_project=True)
    matches = _matching_persisted_servers_for_action(result, name, scope=scope)
    if action == "disable":
        matches = _unique_persisted_server_matches(
            [
                *matches,
                *_matching_raw_unloaded_warning_servers_for_action(cwd, result, name, scope=scope),
            ]
        )
    if source_path is not None:
        requested_source_path = _canonical_source_path(source_path, cwd=cwd)
        matches = [match for match in matches if _same_path(getattr(match, "source_path", None), requested_source_path)]
    if not matches:
        return _('MCP server "{name}" not found').format(name=name)
    if source_path is None and scope == MCPConfigScope.PROJECT.value:
        matches = _nearest_project_source_matches(cwd, name, matches)
    if len(matches) > 1:
        return _quick_ambiguous_scope_message(name, action, matches)
    _toggle_persisted_mcp_server(cwd, action, matches[0])
    await _refresh_live_runtime(context)
    return _toggle_named_message(name, action)


async def _toggle_exact_source_mcp_server(
    context: Any,
    action: str,
    name: str,
    *,
    scope: str,
    source_path: str,
) -> str:
    cwd = _mcp_command_cwd(context)
    try:
        parsed_scope = MCPConfigScope(scope)
    except ValueError:
        return _("Invalid MCP scope {scope!r}. Valid values: user, local, project.").format(scope=scope)
    if parsed_scope not in {MCPConfigScope.USER, MCPConfigScope.LOCAL, MCPConfigScope.PROJECT}:
        return _("Scope {scope!r} cannot be used for persisted MCP config.").format(scope=scope)
    requested_source_path = _canonical_source_path(source_path, cwd=cwd)
    toggle = enable_mcp_server if action == "enable" else disable_mcp_server
    try:
        toggle(name, scope=parsed_scope, cwd=cwd, source_path=requested_source_path)
    except MCPConfigError as exc:
        message = str(exc)
        if "not found" in message:
            return _('MCP server "{name}" not found').format(name=name)
        return _("Error: {error}").format(error=message)
    await _refresh_live_runtime(context)
    return _toggle_named_message(name, action)


def _matching_persisted_servers_for_action(result: Any, name: str, *, scope: str | None) -> list[Any]:
    servers = [*getattr(result, "servers", []), *getattr(result, "pending", [])]
    return [
        server
        for server in servers
        if getattr(server, "name", None) == name
        and (scope is None or _scope_value(getattr(server, "scope", None)) == scope)
    ]


def _matching_raw_unloaded_warning_servers_for_action(
    cwd: Path,
    result: Any,
    name: str,
    *,
    scope: str | None,
) -> list[Any]:
    return [
        server
        for server in _raw_unloaded_warning_servers_for_disable(cwd, result)
        if getattr(server, "name", None) == name
        and (scope is None or _scope_value(getattr(server, "scope", None)) == scope)
    ]


def _unique_persisted_server_matches(matches: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[tuple[Any, str | None, str | None]] = set()
    for match in matches:
        identity = _persisted_server_identity(
            getattr(match, "name", None),
            getattr(match, "scope", None),
            getattr(match, "source_path", None),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(match)
    return unique


def _nearest_project_source_matches(cwd: Path, name: str, matches: list[Any]) -> list[Any]:
    nearest = find_project_mcp_server_file(name, cwd=cwd)
    if nearest is None:
        return matches
    filtered = [
        match
        for match in matches
        if _scope_value(getattr(match, "scope", None)) != MCPConfigScope.PROJECT.value
        or _same_path(getattr(match, "source_path", None), nearest)
    ]
    return filtered or matches


def _same_path(left: Any, right: Path | None) -> bool:
    if left is None or right is None:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return Path(left) == right


def _quick_ambiguous_scope_message(name: str, command: str, matches: list[Any]) -> str:
    return mcp_cli._ambiguous_scope_message(name, command=command, matches=matches)


def _scope_value(scope: Any) -> str | None:
    value = getattr(scope, "value", scope)
    return str(value) if value is not None else None


def _persisted_mcp_server_exists(
    context: Any,
    name: str,
    *,
    scope: str | None = None,
    source_path: str | None = None,
) -> bool:
    cwd = _mcp_command_cwd(context)
    result = load_all_persisted_mcp_configs(cwd=cwd, include_pending_project=True)
    pending = getattr(result, "pending", [])
    represented = [*result.servers, *pending]
    if source_path is not None:
        requested_source_path = _canonical_source_path(source_path, cwd=cwd)
        represented = [
            server for server in represented if _same_path(getattr(server, "source_path", None), requested_source_path)
        ]
    if scope is None and any(getattr(server, "name", None) == name for server in represented):
        return True
    if scope is not None and any(
        getattr(server, "name", None) == name and getattr(getattr(server, "scope", None), "value", None) == scope
        for server in represented
    ):
        return True
    warning_names = _unloaded_warning_server_names(result)
    entries = find_persisted_mcp_server_entries(name, cwd=cwd)
    if scope is not None:
        entries = [entry for entry in entries if getattr(entry.scope, "value", None) == scope]
    if source_path is not None:
        requested_source_path = _canonical_source_path(source_path, cwd=cwd)
        entries = [entry for entry in entries if _same_path(entry.source_path, requested_source_path)]
    return name in warning_names and bool(entries)


async def _toggle_raw_mcp_servers(context: Any, action: str, servers: list[Any]) -> str:
    cwd = _mcp_command_cwd(context)
    for server in servers:
        _toggle_persisted_mcp_server(cwd, action, server)
    await _refresh_live_runtime(context)
    verb = _("Enabled") if action == "enable" else _("Disabled")
    return _("{verb} {count} MCP server(s)").format(verb=verb, count=len(servers))


def _toggle_persisted_mcp_server(cwd: Path, action: str, server: Any) -> None:
    name = getattr(server, "name", None)
    scope = getattr(server, "scope", None)
    if isinstance(name, str) and isinstance(scope, MCPConfigScope):
        source_path = getattr(server, "source_path", None)
        path = Path(source_path) if source_path is not None else None
        toggle = enable_mcp_server if action == "enable" else disable_mcp_server
        toggle(name, scope=scope, cwd=cwd, source_path=path)
        return
    handler = getattr(mcp_cli, "{command}_mcp_server_command".format(command=action))
    scope_value = getattr(scope, "value", None)
    handler(name, scope=scope_value)


def _raw_unloaded_warning_servers_for_disable(cwd: Path, result: Any) -> list[Any]:
    warning_names = _unloaded_warning_server_names(result)
    if not warning_names:
        return []
    represented_servers = [*getattr(result, "servers", []), *getattr(result, "pending", [])]
    represented = {
        _persisted_server_identity(
            getattr(server, "name", None),
            getattr(server, "scope", None),
            getattr(server, "source_path", None),
        )
        for server in represented_servers
    }
    raw_servers = []
    for server in list_persisted_mcp_server_entries(cwd=cwd):
        identity = _persisted_server_identity(server.name, server.scope, server.source_path)
        if server.name in warning_names and identity not in represented:
            raw_servers.append(server)
            represented.add(identity)
    return raw_servers


def _persisted_server_identity(name: Any, scope: Any, source_path: Any) -> tuple[Any, str | None, str | None]:
    scope_value = getattr(scope, "value", scope)
    return (
        name,
        str(scope_value) if scope_value is not None else None,
        str(source_path) if source_path is not None else None,
    )


def _unloaded_warning_server_names(result: Any) -> set[str]:
    return {
        warning.server_name
        for warning in getattr(result, "warnings", [])
        if getattr(warning, "code", None) in {"missing_env", "invalid_config"} and isinstance(warning.server_name, str)
    }


async def _reconnect_live_mcp_server(
    context: Any,
    name: str,
    *,
    scope: str | None = None,
    source_path: str | None = None,
) -> str:
    cwd = _mcp_command_cwd(context)
    resolved_source_path = str(_canonical_source_path(source_path, cwd=cwd)) if source_path is not None else None
    manager = _live_mcp_manager(context)
    if manager is not None and _live_mcp_server_matches(manager, name, scope, source_path=resolved_source_path):
        reconnect = getattr(manager, "reconnect", None)
        if not callable(reconnect):
            return _reconnect_result_message(name, "failed")
        try:
            value = reconnect(name)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            return _("Error: {error}").format(error=str(exc) or exc.__class__.__name__)
        state = _live_mcp_connection_state(manager, name)
        return _reconnect_result_message(name, state)
    if scope is not None or source_path is not None:
        if not _persisted_mcp_server_exists_for_scope(name, scope=scope, cwd=cwd, source_path=resolved_source_path):
            return _('MCP server "{name}" not found').format(name=name)
        diagnostics = mcp_cli.reconnect_mcp_server(name=name, scope=scope, source_path=resolved_source_path, cwd=cwd)
        if not diagnostics:
            return _('MCP server "{name}" not found').format(name=name)
        state = getattr(diagnostics[0], "connection_state", None) or getattr(diagnostics[0], "status", "failed")
        return _reconnect_result_message(name, str(state))
    return _('MCP server "{name}" not found').format(name=name)


def _reconnect_result_message(name: str, state: str) -> str:
    normalized = state.replace("_", "-")
    if normalized == MCPConnectionState.CONNECTED.value.replace("_", "-"):
        return _("Successfully reconnected to {name}").format(name=name)
    if normalized == MCPConnectionState.NEEDS_AUTH.value.replace("_", "-"):
        return _("{name} requires authentication. Use /mcp to authenticate.").format(name=name)
    return _("Failed to reconnect to {name}").format(name=name)


def _persisted_mcp_server_exists_for_scope(
    name: str,
    *,
    scope: str | None,
    cwd: Path,
    source_path: str | None = None,
) -> bool:
    try:
        parsed_scope = MCPConfigScope(scope) if scope is not None else None
    except ValueError:
        return False
    entries = [
        entry
        for entry in find_persisted_mcp_server_entries(name, cwd=cwd)
        if parsed_scope is None or entry.scope is parsed_scope
    ]
    if source_path is not None:
        requested_source_path = _canonical_source_path(source_path, cwd=cwd)
        if parsed_scope is not None:
            try:
                found, _raw_config, _resolved_source_path = read_raw_mcp_server_config(
                    name,
                    scope=parsed_scope,
                    cwd=cwd,
                    source_path=requested_source_path,
                )
            except MCPConfigError:
                return False
            return found
        entries = [entry for entry in entries if _same_path(entry.source_path, requested_source_path)]
        return bool(entries)
    if parsed_scope is None:
        return bool(entries)
    if parsed_scope is not MCPConfigScope.PROJECT:
        return bool(entries)
    nearest = find_project_mcp_server_file(name, cwd=cwd)
    if nearest is None:
        return False
    return any(_same_path(entry.source_path, nearest) for entry in entries)


def _live_mcp_manager(context: Any) -> Any:
    repl = getattr(context, "repl", None)
    return getattr(repl, "_mcp_manager", None) if repl is not None else None


def _canonical_source_path(source_path: str | Path | None, *, cwd: str | Path | None = None) -> Path | None:
    return mcp_cli._canonical_source_path(source_path, cwd=cwd)


def _live_mcp_server_exists(manager: Any, name: str) -> bool:
    return _live_mcp_server_matches(manager, name, None)


def _live_mcp_server_matches(manager: Any, name: str, scope: str | None, *, source_path: str | None = None) -> bool:
    connection = getattr(manager, "connection", None)
    if callable(connection):
        try:
            record = connection(name)
        except Exception:
            return False
        record_scope = _record_scope_value(record)
        return (scope is None or record_scope is None or record_scope == scope) and _record_source_path_matches(
            record,
            source_path,
        )
    list_connections = getattr(manager, "list_connections", None)
    if callable(list_connections):
        try:
            return any(
                getattr(record, "name", None) == name
                and (scope is None or _record_scope_value(record) is None or _record_scope_value(record) == scope)
                and _record_source_path_matches(record, source_path)
                for record in list_connections()
            )
        except Exception:
            return False
    return False


def _record_scope_value(record: Any) -> str | None:
    scoped_config = getattr(record, "scoped_config", None)
    return _scope_value(getattr(scoped_config, "scope", None))


def _record_source_path_matches(record: Any, source_path: str | None) -> bool:
    if source_path is None:
        return True
    scoped_config = getattr(record, "scoped_config", None)
    record_source_path = getattr(scoped_config, "source_path", None)
    return _same_path(record_source_path, Path(source_path))


def _live_mcp_connection_state(manager: Any, name: str) -> str:
    connection_state = getattr(manager, "connection_state", None)
    if callable(connection_state):
        try:
            state = connection_state(name)
        except Exception:
            state = None
        state_value = getattr(state, "value", state)
        if state_value:
            return str(state_value)
    connection = getattr(manager, "connection", None)
    if callable(connection):
        try:
            record = connection(name)
        except Exception:
            return "unknown"
        state = getattr(record, "state", None)
        return str(getattr(state, "value", state or "unknown"))
    return "unknown"


def _mcp_command_cwd(context: Any) -> Path:
    repl = getattr(context, "repl", None)
    cwd = getattr(repl, "_original_cwd", None) if repl is not None else None
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


async def _refresh_live_runtime(context: Any) -> None:
    repl = getattr(context, "repl", None)
    refresh = getattr(repl, "refresh_mcp_integrations", None) if repl is not None else None
    if not callable(refresh):
        return
    value = refresh()
    if inspect.isawaitable(value):
        await value


def _mcp_manager_dialog_class() -> Any:
    from iac_code.ui.dialogs.mcp_manager import MCPManagerDialog

    return MCPManagerDialog
