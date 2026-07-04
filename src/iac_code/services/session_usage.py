"""Session-level provider API usage persistence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from iac_code.i18n import _
from iac_code.services.session_layout import (
    SESSION_LAYOUT_VERSION_V2,
    UnsupportedSessionLayoutError,
    ensure_session_owned_parent,
    is_supported_session_dir_for_id,
    require_supported_session_layout,
)
from iac_code.services.session_metadata import (
    SESSION_JSONL_FILENAME,
    session_metadata_entry_exists,
)
from iac_code.types.stream_events import Usage
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.project_paths import get_projects_dir, project_dir_candidates
from iac_code.utils.state_io import open_text_no_follow

USAGE_JSONL_FILENAME = "usage.jsonl"
UsagePathProvider = Callable[[str, str], Path]


@dataclass
class SessionUsageTotals:
    """Cumulative provider-reported token usage for one session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    recorded_events: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def has_recorded_usage(self) -> bool:
        return self.recorded_events > 0

    def add(self, usage: Usage) -> bool:
        """Add a non-zero usage event and return whether it was recorded."""
        if _usage_is_zero(usage):
            return False
        self.input_tokens += int(usage.input_tokens or 0)
        self.output_tokens += int(usage.output_tokens or 0)
        self.cache_read_input_tokens += int(usage.cache_read_input_tokens or 0)
        self.cache_creation_input_tokens += int(usage.cache_creation_input_tokens or 0)
        self.recorded_events += 1
        return True

    def copy(self) -> SessionUsageTotals:
        return SessionUsageTotals(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            recorded_events=self.recorded_events,
        )


