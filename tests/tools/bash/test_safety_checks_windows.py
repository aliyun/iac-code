"""Tests for Windows-specific sensitive paths."""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

import iac_code.tools.bash.safety_checks as mod


@pytest.fixture(autouse=True)
def _restore_module():
    """Always restore module to real platform state after each test."""
    yield
    importlib.reload(mod)


def test_windows_sensitive_paths_included():
    """On Windows, SENSITIVE_PATHS includes Windows-specific entries."""
    with patch("sys.platform", "win32"):
        importlib.reload(mod)
        paths = mod.SENSITIVE_PATHS

    assert "ntuser.dat" in paths
    assert any("PowerShell" in p for p in paths)
    assert any("Credentials" in p for p in paths)


def test_windows_multi_component_paths_match():
    """Multi-component Windows paths are detected by _path_hits_sensitive."""
    with patch("sys.platform", "win32"):
        importlib.reload(mod)

    assert mod._path_hits_sensitive("C:/Users/me/AppData/Roaming/Microsoft/Windows/PowerShell/profile.ps1")
    assert mod._path_hits_sensitive("C:\\Users\\me\\AppData\\Local\\Microsoft\\Credentials\\data")


def test_unix_sensitive_paths_no_windows_entries():
    """On non-Windows, SENSITIVE_PATHS does not include Windows entries."""
    with patch("sys.platform", "linux"):
        importlib.reload(mod)
        paths = mod.SENSITIVE_PATHS

    assert "ntuser.dat" not in paths
    assert ".bashrc" in paths
    assert ".ssh" in paths
