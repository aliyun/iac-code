"""Result identity survives scope changes and recovery without changing stored data."""

import re
from pathlib import Path

import pytest

from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.tools.base import ToolContext
from iac_code.tools.bash import BashTool
from iac_code.tools.bash.result_references import resolve_command_references
from iac_code.tools.edit_file import EditFileTool
from iac_code.tools.grep import GrepTool
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.result_storage import ResultStorage, resolve_result_reference
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


def reference(result):
    return re.search(r"tool-result://[^\s]+", result.content)[0]


def session_results(root, session_id, transcript=None):
    if not (root / "metadata.json").exists():
        write_session_metadata(
            root,
            SessionMetadata(session_id=session_id, layout_version=SESSION_LAYOUT_VERSION_V2),
        )
    directory = root / "tool-results"
    if transcript:
        directory = root / "pipeline" / "transcripts" / transcript / "tool-results"
    ResultStorage(str(directory)).prepare_directory()
    return directory


@pytest.mark.asyncio
async def test_references_select_original_step_even_with_identical_call_ids(tmp_path):
    root = tmp_path / "session"
    first = session_results(root, "same-session", "transcript_att_0001")
    second = session_results(root, "same-session", "transcript_att_0002")
    normal = session_results(root, "same-session")
    original = "original step\n" * 2000
    a = ResultStorage(str(first)).process("same-call", original)
    b = ResultStorage(str(second)).process("same-call", "different step\n" * 2000)
    assert reference(a) != reference(b)
    for active in [first, second, normal]:
        assert resolve_result_reference(reference(a), str(active)) == a.file_path
        result = await ReadFileTool().execute(
            tool_input={"path": reference(a), "start_line": 1, "end_line": 1},
            context=ToolContext(tool_result_directory=str(active)),
        )
        assert not result.is_error and "original step" in result.content and "different step" not in result.content
    permission = await check_tool_permission(
        BashTool(),
        {"command": "head -n 1 " + reference(a)},
        ToolPermissionContext(
            cwd=str(tmp_path / "workspace"), tool_result_directory=str(normal), trusted_read_directories=[str(normal)]
        ),
    )
    assert permission.behavior == "allow"


def test_foreign_sessions_and_non_result_paths_are_rejected(tmp_path):
    first = session_results(tmp_path / "a", "session-a")
    second = session_results(tmp_path / "b", "session-b")
    result = ResultStorage(str(first)).process("call", "data" * 6000)
    with pytest.raises(ValueError, match="another session"):
        resolve_result_reference(reference(result), str(second))
    bad = reference(result).replace("/tool-results/call.txt", "/metadata.json")
    with pytest.raises(ValueError):
        resolve_result_reference(bad, str(first))


def test_duplicate_call_ids_preserve_every_original_and_retry_idempotently(tmp_path):
    storage = ResultStorage(str(tmp_path))
    first = storage.process("same", "a" * 20001)
    second = storage.process("same", "b" * 20001)
    retry = storage.process("same", "b" * 20001)
    assert first.file_path != second.file_path == retry.file_path
    assert reference(first) != reference(second) == reference(retry)
    assert Path(first.file_path).read_text(encoding="utf-8") == "a" * 20001
    assert Path(second.file_path).read_text(encoding="utf-8") == "b" * 20001


def test_changed_result_content_is_rejected_even_if_size_is_unchanged(tmp_path):
    result = ResultStorage(str(tmp_path)).process("call", "a" * 20001)
    path = Path(result.file_path)
    path.chmod(0o600)
    path.write_text("b" * 20001, encoding="utf-8")
    with pytest.raises(ValueError, match="content has changed"):
        resolve_result_reference(reference(result), str(tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "grep -F 'tool-result://' log.txt",
        "rg -e 'tool-result://one.txt' log.txt",
        "grep 'tool-result://one.txt' log.txt",
        "echo tool-result://one.txt",
        "printf '%s' tool-result://one.txt",
        "jq --arg literal tool-result://one.txt '.x == $literal' input.json",
    ],
)
def test_literal_search_patterns_and_text_arguments_are_not_rewritten(command):
    assert resolve_command_references(command, None) == command


