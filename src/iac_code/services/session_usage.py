"""Session-level provider API usage persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from iac_code.types.stream_events import Usage
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.project_paths import get_project_dir, get_projects_dir, sanitize_path


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
        return self.input_tokens + self.output_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

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

    def __init__(self, projects_dir: Path | str | None = None) -> None:
        self._projects_dir = Path(projects_dir) if projects_dir is not None else get_projects_dir()

    def path_for(self, cwd: str, session_id: str) -> Path:
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
        ensure_private_dir(path.parent)
        row = _usage_to_row(usage, provider=provider, model=model, created_at=created_at)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        ensure_private_file(path)
        return True

    def load(self, cwd: str, session_id: str) -> SessionUsageTotals:
        """Load cumulative usage totals, skipping corrupt or unrelated rows."""
        path = self.path_for(cwd, session_id)
        totals = SessionUsageTotals()
        if not path.exists():
            return totals

        try:
            with open(path, encoding="utf-8") as f:
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
        return totals

    def _project_dir_for(self, cwd: str) -> Path:
        if self._projects_dir == get_projects_dir():
            return get_project_dir(cwd)
        return self._projects_dir / sanitize_path(cwd)


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
