from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentFactoryOptions:
    model: str
    session_id: str | None = None
    cwd: str | None = None
    max_turns: int = 100


@dataclass
class AgentRuntime:
    agent_loop: Any
    session_id: str
    tool_registry: Any
    provider_manager: Any
    command_registry: Any
    task_manager: Any
    memory_manager: Any


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
    from iac_code.providers.manager import ProviderManager
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.services.session_storage import SessionStorage
    from iac_code.skills.bundled import init_bundled_skills
    from iac_code.skills.discovery import discover_all_skills, skill_to_command
    from iac_code.skills.listing import build_skill_listing
    from iac_code.skills.skill_tool import SkillTool
    from iac_code.tasks.notification_queue import NotificationQueue
    from iac_code.tasks.task_state import TaskManager
    from iac_code.tasks.task_tools import TaskGetTool, TaskListTool, TaskStopTool
    from iac_code.tools.base import ToolRegistry
    from iac_code.tools.cloud.registry import register_cloud_tools

    cwd = options.cwd or os.getcwd()
    session_id = options.session_id or str(uuid.uuid4())[:8]

    credentials = load_credentials(model=options.model)

    provider_manager = ProviderManager(model=options.model, credentials=credentials)

    tool_registry = ToolRegistry()
    tool_registry.register_default_tools()
    register_cloud_tools(tool_registry, CloudCredentials())

    session_storage = SessionStorage()

    memory_manager = MemoryManager(memory_dir=str(get_config_dir() / "memory"))
    memory_content = memory_manager.get_prompt_content()
    tool_registry.register(ReadMemoryTool(memory_manager))
    tool_registry.register(WriteMemoryTool(memory_manager))

    task_manager = TaskManager()
    tool_registry.register(TaskListTool(task_manager))
    tool_registry.register(TaskGetTool(task_manager))
    tool_registry.register(TaskStopTool(task_manager))

    notification_queue = NotificationQueue()
    base_system_prompt = build_system_prompt(cwd=cwd, memory_content=memory_content)
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
    for skill in discover_all_skills(cwd):
        cmd = skill_to_command(skill)
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
        )
    )

    skill_listing = build_skill_listing(command_registry.get_model_invocable_skills())
    agent_loop = AgentLoop(
        provider_manager=provider_manager,
        system_prompt=build_system_prompt(cwd=cwd, memory_content=memory_content, skill_listing=skill_listing),
        tool_registry=tool_registry,
        session_storage=session_storage,
        session_id=session_id,
        max_turns=options.max_turns,
        cwd=cwd,
    )

    return AgentRuntime(
        agent_loop=agent_loop,
        session_id=session_id,
        tool_registry=tool_registry,
        provider_manager=provider_manager,
        command_registry=command_registry,
        task_manager=task_manager,
        memory_manager=memory_manager,
    )
