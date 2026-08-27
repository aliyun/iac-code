import json
from types import SimpleNamespace

import pytest
from a2a.types import TaskArtifactUpdateEvent
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.events import (
    _ERROR_TEXT_MAX_CHARS,
    _METADATA_MAX_CHARS,
    _tool_result_metadata,
    _truncate,
    publish_interactive_permission_boundary,
    publish_stream_event,
)
from iac_code.a2a.exposure import A2AExposureType
from iac_code.a2a.input_required import PermissionInputRegistry, PermissionResponse
from iac_code.a2a.projection import project_a2a_data
from iac_code.services.permission_wait import (
    PermissionExecutionIdentityScope,
    PermissionWaitCheckpointStore,
    PermissionWaitCoordinator,
    PermissionWaitPolicy,
    permission_execution_identity,
)
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
from iac_code.services.session_backup import BackupReason, SessionBackupBlocked
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.cloud.aliyun.result_contract import ALIYUN_HTTP_METADATA_KEY
from iac_code.types.stream_events import (
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

from .fakes import FakeEventQueue, UnknownEvent, pending_future


class _ObservedBoundaryBackup:
    def __init__(
        self,
        queue: FakeEventQueue,
        store: PermissionWaitCheckpointStore,
        *,
        fail: bool = False,
        shared_committed: bool = True,
    ) -> None:
        self.queue = queue
        self.store = store
        self.fail = fail
        self.shared_committed = shared_committed
        self.calls: list[tuple[BackupReason, bool]] = []

    def backup_session(self, _cwd, _session_id, *, reason, critical):
        self.calls.append((reason, critical))
        if len(self.calls) == 1:
            assert self.queue.events == []
        paths = list(self.store.paths.permission_waits_dir.glob("pwb_*.json"))
        assert len(paths) == 1
        assert json.loads(paths[0].read_text(encoding="utf-8"))["phase"] == "WAITING"
        if self.fail:
            raise RuntimeError("shared backup failed")
        return SimpleNamespace(
            enabled=True,
            succeeded=True,
            retry_count=0,
            shared_committed=self.shared_committed,
        )


def _durable_permission_fixture(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(lambda: None))
    session_id = "session-1"
    SessionStorage().ensure_v2_session_dir_for_new_session(str(workspace), session_id)
    store = PermissionWaitCheckpointStore(str(workspace), session_id)
    registry = PermissionInputRegistry()
    registry.set_permission_wait_coordinator(PermissionWaitCoordinator(PermissionWaitPolicy()))
    return workspace, session_id, store, registry


def _permission_frame(tool_use_id: str) -> dict:
    return {
        "assistantMessageRef": "session.jsonl:0",
        "assistantMessageDigest": "a" * 64,
        "orderedToolUseIds": [tool_use_id],
        "currentIndex": 0,
        "decisions": [{"toolUseId": tool_use_id, "state": "pending", "source": None, "deniedResult": None}],
    }


def dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


def _aliyun_threshold_pair(limit: int, diagnostics: str = "") -> tuple[str, str]:
    marker = "BUSINESS_TAIL_MARKER"
    empty_body = json.dumps({"payload": "", "tail": marker}, ensure_ascii=False, indent=2)
    payload_size = limit - len(empty_body) - len(diagnostics)
    payload = {"payload": "X" * payload_size, "tail": marker}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    envelope = json.dumps(
        {
            "status": 200,
            "headers": {"requestid": "req-1"},
            "body": payload,
            "content_type": "application/json",
            "content_encoding": None,
            "size": len(body),
        },
        ensure_ascii=False,
        indent=2,
    )
    return body + diagnostics, envelope + diagnostics


@pytest.mark.asyncio
async def test_text_delta_publishes_agent_message() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=TextDeltaEvent(text="hello"))

    assert len(queue.events) == 1
    dumped = dump(queue.events[0])
    assert dumped["status"]["message"]["parts"][0]["text"] == "hello"
    assert dumped["status"]["message"]["role"] == "ROLE_AGENT"


@pytest.mark.asyncio
async def test_empty_text_delta_is_ignored() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=TextDeltaEvent(text=""))

    assert queue.events == []


