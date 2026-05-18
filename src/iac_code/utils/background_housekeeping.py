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


def _run_image_cleanup(current_session_id: str, delay_seconds: float) -> None:
    time.sleep(delay_seconds)
    try:
        from iac_code.utils.image.store import cleanup_old_image_caches

        cleanup_old_image_caches(current_session_id=current_session_id)
        logger.debug("Background cleanup: purged old session image caches")
    except Exception:
        logger.opt(exception=True).debug("Background image cache cleanup failed")


def start_background_housekeeping(
    base_dir: str | None = None,
    delay_seconds: float = DELAY_SECONDS,
    session_id: str | None = None,
) -> tuple[threading.Thread, ...]:
    """Start daemon thread(s) for delayed cleanup.

    Always returns a tuple of started threads so callers (and tests) can
    iterate uniformly. Currently:
        - tool-result cleanup thread (always)
        - image-cache cleanup thread (only when ``session_id`` is provided)
    """
    target_dir = base_dir or _get_default_base_dir()
    threads: list[threading.Thread] = []
    tool_thread = threading.Thread(
        target=_run_cleanup,
        args=(target_dir, delay_seconds),
        daemon=True,
        name="iac-code-housekeeping",
    )
    tool_thread.start()
    threads.append(tool_thread)
    if session_id is not None:
        image_thread = threading.Thread(
            target=_run_image_cleanup,
            args=(session_id, delay_seconds),
            daemon=True,
            name="iac-code-image-housekeeping",
        )
        image_thread.start()
        threads.append(image_thread)
    return tuple(threads)
