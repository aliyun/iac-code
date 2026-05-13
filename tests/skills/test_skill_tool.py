"""Tests for SkillTool."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.skills.skill_tool import SkillTool
from iac_code.tools.base import ToolContext
from iac_code.types.skill_source import SkillSource


def _make_registry_with_skill(
    name: str = "test-skill",
    content: str = "Skill body",
    source: SkillSource = SkillSource.BUNDLED,
    context: str = "inline",
    allowed_tools: list[str] | None = None,
) -> CommandRegistry:
    fm = SkillFrontmatter(
        description=f"Skill {name}",
        context=context,
        allowed_tools=allowed_tools or [],
    )
    skill = SkillDefinition(
        name=name,
        description=f"Skill {name}",
        frontmatter=fm,
        content=content,
        source=source,
    )
    cmd = PromptCommand(name=name, description=f"Skill {name}", skill=skill, source=source)
    registry = CommandRegistry()
    registry.register(cmd)
    return registry


class TestSkillTool:
    def test_tool_properties(self):
        registry = CommandRegistry()
        tool = SkillTool(command_registry=registry)
        assert tool.name == "skill"
        assert tool.is_read_only()
        assert tool.is_concurrency_safe({})
        assert tool.user_facing_name() == "Skill"

    def test_input_schema(self):
        registry = CommandRegistry()
        tool = SkillTool(command_registry=registry)
        schema = tool.input_schema
        assert schema["required"] == ["skill"]
        assert "skill" in schema["properties"]
        assert "args" in schema["properties"]

    @pytest.mark.asyncio
    async def test_execute_inline(self):
        registry = _make_registry_with_skill(content="Hello world")
        tool = SkillTool(command_registry=registry)
        ctx = ToolContext()

        result = await tool.execute(tool_input={"skill": "test-skill"}, context=ctx)
        assert not result.is_error
        assert "loaded (inline)" in result.content
        assert len(result.new_messages) == 1
        assert "<skill-name>test-skill</skill-name>" in result.new_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_execute_not_found(self):
        registry = CommandRegistry()
        tool = SkillTool(command_registry=registry)
        ctx = ToolContext()

        result = await tool.execute(tool_input={"skill": "nonexistent"}, context=ctx)
        assert result.is_error
        assert "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_execute_with_args(self):
        registry = _make_registry_with_skill(content="Process $ARGUMENTS")
        tool = SkillTool(command_registry=registry)
        ctx = ToolContext()

        result = await tool.execute(tool_input={"skill": "test-skill", "args": "my-arg"}, context=ctx)
        assert not result.is_error
        assert "Process my-arg" in result.new_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_records_usage(self):
        registry = _make_registry_with_skill()
        tool = SkillTool(command_registry=registry)
        ctx = ToolContext()

        await tool.execute(tool_input={"skill": "test-skill"}, context=ctx)
        assert registry._skill_usage_counts.get("test-skill", 0) == 1

    @pytest.mark.asyncio
    async def test_execute_missing_skill_definition(self):
        registry = CommandRegistry()
        registry.register(
            PromptCommand(name="test-skill", description="missing", skill=None, source=SkillSource.PROJECT)
        )
        tool = SkillTool(command_registry=registry)

        result = await tool.execute(tool_input={"skill": "test-skill"}, context=ToolContext())

        assert result.is_error
        assert "definition missing" in result.content.lower()

    @pytest.mark.asyncio
    async def test_execute_fork_mode(self, monkeypatch):
        registry = _make_registry_with_skill(source=SkillSource.PROJECT, context="fork")
        tool = SkillTool(
            command_registry=registry,
            session_id="sess-1",
            provider_manager=object(),
            tool_registry=object(),
            system_prompt="system prompt",
        )

        monkeypatch.setattr(
            "iac_code.skills.processor.process_prompt_command",
            AsyncMock(return_value=SimpleNamespace(prompt_content="expanded prompt")),
        )
        monkeypatch.setattr(
            "iac_code.agent.agent_tool.run_sub_agent",
            AsyncMock(return_value=("sub-agent result", SimpleNamespace(tool_use_count=3, token_count=1200))),
        )

        result = await tool.execute(
            tool_input={"skill": "test-skill", "args": "abc"}, context=ToolContext(cwd="/tmp/work")
        )

        assert not result.is_error
        assert "sub-agent result" in result.content
        assert "3 tool calls" in result.content
        assert "1200 tokens" in result.content

    @pytest.mark.asyncio
    async def test_execute_inline_failure_returns_error(self, monkeypatch):
        registry = _make_registry_with_skill()
        tool = SkillTool(command_registry=registry)

        monkeypatch.setattr(
            "iac_code.skills.processor.process_prompt_command", AsyncMock(side_effect=RuntimeError("boom"))
        )

        result = await tool.execute(tool_input={"skill": "test-skill"}, context=ToolContext())

        assert result.is_error
        assert "execution failed" in result.content.lower()
        assert "boom" in result.content

    @pytest.mark.asyncio
    async def test_execute_fork_failure_returns_error(self, monkeypatch):
        registry = _make_registry_with_skill(source=SkillSource.PROJECT, context="fork")
        tool = SkillTool(command_registry=registry)

        monkeypatch.setattr(
            "iac_code.skills.processor.process_prompt_command",
            AsyncMock(return_value=SimpleNamespace(prompt_content="expanded prompt")),
        )
        monkeypatch.setattr("iac_code.agent.agent_tool.run_sub_agent", AsyncMock(side_effect=RuntimeError("fork boom")))

        result = await tool.execute(tool_input={"skill": "test-skill"}, context=ToolContext(cwd="/tmp/work"))

        assert result.is_error
        assert "forked execution failed" in result.content.lower()
        assert "fork boom" in result.content

    def test_normalize_name(self):
        assert SkillTool._normalize_name("/Test-Skill") == "test-skill"
        assert SkillTool._normalize_name("  skill  ") == "skill"

    def test_render_tool_use_message(self):
        registry = CommandRegistry()
        tool = SkillTool(command_registry=registry)
        assert tool.render_tool_use_message({"skill": "test"}) == "test"
        assert tool.render_tool_use_message({}) is None

    def test_has_only_safe_properties(self):
        safe_skill = SimpleNamespace(frontmatter=SimpleNamespace(allowed_tools=[]), content="plain text")
        tool_skill = SimpleNamespace(frontmatter=SimpleNamespace(allowed_tools=["bash(*)"]), content="plain text")
        shell_skill = SimpleNamespace(frontmatter=SimpleNamespace(allowed_tools=[]), content="```!bash\nrm -rf /\n```")

        assert SkillTool._has_only_safe_properties(safe_skill) is True
        assert SkillTool._has_only_safe_properties(tool_skill) is False
        assert SkillTool._has_only_safe_properties(shell_skill) is False


class TestSkillToolPermissions:
    @pytest.mark.asyncio
    async def test_bundled_skill_auto_allowed(self):
        registry = _make_registry_with_skill(source=SkillSource.BUNDLED)
        tool = SkillTool(command_registry=registry)

        result = await tool.check_permissions({"skill": "test-skill"})
        assert result.behavior == "allow"

    @pytest.mark.asyncio
    async def test_safe_project_skill_auto_allowed(self):
        registry = _make_registry_with_skill(source=SkillSource.PROJECT, content="Safe content")
        tool = SkillTool(command_registry=registry)

        result = await tool.check_permissions({"skill": "test-skill"})
        assert result.behavior == "allow"

    @pytest.mark.asyncio
    async def test_skill_with_tools_asks(self):
        registry = _make_registry_with_skill(
            source=SkillSource.PROJECT,
            allowed_tools=["bash(*)"],
        )
        tool = SkillTool(command_registry=registry)

        result = await tool.check_permissions({"skill": "test-skill"})
        assert result.behavior == "ask"

    @pytest.mark.asyncio
    async def test_skill_with_shell_commands_asks(self):
        registry = _make_registry_with_skill(
            source=SkillSource.PROJECT,
            content="Run !`echo hello`",
        )
        tool = SkillTool(command_registry=registry)

        result = await tool.check_permissions({"skill": "test-skill"})
        assert result.behavior == "ask"

    @pytest.mark.asyncio
    async def test_nonexistent_skill_denied(self):
        registry = CommandRegistry()
        tool = SkillTool(command_registry=registry)

        result = await tool.check_permissions({"skill": "nonexistent"})
        assert result.behavior == "deny"

    @pytest.mark.asyncio
    async def test_permission_message_includes_project_source(self):
        registry = _make_registry_with_skill(source=SkillSource.PROJECT, allowed_tools=["bash(*)"])
        tool = SkillTool(command_registry=registry)

        result = await tool.check_permissions({"skill": "test-skill"})

        assert result.behavior == "ask"
        assert "project" in result.message.lower()
