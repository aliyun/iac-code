"""Tests for the install_git_bash subcommand body.

We test the function directly (not via Typer's CliRunner) because the
command is registered on `app` only when sys.platform == "win32"; CI
runs on Linux/macOS where the registration is skipped, so the typer
routing layer is unreachable from tests on those platforms.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import typer


class TestInstallGitBashAlreadyInstalled:
    """If Git Bash is already detectable, the command short-circuits."""

    def test_skips_install_and_exits_zero(self, capsys):
        from iac_code.cli.install_git_bash import install_git_bash

        installed_path = r"C:\Program Files\Git\bin\bash.exe"
        with (
            patch(
                "iac_code.cli.install_git_bash._find_git_bash_path",
                return_value=installed_path,
            ),
            patch("iac_code.cli.install_git_bash.subprocess.run") as mock_run,
            pytest.raises(typer.Exit) as exc_info,
        ):
            install_git_bash()

        assert exc_info.value.exit_code == 0
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert installed_path in out


class TestInstallGitBashSuccess:
    """Not yet installed -> run PS -> re-detect -> exit 0."""

    def test_runs_powershell_and_redetects(self, capsys):
        from iac_code.cli.install_git_bash import install_git_bash
        from iac_code.utils.platform import _NPMMIRROR_CMD, GitBashNotFoundError

        installed_path = r"C:\Program Files\Git\bin\bash.exe"
        find_calls: list[int] = []

        def fake_find() -> str:
            find_calls.append(1)
            if len(find_calls) == 1:
                raise GitBashNotFoundError("not yet installed")
            return installed_path

        with (
            patch(
                "iac_code.cli.install_git_bash._find_git_bash_path",
                side_effect=fake_find,
            ),
            patch(
                "iac_code.cli.install_git_bash.subprocess.run",
                return_value=MagicMock(returncode=0),
            ) as mock_run,
            patch("iac_code.cli.install_git_bash._clear_cache") as mock_clear,
            pytest.raises(typer.Exit) as exc_info,
        ):
            install_git_bash()

        assert exc_info.value.exit_code == 0
        mock_run.assert_called_once_with(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _NPMMIRROR_CMD,
            ],
            check=False,
        )
        mock_clear.assert_called_once()
        assert len(find_calls) == 2
        assert installed_path in capsys.readouterr().out


class TestInstallGitBashPowerShellFails:
    """PowerShell exits non-zero -> exit 1."""

    def test_nonzero_exit_code_propagates(self, capsys):
        from iac_code.cli.install_git_bash import install_git_bash
        from iac_code.utils.platform import GitBashNotFoundError

        with (
            patch(
                "iac_code.cli.install_git_bash._find_git_bash_path",
                side_effect=GitBashNotFoundError("nope"),
            ),
            patch(
                "iac_code.cli.install_git_bash.subprocess.run",
                return_value=MagicMock(returncode=2),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            install_git_bash()

        assert exc_info.value.exit_code == 1
        assert "PowerShell exited" in capsys.readouterr().err


class TestInstallGitBashPostDetectFails:
    """PowerShell returns 0 but bash.exe still not detectable -> exit 1."""

    def test_powershell_zero_but_not_found_exits_one(self, capsys):
        from iac_code.cli.install_git_bash import install_git_bash
        from iac_code.utils.platform import GitBashNotFoundError

        with (
            patch(
                "iac_code.cli.install_git_bash._find_git_bash_path",
                side_effect=GitBashNotFoundError("still missing"),
            ),
            patch(
                "iac_code.cli.install_git_bash.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
            patch("iac_code.cli.install_git_bash._clear_cache"),
            pytest.raises(typer.Exit) as exc_info,
        ):
            install_git_bash()

        assert exc_info.value.exit_code == 1
        assert "bash.exe was not found" in capsys.readouterr().err


class TestInstallGitBashPowerShellMissing:
    """powershell.exe not on PATH -> FileNotFoundError -> exit 1."""

    def test_filenotfound_exits_one(self, capsys):
        from iac_code.cli.install_git_bash import install_git_bash
        from iac_code.utils.platform import GitBashNotFoundError

        with (
            patch(
                "iac_code.cli.install_git_bash._find_git_bash_path",
                side_effect=GitBashNotFoundError("nope"),
            ),
            patch(
                "iac_code.cli.install_git_bash.subprocess.run",
                side_effect=FileNotFoundError("powershell"),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            install_git_bash()

        assert exc_info.value.exit_code == 1
        assert "powershell.exe was not found on PATH" in capsys.readouterr().err
