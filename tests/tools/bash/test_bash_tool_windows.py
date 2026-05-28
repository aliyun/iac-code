"""Tests for BashTool Windows execution path via Git Bash."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.bash.bash_tool import BashTool


def _make_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


@pytest.fixture
def tool():
    return BashTool()


@pytest.fixture
def context(tmp_path):
    return ToolContext(cwd=str(tmp_path))


class TestBashToolWindowsExec:
    """Verify that on Windows the tool uses create_subprocess_exec with Git Bash."""

    @pytest.mark.asyncio
    async def test_windows_uses_exec_with_git_bash(self, tool, context):
        fake_bash = r"C:\Program Files\Git\bin\bash.exe"
        fake_platform = MagicMock()
        fake_platform.shell_path = fake_bash

        proc = _make_process(returncode=0, stdout=b"hello\n")

        with (
            patch("iac_code.tools.bash.bash_tool.sys.platform", "win32"),
            patch("iac_code.tools.bash.bash_tool.PlatformInfo.detect", return_value=fake_platform),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as mock_exec,
        ):
            result = await tool.execute(tool_input={"command": "echo hello"}, context=context)

        mock_exec.assert_called_once_with(
            fake_bash,
            "-c",
            "echo hello",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=context.cwd,
        )
        assert not result.is_error
        assert "hello" in result.content

    @pytest.mark.asyncio
    async def test_windows_exec_nonzero_exit(self, tool, context):
        fake_bash = r"C:\Program Files\Git\bin\bash.exe"
        fake_platform = MagicMock()
        fake_platform.shell_path = fake_bash

        proc = _make_process(returncode=1, stderr=b"not found\n")

        with (
            patch("iac_code.tools.bash.bash_tool.sys.platform", "win32"),
            patch("iac_code.tools.bash.bash_tool.PlatformInfo.detect", return_value=fake_platform),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            result = await tool.execute(tool_input={"command": "bad-cmd"}, context=context)

        assert result.is_error
        assert "not found" in result.content
        assert "Exit code: 1" in result.content

    @pytest.mark.asyncio
    async def test_windows_exec_timeout_kills_process_tree(self, tool, context):
        fake_bash = r"C:\Program Files\Git\bin\bash.exe"
        fake_platform = MagicMock()
        fake_platform.shell_path = fake_bash

        proc = _make_process()
        call_count = 0

        async def communicate_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            return (b"", b"")

        proc.communicate = AsyncMock(side_effect=communicate_side_effect)

        with (
            patch("iac_code.tools.bash.bash_tool.sys.platform", "win32"),
            patch("iac_code.tools.bash.bash_tool.PlatformInfo.detect", return_value=fake_platform),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
            patch("iac_code.tools.bash.bash_tool.kill_process_tree", new_callable=AsyncMock) as mock_kill,
        ):
            result = await tool.execute(tool_input={"command": "sleep 999", "timeout": 1}, context=context)

        mock_kill.assert_awaited_once_with(proc)
        assert result.is_error
        assert "timed out" in result.content.lower() or "timeout" in result.content.lower()

    @pytest.mark.asyncio
    async def test_non_windows_uses_shell(self, tool, context):
        """On non-Windows platforms, create_subprocess_shell is used (not exec)."""
        proc = _make_process(returncode=0, stdout=b"ok\n")

        with (
            patch("iac_code.tools.bash.bash_tool.sys.platform", "linux"),
            patch("asyncio.create_subprocess_shell", new_callable=AsyncMock, return_value=proc) as mock_shell,
        ):
            result = await tool.execute(tool_input={"command": "echo ok"}, context=context)

        mock_shell.assert_called_once_with(
            "echo ok",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=context.cwd,
            start_new_session=True,
        )
        assert not result.is_error
        assert "ok" in result.content
