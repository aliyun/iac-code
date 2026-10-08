"""Private runtime barriers; ROS owner epoch and writer counter are distinct."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from iac_code.config import get_config_dir
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock


class HandoffFrozenError(RuntimeError):
    """The execution has no current physical writer grant."""


class SessionWriterOperation:
    def __init__(self, fence: SessionWriterFence, key: str, kind: str, actor_epoch: int | None):
        self.fence, self.key, self.kind, self.actor_epoch = fence, key, kind, actor_epoch
        self.token: str | None = None

    def __enter__(self) -> None:
        if not self.fence.enabled:
            return
        path, lock = self.fence._paths(self.key)
        with cross_process_file_lock(lock):
            state = self.fence._read(path)
            phase = state.get("phase")
            actor = self.actor_epoch
            if actor is None:
                from iac_code.a2a.execution_control import current_execution_control

                actor = getattr(current_execution_control(), "handoff_epoch", None)
            allowed = state.get("sourceCounter", 0) if phase == "PREPARING" else state.get("writerCounter", 0)
            if actor is not None and actor != allowed:
                raise HandoffFrozenError("A stale writer cannot write a newer physical owner")
            if phase in {"QUIESCED", "PREPARED", "WAITING_COMMIT"}:
                raise HandoffFrozenError("The session is awaiting handoff authorization")
            if phase == "PREPARING" and self.kind in {"input", "tool"}:
                raise HandoffFrozenError("The session is draining inputs and tools")
            self.token = uuid.uuid4().hex
            state["operations"][self.token] = {"kind": self.kind, "startedAt": time.time()}
            atomic_write_json(path, state)

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self.token is not None:
            path, lock = self.fence._paths(self.key)
            with cross_process_file_lock(lock):
                state = self.fence._read(path)
                state["operations"].pop(self.token, None)
                atomic_write_json(path, state)
            self.token = None
        return False

    async def __aenter__(self) -> None:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return self.__exit__(exc_type, exc_value, traceback)


class SessionWriterFence:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_config_dir() / "handoff-control"
        self.enabled = root is not None or bool(os.environ.get("IAC_CODE_HANDOFF_SHARED_DIR"))

    def _paths(self, key: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / f"{digest}.json", self.root / f"{digest}.lock"

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"phase": "ACTIVE", "epoch": 0, "writerCounter": 0, "operations": {}}
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or not isinstance(value.get("operations"), dict):
            raise HandoffFrozenError("The private writer barrier is unverifiable")
        phase = value.get("phase")
        if not isinstance(phase, str) or phase not in {"ACTIVE", "PREPARING", "QUIESCED", "PREPARED", "WAITING_COMMIT"}:
            raise HandoffFrozenError("The private writer barrier phase is unverifiable")
        counters = [value.get("epoch"), value.get("writerCounter")]
        if "sourceCounter" in value or phase == "PREPARING":
            counters.append(value.get("sourceCounter"))
        if any(not isinstance(counter, int) or isinstance(counter, bool) or counter < 0 for counter in counters):
            raise HandoffFrozenError("The private writer barrier counters are unverifiable")
        if phase == "PREPARING" and value["writerCounter"] != value["sourceCounter"] + 1:
            raise HandoffFrozenError("The private writer revocation identity is unverifiable")
        return value

    def operation(self, key: str, *, kind: str = "write", actor_epoch: int | None = None) -> SessionWriterOperation:
        return SessionWriterOperation(self, key, kind, actor_epoch)

    def async_operation(self, key: str, *, kind: str = "write") -> SessionWriterOperation:
        return self.operation(key, kind=kind)

    def bind_owner(self, key: str, owner: dict[str, Any] | None) -> None:
        if not self.enabled:
            return
        from iac_code.a2a.handoff import PhysicalExecutionIdentity

        if not isinstance(owner, dict):
            raise HandoffFrozenError("An authenticated physical execution owner is required")
        canonical = PhysicalExecutionIdentity.model_validate(owner).model_dump(mode="json", by_alias=True)
        path, lock = self._paths(key)
        with cross_process_file_lock(lock):
            state = self._read(path)
            if state.get("phase") != "ACTIVE":
                raise HandoffFrozenError("An owner grant cannot unfreeze a handoff")
            if state.get("targetOwner") not in (None, canonical):
                raise HandoffFrozenError("A stale physical owner cannot admit input")
            state["targetOwner"] = canonical
            atomic_write_json(path, state)

    def owner(self, key: str) -> dict[str, Any] | None:
        path, lock = self._paths(key)
        with cross_process_file_lock(lock):
            return self._read(path).get("targetOwner")

    def prepare(self, keys: list[str], migration_id: str, epoch: int) -> bool:
        with ExitStack() as locks:
            paths = [self._paths(key) for key in sorted(set(keys))]
            for _, lock in paths:
                locks.enter_context(cross_process_file_lock(lock))
            changes = []
            for path, _ in paths:
                state = self._read(path)
                same = state.get("epoch") == epoch and state.get("migrationId") == migration_id
                if same and state.get("phase") in {"PREPARING", "QUIESCED"}:
                    continue
                if state.get("phase") != "ACTIVE" or state.get("epoch", 0) > epoch:
                    return False
                counter = state.get("writerCounter", 0)
                state.update(
                    phase="PREPARING",
                    migrationId=migration_id,
                    epoch=epoch,
                    sourceCounter=counter,
                    writerCounter=counter + 1,
                )
                changes.append((path, state))
            for path, state in changes:
                atomic_write_json(path, state)
        return True

    def quiesce(self, keys: list[str], migration_id: str, epoch: int) -> bool:
        with ExitStack() as locks:
            paths = [self._paths(key) for key in sorted(set(keys))]
            for _, lock in paths:
                locks.enter_context(cross_process_file_lock(lock))
            states = [(path, self._read(path)) for path, _ in paths]
            if any(
                state.get("migrationId") != migration_id
                or state.get("epoch") != epoch
                or state.get("phase") not in {"PREPARING", "QUIESCED"}
                or state["operations"]
                for _, state in states
            ):
                return False
            for path, state in states:
                state["phase"] = "QUIESCED"
                atomic_write_json(path, state)
        return True

    def wait_for_commit(self, keys: list[str], migration_id: str, epoch: int) -> None:
        with ExitStack() as locks:
            paths = [self._paths(key) for key in sorted(set(keys))]
            for _, lock in paths:
                locks.enter_context(cross_process_file_lock(lock))
            states = [(path, self._read(path)) for path, _ in paths]
            for _, state in states:
                same = state.get("migrationId") == migration_id and state.get("epoch") == epoch
                if (
                    state["operations"]
                    or state.get("epoch", 0) > epoch
                    or state.get("phase") not in {"ACTIVE", "WAITING_COMMIT", "QUIESCED"}
                ):
                    raise HandoffFrozenError("The destination has an existing execution owner")
                if state.get("phase") == "WAITING_COMMIT" and not same:
                    raise HandoffFrozenError("Another handoff owns destination staging")
                if state.get("phase") == "ACTIVE" and state.get("targetOwner") is not None and not same:
                    raise HandoffFrozenError("The destination has an active physical owner")
            for path, state in states:
                state.update(phase="WAITING_COMMIT", migrationId=migration_id, epoch=epoch, commitId=None)
                atomic_write_json(path, state)

    def epoch(self, key: str) -> int:
        if not self.enabled:
            return 0
        path, lock = self._paths(key)
        with cross_process_file_lock(lock):
            state = self._read(path)
            return int(
                state.get("sourceCounter", 0) if state.get("phase") == "PREPARING" else state.get("writerCounter", 0)
            )

    def authorize(
        self, keys: list[str], migration_id: str, epoch: int, commit_id: str, target_owner: dict[str, Any] | None = None
    ) -> None:
        with ExitStack() as locks:
            paths = [self._paths(key) for key in sorted(set(keys))]
            for _, lock in paths:
                locks.enter_context(cross_process_file_lock(lock))
            states = [(path, self._read(path)) for path, _ in paths]
            for _, state in states:
                if (
                    state.get("migrationId") != migration_id
                    or state.get("epoch") != epoch
                    or state.get("phase") not in {"WAITING_COMMIT", "ACTIVE"}
                ):
                    raise HandoffFrozenError("The destination authorization identity changed")
                if state.get("commitId") not in (None, commit_id):
                    raise HandoffFrozenError("The ROS commit identity changed")
                if (
                    state.get("phase") == "ACTIVE"
                    and target_owner is not None
                    and state.get("targetOwner") != target_owner
                ):
                    raise HandoffFrozenError("The physical destination owner changed")
            for path, state in states:
                if state.get("phase") != "ACTIVE":
                    state["writerCounter"] = state.get("writerCounter", 0) + 1
                state.update(phase="ACTIVE", commitId=commit_id)
                if target_owner is not None:
                    state["targetOwner"] = target_owner
                atomic_write_json(path, state)

    @staticmethod
    def session_key(path: str | Path) -> str:
        return "session:" + str(Path(path).resolve())

    @staticmethod
    def workspace_key(cwd: str | Path) -> str:
        return "workspace:" + str(Path(cwd).resolve())
