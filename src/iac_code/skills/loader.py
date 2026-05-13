"""Load SkillDefinition from disk."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from iac_code.skills.frontmatter import parse_frontmatter
from iac_code.skills.skill_definition import SkillDefinition


def load_skill_from_path(file_path: Path, skill_name: str) -> SkillDefinition | None:
    """Load a skill from a markdown file.

    Args:
        file_path: Path to the .md file.
        skill_name: Default name (from directory or filename).

    Returns:
        SkillDefinition or None if the file cannot be read.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Failed to read skill file %s: %s", file_path, e)
        return None

    frontmatter, content = parse_frontmatter(text)

    # Use frontmatter name if provided, otherwise use the filesystem-derived name
    name = frontmatter.name or skill_name
    description = frontmatter.description or ""

    return SkillDefinition(
        name=name,
        description=description,
        frontmatter=frontmatter,
        content=content,
        file_path=str(file_path),
        content_length=len(content),
    )
