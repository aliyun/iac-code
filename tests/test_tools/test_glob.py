"""Tests for the GlobTool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.glob import GlobTool


@pytest.fixture
def glob_tool():
    """Create a GlobTool instance."""
    return GlobTool()


class TestGlobTool:
    """Tests for GlobTool."""

    def test_tool_properties(self, glob_tool):
        """Test tool name, description, and schema."""
        assert glob_tool.name == "glob"
        assert "glob" in glob_tool.description.lower() or "pattern" in glob_tool.description.lower()
        assert "pattern" in glob_tool.input_schema["required"]
        assert "pattern" in glob_tool.input_schema["properties"]
        assert "path" in glob_tool.input_schema["properties"]

    def test_is_read_only(self, glob_tool):
        """Test that GlobTool is read-only."""
        assert glob_tool.is_read_only() is True

    @pytest.mark.asyncio
    async def test_match_py_files_recursive(self, tmp_path, glob_tool):
        """Test matching **/*.py files in a temp directory."""
        # Create some Python files
        (tmp_path / "main.py").write_text("# main")
        (tmp_path / "utils.py").write_text("# utils")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "helper.py").write_text("# helper")
        # Create a non-Python file
        (tmp_path / "readme.txt").write_text("readme")

        context = ToolContext(cwd=str(tmp_path))
        result = await glob_tool.execute(
            tool_input={"pattern": "**/*.py"},
            context=context,
        )

        assert result.is_error is False
        assert "main.py" in result.content
        assert "utils.py" in result.content
        assert "helper.py" in result.content
        assert "readme.txt" not in result.content

    @pytest.mark.asyncio
    async def test_match_files_in_subdirectory(self, tmp_path, glob_tool):
        """Test matching files in a specific subdirectory."""
        subdir = tmp_path / "src"
        subdir.mkdir()
        (subdir / "app.py").write_text("# app")
        (subdir / "config.py").write_text("# config")
        # File outside subdir should not match
        (tmp_path / "other.py").write_text("# other")

        context = ToolContext(cwd=str(tmp_path))
        result = await glob_tool.execute(
            tool_input={"pattern": "src/*.py"},
            context=context,
        )

        assert result.is_error is False
        assert "app.py" in result.content
        assert "config.py" in result.content

    @pytest.mark.asyncio
    async def test_no_matches_returns_no_files_found(self, tmp_path, glob_tool):
        """Test that no matches returns 'No files found'."""
        context = ToolContext(cwd=str(tmp_path))
        result = await glob_tool.execute(
            tool_input={"pattern": "**/*.nonexistent"},
            context=context,
        )

        assert result.is_error is False
        assert "No files found" in result.content

    @pytest.mark.asyncio
    async def test_search_with_path_parameter(self, tmp_path, glob_tool):
        """Test search with explicit path parameter."""
        search_dir = tmp_path / "search_root"
        search_dir.mkdir()
        (search_dir / "found.py").write_text("# found")
        # File outside search_dir
        (tmp_path / "outside.py").write_text("# outside")

        context = ToolContext(cwd=str(tmp_path))
        result = await glob_tool.execute(
            tool_input={"pattern": "**/*.py", "path": str(search_dir)},
            context=context,
        )

        assert result.is_error is False
        assert "found.py" in result.content
        assert "outside.py" not in result.content

    @pytest.mark.asyncio
    async def test_returns_relative_paths(self, tmp_path, glob_tool):
        """Test that results are relative paths."""
        (tmp_path / "file.py").write_text("# file")

        context = ToolContext(cwd=str(tmp_path))
        result = await glob_tool.execute(
            tool_input={"pattern": "**/*.py"},
            context=context,
        )

        assert result.is_error is False
        # Should not contain the full absolute tmp_path prefix
        lines = [line.strip() for line in result.content.splitlines() if line.strip()]
        for line in lines:
            assert not line.startswith("/"), f"Expected relative path, got: {line}"

    @pytest.mark.asyncio
    async def test_sorted_by_mtime_descending(self, tmp_path, glob_tool):
        """Test that results are sorted by mtime descending (newest first)."""
        import time

        old_file = tmp_path / "old.py"
        old_file.write_text("# old")
        time.sleep(0.01)
        new_file = tmp_path / "new.py"
        new_file.write_text("# new")

        context = ToolContext(cwd=str(tmp_path))
        result = await glob_tool.execute(
            tool_input={"pattern": "*.py"},
            context=context,
        )

        assert result.is_error is False
        old_pos = result.content.find("old.py")
        new_pos = result.content.find("new.py")
        # Newer file should appear first (lower position)
        assert new_pos < old_pos

    # UI rendering tests
    def test_render_tool_use_message(self, glob_tool):
        """Test render_tool_use_message returns a renderable."""
        result = glob_tool.render_tool_use_message({"pattern": "**/*.py"})
        assert result is not None

    def test_render_tool_result_message(self, glob_tool):
        """Test render_tool_result_message returns a renderable."""
        result = glob_tool.render_tool_result_message("file1.py\nfile2.py")
        assert result is not None

    def test_render_tool_result_message_error(self, glob_tool):
        """Test render_tool_result_message with error."""
        result = glob_tool.render_tool_result_message("Error occurred", is_error=True)
        assert result is not None

    def test_user_facing_name(self, glob_tool):
        """Test user_facing_name returns a string."""
        name = glob_tool.user_facing_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_get_activity_description_with_input(self, glob_tool):
        """Test get_activity_description with input."""
        desc = glob_tool.get_activity_description({"pattern": "**/*.py"})
        assert isinstance(desc, str)
        assert "**/*.py" in desc or "glob" in desc.lower() or "search" in desc.lower()

    def test_get_activity_description_without_input(self, glob_tool):
        """Test get_activity_description without input."""
        desc = glob_tool.get_activity_description()
        assert isinstance(desc, str)
        assert len(desc) > 0
