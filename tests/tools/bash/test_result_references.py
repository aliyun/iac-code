"""Result URIs use the same session fence and Bash permissions as real paths."""

import asyncio
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools.base import ToolContext, ToolRegistry
from iac_code.tools.bash import BashTool
from iac_code.tools.bash.command_parser import parse_command
from iac_code.tools.bash.result_references import resolve_command_references
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.permissions import PermissionMode, PermissionResult, ToolPermissionContext

unix = pytest.mark.skipif(sys.platform == "win32", reason="Executes Unix shell commands")


@pytest.fixture
def result_root(tmp_path):
    root = tmp_path / "results with spaces ' 中文"
    root.mkdir()
    (root / "one.txt").write_text("first result\n", encoding="utf-8")
    (root / "two.txt").write_text("second result\n", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "operand",
    [
        "tool-result://one.txt",
        "'tool-result://one.txt'",
        '"tool-result://one.txt"',
        '"tool-result://"one.txt',
        "'tool-result://'one.txt",
    ],
)
def test_complete_static_arguments(result_root, operand):
    resolved = resolve_command_references(f"cat {operand}", str(result_root))
    assert resolved == "cat " + shlex.quote(str(result_root / "one.txt"))


def test_options_pipes_redirects_and_multiple_references(result_root):
    command = "jq --file=tool-result://one.txt | cat tool-result://two.txt < tool-result://one.txt"
    resolved = resolve_command_references(command, str(result_root))
    assert "tool-result://" not in resolved
    assert shlex.quote("--file=" + str(result_root / "one.txt")) in resolved
    assert shlex.quote(str(result_root / "two.txt")) in resolved
    assert " < " + shlex.quote(str(result_root / "one.txt")) in resolved


def test_assignment_and_nested_shell_commands(result_root):
    command = 'file=tool-result://one.txt; cat "$file"; echo "$(cat tool-result://two.txt)"'
    resolved = resolve_command_references(command, str(result_root))
    assert resolved == (
        "file="
        + shlex.quote(str(result_root / "one.txt"))
        + '; cat "$file"; echo "$(cat '
        + shlex.quote(str(result_root / "two.txt"))
        + ')"'
    )


def test_comments_and_commands_without_references_are_unchanged():
    for command in ["cat ordinary.txt # tool-result://missing.txt", "echo 'unterminated"]:
        assert resolve_command_references(command, None) == command


@pytest.mark.parametrize(
    "command",
    [
        "cat tool-result://../one.txt",
        "cat tool-result:///etc/passwd",
        "cat tool-result://a/../../one.txt",
        r"cat 'tool-result://a\one.txt'",
        "cat tool-result://",
        'cat "tool-result://$name"',
        'cat "tool-result://$(echo one.txt)"',
        "python -c 'open(\"tool-result://one.txt\")'",
        "cat <<'EOF'\ntool-result://one.txt\nEOF",
        "cat 'tool-result://one.txt",
    ],
)
def test_invalid_dynamic_or_embedded_references_are_rejected(result_root, command):
    with pytest.raises((ValueError, OSError)):
        resolve_command_references(command, str(result_root))


def test_reference_requires_current_session_and_rejects_symlinks(result_root, tmp_path):
    with pytest.raises(ValueError):
        resolve_command_references("cat tool-result://one.txt", None)
    other = tmp_path / "another-session"
    other.mkdir()
    assert resolve_command_references("cat tool-result://one.txt", str(other)) == (
        "cat " + shlex.quote(str(other / "one.txt"))
    )
    try:
        (result_root / "link.txt").symlink_to(result_root / "one.txt")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(ValueError):
        resolve_command_references("cat tool-result://link.txt", str(result_root))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_invalid_references_cannot_bypass_permissions(result_root, mode):
    context = ToolPermissionContext(mode=mode, tool_result_directory=str(result_root))
    context.allow_rules = {"user_settings": ["bash(**)"]}
    result = await check_tool_permission(BashTool(), {"command": "cat tool-result://../outside"}, context)
    assert result.behavior == "deny"


