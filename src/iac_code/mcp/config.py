from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, cast

from iac_code.config import _load_yaml, _save_yaml, get_config_dir, get_settings_path
from iac_code.i18n import _
from iac_code.mcp.env_expansion import expand_env
from iac_code.mcp.types import (
    MCPConfigError,
    MCPConfigScope,
    MCPConfigWarning,
    MCPServerConfig,
    MCPTransport,
    ScopedMCPServerConfig,
    validate_mcp_config_no_plaintext_secrets,
)
from iac_code.utils.file_security import atomic_write_text, ensure_private_dir

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESERVED_SERVER_NAMES = {"list_mcp_resources", "read_mcp_resource"}
_MISSING_SERVER_CONFIG = object()


@dataclass(frozen=True)
class MCPConfigLoadResult:
    servers: list[ScopedMCPServerConfig]
    warnings: list[MCPConfigWarning]
    pending: list[ScopedMCPServerConfig]

    def by_name(self) -> dict[str, ScopedMCPServerConfig]:
        return {server.name: server for server in self.servers}


@dataclass(frozen=True)
class _ConfigSource:
    scope: MCPConfigScope
    source_path: Path | None
    servers: Mapping[str, Any]
    order: int

    @property
    def label(self) -> str:
        if self.source_path is None:
            return self.scope.value
        return str(self.source_path)


@dataclass(frozen=True)
class MCPPersistedServerMatch:
    scope: MCPConfigScope
    source_path: Path
    config: dict[str, Any]


@dataclass(frozen=True)
class MCPPersistedServerEntry:
    name: str
    scope: MCPConfigScope
    source_path: Path
    config: Any


@dataclass(frozen=True)
class _Candidate:
    server: ScopedMCPServerConfig
    source_order: int


def load_mcp_configs(
    *,
    cwd: Path,
    workspace_root: Path | None = None,
    session_configs: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    include_pending_project: bool = False,
) -> MCPConfigLoadResult:
    root = _resolve_workspace_root(cwd, workspace_root)
    sources = _collect_sources(cwd=Path(cwd), workspace_root=root, session_configs=session_configs)
    warnings: list[MCPConfigWarning] = []
    pending: list[ScopedMCPServerConfig] = []
    candidates: list[_Candidate] = []

    for source in sources:
        for name, raw_config in source.servers.items():
            scoped = _load_scoped_server(
                name,
                raw_config,
                source=source,
                workspace_root=root,
                env=env,
                include_pending_project=include_pending_project,
                warnings=warnings,
                pending=pending,
            )
            if scoped is None:
                continue

            candidates.append(_Candidate(server=scoped, source_order=source.order))

    merged = _merge_candidates(candidates, warnings)
    return MCPConfigLoadResult(servers=merged.servers, warnings=merged.warnings, pending=pending)


def load_all_persisted_mcp_configs(
    *,
    cwd: Path,
    workspace_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    include_pending_project: bool = True,
) -> MCPConfigLoadResult:
    root = _resolve_workspace_root(cwd, workspace_root)
    sources = _collect_sources(cwd=Path(cwd), workspace_root=root, session_configs=None)
    warnings: list[MCPConfigWarning] = []
    pending: list[ScopedMCPServerConfig] = []
    servers: list[ScopedMCPServerConfig] = []

    for source in sources:
        for name, raw_config in source.servers.items():
            scoped = _load_scoped_server(
                name,
                raw_config,
                source=source,
                workspace_root=root,
                env=env,
                include_pending_project=include_pending_project,
                warnings=warnings,
                pending=pending,
            )
            if scoped is not None:
                servers.append(scoped)

    return MCPConfigLoadResult(servers=servers, warnings=warnings, pending=pending)


def load_exact_mcp_config(
    name: str,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    source_path: Path | None = None,
    workspace_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    include_pending_project: bool = True,
) -> MCPConfigLoadResult:
    root = _resolve_workspace_root(cwd, workspace_root)
    path = _resolve_scope_source_path(scope, cwd=Path(cwd), source_path=source_path, workspace_root=root)
    servers = _load_scope_data(scope, path).get("mcpServers")
    if not isinstance(servers, Mapping) or name not in servers:
        return MCPConfigLoadResult(servers=[], warnings=[], pending=[])
    raw_config = servers[name]

    source = _ConfigSource(scope=scope, source_path=path, servers={name: raw_config}, order=0)
    warnings: list[MCPConfigWarning] = []
    pending: list[ScopedMCPServerConfig] = []
    scoped = _load_scoped_server(
        name,
        raw_config,
        source=source,
        workspace_root=root,
        env=env,
        include_pending_project=include_pending_project,
        warnings=warnings,
        pending=pending,
    )
    servers = [scoped] if scoped is not None else []
    return MCPConfigLoadResult(servers=servers, warnings=warnings, pending=pending)


