"""Command suggestion provider."""

from __future__ import annotations

from typing import Any

from iac_code.commands.registry import CommandRegistry, LocalCommand
from iac_code.i18n import _
from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem, SuggestionProvider

MEMORY_FOLDER_COMMAND = "memory-folder"


class CommandProvider(SuggestionProvider):
    """Provides slash-command suggestions from a CommandRegistry."""

    trigger = "/"

    def __init__(self, registry: CommandRegistry, memory_manager: Any | None = None) -> None:
        self._registry = registry
        self._memory_manager = memory_manager

    def provide(self, token: CompletionToken) -> list[SuggestionItem]:
        """Return suggestions for the given completion token."""
        # Strip the leading "/" to get the query
        query = token.text[1:] if token.text.startswith("/") else token.text

        if self._is_memory_argument_query(query):
            return self._memory_argument_suggestions(query)
        if query.startswith("memory") and len(query) > len("memory") and query[len("memory")].isspace():
            return []

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

    @staticmethod
    def _is_memory_argument_query(query: str) -> bool:
        return (
            query.startswith(MEMORY_FOLDER_COMMAND)
            and len(query) > len(MEMORY_FOLDER_COMMAND)
            and query[len(MEMORY_FOLDER_COMMAND)].isspace()
        )

    def _memory_argument_suggestions(self, query: str) -> list[SuggestionItem]:
        arg_text = query[len(MEMORY_FOLDER_COMMAND) :].lstrip()
        has_trailing_space = bool(arg_text) and arg_text[-1].isspace()
        parts = arg_text.split()

        if not parts:
            return self._memory_first_argument_suggestions("")

        action = parts[0].lower()
        if action == "delete" and (has_trailing_space or len(parts) > 1):
            prefix = parts[1] if len(parts) > 1 else ""
            return self._memory_name_suggestions(prefix, command_prefix="/memory-folder delete ")

        if action == "search" and has_trailing_space:
            return []

        if len(parts) == 1 and not has_trailing_space:
            return self._memory_first_argument_suggestions(parts[0])

        return []

    def _memory_first_argument_suggestions(self, prefix: str) -> list[SuggestionItem]:
        suggestions = [
            self._memory_action_item("search", _("Search saved memories"), "/memory-folder search ", prefix),
            self._memory_action_item("delete", _("Delete a saved memory"), "/memory-folder delete ", prefix),
            self._memory_action_item("help", _("Show memory command help"), "/memory-folder help", prefix),
        ]
        suggestions.extend(self._memory_name_suggestions(prefix, command_prefix="/memory-folder "))
        return [item for item in suggestions if item is not None]

    def _memory_action_item(
        self,
        name: str,
        description: str,
        completion: str,
        prefix: str,
    ) -> SuggestionItem | None:
        if not self._matches_prefix(name, prefix):
            return None
        return SuggestionItem(
            id=f"cmd:memory:{name}",
            display_text=name,
            completion=completion,
            description=description,
            icon="/",
            source="command",
            score=1000.0 - len(name),
        )

    def _memory_name_suggestions(self, prefix: str, *, command_prefix: str) -> list[SuggestionItem]:
        items: list[SuggestionItem] = []
        for memory in self._memory_entries():
            name = str(memory.get("name", ""))
            if not name or not self._matches_prefix(name, prefix):
                continue
            items.append(
                SuggestionItem(
                    id=f"cmd:memory:{name}",
                    display_text=name,
                    completion=f"{command_prefix}{name}",
                    description=str(memory.get("description") or _("Saved memory")),
                    icon="/",
                    source="command",
                    score=500.0 - len(name),
                )
            )
        return sorted(items, key=lambda item: item.display_text)

    def _memory_entries(self) -> list[dict[str, Any]]:
        if self._memory_manager is None:
            return []
        try:
            memories = self._memory_manager.list_memories()
        except (OSError, ValueError):
            return []
        return [memory for memory in memories if isinstance(memory, dict)]

    @staticmethod
    def _matches_prefix(value: str, prefix: str) -> bool:
        return value.casefold().startswith(prefix.casefold())
