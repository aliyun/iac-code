"""Read-only logical Wiki paths across tools, permissions and saved references."""

from __future__ import annotations

import shutil

import pytest

from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.services.token_counter import TokenCounter
from iac_code.services.wiki_auto_search import reference, search
from iac_code.tools import grep, path_safety
from iac_code.tools.base import ToolContext
from iac_code.tools.edit_file import EditFileTool
from iac_code.tools.glob import GlobTool
from iac_code.tools.grep import GrepTool
from iac_code.tools.path_safety import check_write_path, knowledge_uri, resolve_read_path
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    app = tmp_path / "installed app 中文"
    root = app / "knowledge"
    (root / "common/hardware").mkdir(parents=True)
    (root / "common/hardware/facts.md").write_text("# Hardware\nTDX: source:payload\n", encoding="utf-8")
    workspace = tmp_path / "unrelated workspace"
    workspace.mkdir()
    monkeypatch.setattr(path_safety, "get_iac_code_application_root", lambda: app)
    return app, root, workspace


@pytest.mark.parametrize("safe", [False, True])
@pytest.mark.asyncio
async def test_logical_paths_roundtrip_between_three_tools_and_keep_line_numbers(wiki, monkeypatch, safe):
    app, root, workspace = wiki
    permission = ToolPermissionContext(
        cwd=str(workspace), strict_read_directories=[str(workspace), str(app)] if safe else [],
    )
    context = ToolContext(cwd=str(workspace), strict_read_directories=permission.strict_read_directories)
    arguments = {"path": "knowledge://common", "pattern": "**/*.md"}
    glob = GlobTool()
    assert (await glob.check_permissions(arguments, permission)).behavior == "allow"
    paths = await glob.execute(tool_input=arguments, context=context)
    assert paths.content == "knowledge://common/hardware/facts.md"
    for native in ([False, True] if shutil.which("rg") else [False]):
        monkeypatch.setattr(grep, "_is_rg_available", lambda: native)
        tool = GrepTool()
        for mode in ("files_with_matches", "content"):
            arguments = {"path": "knowledge://", "pattern": "TDX", "output_mode": mode}
            assert (await tool.check_permissions(arguments, permission)).behavior == "allow"
            found = await tool.execute(tool_input=arguments, context=context)
            expected = paths.content + (":2:TDX: source:payload" if mode == "content" else "")
            assert found.content == expected
        arguments = {"path": paths.content, "start_line": 2, "end_line": 2}
        assert (await ReadFileTool().check_permissions(arguments, permission)).behavior == "allow"
        body = await ReadFileTool().execute(tool_input=arguments, context=context)
        assert body.content.startswith("File: knowledge://common/hardware/facts.md (lines 2-2 of 2)")
        assert "     2\tTDX: source:payload" in body.content
    assert resolve_read_path("knowledge://common\\hardware\\facts.md", str(workspace)) == str(
        (root / "common/hardware/facts.md").resolve()
    )


@pytest.mark.parametrize("path", [
    "knowledge://../outside.md", "knowledge://common/../../outside.md", "knowledge:///etc/passwd",
    "knowledge://C:/Windows/file", "knowledge://common\\..\\..\\outside.md", "knowledge://%2e%2e/file",
    "knowledge://file?query", "knowledge://file\0.md", "knowledge://file\n.md",
])
@pytest.mark.asyncio
async def test_invalid_logical_paths_never_fall_back_to_cwd_or_broad_trusted_roots(wiki, path):
    app, _, workspace = wiki
    permission = ToolPermissionContext(cwd=str(workspace), trusted_read_directories=[str(app.parent)])
    context = ToolContext(cwd=str(workspace), trusted_read_directories=[str(app.parent)])
    for tool in (ReadFileTool(), GrepTool(), GlobTool()):
        arguments = {"path": path, "pattern": "*"}
        assert (await tool.check_permissions(arguments, permission)).behavior == "deny"
        assert (await tool.execute(tool_input=arguments, context=context)).is_error


@pytest.mark.asyncio
async def test_namespace_symlinks_cannot_escape_even_into_other_application_files(wiki, monkeypatch):
    app, root, workspace = wiki
    outside = app / "outside"
    outside.mkdir()
    (outside / "external.md").write_text("TDX outside namespace", encoding="utf-8")
    (root / "leak.md").symlink_to(outside / "external.md")
    (root / "outside-dir").symlink_to(outside, target_is_directory=True)
    permission = ToolPermissionContext(cwd=str(workspace), trusted_read_directories=[str(app)])
    context = ToolContext(cwd=str(workspace), trusted_read_directories=[str(app)])
    for tool in (ReadFileTool(), GrepTool(), GlobTool()):
        arguments = {"path": "knowledge://outside-dir", "pattern": "*.md"}
        assert (await tool.check_permissions(arguments, permission)).behavior == "deny"
        assert (await tool.execute(tool_input=arguments, context=context)).is_error
    assert (await ReadFileTool().check_permissions({"path": "knowledge://leak.md"}, permission)).behavior == "deny"
    for native in ([False, True] if shutil.which("rg") else [False]):
        monkeypatch.setattr(grep, "_is_rg_available", lambda: native)
        found = await GrepTool().execute(tool_input={"path": "knowledge://", "pattern": "TDX"}, context=context)
        assert found.content == "knowledge://common/hardware/facts.md"
    for pattern in ("*.md", "**/*.md"):
        found = await GlobTool().execute(tool_input={"path": "knowledge://", "pattern": pattern}, context=context)
        assert "leak.md" not in found.content and "external.md" not in found.content


