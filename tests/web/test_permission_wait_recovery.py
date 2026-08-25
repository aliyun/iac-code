from __future__ import annotations

import asyncio
import json

import pytest

from iac_code.agent.message import Message, ToolUseBlock
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
from iac_code.services.permission_wait import PermissionWaitPolicy, canonical_digest
from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings, PermissionResult
from iac_code.types.stream_events import PermissionRequestEvent
from iac_code.web.runtime import WebSessionRuntime
from iac_code.web.session_manager import WebSessionManager


def _seed_permission_turn(manager: WebSessionManager, session):
    tool_use = ToolUseBlock(
        id="tool-create-stack",
        name="aliyun_api",
        input={"product": "ROS", "action": "CreateStack", "params": {"StackName": "test"}},
    )
    assistant = Message(role="assistant", content=[tool_use])
    manager.storage.append(session.cwd, session.session_id, assistant)
    frame = {
        "assistantMessageRef": "session.jsonl:0",
        "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
        "orderedToolUseIds": [tool_use.id],
        "currentIndex": 0,
        "decisions": [
            {
                "toolUseId": tool_use.id,
                "state": "pending",
                "source": None,
                "deniedResult": None,
            }
        ],
    }
    future = asyncio.get_running_loop().create_future()
    event = PermissionRequestEvent(
        tool_name=tool_use.name,
        tool_input=tool_use.input,
        tool_use_id=tool_use.id,
        response_future=future,
        continuation_frame=frame,
        audit_context={"session_id": session.session_id, "cwd": session.cwd},
    )
    return event, future


async def _rebuild_audit_event(_session, _checkpoint, recovered):
    metadata = PermissionAuditMetadata(
        scope="settings_rule",
        source="permission_pipeline",
        rule_source="project_settings",
        rule="aliyun_api(ROS:CreateStack)",
        reason_type="rule",
        reason_detail="current permission rule",
        is_read_only=False,
        operation={"product": "ROS", "action": "CreateStack", "operation_type": "write"},
    )
    return PermissionRequestEvent(
        tool_name=recovered.tool_name,
        tool_input=recovered.tool_input,
        tool_use_id=recovered.tool_use_id,
        permission_result=PermissionResult(behavior="ask", audit=metadata),
        audit_context={
            **recovered.audit_context,
            "metadata": metadata,
            "settings": PermissionAuditSettings(include_tool_input=True, max_file_bytes=1234, max_files=2),
        },
    )


def test_web_permission_wait_policy_defaults_to_unlimited() -> None:
    policy = PermissionWaitPolicy.from_config(None)

    assert policy.resident_timeout_seconds is None
    assert policy.sub_pipeline_timeout_seconds is None
    assert policy.timeout_grace_seconds == 30


@pytest.mark.asyncio
async def test_web_checkpoint_exists_before_permission_request_is_visible(tmp_path, monkeypatch) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="web-permission-order")
    event, _future = _seed_permission_turn(manager, session)
    store = manager.permission_checkpoint_store(session)
    original_append = session.events.append
    observed: list[str] = []

    def append(event_type, payload):
        if event_type == "permission.request":
            active = store.list_active()
            assert len(active) == 1
            assert active[0]["inputId"] == payload["requestId"]
            observed.append(active[0]["phase"])
        return original_append(event_type, payload)

    monkeypatch.setattr(session.events, "append", append)
    request_id = await manager.open_permission_request(
        session,
        {
            "toolName": event.tool_name,
            "toolUseId": event.tool_use_id,
            "toolInput": event.tool_input,
            "message": "Allow deployment?",
        },
        permission_event=event,
        permission_class="normal",
    )

    assert observed == ["WAITING"]
    assert session.pending_permissions[request_id].boundary_id == store.list_active()[0]["boundaryId"]


