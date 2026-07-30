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
    request_policy_override: Any = None
    cli_allowed_tools: list[str] | None = None
    cli_disallowed_tools: list[str] | None = None
    cli_permission_mode: str | None = None
    resume_messages: list | None = None
    mcp_configs: list[dict[str, Any]] | None = None
    mcp_manager_factory: Any = None
    mcp_interactive_project_approval: bool = False
    a2a_safe_mode: bool = False
    # 离线上下文核算契约:仅为算系统提示 + 本地工具定义开销构造 runtime 时置真。
    # 显式禁止连接 MCP / 读取 MCP 钥匙串等外部副作用,但保留完整本地工具注册以保证 token 口径准确。
    disable_external_services: bool = False
    mcp_elicitation_handler: Any = None
    provider_key_override: str | None = None
    provider_api_key_override: str | None = field(default=None, repr=False)
    provider_base_url_override: str | None = None
    provider_config_frozen: bool = False
    effort_override: str | None = None
    provider_config_override: dict[str, Any] | None = field(default=None, repr=False)


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
    aliyun_services: Any | None = None
    _cloud_tools_refresher: Any | None = field(default=None, repr=False)
    mcp_manager: Any | None = None
    mcp_config_warnings: list[Any] | None = None
    mcp_pending_configs: list[Any] | None = None
    _mcp_change_listeners: list[Any] = field(default_factory=list, repr=False)
    _mcp_auth_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    _mcp_auth_flows: set[Any] = field(default_factory=set, repr=False)
    _aliyun_services_closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        try:
            await _close_mcp_auth_flows(self._mcp_auth_tasks, self._mcp_auth_flows)
            if self.mcp_manager is not None:
                with contextlib.suppress(Exception):
                    await self.mcp_manager.disconnect_all()
        finally:
            if self.aliyun_services is not None and not self._aliyun_services_closed:
                try:
                    await self.aliyun_services.aclose()
                except Exception:
                    pass
                else:
                    self._aliyun_services_closed = True

    def add_mcp_change_listener(self, listener: Any) -> None:
        self._mcp_change_listeners.append(listener)

    def refresh_cloud_tools(self) -> None:
        if callable(self._cloud_tools_refresher):
            self._cloud_tools_refresher()


_A2A_SAFE_NORMAL_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_files",
        "glob",
        "grep",
        "aliyun_api",
        "aliyun_doc_search",
        "aliyun_api_doc",
        "ros_stack",
        "skill",
        "read_memory",
        "write_memory",
    }
)


def create_agent_runtime(options: AgentFactoryOptions) -> AgentRuntime:
    from iac_code.config import get_config_dir
    from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services

    aliyun_services = create_aliyun_runtime_services(cache_dir=get_config_dir() / "openmeta-cache")
    try:
        return _create_agent_runtime(options, aliyun_services)
    except BaseException:
        with contextlib.suppress(Exception):
            _run_async_blocking(aliyun_services.aclose())
        raise


