"""Tests for skill listing generation."""

from iac_code.commands.registry import PromptCommand
from iac_code.skills.bundled import get_bundled_skills, init_bundled_skills
from iac_code.skills.discovery import skill_to_command
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.listing import _assemble, _format_full, _format_truncated, build_skill_listing, get_char_budget
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


def _make_prompt_command(
    name: str,
    description: str,
    source: SkillSource = SkillSource.PROJECT,
    when_to_use: str = "",
) -> PromptCommand:
    fm = SkillFrontmatter(description=description, when_to_use=when_to_use)
    skill = SkillDefinition(
        name=name,
        description=description,
        frontmatter=fm,
        content="",
        source=source,
    )
    return PromptCommand(name=name, description=description, skill=skill, source=source)


class TestGetCharBudget:
    def test_default_budget(self):
        assert get_char_budget() == 8_000

    def test_custom_budget(self):
        # 100K tokens * 4 chars * 1% = 4000
        assert get_char_budget(100_000) == 4_000


class TestBuildSkillListing:
    def test_empty_skills(self):
        assert build_skill_listing([]) == ""

    def test_single_skill(self):
        cmd = _make_prompt_command("test", "A test skill")
        result = build_skill_listing([cmd])
        assert "test: A test skill" in result
        assert "The following skills are available" in result

    def test_with_when_to_use(self):
        cmd = _make_prompt_command("debug", "Debug code", when_to_use="Use when debugging")
        result = build_skill_listing([cmd])
        assert "debug:" in result
        assert "Debug code" in result
        assert "Use when debugging" in result

    def test_bundled_skills_priority(self):
        bundled = _make_prompt_command("simplify", "Simplify code", source=SkillSource.BUNDLED)
        project = _make_prompt_command("custom", "Custom skill", source=SkillSource.PROJECT)
        result = build_skill_listing([bundled, project])
        assert "simplify" in result
        assert "custom" in result

    def test_truncation_with_small_budget(self):
        skills = [_make_prompt_command(f"skill-{i}", f"Description for skill {i} " * 10) for i in range(50)]
        result = build_skill_listing(skills, context_window_tokens=1000)
        # Should still produce output, just truncated
        assert len(result) > 0

    def test_extreme_budget_falls_back_to_names_for_non_bundled(self):
        bundled = _make_prompt_command("bundled", "Bundled desc", source=SkillSource.BUNDLED)
        others = [_make_prompt_command(f"skill-{i}", "x" * 200) for i in range(3)]

        result = build_skill_listing([bundled, *others], context_window_tokens=10)

        assert "- bundled:" in result
        assert "- skill-0" in result
        assert "- skill-1" in result
        assert "- skill-2" in result

    def test_format_full_truncates_long_descriptions(self):
        lines = _format_full([_make_prompt_command("demo", "x" * 400, when_to_use="when useful")])
        assert lines[0].startswith("- demo:")
        assert lines[0].endswith("...")

    def test_format_truncated_drops_description_when_budget_too_small(self):
        lines = _format_truncated([_make_prompt_command("demo", "description")], per_skill_budget=3)
        assert lines == ["- demo"]

    def test_assemble_adds_header(self):
        result = _assemble(["- test: description"])
        assert result.startswith("The following skills are available")
        assert result.endswith("- test: description")

    def test_iac_aliyun_listing_contains_strong_trigger_guidance(self):
        init_bundled_skills()
        skill = next(s for s in get_bundled_skills() if s.name == "iac-aliyun")
        result = build_skill_listing([skill_to_command(skill)])
        assert "iac-aliyun" in result
        assert "Alibaba Cloud" in result
        assert "Terraform" in result
        assert "alicloud provider" in result
        assert "必须先调用 skill 工具加载 iac-aliyun" in result
