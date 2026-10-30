"""A previously audited cloud decision still validates its caller after a cold handoff."""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message, ToolUseBlock
from iac_code.services.permission_wait import canonical_digest
from iac_code.services.providers.aliyun_identity import AliyunCallerIdentity, AliyunCallerIdentityUnavailableError
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    Usage,
)
from tests.agent.test_agent_loop_permissions import WriteTool

from .fakes import FakeEventQueue, FakeRuntime
from .test_normal_permission_cold_handoff import isolated_environment as isolated_environment
from .test_normal_permission_handoff_failures import _FailureHandoff
from .test_permission_execution_control import permission_reply


class CloudWriteTool(WriteTool):
    @property
    def name(self):
        return "aliyun_api"


class ContinueProvider:
    def get_model_name(self):
        return "test"

    async def stream(self, messages, system, tools=None):
        yield MessageStartEvent(message_id="completed")
        yield TextDeltaEvent(text="completed cloud write")
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage())


class AuditedCloudHandoff(_FailureHandoff):
    async def run_streaming(self, _prompt):
        context = await self.store.get_context_record("ctx-1")
        tool = ToolUseBlock(id="tool-1", name="aliyun_api", input={"value": "first", "region_id": "cn-hangzhou"})
        storage = SessionStorage()
        index = len(storage.load(str(self.root), context.session_id))
        storage.append(str(self.root), context.session_id, Message(role="assistant", content=[tool]))
        yield PermissionRequestEvent(
            tool_name=tool.name,
            tool_input=tool.input,
            tool_use_id=tool.id,
            response_future=self.response,
            continuation_frame={
                "assistantMessageRef": f"session.jsonl:{index}",
                "assistantMessageDigest": canonical_digest([tool.model_dump(mode="json")]),
                "orderedToolUseIds": [tool.id],
                "currentIndex": 0,
                "decisions": [{"toolUseId": tool.id, "state": "pending", "source": None, "deniedResult": None}],
            },
        )


class CloudRecoveryRuntime:
    def __init__(self):
        self.tool = CloudWriteTool()
        self.registry = ToolRegistry()
        self.registry.register(self.tool)
        self.provider = ContinueProvider()

    def create(self, options):
        return FakeRuntime(
            provider_manager=self.provider,
            tool_registry=self.registry,
            session_id=options.session_id,
            agent_loop=AgentLoop(
                provider_manager=self.provider,
                system_prompt="test",
                tool_registry=self.registry,
                max_turns=1,
                session_storage=SessionStorage(),
                session_id=options.session_id,
                cwd=options.cwd,
                resume_messages=options.resume_messages,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["same", "changed", "unavailable"])
async def test_audited_allow_revalidates_cloud_principal_before_real_agent_loop_resume(tmp_path, monkeypatch, caller):
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "offline-fixture-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "offline-fixture-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou")
    resolver = AsyncMock(return_value=AliyunCallerIdentity(kind="ram_role", account_id="10001", subject_id="role-1"))
    monkeypatch.setattr("iac_code.services.providers.aliyun_identity.AliyunCallerIdentityResolver.resolve", resolver)
    fixture = AuditedCloudHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.suspend()
        fixture.record_decision_audit()
        assert resolver.await_count == 1
        if caller == "changed":
            resolver.return_value = AliyunCallerIdentity(kind="ram_role", account_id="10001", subject_id="other-role")
        elif caller == "unavailable":
            resolver.side_effect = AliyunCallerIdentityUnavailableError("sts_temporarily_unavailable", retryable=True)
        audit = Mock(return_value=True)
        monkeypatch.setattr("iac_code.a2a.executor.emit_permission_boundary_audit", audit)
        runtime = CloudRecoveryRuntime()
        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", runtime.create)
        executor = IacCodeA2AExecutor(
            task_store=fixture.store,
            model="test",
            backup_service=fixture.backup,
            execution_control_service=fixture.recovering,
        )
        queue = FakeEventQueue()
        await executor.execute(permission_reply(tmp_path, input_id=fixture.pending.input_id), queue)
        if caller == "same":
            assert runtime.tool.executed, "The same stable principal must recover an already audited allow"
        else:
            assert not runtime.tool.executed
            assert fixture.document()["executionId"] == fixture.control.execution_id
            expected = "cloud_execution_identity_changed" if caller == "changed" else "sts_temporarily_unavailable"
            assert expected in json.dumps([MessageToDict(event) for event in queue.events])
        assert resolver.await_count == 2
        audit.assert_not_called()
    finally:
        await fixture.close()
