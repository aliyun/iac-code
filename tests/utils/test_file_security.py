"""Tests for cross-platform file permission restriction."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from iac_code.utils.file_security import restrict_file_permissions


@pytest.mark.skipif(sys.platform == "win32", reason="chmod has no effect on Windows NTFS")
def test_unix_file_permissions(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("data")
    with patch("iac_code.utils.file_security._IS_WINDOWS", False):
        restrict_file_permissions(f, directory=False)
    assert oct(f.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod has no effect on Windows NTFS")
def test_unix_directory_permissions(tmp_path):
    d = tmp_path / "secret_dir"
    d.mkdir()
    with patch("iac_code.utils.file_security._IS_WINDOWS", False):
        restrict_file_permissions(d, directory=True)
    assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_windows_file_calls_icacls(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("data")
    with (
        patch("iac_code.utils.file_security._IS_WINDOWS", True),
        patch.dict(os.environ, {"USERNAME": "testuser"}),
        patch("subprocess.run") as mock_run,
    ):
        restrict_file_permissions(f, directory=False)
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "icacls"
    assert str(f) in args
    assert any('"testuser":(R,W)' in a for a in args)


def test_windows_directory_calls_icacls_with_full_control(tmp_path):
    d = tmp_path / "secret_dir"
    d.mkdir()
    with (
        patch("iac_code.utils.file_security._IS_WINDOWS", True),
        patch.dict(os.environ, {"USERNAME": "testuser"}),
        patch("subprocess.run") as mock_run,
    ):
        restrict_file_permissions(d, directory=True)
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert any('"testuser":(F)' in a for a in args)


def test_windows_icacls_failure_silently_ignored(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("data")
    with (
        patch("iac_code.utils.file_security._IS_WINDOWS", True),
        patch.dict(os.environ, {"USERNAME": "testuser"}),
        patch("subprocess.run", side_effect=OSError("not found")),
    ):
        restrict_file_permissions(f, directory=False)


def test_unix_chmod_failure_silently_ignored(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("data")
    with patch("iac_code.utils.file_security._IS_WINDOWS", False), patch("os.chmod", side_effect=OSError("perm")):
        restrict_file_permissions(f, directory=False)
