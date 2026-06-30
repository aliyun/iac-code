from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentFactoryOptions:
    model: str
    session_id: str | None = None
    cwd: str | None = None
    max_turns: int = 100
    cli_allowed_tools: list[str] | None = None
    cli_disallowed_tools: list[str] | None = None
    cli_permission_mode: str | None = None
    resume_messages: list | None = None
    mcp_configs: list[dict[str, Any]] | None = None
    mcp_manager_factory: Any = None
    mcp_interactive_project_approval: bool = False


@dataclass
class AgentRuntime:
    agent_loop: Any
    session_id: str
    tool_registry: Any
    provider_manager: Any
    command_registry: Any
    task_manager: Any
    memory_manager: Any
    legacy_memory_manager: Any
    mcp_manager: Any | None = None
    mcp_config_warnings: list[Any] | None = None
    _mcp_change_listeners: list[Any] = field(default_factory=list, repr=False)
    _mcp_auth_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    _mcp_auth_flows: set[Any] = field(default_factory=set, repr=False)

    async def aclose(self) -> None:
        await _close_mcp_auth_flows(self._mcp_auth_tasks, self._mcp_auth_flows)
        if self.mcp_manager is not None:
            with contextlib.suppress(Exception):
                await self.mcp_manager.disconnect_all()

    def add_mcp_change_listener(self, listener: Any) -> None:
        self._mcp_change_listeners.append(listener)


