"""Tests for the WriteFile tool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.write_file import WriteFileTool


@pytest.fixture
def write_file_tool():
    """Create a WriteFile tool instance."""
    return WriteFileTool()


class TestWriteFileTool:
    """Tests for WriteFileTool."""

    def test_tool_properties(self, write_file_tool):
        """Test tool name, description, and schema."""
        assert write_file_tool.name == "write_file"
        assert "write" in write_file_tool.description.lower()
        assert set(write_file_tool.input_schema["required"]) == {"path", "content"}

    @pytest.mark.asyncio
    async def test_write_new_file(self, tmp_path, write_file_tool):
        """Test writing a new file."""
        file_path = tmp_path / "new_file.txt"
        content = "Hello, world!\nLine 2\n"

        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": str(file_path), "content": content},
            context=context,
        )

        assert result.is_error is False
        assert "successfully" in result.content.lower()
        assert file_path.exists()
        assert file_path.read_text() == content

    @pytest.mark.asyncio
    async def test_overwrite_existing_file(self, tmp_path, write_file_tool):
        """Test overwriting an existing file."""
        file_path = tmp_path / "existing.txt"
        file_path.write_text("Old content")

        new_content = "New content here"
        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": str(file_path), "content": new_content},
            context=context,
        )

        assert result.is_error is False
        assert file_path.read_text() == new_content

    @pytest.mark.asyncio
    async def test_auto_create_parent_directories(self, tmp_path, write_file_tool):
        """Test that parent directories are created automatically."""
        file_path = tmp_path / "subdir" / "nested" / "deep" / "file.txt"
        content = "Nested content"

        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": str(file_path), "content": content},
            context=context,
        )

        assert result.is_error is False
        assert file_path.exists()
        assert file_path.read_text() == content

    @pytest.mark.asyncio
    async def test_relative_path_resolution(self, tmp_path, write_file_tool):
        """Test writing with relative path."""
        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": "relative.txt", "content": "Relative content"},
            context=context,
        )

        assert result.is_error is False
        file_path = tmp_path / "relative.txt"
        assert file_path.exists()
        assert file_path.read_text() == "Relative content"

    @pytest.mark.asyncio
    async def test_write_empty_content(self, tmp_path, write_file_tool):
        """Test writing empty content."""
        file_path = tmp_path / "empty.txt"

        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": str(file_path), "content": ""},
            context=context,
        )

        assert result.is_error is False
        assert file_path.exists()
        assert file_path.read_text() == ""

    @pytest.mark.asyncio
    async def test_write_multiline_content(self, tmp_path, write_file_tool):
        """Test writing multi-line content."""
        file_path = tmp_path / "multiline.txt"
        content = "Line 1\nLine 2\nLine 3\nLine 4\n"

        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": str(file_path), "content": content},
            context=context,
        )

        assert result.is_error is False
        assert "4 lines" in result.content
        assert file_path.read_text() == content

    @pytest.mark.asyncio
    async def test_write_unicode_content(self, tmp_path, write_file_tool):
        """Test writing unicode content."""
        file_path = tmp_path / "unicode.txt"
        content = "Hello 世界! 🎉\nÜnicode tëst"

        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": str(file_path), "content": content},
            context=context,
        )

        assert result.is_error is False
        assert file_path.read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_write_relative_path_in_subdirectory(self, tmp_path, write_file_tool):
        """Test writing with relative path including subdirectory."""
        context = ToolContext(cwd=str(tmp_path))
        result = await write_file_tool.execute(
            tool_input={"path": "subdir/file.txt", "content": "Nested file"},
            context=context,
        )

        assert result.is_error is False
        file_path = tmp_path / "subdir" / "file.txt"
        assert file_path.exists()
        assert file_path.read_text() == "Nested file"

    @pytest.mark.asyncio
    async def test_windows_posix_path_conversion(self, tmp_path, write_file_tool, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "iac_code.tools.write_file.normalize_user_path",
            MagicMock(side_effect=lambda raw: raw),
        )
        from iac_code.tools.base import ToolContext

        context = ToolContext(cwd=str(tmp_path))
        target = tmp_path / "out.txt"
        result = await write_file_tool.execute(
            tool_input={"path": str(target), "content": "x"},
            context=context,
        )
        assert result.is_error is False
        from iac_code.tools.write_file import normalize_user_path

        normalize_user_path.assert_called_once_with(str(target))


class TestWriteFileExtras:
    @pytest.fixture
    def tool(self):
        from iac_code.tools.write_file import WriteFileTool

        return WriteFileTool()

    def test_normalize_input_alias(self, tool):
        inp = {"file_path": "/a.py", "content": "x"}
        tool.normalize_input(inp)
        assert inp["path"] == "/a.py"

    @pytest.mark.asyncio
    async def test_permission_error(self, tool, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr("builtins.open", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"path": str(tmp_path / "x.txt"), "content": "c"}, context=context)
        assert result.is_error is True
        assert "permission" in result.content.lower()

    @pytest.mark.asyncio
    async def test_generic_write_exception(self, tool, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"path": str(tmp_path / "x.txt"), "content": "c"}, context=context)
        assert result.is_error is True
        assert "disk full" in result.content or "writing" in result.content.lower()

    def test_render_tool_use_empty(self, tool):
        assert tool.render_tool_use_message({}) is None

    def test_render_tool_use_path(self, tool):
        assert tool.render_tool_use_message({"path": "/a.py"}) == "/a.py"

    def test_render_tool_result_passes_through(self, tool):
        assert tool.render_tool_result_message("Wrote 3 lines") == "Wrote 3 lines"

    def test_user_facing_name(self, tool):
        assert tool.user_facing_name() == "Write"

    def test_get_activity_description(self, tool):
        msg = tool.get_activity_description({"path": "/x"})
        assert "/x" in msg
        assert tool.get_activity_description(None)

    def test_is_read_only_false(self, tool):
        assert tool.is_read_only() is False

    def test_is_destructive_true(self, tool):
        assert tool.is_destructive() is True
