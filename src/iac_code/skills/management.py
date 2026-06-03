"""Build enabled/disabled skill state for REPL and management UI."""

from __future__ import annotations

from dataclasses import dataclass

from iac_code.commands.registry import PromptCommand
from iac_code.skills.discovery import skill_to_command
from iac_code.skills.settings import normalize_skill_name
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


@dataclass(frozen=True)
class SkillManagementItem:
    """User-facing skill state for the `/skills` picker."""

    name: str
    description: str
    source: SkillSource
    content_length: int
    path: str
    enabled: bool
    locked: bool


@dataclass(frozen=True)
class SkillManagementState:
    """Enabled commands plus disabled metadata for runtime lookups."""

    items: list[SkillManagementItem]
    enabled_commands: list[PromptCommand]
    disabled_commands: dict[str, PromptCommand]
    locked_skill_names: set[str]


def build_skill_management_state(
    skills: list[SkillDefinition],
    disabled_skill_names: set[str],
) -> SkillManagementState:
    """Apply disabled settings to discovered skills.

    Bundled skills are locked on and ignore disabled settings.
    """
    disabled = {normalize_skill_name(name) for name in disabled_skill_names}
    items: list[SkillManagementItem] = []
    enabled_commands: list[PromptCommand] = []
    disabled_commands: dict[str, PromptCommand] = {}
    locked_skill_names: set[str] = set()

    for skill in sorted(skills, key=lambda item: item.name):
        name = normalize_skill_name(skill.name)
        locked = skill.source == SkillSource.BUNDLED
        enabled = locked or name not in disabled
        command = skill_to_command(skill)

        if locked:
            locked_skill_names.add(name)
        if enabled:
            enabled_commands.append(command)
        else:
            disabled_commands[name] = command

        items.append(
            SkillManagementItem(
                name=skill.name,
                description=skill.description,
                source=skill.source,
                content_length=skill.content_length,
                path=skill.skill_root,
                enabled=enabled,
                locked=locked,
            )
        )

    return SkillManagementState(
        items=items,
        enabled_commands=enabled_commands,
        disabled_commands=disabled_commands,
        locked_skill_names=locked_skill_names,
    )