def _create_agent_runtime(options: AgentFactoryOptions, aliyun_services: Any) -> AgentRuntime:
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

    credentials = {} if options.provider_config_frozen else load_credentials(model=options.model)

    provider_key_override = options.provider_key_override
    base_url_override = options.provider_base_url_override if options.provider_config_frozen else None
    # 会话级显式 provider 覆盖时,让 ProviderManager 忽略全局 llm_source 合作方源(如 QwenPaw)的热切换,
    # 否则 stream() 每轮开头的 _check_qwenpaw_config_change 会把本会话选定的 provider 改回。
    ignore_llm_source = False

    from iac_code.config import _get_env_overrides, get_llm_source

    env = _get_env_overrides()
    model = options.model

    if options.provider_config_frozen:
        credentials = (
            {provider_key_override: options.provider_api_key_override or ""}
            if provider_key_override is not None
            else {}
        )
        ignore_llm_source = provider_key_override is not None
    elif env["api_key"]:
        pass  # env overrides handled by load_credentials
    elif provider_key_override:
        # 会话级显式 provider 覆盖优先于全局合作方源 llm_source。
        # 否则 Web 端在合作方(如 QwenPaw)失效后切到普通 provider 发送时,
        # get_llm_source() 仍返回 "qwenpaw",会把本次会话选定的 provider/模型/base_url
        # 强行改回(失效的)合作方端点,导致换 provider 后仍报同样的错。
        ignore_llm_source = True
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

    provider_manager_options: dict[str, Any] = {
        "model": model,
        "credentials": credentials,
        "provider_key_override": provider_key_override,
        "base_url_override": base_url_override,
        "request_policy_override": options.request_policy_override,
        "effort_override": options.effort_override,
        "ignore_llm_source": ignore_llm_source,
    }
    if options.provider_config_override is not None:
        provider_manager_options["provider_config_override"] = options.provider_config_override
    provider_manager = ProviderManager(**provider_manager_options)

    def aliyun_default_region_provider() -> str:
        from iac_code.services.providers.aliyun import DEFAULT_REGION

        credential = CloudCredentials().get_provider("aliyun")
        return credential.region_id if credential is not None and credential.region_id else DEFAULT_REGION

    def aliyun_credential_provider() -> Any:
        credential = CloudCredentials().get_provider("aliyun")
        if credential is not None and credential.mode == "OAuth":
            from iac_code.services.providers.aliyun import AliyunCredentials

            credential = AliyunCredentials.refresh_oauth_if_needed(credential)
        return credential

    aliyun_services.credential_provider = aliyun_credential_provider
    aliyun_services.default_region_provider = aliyun_default_region_provider

    tool_registry = ToolRegistry()
    tool_registry.register_default_tools()
    register_cloud_tools(tool_registry, CloudCredentials(), aliyun_services)

    def refresh_cloud_tools() -> None:
        register_cloud_tools(tool_registry, CloudCredentials(), aliyun_services)
        if options.a2a_safe_mode:
            _filter_tool_registry_for_a2a_safe_mode(tool_registry)

    session_storage = SessionStorage()
    session_dir = _prepare_session_dir_for_artifacts(session_storage, cwd, session_id)

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

    def runtime_provider_display() -> str:
        try:
            return provider_manager.get_provider_display()
        except Exception:
            return ""

    def runtime_model() -> str:
        try:
            return provider_manager.get_model_name()
        except Exception:
            return model

    def build_base_system_prompt() -> str:
        return build_system_prompt(
            cwd=cwd,
            memory_context=memory_runtime.build_memory_context(),
            current_time=runtime_current_time,
            provider_display=runtime_provider_display(),
            model=runtime_model(),
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
    if options.a2a_safe_mode:
        _filter_tool_registry_for_a2a_safe_mode(tool_registry)

    mcp_manager = None
    mcp_config_warnings: list[Any] = []
    mcp_pending_configs: list[Any] = []
    runtime_mcp_change_listeners: list[Any] = []
    mcp_auth_tasks: set[asyncio.Task[Any]] = set()
    mcp_auth_flows: set[Any] = set()
    mcp_workspace_root: Path | None = None
    mcp_server_instructions_holder = {"value": ""}
    mcp_load_result = None
    setup_complete = False
    try:
        if not options.a2a_safe_mode and not options.disable_external_services:
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
            mcp_pending_configs = list(mcp_load_result.pending)
        else:
            mcp_load_result = None

        if mcp_load_result is not None and mcp_load_result.servers and mcp_workspace_root is not None:
            if options.mcp_manager_factory is not None:
                mcp_manager = options.mcp_manager_factory(mcp_load_result.servers, [mcp_workspace_root])
            else:
                mcp_manager = MCPManager(mcp_load_result.servers, roots=[mcp_workspace_root], session_id=session_id)
            _register_mcp_elicitation_handler(mcp_manager, options.mcp_elicitation_handler)
            _run_async_blocking(mcp_manager.connect_all())
            mcp_server_instructions_holder["value"] = _mcp_server_instructions_text(mcp_manager)
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
                session_dir=session_dir,
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
                if capability in {"tools", "resources", "auth", "connection"}:
                    registered_mcp_tool_names = _sync_mcp_tool_registry(
                        tool_registry,
                        mcp_manager,
                        session_id,
                        registered_mcp_tool_names,
                        session_dir=session_dir,
                    )
                if capability in {"prompts", "resources", "auth", "connection"}:
                    registered_mcp_command_names, warnings = await _sync_mcp_command_registry(
                        command_registry,
                        mcp_manager,
                        registered_mcp_command_names,
                    )
                    mcp_config_warnings.extend(warnings)
                _append_new_mcp_connection_warnings(mcp_config_warnings, mcp_manager)
                mcp_server_instructions_holder["value"] = _mcp_server_instructions_text(mcp_manager)
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
        permission_context.trusted_read_directories.extend(
            build_session_trusted_read_directories(session_id, session_dir=session_dir)
        )
        if options.a2a_safe_mode:
            permission_context.strict_read_directories = _a2a_safe_read_directories(
                cwd,
                session_dir=session_dir,
                current_session_dir=_current_session_dir(session_storage, cwd, session_id),
            )
            permission_context.read_path_violation_behavior = "deny"

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
                mcp_server_instructions=mcp_server_instructions_holder["value"],
                current_time=runtime_current_time,
                provider_display=runtime_provider_display(),
                model=runtime_model(),
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
            background_task_starter=lambda: _start_mcp_background_tasks(mcp_manager),
            result_storage_dir=_result_storage_dir_for_session(session_dir),
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
            aliyun_services=aliyun_services,
            _cloud_tools_refresher=refresh_cloud_tools,
            mcp_manager=mcp_manager,
            mcp_config_warnings=mcp_config_warnings,
            mcp_pending_configs=mcp_pending_configs,
            _mcp_change_listeners=runtime_mcp_change_listeners,
            _mcp_auth_tasks=mcp_auth_tasks,
            _mcp_auth_flows=mcp_auth_flows,
        )
        setup_complete = True
        return runtime
    finally:
        if not setup_complete:
            _cleanup_mcp_runtime_setup(mcp_manager, mcp_auth_tasks, mcp_auth_flows)