def create_agent_runtime(options: AgentFactoryOptions) -> AgentRuntime:
    from loguru import logger

    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.agent.agent_tool import AgentTool
    from iac_code.agent.system_prompt import build_system_prompt
    from iac_code.commands import create_default_registry
    from iac_code.commands.registry import PromptCommand
    from iac_code.config import get_config_dir, load_credentials
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.memory_tools import ReadMemoryTool, WriteMemoryTool
    from iac_code.memory.project_memory import ProjectMemoryRuntime
    from iac_code.memory.recall import MemoryRecallService
    from iac_code.providers.manager import ProviderManager
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.services.session_storage import SessionStorage
    from iac_code.skills.bundled import init_bundled_skills
    from iac_code.skills.discovery import discover_all_skills
    from iac_code.skills.listing import build_skill_listing
    from iac_code.skills.management import build_skill_management_state
    from iac_code.skills.settings import load_disabled_skills
    from iac_code.skills.skill_tool import SkillTool
    from iac_code.tasks.notification_queue import NotificationQueue
    from iac_code.tasks.task_state import TaskManager
    from iac_code.tasks.task_tools import TaskGetTool, TaskListTool, TaskStopTool
    from iac_code.tools.base import ToolRegistry
    from iac_code.tools.cloud.registry import register_cloud_tools

    cwd = options.cwd or os.getcwd()
    session_id = options.session_id or str(uuid.uuid4())[:8]
    runtime_current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    credentials = load_credentials(model=options.model)

    provider_key_override = None
    base_url_override = None

    from iac_code.config import _get_env_overrides, get_llm_source

    env = _get_env_overrides()
    model = options.model

    if env["api_key"]:
        pass  # env overrides handled by load_credentials
    elif get_llm_source() == "qwenpaw":
        from iac_code.services.qwenpaw_source import QwenPawError, load_from_qwenpaw

        try:
            qwenpaw_config = load_from_qwenpaw()
        except QwenPawError as exc:
            import sys

            from rich.console import Console

            Console(stderr=True).print(str(exc), style="bold red")
            sys.exit(1)
        if qwenpaw_config:
            model = qwenpaw_config.model
            credentials = {qwenpaw_config.provider_key: qwenpaw_config.api_key or ""}
            provider_key_override = qwenpaw_config.provider_key
            base_url_override = qwenpaw_config.base_url

    provider_manager = ProviderManager(
        model=model,
        credentials=credentials,
        provider_key_override=provider_key_override,
        base_url_override=base_url_override,
    )

    tool_registry = ToolRegistry()
    tool_registry.register_default_tools()
    register_cloud_tools(tool_registry, CloudCredentials())

    session_storage = SessionStorage()

    memory_runtime = ProjectMemoryRuntime(cwd)
    memory_manager = memory_runtime.memory_manager
    legacy_memory_manager = MemoryManager(memory_dir=str(get_config_dir() / "memory"))
    memory_recall_service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider_manager)
    tool_registry.register(ReadMemoryTool(memory_manager))
    tool_registry.register(WriteMemoryTool(memory_manager))

    task_manager = TaskManager()
    tool_registry.register(TaskListTool(task_manager))
    tool_registry.register(TaskGetTool(task_manager))
    tool_registry.register(TaskStopTool(task_manager))

    notification_queue = NotificationQueue()

    def build_base_system_prompt() -> str:
        return build_system_prompt(
            cwd=cwd,
            memory_context=memory_runtime.build_memory_context(),
            current_time=runtime_current_time,
        )

    base_system_prompt = build_base_system_prompt()
    tool_registry.register(
        AgentTool(
            task_manager=task_manager,
            provider_manager=provider_manager,
            tool_registry=tool_registry,
            system_prompt=base_system_prompt,
            notification_queue=notification_queue,
        )
    )

    init_bundled_skills()
    command_registry = create_default_registry()
    skill_state = build_skill_management_state(discover_all_skills(cwd), load_disabled_skills())
    for cmd in skill_state.enabled_commands:
        existing = command_registry.get(cmd.name)
        if existing is not None and not isinstance(existing, PromptCommand):
            logger.warning("Skill '{}' skipped: conflicts with built-in command", cmd.name)
            continue
        command_registry.register(cmd)

    tool_registry.register(
        SkillTool(
            command_registry=command_registry,
            session_id=session_id,
            cwd=cwd,
            provider_manager=provider_manager,
            tool_registry=tool_registry,
            system_prompt=base_system_prompt,
            disabled_skills=skill_state.disabled_commands,
        )
    )

    mcp_manager = None
    mcp_config_warnings: list[Any] = []
    runtime_mcp_change_listeners: list[Any] = []
    mcp_auth_tasks: set[asyncio.Task[Any]] = set()
    mcp_auth_flows: set[Any] = set()
    from iac_code.mcp.config import load_mcp_configs, resolve_mcp_workspace_root
    from iac_code.mcp.manager import MCPManager

    mcp_workspace_root = resolve_mcp_workspace_root(Path(cwd))
    mcp_load_result = load_mcp_configs(
        cwd=Path(cwd),
        workspace_root=mcp_workspace_root,
        session_configs=_session_mcp_configs(options.mcp_configs),
        include_pending_project=options.mcp_interactive_project_approval,
    )
    mcp_config_warnings = mcp_load_result.warnings
    setup_complete = False
    try:
        if mcp_load_result.servers:
            if options.mcp_manager_factory is not None:
                mcp_manager = options.mcp_manager_factory(mcp_load_result.servers, [mcp_workspace_root])
            else:
                mcp_manager = MCPManager(mcp_load_result.servers, roots=[mcp_workspace_root], session_id=session_id)
            _run_async_blocking(mcp_manager.connect_all())
            mcp_config_warnings.extend(_mcp_connection_warnings(mcp_manager))
            scoped_configs_by_name = {server.name: server for server in mcp_load_result.servers}
            registered_mcp_tool_names: set[str] = set()
            registered_mcp_command_names: set[str] = set()
            registered_mcp_auth_tool_names: set[str] = set()
            registered_mcp_auth_tool_names = _sync_mcp_auth_tools(
                tool_registry,
                scoped_configs_by_name,
                mcp_manager,
                registered_mcp_auth_tool_names,
                auth_tasks=mcp_auth_tasks,
                auth_flows=mcp_auth_flows,
                session_id=session_id,
            )
            registered_mcp_tool_names = _sync_mcp_tool_registry(
                tool_registry,
                mcp_manager,
                session_id,
                registered_mcp_tool_names,
            )
            registered_mcp_command_names, command_warnings = _run_async_blocking(
                _sync_mcp_command_registry(command_registry, mcp_manager, registered_mcp_command_names)
            )
            mcp_config_warnings.extend(command_warnings)

            async def on_mcp_changed(server_name: str, capability: str) -> None:
                nonlocal registered_mcp_tool_names, registered_mcp_command_names, registered_mcp_auth_tool_names
                registered_mcp_auth_tool_names = _sync_mcp_auth_tools(
                    tool_registry,
                    scoped_configs_by_name,
                    mcp_manager,
                    registered_mcp_auth_tool_names,
                    auth_tasks=mcp_auth_tasks,
                    auth_flows=mcp_auth_flows,
                    session_id=session_id,
                )
                if capability in {"tools", "resources", "auth"}:
                    registered_mcp_tool_names = _sync_mcp_tool_registry(
                        tool_registry,
                        mcp_manager,
                        session_id,
                        registered_mcp_tool_names,
                    )
                if capability in {"prompts", "resources"}:
                    registered_mcp_command_names, warnings = await _sync_mcp_command_registry(
                        command_registry,
                        mcp_manager,
                        registered_mcp_command_names,
                    )
                    mcp_config_warnings.extend(warnings)
                _append_new_mcp_connection_warnings(mcp_config_warnings, mcp_manager)
                skill_commands = command_registry.get_model_invocable_skills()
                skill_listing_holder["value"] = build_skill_listing(skill_commands)
                agent_loop.set_auto_trigger_skills(skill_commands)
                agent_loop.set_provider(provider_manager, system_prompt=build_agent_system_prompt())
                for listener in list(runtime_mcp_change_listeners):
                    result = listener(server_name, capability)
                    if asyncio.iscoroutine(result):
                        await result

        from iac_code.services.permissions.loader import load_permission_context
        from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

        permission_context = load_permission_context(
            cwd,
            cli_allowed=options.cli_allowed_tools,
            cli_disallowed=options.cli_disallowed_tools,
            cli_mode=options.cli_permission_mode,
        )
        permission_context.trusted_read_directories.extend(build_session_trusted_read_directories(session_id))

        if hasattr(tool_registry, "get"):
            agent_tool = tool_registry.get("agent")
            if agent_tool is not None and hasattr(agent_tool, "_permission_context"):
                setattr(agent_tool, "_permission_context", permission_context)

        skill_listing_holder = {"value": build_skill_listing(command_registry.get_model_invocable_skills())}

        def build_agent_system_prompt() -> str:
            return build_system_prompt(
                cwd=cwd,
                memory_context=memory_runtime.build_memory_context(),
                skill_listing=skill_listing_holder["value"],
                current_time=runtime_current_time,
            )

        agent_loop = AgentLoop(
            provider_manager=provider_manager,
            system_prompt=build_agent_system_prompt(),
            tool_registry=tool_registry,
            session_storage=session_storage,
            session_id=session_id,
            resume_messages=options.resume_messages,
            max_turns=options.max_turns,
            cwd=cwd,
            permission_context=permission_context,
            auto_trigger_skills=command_registry.get_model_invocable_skills(),
            memory_recall_service=memory_recall_service,
            system_prompt_refresher=build_agent_system_prompt,
        )
        if mcp_manager is not None:
            add_change_listener = getattr(mcp_manager, "add_change_listener", None)
            if add_change_listener is not None:
                add_change_listener(on_mcp_changed)

        runtime = AgentRuntime(
            agent_loop=agent_loop,
            session_id=session_id,
            tool_registry=tool_registry,
            provider_manager=provider_manager,
            command_registry=command_registry,
            task_manager=task_manager,
            memory_manager=memory_manager,
            legacy_memory_manager=legacy_memory_manager,
            mcp_manager=mcp_manager,
            mcp_config_warnings=mcp_config_warnings,
            _mcp_change_listeners=runtime_mcp_change_listeners,
            _mcp_auth_tasks=mcp_auth_tasks,
            _mcp_auth_flows=mcp_auth_flows,
        )
        setup_complete = True
        return runtime
    finally:
        if not setup_complete:
            _cleanup_mcp_runtime_setup(mcp_manager, mcp_auth_tasks, mcp_auth_flows)