@pytest.mark.asyncio
async def test_permission_request_is_denied_by_default_and_uses_shape_only_tool_input() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    long_value = "x" * (_METADATA_MAX_CHARS + 100)
    event = PermissionRequestEvent(
        tool_name="bash", tool_input={"cmd": long_value}, tool_use_id="tool-1", response_future=future
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    assert future.result() is False
    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["permission"]["autoApproved"] is False
    assert dumped["metadata"]["iac_code"]["permission"]["toolInput"]["cmd"] == {
        "type": "str",
        "length": len(long_value),
        "fingerprint": fingerprint_text(long_value),
    }


@pytest.mark.asyncio
async def test_mcp_progress_publishes_tool_trace_metadata() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=MCPProgressEvent(
            server_name="live",
            tool_name="echo",
            public_name="mcp__live__echo_8d3f",
            progress=1,
            total=2,
            message="halfway",
            tool_use_id="tool-1",
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert tool["status"] == "progress"
    assert tool["toolUseId"] == "tool-1"
    assert tool["name"] == "mcp__live__echo_8d3f"
    assert tool["mcp"]["serverName"] == "live"
    assert tool["mcp"]["toolName"] == "echo"
    assert tool["mcp"]["progress"] == 1
    assert tool["mcp"]["total"] == 2
    assert tool["mcp"]["message"] == "halfway"
    assert tool["mcpProgress"] == {
        "status": "progress",
        "toolUseId": "tool-1",
        "publicName": "mcp__live__echo_8d3f",
        "originalServerName": "live",
        "originalToolName": "echo",
        "progress": 1,
        "total": 2,
        "message": "halfway",
    }


@pytest.mark.asyncio
async def test_permission_request_tool_input_redacts_secret_values() -> None:
    queue = FakeEventQueue()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": 'cat /Users/alice/.iac-code/settings.yml && curl -H "Authorization: Bearer sk-live-secret"'},
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["permission"]["toolInput"]
    assert "sk-live-secret" not in str(tool_input)
    assert "Authorization: Bearer" not in str(tool_input)
    assert "/Users/alice" not in str(tool_input)
    assert tool_input["cmd"] == {
        "type": "str",
        "length": len(event.tool_input["cmd"]),
        "fingerprint": fingerprint_text(event.tool_input["cmd"]),
    }


@pytest.mark.asyncio
async def test_permission_request_tool_input_redacts_nested_secret_fields() -> None:
    queue = FakeEventQueue()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={
            "product": "ros",
            "action": "CreateStack",
            "customerEmail": "alice@example.com",
            "params": {
                "StackName": "demo",
                "customer-prod-123": "tenant-id",
                "AccessKeySecret": "secret-value",
                "private_key": "private-secret",
                "Signature": "signature-secret",
            },
        },
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["permission"]["toolInput"]
    for forbidden in (
        "secret-value",
        "private-secret",
        "signature-secret",
        "AccessKeySecret",
        "private_key",
        "Signature",
        "customerEmail",
        "customer-prod-123",
    ):
        assert forbidden not in str(tool_input)
    assert tool_input[fingerprint_text("customerEmail")] == {
        "type": "str",
        "length": len("alice@example.com"),
        "fingerprint": fingerprint_text("alice@example.com"),
    }
    assert tool_input["params"][fingerprint_text("StackName")] == {
        "type": "str",
        "length": 4,
        "fingerprint": fingerprint_text("demo"),
    }
    assert tool_input["params"][fingerprint_text("customer-prod-123")] == {
        "type": "str",
        "length": len("tenant-id"),
        "fingerprint": fingerprint_text("tenant-id"),
    }
    assert tool_input["params"][fingerprint_text("AccessKeySecret")] == {"redacted": True}
    assert tool_input["params"][fingerprint_text("private_key")] == {"redacted": True}
    assert tool_input["params"][fingerprint_text("Signature")] == {"redacted": True}


@pytest.mark.asyncio
async def test_aliyun_permission_metadata_uses_summary_for_sensitive_safe_fields() -> None:
    queue = FakeEventQueue()
    pem = "-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----"
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={
            "product": "ros",
            "action": "CreateStack",
            "params": {"TemplateBody": pem, "StackName": "demo"},
        },
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    permission = dumped["metadata"]["iac_code"]["permission"]
    rendered = str(permission)
    assert "toolInput" not in permission
    assert permission["inputSummary"]["tool_name"] == "aliyun_api"
    assert permission["inputSummary"]["params_fields"] == sorted(
        [fingerprint_text("StackName"), fingerprint_text("TemplateBody")]
    )
    assert permission["inputSummary"]["params_field_count"] == 2
    assert "StackName" not in rendered
    assert "TemplateBody" not in rendered
    assert "private-body" not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered


@pytest.mark.asyncio
async def test_permission_request_tool_input_redacts_sensitive_keys() -> None:
    queue = FakeEventQueue()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={
            "cmd": "pwd",
            "api_key": "plain-api-key",
            "nested": [{"accessKeySecret": "nested-access-key-secret"}],
            "headers": {"Authorization": "Bearer auth-token-secret"},
        },
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["permission"]["toolInput"]
    assert tool_input["cmd"] == {
        "type": "str",
        "length": len("pwd"),
        "fingerprint": fingerprint_text("pwd"),
    }
    assert tool_input[fingerprint_text("api_key")] == {"redacted": True}
    assert tool_input[fingerprint_text("nested")] == {"type": "array", "length": 1}
    assert tool_input["headers"][fingerprint_text("Authorization")] == {"redacted": True}
    rendered = str(tool_input)
    assert "api_key" not in rendered
    assert "nested" not in rendered
    assert "Authorization" not in rendered
    assert "plain-api-key" not in rendered
    assert "nested-access-key-secret" not in rendered
    assert "auth-token-secret" not in rendered


@pytest.mark.asyncio
async def test_permission_request_uses_configured_default_decision() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=future,
    )

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        auto_approve_permissions=True,
    )

    assert future.result() is True
    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["permission"]["autoApproved"] is True


@pytest.mark.asyncio
async def test_permission_request_uses_async_resolver() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=future,
    )
    seen: list[str] = []

    async def approve(request: PermissionRequestEvent) -> bool:
        seen.append(request.tool_use_id)
        return True

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        permission_resolver=approve,
    )

    assert seen == ["tool-1"]
    assert future.result() is True
    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["permission"]["autoApproved"] is True