def disable_mcp_server(
    server_name: str,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    source_path: Path | None = None,
    workspace_root: Path | None = None,
) -> ScopedMCPServerConfig:
    return set_mcp_server_disabled(
        server_name,
        scope=scope,
        cwd=cwd,
        source_path=source_path,
        workspace_root=workspace_root,
        disabled=True,
    )


def enable_mcp_server(
    server_name: str,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    source_path: Path | None = None,
    workspace_root: Path | None = None,
) -> ScopedMCPServerConfig:
    return set_mcp_server_disabled(
        server_name,
        scope=scope,
        cwd=cwd,
        source_path=source_path,
        workspace_root=workspace_root,
        disabled=False,
    )


def set_mcp_server_disabled(
    server_name: str,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    disabled: bool,
    source_path: Path | None = None,
    workspace_root: Path | None = None,
) -> ScopedMCPServerConfig:
    root = _resolve_workspace_root(cwd, workspace_root)
    path = _resolve_scope_source_path(scope, cwd=Path(cwd), source_path=source_path, workspace_root=root)
    raw_config = _raw_mcp_server_config(server_name, scope=scope, path=path)
    if raw_config is _MISSING_SERVER_CONFIG:
        raise MCPConfigError(
            _("MCP server {name!r} not found in {scope} config.").format(name=server_name, scope=scope.value)
        )
    try:
        if not isinstance(raw_config, Mapping):
            raise MCPConfigError(_("MCP server {server!r} config must be an object.").format(server=server_name))
        scoped = _raw_scoped_server_config(
            server_name,
            raw_config,
            scope=scope,
            source_path=path,
            workspace_root=root,
            disabled=disabled,
        )
    except MCPConfigError:
        scoped = _raw_invalid_scoped_server_config(
            server_name,
            raw_config,
            scope=scope,
            source_path=path,
            workspace_root=root,
            disabled=disabled,
        )
    state = _load_server_state()
    disabled_servers = state.setdefault("disabled", {})
    if not isinstance(disabled_servers, dict):
        disabled_servers = {}
        state["disabled"] = disabled_servers
    key = _server_state_key(scoped)
    if disabled:
        disabled_servers[key] = _server_state_entry(scoped, disabled=True)
    else:
        disabled_servers.pop(key, None)
    _save_server_state(state)
    return ScopedMCPServerConfig(
        config=scoped.config,
        scope=scoped.scope,
        source_path=scoped.source_path,
        approved=scoped.approved,
        disabled=disabled,
        warning=scoped.warning,
    )


def approve_project_mcp_server(
    server_name: str,
    *,
    project_file: Path,
    workspace_root: Path,
    config_signature: str | None = None,
) -> None:
    state = _load_approval_state()
    approvals = state.setdefault("approvals", {})
    signature = config_signature or _project_config_signature(server_name, project_file)
    approvals[
        _project_approval_key(
            server_name,
            project_file=project_file,
            workspace_root=workspace_root,
            config_signature=signature,
        )
    ] = True
    _save_approval_state(state)


def reject_project_mcp_server(
    server_name: str,
    *,
    project_file: Path,
    workspace_root: Path,
    config_signature: str | None = None,
) -> None:
    state = _load_approval_state()
    approvals = state.setdefault("approvals", {})
    signature = config_signature or _project_config_signature(server_name, project_file)
    approvals[
        _project_approval_key(
            server_name,
            project_file=project_file,
            workspace_root=workspace_root,
            config_signature=signature,
        )
    ] = False
    _save_approval_state(state)


def reset_project_mcp_server_choices() -> None:
    _save_approval_state({"approvals": {}})


def find_project_mcp_server_file(
    server_name: str,
    *,
    cwd: Path,
    workspace_root: Path | None = None,
) -> Path | None:
    root = _resolve_workspace_root(cwd, workspace_root)
    files = _discover_project_files(cwd=Path(cwd), workspace_root=root)
    for project_file in reversed(files):
        servers = _mcp_servers_from_mapping(_load_json_object(project_file))
        if server_name in servers:
            return project_file
    return None


def read_mcp_server_config(
    name: str,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    source_path: Path | None = None,
) -> dict[str, Any] | None:
    path = _resolve_scope_source_path(scope, cwd=Path(cwd), source_path=source_path)
    data = _load_scope_data(scope, path)
    servers = data.get("mcpServers")
    if not isinstance(servers, Mapping):
        return None
    value = servers.get(name)
    return dict(value) if isinstance(value, Mapping) else None


