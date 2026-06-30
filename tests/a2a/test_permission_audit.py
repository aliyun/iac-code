"""Permission audit coverage for A2A automatic boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from iac_code.a2a.artifacts import A2AArtifactStore
from iac_code.a2a.events import publish_stream_event
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator, safe_permission_metadata
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings
from iac_code.types.stream_events import PermissionRequestEvent

from .fakes import FakeEventQueue


def _publisher(
    tmp_path: Path,
    *,
    artifact_store: A2AArtifactStore | None = None,
    exposure_types: object | None = None,
    a2a_artifacts_by_step_id: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[PipelineA2AEventPublisher, FakeEventQueue]:
    queue = FakeEventQueue()
    context = PipelineA2AContext(
        pipeline_run_id="run-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
        parent_step_order=["evaluate_candidates", "confirm_and_select"],
        candidate_step_order=["template_generating"],
        a2a_artifacts_by_step_id=a2a_artifacts_by_step_id or {},
    )
    pipeline_dir = tmp_path / "pipeline"
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(context),
        journal=A2APipelineJournal(pipeline_dir),
        snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
        artifact_store=artifact_store,
        exposure_types=exposure_types,
    )
    return publisher, queue


@pytest.mark.asyncio
async def test_stream_event_auto_approve_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = FakeEventQueue()
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append((record, settings)),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    settings = PermissionAuditSettings(include_tool_input=True)

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd", "apiKey": "secret-value"},
            tool_use_id="toolu-stream-approve",
            response_future=future,
            audit_context={
                "session_id": "session-a2a",
                "settings": settings,
                "metadata": PermissionAuditMetadata(
                    scope="settings_rule",
                    source="permission_pipeline",
                    rule_source="user_settings",
                    rule="bash(pwd)",
                    reason_type="rule",
                    reason_detail="matched ask rule: bash(pwd)",
                    is_read_only=False,
                    operation={"is_read_only": False},
                ),
            },
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is True
    assert len(audit_records) == 1
    record, settings_seen = audit_records[0]
    assert settings_seen is settings
    assert record.session_id == "session-a2a"
    assert record.source == "a2a_auto_approve"
    assert record.scope == "auto_approve"
    assert record.decision == "allow"
    assert record.rule_source == "user_settings"
    assert record.rule == "bash(pwd)"
    assert record.operation == {"is_read_only": False}
    assert record.tool_input_redacted == {
        "cmd": {"type": "str", "length": 3, "fingerprint": fingerprint_text("pwd")},
        fingerprint_text("apiKey"): {"redacted": True},
    }


@pytest.mark.asyncio
async def test_stream_event_auto_approve_denies_when_audit_log_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = FakeEventQueue()
    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", _raise_audit_write_error)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-stream-approve-audit-fail",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="settings_rule",
                    source="permission_pipeline",
                    rule_source="user_settings",
                    rule="bash(pwd)",
                    reason_type="rule",
                    reason_detail="matched allow rule: bash(pwd)",
                    is_read_only=False,
                    operation={"is_read_only": False},
                )
            },
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is False


@pytest.mark.asyncio
async def test_stream_event_auto_approve_denies_untrusted_aliyun_write(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = FakeEventQueue()
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="toolu-stream-write",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="permission_pipeline",
                    reason_type="untrusted_write",
                    reason_detail="untrusted Aliyun write",
                    is_read_only=False,
                    operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
                )
            },
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is False
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "a2a_auto_deny"
    assert record.scope == "auto_deny"
    assert record.decision == "deny"
    assert record.reason_type == "untrusted_write"


@pytest.mark.asyncio
async def test_stream_event_auto_deny_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = FakeEventQueue()
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "deploy"},
            tool_use_id="toolu-stream-deny",
            response_future=future,
        ),
        auto_approve_permissions=False,
    )

    assert future.result() is False
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "a2a_auto_deny"
    assert record.scope == "auto_deny"
    assert record.decision == "deny"


@pytest.mark.asyncio
async def test_stream_event_resolver_decision_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = FakeEventQueue()
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="toolu-stream-resolver",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="permission_pipeline",
                    reason_type="untrusted_write",
                    reason_detail="untrusted Aliyun write",
                    is_read_only=False,
                    operation={"product": "ros", "action": "CreateStack"},
                )
            },
        ),
        permission_resolver=lambda _request: True,
        auto_approve_permissions=False,
    )

    assert future.result() is True
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "a2a_resolver"
    assert record.scope == "a2a_resolver"
    assert record.decision == "allow"
    assert record.reason_type == "a2a_resolver"
    assert record.trigger_reason_type == "untrusted_write"


@pytest.mark.asyncio
async def test_stream_event_resolver_aliyun_write_denies_when_audit_log_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = FakeEventQueue()
    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", _raise_audit_write_error)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="toolu-stream-resolver-audit-fail",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="permission_pipeline",
                    reason_type="untrusted_write",
                    reason_detail="untrusted Aliyun write",
                    is_read_only=False,
                    operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
                )
            },
        ),
        permission_resolver=lambda _request: True,
        auto_approve_permissions=False,
    )

    assert future.result() is False


@pytest.mark.asyncio
async def test_stream_event_completed_future_is_not_auto_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = FakeEventQueue()
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    future.set_result(False)

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-stream-done",
            response_future=future,
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is False
    assert audit_records == []


@pytest.mark.asyncio
async def test_a2a_auto_approve_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publisher, _queue = _publisher(tmp_path)
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-approve",
            response_future=future,
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is True
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "a2a_auto_approve"
    assert record.scope == "auto_approve"
    assert record.decision == "allow"


@pytest.mark.asyncio
async def test_a2a_auto_approve_denies_when_audit_log_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _queue = _publisher(tmp_path)
    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", _raise_audit_write_error)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-approve-audit-fail",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="settings_rule",
                    source="permission_pipeline",
                    rule_source="user_settings",
                    rule="bash(pwd)",
                    reason_type="rule",
                    reason_detail="matched allow rule: bash(pwd)",
                    is_read_only=False,
                    operation={"is_read_only": False},
                )
            },
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is False
    permission = publisher.journal.read_all()[0]["permission"]
    assert permission["approved"] is False
    assert permission["decision"] == "deny"


@pytest.mark.asyncio
async def test_a2a_auto_deny_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publisher, _queue = _publisher(tmp_path)
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "deploy"},
            tool_use_id="toolu-deny",
            response_future=future,
        ),
        auto_approve_permissions=False,
    )

    assert future.result() is False
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "a2a_auto_deny"
    assert record.scope == "auto_deny"
    assert record.decision == "deny"


@pytest.mark.asyncio
async def test_a2a_resolver_decision_is_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _queue = _publisher(tmp_path)
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-pipeline-resolver",
            response_future=future,
        ),
        permission_resolver=lambda _request: False,
        auto_approve_permissions=True,
    )

    assert future.result() is False
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "a2a_resolver"
    assert record.scope == "a2a_resolver"
    assert record.decision == "deny"


@pytest.mark.asyncio
async def test_a2a_resolver_aliyun_write_denies_when_audit_log_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _queue = _publisher(tmp_path)
    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", _raise_audit_write_error)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="toolu-pipeline-resolver-audit-fail",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="permission_pipeline",
                    reason_type="untrusted_write",
                    reason_detail="untrusted Aliyun write",
                    is_read_only=False,
                    operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
                )
            },
        ),
        permission_resolver=lambda _request: True,
        auto_approve_permissions=False,
    )

    assert future.result() is False
    permission = publisher.journal.read_all()[0]["permission"]
    assert permission["approved"] is False
    assert permission["decision"] == "deny"


@pytest.mark.asyncio
async def test_a2a_auto_approve_persistence_failure_audits_applied_deny(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _queue = _publisher(tmp_path)
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def fail_read_all() -> list[dict[str, Any]]:
        raise OSError("read failed")

    publisher.journal.read_all_repairing_tail = fail_read_all  # type: ignore[method-assign]

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "deploy"},
            tool_use_id="toolu-persist-fail",
            response_future=future,
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is False
    assert [record.decision for record in audit_records] == ["allow", "deny"]
    assert audit_records[0].source == "a2a_auto_approve"
    assert audit_records[1].source == "a2a_auto_persistence_failure"
    assert audit_records[1].scope == "auto_deny"
    assert audit_records[1].reason_type == "persistence_failure"
    assert audit_records[1].reason_detail == "permission metadata persistence failed"


@pytest.mark.asyncio
async def test_a2a_resolver_aliyun_write_persistence_failure_audits_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _queue = _publisher(tmp_path)
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def fail_read_all() -> list[dict[str, Any]]:
        raise OSError("read failed")

    publisher.journal.read_all_repairing_tail = fail_read_all  # type: ignore[method-assign]

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="toolu-pipeline-resolver-persist-fail",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="permission_pipeline",
                    reason_type="untrusted_write",
                    reason_detail="untrusted Aliyun write",
                    is_read_only=False,
                    operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
                )
            },
        ),
        permission_resolver=lambda _request: True,
        auto_approve_permissions=False,
    )

    assert future.result() is False
    assert [record.decision for record in audit_records] == ["allow", "deny"]
    assert audit_records[1].source == "a2a_resolver_persistence_failure"
    assert audit_records[1].reason_type == "persistence_failure"
    assert audit_records[1].reason_detail == "permission metadata persistence failed"


@pytest.mark.asyncio
async def test_a2a_completed_future_is_not_auto_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publisher, _queue = _publisher(tmp_path)
    audit_records = []
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    future.set_result(False)

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-pipeline-done",
            response_future=future,
        ),
        auto_approve_permissions=True,
    )

    assert future.result() is False
    assert audit_records == []


def test_safe_permission_metadata_excludes_audit_context() -> None:
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"command": "pwd"},
        tool_use_id="toolu-safe",
    )
    event.audit_context = {"settings": {"max_file_bytes": 1}, "metadata": {"rule": "write_file"}}

    metadata: dict[str, Any] = safe_permission_metadata(event)

    assert "audit_context" not in metadata
    assert "settings" not in metadata
    assert "rule" not in metadata
    assert "settings" not in str(metadata)
    assert "rule" not in str(metadata)


def _raise_audit_write_error(*_args: Any, **_kwargs: Any) -> None:
    raise OSError("disk full")
