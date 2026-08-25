from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from iac_code.agent.message import Message, ToolUseBlock
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    PermissionWaitCoordinator,
    PermissionWaitPolicy,
    build_permission_checkpoint,
    canonical_digest,
    canonicalize_permission_continuation_frame,
    format_utc,
    permission_execution_identity,
    recover_permission_audit_boundary,
    utc_now,
)
from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
from iac_code.services.session_layout import SessionPaths
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import PermissionWaitOutcome


def _store(tmp_path) -> PermissionWaitCheckpointStore:
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    storage.ensure_v2_session_dir_for_new_session("/workspace", "session-1")
    return PermissionWaitCheckpointStore("/workspace", "session-1", storage=storage)


def _record(store: PermissionWaitCheckpointStore, policy: PermissionWaitPolicy, **overrides):
    values = {
        "session_id": "session-1",
        "task_id": "task-1",
        "context_id": "context-1",
        "input_id": "input-1",
        "tool_use_id": "tool-1",
        "tool_name": "aliyun_api",
        "tool_input": {"api": "CreateStack"},
        "permission_class": "normal",
        "continuation_frame": {
            "assistantMessageRef": "session.jsonl:1",
            "assistantMessageDigest": "a" * 64,
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None}],
        },
        "policy": policy,
    }
    values.update(overrides)
    record = build_permission_checkpoint(**values)
    return store.create(record)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, None, 30.0)),
        ({}, (None, None, 30.0)),
        (
            {
                "resident_timeout_seconds": 300,
                "sub_pipeline_timeout_seconds": 120.5,
                "timeout_grace_seconds": 0,
            },
            (300.0, 120.5, 0.0),
        ),
    ],
)
def test_permission_wait_policy_parsing(raw, expected) -> None:
    policy = PermissionWaitPolicy.from_config(raw)
    assert (
        policy.resident_timeout_seconds,
        policy.sub_pipeline_timeout_seconds,
        policy.timeout_grace_seconds,
    ) == expected


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"unknown": 1},
        {"resident_timeout_seconds": 0},
        {"resident_timeout_seconds": True},
        {"sub_pipeline_timeout_seconds": -1},
        {"timeout_grace_seconds": False},
        {"timeout_grace_seconds": float("inf")},
    ],
)
def test_permission_wait_policy_rejects_invalid_values(raw) -> None:
    with pytest.raises(ValueError):
        PermissionWaitPolicy.from_config(raw)


def test_pipeline_continuation_frame_is_bound_to_canonical_transcript() -> None:
    frame = {
        "assistantMessageRef": "session.jsonl:3",
        "assistantMessageDigest": "a" * 64,
    }

    canonical = canonicalize_permission_continuation_frame(
        frame,
        audit_context={"transcript_id": "transcript_att_0001"},
    )

    assert canonical["assistantMessageRef"] == "pipeline/transcripts/transcript_att_0001/session.jsonl:3"
    assert frame["assistantMessageRef"] == "session.jsonl:3"
    with pytest.raises(ValueError, match="transcript context"):
        canonicalize_permission_continuation_frame(
            canonical,
            audit_context={"transcript_id": "transcript_att_0002"},
        )


