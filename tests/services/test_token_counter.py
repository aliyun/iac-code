import pytest

from iac_code.services.token_counter import TokenCounter


def test_count_tool_definitions_includes_schema_text():
    counter = TokenCounter(model="gpt-5")
    small_tool = {
        "name": "read_file",
        "description": "Read a file from disk",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    }
    large_tool = {
        **small_tool,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "encoding": {"type": "string", "enum": ["utf-8", "latin-1"]},
            },
            "required": ["path"],
        },
    }

    small_count = counter.count_tool_definitions([small_tool])
    large_count = counter.count_tool_definitions([large_tool])

    assert large_count > small_count
    assert small_count > counter.count_text("read_file") + counter.count_text("Read a file from disk")


@pytest.mark.parametrize(
    "model",
    [
        "qwen3.7-max",
        "qwq-plus",
        "kimi-k2.6",
        "glm-5.1",
        "doubao-seed-2-0-code-preview-260215",
        "MiniMax-M2.7",
        "gemini-3.5-flash",
        "unknown-model",
    ],
)
def test_non_openai_model_families_use_cjk_aware_fallback(model):
    counter = TokenCounter(model=model)

    assert counter._encoder is None
    cjk_count = counter.count_text("基础设施代码")
    ascii_count = counter.count_text("abcdef")
    assert cjk_count > 0
    assert cjk_count > ascii_count


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
