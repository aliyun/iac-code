"""Tests for the Glob tool."""

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.glob import GlobTool
from iac_code.types.permissions import ToolPermissionContext


@pytest.fixture
def tool():
    return GlobTool()


class TestGlobBasics:
    def test_tool_properties(self, tool):
        assert tool.name == "glob"
        assert tool.input_schema["required"] == ["pattern"]

    @pytest.mark.asyncio
    async def test_match_py_files(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "b.py").write_text("", encoding="utf-8")
        (tmp_path / "c.txt").write_text("", encoding="utf-8")
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
        file_path.write_text("x", encoding="utf-8")
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "*.py", "path": str(file_path)}, context=context)
        assert result.is_error is True
        assert "not a directory" in result.content.lower()

    @pytest.mark.asyncio
    async def test_relative_path_resolved(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(tool_input={"pattern": "*.py", "path": "."}, context=context)
        assert result.is_error is False
        assert "a.py" in result.content

    @pytest.mark.asyncio
    async def test_relative_path_falls_back_to_symlinked_relative_read_directory(self, tool, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        package_root = tmp_path / "iac_code"
        skill_root = package_root / "pipeline" / "selling" / "skills" / "iac-aliyun-cost"
        selling_refs = package_root / "pipeline" / "selling" / "references"
        skill_root.mkdir(parents=True)
        selling_refs.mkdir(parents=True)
        (selling_refs / "template-parameter-recommendation.md").write_text("pipeline", encoding="utf-8")
        try:
            (skill_root / "references").symlink_to(selling_refs, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Cannot create symlink on this platform: {exc}")

        context = ToolContext(
            cwd=str(workspace),
            trusted_read_directories=[str(package_root)],
            relative_read_directories=[str(skill_root)],
        )
        result = await tool.execute(tool_input={"pattern": "*.md", "path": "references"}, context=context)

        assert result.is_error is False
        assert "template-parameter-recommendation.md" in result.content

    @pytest.mark.asyncio
    async def test_recursive_glob_follows_symlinked_reference_directory(self, tool, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        package_root = tmp_path / "iac_code"
        skill_root = package_root / "pipeline" / "selling" / "skills" / "iac-aliyun-cost"
        selling_refs = package_root / "pipeline" / "selling" / "references"
        bundled_products = package_root / "skills" / "bundled" / "iac_aliyun" / "references" / "cloud-products"
        skill_root.mkdir(parents=True)
        selling_refs.mkdir(parents=True)
        bundled_products.mkdir(parents=True)
        (selling_refs / "template-parameter-recommendation.md").write_text("pipeline", encoding="utf-8")
        (bundled_products / "ecs.md").write_text("ecs", encoding="utf-8")
        try:
            (skill_root / "references").symlink_to(selling_refs, target_is_directory=True)
            (selling_refs / "cloud-products").symlink_to(bundled_products, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Cannot create symlink on this platform: {exc}")

        context = ToolContext(
            cwd=str(workspace),
            trusted_read_directories=[str(package_root)],
            relative_read_directories=[str(skill_root)],
        )
        result = await tool.execute(tool_input={"pattern": "**/*.md", "path": "references"}, context=context)

        assert result.is_error is False
        assert "template-parameter-recommendation.md" in result.content
        assert "cloud-products/ecs.md" in result.content.replace("\\", "/")

    @pytest.mark.asyncio
    async def test_recursive_glob_permission_asks_for_symlink_escape(self, tool, tmp_path):
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "secret.md").write_text("secret", encoding="utf-8")
        try:
            (workspace / "outside").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Cannot create symlink on this platform: {exc}")

        context = ToolPermissionContext(cwd=str(workspace))
        result = await tool.check_permissions({"pattern": "**/*.md", "path": "."}, context)

        assert result.behavior == "ask"
        assert "outside allowed directories" in result.message

    @pytest.mark.asyncio
    async def test_strict_roots_do_not_follow_symlink_into_additional_directory(self, tool, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("classified", encoding="utf-8")
        try:
            (project / "link-outside").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Cannot create symlink on this platform: {exc}")

        context = ToolContext(
            cwd=str(project),
            additional_directories=[str(outside)],
            strict_read_directories=[str(project)],
            read_path_violation_behavior="deny",
        )
        result = await tool.execute(tool_input={"pattern": "**/*.txt", "path": str(project)}, context=context)

        assert result.is_error is False
        assert result.content == "No files found"
        assert "secret.txt" not in result.content

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

    @pytest.mark.asyncio
    async def test_windows_posix_path_conversion(self, tmp_path, tool, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "iac_code.tools.glob.normalize_user_path",
            MagicMock(side_effect=lambda raw: raw),
        )
        from iac_code.tools.base import ToolContext

        context = ToolContext(cwd=str(tmp_path))
        result = await tool.execute(
            tool_input={"pattern": "*.txt", "path": str(tmp_path)},
            context=context,
        )
        assert result.is_error is False
        from iac_code.tools.glob import normalize_user_path

        normalize_user_path.assert_any_call(str(tmp_path))


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
