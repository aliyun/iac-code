"""Tests for skill discovery."""

from iac_code.skills.discovery import (
    DynamicSkillTracker,
    _find_project_skills_dirs,
    _scan_skills_dir,
    discover_all_skills,
    skill_to_command,
)
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


class TestScanSkillsDir:
    """Tests for _scan_skills_dir."""

    def test_empty_directory(self, tmp_path):
        """Empty directory returns empty list."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        assert _scan_skills_dir(skills_dir) == []

    def test_nonexistent_directory(self, tmp_path):
        """Non-existent directory returns empty list."""
        assert _scan_skills_dir(tmp_path / "nonexistent") == []

    def test_single_file_format(self, tmp_path):
        """Discover single-file skill (skill-name.md)."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "greet.md").write_text("---\ndescription: Greet\n---\nHello!")

        skills = _scan_skills_dir(skills_dir)
        assert len(skills) == 1
        assert skills[0].name == "greet"
        assert skills[0].description == "Greet"

    def test_directory_format(self, tmp_path):
        """Discover directory-format skill (skill-name/SKILL.md)."""
        skills_dir = tmp_path / "skills"
        skill_folder = skills_dir / "review"
        skill_folder.mkdir(parents=True)
        (skill_folder / "SKILL.md").write_text("---\ndescription: Code review\n---\nReview code.")

        skills = _scan_skills_dir(skills_dir)
        assert len(skills) == 1
        assert skills[0].name == "review"
        assert skills[0].skill_root == str(skill_folder)

    def test_ignores_non_md_files(self, tmp_path):
        """Non-.md files are ignored."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "readme.txt").write_text("Not a skill")
        (skills_dir / "script.sh").write_text("#!/bin/bash")

        assert _scan_skills_dir(skills_dir) == []

    def test_ignores_bare_skill_md(self, tmp_path):
        """SKILL.md at top level is not a single-file skill."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "SKILL.md").write_text("---\ndescription: Ignored\n---\n")

        assert _scan_skills_dir(skills_dir) == []

    def test_multiple_skills(self, tmp_path):
        """Discover multiple skills of different formats."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "alpha.md").write_text("---\ndescription: Alpha\n---\n")
        beta_dir = skills_dir / "beta"
        beta_dir.mkdir()
        (beta_dir / "SKILL.md").write_text("---\ndescription: Beta\n---\n")

        skills = _scan_skills_dir(skills_dir)
        names = {s.name for s in skills}
        assert names == {"alpha", "beta"}


class TestFindProjectSkillsDirs:
    """Tests for _find_project_skills_dirs."""

    def test_finds_skills_dir(self, tmp_path):
        """Finds skills/ directory."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        result = _find_project_skills_dirs(str(tmp_path))
        assert any(str(d) == str(skills_dir) for d in result)

    def test_finds_dotdir_skills(self, tmp_path):
        """Finds .iac-code/skills/ directory."""
        dotdir = tmp_path / ".iac-code" / "skills"
        dotdir.mkdir(parents=True)

        result = _find_project_skills_dirs(str(tmp_path))
        assert any(str(d) == str(dotdir) for d in result)

    def test_priority_order(self, tmp_path):
        """skills/ comes before .iac-code/skills/ in results (low->high priority)."""
        bare = tmp_path / "skills"
        bare.mkdir()
        dotdir = tmp_path / ".iac-code" / "skills"
        dotdir.mkdir(parents=True)

        result = _find_project_skills_dirs(str(tmp_path))
        bare_idx = next(i for i, d in enumerate(result) if str(d) == str(bare))
        dot_idx = next(i for i, d in enumerate(result) if str(d) == str(dotdir))
        assert bare_idx < dot_idx  # bare before dotdir = lower priority


class TestDiscoverAllSkills:
    """Tests for discover_all_skills."""

    def test_discovers_bundled_skills(self, tmp_path):
        """Bundled skills are included."""
        from iac_code.skills.bundled import _bundled_skills, init_bundled_skills

        _bundled_skills.clear()
        init_bundled_skills()

        skills = discover_all_skills(str(tmp_path))
        names = {s.name for s in skills}
        assert "simplify" in names

    def test_project_overrides_bundled(self, tmp_path):
        """Project skill overrides bundled skill with same name."""
        from iac_code.skills.bundled import _bundled_skills, init_bundled_skills

        _bundled_skills.clear()
        init_bundled_skills()

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "simplify.md").write_text("---\ndescription: Custom simplify\n---\nCustom body")

        skills = discover_all_skills(str(tmp_path))
        simplify = next(s for s in skills if s.name == "simplify")
        assert simplify.source == SkillSource.PROJECT
        assert simplify.description == "Custom simplify"


class TestSkillToCommand:
    """Tests for skill_to_command."""

    def test_converts_to_prompt_command(self):
        """SkillDefinition converts to PromptCommand."""
        skill = SkillDefinition(
            name="test",
            description="Test skill",
            frontmatter=SkillFrontmatter(description="Test skill"),
            content="Body",
            source=SkillSource.BUNDLED,
        )
        cmd = skill_to_command(skill)
        assert cmd.is_skill is True
        assert cmd.name == "test"
        assert cmd.description == "Test skill"
        assert cmd.skill is skill
        assert cmd.source == SkillSource.BUNDLED


class TestDynamicSkillTracker:
    """Tests for DynamicSkillTracker."""

    def _make_skill(self, name: str, patterns: list[str]) -> SkillDefinition:
        return SkillDefinition(
            name=name,
            description=f"Skill {name}",
            frontmatter=SkillFrontmatter(paths=patterns),
            content="Body",
        )

    def test_no_activation_without_matching_path(self):
        tracker = DynamicSkillTracker()
        skill = self._make_skill("py-skill", ["*.py"])
        tracker.on_file_accessed("readme.md", [skill])
        assert tracker.get_activated_skills() == []

    def test_activates_on_matching_path(self):
        tracker = DynamicSkillTracker()
        skill = self._make_skill("py-skill", ["*.py"])
        tracker.on_file_accessed("main.py", [skill])
        assert len(tracker.get_activated_skills()) == 1
        assert tracker.get_activated_skills()[0].name == "py-skill"

    def test_no_duplicate_activation(self):
        tracker = DynamicSkillTracker()
        skill = self._make_skill("py-skill", ["*.py"])
        tracker.on_file_accessed("a.py", [skill])
        tracker.on_file_accessed("b.py", [skill])
        assert len(tracker.get_activated_skills()) == 1

    def test_glob_pattern_matching(self):
        tracker = DynamicSkillTracker()
        skill = self._make_skill("src-skill", ["src/**/*.py"])
        tracker.on_file_accessed("src/core/main.py", [skill])
        assert len(tracker.get_activated_skills()) == 1
