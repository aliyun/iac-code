"""Tests for skill processor."""

import pytest

from iac_code.commands.registry import PromptCommand
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.processor import process_prompt_command
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


def _make_command(
    name: str = "test-skill",
    description: str = "Test",
    content: str = "Skill body",
    allowed_tools: list[str] | None = None,
    model: str = "inherit",
    effort: str = "",
    context: str = "inline",
) -> PromptCommand:
    fm = SkillFrontmatter(
        description=description,
        allowed_tools=allowed_tools or [],
        model=model,
        effort=effort,
        context=context,
    )
    skill = SkillDefinition(
        name=name,
        description=description,
        frontmatter=fm,
        content=content,
        source=SkillSource.BUNDLED,
    )
    return PromptCommand(name=name, description=description, skill=skill)


class TestProcessPromptCommand:
    @pytest.mark.asyncio
    async def test_basic_processing(self):
        cmd = _make_command(content="Hello world")
        result = await process_prompt_command(cmd, "")
        assert result.skill_name == "test-skill"
        assert result.prompt_content == "Hello world"
        assert len(result.new_messages) == 1
        assert result.new_messages[0]["role"] == "user"
        assert "<skill-name>test-skill</skill-name>" in result.new_messages[0]["content"]
        assert "Hello world" in result.new_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_with_arguments(self):
        cmd = _make_command(content="Process $ARGUMENTS")
        result = await process_prompt_command(cmd, "my-arg")
        assert "Process my-arg" in result.prompt_content

    @pytest.mark.asyncio
    async def test_no_context_modifier_when_no_overrides(self):
        cmd = _make_command()
        result = await process_prompt_command(cmd, "")
        assert result.context_modifier is None

    @pytest.mark.asyncio
    async def test_context_modifier_with_allowed_tools(self):
        cmd = _make_command(allowed_tools=["bash(*)"])
        result = await process_prompt_command(cmd, "")
        assert result.context_modifier is not None

        modified = result.context_modifier({"allowed_tool_rules": []})
        assert "bash(*)" in modified["allowed_tool_rules"]

    @pytest.mark.asyncio
    async def test_context_modifier_with_model_override(self):
        cmd = _make_command(model="claude-3-opus")
        result = await process_prompt_command(cmd, "")
        assert result.context_modifier is not None

        modified = result.context_modifier({})
        assert modified["model_override"] == "claude-3-opus"

    @pytest.mark.asyncio
    async def test_context_modifier_with_effort(self):
        cmd = _make_command(effort="high")
        result = await process_prompt_command(cmd, "")
        assert result.context_modifier is not None

        modified = result.context_modifier({})
        assert modified["effort_override"] == "high"

    @pytest.mark.asyncio
    async def test_is_fork_flag(self):
        cmd = _make_command(context="fork")
        result = await process_prompt_command(cmd, "")
        assert result.is_fork is True

    @pytest.mark.asyncio
    async def test_is_not_fork_by_default(self):
        cmd = _make_command()
        result = await process_prompt_command(cmd, "")
        assert result.is_fork is False

    @pytest.mark.asyncio
    async def test_raises_for_non_skill_command(self):
        cmd = PromptCommand(name="broken", description="", skill=None)
        with pytest.raises(ValueError, match="not a skill"):
            await process_prompt_command(cmd, "")
