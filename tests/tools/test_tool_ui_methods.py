"""Tests for tool UI and permission methods."""

import pytest

from iac_code.tools.base import Tool, ToolResult
from iac_code.tools.bash import BashTool
from iac_code.tools.edit_file import EditFileTool
from iac_code.tools.list_files import ListFilesTool
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionResult


class TestToolBaseDefaults:
    """Tests for Tool base class default implementations."""

    def test_render_tool_use_message_default(self):
        """Test base Tool render_tool_use_message returns None."""

        class DummyTool(Tool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "Dummy tool"

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input, context):
                return ToolResult.success("ok")

        tool = DummyTool()
        assert tool.render_tool_use_message({}) is None

    def test_render_tool_result_message_default(self):
        """Test base Tool render_tool_result_message returns None."""

        class DummyTool(Tool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "Dummy tool"

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input, context):
                return ToolResult.success("ok")

        tool = DummyTool()
        assert tool.render_tool_result_message("output") is None

    def test_user_facing_name_default(self):
        """Test base Tool user_facing_name returns name."""

        class DummyTool(Tool):
            @property
            def name(self):
                return "my_tool"

            @property
            def description(self):
                return "Dummy tool"

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input, context):
                return ToolResult.success("ok")

        tool = DummyTool()
        assert tool.user_facing_name() == "my_tool"

    def test_get_activity_description_default(self):
        """Test base Tool get_activity_description returns None."""

        class DummyTool(Tool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "Dummy tool"

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input, context):
                return ToolResult.success("ok")

        tool = DummyTool()
        assert tool.get_activity_description() is None

    def test_is_read_only_default(self):
        """Test base Tool is_read_only returns False."""

        class DummyTool(Tool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "Dummy tool"

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input, context):
                return ToolResult.success("ok")

        tool = DummyTool()
        assert tool.is_read_only() is False

    def test_is_destructive_default(self):
        """Test base Tool is_destructive returns False."""

        class DummyTool(Tool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "Dummy tool"

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input, context):
                return ToolResult.success("ok")

        tool = DummyTool()
        assert tool.is_destructive() is False


class TestBashToolUI:
    """Tests for BashTool UI methods."""

    def test_user_facing_name(self):
        """Test BashTool user_facing_name."""
        tool = BashTool()
        assert tool.user_facing_name() == "Bash"

    def test_is_read_only(self):
        """Test BashTool is_read_only returns False."""
        tool = BashTool()
        assert tool.is_read_only() is False

    def test_is_destructive(self):
        """Test BashTool is_destructive returns False."""
        tool = BashTool()
        assert tool.is_destructive() is False

    def test_get_activity_description_with_input(self):
        """Test BashTool get_activity_description with input."""
        tool = BashTool()
        result = tool.get_activity_description({"command": "ls -la"})
        assert result is not None
        assert "ls -la" in result

    def test_get_activity_description_truncates_long_command(self):
        """Test BashTool get_activity_description truncates long commands."""
        tool = BashTool()
        long_cmd = "a" * 100
        result = tool.get_activity_description({"command": long_cmd})
        assert "..." in result
        assert len(result) < 100

    def test_get_activity_description_without_input(self):
        """Test BashTool get_activity_description without input."""
        tool = BashTool()
        result = tool.get_activity_description()
        assert result == "Running command..."

    def test_render_tool_use_message(self):
        """Test BashTool render_tool_use_message returns string."""
        tool = BashTool()
        result = tool.render_tool_use_message({"command": "ls"})
        assert isinstance(result, str)
        assert "ls" in result

    def test_render_tool_use_message_truncates_multiline_and_long_command(self):
        tool = BashTool()
        long_cmd = "line1\nline2\nline3-" + ("x" * 200)
        result = tool.render_tool_use_message({"command": long_cmd})
        assert result.endswith("…")
        assert "line3" not in result

    def test_render_tool_use_message_empty_command_returns_none(self):
        tool = BashTool()
        assert tool.render_tool_use_message({}) is None

    def test_render_tool_result_message_success(self):
        """Test BashTool render_tool_result_message for success."""
        tool = BashTool()
        result = tool.render_tool_result_message("output", is_error=False)
        assert isinstance(result, str)

    def test_render_tool_result_message_truncates_many_lines(self):
        tool = BashTool()
        output = "\n".join(f"line{i}" for i in range(25))
        result = tool.render_tool_result_message(output, is_error=False)
        assert "... 5 more lines" in result

    def test_render_tool_result_message_error(self):
        """Test BashTool render_tool_result_message for error."""
        tool = BashTool()
        result = tool.render_tool_result_message("error", is_error=True)
        assert isinstance(result, str)

    def test_render_tool_use_error_message(self):
        """Test BashTool render_tool_use_error_message."""
        tool = BashTool()
        result = tool.render_tool_use_error_message("Some error")
        assert isinstance(result, str)

    def test_get_tool_use_summary(self):
        """Test BashTool get_tool_use_summary."""
        tool = BashTool()
        result = tool.get_tool_use_summary({"command": "echo hello"})
        assert result is not None
        assert "echo hello" in result

    def test_get_tool_use_summary_without_input(self):
        tool = BashTool()
        assert tool.get_tool_use_summary() is None


class TestReadFileToolUI:
    """Tests for ReadFileTool UI methods."""

    def test_read_file_is_read_only(self):
        """Test ReadFileTool is_read_only returns True."""
        tool = ReadFileTool()
        assert tool.is_read_only() is True


class TestWriteFileToolUI:
    """Tests for WriteFileTool UI methods."""

    def test_write_file_is_read_only(self):
        """Test WriteFileTool is_read_only returns False."""
        tool = WriteFileTool()
        assert tool.is_read_only() is False


class TestListFilesToolUI:
    """Tests for ListFilesTool UI methods."""

    def test_list_files_is_read_only(self):
        """Test ListFilesTool is_read_only returns True."""
        tool = ListFilesTool()
        assert tool.is_read_only() is True


class TestEditFileToolUI:
    """Tests for EditFileTool UI methods."""

    def test_edit_file_is_read_only(self):
        """Test EditFileTool is_read_only returns False."""
        tool = EditFileTool()
        assert tool.is_read_only() is False


class TestToolCheckPermissions:
    """Tests for Tool.check_permissions method."""

    @pytest.mark.asyncio
    async def test_check_permissions_read_only_tool(self):
        """Test check_permissions for read-only tool returns allow."""
        tool = ReadFileTool()
        result = await tool.check_permissions({"path": "/test.txt"})
        assert isinstance(result, PermissionResult)
        assert result.behavior == "allow"

    @pytest.mark.asyncio
    async def test_check_permissions_write_tool(self):
        """Test check_permissions for write tool returns ask."""
        tool = WriteFileTool()
        result = await tool.check_permissions({"path": "/test.txt", "content": "hi"})
        assert isinstance(result, PermissionResult)
        assert result.behavior == "ask"

    @pytest.mark.asyncio
    async def test_check_permissions_bash_tool(self):
        """Test check_permissions for bash tool returns ask."""
        tool = BashTool()
        result = await tool.check_permissions({"command": "rm -rf /"})
        assert isinstance(result, PermissionResult)
        assert result.behavior == "ask"
