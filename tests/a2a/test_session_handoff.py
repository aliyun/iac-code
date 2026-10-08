from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from iac_code.a2a.app import create_app


def test_handoff_capability_does_not_claim_support_without_private_fence_and_shared_root(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_HANDOFF_SHARED_DIR", raising=False)
    app = create_app(host="127.0.0.1", port=41242, token="test-token", model="qwen3.6-plus")
    with TestClient(app) as client:
        unauthorized = client.get("/iac-code/handoff/capabilities")
        capability = client.get("/iac-code/handoff/capabilities", headers={"Authorization": "Bearer test-token"})
    assert unauthorized.status_code == 401
    assert capability.status_code == 200
    assert capability.json()["supported"] is False
    assert capability.json()["version"] == "session-handoff-v1"


def test_handoff_rejects_incomplete_identity_without_mutating_session(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_HANDOFF_SHARED_DIR", str(tmp_path / "shared"))
    app = create_app(host="127.0.0.1", port=41242, token="test-token", model="qwen3.6-plus")
    with TestClient(app) as client:
        result = client.post(
            "/iac-code/handoff/prepare",
            headers={"Authorization": "Bearer test-token"},
            json={"sessionId": "session-1", "migrationId": "migration-1", "epoch": 2},
        )
    assert result.status_code == 400
    assert not (Path(tmp_path) / "shared").exists()


def test_runtime_fence_never_reauthorizes_source_after_quiesce(monkeypatch, tmp_path):
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence

    fence = SessionWriterFence(tmp_path / "control")
    keys = ["context:ctx-1", "thread:thread-1"]
    assert fence.prepare(keys, "migration-1", 2)
    with pytest.raises(HandoffFrozenError):
        with fence.operation(keys[0], kind="input"):
            pytest.fail("Input was accepted while source was preparing")
    with fence.operation(keys[0]):
        assert not fence.quiesce(keys, "migration-1", 2)
        # Atomic quiesce must not stop the other key while one writer drains.
        with fence.operation(keys[1]):
            pass
    assert fence.quiesce(keys, "migration-1", 2)
    with pytest.raises(HandoffFrozenError):
        fence.authorize(keys, "migration-1", 2, "ros-commit-1")
    with pytest.raises(HandoffFrozenError):
        with fence.operation(keys[0]):
            pytest.fail("Source writer survived permanent revocation")


@pytest.mark.asyncio
async def test_input_wait_handoff_restores_only_after_authorization_and_keeps_agui_checkpoint(monkeypatch, tmp_path):
    from iac_code.a2a.handoff import (
        HandoffAuthorizeRequest,
        HandoffPrepareRequest,
        HandoffRestoreRequest,
        PhysicalExecutionIdentity,
        SessionHandoffService,
    )
    from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
    from iac_code.agui.state import FileAguiThreadStateStore
    from iac_code.services.handoff_fence import HandoffFrozenError
    from iac_code.services.session_metadata import SessionMetadata, write_session_metadata
    from iac_code.services.session_storage import SessionStorage
    from iac_code.utils.state_io import atomic_write_json

    source_config, target_config = tmp_path / "source", tmp_path / "target"
    shared = tmp_path / "shared"
    cwd = tmp_path / "workspace" / "session-1"
    cwd.mkdir(parents=True)
    (cwd / "template.yaml").write_text("Resources: {}\n", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(source_config))
    monkeypatch.setenv("IAC_CODE_HANDOFF_SHARED_DIR", str(shared))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "legacy-shared"))
    source_storage = SessionStorage(projects_dir=source_config / "projects")
    session_dir = source_storage.session_dir(str(cwd), "internal-1")
    write_session_metadata(session_dir, SessionMetadata(session_id="internal-1", cwd=str(cwd), layout_version=2))
    (session_dir / "session.jsonl").write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    (session_dir / "permission.json").write_text('{"deadline":1900000000,"appliedDigest":"digest-1"}', encoding="utf-8")
    from iac_code.services.session_backup import SessionBackupService
    from iac_code.services.session_backup_state import SessionBackupState

    legacy = SessionBackupService(source_storage)
    legacy._write_state(
        session_dir,
        SessionBackupState(
            session_id="internal-1",
            generation=7,
            parent_generation=6,
            commit_id="legacy-commit-7",
            status="succeeded",
            reason="permission_pending",
            updated_at="2026-10-08T00:00:00Z",
            writer_id="source-writer",
            publication_proofs={},
        ),
    )
    persistence = A2APersistenceStore(source_config / "a2a")
    persistence.save_context(
        A2AContextSnapshot(context_id="ctx-1", session_id="internal-1", cwd=str(cwd), active_task_id="task-1")
    )
    persistence.save_task(
        A2ATaskSnapshot(
            task_id="task-1", context_id="ctx-1", state="input-required", expected_permission_backup_generation=7
        )
    )
    snapshot = {
        "contextId": "ctx-1",
        "taskId": "task-1",
        "executionId": "exec-a2a-1",
        "revision": 9,
        "inputHandoffReady": True,
        "externalOperations": [],
        "releaseReady": False,
    }
    atomic_write_json(source_config / "a2a" / "execution-control" / "ctx-1.json", snapshot)
    state = {
        "schemaVersion": 1,
        "threadId": "thread-1",
        "contextId": "ctx-1",
        "iacCodeSessionId": "internal-1",
        "cwd": str(cwd),
        "userId": "user-1",
        "execution": {
            "taskId": "task-1",
            "executionId": "exec-agui-1",
            "rosInvocationId": "invocation-1",
            "pending": {"input-1": {"deadline": 1900000000}},
        },
        "appliedResumeDigests": [{"digest": "digest-1"}],
        "runDigests": {"run-1": "digest-run-1"},
    }
    source_agui = FileAguiThreadStateStore(source_config / "agui")
    source_agui.save_thread("thread-1", state)
    task = SimpleNamespace(context_id="ctx-1", state="input-required", expected_permission_backup_generation=7)
    context = SimpleNamespace(session_id="internal-1", cwd=str(cwd))

    class Store:
        _tasks = {"task-1": task}

        async def get_task_record(self, task_id):
            assert task_id == "task-1"
            return task

        async def get_context_record(self, context_id):
            assert context_id == "ctx-1"
            return context

    controls = SimpleNamespace(get_for_context=lambda _: None)
    service = SessionHandoffService(
        task_store=Store(),
        controls=controls,
        persistence_root=source_config / "a2a",
        storage=source_storage,
        agui_store=source_agui,
    )
    request = HandoffPrepareRequest(
        user_id="user-1",
        backend_scope="acs",
        session_id="session-1",
        cwd=str(cwd),
        internal_session_id="internal-1",
        context_id="ctx-1",
        task_id="task-1",
        protocol="agui",
        mode="pipeline",
        source=PhysicalExecutionIdentity(
            sandbox_id="sandbox-a", activation_id="activation-a", run_id="run-1", lease_id="lease-a"
        ),
        migration_id="migration-1",
        epoch=2,
        guidance_id="guide-1",
        input_digest="d" * 64,
        pending_input={"query": "continue"},
        agui_identity={"threadId": "thread-1", "executionId": "exec-agui-1", "rosInvocationId": "invocation-1"},
    )
    service.fence.bind_owner("context:ctx-1", request.source.model_dump(mode="json", by_alias=True))
    prepared = await service.prepare_handoff(request)
    assert prepared.status == "PREPARED"
    assert prepared.receipt.permission_generation == 7
    assert prepared.receipt.recovery_kind == "input_required"
    rebound_request = request.model_copy(update={"migration_id": "migration-2", "epoch": 3})
    from iac_code.a2a.handoff import HandoffDiscoverRequest

    rebound = await service.discover_completed_session(
        HandoffDiscoverRequest(request=rebound_request, checkpoint=prepared.receipt)
    )
    assert rebound.status == "PREPARED"
    assert rebound.receipt.migration_id == "migration-2"
    assert rebound.receipt.source_checkpoint_commit_id == prepared.receipt.commit_id
    assert rebound.receipt.business_revision == prepared.receipt.business_revision
    with pytest.raises(HandoffFrozenError):
        persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    with pytest.raises(Exception):
        source_agui.save_thread("thread-1", state)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target_config))
    target_storage = SessionStorage(projects_dir=target_config / "projects")
    target_agui = FileAguiThreadStateStore(target_config / "agui")
    destination = SessionHandoffService(
        task_store=Store(),
        controls=controls,
        persistence_root=target_config / "a2a",
        storage=target_storage,
        agui_store=target_agui,
    )
    target = PhysicalExecutionIdentity(
        sandbox_id="sandbox-b", activation_id="activation-b", run_id="run-1", lease_id="lease-b"
    )
    restored = await destination.restore_handoff(HandoffRestoreRequest(receipt=prepared.receipt, target=target))
    assert restored.status == "RESTORED"
    assert not target_storage.session_dir(str(cwd), "internal-1").exists()
    assert target_agui.load_thread("thread-1") is None
    with pytest.raises(HandoffFrozenError):
        with destination.fence.operation("context:ctx-1", kind="input"):
            pytest.fail("Prepared target executed before ROS commit")
    authorized = await destination.authorize_destination(
        HandoffAuthorizeRequest(receipt=prepared.receipt, target=target, commit_id="ros-commit-1")
    )
    assert authorized.status == "AUTHORIZED"
    shared_session = tmp_path / "legacy-shared" / "projects" / session_dir.parent.name / "internal-1"
    lineage = legacy._read_state(shared_session, session_id="internal-1", shared=True, missing_ok=True)
    assert lineage is not None
    assert (lineage.generation, lineage.commit_id) == (7, "legacy-commit-7")
    assert target_agui.load_thread("thread-1") == state
    from iac_code.a2a.handoff import HandoffInputRequest

    ack = await destination.apply_pending_input(
        HandoffInputRequest(
            receipt=prepared.receipt, target=target, input_digest="d" * 64, pending_input={"query": "continue"}
        )
    )
    assert ack.status == "NOT_READY"
    assert ack.reason == "agui_input_delivery_required"
    assert prepared.receipt.input_acceptance == "NOT_APPLIED_CONFIRMED"
    assert (target_storage.session_dir(str(cwd), "internal-1") / "permission.json").read_text(
        encoding="utf-8"
    ) == '{"deadline":1900000000,"appliedDigest":"digest-1"}'
    assert (
        await destination.authorize_destination(
            HandoffAuthorizeRequest(receipt=prepared.receipt, target=target, commit_id="ros-commit-1")
        )
    ).status == "AUTHORIZED"
    with destination.fence.operation("context:ctx-1", kind="input"):
        pass

    # The second owner cleans up at its ROS owner epoch. The private writer
    # revocation counter has already advanced farther and must not leak here.
    second_counter = destination.fence.epoch("context:ctx-1")
    old_target_agui = destination.agui_store
    old_target_agui.load_thread("thread-1")
    cleanup = request.model_copy(update={"source": target, "migration_id": "release-owner-2", "epoch": 2})
    released = await destination.prepare_handoff(cleanup)
    assert released.status == "PREPARED"
    assert released.receipt.epoch == 2
    admission = cleanup.model_copy(update={"migration_id": "transfer-owner-3", "epoch": 3})
    third = await destination.discover_completed_session(
        HandoffDiscoverRequest(request=admission, checkpoint=released.receipt)
    )
    assert third.status == "PREPARED"
    assert third.receipt.epoch == 3
    third_owner = target.model_copy(update={"activation_id": "activation-c", "lease_id": "lease-c"})
    assert (
        await destination.restore_handoff(HandoffRestoreRequest(receipt=third.receipt, target=third_owner))
    ).status == "RESTORED"
    assert (
        await destination.authorize_destination(
            HandoffAuthorizeRequest(receipt=third.receipt, target=third_owner, commit_id="ros-commit-3")
        )
    ).status == "AUTHORIZED"
    assert destination.fence.epoch("context:ctx-1") > second_counter
    with pytest.raises(Exception):
        old_target_agui.save_thread("thread-1", state)
    destination.agui_store.save_thread("thread-1", state)


