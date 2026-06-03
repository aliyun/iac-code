"""Tests for CommandProvider."""

from __future__ import annotations

import pytest

from iac_code.commands import create_default_registry
from iac_code.commands.registry import CommandRegistry
from iac_code.ui.suggestions.command_provider import CommandProvider
from iac_code.ui.suggestions.types import CompletionToken


@pytest.fixture
def registry() -> CommandRegistry:
    return create_default_registry()


@pytest.fixture
def provider(registry) -> CommandProvider:
    return CommandProvider(registry)


class _MemoryManager:
    def list_memories(self):
        return [
            {"name": "user-role", "description": "Role", "type": "user", "content": "Senior engineer"},
            {
                "name": "feedback-testing",
                "description": "Testing",
                "type": "feedback",
                "content": "Prefer integration tests",
            },
        ]


@pytest.fixture
def memory_provider(registry) -> CommandProvider:
    return CommandProvider(registry, memory_manager=_MemoryManager())


def make_token(text: str, start: int = 0) -> CompletionToken:
    return CompletionToken(text=text, start=start, end=start + len(text), trigger="/")


class TestCommandProvider:
    def test_trigger(self, provider):
        assert provider.trigger == "/"

    def test_empty_query_returns_all(self, provider):
        """Just '/' → returns all visible commands."""
        token = make_token("/")
        items = provider.provide(token)
        assert len(items) > 0
        # All default commands should appear
        names = {item.display_text for item in items}
        assert "help" in names
        assert "model" in names
        assert "clear" in names

    def test_partial_match_model(self, provider):
        """/mod → results contain 'model'"""
        token = make_token("/mod")
        items = provider.provide(token)
        assert len(items) > 0
        names = [item.display_text for item in items]
        assert "model" in names

    def test_exact_match_help(self, provider):
        """/help → completion includes '/help '"""
        token = make_token("/help")
        items = provider.provide(token)
        assert len(items) > 0
        # Best match should be "help"
        help_items = [i for i in items if i.display_text == "help"]
        assert len(help_items) == 1
        assert help_items[0].completion == "/help "

    def test_no_match(self, provider):
        """/xyzabc → empty results"""
        token = make_token("/xyzabc")
        items = provider.provide(token)
        assert items == []

    def test_source_and_icon(self, provider):
        """All items have source='command' and icon='/'."""
        token = make_token("/")
        items = provider.provide(token)
        for item in items:
            assert item.source == "command"
            assert item.icon == "/"

    def test_id_format(self, provider):
        """Item ids should be prefixed with 'cmd:'."""
        token = make_token("/help")
        items = provider.provide(token)
        for item in items:
            assert item.id.startswith("cmd:")

    def test_partial_match_mid_sentence(self, provider):
        """/cl → clears matches"""
        token = make_token("/cl", start=6)
        items = provider.provide(token)
        assert len(items) > 0
        names = [item.display_text for item in items]
        assert "clear" in names

    def test_arg_hint_propagated(self, provider):
        """Command registered with arg_hint → SuggestionItem exposes it."""
        token = make_token("/debug")
        items = provider.provide(token)
        debug_items = [i for i in items if i.display_text == "debug"]
        assert len(debug_items) == 1
        assert debug_items[0].arg_hint == "[on|off]"

    def test_arg_hint_absent_for_commands_without_one(self, provider):
        """Command without arg_hint → SuggestionItem.arg_hint is None."""
        token = make_token("/clear")
        items = provider.provide(token)
        clear_items = [i for i in items if i.display_text == "clear"]
        assert len(clear_items) == 1
        assert clear_items[0].arg_hint is None

    def test_memory_second_item_suggests_actions_and_memory_names(self, memory_provider):
        """/memory<space> → subcommands plus saved memory names."""
        token = make_token("/memory ")
        items = memory_provider.provide(token)

        names = {item.display_text for item in items}
        assert {"search", "delete", "help", "user-role", "feedback-testing"}.issubset(names)
        assert [item.completion for item in items if item.display_text == "search"] == ["/memory search "]
        assert [item.completion for item in items if item.display_text == "user-role"] == ["/memory user-role"]

    def test_memory_second_item_filters_action_prefix(self, memory_provider):
        """/memory d → delete action suggestion."""
        token = make_token("/memory d")
        items = memory_provider.provide(token)

        assert [item.display_text for item in items] == ["delete"]
        assert items[0].completion == "/memory delete "

    def test_memory_delete_suggests_memory_names(self, memory_provider):
        """/memory delete<space> → saved memory name suggestions."""
        token = make_token("/memory delete ")
        items = memory_provider.provide(token)

        names = [item.display_text for item in items]
        assert names == ["feedback-testing", "user-role"]
        assert items[0].completion == "/memory delete feedback-testing"
        assert all(item.id.startswith("cmd:memory:") for item in items)

    def test_memory_search_query_has_no_argument_suggestions(self, memory_provider):
        """/memory search<space> leaves free-form search input alone."""
        token = make_token("/memory search ")
        assert memory_provider.provide(token) == []
