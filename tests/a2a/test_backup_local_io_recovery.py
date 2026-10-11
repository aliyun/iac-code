from __future__ import annotations

import asyncio
import json
import multiprocessing
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from a2a.types import TaskState, TaskStatusUpdateEvent
from openai import AsyncOpenAI

from iac_code.a2a.backup import PENDING_JOBS_DIRNAME, SessionBackupCoordinator, SessionBackupHandoffError
from iac_code.a2a.execution_control import ExecutionControlService, NaturalCompletionGenerationCarrier
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.agent_loop import AgentLoop
from iac_code.providers.manager import ProviderManager
from iac_code.providers.retry import RetryConfig
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry

from .fakes import FakeEventQueue, FakeRequestContext


class _DeliveredQueue(FakeEventQueue):
    def __init__(self, request):
        super().__init__()
        self.request = request

    async def enqueue_event(self, event):
        await super().enqueue_event(event)
        if isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            NaturalCompletionGenerationCarrier.mark_delivered(self.request)


class _NaturalTurnSDK:
    def __init__(self):
        self.requests = []

    async def handle(self, request):
        self.requests.append(json.loads(request.content))
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "已记住原需求"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=content.encode("utf-8"), headers={"content-type": "text/event-stream"})


class _LocalCommitFault:
    def __init__(self, phase, *, limit=1, on_failure=None):
        self.phase = phase
        self.failures = 0
        self.job = None
        self.limit = limit
        self.on_failure = on_failure

    def write(self, original, path, document, **kwargs):
        target = (
            self.phase in ("ack", "callback-completed")
            and path.parent.name == PENDING_JOBS_DIRNAME
            and document.get("boundary") == "natural_completion"
            and document.get("stagedGeneration") is not None
            and (self.phase == "ack" or document.get("callbackCompleted") is True)
        ) or (self.phase == "receipt" and path.parent.name == "receipts")
        if target and self.failures < self.limit:
            self.failures += 1
            self.job = dict(document)
            if self.on_failure is not None:
                self.on_failure(path, document)
            raise OSError("single exact local commit failure")
        return original(path, document, **kwargs)

    def unlink(self, original, path, **kwargs):
        if self.phase == "unlink" and path.parent.name == PENDING_JOBS_DIRNAME and self.failures < self.limit:
            self.failures += 1
            self.job = json.loads(path.read_text(encoding="utf-8"))
            raise OSError("single exact local unlink failure")
        return original(path, **kwargs)

    def install(self, monkeypatch):
        from iac_code.a2a import backup as backup_module

        original_write = backup_module.atomic_write_json
        original_unlink = Path.unlink
        monkeypatch.setattr(
            backup_module, "atomic_write_json", lambda path, doc, **kw: self.write(original_write, path, doc, **kw)
        )
        monkeypatch.setattr(Path, "unlink", lambda path, **kw: self.unlink(original_unlink, path, **kw))


class _PublicNaturalTurn:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.cwd = root / "workspace"
        self.cwd.mkdir()
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(root / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(root / "shared"))
        self.storage = SessionStorage()
        self.backup = StagedSessionBackupService(root / "staging", self.storage, retry_delays=())
        self.coordinator = SessionBackupCoordinator(self.backup, state_root=root / "state")
        persistence = A2APersistenceStore(root / "a2a")
        self.store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
        self.controls = ExecutionControlService(
            persistence_root=persistence.root, backup_service=self.backup, backup_coordinator=self.coordinator
        )
        self.model = _NaturalTurnSDK()
        self.sdk = AsyncOpenAI(
            api_key="offline-test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.model.handle)),
            max_retries=0,
        )
        self.provider = ProviderManager(
            "gpt-4o",
            {"openai": "offline-test"},
            provider_key_override="openai",
            ignore_llm_source=True,
            provider_config_override={},
            retry_config=RetryConfig(max_retries=0),
        )
        self.original_sdk = self.provider._provider._client
        self.provider._provider._client = self.sdk
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", self.runtime)
        self.executor = IacCodeA2AExecutor(
            task_store=self.store, model="gpt-4o", backup_service=self.backup, execution_control_service=self.controls
        )

    def runtime(self, options):
        tools = ToolRegistry()
        loop = AgentLoop(
            self.provider,
            "Answer the user's request without tools.",
            tools,
            session_storage=self.storage,
            session_id=options.session_id,
            cwd=str(self.cwd),
            resume_messages=options.resume_messages,
        )
        return SimpleNamespace(agent_loop=loop, provider_manager=self.provider, tool_registry=tools)

    async def execute(self, task_id, text):
        request = FakeRequestContext(
            task_id=task_id,
            context_id="ctx-local-io",
            text=text,
            metadata={"iac_code": {"cwd": str(self.cwd)}},
        )
        queue = _DeliveredQueue(request)
        await asyncio.wait_for(self.executor.execute(request, queue), timeout=10)
        return queue

    async def close(self):
        await self.controls.close()
        await self.coordinator.aclose()
        await self.sdk.close()
        await self.original_sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ack", "receipt", "unlink"])
