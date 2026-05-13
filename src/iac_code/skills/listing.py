"""Skill listing generation for system prompt injection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from iac_code.types.skill_source import SkillSource

if TYPE_CHECKING:
    from iac_code.commands.registry import PromptCommand

SKILL_BUDGET_CONTEXT_PERCENT = 0.01
MAX_LISTING_DESC_CHARS = 250
DEFAULT_CHAR_BUDGET = 8_000  # 200K * 4 chars/token * 1%


def get_char_budget(context_window_tokens: int | None = None) -> int:
    if context_window_tokens:
        return int(context_window_tokens * 4 * SKILL_BUDGET_CONTEXT_PERCENT)
    return DEFAULT_CHAR_BUDGET


def build_skill_listing(
    skills: list[PromptCommand],
    context_window_tokens: int | None = None,
) -> str:
    """Build the skill listing string for injection into the system prompt.

    Truncation strategy:
    1. Try full description + when_to_use for all skills
    2. If over budget: bundled skills keep full description; others truncated proportionally
    3. Extreme case: other skills show name only
    """
    if not skills:
        return ""

    budget = get_char_budget(context_window_tokens)

    # Separate bundled and other skills
    bundled = [s for s in skills if s.source == SkillSource.BUNDLED]
    others = [s for s in skills if s.source != SkillSource.BUNDLED]

    # Try full descriptions
    lines = _format_full(bundled + others)
    total = sum(len(line) for line in lines)

    if total <= budget:
        return _assemble(lines)

    # Over budget: bundled keep full, others get proportional budget
    bundled_lines = _format_full(bundled)
    bundled_cost = sum(len(line) for line in bundled_lines)
    remaining_budget = budget - bundled_cost

    if remaining_budget <= 0:
        other_lines = [f"- {s.name}" for s in others]
    else:
        per_skill_budget = remaining_budget // max(len(others), 1)
        other_lines = _format_truncated(others, per_skill_budget)

    return _assemble(bundled_lines + other_lines)


def _format_full(skills: list[PromptCommand]) -> list[str]:
    lines = []
    for s in skills:
        desc = s.description
        if s.when_to_use:
            desc += f"\n{s.when_to_use}"
        if len(desc) > MAX_LISTING_DESC_CHARS:
            desc = desc[: MAX_LISTING_DESC_CHARS - 3] + "..."
        lines.append(f"- {s.name}: {desc}")
    return lines


def _format_truncated(skills: list[PromptCommand], per_skill_budget: int) -> list[str]:
    lines = []
    for s in skills:
        desc = s.description
        max_desc = per_skill_budget - len(s.name) - 4  # "- name: "
        if max_desc <= 0:
            lines.append(f"- {s.name}")
        elif len(desc) > max_desc:
            lines.append(f"- {s.name}: {desc[: max_desc - 3]}...")
        else:
            lines.append(f"- {s.name}: {desc}")
    return lines


def _assemble(lines: list[str]) -> str:
    header = "The following skills are available for use with the Skill tool:\n"
    return header + "\n".join(lines)
