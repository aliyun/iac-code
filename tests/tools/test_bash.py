"""Tests for the Bash tool."""

import sys
from unittest.mock import AsyncMock

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.bash import BashTool

_skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32", reason="Requires Unix shell commands not available on Windows"
)


@pytest.fixture
def bash_tool():
    """Create a Bash tool instance."""
    return BashTool()


class TestBashTool:
    """Tests for BashTool."""

    def test_tool_properties(self, bash_tool):
        """Test tool name, description, and schema."""
        assert bash_tool.name == "bash"
        assert "shell" in bash_tool.description.lower() or "command" in bash_tool.description.lower()
        assert bash_tool.input_schema["required"] == ["command"]

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_execute_simple_command(self, bash_tool, tmp_path):
        """Test executing a simple echo command."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "echo hello"},
            context=context,
        )

        assert result.is_error is False
        assert "hello" in result.content
        assert "Exit code: 0" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_command_failure_nonzero_exit(self, bash_tool, tmp_path):
        """Test command with non-zero exit code returns error."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "exit 1"},
            context=context,
        )

        assert result.is_error is True
        assert "Exit code: 1" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_command_timeout(self, bash_tool, tmp_path):
        """Test command timeout."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "sleep 10", "timeout": 1},
            context=context,
        )

        assert result.is_error is True
        assert "timed out" in result.content.lower()

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_capture_stdout(self, bash_tool, tmp_path):
        """Test capturing stdout."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "echo 'stdout message'"},
            context=context,
        )

        assert result.is_error is False
        assert "STDOUT:" in result.content
        assert "stdout message" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_capture_stderr(self, bash_tool, tmp_path):
        """Test capturing stderr."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "echo 'stderr message' >&2"},
            context=context,
        )

        assert result.is_error is False
        assert "STDERR:" in result.content
        assert "stderr message" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_capture_both_stdout_and_stderr(self, bash_tool, tmp_path):
        """Test capturing both stdout and stderr."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "echo 'out' && echo 'err' >&2"},
            context=context,
        )

        assert result.is_error is False
        assert "STDOUT:" in result.content
        assert "out" in result.content
        assert "STDERR:" in result.content
        assert "err" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_working_directory(self, bash_tool, tmp_path):
        """Test command runs in correct working directory."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "pwd"},
            context=context,
        )

        assert result.is_error is False
        assert str(tmp_path) in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_command_with_pipe(self, bash_tool, tmp_path):
        """Test command with pipe."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "echo 'hello world' | wc -w"},
            context=context,
        )

        assert result.is_error is False
        assert "2" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_command_creating_file(self, bash_tool, tmp_path):
        """Test command that creates a file."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "echo 'test content' > test.txt && cat test.txt"},
            context=context,
        )

        assert result.is_error is False
        assert "test content" in result.content
        assert (tmp_path / "test.txt").exists()

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_command_not_found(self, bash_tool, tmp_path):
        """Test running a non-existent command."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "nonexistent_command_12345"},
            context=context,
        )

        assert result.is_error is True
        assert "Exit code:" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_multiline_output(self, bash_tool, tmp_path):
        """Test command with multiline output."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "printf 'line1\\nline2\\nline3'"},
            context=context,
        )

        assert result.is_error is False
        assert "line1" in result.content
        assert "line2" in result.content
        assert "line3" in result.content

    @_skip_on_windows
    @pytest.mark.asyncio
    async def test_default_timeout(self, bash_tool, tmp_path):
        """Test that default timeout is 120 seconds."""
        context = ToolContext(cwd=str(tmp_path))
        result = await bash_tool.execute(
            tool_input={"command": "echo 'quick command'"},
            context=context,
        )
        assert result.is_error is False

    @pytest.mark.asyncio
    async def test_subprocess_creation_error(self, bash_tool, monkeypatch, tmp_path):
        context = ToolContext(cwd=str(tmp_path))
        mock_target = "asyncio.create_subprocess_exec" if sys.platform == "win32" else "asyncio.create_subprocess_shell"
        monkeypatch.setattr(
            mock_target,
            AsyncMock(side_effect=OSError("spawn failed")),
        )

        result = await bash_tool.execute(tool_input={"command": "echo hi"}, context=context)

        assert result.is_error is True
        assert "Error executing command: spawn failed" in result.content
