from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.i18n import _
from iac_code.mcp.types import MCPConfigWarning, MCPPromptRecord
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillContext, SkillDefinition
from iac_code.types.skill_source import SkillSource


def register_mcp_prompt_commands(command_registry: CommandRegistry, manager: Any) -> list[MCPConfigWarning]:
    warnings: list[MCPConfigWarning] = []
    for record in manager.list_prompts():
        existing = command_registry.get(record.public_name)
        if existing is not None and not _is_mcp_prompt_command(existing):
            warnings.append(
                MCPConfigWarning(
                    source="mcp",
                    server_name=record.server_name,
                    code="command_conflict",
                    message=_("MCP prompt command {command!r} conflicts with an existing command.").format(
                        command=record.public_name
                    ),
                )
            )
            continue

        frontmatter = SkillFrontmatter(
            description=record.description or "",
            when_to_use=record.description or "",
            argument_hint=_argument_hint(record),
            arguments=_argument_names(record),
        )
        skill = SkillDefinition(
            name=record.public_name,
            description=record.description or "",
            frontmatter=frontmatter,
            content="",
            source=SkillSource.PROJECT,
            file_path="mcp://{}/prompt/{}".format(record.server_name, record.prompt_name),
            content_length=0,
            _prompt_provider=_MCPPromptProvider(manager=manager, record=record),
        )
        command_registry.register(
            PromptCommand(
                name=record.public_name,
                description=record.description or _("MCP prompt {prompt}").format(prompt=record.prompt_name),
                skill=skill,
                source=SkillSource.PROJECT,
            )
        )
    return warnings


@dataclass
class _MCPPromptProvider:
    manager: Any
    record: MCPPromptRecord

    async def get_prompt(self, args: str, context: SkillContext) -> str:
        arguments = _parse_prompt_args(args)
        for name in _required_arguments(self.record):
            if name not in arguments or arguments[name] == "":
                raise ValueError(_("Missing required MCP prompt argument: {name}").format(name=name))
        result = await self.manager.get_prompt(self.record.server_name, self.record.prompt_name, arguments)
        return _render_prompt_result(result)


def _required_arguments(record: MCPPromptRecord) -> list[str]:
    if isinstance(record.arguments, dict):
        return [
            name
            for name, schema in record.arguments.items()
            if isinstance(schema, dict) and schema.get("required") is True
        ]
    if isinstance(record.arguments, list):
        return [
            str(_get_value(argument, "name", ""))
            for argument in record.arguments
            if _get_value(argument, "name") and _get_value(argument, "required") is True
        ]
    return []


def _argument_names(record: MCPPromptRecord) -> list[str]:
    if isinstance(record.arguments, dict):
        return [str(key) for key in record.arguments]
    if isinstance(record.arguments, list):
        return [str(_get_value(argument, "name", "")) for argument in record.arguments if _get_value(argument, "name")]
    return []


def _argument_hint(record: MCPPromptRecord) -> str:
    required = _required_arguments(record)
    if required:
        return " ".join("{}=<value>".format(name) for name in required)
    return ""


def _parse_prompt_args(args: str) -> dict[str, str]:
    stripped = args.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if not isinstance(data, dict):
            raise ValueError(_("MCP prompt arguments JSON must be an object."))
        return {str(key): str(value) for key, value in data.items()}

    parsed: dict[str, str] = {}
    current_key: str | None = None
    for part in _split_prompt_arg_tokens(stripped):
        if _starts_key_value(part):
            key, value = part.split("=", 1)
            if not key:
                raise ValueError(_("MCP prompt arguments must use key=value syntax."))
            parsed[key] = value
            current_key = key
            continue
        if current_key is None:
            raise ValueError(_("MCP prompt arguments must use key=value syntax."))
        parsed[current_key] = "{} {}".format(parsed[current_key], part)
    return parsed


def _split_prompt_arg_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(value):
        char = value[i]
        if quote is not None:
            if char == "\\" and i + 1 < len(value) and value[i + 1] == quote:
                current.append(quote)
                i += 2
                continue
            if char == quote:
                quote = None
                i += 1
                continue
            current.append(char)
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(char)
        i += 1
    if quote is not None:
        raise ValueError(_("MCP prompt arguments contain an unterminated quoted value."))
    if current:
        tokens.append("".join(current))
    return tokens


def _starts_key_value(value: str) -> bool:
    if "=" not in value:
        return False
    key, _value = value.split("=", 1)
    if not key:
        return True
    return all(not char.isspace() for char in key)


def _render_prompt_result(result: Any) -> str:
    messages = _get_value(result, "messages", [])
    parts: list[str] = []
    for message in messages:
        role = _get_value(message, "role", "user")
        content = _get_value(message, "content", "")
        text = _content_text(content)
        if text:
            parts.append("{}: {}".format(role, text))
    return "\n\n".join(parts)


def _is_mcp_prompt_command(command: Any) -> bool:
    if not isinstance(command, PromptCommand):
        return False
    skill = command.skill
    return bool(skill is not None and str(skill.file_path).startswith("mcp://"))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    if isinstance(content, list):
        return "\n".join(_content_text(item) for item in content)
    return str(content)


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
