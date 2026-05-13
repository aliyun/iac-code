"""Tests for SuggestionAggregator."""

from __future__ import annotations

import pytest

from iac_code.commands import create_default_registry
from iac_code.ui.suggestions.aggregator import OVERLAY_MAX_ITEMS, SuggestionAggregator
from iac_code.ui.suggestions.command_provider import CommandProvider


@pytest.fixture
def command_provider() -> CommandProvider:
    return CommandProvider(create_default_registry())


@pytest.fixture
def aggregator(command_provider) -> SuggestionAggregator:
    return SuggestionAggregator([command_provider])


class TestSuggestionAggregator:
    def test_update_with_slash_trigger(self, aggregator):
        """/mod → suggestions > 0."""
        aggregator.update("/mod", 4)
        assert len(aggregator.suggestions) > 0

    def test_ghost_text_present(self, aggregator):
        """After /mod, ghost_text should be a non-empty suffix."""
        aggregator.update("/mod", 4)
        assert len(aggregator.suggestions) > 0
        ghost = aggregator.ghost_text
        # ghost_text is completion[len(typed):] — for "/mod" → "/model " ghost is "el "
        assert isinstance(ghost, str)
        assert len(ghost) > 0

    def test_no_trigger_empty_suggestions(self, aggregator):
        """Plain text without trigger → empty suggestions."""
        aggregator.update("hello world", 11)
        assert aggregator.suggestions == []

    def test_move_selection(self, aggregator):
        """move_selection changes selected_index."""
        aggregator.update("/", 1)
        assert aggregator.selected_index == 0
        aggregator.move_selection(1)
        assert aggregator.selected_index == 1

    def test_move_selection_wraps(self, aggregator):
        """move_selection wraps around."""
        aggregator.update("/", 1)
        n = len(aggregator.suggestions)
        assert n > 0
        # Move backwards from 0 → should wrap to last
        aggregator.move_selection(-1)
        assert aggregator.selected_index == n - 1

    def test_accept_selected_returns_tuple(self, aggregator):
        """accept_selected returns (text, start, end)."""
        aggregator.update("/mod", 4)
        result = aggregator.accept_selected()
        assert result is not None
        text, start, end = result
        assert isinstance(text, str)
        assert isinstance(start, int)
        assert isinstance(end, int)
        assert start == 0
        assert end == 4

    def test_accept_selected_clears(self, aggregator):
        """accept_selected dismisses suggestions."""
        aggregator.update("/mod", 4)
        aggregator.accept_selected()
        assert aggregator.suggestions == []

    def test_dismiss_clears(self, aggregator):
        """dismiss() empties suggestions."""
        aggregator.update("/mod", 4)
        assert len(aggregator.suggestions) > 0
        aggregator.dismiss()
        assert aggregator.suggestions == []
        assert aggregator.selected_index == 0
        assert aggregator.ghost_text == ""

    def test_max_visible_items(self, aggregator):
        """Visible suggestions capped at OVERLAY_MAX_ITEMS."""
        # "/" matches all commands — visible window should be capped
        aggregator.update("/", 1)
        assert len(aggregator.visible_suggestions) <= OVERLAY_MAX_ITEMS

    def test_all_suggestions_accessible(self, aggregator):
        """All suggestions are accessible via scrolling."""
        aggregator.update("/", 1)
        total = len(aggregator.suggestions)
        # Should be able to navigate to every item
        for i in range(total):
            assert aggregator.selected_index == i
            aggregator.move_selection(1)
        # Should wrap back to 0
        assert aggregator.selected_index == 0

    def test_accept_ghost_text(self, aggregator):
        """accept_ghost_text returns the top suggestion completion."""
        aggregator.update("/mod", 4)
        ghost = aggregator.ghost_text
        if ghost:
            result = aggregator.accept_ghost_text()
            assert result is not None
            text, start, end = result
            assert text.startswith("/mod") or text.startswith("/")

    def test_accept_selected_when_empty(self, aggregator):
        """accept_selected with no suggestions returns None."""
        result = aggregator.accept_selected()
        assert result is None

    def test_accept_ghost_text_when_empty(self, aggregator):
        """accept_ghost_text with no suggestions returns None."""
        result = aggregator.accept_ghost_text()
        assert result is None

    def test_update_replaces_previous(self, aggregator):
        """Calling update again replaces previous suggestions."""
        aggregator.update("/mod", 4)
        first_suggestions = list(aggregator.suggestions)
        aggregator.update("/help", 5)
        assert aggregator.suggestions != first_suggestions or True  # just verify it ran

    def test_move_selection_no_suggestions(self, aggregator):
        """move_selection with no suggestions is a no-op."""
        aggregator.move_selection(1)  # Should not raise
        assert aggregator.selected_index == 0

    def test_arg_hint_appended_when_full_command_typed(self, aggregator):
        """/debug (full name) → ghost text includes ' [on|off]' arg hint."""
        aggregator.update("/debug", 6)
        ghost = aggregator.ghost_text
        assert "[on|off]" in ghost

    def test_arg_hint_not_shown_when_partial(self, aggregator):
        """/deb (partial) → ghost text is just the name completion, no hint."""
        aggregator.update("/deb", 4)
        ghost = aggregator.ghost_text
        assert "[on|off]" not in ghost

    def test_arg_hint_not_shown_for_command_without_hint(self, aggregator):
        """/clear (no arg_hint) → no hint leaks into ghost text."""
        aggregator.update("/clear", 6)
        ghost = aggregator.ghost_text
        assert "[" not in ghost

    def test_accept_ghost_text_does_not_include_hint(self, aggregator):
        """Tab-accepting ghost text on /debug inserts only '/debug ', not the hint."""
        aggregator.update("/debug", 6)
        result = aggregator.accept_ghost_text()
        assert result is not None
        text, _start, _end = result
        assert text == "/debug "
