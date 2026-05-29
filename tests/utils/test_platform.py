# tests/utils/test_platform.py
from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


class TestPlatformInfoDetectWindows:
    """Test PlatformInfo.detect() on Windows (mocked)."""

    @patch("sys.platform", "win32")
    @patch.dict(os.environ, {"IAC_CODE_GIT_BASH_PATH": "/fake/bash.exe"})
    @patch("os.path.isfile", return_value=True)
    def test_env_var_override(self, mock_isfile):
        from iac_code.utils.platform import PlatformInfo, _clear_cache

        _clear_cache()
        info = PlatformInfo.detect()
        assert info.os_kind == "Windows"
        assert info.shell_path == "/fake/bash.exe"
        assert info.shell_name == "bash"

    @patch("sys.platform", "win32")
    @patch.dict(os.environ, {}, clear=True)
    @patch("os.path.isfile")
    @patch("subprocess.run")
    def test_where_git_discovery(self, mock_run, mock_isfile):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="C:\\Program Files\\Git\\cmd\\git.exe\n",
        )
        mock_isfile.side_effect = lambda p: p == "C:\\Program Files\\Git\\bin\\bash.exe"

        from iac_code.utils.platform import PlatformInfo, _clear_cache

        _clear_cache()
        info = PlatformInfo.detect()
        assert info.shell_path == "C:\\Program Files\\Git\\bin\\bash.exe"

    @patch("sys.platform", "win32")
    @patch.dict(os.environ, {}, clear=True)
    @patch("os.path.isfile", return_value=False)
    @patch("subprocess.run", side_effect=OSError("not found"))
    def test_git_bash_not_found_raises(self, mock_run, mock_isfile):
        from iac_code.utils.platform import GitBashNotFoundError, PlatformInfo, _clear_cache

        _clear_cache()
        with pytest.raises(GitBashNotFoundError, match="Git for Windows"):
            PlatformInfo.detect()

    @patch("sys.platform", "win32")
    @patch.dict(os.environ, {}, clear=True)
    @patch("os.path.isfile")
    @patch("subprocess.run", return_value=MagicMock(returncode=1, stdout=""))
    def test_fallback_common_paths(self, mock_run, mock_isfile):
        mock_isfile.side_effect = lambda p: p == r"C:\Program Files\Git\bin\bash.exe"

        from iac_code.utils.platform import PlatformInfo, _clear_cache

        _clear_cache()
        info = PlatformInfo.detect()
        assert info.shell_path == r"C:\Program Files\Git\bin\bash.exe"


class TestPlatformInfoDetectUnix:
    """Test PlatformInfo.detect() on Unix."""

    @patch("sys.platform", "linux")
    @patch("os.path.isfile")
    def test_finds_bash(self, mock_isfile):
        mock_isfile.side_effect = lambda p: p == "/bin/bash"

        from iac_code.utils.platform import PlatformInfo, _clear_cache

        _clear_cache()
        info = PlatformInfo.detect()
        assert info.os_kind == "Linux"
        assert info.shell_path == "/bin/bash"
        assert info.shell_name == "bash"

    @patch("sys.platform", "darwin")
    @patch("os.path.isfile", return_value=False)
    def test_falls_back_to_sh(self, mock_isfile):
        from iac_code.utils.platform import PlatformInfo, _clear_cache

        _clear_cache()
        info = PlatformInfo.detect()
        assert info.os_kind == "macOS"
        assert info.shell_path == "/bin/sh"
        assert info.shell_name == "sh"


class TestCaching:
    @patch("sys.platform", "linux")
    @patch("os.path.isfile", return_value=True)
    def test_detect_caches_result(self, mock_isfile):
        from iac_code.utils.platform import PlatformInfo, _clear_cache

        _clear_cache()
        info1 = PlatformInfo.detect()
        info2 = PlatformInfo.detect()
        assert info1 is info2


