"""Completed permission receipts cannot poison a later natural finalization."""

import asyncio
from functools import partial

import pytest

from iac_code.a2a.execution_control import ExecutionController
from iac_code.a2a.input_handoff import HandoffCommitResult, InputHandoffStore
from iac_code.agent.message import Message, ToolResultBlock
from iac_code.services.session_storage import SessionStorage

from .test_normal_permission_cold_handoff import _NormalPermissionHandoff
from .test_normal_permission_cold_handoff import isolated_environment as isolated_environment


class PermissionFinalization:
    def __init__(self, root, monkeypatch):
        self.fixture = _NormalPermissionHandoff(root, monkeypatch)
        self.root = root
        self.control: ExecutionController
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def prepare(self):
        await self.fixture.start()
        self.control = self.fixture.control
        self.checkpoints = self.fixture.pending.checkpoint_store
        self.boundary_id = self.fixture.pending.boundary_id
        await self.control.attach_task(asyncio.current_task())
        self.checkpoints.mark_suspended(self.boundary_id)
        self.fixture.executor._permission_wait_coordinator.unregister_live(self.boundary_id)
        self.control._permission_handoff.suspension_started = True
        self.control.set_permission_runtime_close_confirmation(
            partial(self.fixture.store.discard_context_runtime_for_input_handoff, "ctx-1")
        )

    async def gated_close(self):
        self.close_started.set()
        await self.close_release.wait()
        await self.fixture.store.discard_context_runtime_for_input_handoff("ctx-1")

    async def resolve(self):
        self.checkpoints.claim_decision(self.boundary_id, value="allow_once", source="user")
        SessionStorage().append(
            str(self.root),
            self.control.session_id,
            Message(role="user", content=[ToolResultBlock(tool_use_id="tool-1", content="completed")]),
        )
        digest = self.checkpoints.persisted_result_digest(self.checkpoints.load(self.boundary_id))
        assert digest is not None
        self.checkpoints.resolve(self.boundary_id, result_digest=digest, ack={"accepted": True})
        await self.fixture.executor._permission_input_registry.complete(self.fixture.pending)

    async def detach(self):
        return await self.control.detach_task(
            asyncio.current_task(), execution_status="input-required", natural_completion=True
        )

    async def close(self):
        self.close_release.set()
        await self.fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["before_schedule", "queued", "during_runtime_close"])
async def test_completed_permission_does_not_publish_a_stale_finalizer_error(tmp_path, monkeypatch, completion):
    case = PermissionFinalization(tmp_path, monkeypatch)
    try:
        await case.prepare()
        service = case.fixture.service
        if completion == "before_schedule":
            await case.resolve()
            await case.detach()
            assert case.control._suspended_input_handoff_task is None
        elif completion == "queued":
            lock = service._context_start_locks["ctx-1"]
            async with lock:
                await case.detach()
                queued = case.control._suspended_input_handoff_task
                assert queued is not None
                await case.resolve()
            await queued
        else:
            case.control.set_permission_runtime_close_confirmation(case.gated_close)
            await case.detach()
            await asyncio.wait_for(case.close_started.wait(), timeout=1)
            await case.resolve()
            case.close_release.set()
            await case.control._suspended_input_handoff_task

        assert case.checkpoints.load(case.boundary_id)["phase"] == "RESOLVED"
        assert case.control.snapshot()["commitError"] is None
        generation = case.control._natural_completion_generation
        assert generation is not None
        await case.control.finalize_natural_completion(task_id="task-1", completion_generation=generation)
        state = await case.control.observe_state()
        assert state["terminationReason"] == "natural_completion"
        assert state["commitError"] is None
        assert case.control.natural_handoff_admits_replacement(), state
        next_control = await service.begin_execution(
            context_id="ctx-1", task_id="task-2", owner="", cwd=str(tmp_path)
        )
        assert next_control.execution_id != case.control.execution_id
        await next_control.detach_task(asyncio.current_task(), execution_status="completed")
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_unfinished_permission_cas_failure_still_blocks_handoff(tmp_path, monkeypatch):
    case = PermissionFinalization(tmp_path, monkeypatch)
    try:
        await case.prepare()
        await case.control.confirm_permission_runtime_closed()
        lock = case.fixture.service._context_start_locks["ctx-1"]
        async with lock:
            await case.detach()
            snapshot = case.control.snapshot()
            advanced = {**snapshot, "revision": snapshot["revision"] + 1}
            assert InputHandoffStore.write_snapshot(
                case.control._persistence_path,
                advanced,
                snapshot["executionId"],
                snapshot["revision"],
            ) == HandoffCommitResult.APPLIED
        await case.control._suspended_input_handoff_task

        assert case.checkpoints.load(case.boundary_id)["phase"] == "SUSPENDED"
        assert case.control.snapshot()["commitError"] == "input_handoff_not_committed"
        assert case.control.input_handoff_ready() is False
        assert case.control.snapshot()["releaseReady"] is False
    finally:
        await case.close()
