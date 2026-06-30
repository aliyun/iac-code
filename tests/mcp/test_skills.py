from __future__ import annotations

import pytest

from iac_code.commands.registry import CommandRegistry, LocalCommand, PromptCommand
from iac_code.mcp.skills import register_mcp_skill_commands
from iac_code.mcp.types import MCPResourceRecord
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


@pytest.mark.asyncio
async def test_register_mcp_skill_reads_skill_resource_without_local_expansion() -> None:
    registry = CommandRegistry()
    manager = FakeSkillManager()

    warnings = await register_mcp_skill_commands(registry, manager)

    assert warnings == []
    command = registry.get("mcp__ros__vpc")
    assert isinstance(command, PromptCommand)
    assert command.skill.name == "mcp__ros__vpc"
    assert command.skill.description == "VPC guidance"
    assert command.skill.file_path == "mcp://ros/skill://ros/vpc"
    assert command.skill.skill_root == ""
    assert command.skill.frontmatter.allowed_tools == []
    assert command.skill.frontmatter.auto_trigger == {}
    assert "```!bash" in command.skill.content


@pytest.mark.asyncio
async def test_register_mcp_skill_skips_conflicting_local_command() -> None:
    registry = CommandRegistry()
    registry.register(LocalCommand(name="mcp__ros__vpc", description="built in"))

    warnings = await register_mcp_skill_commands(registry, FakeSkillManager())

    assert registry.get("mcp__ros__vpc").description == "built in"
    assert len(warnings) == 1
    assert warnings[0].code == "command_conflict"


@pytest.mark.asyncio
async def test_register_mcp_skill_skips_conflicting_local_prompt_command() -> None:
    registry = CommandRegistry()
    registry.register(_local_prompt_command("mcp__ros__vpc", description="local skill"))

    warnings = await register_mcp_skill_commands(registry, FakeSkillManager())

    assert registry.get("mcp__ros__vpc").description == "local skill"
    assert len(warnings) == 1
    assert warnings[0].code == "command_conflict"


@pytest.mark.asyncio
async def test_register_mcp_skill_warns_and_skips_unreadable_resource() -> None:
    registry = CommandRegistry()

    warnings = await register_mcp_skill_commands(registry, FakeSkillManager(read_error=RuntimeError("read failed")))

    assert registry.get("mcp__ros__vpc") is None
    assert len(warnings) == 1
    assert warnings[0].code == "skill_read_failed"


@pytest.mark.asyncio
async def test_register_mcp_skill_limits_remote_description_and_body() -> None:
    registry = CommandRegistry()
    manager = FakeSkillManager(text=("---\ndescription: {}\n---\n{}").format("d" * 600, "body\n" * 5000))

    warnings = await register_mcp_skill_commands(registry, manager)

    command = registry.get("mcp__ros__vpc")
    assert isinstance(command, PromptCommand)
    assert len(command.skill.description) <= 256
    assert command.skill.content_length <= 20000
    assert len(command.skill.content) <= 20000
    assert any(warning.code == "skill_truncated" for warning in warnings)


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


class FakeSkillManager:
    def __init__(self, text: str | None = None, read_error: Exception | None = None) -> None:
        self.text = text
        self.read_error = read_error

    def list_resources(self) -> list[MCPResourceRecord]:
        return [
            MCPResourceRecord(
                server_name="ros",
                uri="skill://ros/vpc",
                name="vpc",
                mime_type="text/markdown",
            )
        ]

    async def read_resource(self, uri: str, server_name: str | None = None):
        if self.read_error is not None:
            raise self.read_error
        return (
            server_name or "ros",
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": self.text
                        or (
                            "---\n"
                            "description: VPC guidance\n"
                            "allowed_tools:\n"
                            "  - bash(*)\n"
                            "auto_trigger:\n"
                            "  script: run.py\n"
                            "---\n"
                            "# VPC\n"
                            "```!bash\n"
                            "echo should not be granted automatically\n"
                            "```"
                        ),
                    }
                ]
            },
        )
