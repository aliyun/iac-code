"""Built-in skill registration system."""

from __future__ import annotations

from typing import Callable

from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillContext, SkillDefinition
from iac_code.types.skill_source import SkillSource

# Global bundled skill list
_bundled_skills: list[SkillDefinition] = []


def register_bundled_skill(
    *,
    name: str,
    description: str,
    prompt: str | None = None,
    get_prompt: Callable | None = None,
    when_to_use: str = "",
    argument_hint: str = "",
    arguments: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    model: str = "inherit",
    effort: str = "",
    user_invocable: bool = True,
    context: str = "inline",
    agent: str = "general-purpose",
    skill_root: str = "",
    auto_trigger: dict[str, str] | None = None,
) -> None:
    """Register a bundled skill."""
    frontmatter = SkillFrontmatter(
        name=name,
        description=description,
        when_to_use=when_to_use,
        argument_hint=argument_hint,
        arguments=arguments or [],
        allowed_tools=allowed_tools or [],
        model=model,
        effort=effort,
        user_invocable=user_invocable,
        context=context,
        agent=agent,
        auto_trigger=auto_trigger or {},
    )

    # Create prompt provider
    # Only use a custom provider for get_prompt functions.
    # Plain prompt strings go through the standard renderer so that
    # skill_root injection and other pipeline steps apply automatically.
    provider = _FunctionPromptProvider(get_prompt) if get_prompt is not None else None

    skill = SkillDefinition(
        name=name,
        description=description,
        frontmatter=frontmatter,
        content=prompt or "",
        source=SkillSource.BUNDLED,
        skill_root=skill_root,
        content_length=len(prompt or ""),
        _prompt_provider=provider,
    )

    _bundled_skills.append(skill)


def get_bundled_skills() -> list[SkillDefinition]:
    """Return all registered bundled skills."""
    return list(_bundled_skills)


def init_bundled_skills() -> None:
    """Initialize all bundled skills. Called once at startup."""
    # Guard against double initialization
    if _bundled_skills:
        return

    from iac_code.skills.bundled.simplify import register_simplify_skill

    register_simplify_skill()

    from iac_code.skills.bundled.iac_aliyun import register_iac_aliyun_skill

    register_iac_aliyun_skill()


class _FunctionPromptProvider:
    """Prompt provider that delegates to an async function."""

    def __init__(self, func: Callable) -> None:
        self._func = func

    async def get_prompt(self, args: str, context: SkillContext) -> str:
        return await self._func(args, context)
