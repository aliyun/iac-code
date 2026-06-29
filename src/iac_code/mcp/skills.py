from __future__ import annotations

import asyncio
import re
from typing import Any

from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.i18n import _
from iac_code.mcp.types import MCPConfigWarning
from iac_code.skills.frontmatter import parse_frontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource
from iac_code.utils.public_errors import sanitize_public_text

_MAX_SKILL_DESCRIPTION_CHARS = 256
_MAX_SKILL_BODY_CHARS = 20_000


async def register_mcp_skill_commands(command_registry: CommandRegistry, manager: Any) -> list[MCPConfigWarning]:
    warnings: list[MCPConfigWarning] = []
    for resource in manager.list_resources():
        if not resource.is_skill_resource:
            continue
        command_name = resource.public_name or _resource_command_name(resource)
        existing = command_registry.get(command_name)
        if existing is not None and not _is_mcp_prompt_command(existing):
            warnings.append(
                MCPConfigWarning(
                    source="mcp",
                    server_name=resource.server_name,
                    code="command_conflict",
                    message=_("MCP skill command {command!r} conflicts with an existing command.").format(
                        command=command_name
                    ),
                )
            )
            continue

        try:
            server_name, result = await asyncio.wait_for(
                manager.read_resource(resource.uri, server_name=resource.server_name),
                timeout=getattr(manager, "operation_timeout_seconds", 20.0),
            )
        except Exception as exc:
            warnings.append(
                MCPConfigWarning(
                    source="mcp",
                    server_name=resource.server_name,
                    code="skill_read_failed",
                    message=_("MCP skill command {command!r} could not be loaded: {error}").format(
                        command=command_name,
                        error=sanitize_public_text(str(exc) or exc.__class__.__name__),
                    ),
                )
            )
            continue
        text = _first_text_content(result)
        frontmatter, content = parse_frontmatter(text)
        frontmatter.allowed_tools = []
        frontmatter.auto_trigger = {}
        frontmatter.paths = []
        description = frontmatter.description or resource.description or resource.title or resource.name or ""
        truncated = False
        if len(description) > _MAX_SKILL_DESCRIPTION_CHARS:
            description = description[:_MAX_SKILL_DESCRIPTION_CHARS]
            frontmatter.description = description
            truncated = True
        if len(content) > _MAX_SKILL_BODY_CHARS:
            content = content[:_MAX_SKILL_BODY_CHARS]
            truncated = True
        if truncated:
            warnings.append(
                MCPConfigWarning(
                    source="mcp",
                    server_name=resource.server_name,
                    code="skill_truncated",
                    message=_("MCP skill {command!r} was truncated to fit safety limits.").format(command=command_name),
                )
            )
        skill = SkillDefinition(
            name=command_name,
            description=description,
            frontmatter=frontmatter,
            content=content,
            source=SkillSource.PROJECT,
            file_path="mcp://{}/{}".format(server_name, resource.uri),
            skill_root="",
            content_length=len(content),
        )
        command_registry.register(
            PromptCommand(
                name=command_name,
                description=description,
                skill=skill,
                source=SkillSource.PROJECT,
            )
        )
    return warnings


def _first_text_content(result: Any) -> str:
    contents = _get_value(result, "contents", [])
    for content in contents:
        text = _get_value(content, "text")
        if text is not None:
            return str(text)
    return ""


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _is_mcp_prompt_command(command: Any) -> bool:
    if not isinstance(command, PromptCommand):
        return False
    skill = command.skill
    return bool(skill is not None and str(skill.file_path).startswith("mcp://"))


def _safe_identifier(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "mcp"


def _resource_command_name(resource: Any) -> str:
    return "mcp__{}__{}".format(
        _safe_identifier(resource.server_name),
        _safe_identifier(resource.name or "skill"),
    )