class SessionUsageStore:
    """Persist cumulative API usage as a sidecar JSONL file."""

    def __init__(
        self,
        projects_dir: Path | str | None = None,
        *,
        path_provider: UsagePathProvider | None = None,
    ) -> None:
        self._projects_dir = Path(projects_dir) if projects_dir is not None else get_projects_dir()
        self._path_provider = path_provider

    def path_for(self, cwd: str, session_id: str) -> Path:
        if self._path_provider is not None:
            return Path(self._path_provider(cwd, session_id))
        return self._usage_path_for_write(cwd, session_id)

    @property
    def uses_direct_path_provider(self) -> bool:
        return self._path_provider is not None

    def legacy_path_for(self, cwd: str, session_id: str) -> Path:
        return self._project_dir_for(cwd) / f"{session_id}.usage.jsonl"

    def append(
        self,
        cwd: str,
        session_id: str,
        usage: Usage,
        *,
        provider: str | None = None,
        model: str | None = None,
        created_at: datetime | None = None,
    ) -> bool:
        """Append a non-zero provider usage event."""
        if _usage_is_zero(usage):
            return False

        path = self.path_for(cwd, session_id)
        _ensure_usage_parent(path)
        row = _usage_to_row(usage, provider=provider, model=model, created_at=created_at)
        with open_text_no_follow(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        ensure_private_file(path)
        return True

    def load(self, cwd: str, session_id: str) -> SessionUsageTotals:
        """Load cumulative usage totals, skipping corrupt or unrelated rows."""
        totals = SessionUsageTotals()
        if self._path_provider is not None:
            path = self.path_for(cwd, session_id)
            if _can_read_direct_usage_path(path):
                self._load_path(path, totals)
            return totals
        seen: set[Path] = set()
        paths = []
        legacy_session_path = self._existing_legacy_session_path(cwd, session_id)
        for project_dir in self._project_read_dirs_for(cwd):
            session_dir = project_dir / session_id
            if self._can_read_directory_usage(
                session_dir,
                session_id,
                legacy_session_exists=legacy_session_path is not None,
            ):
                paths.append(session_dir / USAGE_JSONL_FILENAME)
            paths.append(project_dir / f"{session_id}.usage.jsonl")
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            self._load_path(path, totals)
        return totals

    def _load_path(self, path: Path, totals: SessionUsageTotals) -> None:
        if not _entry_exists_no_follow(path):
            return

        try:
            with open_text_no_follow(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("Skipping corrupt usage row in {}", path)
                        continue
                    if not isinstance(row, dict) or row.get("type") != "usage":
                        continue
                    totals.add(_row_to_usage(row))
        except OSError as exc:
            logger.debug("Failed to load usage sidecar {}: {}", path, exc)

    def _project_dir_for(self, cwd: str) -> Path:
        return project_dir_candidates(cwd, self._projects_dir)[0]

    def _project_read_dirs_for(self, cwd: str) -> tuple[Path, ...]:
        candidates = project_dir_candidates(cwd, self._projects_dir)
        existing = tuple(candidate for candidate in candidates if candidate.exists())
        return existing or (candidates[0],)

    def _usage_path_for_write(self, cwd: str, session_id: str) -> Path:
        existing_legacy_session_path = self._existing_legacy_session_path(cwd, session_id)
        for project_dir in self._project_read_dirs_for(cwd):
            session_dir = project_dir / session_id
            if _entry_exists_no_follow(session_dir / SESSION_JSONL_FILENAME):
                if is_supported_session_dir_for_id(session_dir, session_id):
                    return session_dir / USAGE_JSONL_FILENAME
                if existing_legacy_session_path is not None:
                    return _legacy_usage_path(existing_legacy_session_path)
                _raise_unsupported_session_metadata(session_dir)
            project_legacy_session_path = project_dir / f"{session_id}.jsonl"
            if project_legacy_session_path.exists():
                return _legacy_usage_path(project_legacy_session_path)
            if session_metadata_entry_exists(session_dir):
                if existing_legacy_session_path is not None:
                    continue
                if is_supported_session_dir_for_id(session_dir, session_id):
                    return session_dir / USAGE_JSONL_FILENAME
                _raise_unsupported_session_metadata(session_dir)
        if existing_legacy_session_path is not None:
            return _legacy_usage_path(existing_legacy_session_path)
        return self._project_dir_for(cwd) / session_id / USAGE_JSONL_FILENAME

    @staticmethod
    def _can_read_directory_usage(session_dir: Path, session_id: str, *, legacy_session_exists: bool) -> bool:
        if not _entry_exists_no_follow(session_dir):
            return True
        has_session_jsonl = _entry_exists_no_follow(session_dir / SESSION_JSONL_FILENAME)
        has_metadata = session_metadata_entry_exists(session_dir)
        if legacy_session_exists and has_metadata and not has_session_jsonl:
            return False
        if not has_session_jsonl and not has_metadata:
            return True
        try:
            return is_supported_session_dir_for_id(session_dir, session_id)
        except UnsupportedSessionLayoutError:
            return False

    def _existing_legacy_session_path(self, cwd: str, session_id: str) -> Path | None:
        for project_dir in self._project_read_dirs_for(cwd):
            legacy_path = project_dir / f"{session_id}.jsonl"
            if legacy_path.exists():
                return legacy_path
        return None


def _ensure_usage_parent(path: Path) -> None:
    session_dir = _session_dir_from_usage_path(path)
    if session_dir is not None and session_metadata_entry_exists(session_dir):
        if require_supported_session_layout(session_dir) == SESSION_LAYOUT_VERSION_V2:
            ensure_session_owned_parent(session_dir, path)
            return
    ensure_private_dir(path.parent)


def _session_dir_from_usage_path(path: Path) -> Path | None:
    if path.name != USAGE_JSONL_FILENAME:
        return None
    parent = path.parent
    if (
        parent.name.startswith("transcript_")
        and parent.parent.name == "transcripts"
        and parent.parent.parent.name == "pipeline"
    ):
        return parent.parent.parent.parent
    return parent


def _legacy_usage_path(legacy_session_path: Path) -> Path:
    return legacy_session_path.with_name(f"{legacy_session_path.stem}.usage.jsonl")


def _can_read_direct_usage_path(path: Path) -> bool:
    session_dir = _session_dir_from_usage_path(path)
    if session_dir is None or not session_metadata_entry_exists(session_dir):
        return True
    try:
        require_supported_session_layout(session_dir)
    except UnsupportedSessionLayoutError:
        return False
    return True


def _entry_exists_no_follow(path: Path) -> bool:
    try:
        path.stat(follow_symlinks=False)
    except OSError:
        return False
    return True


def _raise_unsupported_session_metadata(session_dir: Path) -> None:
    raise UnsupportedSessionLayoutError(_("Unsupported session metadata: {path}").format(path=session_dir))


def _usage_is_zero(usage: Usage) -> bool:
    return (
        int(usage.input_tokens or 0) == 0
        and int(usage.output_tokens or 0) == 0
        and int(usage.cache_read_input_tokens or 0) == 0
        and int(usage.cache_creation_input_tokens or 0) == 0
    )


def _usage_to_row(
    usage: Usage,
    *,
    provider: str | None,
    model: str | None,
    created_at: datetime | None,
) -> dict[str, Any]:
    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    return {
        "type": "usage",
        "version": 1,
        "created_at": timestamp.isoformat().replace("+00:00", "Z"),
        "provider": provider,
        "model": model,
        "input_tokens": int(usage.input_tokens or 0),
        "output_tokens": int(usage.output_tokens or 0),
        "cache_read_input_tokens": int(usage.cache_read_input_tokens or 0),
        "cache_creation_input_tokens": int(usage.cache_creation_input_tokens or 0),
    }


def _row_to_usage(row: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=_int(row.get("input_tokens")),
        output_tokens=_int(row.get("output_tokens")),
        cache_read_input_tokens=_int(row.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_int(row.get("cache_creation_input_tokens")),
    )


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)
