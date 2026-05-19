"""Skill discovery — scan filesystem sources and convert to PromptCommands."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from iac_code.commands.registry import PromptCommand
from iac_code.config import get_config_dir
from iac_code.skills.loader import load_skill_from_path
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


def discover_all_skills(cwd: str) -> list[SkillDefinition]:
    """Discover all available skills from all sources.

    Load order (later entries override earlier with same name):
    1. Bundled skills
    2. User global skills (``<config-dir>/skills/``; defaults to
       ``~/.iac-code/skills/``, follows ``IAC_CODE_CONFIG_DIR``)
    3. Project local skills — skills/ (lower priority)
    4. Project local skills — .iac-code/skills/ (higher priority, overrides same name)
    """
    from iac_code.skills.bundled import get_bundled_skills

    skills: dict[str, SkillDefinition] = {}

    # 1. Bundled skills
    for skill in get_bundled_skills():
        skills[skill.name] = skill

    # 2. User global skills
    user_skills_dir = get_config_dir() / "skills"
    for skill in _scan_skills_dir(user_skills_dir):
        skill.source = SkillSource.USER
        skills[skill.name] = skill

    # 3. Project local skills (two locations, .iac-code/skills/ has higher priority)
    for project_dir in _find_project_skills_dirs(cwd):
        for skill in _scan_skills_dir(project_dir):
            skill.source = SkillSource.PROJECT
            skills[skill.name] = skill

    return list(skills.values())


def _find_project_skills_dirs(cwd: str) -> list[Path]:
    """Find project skills directories, searching up from cwd.

    Returns directories in priority order (low -> high):
    - skills/           (root-level, lower priority)
    - .iac-code/skills/ (config-level, higher priority)
    """
    result: list[Path] = []
    current = Path(cwd).resolve()
    while True:
        # Lower priority: skills/
        bare = current / "skills"
        if bare.is_dir():
            result.append(bare)
        # Higher priority: .iac-code/skills/
        dotdir = current / ".iac-code" / "skills"
        if dotdir.is_dir():
            result.append(dotdir)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return result


def _scan_skills_dir(skills_dir: Path) -> list[SkillDefinition]:
    """Scan a skills directory for skill files."""
    if not skills_dir.is_dir():
        return []

    skills: list[SkillDefinition] = []
    seen_real_paths: set[str] = set()

    for entry in skills_dir.iterdir():
        real_path = str(entry.resolve())
        if real_path in seen_real_paths:
            continue
        seen_real_paths.add(real_path)

        if entry.is_dir():
            # Directory format: skill-name/SKILL.md
            skill_file = entry / "SKILL.md"
            if skill_file.is_file():
                skill = load_skill_from_path(skill_file, skill_name=entry.name)
                if skill:
                    skill.skill_root = str(entry)
                    skills.append(skill)
        elif entry.suffix == ".md" and entry.name != "SKILL.md":
            # Single file format: skill-name.md
            skill = load_skill_from_path(entry, skill_name=entry.stem)
            if skill:
                skills.append(skill)

    return skills


def skill_to_command(skill: SkillDefinition) -> PromptCommand:
    """Convert a SkillDefinition to a PromptCommand."""
    return PromptCommand(
        name=skill.name,
        description=skill.description,
        skill=skill,
        source=skill.source,
    )


class DynamicSkillTracker:
    """Tracks file access and dynamically activates path-matched skills."""

    def __init__(self) -> None:
        self._accessed_paths: set[str] = set()
        self._activated_skills: dict[str, SkillDefinition] = {}

    def on_file_accessed(self, file_path: str, all_skills: list[SkillDefinition]) -> None:
        """Called when a file is accessed by a tool. Checks path-matched skills."""
        self._accessed_paths.add(file_path)
        for skill in all_skills:
            if skill.name in self._activated_skills:
                continue
            if skill.frontmatter.paths and self._matches_any_pattern(file_path, skill.frontmatter.paths):
                self._activated_skills[skill.name] = skill

    def get_activated_skills(self) -> list[SkillDefinition]:
        return list(self._activated_skills.values())

    @staticmethod
    def _matches_any_pattern(file_path: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return True
        return False
