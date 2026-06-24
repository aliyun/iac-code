"""Cross-platform file permission restriction."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from iac_code.utils.state_io import atomic_write_text as durable_atomic_write_text
from iac_code.utils.state_io import safe_replace as durable_safe_replace

_IS_WINDOWS = sys.platform == "win32"
# Kept as a module attribute for callers that patch atomic_write_text internals.
_TEMPFILE_FOR_COMPAT = tempfile


def safe_replace(src: str | Path, dst: str | Path) -> None:
    """os.replace with retry for Windows file locking."""
    durable_safe_replace(src, dst)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with text content."""
    durable_atomic_write_text(path, content, encoding=encoding, durable=True, _safe_replace=safe_replace)


def restrict_file_permissions(path: Path, *, directory: bool) -> None:
    """Restrict file/directory to owner-only access. Silent on failure."""
    if _IS_WINDOWS:
        _restrict_windows(path, directory=directory)
    else:
        try:
            os.chmod(path, 0o700 if directory else 0o600)
        except OSError:
            return


def ensure_private_dir(path: Path) -> Path:
    """Create a directory and restrict it to owner-only access."""
    path.mkdir(parents=True, exist_ok=True)
    restrict_file_permissions(path, directory=True)
    return path


def ensure_private_file(path: Path) -> Path:
    """Restrict an existing file to owner-only access."""
    if path.exists():
        restrict_file_permissions(path, directory=False)
    return path


def _restrict_windows(path: Path, *, directory: bool) -> None:
    username = os.environ.get("USERNAME", "")
    if not username:
        return
    perm = f'"{username}":(F)' if directory else f'"{username}":(R,W)'
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", perm],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
