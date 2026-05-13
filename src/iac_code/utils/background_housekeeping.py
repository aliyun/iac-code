"""Background housekeeping — delayed cleanup of old tool result files."""

from __future__ import annotations

import threading
import time

from loguru import logger

from iac_code.utils.cleanup import cleanup_old_session_files

# Delay before running cleanup after session starts (seconds).
DELAY_SECONDS = 10 * 60  # 10 minutes

_BASE_DIR = None


def _get_default_base_dir() -> str:
    from iac_code.config import get_config_dir

    return _BASE_DIR or str(get_config_dir() / "tool-results")


def _run_cleanup(base_dir: str, delay_seconds: float) -> None:
    time.sleep(delay_seconds)
    try:
        result = cleanup_old_session_files(base_dir)
        if result["deleted"] > 0:
            logger.debug(
                "Background cleanup: deleted {} expired tool result file(s)",
                result["deleted"],
            )
    except Exception:
        logger.opt(exception=True).debug("Background cleanup failed")


def start_background_housekeeping(
    base_dir: str | None = None,
    delay_seconds: float = DELAY_SECONDS,
) -> threading.Thread:
    """Start a daemon thread that cleans up old tool result files after a delay.

    Returns the thread so callers can join() in tests.
    """
    target_dir = base_dir or _get_default_base_dir()
    thread = threading.Thread(
        target=_run_cleanup,
        args=(target_dir, delay_seconds),
        daemon=True,
        name="iac-code-housekeeping",
    )
    thread.start()
    return thread
