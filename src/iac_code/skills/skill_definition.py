"""Core skill definition types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.types.skill_source import SkillSource


class SkillPromptProvider(Protocol):
    """Protocol for skill prompt generation."""

    async def get_prompt(self, args: str, context: SkillContext) -> str:
        """Generate the final prompt content for this skill."""
        ...


@dataclass
class SkillContext:
    """Context available during skill prompt generation."""

    cwd: str
    session_id: str = ""
    skill_dir: str = ""
    skill_root: str = ""


@dataclass
class SkillDefinition:
    """Complete definition of a skill."""

    name: str
    description: str
    frontmatter: SkillFrontmatter
    content: str
    source: SkillSource = SkillSource.PROJECT
    file_path: str = ""
    skill_root: str = ""
    content_length: int = 0

    # Bundled skills can provide a custom prompt generator
    _prompt_provider: SkillPromptProvider | None = field(default=None, repr=False)

    @property
    def is_user_invocable(self) -> bool:
        return self.frontmatter.user_invocable

    @property
    def allowed_tools(self) -> list[str]:
        return self.frontmatter.allowed_tools

    @property
    def model_override(self) -> str:
        return self.frontmatter.model

    @property
    def effort_override(self) -> str:
        return self.frontmatter.effort

    @property
    def context_mode(self) -> str:
        """'inline' or 'fork'"""
        return self.frontmatter.context

    @property
    def agent_type(self) -> str:
        return self.frontmatter.agent

    @property
    def when_to_use(self) -> str:
        return self.frontmatter.when_to_use

    @property
    def auto_trigger(self) -> dict[str, str]:
        return self.frontmatter.auto_trigger

    async def get_prompt(self, args: str, context: SkillContext) -> str:
        """Generate the final prompt content."""
        if self._prompt_provider is not None:
            return await self._prompt_provider.get_prompt(args, context)

        from iac_code.skills.renderer import render_skill_prompt

        return await render_skill_prompt(self, args, context)