@pytest.mark.asyncio
async def test_external_permission_is_backed_up_before_input_required_is_visible(tmp_path, monkeypatch) -> None:
    workspace, session_id, store, registry = _durable_permission_fixture(tmp_path, monkeypatch)
    queue = FakeEventQueue()
    backup = _ObservedBoundaryBackup(queue, store)
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack", "params": {"StackName": "demo"}},
        tool_use_id="tool-write",
        response_future=future,
        continuation_frame=_permission_frame("tool-write"),
        audit_context={"principal_ref": "aliyun:principal-fingerprint", "region": "cn-shanghai"},
    )

    pending = await publish_interactive_permission_boundary(
        queue,
        permission_event=event,
        permission_input_registry=registry,
        task_id="task-1",
        context_id="ctx-1",
        iac_code_session_id=session_id,
        permission_wait_cwd=str(workspace),
        permission_wait_backup_service=backup,
        wait_for_response=False,
    )

    assert backup.calls == [(BackupReason.INPUT_REQUIRED, True)]
    assert len(queue.events) == 1
    assert dump(queue.events[0])["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    checkpoint = store.list_active()
    assert len(checkpoint) == 1
    assert checkpoint[0]["phase"] == "WAITING"
    assert checkpoint[0]["principalRef"] == "aliyun:principal-fingerprint"
    assert checkpoint[0]["region"] == "cn-shanghai"
    await registry.complete(pending)
    future.cancel()


@pytest.mark.asyncio
async def test_external_permission_decision_is_backed_up_before_future_delivery(tmp_path, monkeypatch) -> None:
    workspace, session_id, store, registry = _durable_permission_fixture(tmp_path, monkeypatch)
    queue = FakeEventQueue()
    backup = _ObservedBoundaryBackup(queue, store)
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack", "params": {"StackName": "demo"}},
        tool_use_id="tool-write",
        response_future=future,
        continuation_frame=_permission_frame("tool-write"),
    )
    pending = await publish_interactive_permission_boundary(
        queue,
        permission_event=event,
        permission_input_registry=registry,
        task_id="task-1",
        context_id="ctx-1",
        iac_code_session_id=session_id,
        permission_wait_cwd=str(workspace),
        permission_wait_backup_service=backup,
        wait_for_response=False,
    )

    approved = await registry.answer(
        PermissionResponse(
            task_id="task-1",
            context_id="ctx-1",
            request_task_id="task-1",
            input_id=pending.input_id,
            tool_use_id="tool-write",
            decision="allow_once",
        )
    )

    assert approved is True
    assert backup.calls == [(BackupReason.INPUT_REQUIRED, True), (BackupReason.INPUT_REQUIRED, True)]
    assert future.result() is True
    checkpoint = store.load(str(pending.boundary_id))
    assert checkpoint["decision"]["backupStatus"] == "committed"
    assert checkpoint["decision"]["status"] == "applied"
    await registry.complete(pending)


@pytest.mark.asyncio
async def test_failed_decision_backup_keeps_claim_retriable_without_delivering_future(tmp_path, monkeypatch) -> None:
    workspace, session_id, store, registry = _durable_permission_fixture(tmp_path, monkeypatch)
    queue = FakeEventQueue()
    backup = _ObservedBoundaryBackup(queue, store)
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack"},
        tool_use_id="tool-write",
        response_future=future,
        continuation_frame=_permission_frame("tool-write"),
    )
    pending = await publish_interactive_permission_boundary(
        queue,
        permission_event=event,
        permission_input_registry=registry,
        task_id="task-1",
        context_id="ctx-1",
        iac_code_session_id=session_id,
        permission_wait_cwd=str(workspace),
        permission_wait_backup_service=backup,
        wait_for_response=False,
    )
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id=pending.input_id,
        tool_use_id="tool-write",
        decision="allow_once",
    )

    backup.fail = True
    with pytest.raises(RuntimeError, match="shared backup failed"):
        await registry.answer(response)
    assert future.done() is False
    checkpoint = store.load(str(pending.boundary_id))
    assert checkpoint["decision"]["status"] == "claimed"
    assert checkpoint["decision"]["backupStatus"] == "pending"

    backup.fail = False
    assert await registry.answer(response) is True
    assert future.result() is True
    checkpoint = store.load(str(pending.boundary_id))
    assert checkpoint["decision"]["backupStatus"] == "committed"
    assert checkpoint["decision"]["status"] == "applied"
    await registry.complete(pending)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_user_id", "allowed"),
    [
        ("stable-a2a-user", True),
        ("different-a2a-user", False),
    ],
)
async def test_live_cloud_permission_binds_stable_a2a_user_across_sts_rotation(
    tmp_path,
    monkeypatch,
    reply_user_id,
    allowed,
) -> None:
    workspace, session_id, store, registry = _durable_permission_fixture(tmp_path, monkeypatch)
    queue = FakeEventQueue()
    backup = _ObservedBoundaryBackup(queue, store)
    original = AliyunCredential(
        mode="StsToken",
        access_key_id="original-access-key-id",
        access_key_secret="secret",
        sts_token="token",
        region_id="cn-hangzhou",
    )
    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(lambda: original))
    tool_input = {"product": "ros", "action": "CreateStack", "region_id": "cn-hangzhou"}
    with PermissionExecutionIdentityScope("stable-a2a-user").install():
        principal_ref, region = permission_execution_identity(tool_name="aliyun_api", tool_input=tool_input)
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input=tool_input,
        tool_use_id="tool-write",
        response_future=future,
        continuation_frame=_permission_frame("tool-write"),
        audit_context={
            "principal_ref": principal_ref,
            "principal_kind": "a2a_user",
            "region": region,
        },
    )
    pending = await publish_interactive_permission_boundary(
        queue,
        permission_event=event,
        permission_input_registry=registry,
        task_id="task-1",
        context_id="ctx-1",
        iac_code_session_id=session_id,
        permission_wait_cwd=str(workspace),
        permission_wait_backup_service=backup,
        wait_for_response=False,
    )
    changed = AliyunCredential(
        mode="StsToken",
        access_key_id="changed-access-key-id",
        access_key_secret="secret",
        sts_token="token",
        region_id="cn-hangzhou",
    )
    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(lambda: changed))
    persisted = store.load(str(pending.boundary_id))
    assert persisted["principalKind"] == "a2a_user"
    assert "original-access-key-id" not in json.dumps(persisted)
    assert "secret" not in json.dumps(persisted)
    assert "token" not in json.dumps(persisted)

    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id=pending.input_id,
        tool_use_id="tool-write",
        decision="allow_once",
    )
    with PermissionExecutionIdentityScope(reply_user_id).install():
        if allowed:
            assert await registry.answer(response) is True
        else:
            with pytest.raises(InvalidParamsError, match="cloud execution identity changed"):
                await registry.answer(response)

    if allowed:
        assert future.result() is True
        assert store.load(str(pending.boundary_id))["decision"]["status"] == "applied"
    else:
        assert future.done() is False
        assert store.load(str(pending.boundary_id))["decision"]["status"] == "none"
        future.cancel()
    await registry.complete(pending)


@pytest.mark.asyncio
async def test_failed_critical_permission_backup_is_not_visible_or_recoverable(tmp_path, monkeypatch) -> None:
    workspace, session_id, store, registry = _durable_permission_fixture(tmp_path, monkeypatch)
    queue = FakeEventQueue()
    backup = _ObservedBoundaryBackup(queue, store, fail=True)
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack"},
        tool_use_id="tool-write",
        response_future=future,
        continuation_frame=_permission_frame("tool-write"),
    )

    with pytest.raises(RuntimeError, match="shared backup failed"):
        await publish_interactive_permission_boundary(
            queue,
            permission_event=event,
            permission_input_registry=registry,
            task_id="task-1",
            context_id="ctx-1",
            iac_code_session_id=session_id,
            permission_wait_cwd=str(workspace),
            permission_wait_backup_service=backup,
            wait_for_response=False,
        )

    assert queue.events == []
    assert store.list_active() == []
    assert future.result() is False


@pytest.mark.asyncio
async def test_uncommitted_shared_permission_backup_is_not_visible_or_recoverable(tmp_path, monkeypatch) -> None:
    workspace, session_id, store, registry = _durable_permission_fixture(tmp_path, monkeypatch)
    queue = FakeEventQueue()
    backup = _ObservedBoundaryBackup(queue, store, shared_committed=False)
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack"},
        tool_use_id="tool-write",
        response_future=future,
        continuation_frame=_permission_frame("tool-write"),
    )

    with pytest.raises(SessionBackupBlocked, match="did not reach the shared target"):
        await publish_interactive_permission_boundary(
            queue,
            permission_event=event,
            permission_input_registry=registry,
            task_id="task-1",
            context_id="ctx-1",
            iac_code_session_id=session_id,
            permission_wait_cwd=str(workspace),
            permission_wait_backup_service=backup,
            wait_for_response=False,
        )

    assert queue.events == []
    assert store.list_active() == []
    assert future.result() is False


@pytest.mark.parametrize("resolution", ["auto_approve", "resolver_allow", "resolver_deny"])
@pytest.mark.asyncio
async def test_a2a_internal_permission_resolution_creates_no_checkpoint_or_critical_backup(
    tmp_path,
    monkeypatch,
    resolution,
) -> None:
    workspace, session_id, store, registry = _durable_permission_fixture(tmp_path, monkeypatch)
    queue = FakeEventQueue()
    backup = _ObservedBoundaryBackup(queue, store)
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=future,
    )
    resolver = None
    auto_approve = resolution == "auto_approve"
    if resolution == "resolver_allow":

        def resolver(_request):
            return True
    elif resolution == "resolver_deny":

        def resolver(_request):
            return False

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        permission_resolver=resolver,
        permission_input_registry=registry,
        auto_approve_permissions=auto_approve,
        iac_code_session_id=session_id,
        permission_wait_cwd=str(workspace),
        permission_wait_backup_service=backup,
    )

    assert future.result() is (resolution != "resolver_deny")
    assert store.list_active() == []
    assert backup.calls == []


@pytest.mark.asyncio
async def test_wrapped_permission_request_uses_inner_event() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    inner = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=future,
    )
    event = SubPipelineStreamEvent(sub_pipeline_id="candidate-1", candidate_index=0, inner=inner)
    seen: list[PermissionRequestEvent] = []

    async def approve(request: PermissionRequestEvent) -> bool:
        seen.append(request)
        return True

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        permission_resolver=approve,
    )

    assert seen == [inner]
    assert future.done()
    assert future.result() is True
    dumped = dump(queue.events[0])
    permission = dumped["metadata"]["iac_code"]["permission"]
    assert permission["autoApproved"] is True
    assert permission["toolName"] == "bash"
    assert permission["toolUseId"] == "tool-1"
    assert permission["toolInput"]["cmd"] == {
        "type": "str",
        "length": 3,
        "fingerprint": fingerprint_text("pwd"),
    }


@pytest.mark.asyncio
async def test_unknown_event_is_skipped() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=UnknownEvent())

    assert queue.events == []


@pytest.mark.asyncio
async def test_unknown_event_logs_debug(caplog: pytest.LogCaptureFixture) -> None:
    queue = FakeEventQueue()
    caplog.set_level("DEBUG")

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=UnknownEvent())

    assert "Skipping unmapped A2A stream event: UnknownEvent" in caplog.text


def test_truncate_limits_nested_depth() -> None:
    value = "leaf"
    for _ in range(80):
        value = {"next": value}

    truncated = _truncate(value)

    current = truncated
    for _ in range(32):
        current = current["next"]
    assert current == "[truncated-depth]"


@pytest.mark.asyncio
async def test_error_event_passes_through_error_field() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error="boom with /secret/path", is_retryable=False, error_id="err-123"),
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "boom with /secret/path"
    assert dumped["metadata"]["iac_code"]["error"] == {"retryable": False, "errorId": "err-123"}


@pytest.mark.asyncio
async def test_error_event_redacts_public_error_text() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(
            error="RuntimeError: Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml",
            is_retryable=False,
        ),
    )

    dumped = dump(queue.events[0])
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert "sk-live" not in text
    assert "/Users/alice" not in text


@pytest.mark.asyncio
async def test_retryable_error_event_publishes_error_metadata() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error="should not leak", is_retryable=True, error_id="err-retry"),
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."
    assert dumped["metadata"]["iac_code"]["error"] == {"retryable": True, "errorId": "err-retry"}


@pytest.mark.asyncio
async def test_thinking_delta_is_explicitly_ignored() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=ThinkingDeltaEvent(text="hidden"))

    assert queue.events == []


@pytest.mark.asyncio
async def test_thinking_delta_publishes_raw_metadata_when_enabled() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ThinkingDeltaEvent(text="visible"),
        exposure_types={A2AExposureType.RAW_THINKING},
    )

    assert len(queue.events) == 1
    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_WORKING"
    assert dumped["metadata"]["iac_code"]["thinking"] == {
        "type": "raw_thinking",
        "text": "visible",
    }