@pytest.mark.asyncio
async def test_resolved_paths_follow_existing_workspace_and_deny_rules(result_root, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args = {"command": "cat tool-result://one.txt"}
    context = ToolPermissionContext(cwd=str(workspace), tool_result_directory=str(result_root))
    assert (await check_tool_permission(BashTool(), args, context)).behavior != "allow"
    context.trusted_read_directories = [str(result_root)]
    assert (await check_tool_permission(BashTool(), args, context)).behavior == "allow"
    context.deny_rules = {"user_settings": ["bash(cat:*)"]}
    assert (await check_tool_permission(BashTool(), args, context)).behavior == "deny"
    assert args == {"command": "cat tool-result://one.txt"}


@pytest.mark.asyncio
async def test_resolving_input_does_not_grant_output_write_permission(result_root, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolPermissionContext(
        cwd=str(workspace), tool_result_directory=str(result_root), trusted_read_directories=[str(result_root)]
    )
    result = await check_tool_permission(
        BashTool(), {"command": "cat tool-result://one.txt > " + shlex.quote(str(tmp_path / "outside.txt"))}, context
    )
    assert result.behavior != "allow"


@pytest.mark.asyncio
async def test_executor_rechecks_reference_after_permission_check(result_root, monkeypatch):
    tool = BashTool()
    args = {"command": "cat tool-result://one.txt"}
    context = ToolPermissionContext(
        cwd=str(result_root), tool_result_directory=str(result_root), trusted_read_directories=[str(result_root)]
    )
    assert (await check_tool_permission(tool, args, context)).behavior == "allow"
    path = result_root / "one.txt"
    path.unlink()
    try:
        path.symlink_to(result_root / "two.txt")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    spawn = AsyncMock()
    monkeypatch.setattr("iac_code.tools.bash.bash_tool.create_subprocess_shell", spawn)
    registry = ToolRegistry()
    registry.register(tool)
    results = await ToolExecutor(registry=registry).execute_batch(
        [ToolCallRequest(id="read", name="bash", input=args)],
        ToolContext(cwd=str(result_root), tool_result_directory=str(result_root)),
    )
    assert results[0].is_error
    spawn.assert_not_called()


@unix
@pytest.mark.asyncio
async def test_permission_policy_and_actual_execution_use_identical_command(result_root, tmp_path, monkeypatch):
    import iac_code.services.execution_policy as policy
    import iac_code.tools.bash.permissions as permissions

    checked = []
    monkeypatch.setattr(policy, "check", lambda name, args, *a, **kw: checked.append(args["command"]))
    permission = AsyncMock(return_value=PermissionResult(behavior="allow"))
    monkeypatch.setattr(permissions, "bash_tool_has_permission", permission)
    original_execute = BashTool.execute
    executed = []

    async def execute(self, *, tool_input, context):
        executed.append(tool_input["command"])
        return await original_execute(self, tool_input=tool_input, context=context)

    monkeypatch.setattr(BashTool, "execute", execute)
    tool = BashTool()
    registry = ToolRegistry()
    registry.register(tool)
    args = {"command": "cat tool-result://one.txt"}
    context = ToolPermissionContext(cwd=str(tmp_path), tool_result_directory=str(result_root))
    assert (await check_tool_permission(tool, args, context)).behavior == "allow"
    results = await ToolExecutor(registry=registry).execute_batch(
        [ToolCallRequest(id="read", name="bash", input=args)],
        ToolContext(cwd=str(tmp_path), tool_result_directory=str(result_root)),
    )
    source = resolve_command_references(args["command"], str(result_root))
    expected = resolve_command_references(args["command"], str(result_root), snapshot=True)
    assert source in checked and expected in checked
    assert set(checked) == {source, expected}
    assert [call.args[0] for call in permission.call_args_list] == [source, expected]
    assert executed == [expected]
    assert results[0].metadata["stdout"] == "first result\n"
    assert not results[0].is_error
    assert args["command"] == "cat tool-result://one.txt"


@unix
@pytest.mark.asyncio
async def test_shell_metacharacters_in_physical_path_cannot_execute(tmp_path):
    root = tmp_path / "space ' $HOME $(touch injected) `touch injected2`; 中文"
    root.mkdir()
    (root / "one.txt").write_text("safe", encoding="utf-8")
    result = await BashTool().execute(
        tool_input={"command": "cat tool-result://one.txt"},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(root)),
    )
    assert result.metadata["stdout"] == "safe"
    assert not (tmp_path / "injected").exists()
    assert not (tmp_path / "injected2").exists()


@unix
@pytest.mark.asyncio
async def test_shared_tool_uses_each_call_session_directory(result_root, tmp_path):
    second = tmp_path / "relocated results"
    second.mkdir()
    (second / "one.txt").write_text("relocated", encoding="utf-8")
    tool = BashTool()
    results = await asyncio.gather(
        *[
            tool.execute(
                tool_input={"command": "cat tool-result://one.txt"},
                context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(root)),
            )
            for root in [result_root, second]
        ]
    )
    assert [r.metadata["stdout"] for r in results] == ["first result\n", "relocated"]


