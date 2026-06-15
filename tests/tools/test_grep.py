"""Tests for the Grep tool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.grep import GrepTool, _is_rg_available, _python_grep


@pytest.fixture
def tool():
    return GrepTool()


class TestPythonGrep:
    def test_matches_files_with_matches(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello world\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("nothing\n", encoding="utf-8")
        out = _python_grep("hello", str(tmp_path))
        assert str(tmp_path / "a.txt") in out
        assert str(tmp_path / "b.txt") not in out

    def test_content_output_mode(self, tmp_path):
        (tmp_path / "a.txt").write_text("foo\nhello world\nbar\n", encoding="utf-8")
        out = _python_grep("hello", str(tmp_path), output_mode="content")
        assert "hello world" in out
        assert ":2:" in out

    def test_case_insensitive(self, tmp_path):
        (tmp_path / "a.txt").write_text("HELLO\n", encoding="utf-8")
        assert "a.txt" in _python_grep("hello", str(tmp_path), case_insensitive=True)
        assert _python_grep("hello", str(tmp_path), case_insensitive=False).strip() == ""

    def test_glob_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("hit\n", encoding="utf-8")
        (tmp_path / "a.txt").write_text("hit\n", encoding="utf-8")
        out = _python_grep("hit", str(tmp_path), glob="*.py")
        assert "a.py" in out
        assert "a.txt" not in out

    def test_glob_filter_matches_relative_paths(self, tmp_path):
        src = tmp_path / "src"
        package = src / "pkg"
        package.mkdir(parents=True)
        (src / "app.py").write_text("hit\n", encoding="utf-8")
        (package / "nested.py").write_text("hit\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("hit\n", encoding="utf-8")

        out = _python_grep("hit", str(tmp_path), glob="src/**/*.py")

        assert str(src / "app.py") in out
        assert str(package / "nested.py") in out
        assert str(tmp_path / "app.py") not in out

    def test_glob_filter_single_star_does_not_cross_directories(self, tmp_path):
        src = tmp_path / "src"
        package = src / "pkg"
        package.mkdir(parents=True)
        (src / "app.py").write_text("hit\n", encoding="utf-8")
        (package / "nested.py").write_text("hit\n", encoding="utf-8")

        out = _python_grep("hit", str(tmp_path), glob="src/*.py")

        assert str(src / "app.py") in out
        assert str(package / "nested.py") not in out

    def test_glob_filter_normalizes_windows_separators(self, tmp_path, monkeypatch):
        package = tmp_path / "src" / "pkg"
        package.mkdir(parents=True)
        (package / "app.py").write_text("hit\n", encoding="utf-8")

        monkeypatch.setattr(
            "iac_code.tools.grep.os.path.relpath",
            lambda _filepath, _path: "src\\pkg\\app.py",
        )

        out = _python_grep("hit", str(tmp_path), glob="src/**/*.py")

        assert str(package / "app.py") in out

    def test_invalid_regex_returns_error_message(self, tmp_path):
        out = _python_grep("[unclosed", str(tmp_path))
        assert "Invalid pattern" in out

    def test_max_results_limits_files(self, tmp_path):
        for i in range(5):
            (tmp_path / f"{i}.txt").write_text("hit\n", encoding="utf-8")
        out = _python_grep("hit", str(tmp_path), max_results=2)
        assert len(out.splitlines()) == 2

    def test_unreadable_file_skipped(self, tmp_path, monkeypatch):
        (tmp_path / "a.txt").write_text("hit\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("hit\n", encoding="utf-8")
        real_open = open

        def fail_for_b(path, *args, **kwargs):
            if "b.txt" in str(path):
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fail_for_b)
        out = _python_grep("hit", str(tmp_path))
        assert "a.txt" in out
        assert "b.txt" not in out


@pytest.mark.asyncio
class TestGrepExecute:
    async def test_pattern_found_with_python_fallback(self, tool, tmp_path, monkeypatch):
        (tmp_path / "f.txt").write_text("needle\n", encoding="utf-8")
        monkeypatch.setattr("iac_code.tools.grep._is_rg_available", lambda: False)
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "needle", "path": str(tmp_path)}, context=context)
        assert result.is_error is False
        assert "f.txt" in result.content

    async def test_no_matches_returns_message(self, tool, tmp_path, monkeypatch):
        (tmp_path / "f.txt").write_text("other\n", encoding="utf-8")
        monkeypatch.setattr("iac_code.tools.grep._is_rg_available", lambda: False)
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "needle", "path": str(tmp_path)}, context=context)
        assert result.content == "No matches"

    async def test_path_not_found(self, tool, tmp_path):
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(
            tool_input={"pattern": "x", "path": str(tmp_path / "missing")},
            context=context,
        )
        assert result.is_error is True
        assert "not found" in result.content.lower()

    async def test_windows_posix_path_conversion(self, tmp_path, tool, monkeypatch):
        (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "iac_code.tools.grep.normalize_user_path",
            MagicMock(side_effect=lambda raw: raw),
        )
        from iac_code.tools.base import ToolContext

        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(
            tool_input={"pattern": "hello", "path": str(tmp_path)},
            context=context,
        )
        assert result.is_error is False
        from iac_code.tools.grep import normalize_user_path

        normalize_user_path.assert_called_once_with(str(tmp_path))

    async def test_path_glob_with_rg_matches_relative_paths(self, tool, tmp_path):
        if not _is_rg_available():
            pytest.skip("rg is not available")

        src = tmp_path / "src"
        package = src / "pkg"
        package.mkdir(parents=True)
        (src / "app.py").write_text("hit\n", encoding="utf-8")
        (package / "nested.py").write_text("hit\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("hit\n", encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(
            tool_input={"pattern": "hit", "path": str(tmp_path), "glob": "src/**/*.py"},
            context=context,
        )

        assert result.is_error is False
        assert str(src / "app.py") in result.content
        assert str(package / "nested.py") in result.content
        assert str(tmp_path / "app.py") not in result.content


class TestGrepRendering:
    def test_render_tool_use_empty(self, tool):
        assert tool.render_tool_use_message({}) is None

    def test_render_tool_use_with_path(self, tool):
        msg = tool.render_tool_use_message({"pattern": "x", "path": "/tmp"})
        assert '"x"' in msg and '"/tmp"' in msg

    def test_render_tool_result_no_matches(self, tool):
        assert "0" in tool.render_tool_result_message("No matches")

    def test_render_tool_result_compact(self, tool):
        assert "2" in tool.render_tool_result_message("a.txt\nb.txt")

    def test_render_tool_result_verbose(self, tool):
        msg = tool.render_tool_result_message("a.txt\nb.txt", verbose=True)
        assert "a.txt" in msg and "b.txt" in msg

    def test_render_tool_result_error(self, tool):
        assert tool.render_tool_result_message("bad", is_error=True) == "bad"

    def test_user_facing_name(self, tool):
        assert tool.user_facing_name() == "Search"

    def test_get_activity_description(self, tool):
        assert tool.get_activity_description({"pattern": "x"})
        assert tool.get_activity_description(None)

    def test_is_read_only(self, tool):
        assert tool.is_read_only() is True
