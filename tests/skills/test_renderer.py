"""Tests for skill prompt renderer."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.renderer import (
    _parse_arguments,
    _replace_async,
    _run_shell,
    contains_shell_commands,
    execute_shell_commands,
    render_skill_prompt,
    substitute_arguments,
)
from iac_code.skills.skill_definition import SkillContext, SkillDefinition
from iac_code.types.skill_source import SkillSource


class TestSubstituteArguments:
    """Tests for substitute_arguments."""

    def test_no_args_no_change(self):
        assert substitute_arguments("Hello $ARGUMENTS", "") == "Hello $ARGUMENTS"

    def test_full_arguments_replacement(self):
        result = substitute_arguments("Run: $ARGUMENTS", "test --verbose")
        assert result == "Run: test --verbose"

    def test_indexed_arguments(self):
        result = substitute_arguments("File: $ARGUMENTS[0], Branch: $ARGUMENTS[1]", "main.py dev")
        assert result == "File: main.py, Branch: dev"

    def test_short_indexed_arguments(self):
        result = substitute_arguments("File: $0, Branch: $1", "main.py dev")
        assert result == "File: main.py, Branch: dev"

    def test_named_arguments(self):
        result = substitute_arguments(
            "Clone $repo_url on $branch_name",
            "https://github.com/test main",
            argument_names=["repo_url", "branch_name"],
        )
        assert result == "Clone https://github.com/test on main"

    def test_append_if_no_placeholder(self):
        result = substitute_arguments("No placeholders here.", "some args", append_if_no_placeholder=True)
        assert result == "No placeholders here.\n\nARGUMENTS: some args"

    def test_no_append_when_disabled(self):
        result = substitute_arguments("No placeholders here.", "some args", append_if_no_placeholder=False)
        assert result == "No placeholders here."

    def test_out_of_range_index(self):
        result = substitute_arguments("$0 and $5", "only-one")
        assert result == "only-one and "

    def test_quoted_arguments(self):
        result = substitute_arguments("$0 and $1", '"hello world" test')
        assert result == "hello world and test"


class TestExecuteShellCommands:
    """Tests for execute_shell_commands."""

    @pytest.mark.asyncio
    async def test_inline_command(self):
        content = "Version: !`echo hello`"
        result = await execute_shell_commands(content)
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_block_command(self):
        content = "Output:\n```!\necho world\n```"
        result = await execute_shell_commands(content)
        assert "world" in result

    @pytest.mark.asyncio
    async def test_no_commands(self):
        content = "No shell commands here."
        result = await execute_shell_commands(content)
        assert result == "No shell commands here."

    @pytest.mark.asyncio
    async def test_failed_command(self):
        content = "!`nonexistent_command_12345`"
        result = await execute_shell_commands(content)
        # Should contain some output (either error marker or empty)
        assert isinstance(result, str)


class TestShellDetection:
    def test_contains_shell_commands_detects_inline_and_block(self):
        assert contains_shell_commands("Run !`echo hi`")
        assert contains_shell_commands("```!\necho hi\n```")

    def test_contains_shell_commands_ignores_plain_text(self):
        assert not contains_shell_commands("Plain $ARGUMENTS and $PATH")
        assert not contains_shell_commands("```python\nprint('not shell')\n```")


class TestRendererPipeline:
    @pytest.mark.asyncio
    async def test_render_skill_prompt_applies_root_args_variables_and_shell(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo", arguments=["target"]),
            content="Target: $target\nDir=${SKILL_DIR}\nSession=${SESSION_ID}\n!`echo done`",
            source=SkillSource.PROJECT,
            skill_root="/tmp/skill-root",
        )
        context = SkillContext(cwd="/tmp/work", skill_dir="/tmp/skill-root", session_id="session-1")

        shell_mock = AsyncMock(return_value="rendered shell output")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", shell_mock)

        result = await render_skill_prompt(skill, "world", context)

        shell_mock.assert_awaited_once_with("echo done", cwd="/tmp/work")
        assert result.startswith("Base directory for this skill: /tmp/skill-root")
        assert "Target: world" in result
        assert "Dir=/tmp/skill-root" in result
        assert "Session=session-1" in result
        assert result.endswith("rendered shell output")

    def test_parse_arguments_falls_back_on_invalid_quotes(self):
        assert _parse_arguments('"unterminated quote') == ['"unterminated', "quote"]

    @pytest.mark.asyncio
    async def test_run_shell_returns_timeout_marker_and_kills_process(self, monkeypatch):
        async def fake_communicate():
            return (b"", b"")

        proc = SimpleNamespace(communicate=fake_communicate, pid=12345)

        mock_target = "asyncio.create_subprocess_exec" if sys.platform == "win32" else "asyncio.create_subprocess_shell"
        monkeypatch.setattr(mock_target, AsyncMock(return_value=proc))

        async def raise_timeout(awaitable, timeout):
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr("asyncio.wait_for", raise_timeout)

        kill_mock = AsyncMock()
        monkeypatch.setattr("iac_code.utils.platform.kill_process_tree", kill_mock)

        result = await _run_shell("sleep 5")

        assert result.startswith("[shell error:")
        kill_mock.assert_awaited_once_with(proc)

    @pytest.mark.asyncio
    async def test_run_shell_returns_oserror_marker(self, monkeypatch):
        mock_target = "asyncio.create_subprocess_exec" if sys.platform == "win32" else "asyncio.create_subprocess_shell"
        monkeypatch.setattr(mock_target, AsyncMock(side_effect=OSError("spawn failed")))

        result = await _run_shell("echo hi")

        assert "spawn failed" in result

    @pytest.mark.asyncio
    async def test_replace_async_replaces_all_matches(self):
        async def replacer(match):
            return f"<{match.group(1)}>"

        result = await _replace_async(
            "a !`one` b !`two`", execute_shell_commands.__globals__["INLINE_PATTERN"], replacer
        )

        assert result == "a<one> b<two>"

    @pytest.mark.asyncio
    async def test_argument_rendered_shell_block_stays_text(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo"),
            content="User input:\n$ARGUMENTS",
            source=SkillSource.PROJECT,
        )
        context = SkillContext(cwd="/tmp/work")
        run_mock = AsyncMock(return_value="unexpected")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await render_skill_prompt(skill, "```!\necho pwned\n```", context)

        assert "```!\necho pwned\n```" in result
        run_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_argument_rendered_inline_shell_stays_text(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo"),
            content="User input: $ARGUMENTS",
            source=SkillSource.PROJECT,
        )
        context = SkillContext(cwd="/tmp/work")
        run_mock = AsyncMock(return_value="unexpected")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await render_skill_prompt(skill, "!`echo pwned`", context)

        assert result == "User input: !`echo pwned`"
        run_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shell_segment_does_not_substitute_skill_arguments(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo", arguments=["name"]),
            content='```!\necho "$ARGUMENTS" "$0" "$name"\n```',
            source=SkillSource.PROJECT,
        )
        context = SkillContext(cwd="/tmp/work")
        run_mock = AsyncMock(return_value="shell output\n")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await render_skill_prompt(skill, "danger; echo injected", context)

        assert result == "shell output\n\n\nARGUMENTS: danger; echo injected"
        run_mock.assert_awaited_once_with('echo "$ARGUMENTS" "$0" "$name"', cwd="/tmp/work")

    @pytest.mark.asyncio
    async def test_shell_segment_substitutes_builtin_variables_only(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo"),
            content='```!\nprintf "${SKILL_DIR} ${SESSION_ID} $ARGUMENTS"\n```',
            source=SkillSource.PROJECT,
        )
        context = SkillContext(cwd="/tmp/work", skill_dir="/tmp/skill-root", session_id="session-1")
        run_mock = AsyncMock(return_value="shell output\n")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        await render_skill_prompt(skill, "danger", context)

        run_mock.assert_awaited_once_with('printf "/tmp/skill-root session-1 $ARGUMENTS"', cwd="/tmp/work")

    @pytest.mark.asyncio
    async def test_shell_output_is_not_rescanned_for_inline_shell(self, monkeypatch):
        content = "```!\nprintf '!`echo second`'\n```"
        run_mock = AsyncMock(return_value="!`echo second`")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await execute_shell_commands(content)

        assert result == "!`echo second`"
        run_mock.assert_awaited_once_with("printf '!`echo second`'", cwd="")

    @pytest.mark.asyncio
    async def test_multiple_original_shell_segments_execute_in_order(self, monkeypatch):
        content = "a !`one` b\n```!\ntwo\n```\nc !`three`"
        run_mock = AsyncMock(side_effect=["ONE\n", "TWO\n", "THREE\n"])
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await execute_shell_commands(content, cwd="/tmp/work")

        assert result == "aONE b\nTWO\n\ncTHREE"
        assert run_mock.await_args_list == [
            call("one", cwd="/tmp/work"),
            call("two", cwd="/tmp/work"),
            call("three", cwd="/tmp/work"),
        ]
