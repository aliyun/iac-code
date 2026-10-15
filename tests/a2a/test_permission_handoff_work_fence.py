"""A committed owner cannot start business work after yielding execution."""

import asyncio

import pytest

from iac_code.a2a.execution_control import ExecutionControlConflictError, ExecutionTerminatedError

from .test_normal_permission_cold_handoff import _NormalPermissionHandoff
from .test_normal_permission_cold_handoff import isolated_environment as isolated_environment


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn", [False, True])
async def test_committed_permission_owner_rejects_new_business_work(tmp_path, monkeypatch, spawn):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    task = None
    activity = None
    try:
        await fixture.start()
        await fixture.suspend()
        if spawn:
            task = asyncio.create_task(asyncio.Event().wait())
            with pytest.raises(ExecutionTerminatedError):
                fixture.control.register_spawned_task(task, kind="business")
        else:
            with pytest.raises(ExecutionControlConflictError, match="recovery admission"):
                activity = await fixture.control.begin_activity(
                    "business", check_gate=False, may_spawn_subprocess=True
                )
        assert fixture.control.input_handoff_ready()
        assert await fixture.reserve() is not None
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if activity is not None:
            await fixture.control.end_activity(activity.activity_id)
        await fixture.close()


@pytest.mark.asyncio
async def test_committed_permission_owner_rejects_checkpoint_before_registering_participant(tmp_path, monkeypatch):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.suspend()
        checkpoint = asyncio.create_task(fixture.control.checkpoint())
        # Python 3.10 normalizes CancelledError subclasses at Task boundaries.
        with pytest.raises(asyncio.CancelledError):
            await checkpoint
        assert fixture.control.input_handoff_ready()
        assert await fixture.reserve() is not None
    finally:
        await fixture.close()
