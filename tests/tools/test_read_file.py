"""Tests for the ReadFile tool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.read_file import ReadFileTool


@pytest.fixture
def read_file_tool():
    """Create a ReadFile tool instance."""
    return ReadFileTool()


class TestReadFileTool:
    """Tests for ReadFileTool."""

    def test_tool_properties(self, read_file_tool):
        """Test tool name, description, and schema."""
        assert read_file_tool.name == "read_file"
        assert "read" in read_file_tool.description.lower()
        assert read_file_tool.input_schema["required"] == ["path"]

    @pytest.mark.asyncio
    async def test_read_normal_file(self, tmp_path, read_file_tool):
        """Test reading a normal file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, world!\nLine 2\nLine 3\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": str(test_file)},
            context=context,
        )

        assert result.is_error is False
        assert "Hello, world!" in result.content
        assert "Line 2" in result.content
        assert "3 lines" in result.content

    @pytest.mark.asyncio
    async def test_read_file_with_line_range(self, tmp_path, read_file_tool):
        """Test reading a file with start_line and end_line."""
        test_file = tmp_path / "test.txt"
        lines = "\n".join([f"Line {i}" for i in range(1, 11)])
        test_file.write_text(lines)

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": str(test_file), "start_line": 3, "end_line": 5},
            context=context,
        )

        assert result.is_error is False
        assert "Line 3" in result.content
        assert "Line 4" in result.content
        assert "Line 5" in result.content
        assert "lines 3-5 of 10" in result.content
        assert "Line 1" not in result.content
        assert "Line 6" not in result.content

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path, read_file_tool):
        """Test reading a non-existent file returns error."""
        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": "/nonexistent/path/file.txt"},
            context=context,
        )

        assert result.is_error is True
        assert "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_read_empty_file(self, tmp_path, read_file_tool):
        """Test reading an empty file."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("")

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": str(test_file)},
            context=context,
        )

        assert result.is_error is False
        assert "empty file" in result.content.lower() or "0 lines" in result.content.lower()

    @pytest.mark.asyncio
    async def test_read_relative_path(self, tmp_path, read_file_tool):
        """Test reading a file with relative path."""
        test_file = tmp_path / "relative.txt"
        test_file.write_text("Relative path content")

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": "relative.txt"},
            context=context,
        )

        assert result.is_error is False
        assert "Relative path content" in result.content

    @pytest.mark.asyncio
    async def test_read_file_in_subdirectory(self, tmp_path, read_file_tool):
        """Test reading a file in a subdirectory with relative path."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        test_file = subdir / "nested.txt"
        test_file.write_text("Nested content")

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": "subdir/nested.txt"},
            context=context,
        )

        assert result.is_error is False
        assert "Nested content" in result.content

    @pytest.mark.asyncio
    async def test_read_file_start_line_only(self, tmp_path, read_file_tool):
        """Test reading with only start_line specified."""
        test_file = tmp_path / "test.txt"
        lines = "\n".join([f"Line {i}" for i in range(1, 6)])
        test_file.write_text(lines)

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": str(test_file), "start_line": 3},
            context=context,
        )

        assert result.is_error is False
        assert "Line 3" in result.content
        assert "Line 4" in result.content
        assert "Line 5" in result.content

    @pytest.mark.asyncio
    async def test_read_file_end_line_only(self, tmp_path, read_file_tool):
        """Test reading with only end_line specified."""
        test_file = tmp_path / "test.txt"
        lines = "\n".join([f"Line {i}" for i in range(1, 6)])
        test_file.write_text(lines)

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": str(test_file), "end_line": 2},
            context=context,
        )

        assert result.is_error is False
        assert "Line 1" in result.content
        assert "Line 2" in result.content
        assert "Line 3" not in result.content

    @pytest.mark.asyncio
    async def test_windows_posix_path_conversion(self, tmp_path, read_file_tool, monkeypatch):
        target = tmp_path / "msys_path.txt"
        target.write_text("hello", encoding="utf-8")
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "iac_code.tools.read_file.normalize_user_path",
            MagicMock(side_effect=lambda raw: raw),
        )
        from iac_code.tools.base import ToolContext

        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(
            tool_input={"path": str(target)},
            context=context,
        )
        assert result.is_error is False
        from iac_code.tools.read_file import normalize_user_path

        normalize_user_path.assert_called_once_with(str(target))


