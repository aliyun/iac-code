from types import SimpleNamespace

from iac_code.agent.message import TextBlock, ToolResultBlock, ToolUseBlock
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
        assert "tool_definition_tokens" in usage
        assert "user_message_tokens" in usage
        assert "assistant_message_tokens" in usage
        assert "tool_result_tokens" in usage
        assert "total_tokens" in usage
        assert "context_window" in usage
        assert "usage_percent" in usage

    def test_tool_definitions_count_toward_total_and_usage(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        base_total = cm.get_total_tokens()
        tool = SimpleNamespace(
            name="create_stack",
            description="Create a ROS stack",
            input_schema={
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "Template body"},
                },
            },
        )

        cm.set_tool_definitions([tool])

        usage = cm.get_usage()
        assert usage["tool_definition_tokens"] > 0
        assert cm.get_total_tokens() == base_total + usage["tool_definition_tokens"]
        assert usage["total_tokens"] == cm.get_total_tokens()
        assert usage["usage_percent"] > 0

    def test_set_tool_definitions_copies_input_list(self):
        cm = ContextManager(system_prompt="You are helpful.", model="qwen")
        tool = SimpleNamespace(name="read_file", description="Read file", input_schema={"type": "object"})
        tools = [tool]

        cm.set_tool_definitions(tools)
        before = cm.get_usage()["tool_definition_tokens"]
        tools.append(
            SimpleNamespace(
                name="write_file",
                description="Write a much larger file",
                input_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            )
        )

        assert cm.get_usage()["tool_definition_tokens"] == before


class TestSegmentedCompaction:
    def test_build_compaction_prompt_excludes_recent(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Assistant response {i}")])

        prompt = cm.build_compaction_prompt()
        assert "User message 0" in prompt
        assert "User message 5" not in prompt

    def test_build_compaction_prompt_excludes_recalled_memory_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_recalled_memory_message("# Recalled Memory\nhidden memory body", ["hidden-topic.md"])
        for i in range(6):
            cm.add_user_message(f"User message {i}")
            cm.add_assistant_message([TextBlock(text=f"Assistant response {i}")])

        prompt = cm.build_compaction_prompt()

        assert "User message 0" in prompt
        assert "hidden memory body" not in prompt
        assert "hidden-topic.md" not in prompt

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

    def test_apply_compaction_does_not_split_tool_round_trip(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("User message 0")
        cm.add_assistant_message([TextBlock(text="Assistant response 0")])
        cm.add_user_message("Please read a file")
        cm.add_assistant_message([ToolUseBlock(id="toolu_read", name="read_file", input={"path": "a.txt"})])
        cm.add_tool_results([ToolResultBlock(tool_use_id="toolu_read", content="file contents")])
        cm.add_assistant_message([TextBlock(text="Read complete")])
        cm.add_user_message("User message 2")
        cm.add_assistant_message([TextBlock(text="Assistant response 2")])
        cm.add_user_message("User message 3")
        cm.add_assistant_message([TextBlock(text="Assistant response 3")])

        cm.apply_compaction("Summary of old conversation")

        messages = cm.get_messages()
        assert "Summary" in messages[0].get_text()
        assert messages[1].role == "assistant"
        assert messages[1].get_tool_use_blocks()[0].id == "toolu_read"
        assert messages[2].role == "user"
        assert isinstance(messages[2].content, list)
        assert messages[2].content[0].tool_use_id == "toolu_read"

    def test_compaction_keeps_unfinished_tool_use_in_recent_messages(self):
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.add_user_message("User message 0")
        cm.add_assistant_message([TextBlock(text="Assistant response 0")])
        cm.add_user_message("Start a tool")
        cm.add_assistant_message([ToolUseBlock(id="toolu_pending", name="bash", input={"command": "sleep 1"})])
        cm.add_user_message("Follow-up after interrupted tool use")
        cm.add_assistant_message([TextBlock(text="Assistant response after interruption")])
        cm.add_user_message("User message 3")
        cm.add_assistant_message([TextBlock(text="Assistant response 3")])

        cm.apply_compaction("Summary of old conversation")

        messages = cm.get_messages()
        assert "Summary" in messages[0].get_text()
        assert any(
            msg.get_tool_use_blocks() and msg.get_tool_use_blocks()[0].id == "toolu_pending" for msg in messages[1:]
        )


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

    def test_set_model_recomputes_tool_definition_tokens(self, monkeypatch):
        class FakeTokenCounter:
            def __init__(self, model=""):
                self.model = model

            def count_text(self, text):
                return len(text)

            def count_message(self, message):
                return 1

            def count_tool_definitions(self, tools):
                return 10 if self.model == "qwen" else 30

        monkeypatch.setattr("iac_code.services.context_manager.TokenCounter", FakeTokenCounter)
        cm = ContextManager(system_prompt="sys", model="qwen")
        cm.set_tool_definitions([SimpleNamespace(name="read_file", description="Read file", input_schema={})])
        assert cm.get_usage()["tool_definition_tokens"] == 10

        cm.set_model("claude-opus-4-7")

        assert cm.get_usage()["tool_definition_tokens"] == 30


def test_add_recalled_memory_message_tracks_surfaced_files():
    cm = ContextManager(system_prompt="sys", model="qwen")

    msg = cm.add_recalled_memory_message(
        "# Recalled Memory\nUse YAML for ROS templates",
        ["ros-yaml.md"],
    )

    assert msg.role == "user"
    assert msg.metadata["type"] == "recalled_memory"
    assert cm.get_surfaced_memory_files() == {"ros-yaml.md"}
    assert "Use YAML for ROS templates" in cm.get_api_messages()[0]["content"]


def test_compaction_surfaced_files_come_from_retained_metadata_only():
    cm = ContextManager(system_prompt="sys", model="qwen")
    cm.add_recalled_memory_message("# Recalled Memory\nOld memory", ["old.md"])
    for i in range(6):
        cm.add_user_message(f"User message {i}")
        cm.add_assistant_message(f"Assistant response {i}")
    cm.add_recalled_memory_message("# Recalled Memory\nRecent memory", ["recent.md"])

    cm.apply_compaction("Summary mentions old.md and recent.md")

    assert cm.get_surfaced_memory_files() == {"recent.md"}