def test_recover_permission_audit_boundary_reads_exact_pipeline_transcript(tmp_path) -> None:
    cwd = "/workspace"
    session_id = "session-pipeline"
    transcript_id = "transcript_att_0001"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    root_session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, session_id)
    assert root_session_dir is not None
    transcript_storage = PipelineTranscriptStorage(root_session_dir / "pipeline")
    transcript_storage.append(cwd, transcript_id, Message(role="user", content="deploy"))
    tool_uses = [
        ToolUseBlock(id="tool-read", name="aliyun_api", input={"action": "DescribeVSwitches"}),
        ToolUseBlock(id="tool-write", name="aliyun_api", input={"action": "CreateStack"}),
    ]
    assistant = Message(role="assistant", content=tool_uses)
    transcript_storage.append(cwd, transcript_id, assistant)
    frame = {
        "assistantMessageRef": f"pipeline/transcripts/{transcript_id}/session.jsonl:1",
        "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
        "orderedToolUseIds": [tool_use.id for tool_use in tool_uses],
        "currentIndex": 1,
        "decisions": [
            {"toolUseId": "tool-read", "state": "allow", "source": "policy"},
            {"toolUseId": "tool-write", "state": "pending", "source": None},
        ],
    }
    record = build_permission_checkpoint(
        session_id=session_id,
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id="tool-write",
        tool_name="aliyun_api",
        tool_input=tool_uses[1].input,
        permission_class="pipeline",
        continuation_frame=frame,
        policy=PermissionWaitPolicy(),
    )

    recovered = recover_permission_audit_boundary(
        record,
        cwd=cwd,
        session_id=session_id,
        storage=storage,
    )

    assert recovered is not None
    assert recovered.tool_use_id == "tool-write"
    assert recovered.tool_input == {"action": "CreateStack"}
    assert recovered.audit_context == {
        "session_id": transcript_id,
        "cwd": cwd,
        "root_session_id": session_id,
        "transcript_id": transcript_id,
        "audit_log_path": str(
            SessionPaths.require_supported(root_session_dir).transcript_permission_audit_path(transcript_id)
        ),
    }

    changed = {**record, "payloadDigest": "f" * 64}
    assert (
        recover_permission_audit_boundary(
            changed,
            cwd=cwd,
            session_id=session_id,
            storage=storage,
        )
        is None
    )


def test_recover_permission_audit_boundary_rejects_symlinked_transcript_parent(tmp_path) -> None:
    cwd = "/workspace"
    session_id = "session-pipeline-link"
    transcript_id = "transcript_link"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    root_session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, session_id)
    assert root_session_dir is not None
    outside_storage = PipelineTranscriptStorage(tmp_path / "outside" / "pipeline")
    tool_use = ToolUseBlock(id="tool-write", name="aliyun_api", input={"action": "CreateStack"})
    assistant = Message(role="assistant", content=[tool_use])
    outside_storage.append(cwd, transcript_id, assistant)
    outside_dir = outside_storage.session_dir(cwd, transcript_id)
    transcripts_dir = root_session_dir / "pipeline" / "transcripts"
    transcripts_dir.mkdir(parents=True)
    try:
        (transcripts_dir / transcript_id).symlink_to(outside_dir, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    record = build_permission_checkpoint(
        session_id=session_id,
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id=tool_use.id,
        tool_name=tool_use.name,
        tool_input=tool_use.input,
        permission_class="pipeline",
        continuation_frame={
            "assistantMessageRef": f"pipeline/transcripts/{transcript_id}/session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": [tool_use.id],
            "currentIndex": 0,
            "decisions": [{"toolUseId": tool_use.id, "state": "pending", "source": None}],
        },
        policy=PermissionWaitPolicy(),
    )

    assert (
        recover_permission_audit_boundary(
            record,
            cwd=cwd,
            session_id=session_id,
            storage=storage,
        )
        is None
    )


@pytest.mark.parametrize("pipeline", [False, True])
def test_recover_permission_audit_boundary_rejects_matching_non_tail_message(tmp_path, pipeline) -> None:
    cwd = "/workspace"
    session_id = "session-non-tail"
    transcript_id = "transcript_att_0001"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    root_session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, session_id)
    assert root_session_dir is not None
    tool_use = ToolUseBlock(id="tool-write", name="write_file", input={"path": "template.yml"})
    assistant = Message(role="assistant", content=[tool_use])
    if pipeline:
        transcript_storage = PipelineTranscriptStorage(root_session_dir / "pipeline")
        transcript_storage.append(cwd, transcript_id, assistant)
        transcript_storage.append(cwd, transcript_id, Message(role="user", content="later input"))
        message_ref = f"pipeline/transcripts/{transcript_id}/session.jsonl:0"
        permission_class = "pipeline"
    else:
        storage.append(cwd, session_id, assistant)
        storage.append(cwd, session_id, Message(role="user", content="later input"))
        message_ref = "session.jsonl:0"
        permission_class = "normal"
    record = build_permission_checkpoint(
        session_id=session_id,
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id=tool_use.id,
        tool_name=tool_use.name,
        tool_input=tool_use.input,
        permission_class=permission_class,
        continuation_frame={
            "assistantMessageRef": message_ref,
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": [tool_use.id],
            "currentIndex": 0,
            "decisions": [{"toolUseId": tool_use.id, "state": "pending", "source": None}],
        },
        policy=PermissionWaitPolicy(),
    )

    assert (
        recover_permission_audit_boundary(
            record,
            cwd=cwd,
            session_id=session_id,
            storage=storage,
        )
        is None
    )


