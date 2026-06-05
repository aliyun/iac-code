"""Trusted read roots for current-session runtime artifacts."""

from __future__ import annotations

from iac_code import config


def _validate_session_id(session_id: str) -> None:
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"invalid session_id: {session_id!r}")


def build_session_trusted_read_directories(session_id: str | None) -> list[str]:
    if not session_id:
        return []
    _validate_session_id(session_id)
    config_dir = config.get_config_dir()
    return [
        str(config_dir / "tool-results" / session_id),
        str(config_dir / "image-cache" / session_id),
    ]
