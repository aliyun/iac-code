from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from iac_code.a2a.execution_control import ExecutionControlConflictError, ExecutionController, ExecutionControlService
from iac_code.utils.state_io import atomic_write_json


class _NaturalHandoffFixture:
    def __init__(self, persistence_root: Path | None = None) -> None:
        self.service = ExecutionControlService(persistence_root=persistence_root, backup_service=None)

    async def prepare(self) -> ExecutionController:
        control = await self.service.begin_execution(
            context_id="ctx-1", task_id="task-1", owner="owner-1", cwd="/repo", execution_mode="pipeline"
        )
        current = asyncio.current_task()
        assert current is not None
        generation = await control.detach_task(current, execution_status="input-required", natural_completion=True)
        assert generation is not None
        await control.finalize_natural_completion(task_id="task-1", completion_generation=generation)
        await self.drain(control)
        assert control.natural_handoff_admits_replacement()
        self.control = control
        return control

    async def replace(self, **overrides) -> ExecutionController:
        arguments = {
            "context_id": "ctx-1",
            "task_id": "task-1",
            "owner": "owner-1",
            "cwd": "/repo",
            "execution_mode": "normal",
            "expected_natural_handoff_control": self.control,
        }
        arguments.update(overrides)
        return await self.service.begin_execution(**arguments)

    @staticmethod
    async def drain(control: ExecutionController) -> None:
        while control._background_tasks:
            await asyncio.gather(*tuple(control._background_tasks))
            await asyncio.sleep(0)  # Let completed-task callbacks remove already finished workers.

    async def close(self) -> None:
        current = asyncio.current_task()
        assert current is not None
        for control in self.service._controls.values():
            await control.detach_task(current, execution_status="input-required")
        await self.service.close()


class _BeginBarrier:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self, _context_id: str) -> None:
        self.entered.set()
        await self.release.wait()


class _ClaimBarrier:
    def __init__(self, claim: Callable[..., Any]) -> None:
        self._claim = claim
        self.entered = threading.Event()
        self.release = threading.Event()

    def claim(self, *args: Any, **kwargs: Any) -> Any:
        self.entered.set()
        assert self.release.wait(timeout=5)
        return self._claim(*args, **kwargs)


@pytest.mark.asyncio
async def test_memory_natural_handoff_starts_same_task_normal_followup() -> None:
    fixture = _NaturalHandoffFixture()
    try:
        original = await fixture.prepare()
        replacement = await fixture.replace()
        assert fixture.service.get_for_context("ctx-1") is replacement
        assert replacement is not original
        assert replacement.execution_id != original.execution_id
        assert replacement.execution_mode == "normal"
        assert replacement.phase == "running"
        assert replacement.task_id == "task-1"
        assert not replacement.release_ready
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [{"context_id": "other-context"}, {"task_id": "other-task"}, {"owner": "other-owner"}, {"cwd": "/other-repo"}],
)
async def test_memory_handoff_rejects_different_execution_identity(overrides: dict[str, str]) -> None:
    fixture = _NaturalHandoffFixture()
    try:
        original = await fixture.prepare()
        with pytest.raises(ExecutionControlConflictError):
            await fixture.replace(**overrides)
        assert fixture.service.get_for_context("ctx-1") is original
        assert fixture.service.get_for_context("other-context") is None
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_memory_handoff_rejects_retired_control() -> None:
    fixture = _NaturalHandoffFixture()
    try:
        await fixture.prepare()
        replacement = await fixture.replace()
        with pytest.raises(ExecutionControlConflictError):
            await fixture.replace()
        assert fixture.service.get_for_context("ctx-1") is replacement
        assert replacement.phase == "running"
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_memory_handoff_begin_revalidates_after_explicit_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _NaturalHandoffFixture()
    barrier = _BeginBarrier()
    beginning: asyncio.Task[ExecutionController] | None = None
    try:
        original = await fixture.prepare()
        monkeypatch.setattr(fixture.service, "_await_local_snapshot_quiescence", barrier.wait)
        beginning = asyncio.create_task(fixture.replace())
        await asyncio.wait_for(barrier.entered.wait(), timeout=5)
        await original.terminate(
            execution_id=original.execution_id,
            request_id="explicit-stop",
            connection_epoch=1,
            reason="user_cancel",
        )
        barrier.release.set()
        with pytest.raises(ExecutionControlConflictError):
            await asyncio.wait_for(beginning, timeout=5)
        await fixture.drain(original)
        assert fixture.service.get_for_context("ctx-1") is original
        assert original.termination_reason == "user_cancel"
        assert original.natural_handoff_receipt() is None
        assert not original.natural_handoff_admits_replacement()
    finally:
        barrier.release.set()
        if beginning is not None:
            beginning.cancel()
            await asyncio.gather(beginning, return_exceptions=True)
        await fixture.close()


@pytest.mark.asyncio
async def test_memory_handoff_claim_revalidates_after_explicit_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _NaturalHandoffFixture()
    beginning: asyncio.Task[ExecutionController] | None = None
    barrier: _ClaimBarrier | None = None
    try:
        original = await fixture.prepare()
        store = fixture.service._recoverable_input_admissions
        barrier = _ClaimBarrier(store.claim_begin_without_admission)
        monkeypatch.setattr(store, "claim_begin_without_admission", barrier.claim)
        beginning = asyncio.create_task(fixture.replace())
        assert await asyncio.to_thread(barrier.entered.wait, 5)
        await original.terminate(
            execution_id=original.execution_id,
            request_id="explicit-stop-during-claim",
            connection_epoch=1,
            reason="user_cancel",
        )
        await fixture.drain(original)
        assert original.phase == "terminated"
        assert original.termination_reason == "user_cancel"
        assert original.natural_handoff_receipt() is None
        barrier.release.set()
        with pytest.raises(ExecutionControlConflictError):
            await asyncio.wait_for(beginning, timeout=5)
        assert fixture.service.get_for_context("ctx-1") is original
        assert not original.natural_handoff_admits_replacement()
    finally:
        if barrier is not None:
            barrier.release.set()
        if beginning is not None:
            beginning.cancel()
            await asyncio.gather(beginning, return_exceptions=True)
        await fixture.close()


@pytest.mark.asyncio
async def test_persisted_handoff_keeps_shared_owner_cas(tmp_path: Path) -> None:
    fixture = _NaturalHandoffFixture(tmp_path)
    try:
        original = await fixture.prepare()
        path = tmp_path / "execution-control" / "ctx-1.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["owner"] = "other-owner"
        document["ownerGeneration"] += 1
        atomic_write_json(path, document)
        unchanged = path.read_bytes()
        with pytest.raises(ExecutionControlConflictError):
            await fixture.replace()
        assert path.read_bytes() == unchanged
        assert fixture.service.get_for_context("ctx-1") is original
    finally:
        await fixture.close()
