"""Tests for ToolResult extensions (new_messages and context_modifier)."""

from iac_code.tools.base import ToolResult


class TestToolResultExtensions:
    def test_default_new_messages_empty(self):
        result = ToolResult(content="test")
        assert result.new_messages == []

    def test_default_context_modifier_none(self):
        result = ToolResult(content="test")
        assert result.context_modifier is None

    def test_with_new_messages(self):
        messages = [{"role": "user", "content": "injected"}]
        result = ToolResult(content="ok", new_messages=messages)
        assert len(result.new_messages) == 1
        assert result.new_messages[0]["content"] == "injected"

    def test_with_context_modifier(self):
        def modifier(ctx):
            return {**ctx, "model_override": "test-model"}

        result = ToolResult(content="ok", context_modifier=modifier)
        modified = result.context_modifier({"model_override": None})
        assert modified["model_override"] == "test-model"

    def test_error_factory_has_defaults(self):
        result = ToolResult.error("fail")
        assert result.is_error is True
        assert result.new_messages == []
        assert result.context_modifier is None

    def test_success_factory_has_defaults(self):
        result = ToolResult.success("ok")
        assert result.is_error is False
        assert result.new_messages == []
        assert result.context_modifier is None