def test_checkpoint_claim_is_idempotent_and_conflict_fails(tmp_path) -> None:
    store = _store(tmp_path)
    record = _record(store, PermissionWaitPolicy())

    claimed, created = store.claim_decision(record["boundaryId"], value="allow_once", source="user")
    duplicate, duplicate_created = store.claim_decision(record["boundaryId"], value="allow_once", source="user")

    assert created is True
    assert duplicate_created is False
    assert duplicate["decision"] == claimed["decision"]
    assert claimed["decision"]["auditStatus"] == "pending"
    with pytest.raises(ValueError, match="conflicts"):
        store.claim_decision(record["boundaryId"], value="deny", source="user")


def test_cross_process_store_serializes_one_authoritative_claim_audit(tmp_path) -> None:
    projects = tmp_path / "projects"
    storage = SessionStorage(projects_dir=projects)
    storage.ensure_v2_session_dir_for_new_session("/workspace", "session-1")
    first_store = PermissionWaitCheckpointStore("/workspace", "session-1", storage=storage)
    record = _record(first_store, PermissionWaitPolicy())
    boundary_id = record["boundaryId"]
    claimed, _created = first_store.claim_decision(boundary_id, value="allow_once", source="user")
    claim_id = str(claimed["decision"]["claimId"])
    second_store = PermissionWaitCheckpointStore(
        "/workspace",
        "session-1",
        storage=SessionStorage(projects_dir=projects),
    )
    audit_started = threading.Event()
    release_audit = threading.Event()
    calls: list[str] = []

    def failing_audit(_value: str) -> bool:
        calls.append("owner-failed")
        audit_started.set()
        assert release_audit.wait(timeout=2)
        return False

    def competing_success(_value: str) -> bool:
        calls.append("duplicate-succeeded")
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            first_store.run_claim_audit_once,
            boundary_id,
            claim_id=claim_id,
            audit=failing_audit,
        )
        assert audit_started.wait(timeout=2)
        duplicate = pool.submit(
            second_store.run_claim_audit_once,
            boundary_id,
            claim_id=claim_id,
            audit=competing_success,
        )
        release_audit.set()
        owner_record, owner_created = owner.result(timeout=2)
        duplicate_record, duplicate_created = duplicate.result(timeout=2)

    assert calls == ["owner-failed"]
    assert owner_created is True
    assert duplicate_created is False
    assert owner_record["decision"]["value"] == "deny"
    assert duplicate_record["decision"] == owner_record["decision"]
    assert second_store.load(boundary_id)["decision"]["auditStatus"] == "failed"


def test_checkpoint_rejects_continuation_without_exactly_one_current_pending_tool(tmp_path) -> None:
    store = _store(tmp_path)
    record = build_permission_checkpoint(
        session_id="session-1",
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id="tool-1",
        tool_name="aliyun_api",
        tool_input={"api": "CreateStack"},
        permission_class="normal",
        continuation_frame={
            "assistantMessageRef": "session.jsonl:1",
            "assistantMessageDigest": "a" * 64,
            "orderedToolUseIds": ["tool-1", "tool-2"],
            "currentIndex": 0,
            "decisions": [
                {"toolUseId": "tool-1", "state": "pending", "source": None},
                {"toolUseId": "tool-2", "state": "pending", "source": None},
            ],
        },
        policy=PermissionWaitPolicy(),
    )

    with pytest.raises(ValueError, match="pending boundary"):
        store.create(record)


def test_checkpoint_rejects_noncanonical_continuation_message_reference(tmp_path) -> None:
    store = _store(tmp_path)
    record = build_permission_checkpoint(
        session_id="session-1",
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id="tool-1",
        tool_name="aliyun_api",
        tool_input={"api": "CreateStack"},
        permission_class="normal",
        continuation_frame={
            "assistantMessageRef": "message-1",
            "assistantMessageDigest": "a" * 64,
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None}],
        },
        policy=PermissionWaitPolicy(),
    )

    with pytest.raises(ValueError, match="message reference"):
        store.create(record)


