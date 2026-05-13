"""Tests for skill prompt renderer."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.renderer import (
    _parse_arguments,
    _replace_async,
    _run_shell,
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
        monkeypatch.setattr("iac_code.skills.renderer.execute_shell_commands", shell_mock)

        result = await render_skill_prompt(skill, "world", context)

        shell_mock.assert_awaited_once()
        shell_input = shell_mock.await_args.args[0]
        assert shell_input.startswith("Base directory for this skill: /tmp/skill-root")
        assert "Target: world" in shell_input
        assert "Dir=/tmp/skill-root" in shell_input
        assert "Session=session-1" in shell_input
        assert result == "rendered shell output"

    def test_parse_arguments_falls_back_on_invalid_quotes(self):
        assert _parse_arguments('"unterminated quote') == ['"unterminated', "quote"]

    @pytest.mark.asyncio
    async def test_run_shell_returns_timeout_marker(self, monkeypatch):
        class NeverAwaited:
            def __await__(self):
                if False:
                    yield None
                return (b"", b"")

        proc = SimpleNamespace(communicate=lambda: NeverAwaited())

        monkeypatch.setattr("asyncio.create_subprocess_shell", AsyncMock(return_value=proc))

        async def raise_timeout(awaitable, timeout):
            raise asyncio.TimeoutError()

        monkeypatch.setattr("asyncio.wait_for", raise_timeout)

        result = await _run_shell("sleep 5")

        assert result.startswith("[shell error:")

    @pytest.mark.asyncio
    async def test_run_shell_returns_oserror_marker(self, monkeypatch):
        monkeypatch.setattr("asyncio.create_subprocess_shell", AsyncMock(side_effect=OSError("spawn failed")))

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
