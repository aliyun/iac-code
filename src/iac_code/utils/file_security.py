"""Cross-platform file permission restriction."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"


def safe_replace(src: str, dst: str) -> None:
    """os.replace with retry for Windows file locking."""
    for attempt in range(3):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.1 * (attempt + 1))


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with text content."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        safe_replace(str(temp_path), str(path))
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


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
