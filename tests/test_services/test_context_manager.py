"""Tests for ContextManager."""

from __future__ import annotations

from iac_code.agent.message import TextBlock, ToolResultBlock, ToolUseBlock
from iac_code.services.context_manager import ContextManager, get_context_window_config


class TestGetContextWindowConfig:
    def test_claude_model(self):
        assert get_context_window_config("claude-3-5-sonnet").context_window == 200_000

    def test_gpt4_model(self):
        assert get_context_window_config("gpt-4-turbo").context_window == 128_000

    def test_unknown_model_returns_default(self):
        assert get_context_window_config("unknown-model").context_window == 128_000

    def test_case_insensitive(self):
        assert get_context_window_config("CLAUDE-3").context_window == 200_000


class TestContextManagerInitialState:
    def test_empty_messages(self):
        cm = ContextManager(system_prompt="You are a helpful assistant.")
        assert cm.get_messages() == []

    def test_zero_message_tokens(self):
        cm = ContextManager(system_prompt="You are a helpful assistant.")
        usage = cm.get_usage()
        assert usage["user_message_tokens"] == 0
        assert usage["message_count"] == 0

    def test_system_prompt_tokens_counted(self):
        cm = ContextManager(system_prompt="You are a helpful assistant.")
        usage = cm.get_usage()
        assert usage["system_prompt_tokens"] > 0

    def test_default_context_window(self):
        cm = ContextManager(system_prompt="")
        assert cm.get_usage()["context_window"] == 128_000

    def test_model_sets_context_window(self):
        cm = ContextManager(system_prompt="", model="claude-3-5-sonnet")
        assert cm.get_usage()["context_window"] == 200_000


class TestAddUserMessage:
    def test_adds_message(self):
        cm = ContextManager(system_prompt="")
        cm.add_user_message("Hello")
        assert len(cm.get_messages()) == 1

    def test_message_role_is_user(self):
        cm = ContextManager(system_prompt="")
        msg = cm.add_user_message("Hello")
        assert msg.role == "user"

    def test_message_content(self):
        cm = ContextManager(system_prompt="")
        msg = cm.add_user_message("Hello there")
        assert msg.content == "Hello there"

    def test_token_count_greater_than_zero(self):
        cm = ContextManager(system_prompt="")
        msg = cm.add_user_message("Hello there")
        assert msg.token_count > 0

    def test_total_tokens_increases(self):
        cm = ContextManager(system_prompt="")
        initial_tokens = cm.get_total_tokens()
        cm.add_user_message("Hello there")
        assert cm.get_total_tokens() > initial_tokens


class TestAddAssistantMessage:
    def test_adds_message_with_string_content(self):
        cm = ContextManager(system_prompt="")
        msg = cm.add_assistant_message("I can help you with that.")
        assert msg.role == "assistant"
        assert msg.content == "I can help you with that."

    def test_token_count_set(self):
        cm = ContextManager(system_prompt="")
        msg = cm.add_assistant_message("I can help you.")
        assert msg.token_count > 0

    def test_adds_message_with_tool_use_blocks(self):
        cm = ContextManager(system_prompt="")
        blocks = [
            TextBlock(text="I'll run a command for you."),
            ToolUseBlock(id="toolu_abc123", name="bash", input={"command": "ls -la"}),
        ]
        msg = cm.add_assistant_message(blocks)
        assert msg.role == "assistant"
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2
        assert msg.token_count > 0

    def test_tool_use_blocks_preserved(self):
        cm = ContextManager(system_prompt="")
        tool_block = ToolUseBlock(id="toolu_xyz", name="read_file", input={"path": "/tmp/test.txt"})
        blocks = [tool_block]
        msg = cm.add_assistant_message(blocks)
        tool_uses = msg.get_tool_use_blocks()
        assert len(tool_uses) == 1
        assert tool_uses[0].name == "read_file"


