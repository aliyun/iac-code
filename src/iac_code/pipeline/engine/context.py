"""PipelineContext — versioned cross-step context with DAG-based stale propagation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VersionedField:
    value: Any | None = None
    version: int = 0
    stale: bool = False
    updated_at: float | None = None
    history: list[dict] = field(default_factory=list)


class PipelineContext:
    """Cross-step context manager with versioned fields and stale propagation.

    Fields are identified by name and linked via a dependency DAG.
    When a field is updated, all downstream dependents are marked stale.
    """

    def __init__(self, field_dependencies: dict[str, list[str]]) -> None:
        self._deps = field_dependencies
        self._fields: dict[str, VersionedField] = {name: VersionedField() for name in field_dependencies}

    def set_conclusion(self, field_name: str, value: Any) -> list[str]:
        """Set field value. Returns list of downstream fields marked stale."""
        f = self._fields[field_name]
        if f.value is not None:
            f.history.append({"value": f.value, "version": f.version})
        f.value = value
        f.version += 1
        f.stale = False
        f.updated_at = time.time()
        return self._propagate_stale(field_name)

    def get_conclusion(self, field_name: str) -> Any | None:
        return self._fields[field_name].value

    def get_field(self, field_name: str) -> VersionedField:
        return self._fields[field_name]

    def get_conclusions_summary(self) -> dict[str, dict[str, Any]]:
        """All non-null conclusions with version and stale flag."""
        return {
            name: {"value": f.value, "version": f.version, "stale": f.stale}
            for name, f in self._fields.items()
            if f.value is not None
        }

    def get_stale_fields(self) -> list[str]:
        return [name for name, f in self._fields.items() if f.stale]

    def mark_stale(self, field_name: str) -> list[str]:
        self._fields[field_name].stale = True
        return self._propagate_stale(field_name)

    def clear_stale(self, field_name: str) -> None:
        self._fields[field_name].stale = False

    def _propagate_stale(self, changed_field: str) -> list[str]:
        """BFS stale propagation to downstream dependents."""
        stale_list: list[str] = []
        queue = [changed_field]
        visited = {changed_field}
        while queue:
            current = queue.pop(0)
            for name, deps in self._deps.items():
                if current in deps and name not in visited:
                    visited.add(name)
                    if self._fields[name].value is not None:
                        self._fields[name].stale = True
                        stale_list.append(name)
                    queue.append(name)
        return stale_list

    def snapshot(self) -> dict:
        """Return a flat {field_name: value} dict for all set fields.

        Sibling of to_snapshot() that omits version/stale/history metadata.
        Consumed by callers that need to navigate field values directly
        (e.g., PipelineRunner._resolve_iterate_field for dotted path lookup).
        """
        return {name: f.value for name, f in self._fields.items() if f.value is not None}

    def to_snapshot(self) -> dict:
        return {
            name: {
                "value": f.value,
                "version": f.version,
                "stale": f.stale,
                "updated_at": f.updated_at,
                "history": f.history,
            }
            for name, f in self._fields.items()
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict, field_dependencies: dict[str, list[str]]) -> PipelineContext:
        ctx = cls(field_dependencies)
        for name, data in snapshot.items():
            if name in ctx._fields:
                f = ctx._fields[name]
                f.value = data["value"]
                f.version = data["version"]
                f.stale = data["stale"]
                f.updated_at = data.get("updated_at")
                f.history = data.get("history", [])
        return ctx
