"""Tests for skill discovery."""

import subprocess

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

    def test_ignores_single_file_markdown(self, tmp_path):
        """Top-level markdown files are documentation, not skills."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "greet.md").write_text("---\ndescription: Greet\n---\nHello!")

        assert _scan_skills_dir(skills_dir) == []

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

    def test_ignores_readme_markdown(self, tmp_path):
        """README.md documents a skills directory and is not a single-file skill."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "README.md").write_text("# Skills\n\nDocumentation.")

        assert _scan_skills_dir(skills_dir) == []

    def test_multiple_skills(self, tmp_path):
        """Discover multiple directory-format skills."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        alpha_dir = skills_dir / "alpha"
        alpha_dir.mkdir()
        (alpha_dir / "SKILL.md").write_text("---\ndescription: Alpha\n---\n")
        beta_dir = skills_dir / "beta"
        beta_dir.mkdir()
        (beta_dir / "SKILL.md").write_text("---\ndescription: Beta\n---\n")

        skills = _scan_skills_dir(skills_dir)
        names = {s.name for s in skills}
        assert names == {"alpha", "beta"}


class TestFindProjectSkillsDirs:
    """Tests for _find_project_skills_dirs."""

    def _init_git_repo(self, path):
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)

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

    def test_searches_git_root_to_cwd_only(self, tmp_path):
        """Project skill lookup does not escape the current git repository."""
        outer_skills = tmp_path / "skills"
        outer_skills.mkdir()
        repo = tmp_path / "repo"
        nested = repo / "app" / "service"
        nested.mkdir(parents=True)
        self._init_git_repo(repo)

        root_skills = repo / "skills"
        root_skills.mkdir()
        child_skills = repo / "app" / "skills"
        child_skills.mkdir()

        assert _find_project_skills_dirs(str(nested)) == [root_skills, child_skills]

    def test_nearer_project_skills_have_higher_priority(self, tmp_path):
        """Returned directories are ordered from lower to higher priority."""
        repo = tmp_path / "repo"
        nested = repo / "app" / "service"
        nested.mkdir(parents=True)
        self._init_git_repo(repo)

        root_bare = repo / "skills"
        root_dot = repo / ".iac-code" / "skills"
        child_bare = repo / "app" / "skills"
        child_dot = repo / "app" / ".iac-code" / "skills"
        for path in (root_bare, root_dot, child_bare, child_dot):
            path.mkdir(parents=True)

        assert _find_project_skills_dirs(str(nested)) == [root_bare, root_dot, child_bare, child_dot]


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

    def test_bundled_overrides_project(self, tmp_path):
        """Bundled skill wins over project skill with the same name."""
        from iac_code.skills.bundled import _bundled_skills, init_bundled_skills

        _bundled_skills.clear()
        init_bundled_skills()

        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "simplify"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\ndescription: Custom simplify\n---\nCustom body")

        skills = discover_all_skills(str(tmp_path))
        simplify = next(s for s in skills if s.name == "simplify")
        assert simplify.source == SkillSource.BUNDLED
        assert simplify.description != "Custom simplify"

    def test_nearer_project_skill_overrides_ancestor(self, tmp_path):
        """A project skill nearer cwd overrides an ancestor project skill."""
        repo = tmp_path / "repo"
        nested = repo / "app" / "service"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)

        root_skill = repo / "skills" / "deploy"
        root_skill.mkdir(parents=True)
        (root_skill / "SKILL.md").write_text("---\ndescription: Root deploy\n---\n")
        child_skill = repo / "app" / "skills" / "deploy"
        child_skill.mkdir(parents=True)
        (child_skill / "SKILL.md").write_text("---\ndescription: Child deploy\n---\n")

        skills = discover_all_skills(str(nested))
        deploy = next(s for s in skills if s.name == "deploy")
        assert deploy.description == "Child deploy"


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


class TestUserGlobalSkillsRespectConfigDirEnv:
    def test_user_global_skills_dir_respects_env(self, monkeypatch, tmp_path):
        """User-global skills are loaded from IAC_CODE_CONFIG_DIR/skills."""
        target = tmp_path / "alt-config"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))

        skills_dir = target / "skills"
        skill_dir = skills_dir / "alpha"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\ndescription: Alpha\n---\n")

        project_cwd = tmp_path / "proj"
        project_cwd.mkdir()

        from iac_code.skills.discovery import discover_all_skills
        from iac_code.types.skill_source import SkillSource

        skills = discover_all_skills(str(project_cwd))
        alpha = next((s for s in skills if s.name == "alpha"), None)
        assert alpha is not None
        assert alpha.source == SkillSource.USER