@pytest.mark.parametrize(
    ("permission_class", "mode", "message_ref", "error"),
    [
        ("normal", "pipeline", "session.jsonl:0", "checkpoint class"),
        ("sub_pipeline", "normal", "session.jsonl:0", "checkpoint class"),
        ("normal", "normal", "pipeline/transcripts/transcript-1/session.jsonl:0", "transcript class"),
        ("pipeline", "pipeline", "session.jsonl:0", "transcript class"),
    ],
)
def test_checkpoint_binds_permission_class_mode_and_transcript(
    tmp_path,
    permission_class,
    mode,
    message_ref,
    error,
) -> None:
    store = _store(tmp_path)
    record = build_permission_checkpoint(
        session_id="session-1",
        task_id="task-1",
        context_id="context-1",
        input_id="input-1",
        tool_use_id="tool-1",
        tool_name="aliyun_api",
        tool_input={"api": "CreateStack"},
        permission_class="normal",
        continuation_frame={
            "assistantMessageRef": message_ref,
            "assistantMessageDigest": "a" * 64,
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None}],
        },
        policy=PermissionWaitPolicy(),
    )
    record["permissionClass"] = permission_class
    record["mode"] = mode

    with pytest.raises(ValueError, match=error):
        store.create(record)


def test_cloud_execution_identity_is_non_secret_and_region_bound(monkeypatch) -> None:
    credential = AliyunCredential(
        mode="StsToken",
        access_key_id="sts-access-key-id",
        access_key_secret="must-not-be-persisted",
        sts_token="must-not-be-persisted-either",
        region_id="cn-hangzhou",
    )
    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(lambda: credential))

    principal_ref, region = permission_execution_identity(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack", "region_id": "cn-shanghai"},
    )

    assert principal_ref is not None and principal_ref.startswith("aliyun:")
    assert "sts-access-key-id" not in principal_ref
    assert "must-not-be-persisted" not in principal_ref
    assert region == "cn-shanghai"


def test_local_execution_identity_does_not_depend_on_cloud_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        AliyunCredentials,
        "load",
        staticmethod(lambda: pytest.fail("local permissions must not read cloud credentials")),
    )

    assert permission_execution_identity(tool_name="bash", tool_input={"cmd": "pwd"}) == (None, None)


def test_checkpoint_normalizes_orphan_and_compacts_receipt(tmp_path) -> None:
    store = _store(tmp_path)
    record = _record(store, PermissionWaitPolicy())
    suspended = store.reconcile_deadline(
        record["boundaryId"],
        grace_seconds=30,
        live_owner=False,
    )
    assert suspended["phase"] == "SUSPENDED"

    claimed, _ = store.claim_decision(record["boundaryId"], value="deny", source="user")
    restoring = store.begin_restore(record["boundaryId"])
    receipt = store.resolve(
        record["boundaryId"],
        result_digest="b" * 64,
        ack={"decision": "deny"},
    )

    assert claimed["decision"]["status"] == "claimed"
    assert restoring["phase"] == "RESTORING"
    assert receipt["phase"] == "RESOLVED"
    assert "continuationFrame" not in receipt
    assert receipt["ack"] == {"decision": "deny"}