@pytest.mark.asyncio
async def test_metadata_only_thinking_delta_is_not_published_when_raw_thinking_enabled() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ThinkingDeltaEvent(text="", provider_metadata={"provider": "gemini"}),
        exposure_types={A2AExposureType.RAW_THINKING},
    )

    assert queue.events == []


@pytest.mark.asyncio
async def test_tool_events_are_suppressed_when_tool_trace_is_not_enabled() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseStartEvent(tool_use_id="tool-1", name="bash"),
        exposure_types=frozenset(),
    )

    assert queue.events == []


@pytest.mark.asyncio
async def test_tool_events_publish_metadata_updates() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue, task_id="task-1", context_id="ctx-1", event=ToolUseStartEvent(tool_use_id="tool-1", name="bash")
    )
    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='{"cmd"'),
    )
    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(tool_use_id="tool-1", name="bash", input={"cmd": "pwd"}),
    )
    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="bash", result="ok", is_error=False),
    )

    dumped = [dump(event) for event in queue.events]
    assert dumped[0]["metadata"]["iac_code"]["tool"]["status"] == "started"
    assert dumped[1]["metadata"]["iac_code"]["tool"]["status"] == "input_delta"
    assert "partialJson" not in dumped[1]["metadata"]["iac_code"]["tool"]
    assert dumped[1]["metadata"]["iac_code"]["tool"]["partialJsonLength"] == 6
    assert dumped[2]["metadata"]["iac_code"]["tool"]["status"] == "input_complete"
    assert dumped[2]["metadata"]["iac_code"]["tool"]["name"] == "bash"
    assert dumped[2]["metadata"]["iac_code"]["tool"]["inputSummary"] == {
        "tool_name": "bash",
        "fields": {"cmd": {"type": "str"}},
    }
    assert dumped[2]["metadata"]["iac_code"]["tool"]["toolInput"] == {"cmd": "pwd"}
    assert "input" not in dumped[2]["metadata"]["iac_code"]["tool"]
    assert dumped[3]["metadata"]["iac_code"]["tool"]["status"] == "completed"


@pytest.mark.asyncio
async def test_aliyun_tool_result_trace_exposes_business_content_but_not_internal_http_metadata() -> None:
    queue = FakeEventQueue()
    event = ToolResultEvent(
        tool_use_id="tool-aliyun",
        tool_name="aliyun_api",
        result='{"Business":"value"}',
        metadata={ALIYUN_HTTP_METADATA_KEY: {"contract_version": "aliyun_body_v1", "header_count": 1}},
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert tool["result"] == '{"Business":"value"}'
    assert ALIYUN_HTTP_METADATA_KEY not in str(dumped)
    assert "aliyun_body_v1" not in str(dumped)

    disabled_queue = FakeEventQueue()
    await publish_stream_event(
        disabled_queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        exposure_types=frozenset(),
    )
    assert disabled_queue.events == []


@pytest.mark.parametrize("diagnostics", ["", "\nDelegated diagnostics: preflight passed"])
def test_aliyun_body_only_avoids_envelope_induced_a2a_trace_truncation(diagnostics: str) -> None:
    new_content, old_content = _aliyun_threshold_pair(_METADATA_MAX_CHARS, diagnostics)

    new_projected = _tool_result_metadata(new_content)
    old_projected = _tool_result_metadata(old_content)

    assert len(new_content) <= _METADATA_MAX_CHARS < len(old_content)
    assert new_projected == new_content
    assert len(old_projected) == _METADATA_MAX_CHARS
    assert "BUSINESS_TAIL_MARKER" in new_projected
    assert "BUSINESS_TAIL_MARKER" not in old_projected


@pytest.mark.asyncio
async def test_tool_input_delta_metadata_omits_raw_partial_json() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='ature":"signature-secret"'),
    )

    dumped = dump(queue.events[0])
    tool_metadata = dumped["metadata"]["iac_code"]["tool"]
    rendered = str(tool_metadata)
    assert tool_metadata["status"] == "input_delta"
    assert tool_metadata["partialJsonLength"] == len('ature":"signature-secret"')
    assert "partialJson" not in tool_metadata
    assert "signature-secret" not in rendered


@pytest.mark.asyncio
async def test_tool_use_input_metadata_preserves_values_before_wire_projection() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={"cmd": 'cat /Users/alice/.iac-code/settings.yml && curl -H "Authorization: Bearer sk-live-secret"'},
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert "input" not in tool
    assert tool["inputSummary"] == {"tool_name": "bash", "fields": {"cmd": {"type": "str"}}}
    assert tool["toolInput"] == {
        "cmd": 'cat /Users/alice/.iac-code/settings.yml && curl -H "Authorization: Bearer sk-live-secret"'
    }


@pytest.mark.asyncio
async def test_tool_use_input_metadata_redacts_structured_secret_fields() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={
                "product": "ros",
                "action": "CreateStack",
                "params": {
                    "StackName": "demo",
                    "AccessKeySecret": "secret-value",
                    "Signature": "signature-secret",
                    "private_key": "private-secret",
                    "Authorization": "Bearer bearer-secret",
                    "apiKey": "api-secret",
                },
            },
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    tool_input = tool["inputSummary"]["fields"]
    for forbidden in (
        "secret-value",
        "signature-secret",
        "private-secret",
        "bearer-secret",
        "api-secret",
        "AccessKeySecret",
        "Signature",
        "private_key",
        "Authorization",
        "apiKey",
    ):
        assert forbidden not in str(tool_input)
    assert "input" not in tool
    assert tool_input["params"]["fields"][fingerprint_text("StackName")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("AccessKeySecret")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("Signature")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("private_key")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("Authorization")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("apiKey")] == {"type": "str"}