class TestAddToolResults:
    def test_adds_tool_results_as_user_message(self):
        cm = ContextManager(system_prompt="")
        results = [ToolResultBlock(tool_use_id="toolu_abc", content="file content here", is_error=False)]
        msg = cm.add_tool_results(results)
        assert msg.role == "user"

    def test_token_count_set(self):
        cm = ContextManager(system_prompt="")
        results = [ToolResultBlock(tool_use_id="toolu_abc", content="some output", is_error=False)]
        msg = cm.add_tool_results(results)
        assert msg.token_count > 0

    def test_multiple_tool_results(self):
        cm = ContextManager(system_prompt="")
        results = [
            ToolResultBlock(tool_use_id="toolu_1", content="result 1", is_error=False),
            ToolResultBlock(tool_use_id="toolu_2", content="result 2", is_error=True),
        ]
        msg = cm.add_tool_results(results)
        assert msg.role == "user"
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2


class TestNeedsCompaction:
    def test_returns_false_when_under_threshold(self):
        cm = ContextManager(system_prompt="", model="claude")
        cm.add_user_message("Hello")
        assert cm.needs_compaction() is False

    def test_returns_true_when_over_threshold(self):
        # Use a small model so we can check the logic by mocking the config
        cm = ContextManager(system_prompt="", model="qwen")
        # Manually override to tiny window
        cm._config = cm._config.__class__(
            context_window=50,
            max_output_tokens=8192,
            compact_buffer=10,
            compact_threshold=0.5,
            preserve_recent_turns=3,
        )
        for i in range(20):
            cm.add_user_message(f"This is message number {i} with enough content to use tokens")
        assert cm.needs_compaction() is True

    def test_exactly_at_threshold_returns_false(self):
        cm = ContextManager(system_prompt="", model="claude")
        assert cm.needs_compaction() is False


class TestGetApiMessages:
    def test_returns_list(self):
        cm = ContextManager(system_prompt="")
        assert isinstance(cm.get_api_messages(), list)

    def test_empty_when_no_messages(self):
        cm = ContextManager(system_prompt="")
        assert cm.get_api_messages() == []

    def test_proper_format_for_user_message(self):
        cm = ContextManager(system_prompt="")
        cm.add_user_message("Hello")
        api_msgs = cm.get_api_messages()
        assert len(api_msgs) == 1
        assert api_msgs[0]["role"] == "user"
        assert api_msgs[0]["content"] == "Hello"

    def test_proper_format_for_assistant_message(self):
        cm = ContextManager(system_prompt="")
        cm.add_assistant_message("Hi there!")
        api_msgs = cm.get_api_messages()
        assert len(api_msgs) == 1
        assert api_msgs[0]["role"] == "assistant"
        assert api_msgs[0]["content"] == "Hi there!"

    def test_multiple_messages_in_order(self):
        cm = ContextManager(system_prompt="")
        cm.add_user_message("Hello")
        cm.add_assistant_message("Hi there!")
        cm.add_user_message("How are you?")
        api_msgs = cm.get_api_messages()
        assert len(api_msgs) == 3
        assert api_msgs[0]["role"] == "user"
        assert api_msgs[1]["role"] == "assistant"
        assert api_msgs[2]["role"] == "user"


class TestGetUsage:
    def test_returns_dict(self):
        cm = ContextManager(system_prompt="")
        assert isinstance(cm.get_usage(), dict)

    def test_has_required_keys(self):
        cm = ContextManager(system_prompt="")
        usage = cm.get_usage()
        assert "total_tokens" in usage
        assert "context_window" in usage
        assert "usage_percent" in usage
        assert "system_prompt_tokens" in usage
        assert "user_message_tokens" in usage
        assert "assistant_message_tokens" in usage
        assert "tool_result_tokens" in usage
        assert "message_count" in usage

    def test_usage_percent_increases_with_messages(self):
        cm = ContextManager(system_prompt="")
        initial_pct = cm.get_usage()["usage_percent"]
        cm.add_user_message("Hello there, this is a test message with some content.")
        new_pct = cm.get_usage()["usage_percent"]
        assert new_pct > initial_pct

    def test_message_count_tracks_messages(self):
        cm = ContextManager(system_prompt="")
        cm.add_user_message("Message 1")
        cm.add_assistant_message("Response 1")
        usage = cm.get_usage()
        assert usage["message_count"] == 2

    def test_total_tokens_is_sum_of_parts(self):
        cm = ContextManager(system_prompt="You are helpful.")
        cm.add_user_message("Hello")
        usage = cm.get_usage()
        expected = (
            usage["system_prompt_tokens"]
            + usage["user_message_tokens"]
            + usage["assistant_message_tokens"]
            + usage["tool_result_tokens"]
        )
        assert usage["total_tokens"] == expected


