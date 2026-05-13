"""Project-partitioned session storage paths and git helpers.

Sessions live under ``~/.iac-code/projects/<sanitize(cwd)>/<session_id>.jsonl``.

The directory name encodes the project's working directory; the same
``cwd`` always maps to the same directory, so listing sessions for a
project is just a directory scan.
"""

from __future__ import annotations

import re
import subprocess
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


def get_git_branch(cwd: str) -> str | None:
    """Return the current git branch name at ``cwd``, or ``None``.

    ``None`` means either ``cwd`` is not inside a git repo, ``git`` is
    unavailable, or the call timed out. Detached HEADs return ``"HEAD"``
    from ``rev-parse --abbrev-ref``; we treat that as ``None``.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch
