"""Generic skill auto-trigger dispatcher."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from loguru import logger

from iac_code.commands.registry import PromptCommand
from iac_code.skills.processor import ProcessedSkillResult, process_prompt_command
from iac_code.types.skill_source import SkillSource


def has_skill_tag(content: str, skill_name: str) -> bool:
    return f"<skill-name>{skill_name}</skill-name>" in content


def context_has_skill_tag(messages: list[Any], skill_name: str) -> bool:
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
        if isinstance(content, str) and has_skill_tag(content, skill_name):
            return True
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                else:
                    text = getattr(block, "text", None) or getattr(block, "content", None)
                if isinstance(text, str) and has_skill_tag(text, skill_name):
                    return True
    return False


def find_auto_triggered_skills(
    prompt: str,
    skills: list[PromptCommand],
    *,
    loaded_skill_names: set[str],
    context_messages: list[Any] | None = None,
) -> list[PromptCommand]:
    if not prompt.strip():
        return []

    matches: list[PromptCommand] = []
    context_messages = context_messages or []
    for command in skills:
        skill = command.skill
        if skill is None or command.name in loaded_skill_names:
            continue
        if context_has_skill_tag(context_messages, command.name):
            loaded_skill_names.add(command.name)
            continue
        script = skill.auto_trigger.get("script")
        if not script or command.source != SkillSource.BUNDLED or skill.source != SkillSource.BUNDLED:
            continue
        module = _load_trigger_module(skill.skill_root, script, command.name)
        if module is None or not getattr(module, "ENABLE_AUTO_TRIGGER", True):
            continue
        should_trigger = getattr(module, "should_trigger", None)
        if not callable(should_trigger):
            continue
        try:
            if should_trigger(prompt):
                matches.append(command)
        except Exception as exc:
            logger.warning("Skill auto-trigger failed for {}: {}", command.name, exc)
    return matches


async def process_auto_triggered_skills(
    prompt: str,
    skills: list[PromptCommand],
    *,
    loaded_skill_names: set[str],
    context_messages: list[Any] | None = None,
    session_id: str = "",
) -> list[ProcessedSkillResult]:
    results: list[ProcessedSkillResult] = []
    for command in find_auto_triggered_skills(
        prompt,
        skills,
        loaded_skill_names=loaded_skill_names,
        context_messages=context_messages,
    ):
        result = await process_prompt_command(command, "", session_id=session_id)
        loaded_skill_names.add(command.name)
        results.append(result)
    return results


def _load_trigger_module(skill_root: str, script: str, skill_name: str) -> ModuleType | None:
    if not skill_root:
        return None
    script_path = (Path(skill_root) / script).resolve()
    root_path = Path(skill_root).resolve()
    if root_path not in script_path.parents:
        logger.warning("Skill auto-trigger script escapes skill root: {}", script)
        return None
    if not script_path.is_file():
        logger.warning("Skill auto-trigger script not found for {}: {}", skill_name, script_path)
        return None
    module_name = f"iac_code_skill_auto_trigger_{skill_name.replace('-', '_')}_{abs(hash(str(script_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning("Failed to load skill auto-trigger script for {}: {}", skill_name, exc)
        return None
    return module
