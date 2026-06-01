"""Project-partitioned session storage paths and git helpers.

Sessions live under ``~/.iac-code/projects/<sanitize(cwd)>/<session_id>.jsonl``.

The directory name encodes the project's working directory; the same
``cwd`` always maps to the same directory, so listing sessions for a
project is just a directory scan.
"""

from __future__ import annotations

import os
import re
from hashlib import blake2b
from pathlib import Path

from iac_code.config import get_config_dir

MAX_SANITIZED_LENGTH = 200
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def sanitize_path(name: str) -> str:
    """Replace every non-alphanumeric character with ``-``.

    Long names are truncated and a short hash is appended to keep
    uniqueness while staying within filesystem name limits.
    """
    sanitized = _NON_ALNUM.sub("-", name)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    digest = blake2b(name.encode("utf-8"), digest_size=6).hexdigest()
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{digest}"


def get_projects_dir() -> Path:
    """Root directory holding all per-project session folders."""
    return get_config_dir() / "projects"


def get_project_dir(cwd: str) -> Path:
    """Directory holding session files for a specific working directory."""
    return get_projects_dir() / sanitize_path(cwd)


def get_session_path(cwd: str, session_id: str) -> Path:
    """JSONL file path for a session belonging to ``cwd``."""
    return get_project_dir(cwd) / f"{session_id}.jsonl"


def _resolve_git_dir(worktree_root: str) -> str | None:
    """Given a worktree root, return the absolute path of its git dir.

    For a normal repo, ``<worktree_root>/.git`` is a directory and is
    itself the git dir. For a linked worktree or submodule,
    ``<worktree_root>/.git`` is a file whose first line is
    ``gitdir: <path>`` pointing at the real git dir (absolute or relative
    to *worktree_root*).
    """
    git_path = os.path.join(worktree_root, ".git")
    if os.path.isdir(git_path):
        return git_path
    try:
        with open(git_path, encoding="utf-8") as f:
            line = f.read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir: "):
        return None
    gitdir = line[len("gitdir: ") :]
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(worktree_root, gitdir)
    return gitdir


def find_git_worktree_root(cwd: str) -> Path | None:
    """Return the git worktree root for *cwd*, or ``None`` outside git.

    Walks up from *cwd* looking for ``.git`` (directory for a normal repo,
    file for a linked worktree or submodule). The worktree root is the
    directory containing the ``.git`` entry.

    Pure-Python — never spawns ``git``. On Windows
    ``subprocess.run(["git", ...], timeout=...)`` can hang the asyncio
    event loop because git-for-windows leaves grandchild helper processes
    holding the captured stdout/stderr pipes; after timeout fires and
    ``process.kill()`` runs, the second ``communicate()`` blocks forever.
    """
    current = os.path.abspath(cwd)
    while True:
        git_path = os.path.join(current, ".git")
        if os.path.isdir(git_path) or os.path.isfile(git_path):
            return Path(current).resolve()
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _read_git_head(cwd: str) -> tuple[bool, str]:
    """Walk up from *cwd* looking for ``.git``; if found, read ``HEAD``.

    Returns ``(is_git_repo, head_content)`` where *head_content* is the
    raw trimmed content of the ``HEAD`` file (e.g.
    ``"ref: refs/heads/main"`` or a full SHA), or an empty string if HEAD
    cannot be read.
    """
    root = find_git_worktree_root(cwd)
    if root is None:
        return False, ""
    git_dir = _resolve_git_dir(str(root))
    if git_dir is None:
        return True, ""
    try:
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as f:
            return True, f.read().strip()
    except OSError:
        return True, ""


def get_git_branch(cwd: str) -> str | None:
    """Return the current git branch name at ``cwd``, or ``None``.

    ``None`` means either ``cwd`` is not inside a git repo or HEAD is
    detached.
    """
    is_repo, head = _read_git_head(cwd)
    if not is_repo:
        return None
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/") :]
    return None
