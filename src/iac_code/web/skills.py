"""Web API helpers for skill discovery and enablement settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iac_code.skills.discovery import discover_all_skills, skill_to_command
from iac_code.skills.management import build_skill_management_state
from iac_code.skills.settings import load_disabled_skills, normalize_skill_name, save_disabled_skills
from iac_code.skills.skill_definition import SkillDefinition


def skills_payload(cwd: Path) -> dict[str, list[dict[str, Any]]]:
    return {"skills": _skill_items(cwd)}


def save_disabled_payload(cwd: Path, disabled: list[str]) -> dict[str, list[dict[str, Any]]]:
    skills = discover_all_skills(str(cwd))
    state = build_skill_management_state(skills, {normalize_skill_name(name) for name in disabled})
    save_disabled_skills(
        {normalize_skill_name(name) for name in disabled},
        locked_skill_names=state.locked_skill_names,
    )
    return {"skills": _skill_items_from_skills(skills)}


def _skill_items(cwd: Path) -> list[dict[str, Any]]:
    return _skill_items_from_skills(discover_all_skills(str(cwd)))


def _skill_items_from_skills(skills: list[SkillDefinition]) -> list[dict[str, Any]]:
    state = build_skill_management_state(skills, load_disabled_skills())
    by_name = {normalize_skill_name(skill.name): skill for skill in skills}
    return [
        {
            "name": item.name,
            "description": item.description,
            "source": item.source.value,
            # Normalize to POSIX separators so the path contract is stable
            # across platforms (native paths would use backslashes on Windows).
            "path": Path(item.path).as_posix() if item.path else item.path,
            "contentLength": item.content_length,
            "enabled": item.enabled,
            "locked": item.locked,
            "commandAvailable": bool(item.enabled and by_name[normalize_skill_name(item.name)].is_user_invocable),
            "modelInvocable": bool(skill_to_command(by_name[normalize_skill_name(item.name)]).model_invocable),
        }
        for item in state.items
        if normalize_skill_name(item.name) in by_name
    ]
