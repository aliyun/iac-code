"""W-I6: _read_git_head must NOT spawn subprocess (perf-critical path)."""

from __future__ import annotations


def test_read_git_head_does_not_call_subprocess(monkeypatch, tmp_path):
    """Regression guard against accidentally reverting to subprocess.run('git ...')."""
    import subprocess

    def fail_subprocess(*args, **kwargs):
        raise AssertionError("_read_git_head should not call subprocess")

    monkeypatch.setattr(subprocess, "run", fail_subprocess)
    monkeypatch.setattr(subprocess, "Popen", fail_subprocess)
    monkeypatch.setattr(subprocess, "check_output", fail_subprocess)
    monkeypatch.setattr(subprocess, "check_call", fail_subprocess)

    # Initialize a fake git repo on disk.
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "main").write_text("abc123def456\n", encoding="utf-8")

    from iac_code.utils.project_paths import _read_git_head

    is_repo, head = _read_git_head(str(tmp_path))
    assert is_repo is True, "should detect .git as a git repo"
    # Head should contain either the ref or the SHA, depending on implementation.
    assert head and ("main" in head or "abc123def456" in head)


def test_read_git_head_non_git_dir_returns_false(monkeypatch, tmp_path):
    """Non-git directory should return is_repo=False without crashing."""
    import subprocess

    def fail_subprocess(*args, **kwargs):
        raise AssertionError("no subprocess allowed")

    monkeypatch.setattr(subprocess, "run", fail_subprocess)

    from iac_code.utils.project_paths import _read_git_head

    is_repo, _ = _read_git_head(str(tmp_path))
    assert is_repo is False
