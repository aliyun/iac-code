"""Tool output must remain complete and readable without flooding model context."""

import json
import re
import shutil
from pathlib import Path

import pytest

from iac_code.mcp.output import convert_mcp_tool_result
from iac_code.tools.base import ToolContext
from iac_code.tools.grep import GrepTool
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.result_storage import ResultStorage, resolve_result_reference
from iac_code.types.permissions import ToolPermissionContext


@pytest.mark.parametrize("size", [19_999, 20_000, 20_001])
def test_inline_boundary_and_lossless_file(tmp_path, size):
    original = "中" * size
    processed = ResultStorage(str(tmp_path)).process("boundary", original, tool_name="bash", is_error=True)
    assert processed.is_externalized == (size > 20_000)
    if size > 20_000:
        assert original[:100] not in processed.content
        assert "Status: error" in processed.content
        assert Path(processed.file_path).read_text(encoding="utf-8") == original
    else:
        assert processed.content == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        "中文🙂\r\n" * 10_000,
        json.dumps({"value": "单行🙂" * 20_000}, ensure_ascii=False),
        "".join(f"row {i}: 中文\n" for i in range(20_000)),
    ],
    ids=["unicode-crlf", "long-json-line", "many-lines"],
)
async def test_character_pages_reconstruct_complete_original(tmp_path, original):
    storage = ResultStorage(str(tmp_path / "results"))
    storage.process("large", original)
    context = ToolContext(cwd=str(tmp_path), tool_result_directory=storage.storage_dir)
    query = {"path": "tool-result://large.txt", "start_char": 0}
    chunks = []
    for _ in range(100):
        result = await ReadFileTool().execute(tool_input=query, context=context)
        assert not result.is_error, result.content
        assert len(result.content) <= 20_000
        assert not storage.process("page", result.content).is_externalized
        page = result.metadata["file_page"]
        # The exact source slice appears in the response; metadata alone is not enough.
        expected = original[page["start_char"] : page["end_char"]]
        assert expected in result.content
        chunks.append(expected)
        if not page["has_more"]:
            break
        assert expected
        query["cursor"] = page["next_cursor"]
    else:
        pytest.fail("Pagination did not reach EOF.")
    assert "".join(chunks) == original


@pytest.mark.asyncio
async def test_line_range_continues_within_a_long_line(tmp_path):
    original = "before\n" + "x" * 80_000 + "\nafter\n"
    path = tmp_path / "long.txt"
    path.write_text(original, encoding="utf-8")
    query = {"path": str(path), "start_line": 2, "end_line": 2}
    end_offsets = []
    while True:
        result = await ReadFileTool().execute(tool_input=query, context=ToolContext(cwd=str(tmp_path)))
        assert not result.is_error
        assert len(result.content) <= 20_000
        assert "before" not in result.content and "after" not in result.content
        page = result.metadata["file_page"]
        end_offsets.append(page["end_char"])
        if not page["has_more"]:
            break
        query["cursor"] = page["next_cursor"]
    assert end_offsets == sorted(set(end_offsets))
    assert end_offsets[-1] == len("before\n" + "x" * 80_000 + "\n")