def _session_mcp_configs(configs: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]] | None:
    if not configs:
        return None
    normalized: dict[str, dict[str, Any]] = {}
    for config in configs:
        name = config.get("name")
        if not isinstance(name, str) or not name:
            continue
        normalized[name] = {key: value for key, value in config.items() if key != "name"}
    return normalized


def _mcp_connection_warnings(mcp_manager: Any) -> list[Any]:
    from iac_code.i18n import _
    from iac_code.mcp.types import MCPConfigWarning, MCPConnectionState
    from iac_code.utils.public_errors import sanitize_public_text

    list_connections = getattr(mcp_manager, "list_connections", None)
    if list_connections is None:
        return []
    warnings: list[Any] = []
    for record in list_connections():
        state = getattr(record, "state", None)
        if state not in {MCPConnectionState.FAILED, MCPConnectionState.NEEDS_AUTH}:
            continue
        server_name = getattr(record, "name", None)
        state_value = getattr(state, "value", str(state))
        error = sanitize_public_text(getattr(record, "error", None) or state_value)
        code = "needs_auth" if state is MCPConnectionState.NEEDS_AUTH else "connection_failed"
        if state is MCPConnectionState.NEEDS_AUTH:
            message = _("MCP server {server!r} requires authentication: {error}").format(
                server=server_name,
                error=error,
            )
        else:
            message = _("MCP server {server!r} connection failed: {error}").format(
                server=server_name,
                error=error,
            )
        warnings.append(
            MCPConfigWarning(
                source="mcp",
                server_name=server_name,
                code=code,
                message=message,
            )
        )
    for record in list_connections():
        server_name = getattr(record, "name", None)
        capability_errors = getattr(record, "capability_errors", {}) or {}
        for capability, error in capability_errors.items():
            sanitized = sanitize_public_text(error)
            warnings.append(
                MCPConfigWarning(
                    source="mcp",
                    server_name=server_name,
                    code="{}_failed".format(capability),
                    message=_("MCP server {server!r} {capability} discovery failed: {error}").format(
                        server=server_name,
                        capability=capability,
                        error=sanitized,
                    ),
                )
            )
    return warnings


