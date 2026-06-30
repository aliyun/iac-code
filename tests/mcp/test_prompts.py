from __future__ import annotations

import pytest
from mcp import types

from iac_code.commands.registry import CommandRegistry, LocalCommand, PromptCommand
from iac_code.mcp.manager import MCPManager
from iac_code.mcp.prompts import _parse_prompt_args, register_mcp_prompt_commands
from iac_code.mcp.types import MCPConfigScope, MCPPromptRecord, MCPServerConfig, ScopedMCPServerConfig
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillContext, SkillDefinition
from iac_code.types.skill_source import SkillSource


@pytest.mark.asyncio
async def test_register_mcp_prompt_command_validates_arguments_and_calls_manager() -> None:
    registry = CommandRegistry()
    manager = FakePromptManager()

    warnings = register_mcp_prompt_commands(registry, manager)

    assert warnings == []
    command = registry.get("mcp__ros__review")
    assert isinstance(command, PromptCommand)
    prompt = await command.skill.get_prompt('{"template": "vpc.yml"}', SkillContext(cwd="/repo"))

    assert manager.called_with == {"server_name": "ros", "prompt_name": "review", "arguments": {"template": "vpc.yml"}}
    assert "Review vpc.yml" in prompt

    with pytest.raises(ValueError, match="Missing required MCP prompt argument"):
        await command.skill.get_prompt("{}", SkillContext(cwd="/repo"))


def test_register_mcp_prompt_skips_non_prompt_command_conflicts() -> None:
    registry = CommandRegistry()
    registry.register(LocalCommand(name="mcp__ros__review", description="built in"))

    warnings = register_mcp_prompt_commands(registry, FakePromptManager())

    assert registry.get("mcp__ros__review").description == "built in"
    assert len(warnings) == 1
    assert warnings[0].code == "command_conflict"


def test_register_mcp_prompt_skips_local_prompt_command_conflicts() -> None:
    registry = CommandRegistry()
    registry.register(_local_prompt_command("mcp__ros__review", description="local skill"))

    warnings = register_mcp_prompt_commands(registry, FakePromptManager())

    assert registry.get("mcp__ros__review").description == "local skill"
    assert len(warnings) == 1
    assert warnings[0].code == "command_conflict"


def test_parse_prompt_args_preserves_windows_paths() -> None:
    args = _parse_prompt_args(r"path=C:\Users\alice\file.txt script=C:\Program Files\node\server.js")

    assert args == {
        "path": r"C:\Users\alice\file.txt",
        "script": r"C:\Program Files\node\server.js",
    }


def test_parse_prompt_args_preserves_quoted_backslashes_and_spaces() -> None:
    args = _parse_prompt_args(r'path="C:\Program Files\node\server.js" region=cn-hangzhou')

    assert args["path"] == r"C:\Program Files\node\server.js"
    assert args["region"] == "cn-hangzhou"


@pytest.mark.asyncio
async def test_register_mcp_prompt_command_supports_sdk_argument_lists() -> None:
    registry = CommandRegistry()
    manager = FakePromptManager(
        arguments=[
            types.PromptArgument(name="template", description="Template path", required=True),
            types.PromptArgument(name="region", description="Region", required=False),
        ]
    )

    warnings = register_mcp_prompt_commands(registry, manager)

    assert warnings == []
    command = registry.get("mcp__ros__review")
    assert isinstance(command, PromptCommand)
    assert command.skill.frontmatter.argument_hint == "template=<value>"
    assert command.skill.frontmatter.arguments == ["template", "region"]

    with pytest.raises(ValueError, match="Missing required MCP prompt argument"):
        await command.skill.get_prompt("region=cn-hangzhou", SkillContext(cwd="/repo"))


@pytest.mark.asyncio
async def test_manager_generated_prompt_name_matches_mcp_tool_naming() -> None:
    scoped = ScopedMCPServerConfig(
        config=MCPServerConfig.from_mapping("ros", {"command": "uvx"}),
        scope=MCPConfigScope.SESSION,
    )
    manager = MCPManager(
        [scoped],
        client_factory=lambda config: FakePromptClient(),
    )

    await manager.connect_all()

    assert [prompt.public_name for prompt in manager.list_prompts()] == ["mcp__ros__review"]


def _local_prompt_command(name: str, *, description: str) -> PromptCommand:
    return PromptCommand(
        name=name,
        description=description,
        skill=SkillDefinition(
            name=name,
            description=description,
            frontmatter=SkillFrontmatter(description=description),
            content="local content",
            source=SkillSource.PROJECT,
            file_path="/repo/.iac-code/skills/local/SKILL.md",
            content_length=13,
        ),
        source=SkillSource.PROJECT,
    )


class FakePromptClient:
    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_tools(self) -> list[dict]:
        return []

    async def call_tool(self, name: str, arguments=None, **kwargs):
        return {}

    async def list_resources(self) -> list[dict]:
        return []

    async def read_resource(self, uri: str):
        return {"contents": []}

    async def list_prompts(self) -> list[dict]:
        return [{"name": "review", "arguments": []}]

    async def get_prompt(self, name: str, arguments=None):
        return {"messages": []}


class FakePromptManager:
    def __init__(self, *, arguments=None) -> None:
        self.called_with = {}
        self.arguments = arguments if arguments is not None else {"template": {"required": True}}

    def list_prompts(self) -> list[MCPPromptRecord]:
        return [
            MCPPromptRecord(
                server_name="ros",
                prompt_name="review",
                public_name="mcp__ros__review",
                description="Review template",
                arguments=self.arguments,
            )
        ]

    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict[str, str]):
        self.called_with = {
            "server_name": server_name,
            "prompt_name": prompt_name,
            "arguments": arguments,
        }
        return {"messages": [{"role": "user", "content": {"type": "text", "text": "Review " + arguments["template"]}}]}
