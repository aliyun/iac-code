"""Tests for CommandRegistry skill extensions."""

from iac_code.commands.registry import CommandRegistry, LocalCommand, PromptCommand
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


async def dummy_handler(**kwargs):
    return "ok"


def _make_skill_command(name: str, user_invocable: bool = True) -> PromptCommand:
    fm = SkillFrontmatter(description=f"Skill {name}", user_invocable=user_invocable)
    skill = SkillDefinition(
        name=name,
        description=f"Skill {name}",
        frontmatter=fm,
        content="Body",
        source=SkillSource.BUNDLED,
    )
    return PromptCommand(name=name, description=f"Skill {name}", skill=skill, source=SkillSource.BUNDLED)


class TestCommandRegistrySkillExtensions:
    def test_get_skills(self):
        registry = CommandRegistry()
        registry.register(LocalCommand(name="help", description="Help", handler=dummy_handler))
        registry.register(_make_skill_command("simplify"))

        skills = registry.get_skills()
        assert len(skills) == 1
        assert skills[0].name == "simplify"

    def test_get_user_invocable_skills(self):
        registry = CommandRegistry()
        registry.register(_make_skill_command("public", user_invocable=True))
        registry.register(_make_skill_command("private", user_invocable=False))

        user_skills = registry.get_user_invocable_skills()
        assert len(user_skills) == 1
        assert user_skills[0].name == "public"

    def test_get_model_invocable_skills(self):
        registry = CommandRegistry()
        registry.register(_make_skill_command("skill1"))
        registry.register(_make_skill_command("skill2"))

        model_skills = registry.get_model_invocable_skills()
        assert len(model_skills) == 2

    def test_record_skill_usage(self):
        registry = CommandRegistry()
        registry.record_skill_usage("test")
        registry.record_skill_usage("test")
        registry.record_skill_usage("other")
        assert registry._skill_usage_counts["test"] == 2
        assert registry._skill_usage_counts["other"] == 1

    def test_mixed_commands_in_get_all(self):
        registry = CommandRegistry()
        registry.register(LocalCommand(name="help", description="Help", handler=dummy_handler))
        registry.register(_make_skill_command("simplify"))

        all_cmds = registry.get_all()
        assert len(all_cmds) == 2
        names = {c.name for c in all_cmds}
        assert names == {"help", "simplify"}

    def test_hidden_local_command_is_exact_only(self):
        registry = CommandRegistry()
        registry.register(LocalCommand(name="memory", description="Edit memory", handler=dummy_handler))
        registry.register(
            LocalCommand(
                name="memory-folder",
                description="Legacy memory folder",
                handler=dummy_handler,
                hidden=True,
            )
        )

        assert registry.get("memory-folder") is not None
        assert "memory-folder" not in {cmd.name for cmd in registry.get_all()}
        assert "memory-folder" not in registry.get_completions("memory")
        assert all(match.command.name != "memory-folder" for match in registry.fuzzy_search("memory-folder"))
        assert registry.get_best_prefix_match("memory-f") is None

    def test_skill_is_skill_property(self):
        cmd = _make_skill_command("test")
        assert cmd.is_skill is True

    def test_local_command_is_not_skill(self):
        cmd = LocalCommand(name="help", description="Help", handler=dummy_handler)
        assert cmd.is_skill is False

    def test_prompt_command_properties(self):
        fm = SkillFrontmatter(
            description="Test",
            when_to_use="Use for testing",
            user_invocable=False,
        )
        skill = SkillDefinition(
            name="test",
            description="Test",
            frontmatter=fm,
            content="Body of length 15",
            content_length=15,
        )
        cmd = PromptCommand(name="test", description="Test", skill=skill)
        assert cmd.when_to_use == "Use for testing"
        assert cmd.user_invocable is False
        assert cmd.model_invocable is True
        assert cmd.content_length == 15
