import pytest

from iac_code.providers.base import Message, Provider, ToolDefinition


class TestToolDefinition:
    def test_tool_definition(self):
        td = ToolDefinition(
            name="read_file",
            description="Read a file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        )
        assert td.name == "read_file"


class TestMessage:
    def test_user_message(self):
        msg = Message.user("Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_assistant_text(self):
        msg = Message.assistant_text("Hi there")
        assert msg.role == "assistant"
        assert isinstance(msg.content, list)
        assert msg.content[0].type == "text"
        assert msg.content[0].text == "Hi there"

    def test_tool_result(self):
        msg = Message.tool_result(tool_use_id="t1", content="result", is_error=False)
        assert msg.role == "user"
        assert isinstance(msg.content, list)
        assert msg.content[0].type == "tool_result"

    def test_assistant_tool_use(self):
        msg = Message.assistant_tool_use(tool_use_id="t1", name="bash", input={"command": "ls"})
        assert msg.role == "assistant"
        assert msg.content[0].type == "tool_use"
        assert msg.content[0].name == "bash"


class TestProviderIsAbstract:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Provider()


class TestProviderThinkingHooks:
    def _stub(self):
        from iac_code.providers.base import Provider

        class _Stub(Provider):
            def stream(self, *a, **k):
                raise NotImplementedError

            async def complete(self, *a, **k):
                raise NotImplementedError

            def get_model_name(self) -> str:
                return "stub"

        return _Stub()

    def test_default_build_thinking_kwargs_returns_empty(self):
        assert self._stub()._build_thinking_kwargs() == {}

    def test_default_adjust_max_tokens_passthrough(self):
        assert self._stub()._adjust_max_tokens(8192) == 8192
