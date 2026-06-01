"""Skill suggestion provider (``$`` trigger)."""

from __future__ import annotations

from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem, SuggestionProvider


class SkillProvider(SuggestionProvider):
    """Provides skill-only suggestions for the ``$`` trigger.

    Mirrors :class:`CommandProvider` but restricts results to skills
    (:class:`PromptCommand`), so ``$`` invokes skills exclusively while ``/``
    keeps listing both built-in commands and skills.
    """

    trigger = "$"

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    def provide(self, token: CompletionToken) -> list[SuggestionItem]:
        """Return skill suggestions for the given completion token."""
        # Strip the leading "$" to get the query
        query = token.text[1:] if token.text.startswith("$") else token.text

        matches = self._registry.fuzzy_search(query)

        items: list[SuggestionItem] = []
        for match in matches:
            cmd = match.command
            if not isinstance(cmd, PromptCommand):
                continue
            name = match.name
            items.append(
                SuggestionItem(
                    id=f"skill:{cmd.name}",
                    display_text=name,
                    completion=f"${name} ",
                    description=cmd.description,
                    icon="$",
                    source="skill",
                    score=float(-match.priority * 1000 - match.score),
                )
            )

        return items
