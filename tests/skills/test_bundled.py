"""Tests for bundled skill registration."""

from iac_code.skills.bundled import (
    _bundled_skills,
    get_bundled_skills,
    init_bundled_skills,
    register_bundled_skill,
)
from iac_code.types.skill_source import SkillSource


class TestBundledSkills:
    def setup_method(self):
        """Clear bundled skills before each test."""
        _bundled_skills.clear()

    def teardown_method(self):
        """Avoid leaking test-only bundled skills into later test modules."""
        _bundled_skills.clear()

    def test_register_static_skill(self):
        register_bundled_skill(
            name="test",
            description="A test skill",
            prompt="Do something.",
        )
        skills = get_bundled_skills()
        assert len(skills) == 1
        assert skills[0].name == "test"
        assert skills[0].description == "A test skill"
        assert skills[0].source == SkillSource.BUNDLED
        assert skills[0].content == "Do something."

    def test_register_dynamic_skill(self):
        async def custom_prompt(args, context):
            return f"Custom: {args}"

        register_bundled_skill(
            name="dynamic",
            description="Dynamic skill",
            get_prompt=custom_prompt,
        )
        skills = get_bundled_skills()
        assert len(skills) == 1
        assert skills[0]._prompt_provider is not None

    def test_init_bundled_skills(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        names = {s.name for s in skills}
        assert "simplify" in names

    def test_init_idempotent(self):
        init_bundled_skills()
        count1 = len(get_bundled_skills())
        init_bundled_skills()  # Second call should be no-op
        count2 = len(get_bundled_skills())
        assert count1 == count2

    def test_skill_frontmatter_populated(self):
        register_bundled_skill(
            name="test",
            description="Test",
            prompt="Body",
            when_to_use="Use for testing",
            allowed_tools=["bash"],
            model="claude-3-opus",
            effort="high",
            context="fork",
            agent="explore",
        )
        skill = get_bundled_skills()[0]
        assert skill.frontmatter.when_to_use == "Use for testing"
        assert skill.frontmatter.allowed_tools == ["bash"]
        assert skill.frontmatter.model == "claude-3-opus"
        assert skill.frontmatter.effort == "high"
        assert skill.frontmatter.context == "fork"
        assert skill.frontmatter.agent == "explore"

    def test_register_static_skill_with_auto_trigger_metadata(self):
        register_bundled_skill(
            name="test",
            description="A test skill",
            prompt="Do something.",
            auto_trigger={"script": "auto_trigger.py"},
        )
        skill = get_bundled_skills()[0]
        assert skill.auto_trigger == {"script": "auto_trigger.py"}
