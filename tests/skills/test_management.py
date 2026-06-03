"""Tests for skill management state construction."""

from __future__ import annotations

from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.management import build_skill_management_state
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


def _skill(name: str, source: SkillSource, *, content: str = "abcd") -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=f"{name} description",
        frontmatter=SkillFrontmatter(description=f"{name} description"),
        content=content,
        content_length=len(content),
        source=source,
        skill_root=f"/repo/{name}",
    )


def test_disabled_non_bundled_skills_are_split_out():
    state = build_skill_management_state(
        [_skill("team-review", SkillSource.PROJECT), _skill("iac-aliyun", SkillSource.BUNDLED)],
        {"team-review"},
    )

    assert [cmd.name for cmd in state.enabled_commands] == ["iac-aliyun"]
    assert state.disabled_commands["team-review"].name == "team-review"
    assert {item.name: item.enabled for item in state.items} == {"team-review": False, "iac-aliyun": True}


def test_bundled_skills_ignore_disabled_setting_and_are_locked():
    state = build_skill_management_state([_skill("iac-aliyun", SkillSource.BUNDLED)], {"iac-aliyun"})

    assert [cmd.name for cmd in state.enabled_commands] == ["iac-aliyun"]
    assert state.disabled_commands == {}
    assert state.items[0].enabled is True
    assert state.items[0].locked is True
