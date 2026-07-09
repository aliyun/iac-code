"""Trusted read roots for current-session runtime artifacts."""

from __future__ import annotations

import logging
from pathlib import Path

from iac_code import config
from iac_code.services.session_layout import SessionPaths, UnsupportedSessionLayoutError, ensure_session_owned_dir

logger = logging.getLogger(__name__)


def _validate_session_id(session_id: str) -> None:
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"invalid session_id: {session_id!r}")


def build_session_trusted_read_directories(
    session_id: str | None,
    *,
    session_dir: Path | str | None = None,
) -> list[str]:
    if not session_id:
        return []
    _validate_session_id(session_id)
    config_dir = config.get_config_dir()
    roots = [
        str(config_dir / "tool-results" / session_id),
        str(config_dir / "image-cache" / session_id),
    ]
    if isinstance(session_dir, Path):
        session_path = session_dir
    elif isinstance(session_dir, str):
        session_path = Path(session_dir)
    else:
        session_path = None
    if session_path is not None:
        session_paths = SessionPaths.from_session_dir(session_path)
        for path in (session_paths.tool_results_dir, session_paths.image_cache_dir):
            try:
                roots.append(str(ensure_session_owned_dir(session_paths.session_dir, path)))
            except UnsupportedSessionLayoutError as exc:
                logger.debug("Skipping unsafe session trusted read root error_type=%s", type(exc).__name__)
    return roots
