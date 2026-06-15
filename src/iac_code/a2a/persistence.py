from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from iac_code.a2a.types import validate_protocol_id
from iac_code.utils.file_security import atomic_write_text

_INTERRUPTED_RESTORE_STATES = {"submitted", "working", "auth-required"}
_FILESYSTEM_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WINDOWS_RESERVED_DIGIT_TRANSLATION = str.maketrans(
    {
        "\N{SUPERSCRIPT ONE}": "1",
        "\N{SUPERSCRIPT TWO}": "2",
        "\N{SUPERSCRIPT THREE}": "3",
    }
)
_ENCODED_ID_PREFIX = "a2aid~"


@dataclass(frozen=True)
class A2ATaskSnapshot:
    task_id: str
    context_id: str
    state: str
    output_text: list[str] = field(default_factory=list)
    status_message: str = ""
    updated_at: float = field(default_factory=time.time)
    owner: str = ""


@dataclass(frozen=True)
class A2AContextSnapshot:
    context_id: str
    session_id: str
    cwd: str
    active_task_id: str | None = None
    updated_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class A2ARouteSnapshot:
    name: str
    url: str
    skills: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


class A2APersistenceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.contexts_dir = self.root / "contexts"
        self.routes_path = self.root / "routes.json"

    def save_task(self, snapshot: A2ATaskSnapshot) -> None:
        task_id = validate_protocol_id(snapshot.task_id)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.tasks_dir / f"{_protocol_id_file_stem(task_id)}.json", asdict(snapshot))

    def load_task(self, task_id: str) -> A2ATaskSnapshot | None:
        task_id = validate_protocol_id(task_id)
        data = self._read_json(self.tasks_dir / f"{_protocol_id_file_stem(task_id)}.json")
        if data is None and _protocol_id_file_stem(task_id) != task_id:
            data = self._read_json(self.tasks_dir / f"{task_id}.json")
        if data is None:
            return None
        return self._task_from_dict(data)

    def restore_task(self, task_id: str) -> A2ATaskSnapshot | None:
        snapshot = self.load_task(task_id)
        if snapshot is None:
            return None
        if snapshot.state in _INTERRUPTED_RESTORE_STATES:
            interrupted = A2ATaskSnapshot(
                task_id=snapshot.task_id,
                context_id=snapshot.context_id,
                state="interrupted",
                owner=snapshot.owner,
                output_text=snapshot.output_text,
                status_message="Task was interrupted by process exit and cannot be revived automatically.",
                updated_at=snapshot.updated_at,
            )
            self.save_task(interrupted)
            return interrupted
        return snapshot

    def list_tasks(self) -> list[A2ATaskSnapshot]:
        if not self.tasks_dir.exists():
            return []
        snapshots: list[A2ATaskSnapshot] = []
        for path in sorted(self.tasks_dir.glob("*.json")):
            data = self._read_json(path)
            if data is None:
                continue
            snapshot = self._task_from_dict(data)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def save_context(self, snapshot: A2AContextSnapshot) -> None:
        context_id = validate_protocol_id(snapshot.context_id)
        self.contexts_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.contexts_dir / f"{_protocol_id_file_stem(context_id)}.json", asdict(snapshot))

    def load_context(self, context_id: str) -> A2AContextSnapshot | None:
        context_id = validate_protocol_id(context_id)
        data = self._read_json(self.contexts_dir / f"{_protocol_id_file_stem(context_id)}.json")
        if data is None and _protocol_id_file_stem(context_id) != context_id:
            data = self._read_json(self.contexts_dir / f"{context_id}.json")
        if data is None:
            return None
        return self._context_from_dict(data)

    def save_routes(self, routes: list[A2ARouteSnapshot]) -> None:
        """Persist a cold-start cache of route metadata.

        Explicit CLI route options and future settings-file configuration are
        the source of truth; this cache is only used when a caller asks to save
        or reload recently discovered/configured routes.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_json(self.routes_path, {"routes": [asdict(route) for route in routes]})

    def load_routes(self) -> list[A2ARouteSnapshot]:
        data = self._read_json(self.routes_path)
        raw_routes = data.get("routes") if isinstance(data, dict) else None
        if not isinstance(raw_routes, list):
            return []
        routes: list[A2ARouteSnapshot] = []
        for raw in raw_routes:
            route = self._route_from_dict(raw)
            if route is not None:
                routes.append(route)
        return routes

    @staticmethod
    def _task_from_dict(data: dict[str, object]) -> A2ATaskSnapshot | None:
        task_id = data.get("task_id")
        context_id = data.get("context_id")
        state = data.get("state")
        if not isinstance(task_id, str) or not isinstance(context_id, str) or not isinstance(state, str):
            return None
        raw_output_text = data.get("output_text")
        output_text = (
            [item for item in raw_output_text if isinstance(item, str)] if isinstance(raw_output_text, list) else []
        )
        owner = data.get("owner")
        status_message = data.get("status_message")
        updated_at = data.get("updated_at")
        return A2ATaskSnapshot(
            task_id=task_id,
            context_id=context_id,
            state=state,
            owner=owner if isinstance(owner, str) else "",
            output_text=output_text,
            status_message=status_message if isinstance(status_message, str) else "",
            updated_at=float(updated_at) if isinstance(updated_at, (int, float)) else time.time(),
        )

    @staticmethod
    def _context_from_dict(data: dict[str, object]) -> A2AContextSnapshot | None:
        context_id = data.get("context_id")
        session_id = data.get("session_id")
        cwd = data.get("cwd")
        if not isinstance(context_id, str) or not isinstance(session_id, str) or not isinstance(cwd, str):
            return None
        active_task_id = data.get("active_task_id")
        updated_at = data.get("updated_at")
        return A2AContextSnapshot(
            context_id=context_id,
            session_id=session_id,
            cwd=cwd,
            active_task_id=active_task_id if isinstance(active_task_id, str) else None,
            updated_at=float(updated_at) if isinstance(updated_at, (int, float)) else time.time(),
        )

    @staticmethod
    def _route_from_dict(data: object) -> A2ARouteSnapshot | None:
        if not isinstance(data, Mapping):
            return None
        data = cast("Mapping[str, object]", data)
        name = data.get("name")
        url = data.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            return None
        raw_skills = data.get("skills")
        raw_tags = data.get("tags")
        skills = [item for item in raw_skills if isinstance(item, str)] if isinstance(raw_skills, list) else []
        tags = [item for item in raw_tags if isinstance(item, str)] if isinstance(raw_tags, list) else []
        return A2ARouteSnapshot(name=name, url=url, skills=skills, tags=tags)

    @staticmethod
    def _write_json(path: Path, data: dict[str, object]) -> None:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None


def _protocol_id_file_stem(value: str) -> str:
    protocol_id = validate_protocol_id(value)
    basename = protocol_id.rstrip(".").split(".", 1)[0].upper().translate(_WINDOWS_RESERVED_DIGIT_TRANSLATION)
    if (
        _FILESYSTEM_SAFE_ID_PATTERN.fullmatch(protocol_id) is not None
        and protocol_id == protocol_id.rstrip(".")
        and basename not in _WINDOWS_RESERVED_BASENAMES
    ):
        return protocol_id
    encoded = base64.urlsafe_b64encode(protocol_id.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_ENCODED_ID_PREFIX}{encoded}"
