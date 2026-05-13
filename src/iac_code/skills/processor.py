"""Unified skill processing — shared by slash commands and SkillTool."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from iac_code.skills.skill_definition import SkillContext


@dataclass
class ProcessedSkillResult:
    """Unified result of processing a skill (from either slash command or SkillTool).

    Both execution paths produce this same structure, ensuring identical behavior.
    """

    prompt_content: str
    skill_name: str
    new_messages: list[dict[str, Any]]
    context_modifier: Callable[[dict], dict] | None = None
    is_fork: bool = False


async def process_prompt_command(
    command: Any,
    args: str,
    *,
    session_id: str = "",
) -> ProcessedSkillResult:
    """Unified skill processing — called by BOTH slash commands AND SkillTool.

    1. Render skill prompt (argument substitution + variable replacement + shell execution)
    2. Wrap as <skill-name> tagged message
    3. Build context_modifier (allowed_tools / model / effort)
    """
    skill = command.skill
    if skill is None:
        raise ValueError(f"Command '{command.name}' is not a skill")

    skill_context = SkillContext(
        cwd=os.getcwd(),
        session_id=session_id,
        skill_dir=skill.skill_root or "",
        skill_root=skill.skill_root or "",
    )

    # Step 1: Render prompt
    prompt_content = await skill.get_prompt(args, skill_context)

    # Step 2: Wrap as tagged message
    tagged_content = f"<skill-name>{command.name}</skill-name>\n\n{prompt_content}"
    new_messages = [{"role": "user", "content": tagged_content}]

    # Step 3: Build context_modifier
    allowed_tools = skill.allowed_tools
    model_override = skill.model_override
    effort_override = skill.effort_override

    context_modifier = None
    if allowed_tools or (model_override and model_override != "inherit") or effort_override:

        def context_modifier(ctx: dict) -> dict:
            modified = {**ctx}
            if allowed_tools:
                existing = modified.get("allowed_tool_rules", [])
                modified["allowed_tool_rules"] = existing + allowed_tools
            if model_override and model_override != "inherit":
                modified["model_override"] = model_override
            if effort_override:
                modified["effort_override"] = effort_override
            return modified

    return ProcessedSkillResult(
        prompt_content=prompt_content,
        skill_name=command.name,
        new_messages=new_messages,
        context_modifier=context_modifier,
        is_fork=(skill.context_mode == "fork"),
    )