@unix
@pytest.mark.asyncio
async def test_python_receives_reference_as_separate_argv(result_root, tmp_path):
    program = "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'),end='')"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(program) + ' "tool-result://one.txt"'
    result = await BashTool().execute(
        tool_input={"command": command},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(result_root)),
    )
    assert result.metadata["stdout"] == "first result\n"


@pytest.mark.asyncio
async def test_invalid_reference_never_starts_subprocess(result_root, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr("iac_code.tools.bash.bash_tool.create_subprocess_shell", spawn)
    result = await BashTool().execute(
        tool_input={"command": "cat tool-result://../invalid.txt"},
        context=ToolContext(tool_result_directory=str(result_root)),
    )
    assert result.is_error
    spawn.assert_not_called()


def test_windows_physical_path_is_one_literal_argument(monkeypatch):
    path = r"C:\Users\someone\result files\中文\one.txt"
    monkeypatch.setattr("iac_code.tools.bash.result_references.resolve_result_reference", lambda *args: path)
    command = resolve_command_references("cat tool-result://one.txt", "unused")
    assert parse_command(command).commands[0].argv == ["cat", path]


@unix
@pytest.mark.asyncio
async def test_source_replacement_at_spawn_cannot_change_the_input(result_root, tmp_path, monkeypatch):
    import iac_code.tools.bash.bash_tool as module

    original = module.create_subprocess_shell
    source = result_root / "one.txt"

    async def replace_then_spawn(*args, **kwargs):
        source.unlink()
        source.symlink_to(result_root / "two.txt")
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "create_subprocess_shell", replace_then_spawn)
    result = await BashTool().execute(
        tool_input={"command": "cat tool-result://one.txt"},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(result_root)),
    )
    assert not result.is_error
    assert result.metadata["stdout"] == "first result\n"


@unix
@pytest.mark.asyncio
async def test_absent_references_preserve_shell_conditionals(result_root, tmp_path):
    command = "if test -f tool-result://missing.txt; then cat tool-result://missing.txt; else echo absent; fi"
    result = await BashTool().execute(
        tool_input={"command": command},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(result_root)),
    )
    assert not result.is_error and result.metadata["stdout"] == "absent\n"


@unix
@pytest.mark.asyncio
async def test_absent_reference_cannot_read_a_source_created_later(result_root, tmp_path):
    source = result_root / "missing.txt"
    command = "printf wrong > " + shlex.quote(str(source)) + "; cat tool-result://missing.txt"
    result = await BashTool().execute(
        tool_input={"command": command},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(result_root)),
    )
    assert result.is_error and result.metadata["stdout"] == ""
    assert source.read_text(encoding="utf-8") == "wrong"


@unix
@pytest.mark.asyncio
async def test_complete_large_cat_reuses_original_reference_instead_of_copying_it_again(tmp_path):
    import re

    from iac_code.tools.result_storage import ResultStorage

    storage = ResultStorage(str(tmp_path))
    stored = storage.process("large", "complete 中文\n" * 4000)
    uri = re.search(r"tool-result://\S+", stored.content)[0]
    result = await BashTool().execute(
        tool_input={"command": "cat " + uri},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(tmp_path)),
    )
    assert not result.is_error and len(result.content) < 2000
    assert uri in result.content and "reproduced its complete input" in result.content
    assert not storage.process("cat-output", result.content).is_externalized
    assert sorted(path.name for path in tmp_path.glob("*.txt")) == ["large.txt"]


