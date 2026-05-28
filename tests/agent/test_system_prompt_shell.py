"""Tests for shell detection in system prompt environment section."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

from iac_code.agent.system_prompt import _build_environment_section

_TMP = tempfile.gettempdir()


def test_windows_reports_git_bash():
    """On Windows without SHELL env var, reports git-bash."""
    with (
        patch("iac_code.agent.system_prompt.sys.platform", "win32"),
        patch.dict(os.environ, {"COMSPEC": r"C:\Windows\system32\cmd.exe"}, clear=False),
    ):
        env = os.environ.copy()
        env.pop("SHELL", None)
        with patch.dict(os.environ, env, clear=True):
            result = _build_environment_section(_TMP)

    assert "git-bash" in result


def test_unix_reports_shell_env():
    """On Unix, reports the SHELL env var value."""
    with (
        patch("iac_code.agent.system_prompt.sys.platform", "linux"),
        patch.dict(os.environ, {"SHELL": "/bin/zsh"}, clear=False),
    ):
        result = _build_environment_section(_TMP)

    assert "/bin/zsh" in result


def test_unix_no_shell_reports_unknown():
    """On Unix without SHELL env var, reports unknown."""
    with (
        patch("iac_code.agent.system_prompt.sys.platform", "linux"),
        patch.dict(os.environ, {}, clear=True),
    ):
        result = _build_environment_section(_TMP)

    assert "unknown" in result
