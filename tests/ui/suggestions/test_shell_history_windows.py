"""Tests for Windows shell history path detection."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from iac_code.ui.suggestions.shell_history_provider import _detect_history_path


def test_windows_git_bash_history(tmp_path: Path):
    """On Windows, detect Git Bash .bash_history in USERPROFILE."""
    bash_hist = tmp_path / ".bash_history"
    bash_hist.write_text("ls\ncd /tmp\n", encoding="utf-8")

    with (
        patch("sys.platform", "win32"),
        patch.dict(os.environ, {"USERPROFILE": str(tmp_path), "SHELL": ""}, clear=False),
    ):
        result = _detect_history_path()

    assert result == str(bash_hist)


def test_windows_powershell_history_fallback(tmp_path: Path):
    """On Windows, fall back to PowerShell history if no .bash_history."""
    appdata = tmp_path / "AppData" / "Roaming"
    ps_dir = appdata / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine"
    ps_dir.mkdir(parents=True)
    ps_hist = ps_dir / "ConsoleHost_history.txt"
    ps_hist.write_text("Get-Process\n", encoding="utf-8")

    with (
        patch("sys.platform", "win32"),
        patch.dict(os.environ, {"USERPROFILE": str(tmp_path), "APPDATA": str(appdata), "SHELL": ""}, clear=False),
    ):
        result = _detect_history_path()

    assert result == str(ps_hist)


def test_windows_no_history_returns_none(tmp_path: Path):
    """On Windows with no history files, returns None."""
    with (
        patch("sys.platform", "win32"),
        patch.dict(
            os.environ, {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "AppData"), "SHELL": ""}, clear=False
        ),
    ):
        result = _detect_history_path()

    assert result is None