def test_successor_boundary_atomically_replaces_active_owner_and_keeps_old_ack(tmp_path) -> None:
    store = _store(tmp_path)
    first = _record(store, PermissionWaitPolicy())
    first, _created = store.claim_decision(first["boundaryId"], value="allow_once", source="user")
    first = store.mark_claim_backed_up(first["boundaryId"], claim_id=first["decision"]["claimId"])
    store.mark_applied(first["boundaryId"], claim_id=first["decision"]["claimId"])
    second = build_permission_checkpoint(
        session_id="session-1",
        task_id="task-1",
        context_id="context-1",
        input_id="input-2",
        tool_use_id="tool-2",
        tool_name="aliyun_api",
        tool_input={"api": "DeleteStack"},
        permission_class="normal",
        continuation_frame={
            "assistantMessageRef": "session.jsonl:1",
            "assistantMessageDigest": "b" * 64,
            "orderedToolUseIds": ["tool-1", "tool-2"],
            "currentIndex": 1,
            "decisions": [
                {
                    "toolUseId": "tool-1",
                    "state": "allow",
                    "source": "user",
                    "principalRef": None,
                    "region": None,
                },
                {"toolUseId": "tool-2", "state": "pending", "source": None},
            ],
        },
        policy=PermissionWaitPolicy(),
    )

    created = store.create_successor(second, previous_boundary_id=first["boundaryId"])

    assert [record["boundaryId"] for record in store.list_active()] == [created["boundaryId"]]
    receipt = store.load(first["boundaryId"])
    assert receipt["phase"] == "RESOLVED"
    assert receipt["nextBoundaryId"] == created["boundaryId"]
    assert receipt["ack"] == {
        "decision": "allow_once",
        "accepted": True,
        "nextBoundaryId": created["boundaryId"],
    }
    assert "continuationFrame" not in receipt


def test_deadline_reconciliation_does_not_steal_an_active_restore(tmp_path) -> None:
    store = _store(tmp_path)
    record = _record(store, PermissionWaitPolicy())
    boundary_id = record["boundaryId"]
    store.mark_suspended(boundary_id)
    store.claim_decision(boundary_id, value="allow_once", source="user")
    restoring = store.begin_restore(boundary_id)

    reconciled = store.reconcile_deadline(
        boundary_id,
        grace_seconds=30,
        live_owner=False,
    )

    assert reconciled == restoring
    assert reconciled["phase"] == "RESTORING"


def test_deadline_reconciliation_starts_absolute_grace_when_process_resumes(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=10, timeout_grace_seconds=30)
    created_at = utc_now() - timedelta(seconds=120)
    record = _record(store, policy, now=created_at)

    reconciled = store.reconcile_deadline(
        record["boundaryId"],
        now=created_at + timedelta(seconds=120),
        grace_seconds=policy.timeout_grace_seconds,
        live_owner=True,
    )

    assert reconciled["phase"] == "TIMEOUT_GRACE"
    assert reconciled["graceDeadlineAt"] == format_utc(created_at + timedelta(seconds=150))


def test_deadline_reconciliation_starts_full_grace_when_expiry_is_first_observed(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=10, timeout_grace_seconds=30)
    created_at = utc_now() - timedelta(seconds=20)
    record = _record(store, policy, now=created_at)

    reconciled = store.reconcile_deadline(
        record["boundaryId"],
        now=created_at + timedelta(seconds=20),
        grace_seconds=policy.timeout_grace_seconds,
        live_owner=True,
    )

    assert reconciled["phase"] == "TIMEOUT_GRACE"
    assert reconciled["graceDeadlineAt"] == format_utc(created_at + timedelta(seconds=50))


@pytest.mark.asyncio
async def test_live_reply_in_grace_wins_and_marks_applied(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=10, timeout_grace_seconds=30)
    record = _record(store, policy)
    boundary_id = record["boundaryId"]
    store.transaction(
        boundary_id,
        lambda value: {
            **value,
            "residentDeadlineAt": format_utc(utc_now() - timedelta(seconds=1)),
        },
    )
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=store.load(boundary_id) or record, store=store, future=future)

    claimed, created = await coordinator.claim_live(boundary_id=boundary_id, value="allow_once")

    assert created is True
    assert claimed["phase"] == "TIMEOUT_GRACE"
    assert await future is True
    assert store.load(boundary_id)["decision"]["status"] == "applied"


@pytest.mark.asyncio
async def test_live_reply_audits_before_future_delivery_and_records_result(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy()
    record = _record(store, policy)
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=record, store=store, future=future)
    observed: list[tuple[str, bool]] = []

    def audit(value: str) -> bool:
        observed.append((value, future.done()))
        return True

    await coordinator.claim_live(
        boundary_id=record["boundaryId"],
        value="allow_once",
        on_new_claim=audit,
    )

    assert observed == [("allow_once", False)]
    assert await future is True
    assert store.load(record["boundaryId"])["decision"]["auditStatus"] == "recorded"


