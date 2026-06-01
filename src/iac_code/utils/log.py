"""Logging setup for iac-code using loguru."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from loguru import logger

from iac_code.config import get_config_dir
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

_LOG_FORMAT = "{time:YYYY-MM-DDTHH:mm:ss.SSS} [{level:<5}] {name}:{function}:{line} - {message}"

_startup_handler_id: int | None = None
_runtime_debug_handler_ids: list[int] = []
_debug_enabled: bool = False
_current_log_file: Path | None = None


def _link_latest(log_dir: Path, log_file: Path) -> None:
    """Create a latest.log symlink, falling back to copy on Windows without privileges.

    Note: the copy fallback produces a point-in-time snapshot, not a live link —
    subsequent writes go only to the session log file.
    """
    latest = log_dir / "latest.log"
    latest.unlink(missing_ok=True)
    try:
        latest.symlink_to(log_file.name)
    except OSError:
        try:
            shutil.copy2(log_file, latest)
            ensure_private_file(latest)
        except OSError:
            pass


class _StdlibToLoguruHandler(logging.Handler):
    """Route stdlib logging records to loguru so OTel SDK logs are visible."""

    _LEVEL_MAP = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def emit(self, record: logging.LogRecord) -> None:
        level = self._LEVEL_MAP.get(record.levelno, "INFO")
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(
    session_id: str,
    debug: bool = False,
) -> None:
    """Configure loguru for the application.

    Args:
        session_id: Current session ID, used in log filenames.
        debug: Enable debug file logging.
    """
    global _startup_handler_id, _runtime_debug_handler_ids, _debug_enabled, _current_log_file

    logger.remove()
    _runtime_debug_handler_ids = []

    log_dir = ensure_private_dir(get_config_dir() / "logs")
    log_file = log_dir / f"{session_id}.log"
    level = "DEBUG" if debug else "INFO"

    _startup_handler_id = logger.add(
        str(log_file),
        level=level,
        format=_LOG_FORMAT,
        encoding="utf-8",
    )
    _debug_enabled = debug
    _current_log_file = log_file
    ensure_private_file(log_file)

    _link_latest(log_dir, log_file)

    _install_stdlib_bridge()


def _install_stdlib_bridge() -> None:
    """Install the stdlib → loguru bridge on key namespaces."""
    handler = _StdlibToLoguruHandler()
    for name in ("opentelemetry", "iac_code"):
        stdlib_logger = logging.getLogger(name)
        if not any(isinstance(h, _StdlibToLoguruHandler) for h in stdlib_logger.handlers):
            stdlib_logger.addHandler(handler)
            stdlib_logger.setLevel(logging.DEBUG)


def enable_debug_at_runtime(session_id: str) -> Path:
    """Enable debug logging mid-session (for /debug command).

    Returns:
        Path to the log file.
    """
    global _debug_enabled, _current_log_file

    log_dir = ensure_private_dir(get_config_dir() / "logs")
    log_file = log_dir / f"{session_id}.log"
    _current_log_file = log_file

    if _debug_enabled:
        return log_file

    handler_id = logger.add(
        str(log_file),
        level="DEBUG",
        format=_LOG_FORMAT,
        encoding="utf-8",
    )
    _runtime_debug_handler_ids.append(handler_id)
    _debug_enabled = True
    ensure_private_file(log_file)

    _link_latest(log_dir, log_file)

    return log_file


def disable_debug_at_runtime() -> None:
    """Disable debug logging mid-session (for /debug off)."""
    global _debug_enabled, _startup_handler_id, _runtime_debug_handler_ids

    if not _debug_enabled:
        return

    for hid in _runtime_debug_handler_ids:
        try:
            logger.remove(hid)
        except ValueError:
            pass
    _runtime_debug_handler_ids = []

    if _startup_handler_id is not None and _current_log_file is not None:
        try:
            logger.remove(_startup_handler_id)
        except ValueError:
            pass
        _startup_handler_id = logger.add(
            str(_current_log_file),
            level="INFO",
            format=_LOG_FORMAT,
            encoding="utf-8",
        )

    _debug_enabled = False


def is_debug_enabled() -> bool:
    """Return whether debug-level logging is currently active."""
    return _debug_enabled


def current_log_file() -> Path | None:
    """Return the current session log file path, if setup_logging has been called."""
    return _current_log_file