async def test_public_sdk_natural_handoff_repairs_one_exact_local_io_failure(monkeypatch, tmp_path, phase):
    case = _PublicNaturalTurn(tmp_path, monkeypatch)
    fault = _LocalCommitFault(phase)
    fault.install(monkeypatch)
    try:
        first = await case.execute("task-first", "请记住原标记 LOCAL-IO-原需求 和 10.246.0.0/16。")
        assert fault.failures == 1
        assert len(case.model.requests) == 1
        assert any(
            event.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
            for event in first.events
            if isinstance(event, TaskStatusUpdateEvent)
        )
        control = case.controls.get_for_context("ctx-local-io")
        assert control.natural_handoff_admits_replacement(), control.snapshot()
        assert not list((tmp_path / "staging" / PENDING_JOBS_DIRNAME).glob("*.json"))
        receipts = list((tmp_path / "state").glob("**/receipts/*.json"))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["jobId"] == fault.job["jobId"]
        assert receipt["commitId"] == (fault.job.get("captureCommitId") or fault.job["commitId"])
        snapshots = SessionBackupStagingWorker(tmp_path / "staging", tmp_path / "shared").scan_snapshots()
        sealed = next(item for item in snapshots if item.generation == receipt["backupGeneration"])
        sealed_bytes = (sealed.path / "session.jsonl").read_bytes()
        await case.execute("task-second", "继续上一轮，复述原需求。")
        assert len(case.model.requests) == 2
        assert "LOCAL-IO-原需求" in json.dumps(case.model.requests[-1]["messages"], ensure_ascii=False)
        assert (sealed.path / "session.jsonl").read_bytes() == sealed_bytes
        worker = SessionBackupStagingWorker(
            tmp_path / "staging", tmp_path / "shared", coordinator_state_root=tmp_path / "state"
        )
        await asyncio.to_thread(worker.run_once)
        assert not worker.scan_snapshots()
        assert not list((tmp_path / "staging" / PENDING_JOBS_DIRNAME).glob("*.json"))
    finally:
        await case.close()