@pytest.mark.asyncio
async def test_strict_read_permissions_and_ordinary_paths_are_preserved(wiki):
    _, root, workspace = wiki
    denied = ToolPermissionContext(cwd=str(workspace), strict_read_directories=[str(workspace)])
    for tool in (ReadFileTool(), GrepTool(), GlobTool()):
        decision = await tool.check_permissions({"path": "knowledge://common", "pattern": "*"}, denied)
        assert decision.behavior != "allow"
    ordinary = workspace / "note.md"
    ordinary.write_text("ordinary body", encoding="utf-8")
    body = await ReadFileTool().execute(tool_input={"path": "note.md"}, context=ToolContext(cwd=str(workspace)))
    assert body.content.startswith(f"File: {ordinary}") and "ordinary body" in body.content
    assert check_write_path("knowledge://common/hardware/facts.md", cwd=str(workspace),
                            additional_directories=[str(root)]).behavior == "deny"
    assert check_write_path(str(ordinary), cwd=str(workspace), additional_directories=[]).behavior == "allow"
    result = await GlobTool().execute(tool_input={"path": "knowledge://", "pattern": "../*.md"},
                                      context=ToolContext(cwd=str(workspace)))
    assert result.is_error


@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.BYPASS_PERMISSIONS])
@pytest.mark.parametrize("tool", [WriteFileTool(), EditFileTool()], ids=["write", "edit"])
@pytest.mark.asyncio
async def test_logical_wiki_writes_are_denied_even_with_tool_authorization(wiki, tool, mode):
    _, root, workspace = wiki
    arguments = {
        "path": "knowledge://common/hardware/facts.md", "content": "overwritten",
        "old_string": "TDX", "new_string": "overwritten",
    }
    permission = ToolPermissionContext(
        cwd=str(workspace), mode=mode, additional_directories=[str(root)],
        allow_rules={"project_settings": [tool.name]},
    )
    assert (await tool.check_permissions(arguments, permission)).behavior == "deny"
    assert (await check_tool_permission(tool, arguments, permission)).behavior == "deny"
    result = await tool.execute(tool_input=arguments, context=ToolContext(cwd=str(workspace)))
    assert result.is_error
    assert not list(workspace.iterdir())
    assert (root / "common/hardware/facts.md").read_text(encoding="utf-8") == "# Hardware\nTDX: source:payload\n"

    # Existing blanket write permission must still work for ordinary project files.
    ordinary = workspace / "ordinary.md"
    ordinary.write_text("TDX", encoding="utf-8")
    arguments["path"] = str(ordinary)
    assert (await check_tool_permission(tool, arguments, permission)).behavior == "allow"
    assert not (await tool.execute(tool_input=arguments, context=ToolContext(cwd=str(workspace)))).is_error
    assert ordinary.read_text(encoding="utf-8") == "overwritten"


@pytest.mark.asyncio
async def test_recall_keeps_relative_snapshots_and_resolves_after_installation_moves(wiki, monkeypatch, tmp_path):
    _, root, workspace = wiki
    context = ToolContext(cwd=str(workspace))
    hits = await search(root, ["TDX"], context, None)
    assert hits and hits[0]["path"] == "common/hardware/facts.md"
    snapshot = {"status": "ready", "keywords": ["TDX"], "hits": hits}
    block = reference(snapshot, root, counter=TokenCounter(), max_tokens=1200)
    assert "# knowledge://common/hardware/facts.md" in block and str(root) not in block
    relocated = tmp_path / "new installation"
    shutil.copytree(root, relocated / "knowledge")
    monkeypatch.setattr(path_safety, "get_iac_code_application_root", lambda: relocated)
    restored = reference(snapshot, relocated / "knowledge", counter=TokenCounter(), max_tokens=1200)
    assert restored == block
    assert knowledge_uri(relocated / "knowledge/common/hardware/facts.md") == "knowledge://common/hardware/facts.md"
    body = await ReadFileTool().execute(tool_input={"path": "knowledge://common/hardware/facts.md"}, context=context)
    assert not body.is_error and "source:payload" in body.content