def test_quiesced_session_rejects_late_sidecar_write(monkeypatch, tmp_path):
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence
    from iac_code.services.session_mutation_guard import session_mutation_guard

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_HANDOFF_SHARED_DIR", str(tmp_path / "shared"))
    directory = tmp_path / "session"
    fence = SessionWriterFence()
    keys = [fence.session_key(directory)]
    assert fence.prepare(keys, "migration-1", 2)
    assert fence.quiesce(keys, "migration-1", 2)
    with pytest.raises(HandoffFrozenError):
        with session_mutation_guard(directory):
            (directory / "late.json").write_text("late", encoding="utf-8")
    assert not (directory / "late.json").exists()


@pytest.mark.asyncio
async def test_preparing_source_rejects_execution_admission_even_when_prior_task_is_terminal(monkeypatch, tmp_path):
    from iac_code.a2a.execution_control import ExecutionControlService
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_HANDOFF_SHARED_DIR", str(tmp_path / "shared"))
    fence = SessionWriterFence()
    assert fence.prepare(["context:ctx-1"], "migration-1", 2)
    controls = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=None)
    with pytest.raises(HandoffFrozenError):
        await controls.begin_execution(context_id="ctx-1", task_id="new-task", owner="test", cwd=str(tmp_path))
    assert controls.get_for_context("ctx-1") is None