@pytest.mark.asyncio
async def test_web_pipeline_recovery_audits_canonical_step_transcript(tmp_path) -> None:
    projects = tmp_path / "projects"
    cwd = tmp_path / "project"
    manager = WebSessionManager(projects_dir=projects, cwd=cwd)
    session = manager.create_session(
        session_id="web-pipeline-permission",
        mode="pipeline",
        task_id="task-1",
        context_id="context-1",
    )
    transcript_id = "transcript_att_0001"
    tool_use = ToolUseBlock(
        id="tool-create-stack",
        name="aliyun_api",
        input={"product": "ROS", "action": "CreateStack"},
    )
    assistant = Message(role="assistant", content=[tool_use])
    transcript_storage = PipelineTranscriptStorage(
        manager.storage.session_dir(session.cwd, session.session_id) / "pipeline"
    )
    transcript_storage.append(session.cwd, transcript_id, assistant)
    future = asyncio.get_running_loop().create_future()
    event = PermissionRequestEvent(
        tool_name=tool_use.name,
        tool_input=tool_use.input,
        tool_use_id=tool_use.id,
        response_future=future,
        continuation_frame={
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": [tool_use.id],
            "currentIndex": 0,
            "decisions": [{"toolUseId": tool_use.id, "state": "pending", "source": None}],
        },
        audit_context={
            "session_id": transcript_id,
            "cwd": session.cwd,
            "root_session_id": session.session_id,
            "transcript_id": transcript_id,
        },
    )
    request_id = await manager.open_permission_request(
        session,
        {"toolName": tool_use.name, "toolUseId": tool_use.id, "message": "Allow deployment?"},
        permission_event=event,
        permission_class="pipeline",
    )
    checkpoint = manager.permission_checkpoint_store(session).list_active()[0]
    assert checkpoint["continuationFrame"]["assistantMessageRef"] == (
        f"pipeline/transcripts/{transcript_id}/session.jsonl:0"
    )

    manager.cancel_pending_requests_for_shutdown(session)
    restarted = WebSessionManager(projects_dir=projects, cwd=cwd)
    restarted_session = restarted.create_session(session_id=session.session_id, mode="pipeline")
    result = await restarted.resolve_durable_permission(
        request_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
        audit_event_rebuilder=_rebuild_audit_event,
    )

    assert result["decision"] == "allow_once"
    audit_path = (
        restarted.storage.session_dir(restarted_session.cwd, restarted_session.session_id)
        / "pipeline"
        / "transcripts"
        / transcript_id
        / "permission-audit.jsonl"
    )
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [row["tool_use_id"] for row in rows] == [tool_use.id]
    assert rows[0]["operation"] == {
        "action": "CreateStack",
        "is_read_only": False,
        "operation_type": "write",
        "product": "ROS",
    }
    assert rows[0]["rule_source"] == "project_settings"
    assert rows[0]["tool_input_redacted"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_web_audit_failure_does_not_install_always_allow_rule(tmp_path, monkeypatch, restart) -> None:
    projects = tmp_path / "projects"
    cwd = tmp_path / "project"
    manager = WebSessionManager(projects_dir=projects, cwd=cwd)
    session = manager.create_session(session_id="web-audit-failed-{}".format(restart))
    event, future = _seed_permission_turn(manager, session)
    request_id = await manager.open_permission_request(
        session,
        {
            "toolName": event.tool_name,
            "toolUseId": event.tool_use_id,
            "toolInput": event.tool_input,
            "message": "Always allow deployment?",
        },
        permission_event=event,
        permission_class="normal",
    )
    target_manager = manager
    target_session = session
    if restart:
        manager.cancel_pending_requests_for_shutdown(session)
        target_manager = WebSessionManager(projects_dir=projects, cwd=cwd)
        target_session = target_manager.create_session(session_id=session.session_id)
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_boundary_audit",
        lambda *_args, **_kwargs: False,
    )

    result = await target_manager.resolve_durable_permission(
        request_id,
        {"choice": "always_allow"},
        session_id=target_session.session_id,
        audit_event_rebuilder=_rebuild_audit_event if restart else None,
    )

    assert result["decision"] == "deny"
    assert target_session.permission_context is None
    if not restart:
        assert future.result() is False


