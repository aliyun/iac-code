"""Tests for cross-platform file permission restriction."""

from __future__ import annotations

import os
import sys
from pathlib import Path
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


@pytest.mark.skipif(sys.platform == "win32", reason="chmod has no effect on Windows NTFS")
def test_ensure_private_dir_creates_and_restricts(tmp_path):
    from iac_code.utils.file_security import ensure_private_dir

    path = tmp_path / "a" / "private"
    with patch("iac_code.utils.file_security._IS_WINDOWS", False):
        result = ensure_private_dir(path)

    assert result == path
    assert path.is_dir()
    assert oct(path.stat().st_mode & 0o777) == "0o700"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod has no effect on Windows NTFS")
def test_ensure_private_file_restricts_existing_file(tmp_path):
    from iac_code.utils.file_security import ensure_private_file

    path = tmp_path / "secret.txt"
    path.write_text("secret", encoding="utf-8")
    with patch("iac_code.utils.file_security._IS_WINDOWS", False):
        result = ensure_private_file(path)

    assert result == path
    assert oct(path.stat().st_mode & 0o777) == "0o600"


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


def test_atomic_write_text_uses_same_dir_temp_file_fsync_and_safe_replace(monkeypatch, tmp_path):
    from iac_code.utils import file_security

    path = tmp_path / "settings.yml"
    mkstemp_calls = []
    fsync_calls = []
    replace_calls = []
    original_mkstemp = file_security.tempfile.mkstemp
    original_fsync = file_security.os.fsync
    original_safe_replace = file_security.safe_replace

    def spy_mkstemp(*, prefix, suffix, dir):
        mkstemp_calls.append({"prefix": prefix, "suffix": suffix, "dir": dir})
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    def spy_fsync(fd):
        fsync_calls.append(fd)
        original_fsync(fd)

    def spy_safe_replace(src, dst):
        replace_calls.append((Path(src), Path(dst)))
        original_safe_replace(src, dst)

    monkeypatch.setattr(file_security.tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(file_security.os, "fsync", spy_fsync)
    monkeypatch.setattr(file_security, "safe_replace", spy_safe_replace)

    file_security.atomic_write_text(path, "answer: 42\n")

    assert path.read_text(encoding="utf-8") == "answer: 42\n"
    assert mkstemp_calls == [{"prefix": ".settings.yml.", "suffix": ".tmp", "dir": tmp_path}]
    assert fsync_calls
    assert len(replace_calls) == 1
    temp_path, final_path = replace_calls[0]
    assert temp_path.parent == tmp_path
    assert final_path == path
    assert not list(tmp_path.glob(".settings.yml.*.tmp"))


def test_atomic_write_text_removes_temp_file_when_replace_fails(monkeypatch, tmp_path):
    from iac_code.utils import file_security

    path = tmp_path / "settings.yml"

    def fail_replace(_src, _dst):
        raise PermissionError("locked")

    monkeypatch.setattr(file_security, "safe_replace", fail_replace)

    with pytest.raises(PermissionError, match="locked"):
        file_security.atomic_write_text(path, "answer: 42\n")

    assert not path.exists()
    assert not list(tmp_path.glob(".settings.yml.*.tmp"))
