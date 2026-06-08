from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
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

    skill_listing = build_skill_listing(command_registry.get_model_invocable_skills())

    def build_agent_system_prompt() -> str:
        return build_system_prompt(
            cwd=cwd,
            memory_context=memory_runtime.build_memory_context(),
            skill_listing=skill_listing,
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

    return AgentRuntime(
        agent_loop=agent_loop,
        session_id=session_id,
        tool_registry=tool_registry,
        provider_manager=provider_manager,
        command_registry=command_registry,
        task_manager=task_manager,
        memory_manager=memory_manager,
        legacy_memory_manager=legacy_memory_manager,
    )
