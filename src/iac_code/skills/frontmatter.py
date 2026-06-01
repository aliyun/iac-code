"""YAML Frontmatter parsing for skill markdown files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

FRONTMATTER_REGEX = re.compile(r"^---\s*\n([\s\S]*?)---\s*\n?")

# YAML special characters that need auto-quoting
YAML_SPECIAL_CHARS = set("{}[]*&#!|>%@")


@dataclass
class SkillFrontmatter:
    """Parsed skill frontmatter metadata."""

    name: str = ""
    description: str = ""
    descriptions: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    when_to_use: str = ""
    argument_hint: str = ""
    arguments: list[str] = field(default_factory=list)
    user_invocable: bool = True
    model: str = "inherit"
    effort: str = ""
    context: str = "inline"  # "inline" | "fork"
    agent: str = "general-purpose"
    paths: list[str] = field(default_factory=list)
    auto_trigger: dict[str, str] = field(default_factory=dict)


def parse_frontmatter(markdown: str) -> tuple[SkillFrontmatter, str]:
    """Parse YAML frontmatter from a skill markdown file.

    Returns:
        (frontmatter, content) - parsed metadata and remaining markdown body.
    """
    match = FRONTMATTER_REGEX.match(markdown)
    if not match:
        return SkillFrontmatter(), markdown

    frontmatter_text = match.group(1)
    content = markdown[match.end() :]

    # First parse attempt
    data = _parse_yaml_safe(frontmatter_text)

    # If failed, auto-quote problematic values and retry
    if data is None:
        quoted_text = _quote_problematic_values(frontmatter_text)
        data = _parse_yaml_safe(quoted_text)

    if data is None:
        return SkillFrontmatter(), content

    return _data_to_frontmatter(data), content


def _parse_yaml_safe(text: str) -> dict[str, Any] | None:
    """Safely parse YAML text, returning None on failure."""
    try:
        result = yaml.safe_load(text)
        return result if isinstance(result, dict) else None
    except yaml.YAMLError:
        return None


def _quote_problematic_values(text: str) -> str:
    """Auto-quote values containing special YAML characters (e.g., glob patterns)."""
    lines = []
    for line in text.split("\n"):
        if ":" in line and not line.strip().startswith("-"):
            key, _, value = line.partition(":")
            value = value.strip()
            if (
                value
                and any(c in value for c in YAML_SPECIAL_CHARS)
                and not (value.startswith('"') or value.startswith("'"))
            ):
                line = f'{key}: "{value}"'
        lines.append(line)
    return "\n".join(lines)


def _data_to_frontmatter(data: dict[str, Any]) -> SkillFrontmatter:
    """Convert raw YAML dict to SkillFrontmatter dataclass."""
    from iac_code.i18n import get_current_language

    fm = SkillFrontmatter()
    fm.name = data.get("name", "")
    fm.description = data.get("description", "")

    # Support localized descriptions via "descriptions" dict
    descriptions = data.get("descriptions")
    if isinstance(descriptions, dict):
        fm.descriptions = {str(k): str(v) for k, v in descriptions.items()}
        lang = get_current_language()
        if lang in fm.descriptions:
            fm.description = fm.descriptions[lang]

    # allowed_tools: supports str or list[str]
    tools = data.get("allowed_tools", [])
    fm.allowed_tools = [tools] if isinstance(tools, str) else list(tools or [])

    fm.when_to_use = data.get("when_to_use", "")
    fm.argument_hint = data.get("argument_hint", "")
    fm.arguments = list(data.get("arguments", []))
    fm.user_invocable = data.get("user_invocable", True)
    fm.model = data.get("model", "inherit")
    fm.effort = data.get("effort", "")
    fm.context = data.get("context", "inline")
    fm.agent = data.get("agent", "general-purpose")
    fm.paths = list(data.get("paths", []))
    auto_trigger = data.get("auto_trigger", {})
    if isinstance(auto_trigger, dict):
        fm.auto_trigger = {str(k): str(v) for k, v in auto_trigger.items() if v is not None}

    return fm
