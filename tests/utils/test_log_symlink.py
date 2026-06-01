"""Tests for symlink fallback in iac_code.utils.log."""

import sys
from unittest.mock import patch

import pytest
from loguru import logger

from iac_code.utils.log import enable_debug_at_runtime, setup_logging


def test_setup_logging_symlink_oserror_falls_back_to_copy(tmp_path, monkeypatch):
    """When symlink_to raises OSError, setup_logging should not crash (falls back to copy)."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    with patch("iac_code.utils.log.Path.symlink_to", side_effect=OSError("WinError 1314")):
        setup_logging(session_id="fallback1", debug=False)

    log_file = tmp_path / "logs" / "fallback1.log"
    assert log_file.exists()
    latest = tmp_path / "logs" / "latest.log"
    # Fallback copies the file, so latest.log should exist as a regular file
    assert latest.exists()
    assert not latest.is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
def test_setup_logging_files_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    setup_logging(session_id="private", debug=False)

    log_dir = tmp_path / "logs"
    log_file = log_dir / "private.log"
    assert oct(log_dir.stat().st_mode & 0o777) == "0o700"
    assert oct(log_file.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
def test_setup_logging_copy_fallback_latest_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    with patch("iac_code.utils.log.Path.symlink_to", side_effect=OSError("WinError 1314")):
        setup_logging(session_id="private-fallback", debug=False)

    latest = tmp_path / "logs" / "latest.log"
    assert latest.exists()
    assert not latest.is_symlink()
    assert oct(latest.stat().st_mode & 0o777) == "0o600"


def test_enable_debug_symlink_oserror_falls_back(tmp_path, monkeypatch):
    """When symlink_to raises OSError, enable_debug_at_runtime should not crash."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    # First set up logging normally (need to bypass symlink for setup too)
    with patch("iac_code.utils.log.Path.symlink_to", side_effect=OSError("WinError 1314")):
        setup_logging(session_id="fallback2", debug=False)
        result = enable_debug_at_runtime("fallback2")

    assert result == tmp_path / "logs" / "fallback2.log"
    latest = tmp_path / "logs" / "latest.log"
    assert latest.exists()


def test_setup_logging_symlink_works_normally(tmp_path, monkeypatch):
    """Normal case: symlink is created successfully."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    setup_logging(session_id="normal1", debug=False)

    latest = tmp_path / "logs" / "latest.log"
    assert latest.is_symlink()
    assert latest.resolve() == (tmp_path / "logs" / "normal1.log").resolve()
