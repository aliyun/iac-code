"""Execution ownership after a real first Normal permission suspension."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from iac_code.a2a.execution_control import ExecutionControlConflictError, ExecutionControlService
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.agent.message import Message, ToolUseBlock
from iac_code.services.permission_wait import PermissionWaitPolicy, canonical_digest, format_utc, utc_now
from iac_code.services.session_backup import SessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import PermissionRequestEvent

from .fakes import FakeEventQueue, FakeRequestContext, FakeRuntime
from .test_execution_control_regressions import wait_until
from .test_execution_control_review_regressions import make_executor
from .test_permission_execution_control import continuation_frame


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)


class _NormalPermissionHandoff:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.backup = SessionBackupService()
        _, self.store, self.service = make_executor(tmp_path, self.backup)
        self.executor = IacCodeA2AExecutor(
            task_store=self.store,
            model="test",
            backup_service=self.backup,
            execution_control_service=self.service,
            permission_wait_policy=PermissionWaitPolicy(resident_timeout_seconds=3600, timeout_grace_seconds=0),
        )
        self.response = asyncio.get_running_loop().create_future()
        self.recovering = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=self.backup)
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", self.runtime)
        monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_kw: True)

    def runtime(self, options):
        return FakeRuntime(session_id=options.session_id, agent_loop=self)

    async def run_streaming(self, _prompt):
        context = await self.store.get_context_record("ctx-1")
        storage = SessionStorage()
        index = len(storage.load(str(self.root), context.session_id))
        tool = ToolUseBlock(id="tool-1", name="bash", input={"cmd": "pwd"})
        storage.append(str(self.root), context.session_id, Message(role="assistant", content=[tool]))
        yield PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="tool-1",
            response_future=self.response,
            continuation_frame={
                **continuation_frame(),
                "assistantMessageRef": f"session.jsonl:{index}",
                "assistantMessageDigest": canonical_digest([tool.model_dump(mode="json")]),
            },
        )

    async def start(self) -> None:
        context = await self.store.get_or_create_context(
            context_id="ctx-1",
            cwd=str(self.root),
            runtime_factory=lambda sid: FakeRuntime(session_id=sid, agent_loop=self),
        )
        SessionStorage().append(str(self.root), context.session_id, Message(role="user", content="permission test"))
        await self.executor.execute(
            FakeRequestContext(metadata={"iac_code": {"cwd": str(self.root)}}), FakeEventQueue()
        )
        self.pending = next(iter(self.executor._permission_input_registry._pending.values()))
        self.control = self.service.get_for_context("ctx-1")
        assert self.control is not None
        checkpoint = self.pending.checkpoint_store.load(self.pending.boundary_id)
        assert checkpoint["permissionClass"] == "normal"
        assert self.pending.continuation is not None
        assert self.control.local_input_continuation_ready()
        assert not self.control.input_handoff_ready()

    async def suspend(self) -> None:
        self.pending.checkpoint_store.transaction(
            self.pending.boundary_id,
            lambda value: {**value, "residentDeadlineAt": format_utc(utc_now() - timedelta(seconds=1))},
        )
        assert await self.executor._permission_wait_coordinator.suspend_now(self.pending.boundary_id)
        assert self.pending.continuation is None
        assert not self.executor._permission_input_registry._pending
        assert self.pending.checkpoint_store.load(self.pending.boundary_id)["phase"] == "SUSPENDED"
        await self.drain()

    async def drain(self) -> None:
        while self.control._background_tasks:
            await asyncio.gather(*tuple(self.control._background_tasks))
            await asyncio.sleep(0)

    async def set_owner_pid(self, monkeypatch: pytest.MonkeyPatch, owner_pid_kind: str) -> None:
        if self.control.phase == "paused":
            path = self.root / "a2a" / "execution-control" / "ctx-1.json"
            await wait_until(lambda: json.loads(path.read_text(encoding="utf-8"))["phase"] == "paused")
        else:
            await self.drain()
        path = self.root / "a2a" / "execution-control" / "ctx-1.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["serverInstanceId"] != self.recovering.server_instance_id
        if owner_pid_kind == "same":
            assert document["ownerPid"] == os.getpid()
        else:
            document["ownerPid"] = os.getpid() + 100000
            path.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr("iac_code.a2a.execution_control._pid_alive", lambda _pid: owner_pid_kind != "dead")

    async def reserve(self):
        return await self.recovering.reserve_recoverable_input_continuation(
            context_id="ctx-1", task_id="task-1", owner=""
        )

    async def activate(self, admission):
        return await self.recovering.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="",
            cwd=str(self.root),
            recoverable_input_admission=admission,
        )

    async def close(self) -> None:
        current = asyncio.current_task()
        assert current is not None
        for control in self.recovering._controls.values():
            await control.detach_task(current, execution_status="input-required")
        await self.recovering.close()
        await self.service.close()
        await self.store.stop_cleanup_loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_pid_kind", ["same", "other_alive", "dead"])
async def test_first_normal_permission_suspension_allows_cold_worker_takeover(tmp_path, monkeypatch, owner_pid_kind):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.suspend()
        await fixture.set_owner_pid(monkeypatch, owner_pid_kind)
        admission = await fixture.reserve()
        assert admission is not None, "A fully suspended Normal permission must publish a durable ownership handoff"
        recovered = await fixture.activate(admission)
        assert recovered.execution_id != fixture.control.execution_id
        assert recovered.task_id == "task-1"
        assert recovered.context_id == "ctx-1"
        assert fixture.recovering.get_for_context("ctx-1") is recovered
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_pid_kind", ["same", "other_alive"])
async def test_hot_normal_permission_continuation_rejects_other_worker(tmp_path, monkeypatch, owner_pid_kind):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.set_owner_pid(monkeypatch, owner_pid_kind)
        assert await fixture.reserve() is None
        with pytest.raises(ExecutionControlConflictError):
            await fixture.activate(None)
        assert fixture.pending.continuation is not None
        assert fixture.control.local_input_continuation_ready()
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_pid_kind", ["same", "other_alive", "dead"])
async def test_explicitly_paused_normal_permission_rejects_cold_takeover(tmp_path, monkeypatch, owner_pid_kind):
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.control.pause(
            task_id="task-1",
            expected_execution_id=fixture.control.execution_id,
            request_id="explicit-pause",
            connection_epoch=1,
            reason="disconnect",
            reconnect_timeout_seconds=3600,
        )
        await wait_until(lambda: fixture.control.phase == "paused")
        await fixture.set_owner_pid(monkeypatch, owner_pid_kind)
        assert await fixture.reserve() is None
        with pytest.raises(ExecutionControlConflictError):
            await fixture.activate(None)
    finally:
        await fixture.close()
