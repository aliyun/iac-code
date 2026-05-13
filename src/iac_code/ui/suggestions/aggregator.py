"""Suggestion aggregator that coordinates multiple providers."""

from __future__ import annotations

from iac_code.ui.suggestions.token_extractor import TokenExtractor
from iac_code.ui.suggestions.types import SuggestionItem, SuggestionProvider

OVERLAY_MAX_ITEMS = 5


class SuggestionAggregator:
    """Aggregates suggestions from multiple providers based on the current input."""

    def __init__(self, providers: list[SuggestionProvider]) -> None:
        self._providers = providers
        self._extractor = TokenExtractor()
        self._suggestions: list[SuggestionItem] = []
        self._selected_index: int = 0
        self._token_text: str = ""
        self._token_start: int = 0
        self._token_end: int = 0
        self._active: bool = False

    def update(self, text: str, cursor_pos: int) -> None:
        """Extract a token from text and dispatch to matching providers."""
        token = self._extractor.extract(text, cursor_pos)

        if token is None:
            self.dismiss()
            return

        # Find providers that handle this trigger
        matching = [p for p in self._providers if p.trigger == token.trigger]

        if not matching:
            self.dismiss()
            return

        # Collect suggestions from all matching providers
        all_items: list[SuggestionItem] = []
        for provider in matching:
            items = provider.provide(token)
            all_items.extend(items)

        # Sort by score descending
        all_items.sort(key=lambda i: i.score, reverse=True)
        self._suggestions = all_items
        self._selected_index = 0
        self._token_text = token.text
        self._token_start = token.start
        self._token_end = token.end
        self._active = True

    @property
    def suggestions(self) -> list[SuggestionItem]:
        """The full list of suggestions."""
        return self._suggestions

    @property
    def visible_suggestions(self) -> list[SuggestionItem]:
        """The visible window of suggestions (at most OVERLAY_MAX_ITEMS)."""
        if len(self._suggestions) <= OVERLAY_MAX_ITEMS:
            return self._suggestions
        start = self._visible_start
        return self._suggestions[start : start + OVERLAY_MAX_ITEMS]

    @property
    def visible_selected_index(self) -> int:
        """The selected index relative to the visible window."""
        return self._selected_index - self._visible_start

    @property
    def _visible_start(self) -> int:
        """Calculate the start of the visible window based on selected index."""
        n = len(self._suggestions)
        if n <= OVERLAY_MAX_ITEMS:
            return 0
        # Keep selected item within the visible window
        start = max(0, self._selected_index - OVERLAY_MAX_ITEMS + 1)
        start = min(start, n - OVERLAY_MAX_ITEMS)
        return start

    @property
    def has_more_above(self) -> bool:
        """Whether there are items above the visible window."""
        return self._visible_start > 0

    @property
    def has_more_below(self) -> bool:
        """Whether there are items below the visible window."""
        n = len(self._suggestions)
        return self._visible_start + OVERLAY_MAX_ITEMS < n

    @property
    def ghost_text(self) -> str:
        """Best match completion minus the typed portion.

        Returns the part of the top suggestion's completion that extends
        beyond the already-typed text. When the user has typed the full
        command name and the selected item declares an ``arg_hint``, the
        hint is appended as visual-only ghost text (Tab won't insert it).
        """
        if not self._suggestions:
            return ""

        selected = self._suggestions[self._selected_index]
        typed = self._token_text
        completion = selected.completion

        if not completion.lower().startswith(typed.lower()):
            return ""

        base = completion[len(typed) :]

        arg_hint = selected.arg_hint
        if arg_hint:
            name_only = completion.rstrip()
            if typed.lower() == name_only.lower():
                return base + arg_hint
        return base

    @property
    def selected_index(self) -> int:
        """The currently selected suggestion index."""
        return self._selected_index

    def move_selection(self, delta: int) -> None:
        """Move the selection by delta steps, wrapping around."""
        n = len(self._suggestions)
        if n == 0:
            return
        self._selected_index = (self._selected_index + delta) % n

    def accept_selected(self) -> tuple[str, int, int] | None:
        """Accept the currently selected suggestion.

        Returns (completion_text, token_start, token_end) or None if nothing selected.
        """
        if not self._suggestions or not self._active:
            return None

        item = self._suggestions[self._selected_index]
        result = (item.completion, self._token_start, self._token_end)
        self.dismiss()
        return result

    def accept_ghost_text(self) -> tuple[str, int, int] | None:
        """Accept the ghost text (top suggestion completion).

        Returns (completion_text, token_start, token_end) or None if no ghost text.
        """
        if not self._suggestions or not self._active:
            return None

        ghost = self.ghost_text
        if not ghost:
            return None

        item = self._suggestions[self._selected_index]
        result = (item.completion, self._token_start, self._token_end)
        self.dismiss()
        return result

    def dismiss(self) -> None:
        """Clear all suggestions and reset state."""
        self._suggestions = []
        self._selected_index = 0
        self._token_text = ""
        self._token_start = 0
        self._token_end = 0
        self._active = False