def _filter_tool_registry_for_a2a_safe_mode(tool_registry: Any) -> None:
    for tool in list(tool_registry.list_tools()):
        if tool.name not in _A2A_SAFE_NORMAL_TOOL_NAMES:
            tool_registry.unregister(tool.name)


def _a2a_safe_read_directories(
    cwd: str,
    *,
    session_dir: Path | None,
    current_session_dir: Path | None = None,
) -> list[str]:
    from iac_code.tools.path_safety import get_iac_code_application_root

    roots = [cwd]
    if session_dir is not None:
        roots.append(str(session_dir))
    if current_session_dir is not None:
        roots.append(str(current_session_dir))
    roots.append(str(get_iac_code_application_root()))

    unique_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if root and root not in seen:
            unique_roots.append(root)
            seen.add(root)
    return unique_roots


def _current_session_dir(session_storage: Any, cwd: str, session_id: str) -> Path | None:
    session_dir_factory = getattr(session_storage, "session_dir", None)
    if not callable(session_dir_factory):
        return None
    try:
        raw_session_dir = session_dir_factory(cwd, session_id)
    except (AttributeError, TypeError):
        return None
    if isinstance(raw_session_dir, Path):
        return raw_session_dir
    if isinstance(raw_session_dir, str):
        return Path(raw_session_dir)
    return None


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


def _prepare_session_dir_for_artifacts(session_storage: Any, cwd: str, session_id: str) -> Path | None:
    ensure_v2_session = getattr(session_storage, "ensure_v2_session_dir_for_new_session", None)
    if callable(ensure_v2_session):
        try:
            ensure_v2_session(cwd, session_id)
        except (AttributeError, TypeError):
            pass
    return _session_dir_for_artifacts(session_storage, cwd, session_id)


def _session_dir_for_artifacts(session_storage: Any, cwd: str, session_id: str) -> Path | None:
    v2_session_dir_factory = getattr(session_storage, "v2_session_dir", None)
    used_v2_session_dir = callable(v2_session_dir_factory)
    if not used_v2_session_dir:
        v2_session_dir_factory = getattr(session_storage, "session_dir", None)
        if not callable(v2_session_dir_factory):
            return None
    try:
        raw_session_dir = v2_session_dir_factory(cwd, session_id)
    except (AttributeError, TypeError):
        return None
    if raw_session_dir is None:
        return None
    needs_marker_check = not used_v2_session_dir
    if isinstance(raw_session_dir, Path):
        session_dir = raw_session_dir
    elif isinstance(raw_session_dir, str):
        session_dir = Path(raw_session_dir)
    else:
        if not used_v2_session_dir:
            return None
        session_dir_factory = getattr(session_storage, "session_dir", None)
        if not callable(session_dir_factory):
            return None
        try:
            fallback_session_dir = session_dir_factory(cwd, session_id)
        except (AttributeError, TypeError):
            return None
        if isinstance(fallback_session_dir, Path):
            session_dir = fallback_session_dir
        elif isinstance(fallback_session_dir, str):
            session_dir = Path(fallback_session_dir)
        else:
            return None
        needs_marker_check = True
    if needs_marker_check:
        from iac_code.services.session_layout import SESSION_LAYOUT_VERSION_V2, require_supported_session_layout

        if require_supported_session_layout(session_dir) != SESSION_LAYOUT_VERSION_V2:
            return None
    return session_dir


