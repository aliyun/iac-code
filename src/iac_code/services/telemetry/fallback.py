"""FallbackStore — on-disk persistence of failed event batches."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

_FAILED_PREFIX = "failed_events."
_FAILED_SUFFIX = ".jsonl"


class FallbackStore:
    """JSONL-backed store for event batches that failed default-backend export."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def _ensure_dir(self) -> Path:
        return ensure_private_dir(self._base_dir)

    def write(self, session_id: str, events: Iterable[dict]) -> Path:
        """Write one JSONL file per batch. One line per event."""
        batch_uuid = uuid.uuid4().hex[:12]
        path = self._ensure_dir() / f"{_FAILED_PREFIX}{session_id}.{batch_uuid}{_FAILED_SUFFIX}"
        with path.open("w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        ensure_private_file(path)
        return path

    def list_pending(self) -> Iterator[Path]:
        """Yield every failed-batch file currently on disk."""
        if not self._base_dir.exists():
            return
        for p in self._base_dir.iterdir():
            if p.is_file() and p.name.startswith(_FAILED_PREFIX) and p.suffix == _FAILED_SUFFIX:
                yield p

    def remove(self, path: Path) -> None:
        """Delete a batch file after successful re-export. Silent on missing."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def read(self, path: Path) -> list[dict]:
        """Parse a JSONL batch file. Unparseable lines skipped."""
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
