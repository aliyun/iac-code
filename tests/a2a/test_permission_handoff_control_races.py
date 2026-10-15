"""Explicit control changes must fence a committed permission handoff."""

import asyncio
from datetime import timedelta

import pytest

from iac_code.a2a.execution_control import ExecutionControlConflictError, current_execution_control
from iac_code.services.permission_wait import PermissionWaitPolicy, format_utc, utc_now

from .fakes import FakeEventQueue
from .test_normal_permission_cold_handoff import _NormalPermissionHandoff
from .test_normal_permission_cold_handoff import isolated_environment as isolated_environment
from .test_permission_execution_control import permission_reply


class _TerminationCleanupGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def cleanup(self, _context_id: str, _task_id: str, _reason: str) -> None:
        self.entered.set()
        await self.release.wait()


class _PauseCommitGate:
    def __init__(self, control) -> None:
        self.original = control._persist_snapshot
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def persist(self, snapshot) -> None:
        self.entered.set()
        await self.release.wait()
        await self.original(snapshot)


class _GracePermissionHandoff(_NormalPermissionHandoff):
    async def run_streaming(self, prompt):
        async for event in super().run_streaming(prompt):
            yield event
        self.resumed = True
        self.resumed_control = current_execution_control()


@pytest.mark.asyncio
async def test_permission_termination_revokes_handoff_before_cleanup(tmp_path, monkeypatch):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    gate = _TerminationCleanupGate()
    try:
        await fixture.start()
        await fixture.suspend()
        fixture.control._termination_cleanup = gate.cleanup
        fixture.control._termination_cleanup_complete = False

        await fixture.control.terminate(
            execution_id=fixture.control.execution_id,
            request_id="stop-before-recovery",
            connection_epoch=1,
            reason="user_canceled",
        )
        await asyncio.wait_for(gate.entered.wait(), timeout=5)

        result = await fixture.recovering.reserve_recoverable_input_continuation_result(
            context_id="ctx-1", task_id="task-1", owner=""
        )

        assert not result.admitted, "Termination must revoke ownership before its cleanup can mutate the session"
        assert not gate.release.is_set()
    finally:
        gate.release.set()
        await fixture.close()


@pytest.mark.asyncio
async def test_recovered_owner_prevents_old_permission_termination_cleanup(tmp_path, monkeypatch):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    gate = _TerminationCleanupGate()
    gate.release.set()
    try:
        await fixture.start()
        await fixture.suspend()
        admission = await fixture.reserve()
        assert admission is not None
        recovered = await fixture.activate(admission)
        fixture.control._termination_cleanup = gate.cleanup
        fixture.control._termination_cleanup_complete = False

        with pytest.raises(ExecutionControlConflictError):
            await fixture.control.terminate(
                execution_id=fixture.control.execution_id,
                request_id="stale-owner-stop",
                connection_epoch=1,
                reason="user_canceled",
            )

        await asyncio.sleep(0)
        assert not gate.entered.is_set(), "A replaced owner cannot run cleanup against the new owner's session"
        assert fixture.recovering.get_for_context("ctx-1") is recovered
        assert recovered.phase == "running"
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_permission_pause_invalidates_previously_reserved_recovery(tmp_path, monkeypatch):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    gate = None
    try:
        await fixture.start()
        await fixture.suspend()
        admission = await fixture.reserve()
        assert admission is not None
        gate = _PauseCommitGate(fixture.control)
        monkeypatch.setattr(fixture.control, "_persist_snapshot", gate.persist)

        await fixture.control.pause(
            task_id="task-1",
            expected_execution_id=fixture.control.execution_id,
            request_id="pause-before-activation",
            connection_epoch=1,
            reason="disconnect",
            reconnect_timeout_seconds=3600,
        )
        await asyncio.wait_for(gate.entered.wait(), timeout=5)

        with pytest.raises(ExecutionControlConflictError):
            await fixture.activate(admission)

        assert fixture.recovering.get_for_context("ctx-1") is None
        assert fixture.control.phase in {"pausing", "pause_committing", "paused"}
    finally:
        if gate is not None:
            gate.release.set()
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("inflight", [False, True])
async def test_grace_window_keeps_hot_permission_reply_bound_to_execution(tmp_path, monkeypatch, inflight):
    fixture = _GracePermissionHandoff(tmp_path, monkeypatch)
    suspension = None
    reply = None
    release = asyncio.Event()
    try:
        await fixture.start()
        fixture.executor._permission_wait_coordinator.policy = PermissionWaitPolicy(
            resident_timeout_seconds=3600, timeout_grace_seconds=30
        )
        fixture.pending.checkpoint_store.transaction(
            fixture.pending.boundary_id,
            lambda record: {**record, "residentDeadlineAt": format_utc(utc_now() - timedelta(seconds=1))},
        )

        coordinator = fixture.executor._permission_wait_coordinator
        if inflight:
            entered = asyncio.Event()
            original = coordinator._suspend_owner

            async def suspend_owner(owner):
                entered.set()
                await release.wait()
                return await original(owner)

            monkeypatch.setattr(coordinator, "_suspend_owner", suspend_owner)
            suspension = asyncio.create_task(coordinator.suspend_now(fixture.pending.boundary_id))
            await asyncio.wait_for(entered.wait(), timeout=5)
            reply = asyncio.create_task(
                fixture.executor.execute(
                    permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue()
                )
            )
            await asyncio.sleep(0)
            release.set()
            suspended = await suspension
        else:
            suspended = await coordinator.suspend_now(fixture.pending.boundary_id)
            checkpoint = fixture.pending.checkpoint_store.load(fixture.pending.boundary_id)
            assert checkpoint["phase"] == "TIMEOUT_GRACE"
            assert fixture.pending.continuation is not None

        assert not suspended
        if reply is not None:
            await reply
        else:
            await fixture.executor.execute(
                permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue()
            )

        assert fixture.resumed
        assert fixture.resumed_control is fixture.control, "The grace window must preserve execution ownership tracking"
    finally:
        release.set()
        if suspension is not None:
            await asyncio.gather(suspension, return_exceptions=True)
        if reply is not None:
            await asyncio.gather(reply, return_exceptions=True)
        await fixture.close()
