"""A single execution hands off every concurrent permission under one fence."""

import asyncio
import contextvars
from copy import copy, deepcopy

import pytest

from iac_code.a2a.execution_control import ExecutionControlConflictError, ExecutionController, ExecutionTerminatedError
from iac_code.a2a.input_required import PermissionInputRegistry
from iac_code.a2a.permission_pause import PendingPermissionPauseInspector, PendingPermissionPauseReferences
from iac_code.agent.message import Message, ToolResultBlock, ToolUseBlock
from iac_code.services.permission_wait import canonical_digest
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import MessageStartEvent, SubPipelineStreamEvent

from .test_normal_permission_cold_handoff import _NormalPermissionHandoff
from .test_normal_permission_cold_handoff import isolated_environment as isolated_environment
from .test_pipeline_stream import _publisher


class PermissionCleanupGate:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def suspend(self):
        self.started.set()
        await self.release.wait()
        return True


class PermissionGroup:
    def __init__(self, fixture):
        self.fixture = fixture
        self.store = fixture.pending.checkpoint_store
        self.first = self.store.load(fixture.pending.boundary_id)
        self.second = deepcopy(self.first)
        self.second.update(boundaryId="pwb_concurrent_second", inputId="input-second", toolUseId="tool-2")
        frame = self.second["continuationFrame"]
        frame["orderedToolUseIds"] = ["tool-2"]
        frame["decisions"][0]["toolUseId"] = "tool-2"

    async def register(self):
        self.store.create(self.second)
        control = self.fixture.control
        control.bind_recoverable_permission_boundary(self.store, self.second["boundaryId"], concurrent=True)
        await control.register_permission_pause_boundary(self.store, self.second)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_phase", ["SUSPENDED", "CANCELED"])
async def test_group_waits_for_every_boundary_and_commits_one_shared_capture(tmp_path, monkeypatch, second_phase):
    monkeypatch.setenv("IAC_CODE_A2A_PERMISSION_PAUSE_ENABLED", "1")
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        group = PermissionGroup(fixture)
        await group.register()
        await fixture.suspend()
        inspector = PendingPermissionPauseInspector(tmp_path / "a2a")
        assert inspector.inspect()["status"] == "pending"
        assert not fixture.control.input_handoff_ready()

        if second_phase == "CANCELED":
            group.store.cancel(group.second["boundaryId"])
        else:
            group.store.mark_suspended(group.second["boundaryId"])
        await fixture.service._commit_suspended_permission_handoff(fixture.control)

        assert fixture.control.input_handoff_ready()
        assert inspector.inspect()["status"] == "ready"
        proof = fixture.control.snapshot()["inputHandoff"]
        assert proof["backupReceipt"]["sharedCommitted"] is True
        assert proof["boundaries"] == {
            group.first["boundaryId"]: group.store.load(group.first["boundaryId"])["generation"],
            group.second["boundaryId"]: group.store.load(group.second["boundaryId"])["generation"],
        }
        for record in (group.first, group.second):
            checkpoint = group.store.load(record["boundaryId"])
            if checkpoint["phase"] == "CANCELED":
                continue
            reservation = await fixture.recovering.reserve_recoverable_input_continuation_result(
                context_id="ctx-1", task_id="task-1", owner="", permission_checkpoint=checkpoint
            )
            assert reservation.admitted
            await fixture.recovering.release_recoverable_input_continuation(reservation.token)
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_candidate_tool_result_does_not_compact_a_sibling_permission(tmp_path, monkeypatch):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        group = PermissionGroup(fixture)
        await group.register()
        tool = ToolUseBlock(id="tool-1", name="bash", input={"cmd": "pwd"})
        storage = SessionStorage()
        index = len(storage.load(str(tmp_path), group.first["sessionId"]))
        storage.append(str(tmp_path), group.first["sessionId"], Message(role="assistant", content=[tool]))
        group.store.transaction(
            group.first["boundaryId"],
            lambda record: {
                **record,
                "continuationFrame": {
                    **record["continuationFrame"],
                    "assistantMessageRef": f"session.jsonl:{index}",
                    "assistantMessageDigest": canonical_digest([tool.model_dump(mode="json")]),
                },
            },
        )
        storage.append(
            str(tmp_path),
            group.first["sessionId"],
            Message(role="user", content=[ToolResultBlock(tool_use_id="tool-1", content="fixture result")]),
        )
        publisher, _queue = _publisher(tmp_path)
        publisher.permission_input_registry = PermissionInputRegistry()
        for index, record in enumerate((group.first, group.second)):
            pending = copy(fixture.pending)
            pending.input_id = record["inputId"]
            pending.boundary_id = record["boundaryId"]
            pending.coordinates = {"candidate": {"id": f"candidate-{index}", "index": index}}
            publisher._consumed_durable_permissions.append(pending)

        await publisher.publish(SubPipelineStreamEvent("candidate-0", 0, MessageStartEvent("after-tool-result")))

        assert group.store.load(group.first["boundaryId"])["phase"] == "RESOLVED"
        assert group.store.load(group.second["boundaryId"])["phase"] == "WAITING"
        assert [pending.boundary_id for pending in publisher._consumed_durable_permissions] == [
            group.second["boundaryId"]
        ]
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_completed_permission_receipt_retires_only_its_local_suspension_gate(tmp_path, monkeypatch):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        control = fixture.control
        checkpoint = fixture.pending.checkpoint_store
        boundary_id = fixture.pending.boundary_id
        control._permission_handoff.suspension_started = True
        current = asyncio.current_task()
        with pytest.raises(ExecutionControlConflictError, match="recovery admission"):
            await control.attach_task(current)
        checkpoint.claim_decision(boundary_id, value="allow_once", source="user")
        SessionStorage().append(
            str(tmp_path),
            control.session_id,
            Message(role="user", content=[ToolResultBlock(tool_use_id="tool-1", content="fixture result")]),
        )
        digest = checkpoint.persisted_result_digest(checkpoint.load(boundary_id))
        assert digest is not None
        checkpoint.resolve(boundary_id, result_digest=digest, ack={"accepted": True})

        await control.attach_task(current)
        await control.checkpoint()
        await control.detach_task(current, execution_status="input-required")
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_one_suspension_cannot_release_another_inflight_permission_cleanup(tmp_path):
    control = ExecutionController(
        context_id="ctx",
        task_id="task",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="server",
        persistence_path=None,
        backup_service=None,
    )
    first, second = PermissionCleanupGate(), PermissionCleanupGate()
    operations = [
        asyncio.create_task(control.run_permission_suspension(first.suspend)),
        asyncio.create_task(control.run_permission_suspension(second.suspend)),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(first.started.wait(), second.started.wait()), timeout=1)
        first.release.set()
        assert await operations[0]
        assert control.permission_handoff_requires_admission()
    finally:
        second.release.set()
        await asyncio.gather(*operations)
        await control.close()