@pytest.mark.parametrize(
    "command",
    [
        "cat tool-result://one.txt > tool-result://one.txt",
        "tee tool-result://one.txt",
        "rm tool-result://one.txt",
        "sed -i 's/a/b/' tool-result://one.txt",
        "cp input.txt tool-result://one.txt",
        "chmod +w tool-result://one.txt",
    ],
)
def test_references_cannot_be_used_as_mutation_targets(tmp_path, command):
    (tmp_path / "one.txt").write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="read-only"):
        resolve_command_references(command, str(tmp_path))
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_source_path_deny_rules_cannot_be_bypassed_by_input_copy(tmp_path):
    storage = ResultStorage(str(tmp_path))
    result = storage.process("call", "private" * 4000)
    source = resolve_command_references("cat " + reference(result), str(tmp_path))
    decision = await check_tool_permission(
        BashTool(),
        {"command": "cat " + reference(result)},
        ToolPermissionContext(
            cwd=str(tmp_path), tool_result_directory=str(tmp_path), deny_rules={"user_settings": [f"bash({source})"]}
        ),
    )
    assert decision.behavior == "deny"


@pytest.mark.asyncio
async def test_source_execution_policy_cannot_be_bypassed_by_input_copy(tmp_path, monkeypatch):
    result = ResultStorage(str(tmp_path)).process("call", "private" * 4000)
    source = resolve_command_references("cat " + reference(result), str(tmp_path))
    monkeypatch.setattr(
        "iac_code.services.execution_policy.check",
        lambda name, args, *a, **kw: "source denied" if args["command"] == source else None,
    )
    decision = await check_tool_permission(
        BashTool(),
        {"command": "cat " + reference(result)},
        ToolPermissionContext(cwd=str(tmp_path), tool_result_directory=str(tmp_path)),
    )
    assert decision.behavior == "deny" and decision.message == "source denied"


@pytest.mark.parametrize("transcript", [None, "transcript_att_0001"])
@pytest.mark.asyncio
async def test_backup_restore_keeps_read_and_search_cursors_across_steps(tmp_path, monkeypatch, transcript):
    from iac_code.services.session_backup import BackupReason, SessionBackupService
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    cwd, session_id = str(tmp_path / "workspace"), "cursor-session"
    source = SessionStorage(projects_dir=tmp_path / "source" / "projects")
    session_dir = source.session_dir(cwd, session_id)
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    storage_dir = session_results(session_dir, session_id, transcript)
    service = SessionBackupService(source, retry_delays=())
    service.initialize_session(cwd, session_id)
    original = "needle 中文\n" * 4000
    stored = ResultStorage(str(storage_dir)).process("restore", original)
    uri = reference(stored)
    context = ToolContext(cwd=cwd, tool_result_directory=str(storage_dir))
    read_input = {"path": uri, "start_char": 0, "max_chars": 3000}
    search_input = {"path": uri, "pattern": "needle", "output_mode": "positions", "max_results": 2}
    first = await ReadFileTool().execute(tool_input=read_input, context=context)
    search = await GrepTool().execute(tool_input=search_input, context=context)
    cursor = re.search(r"Next cursor: (\S+)", search.content)[1]
    # Expendable Bash copies must not double the backup payload.
    BashTool().prepare_contextual_input({"command": "cat " + uri}, context)
    assert list(session_dir.rglob(".bash-inputs"))
    service.backup_session(cwd, session_id, reason=BackupReason.NORMAL_TURN_END, critical=True)
    service._rmtree(session_dir)
    target = SessionStorage(projects_dir=tmp_path / "target" / "projects")
    restored = SessionBackupService(target, retry_delays=()).restore_session(cwd, session_id)
    assert restored.restored
    restored_root = target.session_dir(cwd, session_id)
    assert not list(restored_root.rglob(".bash-inputs"))
    active = session_results(restored_root, session_id, "transcript_att_0002" if transcript is None else None)
    context = ToolContext(cwd=cwd, tool_result_directory=str(active))
    second = await ReadFileTool().execute(
        tool_input={**read_input, "cursor": first.metadata["file_page"]["next_cursor"]},
        context=context,
    )
    continued = await GrepTool().execute(tool_input={**search_input, "cursor": cursor}, context=context)
    assert not second.is_error and not continued.is_error
    assert second.metadata["file_page"]["start_char"] == first.metadata["file_page"]["end_char"]
    start, end = second.metadata["file_page"]["start_char"], second.metadata["file_page"]["end_char"]
    assert second.content.split("\n\n", 1)[1] == original[start:end]
    assert ":3: character " in continued.content