@pytest.mark.asyncio
async def test_aliyun_tool_use_input_metadata_keeps_summary_and_renderable_arguments() -> None:
    queue = FakeEventQueue()
    pem = "-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "CreateStack",
                "params": {"TemplateBody": pem, "StackName": "demo"},
            },
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert "input" not in tool
    assert tool["inputSummary"]["tool_name"] == "aliyun_api"
    assert tool["inputSummary"]["params_fields"] == sorted(
        [fingerprint_text("StackName"), fingerprint_text("TemplateBody")]
    )
    assert tool["inputSummary"]["params_field_count"] == 2
    assert tool["toolInput"] == {
        "product": "ros",
        "action": "CreateStack",
        "params": {"TemplateBody": pem, "StackName": "demo"},
    }


@pytest.mark.asyncio
async def test_tool_use_input_metadata_preserves_renderable_business_arguments() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="ros_stack",
            input={
                "action": "CreateStack",
                "params": {
                    "DisableRollback": True,
                    "StackName": "demo-stack",
                    "TemplateBody": {"ROSTemplateFormatVersion": "2015-09-01"},
                },
                "region_id": "cn-hangzhou",
            },
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert "input" not in tool
    assert tool["toolInput"] == {
        "action": "CreateStack",
        "params": {
            "DisableRollback": True,
            "StackName": "demo-stack",
            "TemplateBody": {"ROSTemplateFormatVersion": "2015-09-01"},
        },
        "region_id": "cn-hangzhou",
    }


@pytest.mark.asyncio
async def test_tool_use_input_safe_mode_only_projects_paths() -> None:
    queue = FakeEventQueue()
    tool_input = {
        "action": "CreateStack",
        "path": "/workspace/template.yaml",
        "params": {"DisableRollback": True, "Password": "fake-secret", "StackName": "demo-stack"},
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(tool_use_id="tool-1", name="ros_stack", input=tool_input),
    )

    canonical = dump(queue.events[0])
    projected = project_a2a_data(
        canonical,
        public_path_roots=[{"path": "/workspace", "label": "."}],
        safe_mode=True,
    )
    projected_input = projected["metadata"]["iac_code"]["tool"]["toolInput"]
    assert projected_input == {
        "action": "CreateStack",
        "path": "[PATH]",
        "params": {"DisableRollback": True, "Password": "fake-secret", "StackName": "demo-stack"},
    }
    assert canonical["metadata"]["iac_code"]["tool"]["toolInput"] == tool_input


@pytest.mark.asyncio
async def test_tool_use_input_metadata_keeps_malformed_opaque_artifact_uri_before_wire_projection() -> None:
    queue = FakeEventQueue()
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={"cmd": f"cat {malformed_uri}", "note": malformed_uri},
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert "input" not in tool
    assert tool["inputSummary"]["fields"]["cmd"] == {"type": "str"}
    assert tool["inputSummary"]["fields"][fingerprint_text("note")] == {"type": "str"}
    assert tool["toolInput"] == {"cmd": f"cat {malformed_uri}", "note": malformed_uri}


@pytest.mark.asyncio
async def test_tool_use_input_metadata_keeps_percent_encoded_path_before_wire_projection() -> None:
    queue = FakeEventQueue()
    encoded_path = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={"cmd": f"cat {encoded_path}"},
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert "input" not in tool
    assert tool["inputSummary"]["fields"]["cmd"] == {"type": "str"}
    assert tool["toolInput"] == {"cmd": f"cat {encoded_path}"}


@pytest.mark.asyncio
async def test_tool_use_input_summary_fingerprints_names_without_corrupting_tool_input() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={
                "cmd": "git status",
                "customerEmail": "alice@example.com",
                "customer-prod-123": "tenant-id",
            },
        ),
    )

    tool = dump(queue.events[0])["metadata"]["iac_code"]["tool"]
    assert "input" not in tool
    assert tool["inputSummary"]["tool_name"] == "bash"
    fields = tool["inputSummary"]["fields"]
    assert fields["cmd"] == {"type": "str"}
    assert fields[fingerprint_text("customerEmail")] == {"type": "str"}
    assert fields[fingerprint_text("customer-prod-123")] == {"type": "str"}
    assert tool["toolInput"] == {
        "cmd": "git status",
        "customerEmail": "alice@example.com",
        "customer-prod-123": "tenant-id",
    }


@pytest.mark.asyncio
async def test_failed_tool_result_metadata_preserves_values_before_wire_projection() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="bash",
            result="Tool failed: DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml",
            is_error=True,
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert tool["status"] == "failed"
    assert "hunter2" in str(tool["result"])
    assert "/Users/alice" in str(tool["result"])


@pytest.mark.asyncio
async def test_mcp_tool_result_metadata_remains_an_ordinary_raw_tool_result() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="mcp__remote__echo",
            result=(
                "command=IAC_PRIVATE_COMMAND_ARG_MARKER_56 "
                "metadata=IAC_PRIVATE_NESTED_METADATA_MARKER_56 "
                "url=https://user:pass@example.test/mcp?Signature=IAC_PRIVATE_QUERY_MARKER_56 "
                "path=file:///Users/alice/.iac-code/settings.yml"
            ),
            is_error=False,
        ),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped["metadata"]["iac_code"]["tool"]["result"])
    assert "IAC_PRIVATE_COMMAND_ARG_MARKER_56" in rendered
    assert "IAC_PRIVATE_NESTED_METADATA_MARKER_56" in rendered
    assert "IAC_PRIVATE_QUERY_MARKER_56" in rendered
    assert "user:pass" in rendered
    assert "/Users/alice" in rendered
    assert "[REDACTED]" not in rendered


