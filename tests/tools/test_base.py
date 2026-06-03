"""Tests for the tools/base module."""

import os
from typing import Any

from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


class TestToolContext:
    """Tests for ToolContext."""

    def test_default_cwd(self):
        """Test ToolContext uses current directory by default."""
        ctx = ToolContext()
        assert ctx.cwd == os.getcwd()

    def test_custom_cwd(self):
        """Test ToolContext with custom working directory."""
        ctx = ToolContext(cwd="/tmp")
        assert ctx.cwd == "/tmp"


class TestToolResult:
    """Tests for ToolResult."""

    def test_create_tool_result(self):
        """Test creating a ToolResult directly."""
        result = ToolResult(content="Success", is_error=False)
        assert result.content == "Success"
        assert result.is_error is False

    def test_error_factory(self):
        """Test ToolResult.error() factory method."""
        result = ToolResult.error("Something went wrong")
        assert result.content == "Something went wrong"
        assert result.is_error is True

    def test_success_factory(self):
        """Test ToolResult.success() factory method."""
        result = ToolResult.success("Operation completed")
        assert result.content == "Operation completed"
        assert result.is_error is False


class DummyTool(Tool):
    """A dummy tool for testing purposes."""

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "A dummy tool for testing"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "A test message"},
            },
            "required": ["message"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success(f"Got: {tool_input.get('message')}")


class TestTool:
    """Tests for the Tool base class."""

    def test_to_api_format(self):
        """Test Tool.to_api_format() returns correct structure."""
        tool = DummyTool()
        result = tool.to_api_format()
        assert result == {
            "type": "function",
            "function": {
                "name": "dummy",
                "description": "A dummy tool for testing",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "A test message"},
                    },
                    "required": ["message"],
                },
            },
        }


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_register_and_get(self):
        """Test registering and retrieving a tool."""
        registry = ToolRegistry()
        tool = DummyTool()
        registry.register(tool)
        assert registry.get("dummy") is tool

    def test_get_nonexistent_tool(self):
        """Test getting a non-existent tool returns None."""
        registry = ToolRegistry()
        assert registry.get("nonexistent") is None

    def test_unregister_removes_tool_if_registered(self):
        """Test unregistering a tool removes it from the registry."""
        registry = ToolRegistry()
        tool = DummyTool()
        registry.register(tool)

        registry.unregister("dummy")
        registry.unregister("missing")

        assert registry.get("dummy") is None

    def test_list_tools(self):
        """Test listing all registered tools."""
        registry = ToolRegistry()
        tool = DummyTool()
        registry.register(tool)
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0] is tool

    def test_list_tools_empty(self):
        """Test listing tools when none registered."""
        registry = ToolRegistry()
        assert registry.list_tools() == []

    def test_register_default_tools(self):
        """Test register_default_tools() registers all 8 built-in tools."""
        registry = ToolRegistry()
        registry.register_default_tools()
        tools = registry.list_tools()
        assert len(tools) == 8
        # Check all tool names are present
        tool_names = {t.name for t in tools}
        assert tool_names == {
            "read_file",
            "write_file",
            "edit_file",
            "bash",
            "list_files",
            "glob",
            "grep",
            "web_fetch",
        }

    def test_to_api_format(self):
        """Test to_api_format() converts all tools to API format."""
        registry = ToolRegistry()
        registry.register(DummyTool())
        result = registry.to_api_format()
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "dummy"

    def test_register_multiple_tools(self):
        """Test registering multiple tools."""
        registry = ToolRegistry()
        registry.register_default_tools()
        # All tools should be accessible
        assert registry.get("read_file") is not None
        assert registry.get("write_file") is not None
        assert registry.get("edit_file") is not None
        assert registry.get("bash") is not None
        assert registry.get("list_files") is not None


class TestValidateInput:
    def test_valid_input_passes(self):
        tool = DummyTool()
        valid, error = tool.validate_input({"message": "hello"})
        assert valid is True
        assert error == ""

    def test_missing_required_field_fails(self):
        tool = DummyTool()
        valid, error = tool.validate_input({})
        assert valid is False
        assert "message" in error

    def test_wrong_type_fails(self):
        tool = DummyTool()
        valid, error = tool.validate_input({"message": 123})
        assert valid is False
        assert error != ""

    def test_extra_properties_accepted(self):
        tool = DummyTool()
        valid, error = tool.validate_input({"message": "hi", "extra": "ok"})
        assert valid is True
        assert error == ""