def _append_new_mcp_connection_warnings(existing: list[Any], mcp_manager: Any) -> list[Any]:
    seen = {_mcp_warning_key(warning) for warning in existing}
    added: list[Any] = []
    for warning in _mcp_connection_warnings(mcp_manager):
        key = _mcp_warning_key(warning)
        if key in seen:
            continue
        seen.add(key)
        existing.append(warning)
        added.append(warning)
    return added


def _mcp_warning_key(warning: Any) -> tuple[str, str, str]:
    return (
        str(getattr(warning, "server_name", "")),
        str(getattr(warning, "code", "")),
        str(getattr(warning, "message", warning)),
    )


def _cleanup_mcp_runtime_setup(mcp_manager: Any, auth_tasks: set[asyncio.Task[Any]], auth_flows: set[Any]) -> None:
    if auth_tasks or auth_flows:
        with contextlib.suppress(Exception):
            _run_async_blocking(_close_mcp_auth_flows(auth_tasks, auth_flows))
    if mcp_manager is not None:
        disconnect_all = getattr(mcp_manager, "disconnect_all", None)
        if callable(disconnect_all):
            with contextlib.suppress(Exception):
                _run_async_blocking(disconnect_all())


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
        except BaseException as exc:  # pragma: no cover - exercised through caller failures.
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def _sync_mcp_tool_registry(
    tool_registry: Any,
    mcp_manager: Any,
    session_id: str,
    registered_names: set[str],
) -> set[str]:
    from iac_code.mcp.tools import ListMCPResourcesTool, MCPTool, ReadMCPResourceTool

    records = {record.public_name: record for record in mcp_manager.list_tools()}
    for record in records.values():
        tool_registry.register(MCPTool(manager=mcp_manager, record=record, session_id=session_id))
    desired = set(records)
    if mcp_manager.list_resources():
        if tool_registry.get("list_mcp_resources") is None:
            tool_registry.register(ListMCPResourcesTool(manager=mcp_manager))
        if tool_registry.get("read_mcp_resource") is None:
            tool_registry.register(ReadMCPResourceTool(manager=mcp_manager, session_id=session_id))
        desired.update({"list_mcp_resources", "read_mcp_resource"})
    for name in registered_names - desired:
        tool_registry.unregister(name)
    return desired


def _sync_mcp_auth_tools(
    tool_registry: Any,
    scoped_configs_by_name: dict[str, Any],
    mcp_manager: Any,
    registered_names: set[str],
    *,
    auth_tasks: set[asyncio.Task[Any]] | None = None,
    auth_flows: set[Any] | None = None,
    session_id: str | None = None,
) -> set[str]:
    from iac_code.mcp.tools import MCPAuthenticateTool

    desired: dict[str, str] = {}
    for server_name in getattr(mcp_manager, "needs_auth_servers", lambda: [])():
        desired[_mcp_auth_tool_name(server_name)] = server_name

    for name in registered_names - set(desired):
        tool_registry.unregister(name)
    for name, server_name in desired.items():
        tool_registry.register(
            MCPAuthenticateTool(
                server_name=server_name,
                auth_flow=_mcp_auth_flow_factory(
                    scoped_configs_by_name,
                    mcp_manager,
                    auth_tasks=auth_tasks,
                    auth_flows=auth_flows,
                    session_id=session_id,
                ),
            )
        )
    return set(desired)