@pytest.mark.asyncio
async def test_tool_result_metadata_keeps_paths_canonical_before_wire_projection() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="bash",
            result=(
                "STDOUT:\n"
                "/Users/alice/project/src/app.py:12\n"
                "/Users/alice/.iac-code/tool-results/session-1/result.txt\n"
                "/Users/alice/private/secret.txt\n"
                "Exit code: 0"
            ),
            public_path_roots=[
                {"path": "/Users/alice/project", "label": "."},
                {"path": "/Users/alice/.iac-code", "label": "$IAC_CODE_CONFIG_DIR"},
            ],
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert tool["result"] == (
        "STDOUT:\n"
        "/Users/alice/project/src/app.py:12\n"
        "/Users/alice/.iac-code/tool-results/session-1/result.txt\n"
        "/Users/alice/private/secret.txt\n"
        "Exit code: 0"
    )
    rendered = str(dumped)
    assert "public_path_roots" not in rendered
    assert "publicPathRoots" not in rendered
    assert "/Users/alice" in rendered


@pytest.mark.asyncio
async def test_failed_tool_result_metadata_preserves_malformed_opaque_artifact_uri() -> None:
    queue = FakeEventQueue()
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="bash",
            result=f"Tool failed: {malformed_uri}",
            is_error=True,
        ),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped["metadata"]["iac_code"]["tool"]["result"])
    assert rendered == f"Tool failed: {malformed_uri}"


