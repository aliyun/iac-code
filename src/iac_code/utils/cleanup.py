"""Clean up old tool result files from previous sessions."""

from __future__ import annotations

import os
import time

DEFAULT_CLEANUP_PERIOD_DAYS = 30


def cleanup_old_session_files(
    base_dir: str,
    max_age_days: int = DEFAULT_CLEANUP_PERIOD_DAYS,
) -> dict[str, int]:
    """Delete tool result files older than *max_age_days* under *base_dir*.

    Directory layout expected::

        base_dir/
            <session_id>/
                <tool_use_id>.txt
                ...

    Returns a dict with ``deleted`` and ``errors`` counts.
    """
    result: dict[str, int] = {"deleted": 0, "errors": 0}
    cutoff = time.time() - max_age_days * 86400

    try:
        session_names = os.listdir(base_dir)
    except FileNotFoundError:
        return result

    for session_name in session_names:
        session_dir = os.path.join(base_dir, session_name)
        if not os.path.isdir(session_dir):
            continue

        try:
            filenames = os.listdir(session_dir)
        except OSError:
            result["errors"] += 1
            continue

        for filename in filenames:
            file_path = os.path.join(session_dir, filename)
            if not os.path.isfile(file_path):
                continue
            try:
                if os.stat(file_path).st_mtime < cutoff:
                    os.remove(file_path)
                    result["deleted"] += 1
            except OSError:
                result["errors"] += 1

        # Remove empty session directory
        try:
            os.rmdir(session_dir)
        except OSError:
            pass  # not empty or already removed

    # Remove base_dir if empty
    try:
        os.rmdir(base_dir)
    except OSError:
        pass

    return result
