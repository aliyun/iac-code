"""Command suggestion provider."""

from __future__ import annotations

from iac_code.commands.registry import CommandRegistry, LocalCommand
from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem, SuggestionProvider


class CommandProvider(SuggestionProvider):
    """Provides slash-command suggestions from a CommandRegistry."""

    trigger = "/"

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    def provide(self, token: CompletionToken) -> list[SuggestionItem]:
        """Return suggestions for the given completion token."""
        # Strip the leading "/" to get the query
        query = token.text[1:] if token.text.startswith("/") else token.text

        matches = self._registry.fuzzy_search(query)

        items: list[SuggestionItem] = []
        for match in matches:
            cmd = match.command
            name = match.name
            completion = f"/{name} "
            arg_hint = cmd.arg_hint if isinstance(cmd, LocalCommand) else None
            items.append(
                SuggestionItem(
                    id=f"cmd:{cmd.name}",
                    display_text=name,
                    completion=completion,
                    description=cmd.description,
                    icon="/",
                    source="command",
                    score=float(-match.priority * 1000 - match.score),
                    arg_hint=arg_hint,
                )
            )

        return items