def _result_storage_dir_for_session(session_dir: Path | None) -> Path | None:
    if session_dir is None:
        return None
    from iac_code.services.session_layout import SessionPaths, session_layout_version

    if session_layout_version(session_dir) is None:
        return None
    return SessionPaths.require_supported(session_dir).tool_results_dir


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


def _mcp_server_instructions_text(mcp_manager: Any) -> str:
    method = getattr(mcp_manager, "server_instructions_text", None)
    if callable(method):
        try:
            return str(method() or "")
        except Exception:
            return ""
    list_connections = getattr(mcp_manager, "list_connections", None)
    if not callable(list_connections):
        return ""
    try:
        records = list(list_connections())
    except Exception:
        return ""
    from iac_code.mcp.manager import format_mcp_server_instructions

    return format_mcp_server_instructions(records)


def _register_mcp_elicitation_handler(mcp_manager: Any, handler: Any) -> None:
    set_handler = getattr(mcp_manager, "set_elicitation_handler", None)
    if callable(set_handler):
        set_handler(handler)


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


def _start_mcp_background_tasks(mcp_manager: Any) -> None:
    if mcp_manager is None:
        return
    start_reconnect_tasks = getattr(mcp_manager, "start_reconnect_tasks", None)
    if callable(start_reconnect_tasks):
        start_reconnect_tasks()


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
    *,
    session_dir: Path | str | None = None,
) -> set[str]:
    from iac_code.mcp.tools import ListMCPResourcesTool, MCPTool, ReadMCPResourceTool

    records = {record.public_name: record for record in mcp_manager.list_tools()}
    for record in records.values():
        tool_registry.register(
            MCPTool(manager=mcp_manager, record=record, session_id=session_id, session_dir=session_dir)
        )
    desired = set(records)
    if mcp_manager.list_resources():
        if tool_registry.get("list_mcp_resources") is None:
            tool_registry.register(ListMCPResourcesTool(manager=mcp_manager))
        tool_registry.register(ReadMCPResourceTool(manager=mcp_manager, session_id=session_id, session_dir=session_dir))
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

    for name in registered_names | _registered_mcp_command_names(command_registry):
        _unregister_mcp_command(command_registry, name)
    warnings = register_mcp_prompt_commands(command_registry, mcp_manager)
    warnings.extend(await register_mcp_skill_commands(command_registry, mcp_manager))
    current_names = _current_mcp_command_names(mcp_manager)
    return {name for name in current_names if _is_registered_mcp_command(command_registry.get(name))}, warnings


def _registered_mcp_command_names(command_registry: Any) -> set[str]:
    get_all = getattr(command_registry, "get_all", None)
    if not callable(get_all):
        return set()
    return {str(command.name) for command in get_all() if _is_registered_mcp_command(command)}


def _unregister_mcp_command(command_registry: Any, name: str) -> None:
    command = command_registry.get(name)
    if _is_registered_mcp_command(command):
        command_registry.unregister(command.name)


def _is_registered_mcp_command(command: Any) -> bool:
    from iac_code.commands.registry import PromptCommand

    if not isinstance(command, PromptCommand):
        return False
    skill = getattr(command, "skill", None)
    file_path = str(getattr(skill, "file_path", "") or "")
    return file_path.startswith("mcp://")


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
        from iac_code.mcp.oauth import oauth_scope_identity, safe_oauth_resource_metadata_url, start_oauth_loopback_flow
        from iac_code.mcp.storage import MCPSecretStorage

        scoped = scoped_configs_by_name[server_name]
        required_scopes = getattr(mcp_manager, "required_auth_scopes", lambda name: [])(server_name)
        resource_metadata_url = getattr(mcp_manager, "required_auth_resource_metadata_url", lambda name: None)(
            server_name
        )
        resource_metadata_url = safe_oauth_resource_metadata_url(
            resource_metadata_url if isinstance(resource_metadata_url, str) else None
        )
        flow = await asyncio.to_thread(
            start_oauth_loopback_flow,
            scoped.config,
            storage=MCPSecretStorage(),
            scope=oauth_scope_identity(
                scoped.scope,
                source_path=getattr(scoped, "source_path", None),
                session_id=session_id,
            ),
            required_scopes=required_scopes or None,
            resource_metadata_url=resource_metadata_url,
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