async def _sync_mcp_command_registry(
    command_registry: Any,
    mcp_manager: Any,
    registered_names: set[str],
) -> tuple[set[str], list[Any]]:
    from iac_code.mcp.prompts import register_mcp_prompt_commands
    from iac_code.mcp.skills import register_mcp_skill_commands

    for name in registered_names:
        command_registry.unregister(name)
    warnings = register_mcp_prompt_commands(command_registry, mcp_manager)
    warnings.extend(await register_mcp_skill_commands(command_registry, mcp_manager))
    current_names = _current_mcp_command_names(mcp_manager)
    return {name for name in current_names if command_registry.get(name) is not None}, warnings


def _current_mcp_command_names(mcp_manager: Any) -> set[str]:
    names = {record.public_name for record in mcp_manager.list_prompts()}
    for resource in mcp_manager.list_resources():
        if resource.is_skill_resource:
            names.add(resource.public_name or _mcp_resource_command_name(resource))
    return names


def _safe_mcp_identifier(value: str) -> str:
    import re

    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "mcp"


def _mcp_resource_command_name(resource: Any) -> str:
    return "mcp__{}__{}".format(
        _safe_mcp_identifier(resource.server_name),
        _safe_mcp_identifier(resource.name or "skill"),
    )


def _mcp_auth_tool_name(server_name: str) -> str:
    return "mcp__{}__authenticate".format(_safe_mcp_identifier(server_name))


def _mcp_auth_flow_factory(
    scoped_configs_by_name: dict[str, Any],
    mcp_manager: Any,
    *,
    auth_tasks: set[asyncio.Task[Any]] | None = None,
    auth_flows: set[Any] | None = None,
    session_id: str | None = None,
):
    async def authenticate(server_name: str) -> str:
        from iac_code.i18n import _
        from iac_code.mcp.oauth import oauth_scope_identity, start_oauth_loopback_flow
        from iac_code.mcp.storage import MCPSecretStorage

        scoped = scoped_configs_by_name[server_name]
        flow = await asyncio.to_thread(
            start_oauth_loopback_flow,
            scoped.config,
            storage=MCPSecretStorage(),
            scope=oauth_scope_identity(
                scoped.scope,
                source_path=getattr(scoped, "source_path", None),
                session_id=session_id,
            ),
        )
        if auth_flows is not None:
            auth_flows.add(flow)
        task = asyncio.create_task(_complete_mcp_auth_flow(server_name, flow, mcp_manager, auth_flows=auth_flows))
        if auth_tasks is not None:
            auth_tasks.add(task)
            task.add_done_callback(auth_tasks.discard)
        if flow.browser_opened:
            return _("Opened MCP auth URL for {server!r}:\n{url}").format(
                server=server_name,
                url=flow.authorization_url,
            )
        return _("Open this URL to authenticate MCP server {server!r}:\n{url}").format(
            server=server_name,
            url=flow.authorization_url,
        )

    return authenticate


async def _complete_mcp_auth_flow(
    server_name: str,
    flow: Any,
    mcp_manager: Any,
    *,
    auth_flows: set[Any] | None = None,
) -> None:
    try:
        await asyncio.to_thread(flow.wait)
        reconnect = getattr(mcp_manager, "reconnect", None)
        if reconnect is not None:
            await reconnect(server_name)
    except Exception:
        from loguru import logger

        logger.debug("MCP auth flow for '{}' did not complete.", server_name)
    finally:
        if auth_flows is not None:
            auth_flows.discard(flow)


async def _close_mcp_auth_flows(auth_tasks: set[asyncio.Task[Any]], auth_flows: set[Any]) -> None:
    for flow in list(auth_flows):
        _close_mcp_auth_flow(flow)
    for task in list(auth_tasks):
        task.cancel()
    if auth_tasks:
        await asyncio.gather(*list(auth_tasks), return_exceptions=True)
    auth_tasks.clear()
    auth_flows.clear()


def _close_mcp_auth_flow(flow: Any) -> None:
    callback = getattr(flow, "callback", None)
    close = getattr(callback, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()
