"""Tests for os.replace retry logic on Windows file locking."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from iac_code.utils.file_security import safe_replace as _safe_replace


def test_safe_replace_succeeds_first_try(tmp_path: Path):
    """Normal case: replace succeeds immediately."""
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    _safe_replace(str(src), str(dst))

    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_safe_replace_retries_on_permission_error(tmp_path: Path):
    """On PermissionError, retries up to 3 times."""
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    call_count = [0]
    original_replace = os.replace

    def _mock_replace(s, d):
        call_count[0] += 1
        if call_count[0] < 3:
            raise PermissionError("file locked")
        return original_replace(s, d)

    with patch("os.replace", side_effect=_mock_replace), patch("time.sleep"):
        _safe_replace(str(src), str(dst))

    assert call_count[0] == 3


def test_safe_replace_raises_after_max_retries(tmp_path: Path):
    """After 3 retries, PermissionError propagates."""
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    with patch("os.replace", side_effect=PermissionError("locked")), patch("time.sleep"):
        with pytest.raises(PermissionError):
            _safe_replace(str(src), str(dst))


def test_safe_replace_non_permission_error_raises_immediately(tmp_path: Path):
    """Non-PermissionError exceptions are not retried."""
    with patch("os.replace", side_effect=FileNotFoundError("gone")):
        with pytest.raises(FileNotFoundError):
            _safe_replace("nonexistent", "dst")
