"""Tests for the Glob tool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.glob import GlobTool


@pytest.fixture
def tool():
    return GlobTool()


class TestGlobBasics:
    def test_tool_properties(self, tool):
        assert tool.name == "glob"
        assert tool.input_schema["required"] == ["pattern"]

    @pytest.mark.asyncio
    async def test_match_py_files(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "*.py", "path": str(tmp_path)}, context=context)
        assert result.is_error is False
        assert "a.py" in result.content
        assert "b.py" in result.content
        assert "c.txt" not in result.content

    @pytest.mark.asyncio
    async def test_no_match_returns_message(self, tool, tmp_path):
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "*.nomatch", "path": str(tmp_path)}, context=context)
        assert result.is_error is False
        assert result.content == "No files found"

    @pytest.mark.asyncio
    async def test_path_not_found(self, tool, tmp_path):
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(
            tool_input={"pattern": "*.py", "path": str(tmp_path / "nonexistent")},
            context=context,
        )
        assert result.is_error is True
        assert "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_path_not_a_directory(self, tool, tmp_path):
        file_path = tmp_path / "file.txt"
        file_path.write_text("x")
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "*.py", "path": str(file_path)}, context=context)
        assert result.is_error is True
        assert "not a directory" in result.content.lower()

    @pytest.mark.asyncio
    async def test_relative_path_resolved(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("")
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "*.py", "path": "."}, context=context)
        assert result.is_error is False
        assert "a.py" in result.content

    @pytest.mark.asyncio
    async def test_glob_exception_returned(self, tool, tmp_path, monkeypatch):
        from pathlib import Path

        def boom(self, pattern):
            raise RuntimeError("glob went wrong")

        monkeypatch.setattr(Path, "glob", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "*.py", "path": str(tmp_path)}, context=context)
        assert result.is_error is True
        assert "glob went wrong" in result.content or "glob" in result.content.lower()


class TestGlobRendering:
    def test_render_tool_use_empty(self, tool):
        assert tool.render_tool_use_message({}) is None

    def test_render_tool_use_pattern_only(self, tool):
        msg = tool.render_tool_use_message({"pattern": "*.py"})
        assert '"*.py"' in msg

    def test_render_tool_use_pattern_and_path(self, tool):
        msg = tool.render_tool_use_message({"pattern": "*.py", "path": "/tmp"})
        assert '"*.py"' in msg
        assert '"/tmp"' in msg

    def test_render_tool_result_error_passthrough(self, tool):
        assert tool.render_tool_result_message("bad", is_error=True) == "bad"

    def test_render_tool_result_no_files(self, tool):
        msg = tool.render_tool_result_message("No files found")
        assert "0" in msg

    def test_render_tool_result_compact(self, tool):
        msg = tool.render_tool_result_message("a.py\nb.py")
        assert "2" in msg

    def test_render_tool_result_verbose_lists(self, tool):
        msg = tool.render_tool_result_message("a.py\nb.py", verbose=True)
        assert "a.py" in msg and "b.py" in msg

    def test_user_facing_name(self, tool):
        assert tool.user_facing_name() == "Search"

    def test_get_activity_description(self, tool):
        assert tool.get_activity_description({"pattern": "*.py"})
        assert tool.get_activity_description(None)

    def test_is_read_only(self, tool):
        assert tool.is_read_only() is True
