import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from a2a.types import Message, Role
from openai import AsyncOpenAI

from iac_code.a2a.events import make_text_part
from iac_code.a2a.execution_control import ExecutionControlService
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.providers.manager import ProviderManager
from iac_code.providers.retry import RetryConfig
from iac_code.services.session_backup_staging import StagedSessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry

from .fakes import FakeEventQueue, FakeRequestContext
from .test_pipeline_continuation import _CompletedPreparationModelBody, _WaitingFirstModel


class _HistoryModel(_WaitingFirstModel):
    async def handle(self, request):
        body = json.loads(request.content)
        if self.fault == "judge_failure" and not body.get("stream"):
            self.requests.append(body)
            self.calls += 1
            return httpx.Response(
                200,
                json={
                    "id": "judge-1",
                    "object": "chat.completion",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "not-a-verdict"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        return await super().handle(request)


class _SelectionBody(_CompletedPreparationModelBody):
    async def __aiter__(self):
        async for chunk in super().__aiter__():
            if chunk.startswith(b"data: {"):
                event = json.loads(chunk[6:])
                calls = event["choices"][0]["delta"].get("tool_calls")
                if calls:
                    calls[0]["function"]["arguments"] = json.dumps(
                        {"conclusion": {"options": [{"label": "option-one"}], "user_prompt": "select one"}}
                    )
                chunk = ("data: " + json.dumps(event) + "\n\n").encode()
            yield chunk


class _WaitingSelectionModel(_HistoryModel):
    async def handle(self, request):
        body = json.loads(request.content)
        if self.calls == 0:
            self.requests.append(body)
            self.calls += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_SelectionBody())
        return await super().handle(request)


class _PublicHistoryCase:
    first = "history-original-marker: 请保留第一条真实用户输入。"
    second = "请原样复述第一条消息中的标记，再继续方案。"

    def __init__(self, tmp_path, monkeypatch, *, waiting_input=False):
        self.cwd = str(tmp_path / "workspace")
        Path(self.cwd).mkdir()
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
        self.config = tmp_path / "pipeline"
        self.config.mkdir()
        (self.config / "model.md").write_text("INTERNAL-STEP-PROMPT: plan safely.", encoding="utf-8")
        step = {"id": "plan", "conclusion_field": "plan", "forward": None, "prompt": "model.md"}
        if waiting_input:
            step.update(auto_advance=False, ui_mode="candidate_selection")
        if waiting_input == "structured":
            step.update(
                ui_mode="deployment_confirmation",
                hooks_file="validation.py",
                config={"confirmation_accepts_parameter_overrides": True},
            )
            (self.config / "validation.py").write_text(
                'def validate_structured_confirmation(**kwargs):\n    return "invalid parameters"\n', encoding="utf-8"
            )
        (self.config / "pipeline.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "model-history",
                    "context_dependencies": {"plan": []},
                    "steps": [step],
                }
            ),
            encoding="utf-8",
        )
        self.storage = SessionStorage()
        self.storage.ensure_v2_session_dir_for_new_session(self.cwd, "session-1")
        self.backup = StagedSessionBackupService(tmp_path / "staging", self.storage, retry_delays=())
        self.backup.initialize_session(self.cwd, "session-1")
        self.persistence = A2APersistenceStore(tmp_path / "a2a")
        self.persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=self.cwd))
        self.store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=self.persistence)
        self.control_service = ExecutionControlService(
            persistence_root=self.persistence.root, backup_service=self.backup
        )
        self.executor = IacCodeA2AExecutor(
            task_store=self.store,
            model="gpt-4o",
            backup_service=self.backup,
            execution_control_service=self.control_service,
        )
        self.model = _WaitingSelectionModel() if waiting_input else _HistoryModel()
        self.clients = []
        self.runners = []
        self.tasks = []
        self.history_damage = None
        self.history_target = tmp_path / "unrelated.jsonl"
        monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor._create_pipeline", self.create_runner)
        monkeypatch.setattr(
            "iac_code.a2a.pipeline_executor.discover_pipelines",
            lambda: {"selling": self.config, "model-history": self.config},
        )

    def create_runner(self, **kwargs):
        provider = ProviderManager(
            "gpt-4o",
            {"openai": "offline-test"},
            provider_key_override="openai",
            ignore_llm_source=True,
            provider_config_override={},
            retry_config=RetryConfig(max_retries=0),
        )
        self.clients.append(provider._provider._client)
        sdk = AsyncOpenAI(
            api_key="offline-test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.model.handle)),
            max_retries=0,
        )
        self.clients.append(sdk)
        provider._provider._client = sdk
        runner = PipelineRunner(
            self.config,
            provider,
            ToolRegistry(),
            self.storage,
            "session-1",
            cwd=self.cwd,
            resume_from_sidecar=kwargs.get("resume_from_sidecar", True),
            backup_service=self.backup,
            surface="a2a",
        )
        self.runners.append(runner)
        if self.history_damage is not None:
            path = self.storage.session_path(self.cwd, "session-1")
            self.history_target.write_text("unrelated data\n", encoding="utf-8")
            if self.history_damage == "symlink":
                path.symlink_to(self.history_target)
            else:
                path.mkdir()
        return runner

    async def start(self, text, message_id, task_id="task-old", *, wait_model=True):
        ctx = await self.store.get_or_create_context(
            context_id="ctx-1",
            cwd=self.cwd,
            runtime_factory=lambda _sid: SimpleNamespace(provider_manager=None, tool_registry=ToolRegistry()),
        )
        self.context = ctx
        if ctx.runtime is None:
            ctx.runtime = SimpleNamespace(provider_manager=None, tool_registry=ToolRegistry())
        request = FakeRequestContext(
            task_id=task_id,
            context_id="ctx-1",
            text=text,
            metadata={"iac_code": {"cwd": self.cwd, "run_mode": "pipeline"}},
        )
        request.message = Message(
            message_id=message_id,
            role=Role.ROLE_USER,
            parts=[make_text_part(text)],
            context_id="ctx-1",
            task_id=task_id,
            metadata=request.metadata,
        )
        queue = FakeEventQueue()
        self.model.entered.clear()
        active = asyncio.create_task(self.executor.execute(request, queue))
        self.tasks.append(active)
        if not wait_model:
            await asyncio.wait_for(asyncio.shield(active), timeout=10)
            return queue
        entered = asyncio.create_task(self.model.entered.wait())
        try:
            done, _ = await asyncio.wait((active, entered), timeout=10, return_when=asyncio.FIRST_COMPLETED)
            assert entered in done, queue.events
        finally:
            entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)
        return active

    def users(self):
        return [message.content for message in self.storage.load(self.cwd, "session-1") if message.role == "user"]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.control_service.close()
        for client in self.clients:
            await client.close()


