"""Tests for bash path constraint validation."""

from __future__ import annotations

from unittest.mock import patch

from iac_code.tools.bash.command_parser import SimpleCommand
from iac_code.tools.bash.path_validation import _resolve_candidate, check_path_constraints, validate_path


class TestValidatePath:
    def test_path_within_cwd(self, tmp_path):
        target = str(tmp_path / "sub" / "file.txt")
        assert validate_path(target, str(tmp_path), []) == "allow"

    def test_path_outside_cwd(self, tmp_path):
        assert validate_path("/etc/passwd", str(tmp_path), []) == "deny"

    def test_path_in_additional_dir(self, tmp_path):
        assert validate_path("/shared/libs/foo.py", str(tmp_path), ["/shared/libs"]) == "allow"


class TestResolveCandidateNormalization:
    def test_git_bash_path_normalized_on_windows(self):
        with (
            patch("iac_code.utils.platform.sys.platform", "win32"),
            patch("os.path.isabs", side_effect=lambda p: p.startswith("C:") or p.startswith("/")),
        ):
            resolved = _resolve_candidate("/c/Users/me/repo/file.txt", r"C:\Users\me\repo")
        assert resolved == r"C:\Users\me\repo\file.txt"

    def test_relative_path_unchanged(self, tmp_path):
        resolved = _resolve_candidate("sub/file.txt", str(tmp_path))
        import os

        assert resolved == os.path.normpath(str(tmp_path / "sub" / "file.txt"))


class TestCheckPathConstraints:
    def test_no_paths_passthrough(self, tmp_path):
        cmd = SimpleCommand(text="echo hello", argv=["echo", "hello"], redirects=[])
        r = check_path_constraints(cmd, str(tmp_path), [])
        assert r.behavior == "passthrough"

    def test_rm_outside_cwd(self, tmp_path):
        cmd = SimpleCommand(text="rm /etc/passwd", argv=["rm", "/etc/passwd"], redirects=[])
        r = check_path_constraints(cmd, str(tmp_path), [])
        assert r.behavior in ("ask", "deny")

    def test_redirect_to_dev_null_passthrough(self, tmp_path):
        cmd = SimpleCommand(text="echo ok >/dev/null", argv=["echo", "ok"], redirects=[">/dev/null"])
        r = check_path_constraints(cmd, str(tmp_path), [])
        assert r.behavior == "passthrough"

    def test_redirect_to_dev_stderr_passthrough(self, tmp_path):
        cmd = SimpleCommand(text="echo err >/dev/stderr", argv=["echo", "err"], redirects=[">/dev/stderr"])
        r = check_path_constraints(cmd, str(tmp_path), [])
        assert r.behavior == "passthrough"

    def test_redirect_to_dev_null_with_real_path_still_checks(self, tmp_path):
        cmd = SimpleCommand(
            text="cp /etc/passwd /dev/null",
            argv=["cp", "/etc/passwd", "/dev/null"],
            redirects=[],
        )
        r = check_path_constraints(cmd, str(tmp_path), [])
        assert r.behavior in ("ask", "deny")

    def test_rm_dev_null_argv_not_skipped(self, tmp_path):
        """Pseudo-device skip only applies to redirects, not argv paths."""
        cmd = SimpleCommand(text="rm /dev/null", argv=["rm", "/dev/null"], redirects=[])
        r = check_path_constraints(cmd, str(tmp_path), [])
        assert r.behavior in ("ask", "deny")