@pytest.mark.asyncio
async def test_preparing_source_rejects_agui_admission_before_any_run_digest_is_written(monkeypatch, tmp_path):
    from ag_ui.core import RunAgentInput

    from iac_code.agui.adapter import AguiA2AAdapter
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_HANDOFF_SHARED_DIR", str(tmp_path / "shared"))
    fence = SessionWriterFence()
    assert fence.prepare(["thread:thread-1"], "migration-1", 2)
    adapter = AguiA2AAdapter(a2a_url="http://127.0.0.1:41242", state_dir=tmp_path / "agui")
    run_input = RunAgentInput(
        thread_id="thread-1",
        run_id="run-new",
        messages=[],
        tools=[],
        context=[],
        state={},
        forwarded_props={
            "iac_code": {
                "schemaVersion": 1,
                "cwd": str(tmp_path),
                "userId": "user-1",
                "rosInvocationId": "invocation-new",
            }
        },
    )
    try:
        with pytest.raises(HandoffFrozenError):
            await adapter.admit(run_input, "digest-new")
        assert adapter._state_store.load_thread("thread-1") is None
    finally:
        await adapter.aclose()


def test_old_persistence_writer_cannot_write_after_same_runtime_new_activation(monkeypatch, tmp_path):
    from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_HANDOFF_SHARED_DIR", str(tmp_path / "shared"))
    store = A2APersistenceStore(tmp_path / "a2a")
    snapshot = A2AContextSnapshot(context_id="ctx-1", session_id="internal-1", cwd=str(tmp_path))
    store.save_context(snapshot)
    fence = SessionWriterFence()
    assert fence.prepare(["context:ctx-1"], "migration-1", 2)
    assert fence.quiesce(["context:ctx-1"], "migration-1", 2)
    fence.wait_for_commit(["context:ctx-1"], "migration-1", 2)
    fence.authorize(["context:ctx-1"], "migration-1", 2, "ros-commit-1")
    with pytest.raises(HandoffFrozenError):
        store.save_context(snapshot)
    A2APersistenceStore(tmp_path / "a2a").save_context(snapshot)