@pytest.mark.asyncio
async def test_error_event_redacts_malformed_opaque_artifact_uri() -> None:
    queue = FakeEventQueue()
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error=f"boom {malformed_uri}", is_retryable=False),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped["status"]["message"]["parts"][0]["text"])
    assert "[PATH]" in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_externalizes_large_file_metadata(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {"artifact": {"filename": "result.txt", "mediaType": "text/plain", "content": "hello artifact"}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    dumped = dump(queue.events[1])
    artifact = dumped["metadata"]["iac_code"]["tool"]["artifact"]
    assert artifact["filename"] == "result.txt"
    assert artifact["byteSize"] == 14


@pytest.mark.asyncio
async def test_tool_result_artifact_windows_filename_does_not_leak_path(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {
        "artifact": {
            "filename": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "mediaType": "text/yaml",
            "content": "ROSTemplate",
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    assert dumped["artifact"]["name"] == "template.yaml"
    assert dumped["artifact"]["parts"][0]["filename"] == "template.yaml"
    assert r"C:\\" not in rendered
    assert "%5CUsers" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_uri_only_artifact_preserves_existing_fields_before_wire_projection() -> None:
    queue = FakeEventQueue()
    result = {
        "artifact": {
            "filename": "template.yaml",
            "uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "downloadUrl": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "publicUrl": r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "encodedOwnerUrl": "iac-code-artifact://C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo/template.yaml",
            "backupUri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
            "sourceUri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "source": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "metadata": {
                "uri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
                "byteSize": 10,
            },
            "parts": [
                {
                    "url": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                    "metadata": {"uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"},
                }
            ],
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    rendered = str(dumped)
    assert artifact["filename"] == "template.yaml"
    assert artifact == result["artifact"]
    assert "file://" in rendered
    assert "Users" in rendered
    assert ".iac-code" in rendered


@pytest.mark.asyncio
async def test_tool_result_uri_only_artifact_keeps_valid_opaque_uri() -> None:
    queue = FakeEventQueue()
    uri = "iac-code-artifact://artifact-1/template.yaml"
    result = {
        "artifact": {
            "filename": "template.yaml",
            "uri": uri,
            "downloadUrl": uri,
            "parts": [{"url": uri}],
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact["uri"] == uri
    assert artifact["downloadUrl"] == uri
    assert artifact["parts"][0]["url"] == uri
    rendered = str(dumped)
    assert "iac-code-artifac[PATH]" not in rendered
    assert "file://" not in rendered


@pytest.mark.asyncio
async def test_tool_result_artifact_list_preserves_values_before_wire_projection() -> None:
    queue = FakeEventQueue()
    legacy_uri = r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"
    result = {
        "artifact": [
            legacy_uri,
            {
                "filename": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
                "uri": [legacy_uri],
                "parts": [legacy_uri, {"url": legacy_uri}],
            },
        ]
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact == result["artifact"]
    rendered = str(dumped)
    assert "file://" in rendered
    assert "Users" in rendered
    assert ".iac-code" in rendered


@pytest.mark.asyncio
async def test_tool_result_artifact_scalar_is_preserved_before_wire_projection() -> None:
    queue = FakeEventQueue()
    result = {"artifact": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact == result["artifact"]
    rendered = str(dumped)
    assert "file://" in rendered
    assert "Users" in rendered
    assert ".iac-code" in rendered


@pytest.mark.asyncio
async def test_tool_result_artifact_payload_is_externalized_without_redacting_metadata() -> None:
    queue = FakeEventQueue()
    result = {
        "artifact": {
            "filename": "result.txt",
            "Content": "secret content",
            "Raw": "secret raw",
            "Base64": "c2VjcmV0",
            "Path": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "metadata": {"label": "safe", "api_key": "plain-secret"},
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact == {"filename": "result.txt", "metadata": {"label": "safe", "api_key": "plain-secret"}}
    rendered = str(dumped)
    assert "secret content" not in rendered
    assert "secret raw" not in rendered
    assert "c2VjcmV0" not in rendered
    assert "plain-secret" in rendered


@pytest.mark.asyncio
async def test_tool_result_metadata_externalizes_root_artifact_list_without_redaction() -> None:
    queue = FakeEventQueue()
    result = [
        {
            "artifact": {
                "filename": "template.yaml",
                "Content": "RAW-TEMPLATE-CONTENT",
                "metadata": {"token": "plain-token"},
                "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
            }
        }
    ]

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"][0]["artifact"]
    assert artifact == {
        "filename": "template.yaml",
        "metadata": {"token": "plain-token"},
        "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
    }
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "plain-token" in rendered
    assert "Alice and Bob" in rendered
    assert ".iac-code" in rendered


@pytest.mark.asyncio
async def test_tool_result_metadata_externalizes_case_variant_artifact_key_without_redaction() -> None:
    queue = FakeEventQueue()
    result = {
        "Artifact": {
            "filename": "template.yaml",
            "Content": "RAW-TEMPLATE-CONTENT",
            "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["Artifact"]
    assert artifact == {
        "filename": "template.yaml",
        "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
    }
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "Alice and Bob" in rendered
    assert ".iac-code" in rendered


@pytest.mark.asyncio
async def test_failed_tool_result_dict_externalizes_artifact_payload_without_redacting_values() -> None:
    queue = FakeEventQueue()
    result = {
        "artifact": {
            "filename": "template.yaml",
            "Content": "RAW-TEMPLATE-CONTENT",
            "Raw": "RAW",
            "Base64": "UkFX",
            "metadata": {"Authorization": "Bearer plain-auth-value"},
        },
        "api_key": "secret-key",
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=True),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    result_metadata = dumped["metadata"]["iac_code"]["tool"]["result"]
    assert result_metadata == {
        "artifact": {"filename": "template.yaml", "metadata": {"Authorization": "Bearer plain-auth-value"}},
        "api_key": "secret-key",
    }
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "plain-auth-value" in rendered
    assert "secret-key" in rendered


@pytest.mark.asyncio
async def test_tool_result_publishes_standard_artifact_update_event(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {"artifact": {"filename": "result.txt", "mediaType": "text/plain", "content": "hello artifact"}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    artifact_event = queue.events[0]
    assert isinstance(artifact_event, TaskArtifactUpdateEvent)
    dumped = dump(artifact_event)
    assert dumped["artifact"]["name"] == "result.txt"
    assert dumped["artifact"]["parts"][0]["url"].startswith("iac-code-artifact://")
    assert dumped["artifact"]["parts"][0]["mediaType"] == "text/plain"
    assert dumped["artifact"]["metadata"]["byteSize"] == 14
    assert dumped["lastChunk"] is True
    assert dumped.get("append", False) is False
    rendered = str(dumped)
    assert "file://" not in rendered
    assert str(tmp_path) not in rendered
    assert (
        dumped["artifact"]["artifactId"]
        == dump(queue.events[1])["metadata"]["iac_code"]["tool"]["artifact"]["artifactId"]
    )


@pytest.mark.asyncio
async def test_tool_result_skips_non_text_artifact_content(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {"artifact": {"filename": "result.bin", "mediaType": "application/octet-stream", "content": b"binary"}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    dumped = dump(queue.events[0])
    assert "artifact" not in dumped["metadata"]["iac_code"]["tool"]


@pytest.mark.asyncio
async def test_tool_result_externalizes_base64_binary_artifact(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {
        "artifact": {
            "filename": "diagram.png",
            "mediaType": "image/png",
            "bytes": "iVBORw0KGgppbWFnZQ==",
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="draw", result=result, is_error=False),
        artifact_store=store,
    )

    artifact_event = queue.events[0]
    assert isinstance(artifact_event, TaskArtifactUpdateEvent)
    dumped = dump(artifact_event)
    assert dumped["artifact"]["parts"][0]["mediaType"] == "image/png"
    assert dumped["artifact"]["metadata"]["byteSize"] == 13
    artifact_metadata = dump(queue.events[1])["metadata"]["iac_code"]["tool"]["artifact"]
    assert artifact_metadata["mediaType"] == "image/png"
    assert store.path_for(artifact_metadata["artifactId"]).read_bytes() == b"\x89PNG\r\n\x1a\nimage"


@pytest.mark.asyncio
async def test_tool_result_externalizes_workspace_path_binary_artifact(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    source = tmp_path / "voice.wav"
    source.write_bytes(b"RIFFaudio")
    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path / "artifacts")
    result = {"artifact": {"filename": "voice.wav", "mediaType": "audio/wav", "path": str(source)}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="record", result=result, is_error=False),
        artifact_store=store,
    )

    artifact_metadata = dump(queue.events[1])["metadata"]["iac_code"]["tool"]["artifact"]
    assert artifact_metadata["byteSize"] == 9
    assert artifact_metadata["sourcePath"] == str(source)
    assert dump(queue.events[0])["artifact"]["metadata"]["sourcePath"] == str(source)
    assert store.path_for(artifact_metadata["artifactId"]).read_bytes() == b"RIFFaudio"


@pytest.mark.asyncio
async def test_tool_result_externalizes_artifact_declared_in_event_metadata(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    source = tmp_path / "template.yaml"
    source.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path / "artifacts")

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="write_file",
            result="Successfully wrote template.yaml",
            metadata={
                "artifact": {
                    "filename": "template.yaml",
                    "mediaType": "application/yaml",
                    "path": str(source),
                }
            },
        ),
        artifact_store=store,
    )

    artifact = dump(queue.events[0])["artifact"]
    assert artifact["name"] == "template.yaml"
    assert artifact["metadata"]["sourcePath"] == str(source)
    assert store.path_for(artifact["artifactId"]).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_message_end_publishes_usage_metadata() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=MessageEndEvent(
            stop_reason="end_turn",
            usage=Usage(provider="dashscope", model="qwen", input_tokens=2, output_tokens=3),
        ),
    )

    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["usage"]["totalTokens"] == 5
    assert dumped["metadata"]["iac_code"]["usage"]["provider"] == "dashscope"
    assert dumped["metadata"]["iac_code"]["usage"]["model"] == "qwen"


@pytest.mark.asyncio
async def test_error_event_truncates_overlong_payload() -> None:
    queue = FakeEventQueue()
    long_error = "X" * (_ERROR_TEXT_MAX_CHARS + 500)

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error=long_error, is_retryable=False),
    )

    dumped = dump(queue.events[0])
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert len(text) <= _ERROR_TEXT_MAX_CHARS
    assert text == "X" * _ERROR_TEXT_MAX_CHARS


@pytest.mark.asyncio
async def test_retryable_error_event_still_says_retry() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error="should not leak", is_retryable=True),
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."