@pytest.mark.asyncio
async def test_live_reply_commits_required_backup_before_future_delivery(tmp_path) -> None:
    store = _store(tmp_path)
    record = _record(store, PermissionWaitPolicy())
    boundary_id = record["boundaryId"]
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator()
    coordinator.register_live(record=record, store=store, future=future)
    observed: list[tuple[str, str, bool]] = []

    async def backup(_record: dict) -> None:
        decision = store.load(boundary_id)["decision"]
        observed.append((decision["status"], decision["backupStatus"], future.done()))

    await coordinator.claim_live(
        boundary_id=boundary_id,
        value="allow_once",
        before_delivery=backup,
    )

    assert observed == [("claimed", "pending", False)]
    assert await future is True
    decision = store.load(boundary_id)["decision"]
    assert decision["backupStatus"] == "committed"
    assert decision["status"] == "applied"


@pytest.mark.asyncio
async def test_canceled_reply_request_does_not_strand_durable_future_delivery(tmp_path) -> None:
    store = _store(tmp_path)
    record = _record(store, PermissionWaitPolicy())
    boundary_id = record["boundaryId"]
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator()
    coordinator.register_live(record=record, store=store, future=future)
    backup_started = asyncio.Event()
    release_backup = asyncio.Event()

    async def backup(_record: dict) -> None:
        backup_started.set()
        await release_backup.wait()

    reply = asyncio.create_task(
        coordinator.claim_live(
            boundary_id=boundary_id,
            value="allow_once",
            before_delivery=backup,
        )
    )
    await backup_started.wait()
    reply.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reply
    release_backup.set()

    assert await asyncio.wait_for(future, timeout=1) is True
    decision = store.load(boundary_id)["decision"]
    assert decision["backupStatus"] == "committed"
    assert decision["status"] == "applied"


@pytest.mark.asyncio
async def test_live_allow_audit_failure_is_durably_downgraded_before_delivery(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy()
    record = _record(store, policy)
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=record, store=store, future=future)

    await coordinator.claim_live(
        boundary_id=record["boundaryId"],
        value="allow_once",
        on_new_claim=lambda _value: False,
    )

    assert await future is False
    decision = store.load(record["boundaryId"])["decision"]
    assert decision["value"] == "deny"
    assert decision["status"] == "applied"
    assert decision["auditStatus"] == "failed"


@pytest.mark.asyncio
async def test_grace_expiry_suspends_without_denial(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=0.01, timeout_grace_seconds=0)
    record = _record(store, policy)
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=record, store=store, future=future)

    assert await asyncio.wait_for(future, timeout=1) is PermissionWaitOutcome.SUSPEND
    assert store.load(record["boundaryId"])["phase"] == "SUSPENDING"
    coordinator.unregister_live(record["boundaryId"])
    assert store.load(record["boundaryId"])["phase"] == "SUSPENDED"
    assert store.load(record["boundaryId"])["decision"]["status"] == "none"


@pytest.mark.asyncio
async def test_unexpected_resident_timer_cancellation_rearms_from_absolute_deadline(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=0.2, timeout_grace_seconds=0)
    record = _record(store, policy)
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=record, store=store, future=future)

    owner = coordinator._owners[record["boundaryId"]]
    assert owner.timer is not None
    owner.timer.cancel()

    assert await asyncio.wait_for(future, timeout=1) is PermissionWaitOutcome.SUSPEND
    assert store.load(record["boundaryId"])["phase"] == "SUSPENDING"
    coordinator.unregister_live(record["boundaryId"])
    assert store.load(record["boundaryId"])["phase"] == "SUSPENDED"


@pytest.mark.asyncio
async def test_duplicate_live_registration_keeps_original_generation_fenced_timer(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=0.01, timeout_grace_seconds=0.05)
    record = _record(store, policy)
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=record, store=store, future=future)

    for _ in range(100):
        if store.load(record["boundaryId"])["phase"] == "TIMEOUT_GRACE":
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("resident timer did not enter TIMEOUT_GRACE")

    coordinator.register_live(record=record, store=store, future=future)

    assert await asyncio.wait_for(future, timeout=1) is PermissionWaitOutcome.SUSPEND
    assert store.load(record["boundaryId"])["phase"] == "SUSPENDING"


