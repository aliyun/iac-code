"""Tests for bash path constraint validation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from iac_code.tools.bash.command_parser import SimpleCommand
from iac_code.tools.bash.path_validation import (
    _resolve_candidate,
    check_path_constraints,
    check_read_path_constraints,
    validate_path,
)


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


class TestCheckReadPathConstraints:
    def test_cat_etc_passwd_asks_path_constraint(self, tmp_path):
        cmd = SimpleCommand(text="cat /etc/passwd", argv=["cat", "/etc/passwd"], redirects=[])
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    def test_cat_file_in_cwd_passthrough(self, tmp_path):
        cmd = SimpleCommand(text="cat file.txt", argv=["cat", "file.txt"], redirects=[])
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "passthrough"

    @pytest.mark.parametrize(
        ("text", "redirects"),
        [
            ("cat < /etc/passwd", ["< /etc/passwd"]),
            ("grep root </etc/passwd", ["</etc/passwd"]),
            ("sed -n 1p < /etc/passwd", ["< /etc/passwd"]),
        ],
    )
    def test_input_redirect_read_paths_outside_allowed_roots_ask(self, text, redirects, tmp_path):
        cmd = SimpleCommand(text=text, argv=text.split()[:1], redirects=redirects)
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.parametrize("path", ["~/notes.txt", "$HOME/notes.txt"])
    def test_shell_expanded_read_paths_ask(self, path, tmp_path):
        cmd = SimpleCommand(text=f"cat {path}", argv=["cat", path], redirects=[])
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    def test_grep_recursive_iac_code_asks_safety_check(self, tmp_path):
        cmd = SimpleCommand(text="grep -R token ~/.iac-code", argv=["grep", "-R", "token", "~/.iac-code"], redirects=[])
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "safety_check"

    def test_grep_pattern_not_treated_as_path(self, tmp_path):
        cmd = SimpleCommand(text="grep needle file.txt", argv=["grep", "needle", "file.txt"], redirects=[])
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "passthrough"

    @pytest.mark.parametrize(
        ("text", "argv"),
        [
            ("grep needle .env", ["grep", "needle", ".env"]),
            ("grep -f ~/.ssh/id_rsa file.txt", ["grep", "-f", "~/.ssh/id_rsa", "file.txt"]),
            ("grep --file ~/.ssh/id_rsa file.txt", ["grep", "--file", "~/.ssh/id_rsa", "file.txt"]),
            ("sed --file ~/.ssh/id_rsa file.txt", ["sed", "--file", "~/.ssh/id_rsa", "file.txt"]),
            ("fd pattern .env", ["fd", "pattern", ".env"]),
        ],
    )
    def test_read_paths_touching_sensitive_files_ask_safety_check(self, text, argv, tmp_path):
        cmd = SimpleCommand(text=text, argv=argv, redirects=[])
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "safety_check"

    @pytest.mark.parametrize(
        ("text", "argv"),
        [
            ("grep -n root /etc/passwd", ["grep", "-n", "root", "/etc/passwd"]),
            ("rg -n root /etc/passwd", ["rg", "-n", "root", "/etc/passwd"]),
            ("grep -e root /etc/passwd", ["grep", "-e", "root", "/etc/passwd"]),
            ("sed -e 1p /etc/passwd", ["sed", "-e", "1p", "/etc/passwd"]),
            ("sed -n 1p /etc/passwd", ["sed", "-n", "1p", "/etc/passwd"]),
            ("sed -f /etc/passwd file.txt", ["sed", "-f", "/etc/passwd", "file.txt"]),
            ("ls /etc", ["ls", "/etc"]),
            ("sha256sum /etc/passwd", ["sha256sum", "/etc/passwd"]),
            ("diff /etc/passwd file.txt", ["diff", "/etc/passwd", "file.txt"]),
            ("jq . /etc/passwd", ["jq", ".", "/etc/passwd"]),
            ("rg --files /etc", ["rg", "--files", "/etc"]),
            ("find -L /etc -maxdepth 1", ["find", "-L", "/etc", "-maxdepth", "1"]),
            ("tail -f /etc/passwd", ["tail", "-f", "/etc/passwd"]),
            ("cat -e /etc/passwd", ["cat", "-e", "/etc/passwd"]),
        ],
    )
    def test_read_paths_outside_allowed_roots_ask_path_constraint(self, text, argv, tmp_path):
        cmd = SimpleCommand(text=text, argv=argv, redirects=[])
        r = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"