@unix
@pytest.mark.asyncio
async def test_writing_to_an_argv_input_copy_cannot_change_original(tmp_path):
    from iac_code.tools.result_storage import ResultStorage

    content = "original" * 4000
    stored = ResultStorage(str(tmp_path)).process("source", content)
    program = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('changed',encoding='utf-8')"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(program) + " tool-result://source.txt"
    await BashTool().execute(
        tool_input={"command": command},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(tmp_path)),
    )
    assert Path(stored.file_path).read_text(encoding="utf-8") == content


@unix
@pytest.mark.asyncio
@pytest.mark.parametrize("trailing_newline", [False, True])
@pytest.mark.parametrize("pipeline_mode", [False, True])
async def test_grep_of_complete_long_json_line_reuses_reference_losslessly(tmp_path, trailing_newline, pipeline_mode):
    import json
    import re

    from iac_code.tools.result_storage import ResultStorage

    original = json.dumps({"needle": "中文" * 15000}, ensure_ascii=False) + ("\n" if trailing_newline else "")
    storage = ResultStorage(str(tmp_path))
    stored = storage.process("json", original)
    uri = re.search(r"tool-result://\S+", stored.content)[0]
    context = ToolContext(cwd=str(tmp_path), tool_result_directory=str(tmp_path), pipeline_mode=pipeline_mode)
    # Repeating the same whole-line search must not create a chain of result files.
    for _attempt in range(2):
        result = await BashTool().execute(tool_input={"command": "grep -F needle " + uri}, context=context)
        assert not result.is_error
        assert result.metadata["stdout"] == original.rstrip("\n") + "\n"
        assert result.metadata["stderr"] == "" and result.metadata["exitCode"] == 0
        assert uri in result.content and len(result.content) < 2000
        assert ("plus one trailing LF" in result.content) == (not trailing_newline)
        assert f"Characters: {len(original)};" in result.content
        assert not storage.process("grep-output", result.content).is_externalized
        assert Path(stored.file_path).read_text(encoding="utf-8") == original
        assert sorted(path.name for path in tmp_path.glob("*.txt")) == ["json.txt"]


@unix
@pytest.mark.asyncio
async def test_grep_transformed_long_line_keeps_its_complete_distinct_output(tmp_path):
    import json
    import re

    from iac_code.tools.result_storage import ResultStorage

    original = json.dumps({"needle": "x" * 30000})
    storage = ResultStorage(str(tmp_path))
    stored = storage.process("json", original)
    uri = re.search(r"tool-result://\S+", stored.content)[0]
    result = await BashTool().execute(
        tool_input={"command": "grep -n needle " + uri},
        context=ToolContext(cwd=str(tmp_path), tool_result_directory=str(tmp_path)),
    )
    assert not result.is_error and result.metadata["stdout"] == "1:" + original + "\n"
    externalized = storage.process("grep-numbered", result.content)
    assert externalized.is_externalized and uri not in externalized.content
    assert Path(externalized.file_path).read_text(encoding="utf-8") == result.content
    assert Path(stored.file_path).read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "operand",
    [
        "tool-result://*.txt",
        "'tool-result://one?.txt'",
        '"tool-result://[ot]*.txt"',
        "tool-result://{one,two}.txt",
    ],
)
def test_result_reference_wildcards_are_rejected_before_execution(result_root, operand):
    with pytest.raises(ValueError, match="wildcard"):
        resolve_command_references("cat " + operand, str(result_root))


def test_wildcards_in_search_text_remain_literal_and_do_not_block_file_reference(result_root):
    command = "grep -F 'tool-result://*.txt' tool-result://one.txt"
    resolved = resolve_command_references(command, str(result_root))
    assert resolved == "grep -F 'tool-result://*.txt' " + shlex.quote(str(result_root / "one.txt"))
    assert resolve_command_references("cat *.txt", None) == "cat *.txt"
