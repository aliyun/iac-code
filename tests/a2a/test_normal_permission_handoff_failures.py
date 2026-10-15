"""Failure and concurrency fences around the real Normal permission suspension."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest

from iac_code.a2a.execution_control import (
    ExecutionControlConflictError,
    ExecutionControlService,
    current_execution_control,
)
from iac_code.a2a.executor import IacCodeA2AExecutor, InputResponseExecutionControlConflictError
from iac_code.a2a.input_handoff import HandoffCommitResult, InputHandoffStore
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.permission_wait import format_utc, utc_now
from iac_code.services.session_backup import BackupResult
from iac_code.services.session_backup_staging import StagedSessionBackupService
from iac_code.types.stream_events import PermissionRequestEvent, TextDeltaEvent

from .fakes import FakeEventQueue, FakeRuntime
from .test_execution_control_regressions import wait_until
from .test_normal_permission_cold_handoff import (
    _NormalPermissionHandoff,
)
from .test_normal_permission_cold_handoff import (
    isolated_environment as isolated_environment,
)
from .test_permission_execution_control import continuation_frame, permission_reply


class _BackupFailure:
    def __init__(self, fixture: _NormalPermissionHandoff, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
        self.kind: str | None = kind
        self.original = fixture.backup.backup_recoverable_input_handoff
        monkeypatch.setattr(fixture.backup, "backup_recoverable_input_handoff", self.backup)

    def backup(self, cwd: str, session_id: str, *, operation_commit_id: str) -> BackupResult:
        if self.kind == "error":
            raise OSError("offline handoff backup failed")
        if self.kind == "staged":
            return BackupResult(
                enabled=True,
                succeeded=True,
                generation=1,
                commit_id=operation_commit_id,
                staged_committed=True,
                shared_committed=False,
            )
        return self.original(cwd, session_id, operation_commit_id=operation_commit_id)


class _FinalCommitFailure:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, timing: str) -> None:
        self.timing = timing
        self.failed = False
        self.original = InputHandoffStore.write_snapshot
        monkeypatch.setattr(InputHandoffStore, "write_snapshot", staticmethod(self.write))

    def write(self, control_path, snapshot, expected_execution_id, expected_revision, expected_proof=None):
        proof = snapshot.get("inputHandoff")
        final = isinstance(proof, dict) and proof.get("phase") == "COMMITTED"
        fail = final and not self.failed
        if fail:
            self.failed = True
            if self.timing == "before":
                raise OSError("offline final ownership write failed before rename")
        result = self.original(
            control_path, snapshot, expected_execution_id, expected_revision, expected_proof=expected_proof
        )
        if fail and result != HandoffCommitResult.FENCED_OUT:
            raise OSError("offline final ownership receipt lost after rename")
        return result


class _SuspensionGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.original: Any = None

    async def suspend(self) -> None:
        self.entered.set()
        await self.release.wait()
        if self.original is not None:
            await self.original()


class _MirrorFailure:
    def __init__(self, fixture: _NormalPermissionHandoff, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fail = True
        self.original = fixture.backup._mirror
        monkeypatch.setattr(fixture.backup, "_mirror", self.mirror)
        monkeypatch.setattr(fixture.backup, "_retry_delays", ())

    def mirror(self, source, destination):
        if self.fail:
            raise OSError("offline shared mirror unavailable")
        return self.original(source, destination)


class _GenerationRace:
    def __init__(self, fixture: _NormalPermissionHandoff, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
        self.store = fixture.pending.checkpoint_store
        self.kind = kind
        self.original = self.store.run_generation_fenced
        monkeypatch.setattr(self.store, "run_generation_fenced", self.run)

    @staticmethod
    def advance(record: dict[str, Any]) -> dict[str, Any]:
        return {**record, "generation": record["generation"] + 1}

    def run(self, boundary_id: str, *, expected_generation: int, operation: Any) -> Any:
        if self.kind == "cancel":
            self.store.cancel(boundary_id)
        else:
            self.store.transaction(boundary_id, self.advance)
        return self.original(boundary_id, expected_generation=expected_generation, operation=operation)


class _FailureHandoff(_NormalPermissionHandoff):
    def record_decision_audit(self) -> None:
        store = self.pending.checkpoint_store
        claimed, _ = store.claim_decision(self.pending.boundary_id, value="allow_once", source="user")
        store.run_claim_audit_once(
            self.pending.boundary_id, claim_id=claimed["decision"]["claimId"], audit=lambda _: True
        )

    def use_staged_backup(self) -> None:
        self.backup = StagedSessionBackupService(self.root / "staging", retry_delays=())
        self.store._backup_service = self.backup
        self.executor._backup_service = self.backup
        self.service._backup_service = self.backup
        self.recovering._backup_service = self.backup

    @staticmethod
    def expired_deadline(record: dict[str, Any]) -> dict[str, Any]:
        return {**record, "residentDeadlineAt": format_utc(utc_now() - timedelta(seconds=1))}

    async def suspend_with_checkpoint_race(self) -> None:
        self.pending.checkpoint_store.transaction(self.pending.boundary_id, self.expired_deadline)
        assert await self.executor._permission_wait_coordinator.suspend_now(self.pending.boundary_id)
        assert self.pending.continuation is None
        assert not self.executor._permission_input_registry._pending
        await self.drain()

    def document(self) -> dict[str, Any]:
        path = self.root / "a2a/execution-control/ctx-1.json"
        return json.loads(path.read_text(encoding="utf-8"))

    async def local_reserve(self):
        return await self.service.reserve_recoverable_input_continuation_result(
            context_id="ctx-1", task_id="task-1", owner=""
        )

    async def assert_not_recoverable(self) -> None:
        assert not self.control.input_handoff_ready()
        assert self.document()["inputHandoffReady"] is False
        result = await self.recovering.reserve_recoverable_input_continuation_result(
            context_id="ctx-1", task_id="task-1", owner=""
        )
        assert result.token is None
        assert not result.admitted


class _RecoveryLoop:
    def __init__(self, fixture: _FailureHandoff) -> None:
        self.fixture = fixture
        self.resumed: list[str] = []

    def runtime(self, options: Any) -> FakeRuntime:
        return FakeRuntime(session_id=options.session_id, agent_loop=self)

    async def resume_permission_boundary(self, checkpoint: dict[str, Any]):
        control = current_execution_control()
        assert control is not None
        assert control.execution_id != self.fixture.control.execution_id
        self.resumed.append(checkpoint["decision"]["value"])
        yield TextDeltaEvent(text="recovered result")


class _SecondPermissionLoop(_RecoveryLoop):
    async def resume_permission_boundary(self, checkpoint: dict[str, Any]):
        async for event in super().resume_permission_boundary(checkpoint):
            yield event
        frame = continuation_frame()
        frame["orderedToolUseIds"] = ["tool-2"]
        frame["decisions"][0]["toolUseId"] = "tool-2"
        yield PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="tool-2",
            response_future=asyncio.get_running_loop().create_future(),
            continuation_frame=frame,
        )


class _ForbiddenOrdinaryBegin:
    async def begin(self, **_kwargs):
        raise AssertionError("A rejected recovery must not fall through to ordinary begin")


class _RevisionAdvanceBeforeActivation:
    def __init__(self, fixture: _FailureHandoff, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fixture = fixture
        self.original = fixture.recovering.begin_execution
        monkeypatch.setattr(fixture.recovering, "begin_execution", self.begin)

    async def begin(self, **kwargs):
        document = self.fixture.document()
        document["revision"] += 1
        document["persistedRevision"] = document["revision"]
        path = self.fixture.root / "a2a/execution-control/ctx-1.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return await self.original(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "staged"])
async def test_failed_backup_never_releases_owner_and_same_process_can_retry(tmp_path, monkeypatch, failure):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    fault = _BackupFailure(fixture, monkeypatch, failure)
    try:
        await fixture.start()
        await fixture.suspend()
        await fixture.assert_not_recoverable()
        assert fixture.document()["inputHandoff"]["phase"] == "PREPARING"

        fault.kind = None
        result = await fixture.local_reserve()

        assert result.admitted, "A retry with a real shared backup receipt must finish the same handoff"
        assert fixture.control.input_handoff_ready()
        assert fixture.document()["inputHandoff"]["phase"] == "COMMITTED"
        assert fixture.document()["inputHandoff"]["backupReceipt"]["sharedCommitted"] is True
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["suspend_callback", "runtime_close"])
async def test_running_suspension_cleanup_cannot_be_taken_over(tmp_path, monkeypatch, blocked):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    gate = _SuspensionGate()
    suspension = None
    try:
        await fixture.start()
        if blocked == "suspend_callback":
            gate.original = fixture.pending.suspend_callback
            fixture.pending.suspend_callback = gate.suspend
        else:
            context = fixture.store._contexts["ctx-1"]
            context.runtime.aclose = gate.suspend
        suspension = asyncio.create_task(fixture.suspend())
        await asyncio.wait_for(gate.entered.wait(), timeout=5)

        await fixture.assert_not_recoverable()

        gate.release.set()
        await suspension
        assert fixture.control.input_handoff_ready()
        assert await fixture.reserve() is not None
    finally:
        gate.release.set()
        if suspension is not None:
            await suspension
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("subprocess", [False, True])
async def test_undrained_business_activity_prevents_permission_handoff(tmp_path, monkeypatch, subprocess):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    activity = None
    try:
        await fixture.start()
        activity = await fixture.control.begin_activity(
            "bash" if subprocess else "business", check_gate=False, may_spawn_subprocess=subprocess
        )
        await fixture.suspend_with_checkpoint_race()

        await fixture.assert_not_recoverable()
        assert not (await fixture.local_reserve()).admitted

        await fixture.control.end_activity(activity.activity_id)
        activity = None
        assert (await fixture.local_reserve()).admitted
        assert fixture.control.input_handoff_ready()
    finally:
        if activity is not None:
            await fixture.control.end_activity(activity.activity_id)
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["background_agent", "business", "subprocess"])
@pytest.mark.parametrize("staged", [False, True])
async def test_late_business_drain_publishes_permission_handoff_for_new_worker(tmp_path, monkeypatch, blocker, staged):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    release = asyncio.Event()
    background = None
    activity = None
    try:
        if staged:
            fixture.use_staged_backup()
        await fixture.start()
        if blocker == "background_agent":
            background = asyncio.create_task(release.wait())
            fixture.control.register_spawned_task(background, kind="background_agent")
        else:
            activity = await fixture.control.begin_activity(
                blocker, check_gate=False, may_spawn_subprocess=blocker == "subprocess"
            )
        await fixture.suspend_with_checkpoint_race()
        await fixture.assert_not_recoverable()

        if background is not None:
            release.set()
            await background
        else:
            await fixture.control.end_activity(activity.activity_id)
            activity = None
        # No request returns to the original service to retry the handoff.
        await wait_until(fixture.control.input_handoff_ready)
        proof = fixture.document()["inputHandoff"]
        assert proof["phase"] == "COMMITTED"
        assert proof["backupReceipt"]["sharedCommitted"] is True
        admission = await fixture.reserve()
        assert admission is not None
        recovered = await fixture.activate(admission)
        assert recovered.execution_id != fixture.control.execution_id
        assert recovered.task_id == fixture.control.task_id
    finally:
        release.set()
        if background is not None:
            await asyncio.gather(background, return_exceptions=True)
        if activity is not None:
            await fixture.control.end_activity(activity.activity_id)
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
async def test_late_background_agent_completion_allows_cold_executor_permission_reply(tmp_path, monkeypatch, staged):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    release = asyncio.Event()
    background = None
    cold_store = None
    try:
        if staged:
            fixture.use_staged_backup()
        await fixture.start()
        background = asyncio.create_task(release.wait())
        fixture.control.register_spawned_task(background, kind="background_agent")
        await fixture.suspend_with_checkpoint_race()
        await fixture.assert_not_recoverable()

        release.set()
        await background
        await wait_until(fixture.control.input_handoff_ready)
        fixture.record_decision_audit()
        recovery = _RecoveryLoop(fixture)
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", recovery.runtime)
        cold_store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "a2a"), backup_service=fixture.backup)
        cold_store.set_execution_control_provider(
            fixture.recovering.snapshot_for_context, fixture.recovering.has_active_work
        )
        executor = IacCodeA2AExecutor(
            task_store=cold_store,
            model="test",
            backup_service=fixture.backup,
            execution_control_service=fixture.recovering,
        )

        await executor.execute(permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue())

        assert recovery.resumed == ["allow_once"]
        assert fixture.pending.checkpoint_store.load(fixture.pending.boundary_id)["phase"] == "RESOLVED"
        assert fixture.recovering.get_for_context("ctx-1").execution_id != fixture.control.execution_id
    finally:
        release.set()
        if background is not None:
            await asyncio.gather(background, return_exceptions=True)
        if cold_store is not None:
            await cold_store.stop_cleanup_loop()
        await fixture.close()


@pytest.mark.asyncio
async def test_same_service_permission_reply_acquires_new_owner_before_resuming_boundary(tmp_path, monkeypatch):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.suspend()
        recovery = _RecoveryLoop(fixture)
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", recovery.runtime)
        # The shared fixture uses a fake continuation transcript. Establish its
        # offline decision audit exactly as the existing cold-recovery tests do.
        store = fixture.pending.checkpoint_store
        claimed, _ = store.claim_decision(fixture.pending.boundary_id, value="allow_once", source="user")
        store.run_claim_audit_once(
            fixture.pending.boundary_id, claim_id=claimed["decision"]["claimId"], audit=lambda _: True
        )

        await fixture.executor.execute(permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue())

        assert recovery.resumed == ["allow_once"]
        assert fixture.pending.checkpoint_store.load(fixture.pending.boundary_id)["phase"] == "RESOLVED"
        assert fixture.service.get_for_context("ctx-1").execution_id != fixture.control.execution_id
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["generation", "cancel"])
async def test_checkpoint_change_before_ownership_preparation_blocks_commit(tmp_path, monkeypatch, race):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        _GenerationRace(fixture, monkeypatch, race)

        await fixture.suspend_with_checkpoint_race()

        await fixture.assert_not_recoverable()
        assert fixture.document().get("inputHandoff") is None
        checkpoint = fixture.pending.checkpoint_store.load(fixture.pending.boundary_id)
        if race == "cancel":
            assert checkpoint["phase"] == "CANCELED"
        else:
            assert checkpoint["generation"] > fixture.control._permission_handoff.proof.boundary_generation
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("task_id", "continue_input_required"), [("task-2", False), ("task-1", True)])
async def test_preparing_handoff_cannot_be_bypassed_by_same_service_ordinary_begin(
    tmp_path, monkeypatch, task_id, continue_input_required
):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    _BackupFailure(fixture, monkeypatch, "error")
    try:
        await fixture.start()
        await fixture.suspend()
        before = fixture.document()
        assert before["inputHandoff"]["phase"] == "PREPARING"

        with pytest.raises(ExecutionControlConflictError):
            await fixture.service.begin_execution(
                context_id="ctx-1",
                task_id=task_id,
                owner="",
                cwd=str(tmp_path),
                continue_input_required=continue_input_required,
            )

        assert fixture.document() == before
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
async def test_real_failed_mirror_then_decision_generation_advance_can_retry_handoff(tmp_path, monkeypatch, staged):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    try:
        if staged:
            fixture.use_staged_backup()
        await fixture.start()
        fault = _MirrorFailure(fixture, monkeypatch)
        await fixture.suspend()
        await fixture.assert_not_recoverable()
        previous = fixture.document()["inputHandoff"]
        store = fixture.pending.checkpoint_store
        claimed, _ = store.claim_decision(fixture.pending.boundary_id, value="allow_once", source="user")
        assert claimed["generation"] > previous["boundaryGeneration"]
        fault.fail = False

        result = await fixture.local_reserve()

        assert result.admitted, "A failed backup attempt must not permanently fence the newer decision generation"
        document = fixture.document()
        assert document["inputHandoff"]["phase"] == "COMMITTED"
        assert document["inputHandoff"]["boundaryGeneration"] == claimed["generation"]
        assert document["inputHandoff"]["backupReceipt"]["sharedCommitted"] is True
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_two_fresh_workers_reserve_once_and_replaced_writer_cannot_overwrite(tmp_path, monkeypatch):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    contender = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=fixture.backup)
    try:
        await fixture.start()
        await fixture.suspend()
        before = fixture.document()
        services = [fixture.recovering, contender]
        results = await asyncio.gather(
            *[
                service.reserve_recoverable_input_continuation_result(context_id="ctx-1", task_id="task-1", owner="")
                for service in services
            ]
        )

        assert sum(result.admitted for result in results) == 1
        assert {result.reason for result in results} == {"ADMITTED", "ADMISSION_BUSY"}
        winner = services[next(index for index, result in enumerate(results) if result.admitted)]
        admission = next(result.token for result in results if result.admitted)
        recovered = await winner.begin_execution(
            context_id="ctx-1", task_id="task-1", owner="", cwd=str(tmp_path), recoverable_input_admission=admission
        )
        after = fixture.document()
        assert recovered.execution_id != before["executionId"]
        assert after["executionId"] == recovered.execution_id

        stale = fixture.control.snapshot()
        stale["revision"] = after["revision"] + 100
        await fixture.control._persist_snapshot(stale)

        assert fixture.document() == after
        assert fixture.control.persisted_revision < stale["revision"]
        current = asyncio.current_task()
        assert current is not None
        await recovered.detach_task(current, execution_status="input-required")
    finally:
        await contender.close()
        await fixture.close()


@pytest.mark.asyncio
async def test_disabled_backup_keeps_cold_takeover_closed_but_local_permission_reply_works(tmp_path, monkeypatch):
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.suspend()
        await fixture.assert_not_recoverable()
        proof = fixture.document().get("inputHandoff")
        assert proof is None or proof.get("phase") != "COMMITTED"
        recovery = _RecoveryLoop(fixture)
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", recovery.runtime)
        fixture.record_decision_audit()

        await fixture.executor.execute(permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue())

        assert recovery.resumed == ["allow_once"]
        assert fixture.pending.checkpoint_store.load(fixture.pending.boundary_id)["phase"] == "RESOLVED"
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_second_resident_permission_after_cold_recovery_does_not_inherit_durable_handoff(tmp_path, monkeypatch):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    fresh = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=fixture.backup)
    executor = None
    second = None
    execution = None
    try:
        await fixture.start()
        await fixture.suspend()
        fixture.record_decision_audit()
        recovery = _SecondPermissionLoop(fixture)
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", recovery.runtime)
        executor = IacCodeA2AExecutor(
            task_store=fixture.store,
            model="test",
            backup_service=fixture.backup,
            execution_control_service=fixture.recovering,
        )

        execution = asyncio.create_task(
            executor.execute(permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue())
        )
        await wait_until(lambda: bool(executor._permission_input_registry._pending))

        second = next(iter(executor._permission_input_registry._pending.values()))
        assert second.request.tool_use_id == "tool-2"
        assert second.request.response_future is not None
        assert not second.request.response_future.done()
        control = fixture.recovering.get_for_context("ctx-1")
        assert control is not None
        assert control.execution_id != fixture.control.execution_id
        assert not control.input_handoff_ready()
        result = await fresh.reserve_recoverable_input_continuation_result(
            context_id="ctx-1", task_id="task-1", owner=""
        )
        assert not result.admitted
        assert fixture.document()["inputHandoffReady"] is False
    finally:
        if execution is not None:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        if second is not None and executor is not None:
            await executor._permission_input_registry.complete(second)
        await fresh.close()
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["related", "unrelated"])
async def test_reserved_admission_is_fenced_when_control_revision_advances(tmp_path, monkeypatch, change):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.suspend()
        admission = await fixture.reserve()
        assert admission is not None
        advanced = fixture.document()
        advanced["revision"] += 1
        advanced["persistedRevision"] = advanced["revision"]
        if change == "related":
            advanced["inputHandoffReady"] = False
        else:
            advanced["pauseReason"] = "independent-control-update"
        path = fixture.root / "a2a/execution-control/ctx-1.json"
        path.write_text(json.dumps(advanced), encoding="utf-8")

        with pytest.raises(ExecutionControlConflictError):
            await fixture.activate(admission)

        assert fixture.document() == advanced
        assert fixture.recovering.get_for_context("ctx-1") is None
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_permission_recovery_rejection_preserves_typed_reason_without_ordinary_begin(tmp_path, monkeypatch):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    _BackupFailure(fixture, monkeypatch, "error")
    try:
        await fixture.start()
        await fixture.suspend()
        fixture.record_decision_audit()
        forbidden = _ForbiddenOrdinaryBegin()
        monkeypatch.setattr(fixture.service, "begin_execution", forbidden.begin)
        before = fixture.document()["executionId"]

        with pytest.raises(InputResponseExecutionControlConflictError, match="HANDOFF_NOT_COMMITTED"):
            await fixture.executor.execute(
                permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue()
            )

        assert fixture.document()["executionId"] == before
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_final_handoff_write_failure_can_retry_same_proof_and_backup_receipt(tmp_path, monkeypatch, timing):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    _FinalCommitFailure(monkeypatch, timing)
    recovered = None
    try:
        await fixture.start()
        await fixture.suspend()
        before = fixture.document()
        assert before["inputHandoff"]["phase"] == ("PREPARING" if timing == "before" else "COMMITTED")
        assert before["inputHandoffReady"] is (timing == "after")
        assert not fixture.control.input_handoff_ready()
        assert fixture.control._input_handoff_proof.phase == "PREPARING"
        assert fixture.control._commit_error == "input_handoff_commit_failed"

        result = await fixture.local_reserve()

        assert result.admitted, "The exact committed handoff must remain retryable when its write receipt is lost"
        assert fixture.control.input_handoff_ready()
        committed = fixture.document()
        assert committed["inputHandoff"]["phase"] == "COMMITTED"
        assert committed["inputHandoff"]["backupCommitId"] == before["inputHandoff"]["backupCommitId"]
        assert committed["inputHandoff"]["backupReceipt"]["sharedCommitted"] is True
        if timing == "after":
            assert fixture.control._permission_handoff.result == HandoffCommitResult.ALREADY_APPLIED
            assert committed == before
        else:
            assert fixture.control._permission_handoff.result == HandoffCommitResult.APPLIED
        recovered = await fixture.service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="",
            cwd=str(tmp_path),
            recoverable_input_admission=result.token,
        )
        assert recovered.execution_id != fixture.control.execution_id
    finally:
        if recovered is not None:
            current = asyncio.current_task()
            assert current is not None
            await recovered.detach_task(current, execution_status="input-required")
        await fixture.close()


@pytest.mark.asyncio
async def test_cold_permission_activation_race_reports_stale_revision_and_recorded_decision(tmp_path, monkeypatch):
    fixture = _FailureHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.suspend()
        fixture.record_decision_audit()
        recovery = _RecoveryLoop(fixture)
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", recovery.runtime)
        executor = IacCodeA2AExecutor(
            task_store=fixture.store,
            model="test",
            backup_service=fixture.backup,
            execution_control_service=fixture.recovering,
        )
        _RevisionAdvanceBeforeActivation(fixture, monkeypatch)
        previous_execution = fixture.document()["executionId"]

        with pytest.raises(InputResponseExecutionControlConflictError) as captured:
            await executor.execute(permission_reply(tmp_path, input_id=fixture.pending.input_id), FakeEventQueue())

        assert captured.value.data == {
            "kind": "permission_recovery_conflict",
            "reason": "STALE_REVISION",
            "recoverable": True,
            "decisionRecorded": True,
            "executionResumed": False,
        }
        assert recovery.resumed == []
        assert fixture.document()["executionId"] == previous_execution
        checkpoint = fixture.pending.checkpoint_store.load(fixture.pending.boundary_id)
        assert checkpoint["decision"]["value"] == "allow_once"
        assert checkpoint["decision"]["auditStatus"] == "recorded"
    finally:
        await fixture.close()
