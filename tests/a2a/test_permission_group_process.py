"""A committed group has one recovery owner across independent processes."""

import asyncio
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from iac_code.a2a.execution_control import ExecutionControlService
from iac_code.a2a.permission_pause import PendingPermissionPauseReferences
from iac_code.services.session_backup import SessionBackupService

from .test_normal_permission_cold_handoff import _NormalPermissionHandoff
from .test_normal_permission_cold_handoff import isolated_environment as isolated_environment
from .test_permission_group_handoff import PermissionGroup


class GroupRecoveryWorker:
    @classmethod
    def run(cls, root, checkpoint, start, activate, output):
        asyncio.run(cls.recover(Path(root), checkpoint, start, activate, output))

    @staticmethod
    async def recover(root, checkpoint, start, activate, output):
        service = ExecutionControlService(persistence_root=root / "a2a", backup_service=SessionBackupService())
        control = None
        try:
            assert await asyncio.to_thread(start.wait, 15)
            result = await service.reserve_recoverable_input_continuation_result(
                context_id="ctx-1", task_id="task-1", owner="", permission_checkpoint=checkpoint
            )
            output.put({"phase": "reserved", "pid": os.getpid(), "reason": result.reason})
            if not result.admitted:
                return
            assert await asyncio.to_thread(activate.wait, 15)
            control = await service.begin_execution(
                context_id="ctx-1",
                task_id="task-1",
                owner="",
                cwd=str(root),
                recoverable_input_admission=result.token,
            )
            output.put({"phase": "activated", "pid": os.getpid(), "executionId": control.execution_id})
        except BaseException as exc:
            output.put({"phase": "error", "type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            if control is not None:
                await control.detach_task(asyncio.current_task(), execution_status="input-required")
            await service.close()


class ReferenceWriteFailure:
    def __init__(self, original):
        self.original = original
        self.failed = False

    def write(self, path, document, **kwargs):
        if path.name == "pwb_concurrent_second.json" and not self.failed:
            self.failed = True
            raise OSError("injected second reference write failure")
        return self.original(path, document, **kwargs)


@pytest.mark.asyncio
async def test_two_processes_activate_only_one_owner_for_the_complete_group(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_PERMISSION_PAUSE_ENABLED", "1")
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    start, activate, output = context.Event(), context.Event(), context.Queue()
    workers = []
    try:
        await fixture.start()
        group = PermissionGroup(fixture)
        await group.register()
        await fixture.suspend()
        group.store.mark_suspended(group.second["boundaryId"])
        await fixture.service._commit_suspended_permission_handoff(fixture.control)
        checkpoint = group.store.load(group.first["boundaryId"])
        original = fixture.control.snapshot()
        for _ in range(2):
            process = context.Process(
                target=GroupRecoveryWorker.run, args=(str(tmp_path), checkpoint, start, activate, output)
            )
            process.start()
            workers.append(process)
        start.set()
        results = [await asyncio.to_thread(output.get, True, 15) for _ in workers]
        assert {result["reason"] for result in results} == {"ADMITTED", "ADMISSION_BUSY"}, results
        winner = next(result["pid"] for result in results if result["reason"] == "ADMITTED")
        activate.set()
        activated = await asyncio.to_thread(output.get, True, 15)
        assert activated["phase"] == "activated", activated
        assert activated["pid"] == winner
        for process in workers:
            await asyncio.to_thread(process.join, 15)
            assert process.exitcode == 0

        path = tmp_path / "a2a/execution-control/ctx-1.json"
        recovered = json.loads(path.read_text(encoding="utf-8"))
        assert recovered["executionId"] == activated["executionId"] != original["executionId"]
        assert recovered["ownerIdentity"]["pid"] == winner
        references = PendingPermissionPauseReferences(tmp_path / "a2a")
        index = [references.read_document(path) for path in references.paths()]
        assert len(index) == 2
        assert {record["executionId"] for record in index} == {recovered["executionId"]}
        assert {record["ownerIdentity"]["pid"] for record in index} == {winner}
        await fixture.control._persist_snapshot({**original, "revision": recovered["revision"] + 100})
        assert json.loads(path.read_text(encoding="utf-8"))["executionId"] == recovered["executionId"]
    finally:
        start.set()
        activate.set()
        for process in workers:
            if process.is_alive():
                process.kill()
            await asyncio.to_thread(process.join, 5)
            process.close()
        output.close()
        await fixture.close()


@pytest.mark.asyncio
async def test_partial_reference_io_failure_rolls_back_control_and_can_retry(tmp_path, monkeypatch):
    import iac_code.a2a.permission_pause as permission_pause

    monkeypatch.setenv("IAC_CODE_A2A_PERMISSION_PAUSE_ENABLED", "1")
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        group = PermissionGroup(fixture)
        await group.register()
        await fixture.suspend()
        group.store.mark_suspended(group.second["boundaryId"])
        await fixture.service._commit_suspended_permission_handoff(fixture.control)
        references = PendingPermissionPauseReferences(tmp_path / "a2a")
        before = {path: path.read_bytes() for path in references.paths()}
        path = tmp_path / "a2a/execution-control/ctx-1.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        result = await fixture.recovering.reserve_recoverable_input_continuation_result(
            context_id="ctx-1",
            task_id="task-1",
            owner="",
            permission_checkpoint=group.store.load(group.first["boundaryId"]),
        )
        assert result.admitted
        fault = ReferenceWriteFailure(permission_pause.atomic_write_json)
        monkeypatch.setattr(permission_pause, "atomic_write_json", fault.write)
        with pytest.raises(OSError, match="second reference"):
            await fixture.activate(result.token)
        assert fault.failed
        assert json.loads(path.read_text(encoding="utf-8")) == original
        assert {path: path.read_bytes() for path in references.paths()} == before
        recovered = await fixture.activate(result.token)
        assert recovered.execution_id != original["executionId"]
        assert json.loads(path.read_text(encoding="utf-8"))["executionId"] == recovered.execution_id
    finally:
        await fixture.close()