class _LocalBoundary:
    def __init__(self, root, monkeypatch):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(root / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(root / "shared"))
        self.cwd = str(root / "workspace")
        self.storage = SessionStorage()
        self.session_dir = self.storage.ensure_v2_session_dir_for_new_session(self.cwd, "session-local")
        (self.session_dir / "session.jsonl").write_text("sealed old boundary\n", encoding="utf-8")
        self.service = StagedSessionBackupService(root / "staging", self.storage, retry_delays=())
        self.service.initialize_session(self.cwd, "session-local")
        self.coordinator = SessionBackupCoordinator(self.service, state_root=root / "state")
        self.root = root
        self.callbacks = []

    async def callback(self, generation, commit):
        self.callbacks.append((generation, commit))

    async def register(self, *, callback=None, action=None):
        return await self.coordinator.register_boundary(
            cwd=self.cwd,
            session_id="session-local",
            context_id="ctx-local",
            execution_id="exec-local",
            boundary="natural_completion",
            reason=BackupReason.TERMINAL,
            on_staged=callback,
            staged_action=action,
        )

    def documents(self):
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "staging" / PENDING_JOBS_DIRNAME).glob("*.json")
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["callback-completed", "receipt", "unlink"])
async def test_local_commit_repair_never_replays_successful_callback(monkeypatch, tmp_path, phase):
    case = _LocalBoundary(tmp_path, monkeypatch)
    fault = _LocalCommitFault(phase)
    fault.install(monkeypatch)
    try:
        handoff = await case.register(callback=case.callback)
        assert fault.failures == 1
        assert case.callbacks == [(handoff.snapshot_generation, handoff.snapshot_commit_id)]
        assert case.documents() == []
        receipts = list((tmp_path / "state").glob("**/receipts/*.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text(encoding="utf-8"))["commitId"] == handoff.snapshot_commit_id
    finally:
        await case.coordinator.aclose()


@pytest.mark.asyncio
async def test_business_callback_oserror_is_not_a_local_commit_retry(monkeypatch, tmp_path):
    case = _LocalBoundary(tmp_path, monkeypatch)

    async def failed_callback(generation, commit):
        case.callbacks.append((generation, commit))
        raise OSError("business callback side effect outcome is unknown")

    try:
        with pytest.raises(SessionBackupHandoffError):
            await case.register(callback=failed_callback)
        assert len(case.callbacks) == 1
        pending = case.documents()
        assert len(pending) == 1 and pending[0]["callbackCompleted"] is False
        assert not list((tmp_path / "state").glob("**/receipts/*.json"))
    finally:
        await case.coordinator.aclose()


@pytest.mark.asyncio
async def test_durable_callback_resolver_oserror_is_not_replayed_after_ack_repair(monkeypatch, tmp_path):
    case = _LocalBoundary(tmp_path, monkeypatch)

    async def resolver(action, generation, commit):
        assert action == {"kind": "original-action"}
        case.callbacks.append((generation, commit))
        raise OSError("business resolver outcome is unknown")

    case.coordinator.set_staged_action_resolver(resolver)
    fault = _LocalCommitFault("ack", on_failure=lambda *_args: case.coordinator._staged_callbacks.clear())
    fault.install(monkeypatch)
    try:
        with pytest.raises(SessionBackupHandoffError):
            await case.register(callback=case.callback, action={"kind": "original-action"})
        assert fault.failures == 1 and len(case.callbacks) == 1
        pending = case.documents()
        assert len(pending) == 1 and pending[0]["callbackCompleted"] is False
        assert not list((tmp_path / "state").glob("**/receipts/*.json"))
    finally:
        await case.coordinator.aclose()


@pytest.mark.asyncio
async def test_initial_job_creation_oserror_never_starts_a_local_retry(monkeypatch, tmp_path):
    from iac_code.a2a import backup as backup_module

    case = _LocalBoundary(tmp_path, monkeypatch)
    original_write = backup_module.atomic_write_json
    failures = []

    def fail_new_job(path, document, **kwargs):
        if path.parent.name == PENDING_JOBS_DIRNAME:
            failures.append(document)
            raise OSError("new job has no captured proof")
        return original_write(path, document, **kwargs)

    monkeypatch.setattr(backup_module, "atomic_write_json", fail_new_job)
    try:
        with pytest.raises(SessionBackupHandoffError):
            await case.register()
        assert len(failures) == 1 and failures[0]["captureStarted"] is False
        assert case.documents() == []
        assert not SessionBackupStagingWorker(tmp_path / "staging", tmp_path / "shared").scan_snapshots()
    finally:
        await case.coordinator.aclose()


@pytest.mark.asyncio
async def test_local_commit_repair_is_bounded_when_ack_io_keeps_failing(monkeypatch, tmp_path):
    case = _LocalBoundary(tmp_path, monkeypatch)
    fault = _LocalCommitFault("ack", limit=100)
    fault.install(monkeypatch)
    try:
        with pytest.raises(SessionBackupHandoffError):
            await case.register()
        assert fault.failures == 2
        pending = case.documents()
        assert len(pending) == 1 and pending[0]["stagedGeneration"] is None
        worker = SessionBackupStagingWorker(
            tmp_path / "staging", tmp_path / "shared", coordinator_state_root=tmp_path / "state"
        )
        assert await asyncio.to_thread(worker.run_once) == 1
        assert len(worker.scan_snapshots()) == 1
        assert case.documents() == pending
    finally:
        await case.coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["job-identity", "wrong-commit", "wrong-generation", "missing-callback"])
async def test_local_commit_repair_rejects_unproved_original_boundary(monkeypatch, tmp_path, change):
    from iac_code.a2a import backup as backup_module

    case = _LocalBoundary(tmp_path, monkeypatch)
    original_write = backup_module.atomic_write_json

    def change_proof(path, document):
        if change == "job-identity":
            current = json.loads(path.read_text(encoding="utf-8"))
            current["executionId"] = "other-execution"
            original_write(path, current, durable=True)
        elif change in ("wrong-commit", "wrong-generation"):
            worker = SessionBackupStagingWorker(tmp_path / "staging", tmp_path / "shared")
            snapshot = worker.scan_snapshots()[0]
            marker = case.service._read_state(snapshot.path, session_id="session-local", shared=True)
            changed = (
                replace(marker, commit_id="other-commit")
                if change == "wrong-commit"
                else replace(marker, generation=marker.generation + 1)
            )
            case.service._write_state(snapshot.path, changed)
            case.service._write_state(case.session_dir, changed)
        else:
            # Losing an uncompleted process-local callback without a durable
            # recovery action cannot turn a local ACK into a successful boundary.
            case.coordinator._staged_callbacks.clear()

    fault = _LocalCommitFault("ack", on_failure=change_proof)
    fault.install(monkeypatch)
    try:
        with pytest.raises(SessionBackupHandoffError):
            await case.register(callback=case.callback if change == "missing-callback" else None)
        assert case.callbacks == []
        assert len(case.documents()) == 1
        assert not list((tmp_path / "state").glob("**/receipts/*.json"))
    finally:
        await case.coordinator.aclose()


class _PublisherProcess:
    @staticmethod
    def publish(staging, shared, state, snapshot, ready, start, results):
        worker = SessionBackupStagingWorker(staging, shared, coordinator_state_root=state)
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError("publisher barrier timeout")
        worker.publish_snapshot(snapshot)
        results.put("published")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_write", [1, 2], ids=["initial-ack", "repair-ack"])
async def test_cancel_during_local_ack_repair_drains_original_callback_and_receipt(monkeypatch, tmp_path, cancel_write):
    from iac_code.a2a import backup as backup_module

    case = _LocalBoundary(tmp_path, monkeypatch)
    original_write = backup_module.atomic_write_json
    entered, release = threading.Event(), threading.Event()
    writes = []

    def fail_then_block_repair(path, document, **kwargs):
        if (
            path.parent.name == PENDING_JOBS_DIRNAME
            and document.get("stagedGeneration") is not None
            and document.get("callbackCompleted") is False
        ):
            writes.append(dict(document))
            if len(writes) == cancel_write:
                entered.set()
                assert release.wait(10)
            if len(writes) == 1:
                raise OSError("single exact ACK commit failure")
        return original_write(path, document, **kwargs)

    monkeypatch.setattr(backup_module, "atomic_write_json", fail_then_block_repair)
    task = asyncio.create_task(case.register(callback=case.callback))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert case.documents() == []
        assert len(writes) == 2
        receipts = list((tmp_path / "state").glob("**/receipts/*.json"))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["jobId"] == writes[0]["jobId"]
        assert receipt["commitId"] == writes[0]["stagedCommitId"]
        assert case.callbacks == [(receipt["backupGeneration"], receipt["commitId"])]
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        await case.coordinator.aclose()


@pytest.mark.asyncio
async def test_public_sdk_local_io_repair_finishes_before_shared_copy_and_two_publishers(monkeypatch, tmp_path):
    case = _PublicNaturalTurn(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original_mirror = SessionBackupService._mirror
    threads = []
    snapshots = []
    errors = []

    def blocked_mirror(service, source, destination, *args, **kwargs):
        if Path(destination).is_relative_to(tmp_path / "shared"):
            entered.set()
            assert release.wait(15)
        return original_mirror(service, source, destination, *args, **kwargs)

    def publish(worker, snapshot):
        try:
            worker.publish_snapshot(snapshot)
        except BaseException as exc:
            errors.append(exc)

    def hold_actual_publication(_path, document):
        worker = SessionBackupStagingWorker(
            tmp_path / "staging", tmp_path / "shared", coordinator_state_root=tmp_path / "state"
        )
        snapshot = next(item for item in worker.scan_snapshots() if item.generation == document["stagedGeneration"])
        snapshots.append((snapshot, (snapshot.path / "session.jsonl").read_bytes()))
        thread = threading.Thread(target=publish, args=(worker, snapshot))
        threads.append(thread)
        thread.start()
        assert entered.wait(5)

    monkeypatch.setattr(SessionBackupService, "_mirror", blocked_mirror)
    fault = _LocalCommitFault("ack", on_failure=hold_actual_publication)
    fault.install(monkeypatch)
    try:
        await case.execute("task-barrier", "原用户的 sealed-长共享锁 请求")
        assert case.controls.get_for_context("ctx-local-io").natural_handoff_admits_replacement()
        assert entered.is_set() and not release.is_set() and threads[0].is_alive()
        assert not list((tmp_path / "staging" / PENDING_JOBS_DIRNAME).glob("*.json"))
        snapshot, sealed_bytes = snapshots[0]
        assert (snapshot.path / "session.jsonl").read_bytes() == sealed_bytes
        # Two fresh processes race the same exact receipt/retirement after
        # the first publisher is released; none owns in-memory retry flags.
        context = multiprocessing.get_context("spawn")
        ready, results = context.Queue(), context.Queue()
        start = context.Event()
        processes = [
            context.Process(
                target=_PublisherProcess.publish,
                args=(
                    str(tmp_path / "staging"),
                    str(tmp_path / "shared"),
                    str(tmp_path / "state"),
                    snapshot,
                    ready,
                    start,
                    results,
                ),
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                assert await asyncio.to_thread(ready.get, True, 15)
            start.set()
            release.set()
            for process in processes:
                await asyncio.to_thread(process.join, 15)
                assert process.exitcode == 0
            assert [await asyncio.to_thread(results.get, True, 2) for _ in processes] == ["published", "published"]
            threads[0].join(5)
            assert errors == []
            assert not snapshot.path.exists()
            shared = tmp_path / "shared" / "projects" / snapshot.project / snapshot.session_id
            assert (shared / "session.jsonl").read_bytes() == sealed_bytes
        finally:
            start.set()
            release.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            ready.close()
            results.close()
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        await case.close()
