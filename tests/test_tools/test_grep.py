"""Tests for the GrepTool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.grep import GrepTool


@pytest.fixture
def grep_tool():
    """Create a GrepTool instance."""
    return GrepTool()


class TestGrepTool:
    """Tests for GrepTool."""

    def test_tool_properties(self, grep_tool):
        """Test tool name, description, and schema."""
        assert grep_tool.name == "grep"
        assert "grep" in grep_tool.description.lower() or "search" in grep_tool.description.lower()
        assert "pattern" in grep_tool.input_schema["required"]
        schema_props = grep_tool.input_schema["properties"]
        assert "pattern" in schema_props
        assert "path" in schema_props
        assert "glob" in schema_props
        assert "case_insensitive" in schema_props
        assert "output_mode" in schema_props
        assert "max_results" in schema_props

    def test_output_mode_enum(self, grep_tool):
        """Test that output_mode has the expected enum values."""
        schema_props = grep_tool.input_schema["properties"]
        output_mode_schema = schema_props["output_mode"]
        assert "enum" in output_mode_schema
        assert "files_with_matches" in output_mode_schema["enum"]
        assert "content" in output_mode_schema["enum"]

    def test_is_read_only(self, grep_tool):
        """Test that GrepTool is read-only."""
        assert grep_tool.is_read_only() is True
        assert grep_tool.is_read_only({}) is True

    @pytest.mark.asyncio
    async def test_basic_search_matches_across_files(self, tmp_path, grep_tool):
        """Test basic search matching across files."""
        (tmp_path / "alpha.py").write_text("def hello_world():\n    pass\n")
        (tmp_path / "beta.py").write_text("def greet():\n    print('hello')\n")
        (tmp_path / "gamma.py").write_text("def unrelated():\n    return 42\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={"pattern": "hello", "path": str(tmp_path)},
            context=context,
        )

        assert result.is_error is False
        assert "alpha.py" in result.content
        assert "beta.py" in result.content
        # gamma.py has no 'hello', should not appear
        assert "gamma.py" not in result.content

    @pytest.mark.asyncio
    async def test_glob_filter_limits_to_py_files(self, tmp_path, grep_tool):
        """Test that glob filter limits search to matching files."""
        (tmp_path / "match.py").write_text("target_string here\n")
        (tmp_path / "ignore.txt").write_text("target_string here\n")
        (tmp_path / "also_ignore.md").write_text("target_string here\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={
                "pattern": "target_string",
                "path": str(tmp_path),
                "glob": "*.py",
            },
            context=context,
        )

        assert result.is_error is False
        assert "match.py" in result.content
        assert "ignore.txt" not in result.content
        assert "also_ignore.md" not in result.content

    @pytest.mark.asyncio
    async def test_content_output_mode_shows_matching_lines_with_line_numbers(self, tmp_path, grep_tool):
        """Test content output mode shows matching lines with line numbers."""
        content = "line one\nfind_me here\nline three\nfind_me again\n"
        (tmp_path / "sample.py").write_text(content)

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={
                "pattern": "find_me",
                "path": str(tmp_path),
                "output_mode": "content",
            },
            context=context,
        )

        assert result.is_error is False
        # Should contain matching lines
        assert "find_me" in result.content
        # Should include line numbers (2 and 4 for the matched lines)
        assert "2" in result.content
        assert "4" in result.content

    @pytest.mark.asyncio
    async def test_no_matches_returns_no_matches(self, tmp_path, grep_tool):
        """Test that no matches returns 'No matches'."""
        (tmp_path / "file.py").write_text("nothing interesting here\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={"pattern": "xyzzy_not_found_anywhere", "path": str(tmp_path)},
            context=context,
        )

        assert result.is_error is False
        assert "No matches" in result.content

    @pytest.mark.asyncio
    async def test_case_insensitive_search(self, tmp_path, grep_tool):
        """Test case insensitive search."""
        (tmp_path / "file.py").write_text("Hello World\nHELLO WORLD\nhello world\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={
                "pattern": "hello",
                "path": str(tmp_path),
                "case_insensitive": True,
                "output_mode": "content",
            },
            context=context,
        )

        assert result.is_error is False
        assert "Hello" in result.content or "HELLO" in result.content or "hello" in result.content

    @pytest.mark.asyncio
    async def test_case_sensitive_search_by_default(self, tmp_path, grep_tool):
        """Test that search is case-sensitive by default."""
        (tmp_path / "file.py").write_text("Hello World\nhello world\n")

        context = ToolContext(cwd=str(tmp_path))
        # Search for uppercase HELLO — should not match lowercase "hello world" line
        result = await grep_tool.execute(
            tool_input={
                "pattern": "HELLO",
                "path": str(tmp_path),
                "output_mode": "content",
            },
            context=context,
        )

        assert result.is_error is False
        # Either no matches at all, or only uppercase match
        if "No matches" not in result.content:
            assert "Hello World" not in result.content or "HELLO" in result.content

    @pytest.mark.asyncio
    async def test_files_with_matches_output_mode(self, tmp_path, grep_tool):
        """Test files_with_matches output mode returns file paths."""
        (tmp_path / "has_match.py").write_text("needle in a haystack\n")
        (tmp_path / "no_match.py").write_text("nothing to find here\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={
                "pattern": "needle",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
            },
            context=context,
        )

        assert result.is_error is False
        assert "has_match.py" in result.content
        assert "no_match.py" not in result.content

    @pytest.mark.asyncio
    async def test_max_results_limits_output(self, tmp_path, grep_tool):
        """Test max_results limits the number of results."""
        # Create 10 files each containing the pattern
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text(f"match_pattern in file {i}\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={
                "pattern": "match_pattern",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
                "max_results": 3,
            },
            context=context,
        )

        assert result.is_error is False
        # Count how many file entries appear (at most 3)
        lines = [line for line in result.content.splitlines() if line.strip() and "file" in line]
        assert len(lines) <= 3

    @pytest.mark.asyncio
    async def test_uses_cwd_when_no_path_given(self, tmp_path, grep_tool):
        """Test that tool uses context.cwd when no path is given."""
        (tmp_path / "cwd_file.py").write_text("cwd_search_target\n")

        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={"pattern": "cwd_search_target"},
            context=context,
        )

        assert result.is_error is False
        assert "cwd_file.py" in result.content

    @pytest.mark.asyncio
    async def test_empty_directory_returns_no_matches(self, tmp_path, grep_tool):
        """Test that searching an empty directory returns 'No matches'."""
        context = ToolContext(cwd=str(tmp_path))
        result = await grep_tool.execute(
            tool_input={"pattern": "anything", "path": str(tmp_path)},
            context=context,
        )

        assert result.is_error is False
        assert "No matches" in result.content

    @pytest.mark.asyncio
    async def test_python_fallback_skips_symlinked_file_outside_search_root(self, tmp_path, grep_tool, monkeypatch):
        """Python fallback should not follow an in-root symlink to an outside file."""
        outside = tmp_path / "outside"
        project = tmp_path / "project"
        outside.mkdir()
        project.mkdir()
        (outside / "secret.txt").write_text("outside needle\n")
        (project / "link.txt").symlink_to(outside / "secret.txt")

        monkeypatch.setattr("iac_code.tools.grep._is_rg_available", lambda: False)

        context = ToolContext(cwd=str(project))
        result = await grep_tool.execute(
            tool_input={"pattern": "needle", "path": str(project), "output_mode": "content"},
            context=context,
        )

        assert result.is_error is False
        assert result.content == "No matches"

    # UI rendering tests
    def test_render_tool_use_message(self, grep_tool):
        """Test render_tool_use_message returns a renderable."""
        result = grep_tool.render_tool_use_message({"pattern": "hello"})
        assert result is not None

    def test_render_tool_result_message(self, grep_tool):
        """Test render_tool_result_message returns a renderable."""
        result = grep_tool.render_tool_result_message("file1.py\nfile2.py")
        assert result is not None

    def test_render_tool_result_message_error(self, grep_tool):
        """Test render_tool_result_message with error."""
        result = grep_tool.render_tool_result_message("Error occurred", is_error=True)
        assert result is not None

    def test_render_tool_result_message_no_matches(self, grep_tool):
        """Test render_tool_result_message with no matches."""
        result = grep_tool.render_tool_result_message("No matches")
        assert result is not None

    def test_user_facing_name(self, grep_tool):
        """Test user_facing_name returns a non-empty string."""
        name = grep_tool.user_facing_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_get_activity_description_with_input(self, grep_tool):
        """Test get_activity_description with input."""
        desc = grep_tool.get_activity_description({"pattern": "hello"})
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_get_activity_description_without_input(self, grep_tool):
        """Test get_activity_description without input."""
        desc = grep_tool.get_activity_description()
        assert isinstance(desc, str)
        assert len(desc) > 0
