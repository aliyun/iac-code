from __future__ import annotations

import json
import time
import uuid
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

from scripts.observability.local_observe.records import Record


class ObserveStore:
    def __init__(self, *, data_dir: Path, memory_limit: int = 5000) -> None:
        self.data_dir = Path(data_dir)
        self.memory_limit = memory_limit
        self._records: deque[Record] = deque(maxlen=memory_limit)
        self._lock = Lock()
        self._persistence_error: str | None = None
        self.current_jsonl_path = self._new_jsonl_path()

    def _new_jsonl_path(self) -> Path:
        run_dir = self.data_dir / "runs" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "records.jsonl"
        path.touch()
        return path

    def append_many(self, records: list[Record]) -> None:
        with self._lock:
            for record in records:
                self._records.append(record)
            try:
                with self.current_jsonl_path.open("a", encoding="utf-8") as fh:
                    for record in records:
                        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                self._persistence_error = None
            except OSError as exc:
                self._persistence_error = str(exc)

    def records(self) -> list[Record]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self.current_jsonl_path = self._new_jsonl_path()
            self._persistence_error = None

    def export_text(self) -> str:
        try:
            return self.current_jsonl_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "record_count": len(self._records),
                "memory_limit": self.memory_limit,
                "jsonl_path": str(self.current_jsonl_path),
                "persistence_ok": self._persistence_error is None,
                "persistence_error": self._persistence_error,
            }