def test_failed_publication_never_replaces_an_existing_result(tmp_path, monkeypatch):
    storage = ResultStorage(str(tmp_path))
    first = storage.process("call", "a" * 20001)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("iac_code.tools.result_storage.os.link", fail)
    with pytest.raises(OSError, match="disk full"):
        storage.process("call", "b" * 20001)
    assert Path(first.file_path).read_text(encoding="utf-8") == "a" * 20001
    assert not list(tmp_path.glob(".result-*"))


def test_source_change_between_resolution_and_copy_is_rejected(tmp_path, monkeypatch):
    import iac_code.tools.result_storage as module

    stored = ResultStorage(str(tmp_path)).process("call", "a" * 20001)
    original = module.resolve_result_reference

    def change_after_lookup(*args, **kwargs):
        path = original(*args, **kwargs)
        Path(path).write_text("b" * 20001, encoding="utf-8")
        return path

    monkeypatch.setattr(module, "resolve_result_reference", change_after_lookup)
    with pytest.raises(ValueError, match="changed while preparing"):
        module.bash_result_snapshot(reference(stored), str(tmp_path))
    assert not list((tmp_path / ".bash-inputs").glob("*.txt"))


def test_long_call_ids_remain_readable_and_safe(tmp_path):
    result = ResultStorage(str(tmp_path)).process("call_" + "x" * 4000, "data" * 6000)
    assert len(Path(result.file_path).name) < 255
    assert resolve_result_reference(reference(result), str(tmp_path)) == result.file_path


def test_standalone_storage_refuses_a_symlinked_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "results"
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(OSError, match="Unsafe"):
        ResultStorage(str(root)).process("call", "data" * 6000)
    assert not list(outside.iterdir())


def test_windows_session_delete_can_remove_readonly_input_copies(tmp_path):
    import os

    from iac_code.services.session_storage import SessionStorage

    cache = tmp_path / ".bash-inputs"
    cache.mkdir()
    path = cache / "input.txt"
    path.write_text("cached", encoding="utf-8")
    path.chmod(0o400)
    error = PermissionError("readonly")
    SessionStorage._remove_readonly_result_input(os.unlink, str(path), (PermissionError, error, None))
    assert not path.exists()
    other = tmp_path / "unrelated.txt"
    other.write_text("keep", encoding="utf-8")
    with pytest.raises(PermissionError):
        SessionStorage._remove_readonly_result_input(os.unlink, str(other), (PermissionError, error, None))
    assert other.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_class", [WriteFileTool, EditFileTool])
@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("path_key", ["path", "file_path"])
async def test_write_tools_reject_result_references_in_every_permission_mode(tmp_path, tool_class, mode, path_key):
    original = "original" * 4000
    storage = ResultStorage(str(tmp_path / "results"))
    stored = storage.process("source", original)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = tool_class()
    arguments = {path_key: reference(stored), "content": "changed", "old_string": "original", "new_string": "changed"}
    decision = await check_tool_permission(
        tool,
        arguments,
        ToolPermissionContext(
            mode=mode,
            cwd=str(workspace),
            tool_result_directory=str(tmp_path / "results"),
            allow_rules={"user_settings": [tool.name]},
        ),
    )
    assert decision.behavior == "deny" and "read-only" in decision.message
    tool.normalize_input(arguments)
    result = await tool.execute(tool_input=arguments, context=ToolContext(cwd=str(workspace)))
    assert result.is_error and "read-only" in result.content
    assert Path(stored.file_path).read_text(encoding="utf-8") == original
    assert not list(workspace.iterdir()), "Reject before treating the URI as a relative filesystem path."