@pytest.mark.asyncio
async def test_web_restart_rehydrates_safe_prompt_and_claims_old_permission_once(tmp_path) -> None:
    projects = tmp_path / "projects"
    cwd = tmp_path / "project"
    manager = WebSessionManager(projects_dir=projects, cwd=cwd)
    session = manager.create_session(session_id="web-permission-restart")
    event, _future = _seed_permission_turn(manager, session)
    request_id = await manager.open_permission_request(
        session,
        {
            "toolName": event.tool_name,
            "toolUseId": event.tool_use_id,
            "toolInput": event.tool_input,
            "message": "Allow deployment?",
        },
        permission_event=event,
        permission_class="normal",
    )

    restarted = WebSessionManager(projects_dir=projects, cwd=cwd)
    restored_session = restarted.create_session(session_id=session.session_id)
    restored = restored_session.pending_permissions[request_id]

    assert restored.payload["resumable"] is True
    assert restored.payload["toolInput"] == {}
    assert restored.payload["permissionWaitStatus"] == "suspended"

    result = await restarted.resolve_durable_permission(
        request_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
        audit_event_rebuilder=_rebuild_audit_event,
    )

    assert result["resolved"] is True
    assert result["needsRecovery"] is True
    assert result["decision"] == "allow_once"
    checkpoint = result["checkpoint"]
    assert checkpoint["phase"] == "SUSPENDED"
    assert checkpoint["decision"]["status"] == "claimed"
    assert checkpoint["decision"]["auditStatus"] == "recorded"
    audit_path = restarted.storage.session_dir(str(cwd.resolve()), session.session_id) / "permission-audit.jsonl"
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [row["tool_use_id"] for row in audit_rows] == [event.tool_use_id]

    # Rehydrate again to exercise the persisted idempotent claim.  The same
    # answer is accepted without adding a second decision audit row.
    duplicate_manager = WebSessionManager(projects_dir=projects, cwd=cwd)
    duplicate_session = duplicate_manager.create_session(session_id=session.session_id)
    duplicate = await duplicate_manager.resolve_durable_permission(
        request_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
    )
    assert duplicate["decision"] == "allow_once"
    duplicate_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(duplicate_rows) == 1

    store = duplicate_manager.permission_checkpoint_store(duplicate_session)
    store.resolve(
        checkpoint["boundaryId"],
        result_digest="result-digest",
        ack={"decision": "allow_once", "accepted": True},
    )
    receipt_manager = WebSessionManager(projects_dir=projects, cwd=cwd)
    receipt_manager.create_session(session_id=session.session_id)
    receipt = await receipt_manager.resolve_durable_permission(
        request_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
    )
    assert receipt["resolved"] is True
    assert receipt["duplicate"] is True
    assert receipt["needsRecovery"] is False
    with pytest.raises(ValueError, match="conflicts with receipt"):
        await receipt_manager.resolve_durable_permission(
            request_id,
            {"choice": "reject_once"},
            session_id=session.session_id,
        )


@pytest.mark.asyncio
async def test_web_permission_cancel_and_answer_use_checkpoint_lock(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="web-permission-cancel")
    event, future = _seed_permission_turn(manager, session)
    request_id = await manager.open_permission_request(
        session,
        {
            "toolName": event.tool_name,
            "toolUseId": event.tool_use_id,
            "toolInput": event.tool_input,
            "message": "Allow deployment?",
        },
        permission_event=event,
        permission_class="normal",
    )
    pending = session.pending_permissions[request_id]
    assert pending.boundary_id is not None
    assert pending.checkpoint_store is not None

    pending.checkpoint_store.claim_decision(
        pending.boundary_id,
        value="allow_once",
        source="user",
    )
    manager.cancel_permission_request(request_id, session_id=session.session_id)

    assert request_id in session.pending_permissions
    assert future.cancelled() is False
    assert pending.checkpoint_store.load(pending.boundary_id)["decision"]["value"] == "allow_once"

    second_manager = WebSessionManager(projects_dir=tmp_path / "projects-2", cwd=tmp_path / "project-2")
    second_session = second_manager.create_session(session_id="web-permission-cancel-wins")
    second_event, second_future = _seed_permission_turn(second_manager, second_session)
    second_id = await second_manager.open_permission_request(
        second_session,
        {
            "toolName": second_event.tool_name,
            "toolUseId": second_event.tool_use_id,
            "toolInput": second_event.tool_input,
            "message": "Allow deployment?",
        },
        permission_event=second_event,
        permission_class="normal",
    )
    second_pending = second_session.pending_permissions[second_id]
    second_manager.cancel_permission_request(second_id, session_id=second_session.session_id)

    assert second_id not in second_session.pending_permissions
    assert second_future.cancelled() is True
    assert second_pending.checkpoint_store.load(second_pending.boundary_id)["phase"] == "CANCELED"


