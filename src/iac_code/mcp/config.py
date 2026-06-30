from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from iac_code.config import _load_yaml, _save_yaml, get_config_dir, get_settings_path
from iac_code.i18n import _
from iac_code.mcp.env_expansion import expand_env
from iac_code.mcp.types import MCPConfigError, MCPConfigScope, MCPConfigWarning, MCPServerConfig, ScopedMCPServerConfig
from iac_code.utils.file_security import atomic_write_text, ensure_private_dir

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESERVED_SERVER_NAMES = {"list_mcp_resources", "read_mcp_resource"}


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
            if not isinstance(name, str) or not name:
                warnings.append(
                    MCPConfigWarning(
                        source=source.label,
                        code="invalid_name",
                        message=_("MCP server names must be non-empty strings."),
                    )
                )
                continue
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
                continue

            expanded, env_warnings = expand_env(raw_config, env=env, source=source.label, server_name=name)
            warnings.extend(env_warnings)
            if any(warning.code == "missing_env" for warning in env_warnings):
                continue
            try:
                config = MCPServerConfig.from_mapping(name, expanded)
            except MCPConfigError as exc:
                warnings.append(
                    MCPConfigWarning(
                        source=source.label,
                        server_name=name,
                        code="invalid_config",
                        message=str(exc),
                    )
                )
                continue

            config_signature = config.content_signature()
            approved = source.scope is not MCPConfigScope.PROJECT or _is_project_server_approved(
                name,
                project_file=source.source_path,
                workspace_root=root,
                config_signature=config_signature,
            )
            scoped = ScopedMCPServerConfig(
                config=config,
                scope=source.scope,
                source_path=str(source.source_path) if source.source_path is not None else None,
                approved=approved,
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
                        warning=warning,
                    )
                )
                continue

            candidates.append(_Candidate(server=scoped, source_order=source.order))

    merged = _merge_candidates(candidates, warnings)
    return MCPConfigLoadResult(servers=merged.servers, warnings=merged.warnings, pending=pending)


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


def read_mcp_server_config(name: str, *, scope: MCPConfigScope, cwd: Path) -> dict[str, Any] | None:
    path = _scope_path(scope, cwd)
    data = _load_scope_data(scope, path)
    servers = data.get("mcpServers")
    if not isinstance(servers, Mapping):
        return None
    value = servers.get(name)
    return dict(value) if isinstance(value, Mapping) else None


def remove_mcp_server_config(name: str, *, scope: MCPConfigScope, cwd: Path) -> Path | None:
    path = _scope_path(scope, cwd)
    data = _load_scope_data(scope, path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return None
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


def _scope_path(scope: MCPConfigScope, cwd: Path) -> Path:
    root = _resolve_workspace_root(cwd, None)
    if scope is MCPConfigScope.USER:
        return get_settings_path()
    if scope is MCPConfigScope.LOCAL:
        return root / ".iac-code" / "settings.local.yml"
    if scope is MCPConfigScope.PROJECT:
        return root / ".mcp.json"
    raise MCPConfigError(_("MCP scope {scope!r} is not a persisted config scope.").format(scope=scope.value))


def _load_scope_data(scope: MCPConfigScope, path: Path) -> dict[str, Any]:
    if scope is MCPConfigScope.PROJECT:
        return _load_json_object(path)
    return _load_yaml(path)


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
    expanded, _warnings = expand_env(
        raw_config,
        env=os.environ,
        source=str(project_file),
        server_name=server_name,
    )
    return MCPServerConfig.from_mapping(server_name, expanded).content_signature()