def read_raw_mcp_server_config(
    name: str,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    source_path: Path | None = None,
) -> tuple[bool, Any, Path]:
    path = _resolve_scope_source_path(scope, cwd=Path(cwd), source_path=source_path)
    servers = _mcp_servers_from_mapping(_load_scope_data(scope, path))
    if name not in servers:
        return False, None, path
    return True, servers[name], path


def find_persisted_mcp_server_entries(
    name: str,
    *,
    cwd: Path,
    workspace_root: Path | None = None,
    nearest_project: bool = True,
) -> list[MCPPersistedServerEntry]:
    root = _resolve_workspace_root(cwd, workspace_root)
    entries: list[MCPPersistedServerEntry] = []

    local_path = root / ".iac-code" / "settings.local.yml"
    local_entry = _persisted_server_entry(name, scope=MCPConfigScope.LOCAL, path=local_path)
    if local_entry is not None:
        entries.append(local_entry)

    project_path = (
        find_project_mcp_server_file(name, cwd=cwd, workspace_root=root) if nearest_project else root / ".mcp.json"
    )
    project_entry = (
        _persisted_server_entry(name, scope=MCPConfigScope.PROJECT, path=project_path)
        if project_path is not None
        else None
    )
    if project_entry is not None:
        entries.append(project_entry)

    user_path = get_settings_path()
    user_entry = _persisted_server_entry(name, scope=MCPConfigScope.USER, path=user_path)
    if user_entry is not None:
        entries.append(user_entry)

    return entries


def list_persisted_mcp_server_entries(
    *,
    cwd: Path,
    workspace_root: Path | None = None,
) -> list[MCPPersistedServerEntry]:
    root = _resolve_workspace_root(cwd, workspace_root)
    entries: list[MCPPersistedServerEntry] = []
    sources = _collect_sources(cwd=Path(cwd), workspace_root=root, session_configs=None)
    for source in sources:
        if source.scope is MCPConfigScope.SESSION or source.source_path is None:
            continue
        entries.extend(
            MCPPersistedServerEntry(
                name=name,
                scope=source.scope,
                source_path=source.source_path,
                config=config,
            )
            for name, config in source.servers.items()
        )
    return entries


def find_persisted_mcp_server_matches(
    name: str,
    *,
    cwd: Path,
    workspace_root: Path | None = None,
    nearest_project: bool = True,
) -> list[MCPPersistedServerMatch]:
    root = _resolve_workspace_root(cwd, workspace_root)
    matches: list[MCPPersistedServerMatch] = []

    local_path = root / ".iac-code" / "settings.local.yml"
    local_match = _persisted_server_match(name, scope=MCPConfigScope.LOCAL, path=local_path)
    if local_match is not None:
        matches.append(local_match)

    project_path = (
        find_project_mcp_server_file(name, cwd=cwd, workspace_root=root) if nearest_project else root / ".mcp.json"
    )
    project_match = (
        _persisted_server_match(name, scope=MCPConfigScope.PROJECT, path=project_path)
        if project_path is not None
        else None
    )
    if project_match is not None:
        matches.append(project_match)

    user_path = get_settings_path()
    user_match = _persisted_server_match(name, scope=MCPConfigScope.USER, path=user_path)
    if user_match is not None:
        matches.append(user_match)

    return matches