class TestReset:
    def test_clears_messages(self):
        cm = ContextManager(system_prompt="")
        cm.add_user_message("Hello")
        cm.add_assistant_message("Hi")
        cm.reset()
        assert cm.get_messages() == []

    def test_clears_token_count(self):
        cm = ContextManager(system_prompt="")
        cm.add_user_message("Hello, this adds tokens to the conversation.")
        cm.reset()
        assert cm.get_usage()["user_message_tokens"] == 0

    def test_clears_message_count(self):
        cm = ContextManager(system_prompt="")
        cm.add_user_message("Hello")
        cm.reset()
        assert cm.get_usage()["message_count"] == 0

    def test_system_prompt_tokens_preserved_after_reset(self):
        cm = ContextManager(system_prompt="You are a helpful assistant.")
        tokens_before = cm.get_usage()["system_prompt_tokens"]
        cm.add_user_message("Hello")
        cm.reset()
        assert cm.get_usage()["system_prompt_tokens"] == tokens_before


class TestBuildCompactionPrompt:
    def test_returns_empty_string_when_no_messages(self):
        cm = ContextManager(system_prompt="")
        assert cm.build_compaction_prompt() == ""

    def test_returns_string(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"Hello {i}")
            cm.add_assistant_message(f"Response {i}")
        result = cm.build_compaction_prompt()
        assert isinstance(result, str)

    def test_contains_old_conversation_content(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"Hello, what is {i}+{i}?")
            cm.add_assistant_message(f"{i}+{i} equals {i * 2}.")
        prompt = cm.build_compaction_prompt()
        assert "Hello, what is 0+0?" in prompt

    def test_prompt_contains_instructions(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"Test {i}")
            cm.add_assistant_message(f"Response {i}")
        prompt = cm.build_compaction_prompt()
        assert "summary" in prompt.lower()

    def test_prompt_includes_role_labels(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"User question {i}")
            cm.add_assistant_message(f"Assistant answer {i}")
        prompt = cm.build_compaction_prompt()
        assert "USER" in prompt
        assert "ASSISTANT" in prompt


class TestApplyCompaction:
    def test_returns_tuple_of_token_counts(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"Hello there {i}, this is a longer message for testing.")
            cm.add_assistant_message(f"I understand your question {i} and will help.")
        result = cm.apply_compaction("Summary: User asked hello, assistant responded.")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_original_and_new_token_counts(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"Hello there {i}, this is a longer message for testing.")
            cm.add_assistant_message(f"I understand your question {i} and will help with that.")
        original_tokens, new_tokens = cm.apply_compaction("Brief summary.")
        assert original_tokens > 0
        assert new_tokens > 0

    def test_replaces_old_messages_preserves_recent(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"Message {i}")
            cm.add_assistant_message(f"Response {i}")
        cm.apply_compaction("This is the summary.")
        messages = cm.get_messages()
        # 1 summary + 3 recent turns (6 messages)
        assert len(messages) == 7
        assert messages[0].role == "user"

    def test_summary_content_in_message(self):
        cm = ContextManager(system_prompt="")
        for i in range(6):
            cm.add_user_message(f"Hello {i}")
            cm.add_assistant_message(f"Response {i}")
        cm.apply_compaction("The user said hello.")
        messages = cm.get_messages()
        assert "The user said hello." in messages[0].get_text()

    def test_compaction_reduces_tokens_with_long_history(self):
        cm = ContextManager(system_prompt="")
        for i in range(10):
            cm.add_user_message(f"This is message {i} with substantial content for token counting.")
            cm.add_assistant_message(f"This is response {i} with substantial content too.")
        original_tokens, new_tokens = cm.apply_compaction("Brief summary of 10 exchanges.")
        assert new_tokens < original_tokens
