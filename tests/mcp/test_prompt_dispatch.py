from __future__ import annotations

from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.mcp.prompt_dispatch import is_mcp_prompt_command, lookup_mcp_prompt_command
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


def _command(name: str, file_path: str) -> PromptCommand:
    return PromptCommand(
        name=name,
        description=name,
        skill=SkillDefinition(
            name=name,
            description=name,
            frontmatter=SkillFrontmatter(description=name),
            content="",
            source=SkillSource.PROJECT,
            file_path=file_path,
            content_length=0,
        ),
        source=SkillSource.PROJECT,
    )


def test_mcp_prompt_command_file_path_must_be_direct_prompt_path() -> None:
    prompt_command = _command("mcp__ros__review", "mcp://ros/prompt/review")
    resource_skill = _command("mcp__ros__skill_prompt", "mcp://ros/skill://ros/prompt/foo")

    assert is_mcp_prompt_command(prompt_command) is True
    assert is_mcp_prompt_command(resource_skill) is False


def test_lookup_mcp_prompt_command_ignores_resource_skill_with_prompt_segment() -> None:
    registry = CommandRegistry()
    registry.register(_command("mcp__ros__skill_prompt", "mcp://ros/skill://ros/prompt/foo"))

    assert lookup_mcp_prompt_command(registry, "/mcp__ros__skill_prompt topic=vpc") is None
