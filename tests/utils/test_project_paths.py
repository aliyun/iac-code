"""Tests for the project-path sanitizer and helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from iac_code.utils.project_paths import (
    MAX_SANITIZED_LENGTH,
    WINDOWS_SESSION_PROJECT_DIR_MAX_LENGTH,
    find_git_worktree_root,
    format_resume_command,
    get_git_branch,
    get_project_dir,
    project_dir_candidates,
    same_project_path,
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
        # Legacy sanitizer behavior is retained for non-session callers.
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


def test_get_project_dir_prefers_existing_legacy_long_directory(tmp_path, monkeypatch):
    import iac_code.utils.project_paths as project_paths

    cwd = "x" * (MAX_SANITIZED_LENGTH + 50)
    legacy_name = project_paths._legacy_sanitize_path(cwd)
    generated = project_dir_candidates(cwd, tmp_path / "projects", platform="win32")
    assert generated[2].name == legacy_name

    current_dir = tmp_path / "projects" / "current-bounded"
    legacy_dir = tmp_path / "projects" / "legacy-existing"
    legacy_dir.mkdir(parents=True)
    monkeypatch.setattr(project_paths, "project_dir_candidates", lambda _cwd: (current_dir, legacy_dir))

    assert get_project_dir(cwd) == legacy_dir


def test_get_project_dir_uses_bounded_component_for_new_long_directory(tmp_path, monkeypatch):
    cwd = "x" * (MAX_SANITIZED_LENGTH + 50)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    project_dir = get_project_dir(cwd)

    assert len(project_dir.name) <= MAX_SANITIZED_LENGTH


def test_session_component_bounded_for_medium_cwd_below_legacy_threshold(tmp_path, monkeypatch):
    """A cwd that is long for Windows but under ``MAX_SANITIZED_LENGTH`` must
    still yield a bounded write component so the nested session layout fits
    within ``MAX_PATH`` — while the untruncated legacy alias stays readable."""
    cwd = "/" + "medium-project/" * 8  # ~120 chars, well under MAX_SANITIZED_LENGTH
    assert WINDOWS_SESSION_PROJECT_DIR_MAX_LENGTH < len(cwd) <= MAX_SANITIZED_LENGTH
    candidates = project_dir_candidates(cwd, tmp_path, platform="win32")
    write_dir, legacy_dir = candidates

    # New writes use the aggressively bounded component ...
    assert len(write_dir.name) <= WINDOWS_SESSION_PROJECT_DIR_MAX_LENGTH
    # ... but the pre-fix untruncated directory remains a read candidate, so
    # sessions already stored there stay discoverable after upgrade.
    assert legacy_dir.name == sanitize_path(cwd)
    assert len(legacy_dir.name) == len(cwd)


def test_windows_long_session_candidates_include_previous_200_character_alias(tmp_path):
    cwd = "C:\\" + "nested-workspace\\" * 30

    current_dir, previous_dir, legacy_dir = project_dir_candidates(cwd, tmp_path, platform="win32")

    assert len(current_dir.name) == WINDOWS_SESSION_PROJECT_DIR_MAX_LENGTH
    assert len(previous_dir.name) == MAX_SANITIZED_LENGTH
    assert len(legacy_dir.name) == MAX_SANITIZED_LENGTH + 13
    assert current_dir.name[-13:] == previous_dir.name[-13:] == legacy_dir.name[-13:]


def test_session_component_keeps_windows_leaf_within_max_path(tmp_path, monkeypatch):
    """Deepest realistic leaf under a typical Windows config root must fit in 260."""
    cwd = "C:\\Users\\somebody\\" + "nested-workspace\\" * 12 + "service"
    write_dir = project_dir_candidates(cwd, tmp_path, platform="win32")[0]

    config_root = "C:\\Users\\somebody\\.iac-code\\projects"
    session_id = "0123456789abcdef0123456789abcdef"  # 32-char uuid hex
    # <root>\<component>\<session_id>.conflict-sidecars\session.jsonl is the deepest leaf.
    leaf_len = (
        len(config_root)
        + 1
        + len(write_dir.name)
        + 1
        + len(session_id)
        + len(".conflict-sidecars")
        + 1
        + len("session.jsonl")
    )
    assert leaf_len < 260


def test_non_windows_session_component_keeps_existing_length(tmp_path):
    cwd = "/" + "medium-project/" * 8

    write_dir = project_dir_candidates(cwd, tmp_path, platform="linux")[0]

    assert write_dir.name == sanitize_path(cwd)


class TestProjectPathComparison:
    def test_windows_drive_case_and_separators_match(self):
        assert same_project_path(r"C:\Users\Me\Repo", "c:/Users/Me/Repo")


class TestFormatResumeCommand:
    def test_windows_command_quotes_for_cmd_exe(self):
        command = format_resume_command(r"C:\Users\Me\iac repo & unsafe", "abc & unsafe", platform="win32")

        assert command == r'cd /d "C:\Users\Me\iac repo & unsafe" && iac-code --resume "abc & unsafe"'

    def test_windows_unc_command_uses_pushd(self):
        command = format_resume_command(r"\\server\share\My Repo", "abc & unsafe", platform="win32")

        assert command == r'pushd "\\server\share\My Repo" && iac-code --resume "abc & unsafe" & popd'

    def test_windows_command_escapes_cmd_expansion_characters(self):
        command = format_resume_command(r"C:\Users\%USERNAME%\repo^name!", "abc%id^!", platform="win32")

        assert command == r'cd /d "C:\Users\^%USERNAME^%\repo^^name^!" && iac-code --resume "abc^%id^^^!"'

    def test_posix_command_keeps_shell_quoting(self):
        command = format_resume_command("/project a;unsafe", "abc123", platform="linux")

        assert command == "cd '/project a;unsafe' && iac-code --resume abc123"


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


class TestFindGitWorktreeRoot:
    """Regression: ``find_git_worktree_root`` must not spawn ``git``.

    Same Windows-asyncio-hang reason as :class:`TestGetGitBranch`.
    """

    def test_outside_repo_returns_none(self, tmp_path: Path):
        assert find_git_worktree_root(str(tmp_path)) is None

    def test_repo_at_cwd(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        assert find_git_worktree_root(str(tmp_path)) == tmp_path.resolve()

    def test_repo_subdir_walks_up(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert find_git_worktree_root(str(sub)) == tmp_path.resolve()

    def test_symlinked_repo_returns_logical_worktree_root(self, tmp_path: Path):
        physical = tmp_path / "mount-root" / "oss" / "bucket"
        physical.mkdir(parents=True)
        (physical / ".git").mkdir()
        logical = tmp_path / "workspace"
        logical.symlink_to(physical, target_is_directory=True)
        sub = logical / "ctx-1"
        sub.mkdir()

        assert find_git_worktree_root(str(sub)) == logical

    def test_worktree_with_absolute_gitdir_pointer(self, tmp_path: Path):
        """A linked worktree's root is the dir containing its .git file."""
        real_git = tmp_path / "real" / ".git"
        real_git.mkdir(parents=True)
        meta = real_git / "worktrees" / "wt"
        meta.mkdir(parents=True)

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {meta}\n", encoding="utf-8")

        assert find_git_worktree_root(str(wt)) == wt.resolve()

    def test_worktree_with_relative_gitdir_pointer(self, tmp_path: Path):
        real_git = tmp_path / ".git"
        real_git.mkdir()
        meta = real_git / "worktrees" / "wt"
        meta.mkdir(parents=True)

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")

        assert find_git_worktree_root(str(wt)) == wt.resolve()

    def test_no_subprocess_call(self, tmp_path: Path):
        """Hard guarantee: detection never invokes subprocess."""
        (tmp_path / ".git").mkdir()
        with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
            find_git_worktree_root(str(tmp_path))
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