@pytest.mark.asyncio
async def test_same_runtime_activation_discards_old_task_cache_without_renewing_old_writer(monkeypatch, tmp_path):
    from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.a2a.types import A2AContextRecord, A2ATaskRecord
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_HANDOFF_SHARED_DIR", str(tmp_path / "shared"))
    old = A2APersistenceStore(tmp_path / "a2a")
    old.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="internal-1", cwd=str(tmp_path)))
    old.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    store = A2ATaskStore(persistence=old)
    await store.bind_context_llm_headers("ctx-1", {"Authorization": "Bearer fake-old-caller"})
    store._contexts["ctx-1"] = A2AContextRecord(context_id="ctx-1", session_id="internal-1", cwd=str(tmp_path))
    store._tasks["task-1"] = A2ATaskRecord(task_id="task-1", context_id="ctx-1", state="working")
    fence = SessionWriterFence()
    assert fence.prepare(["context:ctx-1"], "migration-1", 2)
    assert fence.quiesce(["context:ctx-1"], "migration-1", 2)
    fence.wait_for_commit(["context:ctx-1"], "migration-1", 2)
    await store.activate_handoff_snapshot(context_id="ctx-1", task_id="task-1")
    assert await store.resolve_context_llm_headers("ctx-1", None) == {}
    fence.authorize(["context:ctx-1"], "migration-1", 2, "commit-1")
    assert (await store.get_task_record("task-1")).state == "input-required"
    with pytest.raises(HandoffFrozenError):
        old.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    store._persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))