@pytest.mark.asyncio
async def test_relocated_session_reference_and_cross_session_isolation(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    ResultStorage(str(source)).process("r1", "private-result\n" * 4_000)
    shutil.copytree(source, restored)
    shutil.rmtree(source)
    tool = ReadFileTool()
    reference = "tool-result://r1.txt"
    permission = await tool.check_permissions(
        {"path": reference},
        ToolPermissionContext(tool_result_directory=str(restored)),
    )
    assert permission.behavior == "allow"
    result = await tool.execute(
        tool_input={"path": reference, "start_line": 1, "end_line": 1},
        context=ToolContext(tool_result_directory=str(restored)),
    )
    assert "private-result" in result.content
    permission = await tool.check_permissions(
        {"path": reference},
        ToolPermissionContext(tool_result_directory=str(tmp_path / "other")),
    )
    assert permission.behavior == "deny"


@pytest.mark.parametrize(
    "reference",
    [
        "tool-result://../secret",
        "tool-result:///etc/passwd",
        "tool-result://a/../../secret",
        "tool-result://a\\secret",
        "tool-result://C:/secret",
        "tool-result://",
    ],
)
def test_unsafe_references_are_rejected(tmp_path, reference):
    with pytest.raises(ValueError):
        resolve_result_reference(reference, str(tmp_path))


def test_result_reference_cannot_follow_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "results"
    root.mkdir()
    try:
        (root / "escape.txt").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks unavailable.")
    with pytest.raises(ValueError, match="Unsafe"):
        resolve_result_reference("tool-result://escape.txt", str(root))


@pytest.mark.asyncio
async def test_read_cursor_rejects_changed_file_or_range(tmp_path):
    path = tmp_path / "f"
    path.write_text("text\n" * 10_000, encoding="utf-8")
    query = {"path": str(path), "start_char": 0}
    first = await ReadFileTool().execute(tool_input=query, context=ToolContext())
    cursor = first.metadata["file_page"]["next_cursor"]
    for change in [{"start_char": 1}, {}]:
        if not change:
            path.write_text("changed", encoding="utf-8")
        result = await ReadFileTool().execute(
            tool_input={**query, **change, "cursor": cursor},
            context=ToolContext(),
        )
        assert result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("use_rg", [False, True])
async def test_search_all_pages_without_silent_match_loss(tmp_path, monkeypatch, use_rg):
    if use_rg and not shutil.which("rg"):
        pytest.skip("rg unavailable")
    monkeypatch.setattr("iac_code.tools.grep._is_rg_available", lambda: use_rg)
    root = tmp_path / "results"
    root.mkdir()
    (root / "search.txt").write_text("".join(f"hit {i}\n" for i in range(215)), encoding="utf-8")
    context = ToolContext(tool_result_directory=str(root))
    query = {"path": "tool-result://search.txt", "pattern": "hit", "output_mode": "content", "max_results": 100}
    found = []
    while True:
        result = await GrepTool().execute(tool_input=query, context=context)
        assert not result.is_error
        assert len(result.content) <= 20_000
        found.extend(int(value) for value in re.findall(r":\d+:hit (\d+)", result.content))
        page = result.metadata["search_page"]
        if not page["has_more"]:
            break
        query["cursor"] = page["next_cursor"]
    assert found == list(range(215))


@pytest.mark.asyncio
async def test_search_positions_can_locate_a_long_json_line(tmp_path):
    path = tmp_path / "long.json"
    path.write_text('{"value":"' + "中" * 80_000 + 'needle"}', encoding="utf-8")
    result = await GrepTool().execute(
        tool_input={"path": str(path), "pattern": "needle", "output_mode": "positions"},
        context=ToolContext(cwd=str(tmp_path)),
    )
    assert not result.is_error and len(result.content) < 1_000
    assert "character 80010" in result.content


@pytest.mark.asyncio
async def test_mcp_artifact_reference_can_be_read_without_another_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    original = "mcp text\n" * 4_000
    result = convert_mcp_tool_result(
        {"content": [{"type": "text", "text": original}], "isError": True},
        server_name="server",
        tool_name="tool",
        session_id="session",
    )
    assert result.is_error
    reference = re.search(r"tool-result://[^\s]+", result.content)[0].rstrip(".")
    root = tmp_path / "tool-results" / "session"
    assert not ResultStorage(str(root)).process("outer", result.content).is_externalized
    page = await ReadFileTool().execute(
        tool_input={"path": reference},
        context=ToolContext(tool_result_directory=str(root)),
    )
    assert not page.is_error and "mcp text" in page.content


@pytest.mark.asyncio
async def test_real_session_backup_restore_preserves_result_references(tmp_path, monkeypatch):
    from iac_code.services.session_backup import BackupReason, SessionBackupService
    from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    cwd, session_id = str(tmp_path / "workspace"), "result-session"
    source = SessionStorage(projects_dir=tmp_path / "source" / "projects")
    session_dir = source.session_dir(cwd, session_id)
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    service = SessionBackupService(source, retry_delays=())
    service.initialize_session(cwd, session_id)
    original = "restored-content 中文\n" * 4_000
    ResultStorage(str(session_dir / "tool-results")).process("restore", original)
    service.backup_session(cwd, session_id, reason=BackupReason.NORMAL_TURN_END, critical=True)
    shutil.rmtree(session_dir)
    target = SessionStorage(projects_dir=tmp_path / "target" / "projects")
    restored = SessionBackupService(target, retry_delays=()).restore_session(cwd, session_id)
    assert restored.restored
    result = await ReadFileTool().execute(
        tool_input={"path": "tool-result://restore.txt", "start_char": 0, "end_char": len(original.splitlines()[0])},
        context=ToolContext(tool_result_directory=str(target.session_dir(cwd, session_id) / "tool-results")),
    )
    assert not result.is_error and original.splitlines()[0] in result.content


def test_externalized_output_preserves_exit_code_and_incomplete_source_status(tmp_path):
    result = ResultStorage(str(tmp_path)).process(
        "status",
        "output\n" * 4_000,
        tool_name="bash",
        is_error=True,
        metadata={"exitCode": 2, "web_fetch": {"source_truncated": True}},
    )
    assert "Status: error" in result.content
    assert "Exit code: 2" in result.content
    assert "source was incomplete" in result.content


@pytest.mark.asyncio
async def test_internal_wiki_search_ranks_candidates_from_later_pages(tmp_path, monkeypatch):
    from iac_code.services.wiki_auto_search import search

    monkeypatch.setattr("iac_code.tools.grep._is_rg_available", lambda: False)
    for index in range(70):
        (tmp_path / f"page-{index:03d}.md").write_text(
            "# Heading\ncommon-term " + "padding " * 100 + "\n",
            encoding="utf-8",
        )
    (tmp_path / "zz-late.md").write_text("# Best match\nunique-late\n", encoding="utf-8")
    hits = await search(
        tmp_path,
        ["common-term", "unique-late"],
        ToolContext(cwd=str(tmp_path)),
        ToolPermissionContext(cwd=str(tmp_path)),
        weights={"common-term": 0.1, "unique-late": 1.0},
    )
    assert any(hit["path"] == "zz-late.md" for hit in hits)


@pytest.mark.asyncio
async def test_carriage_return_progress_output_is_not_a_line_break(tmp_path):
    original = "working\r" * 10000
    path = tmp_path / "progress.txt"
    path.write_bytes(original.encode("utf-8"))
    arguments = {"path": str(path)}
    reconstructed = []
    while True:
        result = await ReadFileTool().execute(tool_input=arguments, context=ToolContext())
        assert not result.is_error
        reconstructed.append(re.sub(r"(?m)^ *\d+(?::\d+)?\t", "", result.content.split("\n\n", 1)[1]))
        page = result.metadata["file_page"]
        if not page["has_more"]:
            break
        arguments["cursor"] = page["next_cursor"]
    assert "".join(reconstructed) == original
