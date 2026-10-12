import json

import pytest

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.pipeline.engine.user_input import PipelineInputAcceptance, PipelineUserInput
from iac_code.services.session_storage import PIPELINE_USER_HISTORY_KEY, PipelineUserHistoryRecord, SessionStorage

from ..pipeline.engine.test_pipeline_runner import _build_two_step_runner


class _NumericConfirmationHistoryCase:
    def __init__(self, tmp_path):
        self.cwd = str(tmp_path)
        self.storage = SessionStorage(tmp_path / "projects")
        self.storage.ensure_v2_session_dir_for_new_session(self.cwd, "test")
        self.runner = _build_two_step_runner(tmp_path, storage=self.storage, auto_advance_first=False, surface="a2a")
        self.runner._loaded.steps[0].ui_mode = "deployment_confirmation"
        self.runner._step_executor.execute = self.execute_step
        self.consumed = []
        self.persist_calls = 0
        self.original = " 1 "
        self.record = PipelineUserHistoryRecord("confirm-1", "ctx-1", "task-1", self.original)
        self.receipt = PipelineInputAcceptance(self.persist)
        self.user_input = PipelineUserInput(self.original, self.original, False, self.receipt)

    def persist(self):
        self.persist_calls += 1
        return self.storage.append_user_input_once(self.cwd, "test", self.record)

    async def execute_step(self, step, context, session_id, user_message=None, **kwargs):
        self.consumed.append(user_message)
        yield StepResult(
            step_id=step.step_id,
            status=StepStatus.COMPLETED,
            conclusion={
                "status": "awaiting_confirmation",
                "user_prompt": "Confirm deployment",
                "options": [{"id": "deploy", "action": "confirm", "label": "Deploy"}],
            },
        )

    async def wait_for_confirmation(self):
        events = [event async for event in self.runner.run("original request")]
        assert any(
            isinstance(event, PipelineEvent) and event.type == PipelineEventType.USER_INPUT_REQUIRED for event in events
        )

    async def resume(self):
        return [event async for event in self.runner.resume(self.user_input)]

    def users(self):
        return [message for message in self.storage.load(self.cwd, "test") if message.role == "user"]


@pytest.mark.asyncio
async def test_numeric_confirmation_preserves_original_history_and_same_receipt_exactly_once(tmp_path):
    case = _NumericConfirmationHistoryCase(tmp_path)
    await case.wait_for_confirmation()

    await case.resume()
    await case.resume()

    assert [json.loads(value) for value in case.consumed[1:]] == [{"action": "confirm"}, {"action": "confirm"}]
    assert case.receipt.accepted
    assert case.persist_calls == 1
    users = case.users()
    assert [message.content for message in users] == [" 1 "]
    assert users[0].metadata[PIPELINE_USER_HISTORY_KEY] == {
        "message_id": "confirm-1",
        "context_id": "ctx-1",
        "task_id": "task-1",
    }


@pytest.mark.asyncio
async def test_numeric_confirmation_history_fsync_failure_blocks_step_until_durable_retry(tmp_path, monkeypatch):
    case = _NumericConfirmationHistoryCase(tmp_path)
    await case.wait_for_confirmation()
    waiting = dict(case.runner._waiting_input_started_at)
    options = dict(case.runner._waiting_input_options_by_step)

    def fail_parent_sync(_path):
        raise OSError("offline history fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(case.storage, "_sync_history_parent", fail_parent_sync)
        with pytest.raises(OSError, match="offline history fsync failure"):
            await case.resume()

    assert case.consumed == ["original request"]
    assert not case.receipt.accepted
    assert case.runner._waiting_input_started_at == waiting
    assert case.runner._waiting_input_options_by_step == options

    await case.resume()
    assert case.receipt.accepted
    assert case.persist_calls == 2
    assert json.loads(case.consumed[-1]) == {"action": "confirm"}
    assert [message.content for message in case.users()] == [" 1 "]