class TestReadFileErrors:
    @pytest.mark.asyncio
    async def test_permission_error(self, tmp_path, read_file_tool, monkeypatch):
        file_path = tmp_path / "x.txt"
        file_path.write_text("abc")

        def boom(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr("builtins.open", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(tool_input={"path": str(file_path)}, context=context)
        assert result.is_error is True
        assert "permission" in result.content.lower()

    @pytest.mark.asyncio
    async def test_binary_file_unicode_decode_error(self, tmp_path, read_file_tool, monkeypatch):
        file_path = tmp_path / "bin.bin"
        file_path.write_bytes(b"\x00\xff\xfe")

        def boom(*a, **kw):
            raise UnicodeDecodeError("utf-8", b"\x00", 0, 1, "invalid")

        monkeypatch.setattr("builtins.open", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(tool_input={"path": str(file_path)}, context=context)
        assert result.is_error is True
        assert "binary" in result.content.lower()

    @pytest.mark.asyncio
    async def test_generic_exception(self, tmp_path, read_file_tool, monkeypatch):
        file_path = tmp_path / "x.txt"
        file_path.write_text("abc")

        def boom(*a, **kw):
            raise RuntimeError("unexpected")

        monkeypatch.setattr("builtins.open", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await read_file_tool.execute(tool_input={"path": str(file_path)}, context=context)
        assert result.is_error is True
        assert "unexpected" in result.content


class TestReadFileRendering:
    def test_normalize_input_aliases_file_path(self, read_file_tool):
        inp = {"file_path": "/a.py"}
        read_file_tool.normalize_input(inp)
        assert inp.get("path") == "/a.py"

    def test_render_tool_use_empty_path_returns_none(self, read_file_tool):
        assert read_file_tool.render_tool_use_message({}) is None

    def test_render_tool_use_basename_by_default(self, read_file_tool):
        assert read_file_tool.render_tool_use_message({"path": "/a/b/c.py"}) == "c.py"

    def test_render_tool_use_verbose_full_path(self, read_file_tool):
        assert read_file_tool.render_tool_use_message({"path": "/a/b/c.py"}, verbose=True) == "/a/b/c.py"

    def test_render_tool_use_with_line_range(self, read_file_tool):
        msg = read_file_tool.render_tool_use_message({"path": "/a.py", "start_line": 3, "end_line": 5})
        assert "lines 3-5" in msg

    def test_render_tool_use_with_start_only(self, read_file_tool):
        msg = read_file_tool.render_tool_use_message({"path": "/a.py", "start_line": 3})
        assert "from line 3" in msg

    def test_render_tool_result_is_error_passthrough(self, read_file_tool):
        assert read_file_tool.render_tool_result_message("Permission denied", is_error=True) == "Permission denied"

    def test_render_tool_result_verbose_strips(self, read_file_tool):
        out = "File: /a (2 lines)\n\n     1\tx\n     2\ty\n"
        assert read_file_tool.render_tool_result_message(out, verbose=True) == out.strip()

    def test_render_tool_result_compact_reports_lines(self, read_file_tool):
        out = "File: /a (2 lines)\n\n     1\tx\n     2\ty"
        msg = read_file_tool.render_tool_result_message(out)
        assert "2" in msg

    def test_render_tool_result_compact_empty_output(self, read_file_tool):
        assert read_file_tool.render_tool_result_message("") is not None

    def test_user_facing_name(self, read_file_tool):
        assert read_file_tool.user_facing_name() == "Read"

    def test_get_activity_description_with_input(self, read_file_tool):
        msg = read_file_tool.get_activity_description({"path": "/f.py"})
        assert "/f.py" in msg

    def test_get_activity_description_default(self, read_file_tool):
        assert read_file_tool.get_activity_description(None)

    def test_is_read_only(self, read_file_tool):
        assert read_file_tool.is_read_only() is True