class TestNormalizeUserPath:
    """normalize_user_path: platform-aware POSIX->Windows path conversion."""

    @patch("iac_code.utils.platform.sys")
    def test_passthrough_on_linux(self, mock_sys):
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "linux"
        assert normalize_user_path("/c/Users/foo") == "/c/Users/foo"
        assert normalize_user_path("/cygdrive/c/foo") == "/cygdrive/c/foo"
        assert normalize_user_path("//server/share") == "//server/share"

    @patch("iac_code.utils.platform.sys")
    def test_passthrough_on_macos(self, mock_sys):
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "darwin"
        assert normalize_user_path("/c/Users/foo") == "/c/Users/foo"

    @patch("iac_code.utils.platform.sys")
    def test_windows_converts_msys_drive(self, mock_sys):
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "win32"
        assert normalize_user_path("/c/Users/foo") == "C:\\Users\\foo"

    @patch("iac_code.utils.platform.sys")
    def test_windows_converts_cygdrive(self, mock_sys):
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "win32"
        assert normalize_user_path("/cygdrive/c/foo") == "C:\\foo"

    @patch("iac_code.utils.platform.sys")
    def test_windows_converts_unc(self, mock_sys):
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "win32"
        assert normalize_user_path("//server/share") == "\\\\server\\share"

    @patch("iac_code.utils.platform.sys")
    def test_windows_passes_through_non_msys_posix(self, mock_sys):
        """A path like /foo (no drive letter after the slash) on Windows is
        passed through unchanged -- it's not an MSYS drive shape."""
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "win32"
        assert normalize_user_path("/foo") == "/foo"

    @patch("iac_code.utils.platform.sys")
    def test_windows_passes_through_already_windows(self, mock_sys):
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "win32"
        assert normalize_user_path("C:\\Users\\foo") == "C:\\Users\\foo"

    @patch("iac_code.utils.platform.sys")
    def test_windows_passes_through_relative(self, mock_sys):
        from iac_code.utils.platform import normalize_user_path

        mock_sys.platform = "win32"
        assert normalize_user_path("relative/path") == "relative/path"


class TestKillProcessTree:
    """kill_process_tree: platform-aware process tree cleanup."""

    @pytest.mark.asyncio
    async def test_windows_taskkill_success(self):
        from iac_code.utils.platform import kill_process_tree

        proc = MagicMock()
        proc.pid = 12345

        with (
            patch("iac_code.utils.platform.sys.platform", "win32"),
            patch("iac_code.utils.platform.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            await kill_process_tree(proc)

        mock_run.assert_called_once_with(
            ["taskkill", "/PID", "12345", "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_windows_taskkill_failure_falls_back_to_kill(self):
        from iac_code.utils.platform import kill_process_tree

        proc = MagicMock()
        proc.pid = 12345

        with (
            patch("iac_code.utils.platform.sys.platform", "win32"),
            patch("iac_code.utils.platform.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            await kill_process_tree(proc)

        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_windows_taskkill_timeout_falls_back_to_kill(self):
        from iac_code.utils.platform import kill_process_tree

        proc = MagicMock()
        proc.pid = 12345

        with (
            patch("iac_code.utils.platform.sys.platform", "win32"),
            patch("iac_code.utils.platform.subprocess.run", side_effect=subprocess.TimeoutExpired("taskkill", 5)),
        ):
            await kill_process_tree(proc)

        proc.kill.assert_called_once()

    @pytest.mark.skipif(sys.platform == "win32", reason="os.killpg not available on Windows")
    @pytest.mark.asyncio
    async def test_unix_uses_killpg(self):
        from iac_code.utils.platform import kill_process_tree

        proc = MagicMock()
        proc.pid = 12345

        with (
            patch("iac_code.utils.platform.sys.platform", "linux"),
            patch("iac_code.utils.platform.os.killpg") as mock_killpg,
        ):
            await kill_process_tree(proc)

        import signal

        mock_killpg.assert_called_once_with(12345, signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_pid_none_noop(self):
        from iac_code.utils.platform import kill_process_tree

        proc = MagicMock()
        proc.pid = None

        await kill_process_tree(proc)
        proc.kill.assert_not_called()


class TestNpmmirrorCmd:
    """Regression: install args must show progress, not run fully silent."""

    def test_uses_silent_not_verysilent(self):
        from iac_code.utils.platform import _NPMMIRROR_CMD

        assert "/SILENT /NORESTART" in _NPMMIRROR_CMD
        assert "/VERYSILENT" not in _NPMMIRROR_CMD


class TestGitBashHint:
    """Hint must reference the new subcommand and must not contain the
    long PowerShell one-liner that Rich wraps and PowerShell rejects on paste."""

    def test_hint_references_install_git_bash_subcommand(self):
        from iac_code.utils.platform import _git_bash_hint

        text = _git_bash_hint()
        assert "iac-code install-git-bash" in text

    def test_hint_does_not_contain_long_powershell_oneliner(self):
        from iac_code.utils.platform import _git_bash_hint

        text = _git_bash_hint()
        assert "Invoke-RestMethod" not in text
        assert "Invoke-WebRequest" not in text
        assert "Start-Process" not in text

    def test_hint_still_mentions_winget(self):
        from iac_code.utils.platform import _git_bash_hint

        text = _git_bash_hint()
        assert "winget install --id Git.Git" in text

    def test_hint_still_mentions_env_var_override(self):
        from iac_code.utils.platform import _git_bash_hint

        text = _git_bash_hint()
        assert "IAC_CODE_GIT_BASH_PATH" in text
