"""Sub Pipeline suspension ends delivery before its independent runtime teardown."""

import asyncio

import pytest

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.services.permission_wait import PermissionWaitPolicy
from iac_code.services.session_backup import SessionBackupService
from iac_code.types.stream_events import (
    PermissionRequestEvent,
    PermissionWaitOutcome,
    PermissionWaitSuspended,
    SubPipelineStreamEvent,
)

from .fakes import FakeEventQueue, FakeRequestContext
from .test_execution_control_regressions import wait_until
from .test_execution_control_review_regressions import make_executor
from .test_permission_execution_control import continuation_frame
from .test_pipeline_executor import FakePipeline, _fake_runtime


class SuspendedCandidatePipeline(FakePipeline):
    def __init__(self, root):
        super().__init__([], session_dir=root / "sidecar")
        self.response = asyncio.get_running_loop().create_future()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def run(self, _prompt):
        yield SubPipelineStreamEvent(
            "candidate-0",
            0,
            PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=self.response,
                continuation_frame=continuation_frame(),
                audit_context={"transcript_id": "candidate-0"},
            ),
        )
        assert await self.response is PermissionWaitOutcome.SUSPEND
        raise PermissionWaitSuspended("candidate suspended")

    async def close_runtime(self, _context_id):
        self.close_started.set()
        await self.release_close.wait()


@pytest.mark.asyncio
async def test_sub_pipeline_sse_end_does_not_wait_for_runtime_close(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
    pipeline = SuspendedCandidatePipeline(tmp_path)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *_a, **_kw: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda _options: _fake_runtime())
    backup = SessionBackupService()
    _executor, store, service = make_executor(tmp_path, backup)
    monkeypatch.setattr(store, "discard_context_runtime_for_input_handoff", pipeline.close_runtime)
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="test",
        backup_service=backup,
        execution_control_service=service,
        permission_wait_policy=PermissionWaitPolicy(resident_timeout_seconds=0.01, timeout_grace_seconds=0),
    )
    execution = asyncio.create_task(
        executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())
    )
    try:
        await asyncio.wait_for(pipeline.close_started.wait(), timeout=5)
        done, _pending = await asyncio.wait({execution}, timeout=0.25)
        assert execution in done, "SSE must end while the independent close is still blocked"
        control = service.get_for_context("ctx-1")
        assert not control.input_handoff_ready()
        pipeline.release_close.set()
        await wait_until(control.input_handoff_ready)
        assert control.snapshot()["inputHandoff"]["phase"] == "COMMITTED"
    finally:
        pipeline.release_close.set()
        await asyncio.gather(execution, return_exceptions=True)
        await service.close()
        await store.stop_cleanup_loop()
