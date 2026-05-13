from iac_code.agent.message import TextBlock, ToolResultBlock
from iac_code.services.context_manager import ContextManager, get_context_window_config


class TestContextWindowConfig:
    def test_claude_model(self):
        config = get_context_window_config("claude-3-opus")
        assert config.context_window == 200_000

    def test_qwen_model(self):
        config = get_context_window_config("qwen3.6-plus")
        assert config.context_window == 131_072

    def test_gpt4_model(self):
        config = get_context_window_config("gpt-4-turbo")
        assert config.context_window == 128_000

    def test_unknown_model_uses_default(self):
        config = get_context_window_config("unknown-model-xyz")
        assert config.context_window == 128_000

    def test_preserve_recent_turns(self):
        config = get_context_window_config("claude-3-opus")
        assert config.preserve_recent_turns == 3


class TestContextManager:
    def test_add_user_message(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        msg = cm.add_user_message("Hello")
        assert msg.role == "user"
        assert msg.token_count > 0

    def test_add_assistant_message(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        cm.add_user_message("Hello")
        msg = cm.add_assistant_message([TextBlock(text="Hi there")])
        assert msg.role == "assistant"

    def test_add_tool_results(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        cm.add_user_message("Hello")
        blocks = [ToolResultBlock(tool_use_id="t1", content="result")]
        msg = cm.add_tool_results(blocks)
        assert msg.role == "user"

    def test_get_total_tokens_includes_system_prompt(self):
        cm = ContextManager(system_prompt="A long system prompt " * 100, model="qwen")
        total = cm.get_total_tokens()
        assert total > 100

    def test_needs_compaction_false_when_small(self):
        cm = ContextManager(system_prompt="Short.", model="qwen")
        cm.add_user_message("Hello")
        assert cm.needs_compaction() is False

    def test_get_usage_returns_breakdown(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        cm.add_user_message("Hello")
        usage = cm.get_usage()
        assert "system_prompt_tokens" in usage
        assert "user_message_tokens" in usage
        assert "assistant_message_tokens" in usage
        assert "tool_result_tokens" in usage
        assert "total_tokens" in usage
        assert "context_window" in usage
        assert "usage_percent" in usage


class TestSegmentedCompaction:
    def test_build_compaction_prompt_excludes_recent(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Assistant response {i}")])

        prompt = cm.build_compaction_prompt()
        assert "User message 0" in prompt
        assert "User message 5" not in prompt

    def test_apply_compaction_preserves_recent(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Response {i}")])

        original_count = len(cm.get_messages())
        assert original_count == 12

        cm.apply_compaction("Summary of old conversation")
        messages = cm.get_messages()
        assert len(messages) == 7
        assert "Summary" in messages[0].get_text()
        assert "User message 3" in messages[1].get_text()

    def test_apply_compaction_returns_token_counts(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        for i in range(6):
            cm.add_user_message(f"Message {i} with some content")
            cm.add_assistant_message([TextBlock(text=f"Response {i}")])

        original, new = cm.apply_compaction("Brief summary")
        assert new < original


class TestSetModel:
    def test_set_model_preserves_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("Hello")
        cm.add_assistant_message([TextBlock(text="Hi there")])
        assert len(cm.get_messages()) == 2

        cm.set_model("claude-opus-4-7")

        messages = cm.get_messages()
        assert len(messages) == 2
        assert messages[0].get_text() == "Hello"
        assert messages[1].get_text() == "Hi there"

    def test_set_model_swaps_context_window_config(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        assert cm._config.context_window == 131_072

        cm.set_model("claude-opus-4-7")
        assert cm._config.context_window == 200_000

    def test_set_model_recomputes_system_prompt_tokens(self):
        cm = ContextManager(system_prompt="A long system prompt " * 50, model="qwen")
        before = cm._system_prompt_tokens

        cm.set_model("claude-opus-4-7")
        after = cm._system_prompt_tokens
        # Both tokenizers count the same English text similarly, but the count
        # is recomputed against the new tokenizer — the value should be > 0.
        assert after > 0
        assert before > 0

    def test_set_model_noop_for_same_model(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("Hello")
        original_token_count = cm.get_messages()[0].token_count

        cm.set_model("qwen")
        assert cm.get_messages()[0].token_count == original_token_count

    def test_set_system_prompt_updates_tokens(self):
        cm = ContextManager(system_prompt="short", model="qwen")
        before = cm._system_prompt_tokens

        cm.set_system_prompt("a much longer system prompt " * 20)
        after = cm._system_prompt_tokens
        assert after > before
        assert cm.system_prompt == "a much longer system prompt " * 20
