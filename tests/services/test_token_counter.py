from iac_code.services.token_counter import TokenCounter


class TestTokenCounter:
    def test_count_text_nonempty(self):
        counter = TokenCounter()
        count = counter.count_text("Hello world")
        assert count > 0

    def test_count_text_empty(self):
        counter = TokenCounter()
        assert counter.count_text("") == 0

    def test_count_message_text(self):
        counter = TokenCounter()
        msg = {"role": "user", "content": "Hello"}
        count = counter.count_message(msg)
        assert count > 0

    def test_count_message_with_tool_use(self):
        counter = TokenCounter()
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll read that file."},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "/tmp/f"}},
            ],
        }
        count = counter.count_message(msg)
        assert count > 20  # text + tool overhead + name + input

    def test_count_message_with_tool_result(self):
        counter = TokenCounter()
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file contents here"},
            ],
        }
        count = counter.count_message(msg)
        assert count > 5

    def test_count_messages_sums(self):
        counter = TokenCounter()
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        total = counter.count_messages(msgs)
        individual = sum(counter.count_message(m) for m in msgs)
        assert total == individual

    def test_model_aware_construction(self):
        # Should not raise for any model name
        TokenCounter(model="claude-3-opus")
        TokenCounter(model="gpt-4")
        TokenCounter(model="qwen-turbo")
        TokenCounter(model="unknown-model")
