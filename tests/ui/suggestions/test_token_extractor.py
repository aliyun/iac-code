"""Tests for TokenExtractor."""

from __future__ import annotations

import pytest

from iac_code.ui.suggestions.token_extractor import TokenExtractor


@pytest.fixture
def extractor() -> TokenExtractor:
    return TokenExtractor()


class TestTokenExtractor:
    def test_slash_at_start(self, extractor):
        """/mod at line start → trigger='/'"""
        text = "/mod"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "/"
        assert token.text == "/mod"
        assert token.start == 0
        assert token.end == 4

    def test_slash_after_space(self, extractor):
        """'hello /mod' → trigger='/'"""
        text = "hello /mod"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "/"
        assert token.text == "/mod"

    def test_slash_inside_path_no_trigger(self, extractor):
        """'src/ui' has / inside path → no command trigger"""
        text = "src/ui"
        token = extractor.extract(text, len(text))
        # "/" is not at start of token, so no "/" trigger
        assert token is None

    def test_at_trigger(self, extractor):
        """'@src/ui' → trigger='@'"""
        text = "@src/ui"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "@"
        assert token.text == "@src/ui"

    def test_at_trigger_with_trailing_slash(self, extractor):
        """'@src/' → trigger='@'"""
        text = "@src/"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "@"
        assert token.text == "@src/"

    def test_at_trigger_mid_sentence(self, extractor):
        """'look at @config' → trigger='@'"""
        text = "look at @config"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "@"
        assert token.text == "@config"

    def test_bang_at_start(self, extractor):
        """'!git' at start → trigger='!'"""
        text = "!git"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "!"
        assert token.text == "!git"

    def test_bang_mid_input_no_trigger(self, extractor):
        """'hello!' in middle → no '!' trigger"""
        text = "hello!"
        token = extractor.extract(text, len(text))
        # '!' is not at start, so not a valid shell trigger
        assert token is None

    def test_empty_input(self, extractor):
        """Empty input → None"""
        token = extractor.extract("", 0)
        assert token is None

    def test_slash_alone(self, extractor):
        """'/' alone → trigger='/'"""
        text = "/"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "/"
        assert token.text == "/"

    def test_cursor_at_zero(self, extractor):
        """cursor at position 0 → None"""
        token = extractor.extract("/mod", 0)
        assert token is None

    def test_slash_after_tab(self, extractor):
        """Tab before slash → still a valid command trigger"""
        text = "\t/cmd"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "/"

    def test_token_start_and_end_positions(self, extractor):
        """Verify start and end positions are correct."""
        text = "look at @config"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.start == 8
        assert token.end == 15
        assert text[token.start : token.end] == "@config"

    def test_at_only(self, extractor):
        """'@' alone → trigger='@'"""
        text = "@"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "@"
        assert token.text == "@"

    def test_bang_only_at_start(self, extractor):
        """'!' alone at start → trigger='!'"""
        text = "!"
        token = extractor.extract(text, len(text))
        assert token is not None
        assert token.trigger == "!"
        assert token.text == "!"
