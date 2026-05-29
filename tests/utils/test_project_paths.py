"""Tests for the project-path sanitizer and helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from iac_code.utils.project_paths import (
    MAX_SANITIZED_LENGTH,
    get_git_branch,
    sanitize_path,
)


class TestSanitizePath:
    def test_replaces_non_alnum_with_dash(self):
        assert sanitize_path("/Users/x/proj") == "-Users-x-proj"
        assert sanitize_path("/tmp/my proj.git") == "-tmp-my-proj-git"

    def test_preserves_alphanumerics(self):
        assert sanitize_path("abc123") == "abc123"

    def test_long_path_gets_hash_suffix(self):
        original = "x" * (MAX_SANITIZED_LENGTH + 50)
        result = sanitize_path(original)
        # Length should be MAX_SANITIZED_LENGTH + dash + hash
        assert len(result) > MAX_SANITIZED_LENGTH
        assert result.startswith("x" * MAX_SANITIZED_LENGTH)
        assert "-" in result[MAX_SANITIZED_LENGTH:]

    def test_long_paths_are_unique_per_input(self):
        a = "a" * (MAX_SANITIZED_LENGTH + 1)
        b = "b" * (MAX_SANITIZED_LENGTH + 1)
        assert sanitize_path(a) != sanitize_path(b)

    def test_unicode_replaced(self):
        # Chinese characters are non-ASCII non-alnum → replaced with dashes
        assert sanitize_path("/projects/中文/repo") == "-projects----repo"

    def test_empty_string(self):
        assert sanitize_path("") == ""


class TestGetGitBranch:
    """Regression: ``get_git_branch`` must not spawn ``git``.

    Background: ``subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
    timeout=2.0)`` was observed to hang the asyncio event loop on Windows
    when invoked from the ACP server process — the ``timeout=2.0`` did not
    kick in because ``subprocess.run``'s second ``communicate()`` after
    ``kill()`` blocks waiting for stdout/stderr handles still held by
    grandchild processes spawned by git-for-windows. Reading ``.git/HEAD``
    directly sidesteps the issue.
    """

    def test_non_repo_returns_none(self, tmp_path: Path):
        assert get_git_branch(str(tmp_path)) is None

    def test_repo_returns_branch(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        assert get_git_branch(str(tmp_path)) == "main"

    def test_repo_subdir_walks_up(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert get_git_branch(str(sub)) == "feature/x"

    def test_detached_head_returns_none(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("abcdef0123456789abcdef0123456789abcdef01\n", encoding="utf-8")
        assert get_git_branch(str(tmp_path)) is None

    def test_worktree_with_absolute_gitdir_pointer(self, tmp_path: Path):
        real = tmp_path / "real"
        real.mkdir()
        real_git = real / ".git"
        real_git.mkdir()
        worktree_meta = real_git / "worktrees" / "wt"
        worktree_meta.mkdir(parents=True)
        (worktree_meta / "HEAD").write_text("ref: refs/heads/wt-branch\n", encoding="utf-8")

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {worktree_meta}\n", encoding="utf-8")

        assert get_git_branch(str(wt)) == "wt-branch"

    def test_worktree_with_relative_gitdir_pointer(self, tmp_path: Path):
        real_git = tmp_path / ".git"
        real_git.mkdir()
        worktree_meta = real_git / "worktrees" / "wt"
        worktree_meta.mkdir(parents=True)
        (worktree_meta / "HEAD").write_text("ref: refs/heads/rel-branch\n", encoding="utf-8")

        wt = tmp_path / "wt"
        wt.mkdir()
        # Relative path from wt/ to tmp_path/.git/worktrees/wt
        (wt / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")

        assert get_git_branch(str(wt)) == "rel-branch"

    def test_no_subprocess_call(self, tmp_path: Path):
        """Hard guarantee: detection never invokes subprocess."""
        with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
            get_git_branch(str(tmp_path))
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
