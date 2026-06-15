"""Tests for the EditFile tool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.edit_file import EditFileTool


@pytest.fixture
def edit_file_tool():
    """Create an EditFile tool instance."""
    return EditFileTool()


class TestEditFileTool:
    """Tests for EditFileTool."""

    def test_tool_properties(self, edit_file_tool):
        """Test tool name, description, and schema."""
        assert edit_file_tool.name == "edit_file"
        assert "edit" in edit_file_tool.description.lower()
        assert set(edit_file_tool.input_schema["required"]) == {"path", "old_string", "new_string"}

    @pytest.mark.asyncio
    async def test_normal_search_replace(self, tmp_path, edit_file_tool):
        """Test normal search and replace operation."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("Hello, world!\nThis is a test.\n", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": str(file_path),
                "old_string": "Hello, world!",
                "new_string": "Goodbye, world!",
            },
            context=context,
        )

        assert result.is_error is False
        assert "successfully" in result.content.lower()
        assert file_path.read_text(encoding="utf-8") == "Goodbye, world!\nThis is a test.\n"

    @pytest.mark.asyncio
    async def test_old_string_not_found(self, tmp_path, edit_file_tool):
        """Test error when old_string is not found."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("Some content here", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": str(file_path),
                "old_string": "nonexistent text",
                "new_string": "replacement",
            },
            context=context,
        )

        assert result.is_error is True
        assert "not found" in result.content.lower()
        assert file_path.read_text(encoding="utf-8") == "Some content here"

    @pytest.mark.asyncio
    async def test_old_string_multiple_matches(self, tmp_path, edit_file_tool):
        """Test error when old_string matches multiple times."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello hello hello", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": str(file_path),
                "old_string": "hello",
                "new_string": "world",
            },
            context=context,
        )

        assert result.is_error is True
        assert "3 times" in result.content
        assert file_path.read_text(encoding="utf-8") == "hello hello hello"

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path, edit_file_tool):
        """Test error when file does not exist."""
        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": "/nonexistent/file.txt",
                "old_string": "old",
                "new_string": "new",
            },
            context=context,
        )

        assert result.is_error is True
        assert "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_replace_multiline(self, tmp_path, edit_file_tool):
        """Test replacing multi-line content."""
        file_path = tmp_path / "test.txt"
        original = "def foo():\n    pass\n"
        file_path.write_text(original, encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": str(file_path),
                "old_string": "def foo():\n    pass",
                "new_string": "def foo():\n    return 42",
            },
            context=context,
        )

        assert result.is_error is False
        assert file_path.read_text(encoding="utf-8") == "def foo():\n    return 42\n"

    @pytest.mark.asyncio
    async def test_relative_path(self, tmp_path, edit_file_tool):
        """Test editing with relative path."""
        file_path = tmp_path / "relative.txt"
        file_path.write_text("old content", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": "relative.txt",
                "old_string": "old content",
                "new_string": "new content",
            },
            context=context,
        )

        assert result.is_error is False
        assert file_path.read_text(encoding="utf-8") == "new content"

    @pytest.mark.asyncio
    async def test_replace_with_empty_string(self, tmp_path, edit_file_tool):
        """Test replacing content with empty string (deletion)."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("Hello, world!", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": str(file_path),
                "old_string": ", world",
                "new_string": "",
            },
            context=context,
        )

        assert result.is_error is False
        assert file_path.read_text(encoding="utf-8") == "Hello!"

    @pytest.mark.asyncio
    async def test_replace_preserves_whitespace(self, tmp_path, edit_file_tool):
        """Test that whitespace in old_string is matched exactly."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("    indented line\n", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": str(file_path),
                "old_string": "    indented line",
                "new_string": "  less indented",
            },
            context=context,
        )

        assert result.is_error is False
        assert file_path.read_text(encoding="utf-8") == "  less indented\n"

    @pytest.mark.asyncio
    async def test_unique_match_with_context(self, tmp_path, edit_file_tool):
        """Test that including more context makes a unique match."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello\nworld hello\nhello world\n", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={
                "path": str(file_path),
                "old_string": "hello\nworld hello",
                "new_string": "REPLACED\nworld hello",
            },
            context=context,
        )

        assert result.is_error is False
        assert file_path.read_text(encoding="utf-8") == "REPLACED\nworld hello\nhello world\n"

    @pytest.mark.asyncio
    async def test_windows_posix_path_conversion(self, tmp_path, edit_file_tool, monkeypatch):
        target = tmp_path / "edit.txt"
        target.write_text("foo bar", encoding="utf-8")
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "iac_code.tools.edit_file.normalize_user_path",
            MagicMock(side_effect=lambda raw: raw),
        )
        from iac_code.tools.base import ToolContext

        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={"path": str(target), "old_string": "foo", "new_string": "baz"},
            context=context,
        )
        assert result.is_error is False
        from iac_code.tools.edit_file import normalize_user_path

        normalize_user_path.assert_called_once_with(str(target))

    @pytest.mark.asyncio
    async def test_read_error_returned(self, tmp_path, edit_file_tool, monkeypatch):
        file_path = tmp_path / "x.txt"
        file_path.write_text("abc", encoding="utf-8")

        def boom(*a, **kw):
            raise OSError("boom-read")

        monkeypatch.setattr("builtins.open", boom)
        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={"path": str(file_path), "old_string": "a", "new_string": "b"},
            context=context,
        )
        assert result.is_error is True
        assert "boom-read" in result.content or "reading" in result.content.lower()

    @pytest.mark.asyncio
    async def test_write_error_returned(self, tmp_path, edit_file_tool, monkeypatch):
        file_path = tmp_path / "x.txt"
        file_path.write_text("abc", encoding="utf-8")

        real_open = open
        calls = {"count": 0}

        def fail_on_write(path, *args, **kwargs):
            calls["count"] += 1
            mode = args[0] if args else kwargs.get("mode", "r")
            if "w" in mode:
                raise OSError("boom-write")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fail_on_write)
        context = ToolContext(cwd=str(tmp_path))
        result = await edit_file_tool.execute(
            tool_input={"path": str(file_path), "old_string": "a", "new_string": "b"},
            context=context,
        )
        assert result.is_error is True
        assert "writing" in result.content.lower() or "boom-write" in result.content


class TestEditFileRendering:
    def test_normalize_input_aliases_file_path(self, edit_file_tool):
        inp = {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"}
        edit_file_tool.normalize_input(inp)
        assert "path" in inp and inp["path"] == "/a/b.py"
        assert "file_path" not in inp

    def test_normalize_input_noop_when_path_already_set(self, edit_file_tool):
        inp = {"path": "/a.py", "file_path": "/b.py", "old_string": "", "new_string": ""}
        edit_file_tool.normalize_input(inp)
        assert inp["path"] == "/a.py"
        assert inp.get("file_path") == "/b.py"

    def test_render_tool_use_empty_path_returns_none(self, edit_file_tool):
        assert edit_file_tool.render_tool_use_message({}) is None

    def test_render_tool_use_compact_returns_path(self, edit_file_tool):
        assert edit_file_tool.render_tool_use_message({"path": "/a.py"}) == "/a.py"

    def test_render_tool_use_verbose_short_old(self, edit_file_tool):
        msg = edit_file_tool.render_tool_use_message(
            {"path": "/a.py", "old_string": "abc", "new_string": "xyz"},
            verbose=True,
        )
        assert "/a.py" in msg and "abc" in msg

    def test_render_tool_use_verbose_long_old_truncated(self, edit_file_tool):
        long_old = "x" * 120
        msg = edit_file_tool.render_tool_use_message(
            {"path": "/a.py", "old_string": long_old, "new_string": "y"},
            verbose=True,
        )
        assert "…" in msg
        assert "/a.py" in msg

    def test_render_tool_use_verbose_replaces_newlines(self, edit_file_tool):
        msg = edit_file_tool.render_tool_use_message(
            {"path": "/a.py", "old_string": "line1\nline2", "new_string": ""},
            verbose=True,
        )
        assert "↵" in msg
        assert "\n" not in msg

    def test_render_tool_result_compact_first_line_only(self, edit_file_tool):
        out = "Successfully edited /a.py\nMore detail"
        assert edit_file_tool.render_tool_result_message(out) == "Successfully edited /a.py"

    def test_render_tool_result_verbose_full(self, edit_file_tool):
        out = "Line1\nLine2"
        assert edit_file_tool.render_tool_result_message(out, verbose=True) == "Line1\nLine2"

    def test_user_facing_name_create_for_empty_old(self, edit_file_tool):
        assert edit_file_tool.user_facing_name({"old_string": ""}) == "Create"

    def test_user_facing_name_update_for_nonempty_old(self, edit_file_tool):
        assert edit_file_tool.user_facing_name({"old_string": "x"}) == "Update"

    def test_user_facing_name_neutral_when_old_string_missing(self, edit_file_tool):
        """During streaming `old_string` may not have arrived yet; fall back to
        a neutral name so a Create operation doesn't briefly render as Update."""
        assert edit_file_tool.user_facing_name(None) == "Edit"
        assert edit_file_tool.user_facing_name({"path": "/f.py"}) == "Edit"

    def test_get_activity_description_with_input(self, edit_file_tool):
        msg = edit_file_tool.get_activity_description({"path": "/f.py"})
        assert "/f.py" in msg

    def test_get_activity_description_default(self, edit_file_tool):
        msg = edit_file_tool.get_activity_description(None)
        assert msg  # non-empty fallback

    def test_is_destructive_always_true(self, edit_file_tool):
        assert edit_file_tool.is_destructive() is True
        assert edit_file_tool.is_destructive({"path": "/a"}) is True