def remove_mcp_server_config(
    name: str,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    source_path: Path | None = None,
) -> Path | None:
    path = _resolve_scope_source_path(scope, cwd=Path(cwd), source_path=source_path)
    data = _load_scope_data(scope, path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return None
    _clear_mcp_server_disabled_state(
        name,
        servers[name],
        scope=scope,
        cwd=cwd,
        source_path=path,
    )
    servers.pop(name)
    if scope is MCPConfigScope.PROJECT:
        _save_json(path, data)
    else:
        _save_yaml(path, data)
    return path


def write_mcp_server_config(
    name: str,
    config: Mapping[str, Any],
    *,
    scope: MCPConfigScope,
    cwd: Path,
    workspace_root: Path | None = None,
) -> Path:
    name_error = _server_name_error(name)
    if name_error is not None:
        raise MCPConfigError(name_error)
    validate_mcp_config_no_plaintext_secrets(config)
    MCPServerConfig.from_mapping(name, config)
    root = _resolve_workspace_root(cwd, workspace_root)

    if scope is MCPConfigScope.USER:
        path = get_settings_path()
        data = _load_yaml(path)
        _set_mcp_server(data, name, config)
        _save_yaml(path, data)
        return path

    if scope is MCPConfigScope.LOCAL:
        path = root / ".iac-code" / "settings.local.yml"
        data = _load_yaml(path)
        _set_mcp_server(data, name, config)
        _save_yaml(path, data)
        return path

    if scope is MCPConfigScope.PROJECT:
        path = root / ".mcp.json"
        data = _load_json_object(path)
        _set_mcp_server(data, name, config)
        _save_json(path, data)
        return path

    raise MCPConfigError(_("Cannot persist MCP server config to {scope!r} scope.").format(scope=scope.value))


def _scope_path(scope: MCPConfigScope, cwd: Path, workspace_root: Path | None = None) -> Path:
    root = _resolve_workspace_root(cwd, workspace_root)
    if scope is MCPConfigScope.USER:
        return get_settings_path()
    if scope is MCPConfigScope.LOCAL:
        return root / ".iac-code" / "settings.local.yml"
    if scope is MCPConfigScope.PROJECT:
        return root / ".mcp.json"
    raise MCPConfigError(_("MCP scope {scope!r} is not a persisted config scope.").format(scope=scope.value))


def _resolve_scope_source_path(
    scope: MCPConfigScope,
    *,
    cwd: Path,
    source_path: Path | None,
    workspace_root: Path | None = None,
) -> Path:
    if source_path is None:
        return _scope_path(scope, cwd, workspace_root=workspace_root)

    root = _resolve_workspace_root(cwd, workspace_root)
    path = Path(source_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    resolved_path = path.resolve(strict=False)

    if scope is MCPConfigScope.USER:
        allowed = get_settings_path().expanduser().resolve(strict=False)
        if resolved_path == allowed:
            return resolved_path
    elif scope is MCPConfigScope.LOCAL:
        allowed = (root / ".iac-code" / "settings.local.yml").resolve(strict=False)
        if resolved_path == allowed:
            return resolved_path
    elif scope is MCPConfigScope.PROJECT:
        if resolved_path.name == ".mcp.json" and _path_is_within(resolved_path, root.resolve(strict=False)):
            return resolved_path
    else:
        raise MCPConfigError(_("MCP scope {scope!r} is not a persisted config scope.").format(scope=scope.value))

    raise MCPConfigError(
        _("MCP source path {path} does not belong to {scope} scope.").format(path=resolved_path, scope=scope.value)
    )


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _load_scope_data(scope: MCPConfigScope, path: Path) -> dict[str, Any]:
    if scope is MCPConfigScope.PROJECT:
        return _load_json_object(path)
    return _load_yaml(path)


def _persisted_server_match(name: str, *, scope: MCPConfigScope, path: Path) -> MCPPersistedServerMatch | None:
    value = _mcp_servers_from_mapping(_load_scope_data(scope, path)).get(name)
    if not isinstance(value, Mapping):
        return None
    return MCPPersistedServerMatch(scope=scope, source_path=path, config=dict(value))


def _persisted_server_entry(name: str, *, scope: MCPConfigScope, path: Path) -> MCPPersistedServerEntry | None:
    servers = _mcp_servers_from_mapping(_load_scope_data(scope, path))
    if name not in servers:
        return None
    return MCPPersistedServerEntry(name=name, scope=scope, source_path=path, config=servers[name])


def _collect_sources(
    *,
    cwd: Path,
    workspace_root: Path,
    session_configs: Mapping[str, Any] | None,
) -> list[_ConfigSource]:
    order = 0
    sources: list[_ConfigSource] = []

    user_settings_path = get_settings_path()
    sources.append(
        _ConfigSource(
            scope=MCPConfigScope.USER,
            source_path=user_settings_path,
            servers=_mcp_servers_from_mapping(_load_yaml(user_settings_path)),
            order=order,
        )
    )
    order += 1

    for project_file in _discover_project_files(cwd=cwd, workspace_root=workspace_root):
        sources.append(
            _ConfigSource(
                scope=MCPConfigScope.PROJECT,
                source_path=project_file,
                servers=_mcp_servers_from_mapping(_load_json_object(project_file)),
                order=order,
            )
        )
        order += 1

    local_settings_path = workspace_root / ".iac-code" / "settings.local.yml"
    sources.append(
        _ConfigSource(
            scope=MCPConfigScope.LOCAL,
            source_path=local_settings_path,
            servers=_mcp_servers_from_mapping(_load_yaml(local_settings_path)),
            order=order,
        )
    )
    order += 1

    if session_configs:
        sources.append(
            _ConfigSource(
                scope=MCPConfigScope.SESSION,
                source_path=None,
                servers=session_configs,
                order=order,
            )
        )

    return sources


def _merge_candidates(
    candidates: list[_Candidate],
    warnings: list[MCPConfigWarning],
) -> MCPConfigLoadResult:
    by_name: dict[str, _Candidate] = {}
    for candidate in candidates:
        existing = by_name.get(candidate.server.name)
        if existing is None or _candidate_sort_key(candidate) >= _candidate_sort_key(existing):
            by_name[candidate.server.name] = candidate

    by_signature: dict[str, _Candidate] = {}
    for candidate in sorted(by_name.values(), key=_candidate_sort_key):
        signature = candidate.server.config.content_signature()
        existing = by_signature.get(signature)
        if existing is None:
            by_signature[signature] = candidate
            continue
        by_signature[signature] = candidate
        warnings.append(
            MCPConfigWarning(
                source=candidate.server.source_path or candidate.server.scope.value,
                server_name=candidate.server.name,
                code="duplicate_config",
                message=_(
                    "MCP server {existing!r} has the same content signature as {current!r}; "
                    "keeping higher-precedence server {current!r}."
                ).format(existing=existing.server.name, current=candidate.server.name),
            )
        )

    servers = [candidate.server for candidate in sorted(by_signature.values(), key=_candidate_sort_key)]
    return MCPConfigLoadResult(servers=servers, warnings=warnings, pending=[])


def _load_scoped_server(
    name: object,
    raw_config: object,
    *,
    source: _ConfigSource,
    workspace_root: Path,
    env: Mapping[str, str] | None,
    include_pending_project: bool,
    warnings: list[MCPConfigWarning],
    pending: list[ScopedMCPServerConfig],
) -> ScopedMCPServerConfig | None:
    if not isinstance(name, str) or not name:
        warnings.append(
            MCPConfigWarning(
                source=source.label,
                code="invalid_name",
                message=_("MCP server names must be non-empty strings."),
            )
        )
        return None

    name_error = _server_name_error(name)
    if name_error is not None:
        warnings.append(
            MCPConfigWarning(
                source=source.label,
                server_name=name,
                code="invalid_name",
                message=name_error,
            )
        )
        return None

    raw_scoped = _maybe_raw_scoped_server_config(
        name,
        raw_config,
        source=source,
        workspace_root=workspace_root,
        disabled=None,
    )
    if raw_scoped is not None and raw_scoped.disabled:
        return raw_scoped
    raw_invalid = _maybe_disabled_raw_invalid_scoped_server_config(
        name,
        raw_config,
        source=source,
        workspace_root=workspace_root,
    )
    if raw_invalid is not None:
        return raw_invalid

    try:
        _validate_raw_config_no_plaintext_secrets(raw_config)
    except MCPConfigError as exc:
        warnings.append(
            MCPConfigWarning(
                source=source.label,
                server_name=name,
                code="invalid_config",
                message=str(exc),
            )
        )
        return None

    expanded, env_warnings = _expand_env_preserving_headers_helper(
        raw_config,
        env=env,
        source=source.label,
        server_name=name,
    )
    warnings.extend(env_warnings)
    if any(warning.code == "missing_env" for warning in env_warnings):
        return None

    try:
        config = MCPServerConfig.from_mapping(
            name,
            cast(Mapping[str, Any], expanded),
            validate_headers_helper_plaintext=False,
        )
    except MCPConfigError as exc:
        warnings.append(
            MCPConfigWarning(
                source=source.label,
                server_name=name,
                code="invalid_config",
                message=str(exc),
            )
        )
        return None
    if source.source_path is not None:
        config = replace(config, source_dir=str(source.source_path.parent))

    config_signature = config.content_signature()
    approved = source.scope is not MCPConfigScope.PROJECT or _is_project_server_approved(
        name,
        project_file=source.source_path,
        workspace_root=workspace_root,
        config_signature=config_signature,
    )
    disabled = _is_mcp_server_disabled(
        name,
        scope=source.scope,
        source_path=source.source_path,
        state_config_signature=_server_state_config_signature(config),
    )
    scoped = ScopedMCPServerConfig(
        config=config,
        scope=source.scope,
        source_path=str(source.source_path) if source.source_path is not None else None,
        approved=approved,
        disabled=disabled,
    )
    if source.scope is MCPConfigScope.PROJECT and not approved and not include_pending_project:
        warning = MCPConfigWarning(
            source=source.label,
            server_name=name,
            code="pending_approval",
            message=_("Project MCP server {name!r} is pending approval.").format(name=name),
        )
        warnings.append(warning)
        pending.append(
            ScopedMCPServerConfig(
                config=config,
                scope=source.scope,
                source_path=scoped.source_path,
                approved=False,
                disabled=disabled,
                warning=warning,
            )
        )
        return None

    return scoped


def _validate_raw_config_no_plaintext_secrets(raw_config: object) -> None:
    if not isinstance(raw_config, Mapping):
        return
    config = cast(Mapping[str, Any], raw_config)
    validate_mcp_config_no_plaintext_secrets(config, reject_plaintext_values=False)


def _expand_env_preserving_headers_helper(
    raw_config: object,
    *,
    env: Mapping[str, str] | None,
    source: str,
    server_name: str,
) -> tuple[object, list[MCPConfigWarning]]:
    if not isinstance(raw_config, Mapping):
        return expand_env(raw_config, env=env, source=source, server_name=server_name)
    config = cast(Mapping[str, Any], raw_config)
    headers_helper = config.get("headersHelper")
    if not isinstance(headers_helper, str):
        return expand_env(raw_config, env=env, source=source, server_name=server_name)
    config_for_expansion = dict(config)
    config_for_expansion.pop("headersHelper", None)
    expanded, warnings = expand_env(config_for_expansion, env=env, source=source, server_name=server_name)
    if isinstance(expanded, dict):
        expanded["headersHelper"] = headers_helper
    return expanded, warnings


def _raw_mcp_server_config(server_name: str, *, scope: MCPConfigScope, path: Path) -> Any:
    servers = _mcp_servers_from_mapping(_load_scope_data(scope, path))
    if server_name not in servers:
        return _MISSING_SERVER_CONFIG
    return servers[server_name]


def _maybe_raw_scoped_server_config(
    name: str,
    raw_config: object,
    *,
    source: _ConfigSource,
    workspace_root: Path,
    disabled: bool | None,
) -> ScopedMCPServerConfig | None:
    if not isinstance(raw_config, Mapping):
        return None
    raw_mapping = cast(Mapping[str, Any], raw_config)
    try:
        return _raw_scoped_server_config(
            name,
            raw_mapping,
            scope=source.scope,
            source_path=source.source_path,
            workspace_root=workspace_root,
            disabled=disabled,
        )
    except MCPConfigError:
        return None


def _maybe_disabled_raw_invalid_scoped_server_config(
    name: str,
    raw_config: object,
    *,
    source: _ConfigSource,
    workspace_root: Path,
) -> ScopedMCPServerConfig | None:
    try:
        scoped = _raw_invalid_scoped_server_config(
            name,
            raw_config,
            scope=source.scope,
            source_path=source.source_path,
            workspace_root=workspace_root,
            disabled=None,
        )
    except MCPConfigError:
        return None
    return scoped if scoped.disabled else None


def _raw_scoped_server_config(
    name: str,
    raw_config: Mapping[str, Any],
    *,
    scope: MCPConfigScope,
    source_path: Path | None,
    workspace_root: Path,
    disabled: bool | None,
) -> ScopedMCPServerConfig:
    config = MCPServerConfig.from_mapping(name, raw_config)
    if source_path is not None:
        config = replace(config, source_dir=str(source_path.parent))
    config_signature = config.content_signature()
    approved = scope is not MCPConfigScope.PROJECT or _is_project_server_approved(
        name,
        project_file=source_path,
        workspace_root=workspace_root,
        config_signature=config_signature,
    )
    resolved_disabled = (
        _is_mcp_server_disabled(
            name,
            scope=scope,
            source_path=source_path,
            state_config_signature=_server_state_config_signature(config),
        )
        if disabled is None
        else disabled
    )
    return ScopedMCPServerConfig(
        config=config,
        scope=scope,
        source_path=str(source_path) if source_path is not None else None,
        approved=approved,
        disabled=resolved_disabled,
    )


def _raw_invalid_scoped_server_config(
    name: str,
    raw_config: object,
    *,
    scope: MCPConfigScope,
    source_path: Path | None,
    workspace_root: Path,
    disabled: bool | None,
) -> ScopedMCPServerConfig:
    raw = _raw_invalid_config_payload(raw_config)
    transport = _raw_invalid_transport(name, raw_config)
    config = MCPServerConfig(name=name, transport=transport, raw=raw)
    if source_path is not None:
        config = replace(config, source_dir=str(source_path.parent))
    config_signature = config.content_signature()
    approved = scope is not MCPConfigScope.PROJECT or _is_project_server_approved(
        name,
        project_file=source_path,
        workspace_root=workspace_root,
        config_signature=config_signature,
    )
    resolved_disabled = (
        _is_mcp_server_disabled(
            name,
            scope=scope,
            source_path=source_path,
            state_config_signature=_server_state_config_signature(config),
        )
        if disabled is None
        else disabled
    )
    return ScopedMCPServerConfig(
        config=config,
        scope=scope,
        source_path=str(source_path) if source_path is not None else None,
        approved=approved,
        disabled=resolved_disabled,
    )


def _raw_invalid_config_payload(raw_config: object) -> dict[str, Any]:
    if isinstance(raw_config, Mapping):
        return {str(key): value for key, value in raw_config.items()}
    return {"__invalidConfig": raw_config}


def _raw_invalid_transport(name: str, raw_config: object) -> MCPTransport:
    if not isinstance(raw_config, Mapping):
        return MCPTransport.STDIO
    raw_mapping = cast(Mapping[str, Any], raw_config)
    type_value = raw_mapping.get("type")
    if isinstance(type_value, str):
        with suppress(MCPConfigError):
            return MCPTransport.from_value(type_value, server_name=name)
    if "command" in raw_mapping:
        return MCPTransport.STDIO
    return MCPTransport.STDIO


def _candidate_sort_key(candidate: _Candidate) -> tuple[int, int]:
    return (candidate.server.precedence, candidate.source_order)


def _resolve_workspace_root(cwd: Path, workspace_root: Path | None) -> Path:
    if workspace_root is not None:
        return Path(workspace_root).resolve()

    current = Path(cwd).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if (path / ".git").exists():
            return path
    return current


def resolve_mcp_workspace_root(cwd: Path, workspace_root: Path | None = None) -> Path:
    return _resolve_workspace_root(cwd, workspace_root)


def _discover_project_files(*, cwd: Path, workspace_root: Path) -> list[Path]:
    current = Path(cwd).resolve()
    root = workspace_root.resolve()
    if current.is_file():
        current = current.parent
    if current != root and root not in current.parents:
        current = root

    chain: list[Path] = []
    path = current
    while True:
        chain.append(path)
        if path == root:
            break
        path = path.parent

    project_files: list[Path] = []
    for directory in reversed(chain):
        project_file = directory / ".mcp.json"
        if project_file.exists():
            project_files.append(project_file)
    return project_files


def _mcp_servers_from_mapping(data: Mapping[str, Any]) -> Mapping[str, Any]:
    servers = data.get("mcpServers") if isinstance(data, Mapping) else None
    return servers if isinstance(servers, Mapping) else {}


def _set_mcp_server(data: dict[str, Any], name: str, config: Mapping[str, Any]) -> None:
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers[name] = dict(config)


def _server_name_error(name: str) -> str | None:
    if not name:
        return _("MCP server names must be non-empty strings.")
    if name in _RESERVED_SERVER_NAMES or name.startswith("mcp__"):
        return _("MCP server name {name!r} is reserved.").format(name=name)
    if _SERVER_NAME_RE.fullmatch(name) is None:
        return _("MCP server name {name!r} may only contain letters, numbers, dot, underscore, and hyphen.").format(
            name=name
        )
    return None


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, content)


def _approval_state_path() -> Path:
    return get_config_dir() / "mcp" / "project-approvals.json"


def _load_approval_state() -> dict[str, Any]:
    return _load_json_object(_approval_state_path())


def _save_approval_state(state: Mapping[str, Any]) -> None:
    path = _approval_state_path()
    ensure_private_dir(path.parent)
    _save_json(path, state)


def _server_state_path() -> Path:
    return get_config_dir() / "mcp" / "server-states.json"


def _load_server_state() -> dict[str, Any]:
    return _load_json_object(_server_state_path())


def _save_server_state(state: Mapping[str, Any]) -> None:
    path = _server_state_path()
    ensure_private_dir(path.parent)
    _save_json(path, state)


def _is_mcp_server_disabled(
    server_name: str,
    *,
    scope: MCPConfigScope,
    source_path: Path | None,
    state_config_signature: str,
) -> bool:
    if scope not in {MCPConfigScope.USER, MCPConfigScope.LOCAL, MCPConfigScope.PROJECT}:
        return False
    state = _load_server_state()
    disabled = state.get("disabled")
    if not isinstance(disabled, Mapping):
        return False
    key = _server_state_key_from_parts(
        server_name=server_name,
        scope=scope,
        source_path=source_path,
        state_config_signature=state_config_signature,
    )
    entry = disabled.get(key)
    return isinstance(entry, Mapping) and entry.get("disabled") is True


def _clear_mcp_server_disabled_state(
    server_name: str,
    raw_config: Any,
    *,
    scope: MCPConfigScope,
    cwd: Path,
    source_path: Path | None,
) -> None:
    if scope not in {MCPConfigScope.USER, MCPConfigScope.LOCAL, MCPConfigScope.PROJECT}:
        return
    root = _resolve_workspace_root(cwd, None)
    try:
        if not isinstance(raw_config, Mapping):
            raise MCPConfigError(_("MCP server {server!r} config must be an object.").format(server=server_name))
        scoped = _raw_scoped_server_config(
            server_name,
            raw_config,
            scope=scope,
            source_path=source_path,
            workspace_root=root,
            disabled=False,
        )
    except MCPConfigError:
        scoped = _raw_invalid_scoped_server_config(
            server_name,
            raw_config,
            scope=scope,
            source_path=source_path,
            workspace_root=root,
            disabled=False,
        )
    state = _load_server_state()
    disabled_servers = state.get("disabled")
    if not isinstance(disabled_servers, dict):
        return
    key = _server_state_key(scoped)
    if key in disabled_servers:
        disabled_servers.pop(key, None)
        _save_server_state(state)


def _server_state_key(scoped: ScopedMCPServerConfig) -> str:
    return _server_state_key_from_parts(
        server_name=scoped.name,
        scope=scoped.scope,
        source_path=Path(scoped.source_path) if scoped.source_path is not None else None,
        state_config_signature=_server_state_config_signature(scoped.config),
    )


def _server_state_key_from_parts(
    *,
    server_name: str,
    scope: MCPConfigScope,
    source_path: Path | None,
    state_config_signature: str,
) -> str:
    material = {
        "scope": scope.value,
        "scopeIdentity": _scope_identity(scope, source_path),
        "serverName": server_name,
        "sourceIdentity": _source_identity(source_path),
        "stateConfigSignature": state_config_signature,
    }
    data = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _server_state_config_signature(config: MCPServerConfig) -> str:
    if config.oauth is None:
        oauth = None
    else:
        oauth = {
            "clientId": config.oauth.client_id,
            "clientSecretEnv": config.oauth.client_secret_env,
            "callbackPort": config.oauth.callback_port,
            "authServerMetadataUrl": config.oauth.auth_server_metadata_url,
            "clientMetadataUrl": config.oauth.client_metadata_url,
        }
    material = {
        "transport": config.transport.value,
        "command": config.command,
        "args": list(config.args),
        "env": config.env,
        "url": config.url,
        "headers": config.headers,
        "headersHelper": config.headers_helper,
        "oauth": oauth,
        "raw": config.raw,
    }
    data = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.pbkdf2_hmac("sha256", data, b"iac-code-mcp-server-state-signature-v1", 100_000).hex()
    prefix = "stdio" if config.transport is MCPTransport.STDIO else "url"
    return "{}:state:{}".format(prefix, digest)


def _server_state_entry(scoped: ScopedMCPServerConfig, *, disabled: bool) -> dict[str, Any]:
    return {
        "disabled": disabled,
        "scope": scoped.scope.value,
        "scopeIdentity": _scope_identity(
            scoped.scope,
            Path(scoped.source_path) if scoped.source_path is not None else None,
        ),
        "serverName": scoped.name,
        "sourceIdentity": _source_identity(Path(scoped.source_path) if scoped.source_path is not None else None),
        "configSignature": scoped.config.content_signature(),
        "stateConfigSignature": _server_state_config_signature(scoped.config),
    }


def _scope_identity(scope: MCPConfigScope, source_path: Path | None) -> str:
    if scope is MCPConfigScope.USER:
        return scope.value
    if source_path is None:
        return scope.value
    return "{}:{}".format(scope.value, _source_identity(source_path))


def _source_identity(source_path: Path | None) -> str:
    if source_path is None:
        return ""
    return Path(source_path).expanduser().resolve().as_posix()


def _is_project_server_approved(
    server_name: str,
    *,
    project_file: Path | None,
    workspace_root: Path,
    config_signature: str,
) -> bool:
    if project_file is None:
        return False
    state = _load_approval_state()
    approvals = state.get("approvals")
    if not isinstance(approvals, Mapping):
        return False
    key = _project_approval_key(
        server_name,
        project_file=project_file,
        workspace_root=workspace_root,
        config_signature=config_signature,
    )
    return approvals.get(key) is True


def _project_approval_key(
    server_name: str,
    *,
    project_file: Path,
    workspace_root: Path,
    config_signature: str,
) -> str:
    material = "\0".join(
        [
            str(Path(workspace_root).resolve()),
            str(Path(project_file).resolve()),
            server_name,
            config_signature,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _project_config_signature(server_name: str, project_file: Path) -> str:
    raw_config = _mcp_servers_from_mapping(_load_json_object(project_file)).get(server_name)
    _validate_raw_config_no_plaintext_secrets(raw_config)
    expanded, _warnings = _expand_env_preserving_headers_helper(
        raw_config,
        env=os.environ,
        source=str(project_file),
        server_name=server_name,
    )
    return MCPServerConfig.from_mapping(
        server_name,
        cast(Mapping[str, Any], expanded),
        validate_headers_helper_plaintext=False,
    ).content_signature()
