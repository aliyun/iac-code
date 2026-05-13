"""Tests for suggestion types."""

from __future__ import annotations

import pytest

from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem, SuggestionProvider


class TestCompletionToken:
    def test_frozen(self):
        token = CompletionToken(text="/mod", start=0, end=4, trigger="/")
        with pytest.raises((AttributeError, TypeError)):
            token.text = "other"  # type: ignore[misc]

    def test_fields(self):
        token = CompletionToken(text="@src/ui", start=5, end=12, trigger="@")
        assert token.text == "@src/ui"
        assert token.start == 5
        assert token.end == 12
        assert token.trigger == "@"

    def test_equality(self):
        t1 = CompletionToken(text="/help", start=0, end=5, trigger="/")
        t2 = CompletionToken(text="/help", start=0, end=5, trigger="/")
        assert t1 == t2


class TestSuggestionItem:
    def test_fields(self):
        item = SuggestionItem(
            id="cmd:model",
            display_text="model",
            completion="/model ",
            description="Show or switch model",
            icon="/",
            source="command",
            score=10.0,
        )
        assert item.id == "cmd:model"
        assert item.display_text == "model"
        assert item.completion == "/model "
        assert item.description == "Show or switch model"
        assert item.icon == "/"
        assert item.source == "command"
        assert item.score == 10.0

    def test_mutable(self):
        item = SuggestionItem(
            id="cmd:help",
            display_text="help",
            completion="/help ",
            description="Show help",
            icon="/",
            source="command",
            score=5.0,
        )
        item.score = 99.0
        assert item.score == 99.0


class TestSuggestionProvider:
    def test_abstract(self):
        """SuggestionProvider cannot be instantiated directly."""
        with pytest.raises(TypeError):
            SuggestionProvider()  # type: ignore[abstract]

    def test_concrete_implementation(self):
        """A concrete subclass with all methods implemented can be instantiated."""

        class ConcreteProvider(SuggestionProvider):
            @property
            def trigger(self) -> str:
                return "/"

            async def provide(self, token):
                return []

        provider = ConcreteProvider()
        assert provider.trigger == "/"