@pytest.mark.asyncio
async def test_web_duplicate_live_answer_stays_ack_only_until_boundary_receipt(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="web-permission-live-duplicate")
    event, future = _seed_permission_turn(manager, session)
    request_id = await manager.open_permission_request(
        session,
        {
            "toolName": event.tool_name,
            "toolUseId": event.tool_use_id,
            "toolInput": event.tool_input,
            "message": "Allow deployment?",
        },
        permission_event=event,
        permission_class="normal",
    )
    pending = session.pending_permissions[request_id]
    assert pending.boundary_id is not None

    first = await manager.resolve_durable_permission(
        request_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
    )
    duplicate = await manager.resolve_durable_permission(
        request_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
    )

    assert first["duplicate"] is False
    assert first["needsRecovery"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["needsRecovery"] is False
    assert future.result() is True
    assert request_id in session.pending_permissions
    assert manager.permission_wait_coordinator.has_live_boundary(pending.boundary_id) is True

    manager.resolve_permission_boundaries(session, [pending.boundary_id])

    assert request_id not in session.pending_permissions
    assert manager.permission_wait_coordinator.has_live_boundary(pending.boundary_id) is False
    assert pending.checkpoint_store.load(pending.boundary_id)["phase"] == "RESOLVED"


@pytest.mark.asyncio
async def test_web_lifecycle_shutdown_orphans_durable_wait_for_restart_recovery(tmp_path) -> None:
    projects = tmp_path / "projects"
    cwd = tmp_path / "project"
    manager = WebSessionManager(projects_dir=projects, cwd=cwd)
    session = manager.create_session(session_id="web-permission-shutdown")
    event, future = _seed_permission_turn(manager, session)
    request_id = await manager.open_permission_request(
        session,
        {
            "toolName": event.tool_name,
            "toolUseId": event.tool_use_id,
            "toolInput": event.tool_input,
            "message": "Allow deployment?",
        },
        permission_event=event,
        permission_class="normal",
    )
    pending = session.pending_permissions[request_id]
    assert pending.boundary_id is not None
    waiter = asyncio.create_task(
        WebSessionRuntime(session, manager=manager)._await_permission_request(request_id, event)
    )
    await asyncio.sleep(0)

    manager.cancel_pending_requests_for_shutdown(session)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    checkpoint = pending.checkpoint_store.load(pending.boundary_id)
    assert checkpoint["phase"] == "SUSPENDED"
    assert checkpoint["decision"]["status"] == "none"
    assert future.done() is False
    assert request_id in session.pending_permissions
    assert manager.permission_wait_coordinator.has_live_boundary(pending.boundary_id) is False

    restarted = WebSessionManager(projects_dir=projects, cwd=cwd)
    restarted.create_session(session_id=session.session_id)
    recovered = await restarted.resolve_durable_permission(
        request_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
        audit_event_rebuilder=_rebuild_audit_event,
    )
    assert recovered["resolved"] is True
    assert recovered["needsRecovery"] is True
    assert recovered["checkpoint"]["phase"] == "SUSPENDED"


@pytest.mark.asyncio
async def test_web_successor_replaces_old_live_owner_and_old_answer_returns_receipt(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="web-permission-successor")
    tools = [
        ToolUseBlock(id="tool-1", name="aliyun_api", input={"action": "CreateStack"}),
        ToolUseBlock(id="tool-2", name="aliyun_api", input={"action": "DeleteStack"}),
    ]
    assistant = Message(role="assistant", content=tools)
    manager.storage.append(session.cwd, session.session_id, assistant)
    digest = canonical_digest([block.model_dump(mode="json") for block in tools])
    first_future = asyncio.get_running_loop().create_future()
    first_event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input=tools[0].input,
        tool_use_id=tools[0].id,
        response_future=first_future,
        continuation_frame={
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": digest,
            "orderedToolUseIds": ["tool-1", "tool-2"],
            "currentIndex": 0,
            "decisions": [
                {"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None},
                {"toolUseId": "tool-2", "state": "not_evaluated", "source": None, "deniedResult": None},
            ],
        },
        audit_context={"session_id": session.session_id, "cwd": session.cwd},
    )
    first_id = await manager.open_permission_request(
        session,
        {"toolName": "aliyun_api", "toolUseId": "tool-1", "message": "Allow first?"},
        permission_event=first_event,
        permission_class="normal",
    )
    await manager.resolve_durable_permission(first_id, {"choice": "allow_once"}, session_id=session.session_id)
    first_boundary = session.pending_permissions[first_id].boundary_id
    assert first_boundary is not None

    second_future = asyncio.get_running_loop().create_future()
    second_event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input=tools[1].input,
        tool_use_id=tools[1].id,
        response_future=second_future,
        continuation_frame={
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": digest,
            "orderedToolUseIds": ["tool-1", "tool-2"],
            "currentIndex": 1,
            "decisions": [
                {
                    "toolUseId": "tool-1",
                    "state": "allow",
                    "source": "user",
                    "principalRef": None,
                    "region": None,
                    "deniedResult": None,
                },
                {"toolUseId": "tool-2", "state": "pending", "source": None, "deniedResult": None},
            ],
            "previousBoundaryId": first_boundary,
        },
        audit_context={"session_id": session.session_id, "cwd": session.cwd},
    )
    second_id = await manager.open_permission_request(
        session,
        {"toolName": "aliyun_api", "toolUseId": "tool-2", "message": "Allow second?"},
        permission_event=second_event,
        permission_class="normal",
    )

    assert first_id not in session.pending_permissions
    assert second_id in session.pending_permissions
    assert manager.permission_wait_coordinator.has_live_boundary(first_boundary) is False
    receipt = manager.permission_checkpoint_store(session).load(first_boundary)
    assert receipt["phase"] == "RESOLVED"
    duplicate = await manager.resolve_durable_permission(
        first_id,
        {"choice": "allow_once"},
        session_id=session.session_id,
    )
    assert duplicate["duplicate"] is True
    assert duplicate["needsRecovery"] is False
