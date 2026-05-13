"""Tests for the TokenCounter service."""

import pytest

from iac_code.services.token_counter import TokenCounter


@pytest.fixture
def counter():
    """Create a TokenCounter instance."""
    return TokenCounter()


class TestTokenCounter:
    """Tests for TokenCounter."""

    def test_count_text_returns_positive_int_for_non_empty_string(self, counter):
        """count_text returns int > 0 for non-empty string."""
        result = counter.count_text("Hello, world!")
        assert isinstance(result, int)
        assert result > 0

    def test_count_text_returns_zero_for_empty_string(self, counter):
        """count_text returns 0 for empty string."""
        result = counter.count_text("")
        assert result == 0

    def test_count_message_with_user_message_string_content(self, counter):
        """count_message with user message (string content) returns > 0."""
        message = {"role": "user", "content": "What is the weather today?"}
        result = counter.count_message(message)
        assert isinstance(result, int)
        assert result > 0

    def test_count_message_with_assistant_tool_use_blocks(self, counter):
        """count_message with assistant message containing tool_use blocks returns > 0."""
        message = {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_123",
                    "name": "read_file",
                    "input": {"path": "/some/file.txt"},
                },
            ],
        }
        result = counter.count_message(message)
        assert isinstance(result, int)
        assert result > 0

    def test_count_messages_with_list_of_messages(self, counter):
        """count_messages with list of messages returns > 0."""
        messages = [
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there, how can I help?"},
        ]
        result = counter.count_messages(messages)
        assert isinstance(result, int)
        assert result > 0

    def test_count_text_on_long_text_returns_more_than_100(self, counter):
        """count_text on long text returns > 100."""
        long_text = "word " * 200  # 200 repetitions of "word ", well over 100 tokens
        result = counter.count_text(long_text)
        assert isinstance(result, int)
        assert result > 100