@pytest.mark.asyncio
async def test_duplicate_live_registration_rejects_different_future(tmp_path) -> None:
    store = _store(tmp_path)
    record = _record(store, PermissionWaitPolicy())
    coordinator = PermissionWaitCoordinator(PermissionWaitPolicy())
    first: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    second: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator.register_live(record=record, store=store, future=first)

    with pytest.raises(RuntimeError, match="different live owner"):
        coordinator.register_live(record=record, store=store, future=second)


@pytest.mark.asyncio
async def test_resident_timer_retries_when_grace_callback_observes_unexpired_deadline(
    monkeypatch,
    tmp_path,
) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=0.01, timeout_grace_seconds=0.01)
    record = _record(store, policy)
    boundary_id = record["boundaryId"]
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    original_suspend_now = coordinator.suspend_now
    attempts = 0

    async def observe_early_grace_deadline(value: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return await original_suspend_now(value)

    monkeypatch.setattr(coordinator, "suspend_now", observe_early_grace_deadline)
    coordinator.register_live(record=record, store=store, future=future)

    assert await asyncio.wait_for(future, timeout=1) is PermissionWaitOutcome.SUSPEND
    assert attempts == 2
    assert store.load(boundary_id)["phase"] == "SUSPENDING"


@pytest.mark.asyncio
async def test_late_reply_waits_for_suspending_owner_before_recovery(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=0.01, timeout_grace_seconds=0)
    record = _record(store, policy)
    boundary_id = record["boundaryId"]
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=record, store=store, future=future)

    assert await asyncio.wait_for(future, timeout=1) is PermissionWaitOutcome.SUSPEND
    claimed, created = await coordinator.claim_live(boundary_id=boundary_id, value="allow_once")
    assert created is True
    assert claimed["phase"] == "SUSPENDING"
    waiter = asyncio.create_task(coordinator.wait_for_suspended_owner(boundary_id, timeout_seconds=1))
    await asyncio.sleep(0)
    assert waiter.done() is False

    coordinator.unregister_live(boundary_id)
    assert await waiter is True

    persisted = store.load(boundary_id)
    assert persisted["phase"] == "SUSPENDED"
    assert persisted["decision"]["status"] == "claimed"
    assert persisted["decision"]["value"] == "allow_once"


@pytest.mark.asyncio
async def test_slow_suspending_owner_is_not_reclassified_as_crashed(tmp_path) -> None:
    store = _store(tmp_path)
    policy = PermissionWaitPolicy(resident_timeout_seconds=0.01, timeout_grace_seconds=0)
    record = _record(store, policy)
    boundary_id = record["boundaryId"]
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(policy)
    coordinator.register_live(record=record, store=store, future=future)

    assert await asyncio.wait_for(future, timeout=1) is PermissionWaitOutcome.SUSPEND
    await coordinator.claim_live(boundary_id=boundary_id, value="allow_once")

    assert await coordinator.wait_for_suspended_owner(boundary_id, timeout_seconds=0.01) is False
    assert coordinator.has_live_boundary(boundary_id) is True
    assert store.load(boundary_id)["phase"] == "SUSPENDING"

    coordinator.unregister_live(boundary_id)
    assert await coordinator.wait_for_suspended_owner(boundary_id, timeout_seconds=0.01) is True
    assert store.load(boundary_id)["phase"] == "SUSPENDED"


@pytest.mark.asyncio
async def test_stale_live_owner_generation_cannot_deliver_suspend(tmp_path) -> None:
    store = _store(tmp_path)
    record = _record(store, PermissionWaitPolicy())
    boundary_id = record["boundaryId"]
    future: asyncio.Future[bool | PermissionWaitOutcome] = asyncio.get_running_loop().create_future()
    coordinator = PermissionWaitCoordinator(PermissionWaitPolicy())
    coordinator.register_live(record=record, store=store, future=future)
    store.transaction(
        boundary_id,
        lambda value: {
            **value,
            "phase": "SUSPENDING",
            "generation": int(value["generation"]) + 1,
        },
    )

    assert await coordinator.suspend_now(boundary_id) is False
    assert future.done() is False
    assert store.load(boundary_id)["phase"] == "SUSPENDING"
    coordinator.unregister_live(boundary_id)