def test_same_sandbox_third_owner_uses_private_counter_without_changing_ros_epoch(tmp_path):
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence

    fence = SessionWriterFence(tmp_path / "control")
    keys = ["context:ctx-1"]
    assert fence.prepare(keys, "release-owner-1", 1)
    assert fence.quiesce(keys, "release-owner-1", 1)
    fence.wait_for_commit(keys, "transfer-owner-2", 2)
    fence.authorize(keys, "transfer-owner-2", 2, "commit-owner-2")
    second_counter = fence.epoch(keys[0])
    assert fence.prepare(keys, "release-owner-2", 2)
    with fence.operation(keys[0], actor_epoch=second_counter):
        pass
    assert fence.quiesce(keys, "release-owner-2", 2)
    fence.wait_for_commit(keys, "transfer-owner-3", 3)
    fence.authorize(keys, "transfer-owner-3", 3, "commit-owner-3")
    assert fence.epoch(keys[0]) > second_counter
    with pytest.raises(HandoffFrozenError):
        with fence.operation(keys[0], actor_epoch=second_counter):
            pytest.fail("Second owner wrote into the third owner")


@pytest.mark.parametrize(
    "field",
    ["epoch", "backup_generation", "business_revision", "permission_generation", "source_quiesced", "shared_committed"],
)
def test_migration_receipt_rejects_boolean_counters(field):
    from pydantic import ValidationError

    from iac_code.a2a.handoff import MigrationReceipt

    payload = dict(
        user_id="user-1",
        backend_scope="acs",
        session_id="session-1",
        cwd="/workspace/session-1",
        internal_session_id="internal-1",
        context_id="ctx-1",
        task_id="task-1",
        protocol="a2a",
        mode="normal",
        source=dict(sandbox_id="sandbox-1", activation_id="activation-1", run_id="run-1", lease_id="lease-1"),
        migration_id="migration-1",
        epoch=2,
        commit_id="commit-1",
        manifest_digest="a" * 64,
        backup_generation=2,
        business_revision=0,
        source_quiesced=True,
        shared_committed=True,
        recovery_kind="terminal",
    )
    payload[field] = 1 if field in {"source_quiesced", "shared_committed"} else True
    with pytest.raises(ValidationError):
        MigrationReceipt.model_validate(payload)


@pytest.mark.parametrize("field", ["source_quiesced", "shared_committed"])
def test_migration_receipt_requires_explicit_proof(field):
    from pydantic import ValidationError

    from iac_code.a2a.handoff import MigrationReceipt

    payload = dict(
        user_id="user-1",
        backend_scope="acs",
        session_id="session-1",
        cwd="/workspace/session-1",
        internal_session_id="internal-1",
        context_id="ctx-1",
        task_id="task-1",
        protocol="a2a",
        mode="normal",
        source=dict(sandbox_id="sandbox-1", activation_id="activation-1", run_id="run-1", lease_id="lease-1"),
        migration_id="migration-1",
        epoch=2,
        commit_id="commit-1",
        manifest_digest="a" * 64,
        backup_generation=2,
        business_revision=0,
        source_quiesced=True,
        shared_committed=True,
        recovery_kind="terminal",
    )
    payload.pop(field)
    with pytest.raises(ValidationError):
        MigrationReceipt.model_validate(payload)


@pytest.mark.parametrize(
    "invalid",
    [
        {"phase": "BROKEN"},
        {"epoch": True},
        {"writerCounter": -1},
        {"writerCounter": "0"},
        {"phase": "PREPARING", "sourceCounter": True},
        {"phase": "PREPARING", "sourceCounter": 4},
    ],
)
def test_corrupt_private_writer_fence_never_permits_mutation(tmp_path, invalid):
    from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence
    from iac_code.utils.state_io import atomic_write_json

    fence = SessionWriterFence(tmp_path / "control")
    key = "context:ctx-1"
    path, _ = fence._paths(key)
    atomic_write_json(path, {"phase": "ACTIVE", "epoch": 0, "writerCounter": 0, "operations": {}} | invalid)
    with pytest.raises(HandoffFrozenError):
        with fence.operation(key):
            pytest.fail("A corrupt private grant allowed a business mutation")
