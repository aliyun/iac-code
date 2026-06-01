"""Skill discovery — scan filesystem sources and convert to PromptCommands."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from iac_code.commands.registry import PromptCommand
from iac_code.config import get_config_dir
from iac_code.skills.loader import load_skill_from_path
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource
from iac_code.utils.project_paths import find_git_worktree_root


def discover_all_skills(cwd: str) -> list[SkillDefinition]:
    """Discover all available skills from all sources.

    Load order (later entries override earlier with same name):
    1. User global skills (``<config-dir>/skills/``; defaults to
       ``~/.iac-code/skills/``, follows ``IAC_CODE_CONFIG_DIR``)
    2. Project local skills, from git root toward cwd:
       ``skills/`` then ``.iac-code/skills/`` at each level
    3. Bundled skills
    """
    from iac_code.skills.bundled import get_bundled_skills

    skills: dict[str, SkillDefinition] = {}

    # 1. User global skills
    user_skills_dir = get_config_dir() / "skills"
    for skill in _scan_skills_dir(user_skills_dir):
        skill.source = SkillSource.USER
        skills[skill.name] = skill

    # 2. Project local skills. The directory order is low -> high priority.
    for project_dir in _find_project_skills_dirs(cwd):
        for skill in _scan_skills_dir(project_dir):
            skill.source = SkillSource.PROJECT
            skills[skill.name] = skill

    # 3. Bundled skills have the highest priority and cannot be shadowed by
    # project-local or user-global skills with the same name.
    for skill in get_bundled_skills():
        skills[skill.name] = skill

    return list(skills.values())


def _find_project_skills_dirs(cwd: str) -> list[Path]:
    """Find project skills directories from project root toward cwd.

    Returns directories in priority order (low -> high):
    - ancestor ``skills/`` before descendant ``skills/``
    - within the same directory, ``skills/`` before ``.iac-code/skills/``
    """
    result: list[Path] = []
    current = Path(cwd).resolve()
    git_root = find_git_worktree_root(str(current))
    search_dirs = _project_search_dirs(current, git_root)

    for current in search_dirs:
        bare = current / "skills"
        if bare.is_dir():
            result.append(bare)
        dotdir = current / ".iac-code" / "skills"
        if dotdir.is_dir():
            result.append(dotdir)
    return result


def _project_search_dirs(cwd: Path, git_root: Path | None) -> list[Path]:
    """Return directories to inspect, ordered from low to high priority."""
    if git_root is None:
        return [cwd]

    try:
        cwd.relative_to(git_root)
    except ValueError:
        return [cwd]

    dirs: list[Path] = []
    while True:
        dirs.append(cwd)
        if cwd == git_root:
            break
        parent = cwd.parent
        if parent == cwd:
            break
        cwd = parent
    dirs.reverse()
    return dirs


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
        normalized_path = file_path.replace("\\", "/")
        for pattern in patterns:
            if fnmatch.fnmatch(normalized_path, pattern.replace("\\", "/")):
                return True
        return False
