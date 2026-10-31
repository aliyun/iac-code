"""Transient decision failures preserve the original resident candidate card."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from iac_code.a2a.input_required import PermissionIdentityValidationError, PermissionInputRegistry, PermissionResponse
from iac_code.agent.message import Message, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
from iac_code.services.permission_wait import PermissionWaitCoordinator, PermissionWaitPolicy, canonical_digest
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import MessageStartEvent, PermissionRequestEvent, SubPipelineStreamEvent

from .test_permission_execution_control import continuation_frame
from .test_pipeline_stream import _publisher


class ResidentCandidatePermission:
    def __init__(self, root):
        self.root = root
        self.registry = PermissionInputRegistry()
        self.registry.set_permission_wait_coordinator(PermissionWaitCoordinator(PermissionWaitPolicy()))
        self.publisher, _queue = _publisher(root)
        self.publisher.permission_input_registry = self.registry
        self.publisher.permission_wait_cwd = str(root)
        self.publisher.permission_wait_session_id = "session-1"
        session = SessionStorage().ensure_v2_session_dir_for_new_session(str(root), "session-1")
        self.transcript = PipelineTranscriptStorage(session / "pipeline")
        self.future = asyncio.get_running_loop().create_future()

    async def open(self):
        tool = ToolUseBlock(id="tool-1", name="bash", input={"cmd": "pwd"})
        self.transcript.append(str(self.root), "candidate-0", Message(role="assistant", content=[tool]))
        await self.publisher.publish(
            SubPipelineStreamEvent(
                "candidate-0",
                0,
                PermissionRequestEvent(
                    tool_name="bash",
                    tool_input={"cmd": "pwd"},
                    tool_use_id="tool-1",
                    response_future=self.future,
                    continuation_frame={
                        **continuation_frame(),
                        "assistantMessageDigest": canonical_digest([tool.model_dump(mode="json")]),
                    },
                    audit_context={"transcript_id": "candidate-0"},
                ),
            )
        )
        self.pending = next(iter(self.registry._pending.values()))
        self.registry.activate_durable_boundary(self.pending)
        self.response = PermissionResponse("task-1", "ctx-1", "task-1", self.pending.input_id, "tool-1", "allow_once")

    def persist_result(self):
        self.transcript.append(
            str(self.root),
            "candidate-0",
            Message(role="user", content=[ToolResultBlock(tool_use_id="tool-1", content="fixture result")]),
        )

    async def close(self):
        await self.registry.complete(self.pending)
        if not self.future.done():
            self.future.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["identity", "decision_backup", "resolution_publication"])
async def test_transient_candidate_reply_failure_can_retry_the_same_card(tmp_path, monkeypatch, fault):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_kw: True)
    candidate = ResidentCandidatePermission(tmp_path)
    await candidate.open()
    try:
        if fault == "identity":
            error = PermissionIdentityValidationError("caller_temporarily_unavailable", retryable=True)
            monkeypatch.setattr(
                candidate.registry, "_validate_live_execution_identity", AsyncMock(side_effect=[error, None])
            )
        elif fault == "decision_backup":
            error = RuntimeError("decision backup temporarily unavailable")
            monkeypatch.setattr(
                candidate.registry, "_backup_claim_before_delivery", AsyncMock(side_effect=[error, None])
            )
        else:
            error = RuntimeError("resolution publication temporarily unavailable")
            monkeypatch.setattr(
                candidate.publisher, "publish_permission_resolution", AsyncMock(side_effect=[error, True])
            )
        with pytest.raises(type(error)):
            await candidate.registry.answer(candidate.response)
        assert not candidate.future.done()
        assert await candidate.registry.answer(candidate.response)
        assert candidate.future.result() is True
        candidate.persist_result()
        await candidate.publisher.publish(SubPipelineStreamEvent("candidate-0", 0, MessageStartEvent("after-result")))
        checkpoint = candidate.pending.checkpoint_store.load(candidate.pending.boundary_id)
        assert checkpoint["phase"] == "RESOLVED"
        assert not candidate.registry._pending
        assert not candidate.publisher._consumed_durable_permissions
    finally:
        await candidate.close()


@pytest.mark.asyncio
async def test_cancel_after_failed_decision_backup_seals_the_unreleased_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_kw: True)
    candidate = ResidentCandidatePermission(tmp_path)
    await candidate.open()
    try:
        monkeypatch.setattr(
            candidate.registry, "_backup_claim_before_delivery", AsyncMock(side_effect=RuntimeError("backup failed"))
        )
        with pytest.raises(RuntimeError, match="backup failed"):
            await candidate.registry.answer(candidate.response)
        assert not candidate.future.done()
        await candidate.registry.cancel_task("task-1")
        checkpoint = candidate.pending.checkpoint_store.load(candidate.pending.boundary_id)
        assert checkpoint["phase"] == "CANCELED"
        assert checkpoint["decision"]["value"] == "allow_once"
        assert candidate.future.result() is False
        assert not candidate.registry._pending
    finally:
        await candidate.close()
