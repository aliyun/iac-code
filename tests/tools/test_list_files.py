"""Tests for the ListFiles tool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.list_files import ListFilesTool


@pytest.fixture
def list_files_tool():
    """Create a ListFiles tool instance."""
    return ListFilesTool()


class TestListFilesTool:
    """Tests for ListFilesTool."""

    def test_tool_properties(self, list_files_tool):
        """Test tool name, description, and schema."""
        assert list_files_tool.name == "list_files"
        assert "list" in list_files_tool.description.lower() or "directory" in list_files_tool.description.lower()
        assert list_files_tool.input_schema["required"] == []

    @pytest.mark.asyncio
    async def test_list_directory_contents(self, tmp_path, list_files_tool):
        """Test listing directory contents."""
        # Create some files and directories
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.py").write_text("content2")
        (tmp_path / "subdir").mkdir()

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": str(tmp_path)},
            context=context,
        )

        assert result.is_error is False
        assert "file1.txt" in result.content
        assert "file2.py" in result.content
        assert "subdir/" in result.content

    @pytest.mark.asyncio
    async def test_list_empty_directory(self, tmp_path, list_files_tool):
        """Test listing an empty directory."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": str(empty_dir)},
            context=context,
        )

        assert result.is_error is False
        assert "empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_path_not_found(self, tmp_path, list_files_tool):
        """Test error when path does not exist."""
        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": "/nonexistent/path"},
            context=context,
        )

        assert result.is_error is True
        assert "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_distinguish_files_and_directories(self, tmp_path, list_files_tool):
        """Test that files and directories are distinguished."""
        (tmp_path / "regular_file.txt").write_text("content")
        (tmp_path / "a_directory").mkdir()

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": str(tmp_path)},
            context=context,
        )

        assert result.is_error is False
        # Directories should have trailing slash
        assert "a_directory/" in result.content
        # Files should not have trailing slash but may have size
        assert "regular_file.txt" in result.content

    @pytest.mark.asyncio
    async def test_default_path_uses_cwd(self, tmp_path, list_files_tool):
        """Test that no path defaults to current working directory."""
        (tmp_path / "cwd_file.txt").write_text("content")

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={},
            context=context,
        )

        assert result.is_error is False
        assert "cwd_file.txt" in result.content

    @pytest.mark.asyncio
    async def test_relative_path(self, tmp_path, list_files_tool):
        """Test listing with relative path."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested_file.txt").write_text("content")

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": "subdir"},
            context=context,
        )

        assert result.is_error is False
        assert "nested_file.txt" in result.content

    @pytest.mark.asyncio
    async def test_path_is_file_not_directory(self, tmp_path, list_files_tool):
        """Test error when path is a file, not a directory."""
        file_path = tmp_path / "just_a_file.txt"
        file_path.write_text("content")

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": str(file_path)},
            context=context,
        )

        assert result.is_error is True
        assert "not a directory" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shows_file_sizes(self, tmp_path, list_files_tool):
        """Test that file sizes are shown."""
        (tmp_path / "sized_file.txt").write_text("12345678901234567890")  # 20 bytes

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": str(tmp_path)},
            context=context,
        )

        assert result.is_error is False
        assert "sized_file.txt" in result.content
        # Should show some size indicator (B, KB, etc.)
        assert "B" in result.content or "KB" in result.content

    @pytest.mark.asyncio
    async def test_sorted_output(self, tmp_path, list_files_tool):
        """Test that output is sorted alphabetically."""
        (tmp_path / "zebra.txt").write_text("z")
        (tmp_path / "alpha.txt").write_text("a")
        (tmp_path / "middle.txt").write_text("m")

        context = ToolContext(cwd=str(tmp_path))
        result = await list_files_tool.execute(
            tool_input={"path": str(tmp_path)},
            context=context,
        )

        assert result.is_error is False
        # Check alphabetical order
        alpha_pos = result.content.find("alpha.txt")
        middle_pos = result.content.find("middle.txt")
        zebra_pos = result.content.find("zebra.txt")
        assert alpha_pos < middle_pos < zebra_pos


class TestListFilesExtras:
    @pytest.fixture
    def tool(self):
        from iac_code.tools.list_files import ListFilesTool

        return ListFilesTool()

    @pytest.mark.asyncio
    async def test_permission_error(self, tool, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr("os.listdir", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"path": str(tmp_path)}, context=context)
        assert result.is_error is True
        assert "permission" in result.content.lower()

    @pytest.mark.asyncio
    async def test_generic_listdir_error(self, tool, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("weird")

        monkeypatch.setattr("os.listdir", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"path": str(tmp_path)}, context=context)
        assert result.is_error is True
        assert "weird" in result.content

    def test_render_tool_use_default(self, tool):
        assert tool.render_tool_use_message({}) == "."

    def test_render_tool_result_is_error_passthrough(self, tool):
        assert tool.render_tool_result_message("bad", is_error=True) == "bad"

    def test_render_tool_result_compact_summary(self, tool):
        out = "Directory: /a\n\n  f.txt (10B)\n  g.txt (20B)"
        msg = tool.render_tool_result_message(out)
        assert "2" in msg

    def test_render_tool_result_verbose_lists(self, tool):
        out = "Directory: /a\n\n  f.txt (10B)"
        msg = tool.render_tool_result_message(out, verbose=True)
        assert "f.txt" in msg

    def test_user_facing_name(self, tool):
        assert tool.user_facing_name() == "List"

    def test_get_activity_description(self, tool):
        assert tool.get_activity_description({"path": "."})
        assert tool.get_activity_description(None)

    def test_is_read_only(self, tool):
        assert tool.is_read_only() is True

    def test_format_size_bytes(self):
        from iac_code.tools.list_files import _format_size

        assert _format_size(0) == "0B"
        assert _format_size(512) == "512B"

    def test_format_size_kb(self):
        from iac_code.tools.list_files import _format_size

        assert "KB" in _format_size(2048)

    def test_format_size_mb(self):
        from iac_code.tools.list_files import _format_size

        assert "MB" in _format_size(2 * 1024 * 1024)

    def test_format_size_gb(self):
        from iac_code.tools.list_files import _format_size

        assert "GB" in _format_size(2 * 1024 * 1024 * 1024)

    def test_format_size_tb(self):
        from iac_code.tools.list_files import _format_size

        assert "TB" in _format_size(2 * 1024 * 1024 * 1024 * 1024)
