# tests/utils/test_windows_paths.py
"""Tests for src/iac_code/utils/windows_paths.py — pure string conversion."""

from __future__ import annotations


class TestPosixPathToWindows:
    """posix_path_to_windows() is a pure function; no platform dependency."""

    def test_msys_drive_lowercase(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("/c/Users/foo") == "C:\\Users\\foo"

    def test_msys_drive_uppercase(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("/C/Users/foo") == "C:\\Users\\foo"

    def test_msys_drive_root_only(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("/c") == "C:\\"
        assert posix_path_to_windows("/c/") == "C:\\"

    def test_cygdrive(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("/cygdrive/c/Users/foo") == "C:\\Users\\foo"

    def test_cygdrive_root_only(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("/cygdrive/c") == "C:\\"
        assert posix_path_to_windows("/cygdrive/c/") == "C:\\"

    def test_unc_path(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("//server/share/foo") == "\\\\server\\share\\foo"

    def test_relative_path_flips_slashes(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("relative/path") == "relative\\path"

    def test_already_windows_path_passthrough(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("C:\\Users\\foo") == "C:\\Users\\foo"

    def test_empty_string(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("") == ""

    def test_single_slash(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("/") == "\\"

    def test_drive_with_subpath_keeps_uppercase(self):
        from iac_code.utils.windows_paths import posix_path_to_windows

        assert posix_path_to_windows("/d/Projects/iac-code") == "D:\\Projects\\iac-code"