@pytest.mark.asyncio
async def test_public_pipeline_model_request_preserves_only_original_sdk_user_text(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        await case.start(case.first, "first-1")
        assert case.model.calls == 1
        assert case.runners[0]._execution["active_attempt_id"] == "att_0001"
        assert case.users() == [case.first]
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_public_canceled_successor_preserves_both_sdk_queries_before_model(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        active = await case.start(case.first, "first-1")
        control = case.control_service.get_for_context("ctx-1")
        await control.terminate(
            execution_id=control.execution_id,
            request_id="stop-1",
            connection_epoch=1,
            reason="stream_terminal_cleanup",
        )
        for _ in range(500):
            if control.release_ready:
                break
            await asyncio.sleep(0.01)
        assert control.release_ready and active.done()
        assert (await case.store.get_task_record("task-old")).state == "canceled"
        old_transcript = case.runners[0]._transcript_storage.session_path(case.cwd, "transcript_att_0001")
        sealed = old_transcript.read_bytes()
        successor = await case.executor.resolve_omitted_pipeline_task_id(
            context_id="ctx-1", cwd=case.cwd, invocation_id="next-1"
        )
        await case.start(case.second, "next-1", successor)
        assert case.model.calls == 3  # old model, real interrupt judge, successor model
        assert old_transcript.read_bytes() == sealed
        assert case.users() == [case.first, case.second]
        users = [row["content"] for row in case.model.requests[-1]["messages"] if row["role"] == "user"]
        assert users == [case.first, case.second]
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_parent_rollback_history_failure_preserves_active_candidate_work(tmp_path, monkeypatch):
    from iac_code.pipeline.engine.interrupt import InterruptVerdict
    from iac_code.pipeline.engine.user_input import PipelineInputAcceptance, PipelineUserInput

    case = _PublicHistoryCase(tmp_path, monkeypatch)
    candidate_release = asyncio.Event()
    candidate = asyncio.create_task(candidate_release.wait())
    try:
        await case.start(case.first, "first-1")
        runner = case.runners[0]
        runner._active_candidates[0] = {"task": candidate}
        runner._pending_candidate_restarts[0] = {"step_id": "plan"}
        execution = dict(runner._execution)

        def fail_history():
            raise OSError("offline history fsync failure")

        receipt = PipelineInputAcceptance(fail_history)
        receipt.qualify_interrupt()
        user_input = PipelineUserInput(case.second, case.second, False, acceptance=receipt)
        with pytest.raises(OSError):
            runner.apply_hard_interrupt(
                InterruptVerdict(action="hard_interrupt", reason="change plan", rollback_target="plan"),
                source_input=user_input,
            )
        candidate_release.set()
        assert await candidate is True
        assert runner._pending_candidate_restarts == {0: {"step_id": "plan"}}
        assert runner._execution == execution
        assert not receipt.accepted
        assert case.model.calls == 1
        assert case.users() == [case.first]
    finally:
        candidate.cancel()
        await asyncio.gather(candidate, return_exceptions=True)
        await case.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ['{"selected_candidate_index": 0}', '{"selected_candidate_index": 99}'])
async def test_public_cold_waiting_input_history_requires_real_selection_validation(tmp_path, monkeypatch, choice):
    case = _PublicHistoryCase(tmp_path, monkeypatch, waiting_input=True)
    try:
        await case.start(case.first, "first-1", wait_model=False)
        assert (await case.store.get_task_record("task-old")).state == "input-required"
        assert case.model.calls == 1
        before = case.storage.session_path(case.cwd, "session-1").read_bytes()
        if "99" in choice:
            await case.start(choice, "invalid-1", wait_model=False)
            assert case.model.calls == 1
            assert case.users() == [case.first]
            assert case.storage.session_path(case.cwd, "session-1").read_bytes() == before
            assert (await case.store.get_task_record("task-old")).state == "input-required"
        else:
            await case.start(choice, "selected-1")
            assert case.model.calls == 2
            assert case.users() == [case.first, choice]
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_public_history_failure_prevents_new_attempt_and_model_consumption(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:

        def fail_sync(_fd):
            raise OSError("offline history fsync failure")

        monkeypatch.setattr(SessionStorage, "_sync_history_parent", staticmethod(fail_sync))
        await case.start(case.first, "first-1", wait_model=False)
        assert case.model.calls == 0
        assert not case.runners[0]._execution.get("active_attempt_id")
        assert (await case.store.get_task_record("task-old")).state == "failed"
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_public_active_continue_input_records_only_after_real_judge_acceptance(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        await case.start(case.first, "first-1")
        await case.start(case.second, "active-1", wait_model=False)
        assert case.model.calls == 2  # first model plus the actual interrupt judge
        assert case.users() == [case.first, case.second]
        assert case.runners[0]._execution["active_attempt_id"] == "att_0001"
    finally:
        await case.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["answered", "stale", "inflight", "publish_failed", "history_failed"])
async def test_active_question_history_linearizes_before_actual_answer_consumption(tmp_path, monkeypatch, route):
    from unittest.mock import AsyncMock

    from iac_code.a2a.pipeline_executor import _PendingAskUserQuestion
    from iac_code.types.stream_events import AskUserQuestionEvent

    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        await case.start(case.first, "first-1")
        runtime = case.context.runtime
        future = asyncio.get_running_loop().create_future()
        question = AskUserQuestionEvent(
            tool_use_id="ask-1",
            question="choose",
            options=[{"id": "one", "label": "option-one"}],
            response_future=future,
        )
        runtime.pending_question = _PendingAskUserQuestion(
            question,
            {
                "eventType": "input_required",
                "scope": "step",
                "input": {"inputId": "ask-1"},
                "step": {"id": "plan", "runId": "plan-1"},
            },
        )
        if route == "stale":
            future.set_result({"free_text": "prior"})
        if route == "inflight":
            runtime.question_answer_in_flight.set()
            runtime.question_answer_settled.set()
        if route == "publish_failed":
            monkeypatch.setattr(runtime.publisher, "publish_manual", AsyncMock(return_value=None))
        if route == "history_failed":

            def fail_parent(_path):
                raise OSError("offline history parent fsync failure")

            monkeypatch.setattr(SessionStorage, "_sync_history_parent", staticmethod(fail_parent))
        await case.start(case.second, "answer-1", wait_model=False)
        if route == "answered":
            assert future.done()
            assert future.result()["free_text"] == case.second
            assert case.users() == [case.first, case.second]
        else:
            # Failure to publish the question answer retains that future. The
            # original route may still accept the input as a separately judged
            # ordinary interrupt; only that real acceptance can export it.
            assert case.users() == (
                [case.first, case.second] if route in {"history_failed", "publish_failed"} else [case.first]
            )
            if route != "stale":
                assert not future.done()
        assert case.model.calls == (2 if route == "publish_failed" else 1)
    finally:
        await case.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["supplement", "dropped", "judge_failure"])
async def test_active_interrupt_history_requires_actual_consumption(tmp_path, monkeypatch, mode):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        await case.start(case.first, "first-1")
        if mode in {"supplement", "dropped"}:
            case.model.fault = "supplement"
            if mode == "dropped":
                case.runners[0]._step_executor.current_agent_loop._accepting_injected_user_messages = False
        else:
            case.model.fault = "judge_failure"
        await case.start(case.second, "active-1", wait_model=False)
        assert case.users() == ([case.first, case.second] if mode == "supplement" else [case.first])
        assert case.model.calls >= 2
        if mode == "supplement":
            assert case.runners[0]._step_executor.current_agent_loop._pending_injections
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_completed_exact_task_observation_with_text_does_not_export_new_input(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    case.model = _WaitingSelectionModel()
    try:
        await case.start(case.first, "first-1", wait_model=False)
        assert (await case.store.get_task_record("task-old")).state == "completed"
        before = case.storage.session_path(case.cwd, "session-1").read_bytes()
        from a2a.utils.errors import InvalidParamsError

        with pytest.raises(InvalidParamsError, match="empty input"):
            await case.start("", "replay-1", wait_model=False)
        await case.start(case.second, "observe-1", wait_model=False)
        assert case.model.calls == 1
        assert case.users() == [case.first]
        assert case.storage.session_path(case.cwd, "session-1").read_bytes() == before
    finally:
        await case.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_exported_root_history_never_becomes_missing_attempt_resume_input(tmp_path, monkeypatch, malformed):
    from iac_code.agent.message import Message as AgentMessage
    from iac_code.services.session_storage import PipelineUserHistoryRecord

    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        case.storage.append(case.cwd, "session-1", AgentMessage(role="user", content="ordinary legacy transcript"))
        case.storage.append_user_input_once(
            case.cwd, "session-1", PipelineUserHistoryRecord("export-1", "ctx-1", "task-old", case.first)
        )
        if malformed:
            from iac_code.services.session_storage import PIPELINE_USER_HISTORY_KEY

            case.storage.append(
                case.cwd,
                "session-1",
                AgentMessage(role="user", content="invalid export", metadata={PIPELINE_USER_HISTORY_KEY: None}),
            )
        runner = case.create_runner(resume_from_sidecar=False)
        messages = runner._resume_messages_for_current_parent_step("plan")
        assert [message.content for message in messages] == ["ordinary legacy transcript"]
        assert case.model.calls == 0
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_canceled_attempt_missing_transcript_is_not_repaired_by_public_root_history(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        active = await case.start(case.first, "first-1")
        control = case.control_service.get_for_context("ctx-1")
        await control.terminate(
            execution_id=control.execution_id, request_id="stop-1", connection_epoch=1, reason="stream_terminal_cleanup"
        )
        for _ in range(500):
            if control.release_ready:
                break
            await asyncio.sleep(0.01)
        assert control.release_ready and active.done()
        transcript = case.runners[0]._transcript_storage.session_path(case.cwd, "transcript_att_0001")
        transcript.unlink()
        successor = await case.executor.resolve_omitted_pipeline_task_id(
            context_id="ctx-1", cwd=case.cwd, invocation_id="next-1"
        )
        await case.start(case.second, "next-1", successor, wait_model=False)
        assert case.model.calls == 1
        assert (await case.store.get_task_record(successor)).state == "failed"
        assert case.users() == [case.first]
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_cold_structured_confirmation_rejected_before_history_or_model(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch, waiting_input="structured")
    try:
        await case.start(case.first, "first-1", wait_model=False)
        assert (await case.store.get_task_record("task-old")).state == "input-required"
        before = case.storage.session_path(case.cwd, "session-1").read_bytes()
        await case.start('{"parameters":{"count":-1}}', "invalid-1", wait_model=False)
        assert case.model.calls == 1
        assert case.users() == [case.first]
        assert case.storage.session_path(case.cwd, "session-1").read_bytes() == before
        assert (await case.store.get_task_record("task-old")).state == "input-required"
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_real_question_image_queue_and_recovery_snapshot_survive_history_io_failure(tmp_path, monkeypatch):
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion
    from iac_code.agent.message import ImageBlock, TextBlock
    from iac_code.pipeline.engine.user_input import PipelineInputAcceptance, PipelineUserInput
    from iac_code.services.session_storage import PipelineUserHistoryRecord
    from iac_code.types.stream_events import AskUserQuestionEvent

    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        await case.start(case.first, "first-1")
        runtime = case.context.runtime
        future = asyncio.get_running_loop().create_future()
        question = AskUserQuestionEvent(tool_use_id="ask-1", question="choose", options=[], response_future=future)
        envelope = await runtime.publisher.publish_manual(
            "input_required",
            "step",
            status="input_required",
            data={
                "kind": "ask_user_question",
                "toolUseId": "ask-1",
                "inputId": "ask-1",
                "question": "choose",
                "options": [],
                "required": True,
            },
        )
        runtime.pending_question = _PendingAskUserQuestion(question, envelope)
        writer = PipelineUserHistoryRecord("image-1", "ctx-1", "task-old", case.second)
        receipt = PipelineInputAcceptance(lambda: case.storage.append_user_input_once(case.cwd, "session-1", writer))
        user_input = PipelineUserInput(
            [TextBlock(text=case.second), ImageBlock(media_type="image/png", data="aGVsbG8=")],
            case.second,
            True,
            receipt,
        )
        loop = case.runners[0]._step_executor.current_agent_loop
        before = list(loop._pending_injections)

        def fail_parent(_path):
            raise OSError("offline parent fsync failure")

        monkeypatch.setattr(SessionStorage, "_sync_history_parent", staticmethod(fail_parent))
        executor = IacCodeA2APipelineExecutor(
            task_store=case.store,
            model="gpt-4o",
            metrics=NoOpA2AMetrics(),
            artifact_store=None,
            push_notifier=None,
            permission_resolver=None,
            auto_approve_permissions=False,
            thinking_exposure_types=None,
            backup_service=case.backup,
        )
        with pytest.raises(OSError):
            await executor._route_pending_question_answer(runtime, user_input)
        assert list(loop._pending_injections) == before
        assert not future.done() and not receipt.accepted
        assert runtime.publisher.snapshot_store.load()["pendingInput"]["kind"] == "ask_user_question"
        assert case.model.calls == 1
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_partial_history_tail_stops_public_business_and_is_not_skipped_on_retry(tmp_path, monkeypatch):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        path = case.storage.session_path(case.cwd, "session-1")
        path.write_text('{"role":"user","content":"partial user row"}', encoding="utf-8")
        before = path.read_bytes()
        await case.start(case.first, "first-1", wait_model=False)
        assert case.model.calls == 0
        assert not case.runners[0]._execution.get("active_attempt_id")
        assert (await case.store.get_task_record("task-old")).state == "failed"
        assert path.read_bytes() == before
        await case.start(case.second, "next-1", "task-next", wait_model=False)
        assert case.model.calls == 0
        assert path.read_bytes() == before
    finally:
        await case.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["symlink", "directory"])
async def test_v2_history_entry_changed_after_runner_creation_fails_before_business(tmp_path, monkeypatch, damage):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    case.history_damage = damage
    try:
        await case.start(case.first, "first-1", wait_model=False)
        assert case.model.calls == 0
        assert not case.runners[0]._execution.get("active_attempt_id")
        # The existing backup guard cannot publish a safe terminal while
        # the root is unsafe; preserve its recoverable input-required state.
        assert (await case.store.get_task_record("task-old")).state == "input-required"
        assert case.history_target.read_text(encoding="utf-8") == "unrelated data\n"
    finally:
        await case.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["continue", "supplement"])
async def test_active_judge_cannot_accept_input_after_terminal_publication_started(tmp_path, monkeypatch, action):
    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        await case.start(case.first, "first-1")
        runtime = case.context.runtime
        case.model.fault = "supplement" if action == "supplement" else None
        case.model.before_judge_response = lambda: setattr(runtime, "terminal_publication_started", True)
        before = list(case.runners[0]._step_executor.current_agent_loop._pending_injections)
        await case.start(case.second, "late-1", wait_model=False)
        assert case.users() == [case.first]
        assert list(case.runners[0]._step_executor.current_agent_loop._pending_injections) == before
        assert case.model.calls == 2
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_failed_question_publication_itself_never_accepts_or_persists_answer(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion
    from iac_code.pipeline.engine.user_input import PipelineInputAcceptance, PipelineUserInput
    from iac_code.services.session_storage import PipelineUserHistoryRecord
    from iac_code.types.stream_events import AskUserQuestionEvent

    case = _PublicHistoryCase(tmp_path, monkeypatch)
    try:
        await case.start(case.first, "first-1")
        runtime = case.context.runtime
        future = asyncio.get_running_loop().create_future()
        question = AskUserQuestionEvent(tool_use_id="ask-1", question="choose", options=[], response_future=future)
        runtime.pending_question = _PendingAskUserQuestion(
            question, {"eventType": "input_required", "scope": "step", "input": {"inputId": "ask-1"}}
        )
        monkeypatch.setattr(runtime.publisher, "publish_manual", AsyncMock(return_value=None))
        record = PipelineUserHistoryRecord("answer-1", "ctx-1", "task-old", case.second)
        receipt = PipelineInputAcceptance(lambda: case.storage.append_user_input_once(case.cwd, "session-1", record))
        executor = IacCodeA2APipelineExecutor(
            task_store=case.store,
            model="gpt-4o",
            metrics=NoOpA2AMetrics(),
            artifact_store=None,
            push_notifier=None,
            permission_resolver=None,
            auto_approve_permissions=False,
            thinking_exposure_types=None,
            backup_service=case.backup,
        )
        result = await executor._route_pending_question_answer(
            runtime, PipelineUserInput(case.second, case.second, False, receipt)
        )
        assert result == "not_routed"
        assert not receipt.accepted and not future.done()
        assert case.users() == [case.first]
        assert case.model.calls == 1
    finally:
        await case.close()


@pytest.mark.asyncio
async def test_preexisting_duck_input_selects_and_consumes_original_text_without_history_carrier(tmp_path):
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore

    class Pipeline:
        sidecar_status = None

        async def run(self, user_input):
            assert type(user_input) is str
            yield user_input

    executor = object.__new__(IacCodeA2APipelineExecutor)
    original = "preexisting internal continuation input"
    selected = await executor._select_stream(
        Pipeline(),
        original,
        pipeline_input=SimpleNamespace(content=original, display_text=original, has_images=False),
        publisher=SimpleNamespace(
            journal=A2APipelineJournal(tmp_path / "pipeline"),
            snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
        ),
        task_id="task-1",
        context_id="ctx-1",
        fresh_pipeline_factory=Pipeline,
    )
    assert selected.accepts_user_input
    assert [event async for event in selected.stream] == [original]