@pytest.mark.asyncio
async def test_recovery_cas_rebinds_every_original_card_to_the_replacement_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_PERMISSION_PAUSE_ENABLED", "1")
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        group = PermissionGroup(fixture)
        await group.register()
        await fixture.suspend()
        group.store.mark_suspended(group.second["boundaryId"])
        await fixture.service._commit_suspended_permission_handoff(fixture.control)
        checkpoint = group.store.load(group.first["boundaryId"])
        reservation = await fixture.recovering.reserve_recoverable_input_continuation_result(
            context_id="ctx-1", task_id="task-1", owner="", permission_checkpoint=checkpoint
        )
        assert reservation.admitted
        recovered = await fixture.activate(reservation.token)

        references = PendingPermissionPauseReferences(tmp_path / "a2a")
        documents = [references.read_document(path) for path in references.paths()]
        assert len(documents) == 2
        assert {record["executionId"] for record in documents} == {recovered.execution_id}
        assert all(record["ownerIdentity"] == recovered.snapshot()["ownerIdentity"] for record in documents)
        assert PendingPermissionPauseInspector(tmp_path / "a2a").inspect()["status"] == "pending"
        current = group.store.load(group.second["boundaryId"])
        reactivated = group.store.reactivate(current["boundaryId"], {**current, "phase": "WAITING"})
        await recovered.register_permission_pause_boundary(group.store, reactivated)
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_live_registered_candidate_advances_while_sibling_suspends_but_new_owner_is_rejected(
    tmp_path, monkeypatch
):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="server",
        persistence_path=None,
        backup_service=None,
        execution_mode="pipeline",
    )
    gate = PermissionCleanupGate()
    suspension = foreign = None
    try:
        await fixture.start()
        current = asyncio.current_task()
        await control.attach_task(current)
        control.bind_recoverable_permission_boundary(fixture.pending.checkpoint_store, fixture.pending.boundary_id)
        suspension = asyncio.create_task(control.run_permission_suspension(gate.suspend))
        await asyncio.wait_for(gate.started.wait(), timeout=1)
        assert control.permission_handoff_requires_admission()
        await control.checkpoint()
        activity = await control.begin_activity("sibling-model")
        await control.end_activity(activity.activity_id)
        child = asyncio.create_task(asyncio.sleep(0))
        control.register_spawned_task(child, kind="sibling-model")
        await child

        foreign = contextvars.Context().run(asyncio.create_task, control.checkpoint())
        # Python 3.10 discards CancelledError subclasses across a Task boundary.
        with pytest.raises(asyncio.CancelledError):
            await foreign
        with pytest.raises(ExecutionControlConflictError):
            await control.attach_task(current)
        control.set_permission_runtime_close_confirmation(lambda: asyncio.sleep(0))
        with pytest.raises(ExecutionTerminatedError):
            await control.checkpoint()
    finally:
        gate.release.set()
        if suspension is not None:
            await suspension
        if foreign is not None:
            await asyncio.gather(foreign, return_exceptions=True)
        await control.detach_task(asyncio.current_task(), execution_status="input-required")
        await control.close()
        await fixture.close()
