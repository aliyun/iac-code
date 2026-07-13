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
    assert command.skill is not None
    assert command.skill.name == "mcp__ros__vpc"
    assert command.skill.description == "VPC guidance"
    assert command.skill.file_path == "mcp://ros/skill://ros/vpc"
    assert command.skill.skill_root == ""
    assert command.skill.frontmatter.allowed_tools == []
    assert command.skill.frontmatter.auto_trigger == {}
    assert "```!bash" in command.skill.content
    assert registry.get("ros:vpc") is command
    assert command.aliases == ["ros:vpc"]


@pytest.mark.asyncio
async def test_register_mcp_skill_skips_conflicting_local_command() -> None:
    registry = CommandRegistry()
    registry.register(LocalCommand(name="mcp__ros__vpc", description="built in"))

    warnings = await register_mcp_skill_commands(registry, FakeSkillManager())

    existing = registry.get("mcp__ros__vpc")
    assert existing is not None
    assert existing.description == "built in"
    assert len(warnings) == 1
    assert warnings[0].code == "command_conflict"


@pytest.mark.asyncio
async def test_register_mcp_skill_skips_conflicting_local_prompt_command() -> None:
    registry = CommandRegistry()
    registry.register(_local_prompt_command("mcp__ros__vpc", description="local skill"))

    warnings = await register_mcp_skill_commands(registry, FakeSkillManager())

    existing = registry.get("mcp__ros__vpc")
    assert existing is not None
    assert existing.description == "local skill"
    assert len(warnings) == 1
    assert warnings[0].code == "command_conflict"


@pytest.mark.asyncio
async def test_register_mcp_skill_keeps_canonical_name_and_adds_compatibility_alias() -> None:
    registry = CommandRegistry()
    manager = FakeSkillManager(server_name="yuque", resource_name="search")

    warnings = await register_mcp_skill_commands(registry, manager)

    assert warnings == []
    canonical = registry.get("mcp__yuque__search")
    alias = registry.get("yuque:search")
    assert canonical is not None
    assert alias is canonical
    assert isinstance(canonical, PromptCommand)
    assert canonical.name == "mcp__yuque__search"
    assert canonical.aliases == ["yuque:search"]
    assert canonical.skill is not None
    assert canonical.skill.name == "mcp__yuque__search"


@pytest.mark.asyncio
async def test_register_mcp_skill_reports_alias_conflict_without_overwriting_existing_command() -> None:
    registry = CommandRegistry()
    registry.register(LocalCommand(name="yuque:search", description="existing alias target"))
    manager = FakeSkillManager(server_name="yuque", resource_name="search")

    warnings = await register_mcp_skill_commands(registry, manager)

    canonical = registry.get("mcp__yuque__search")
    assert isinstance(canonical, PromptCommand)
    alias_target = registry.get("yuque:search")
    assert alias_target is not None
    assert alias_target.description == "existing alias target"
    assert len(warnings) == 1
    assert warnings[0].code == "alias_conflict"
    assert "yuque:search" in warnings[0].message


@pytest.mark.asyncio
async def test_register_mcp_skill_warns_and_skips_unreadable_resource() -> None:
    registry = CommandRegistry()

    warnings = await register_mcp_skill_commands(registry, FakeSkillManager(read_error=RuntimeError("read failed")))

    assert registry.get("mcp__ros__vpc") is None
    assert len(warnings) == 1
    assert warnings[0].code == "skill_read_failed"


@pytest.mark.asyncio
async def test_register_mcp_skill_warns_and_skips_malformed_resource_without_blocking_valid_skill() -> None:
    registry = CommandRegistry()
    manager = MixedSkillManager()

    warnings = await register_mcp_skill_commands(registry, manager)

    assert registry.get("mcp__ros__bad") is None
    good = registry.get("mcp__ros__good")
    assert isinstance(good, PromptCommand)
    assert good.skill is not None
    assert good.skill.description == "Good guidance"
    assert len(warnings) == 1
    assert warnings[0].code == "skill_read_failed"
    assert warnings[0].server_name == "ros"
    assert "mcp__ros__bad" in warnings[0].message


@pytest.mark.asyncio
async def test_register_mcp_skill_limits_remote_description_and_body() -> None:
    registry = CommandRegistry()
    manager = FakeSkillManager(text=("---\ndescription: {}\n---\n{}").format("d" * 600, "body\n" * 5000))

    warnings = await register_mcp_skill_commands(registry, manager)

    command = registry.get("mcp__ros__vpc")
    assert isinstance(command, PromptCommand)
    assert command.skill is not None
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
    def __init__(
        self,
        text: str | None = None,
        read_error: Exception | None = None,
        *,
        server_name: str = "ros",
        resource_name: str = "vpc",
    ) -> None:
        self.text = text
        self.read_error = read_error
        self.server_name = server_name
        self.resource_name = resource_name

    def list_resources(self) -> list[MCPResourceRecord]:
        return [
            MCPResourceRecord(
                server_name=self.server_name,
                uri="skill://{}/{}".format(self.server_name, self.resource_name),
                name=self.resource_name,
                mime_type="text/markdown",
            )
        ]

    async def read_resource(self, uri: str, server_name: str | None = None):
        if self.read_error is not None:
            raise self.read_error
        return (
            server_name or self.server_name,
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


class MixedSkillManager:
    def list_resources(self) -> list[MCPResourceRecord]:
        return [
            MCPResourceRecord(server_name="ros", uri="skill://ros/bad", name="bad", mime_type="text/markdown"),
            MCPResourceRecord(server_name="ros", uri="skill://ros/good", name="good", mime_type="text/markdown"),
        ]

    async def read_resource(self, uri: str, server_name: str | None = None):
        if uri.endswith("/bad"):
            text = "---\ndescription: Bad guidance\narguments: 1\n---\n# Bad\n"
        else:
            text = "---\ndescription: Good guidance\n---\n# Good\n"
        return (
            server_name or "ros",
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": text,
                    }
                ]
            },
        )
