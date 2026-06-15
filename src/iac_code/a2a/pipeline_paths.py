from __future__ import annotations

from pathlib import Path

from iac_code.services.session_storage import SessionStorage


def a2a_pipeline_dir_for_sidecar_dir(sidecar_dir: str | Path) -> Path:
    path = Path(sidecar_dir)
    if path.name == "pipeline":
        return path.parent / "a2a" / "pipeline"
    return path


def a2a_pipeline_dir_for_session(*, cwd: str, session_id: str) -> Path:
    return SessionStorage().session_dir(cwd, session_id) / "a2a" / "pipeline"


def existing_a2a_pipeline_dir_for_session(*, cwd: str, session_id: str) -> Path:
    session_dir = SessionStorage().session_dir(cwd, session_id)
    preferred = session_dir / "a2a" / "pipeline"
    legacy = session_dir / "pipeline"
    if _has_a2a_metadata(preferred) or not _has_a2a_metadata(legacy):
        return preferred
    return legacy


def _has_a2a_metadata(path: Path) -> bool:
    return (path / "a2a-events.jsonl").exists() or (path / "a2a-snapshot.json").exists()
