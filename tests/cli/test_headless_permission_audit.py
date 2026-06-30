"""Permission audit coverage for headless execution boundaries."""

from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, patch

import pytest

from iac_code.cli.headless import EXIT_OK, HeadlessRunner
from iac_code.cli.output_formats import OutputFormat
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.services.telemetry.names import Events
from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings
from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, SubPipelineStreamEvent, Usage


def _make_runner(
    output_format: OutputFormat = OutputFormat.TEXT,
    output_stream: io.StringIO | None = None,
) -> HeadlessRunner:
    return HeadlessRunner(
        model="test-model",
        output_format=output_format,
        output_stream=output_stream or io.StringIO(),
    )


async def _fake_stream(*events):
    """Create an async generator that yields the given events."""
    for event in events:
        yield event


@pytest.mark.asyncio
async def test_headless_auto_approval_uses_general_permission_event(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)
    audit_records = []
    telemetry_events = []

    def fake_emit_permission_audit(record, settings=None):
        audit_records.append((record, settings))

    def fake_log_event(event_name, metadata):
        telemetry_events.append((event_name, metadata))

    monkeypatch.setattr("iac_code.services.permissions.audit.emit_permission_audit", fake_emit_permission_audit)
    monkeypatch.setattr("iac_code.cli.headless.log_event", fake_log_event, raising=False)
    monkeypatch.setattr("iac_code.services.telemetry.log_event", fake_log_event)
    monkeypatch.setattr("iac_code.cli.headless.graceful_shutdown", lambda: None)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    events = [
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "ls", "apiKey": "secret-value"},
            tool_use_id="tu_1",
            response_future=future,
            audit_context={
                "session_id": "session-headless",
                "settings": PermissionAuditSettings(include_tool_input=True, max_file_bytes=123, max_files=2),
                "metadata": PermissionAuditMetadata(
                    scope="settings_rule",
                    source="permission_pipeline",
                    rule_source="user_settings",
                    rule="bash(ls)",
                    reason_type="rule",
                    reason_detail="matched ask rule: bash(ls)",
                    is_read_only=False,
                    operation={"is_read_only": False},
                ),
            },
        ),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    assert future.result() is True
    assert len(audit_records) == 1
    record, settings_seen = audit_records[0]
    assert settings_seen is events[0].audit_context["settings"]
    assert record.session_id == "session-headless"
    assert record.source == "headless_auto_approve"
    assert record.scope == "auto_approve"
    assert record.decision == "allow"
    assert record.rule_source == "user_settings"
    assert record.rule == "bash(ls)"
    assert record.operation == {"is_read_only": False}
    assert record.tool_input_redacted == {
        "command": {"type": "str", "length": 2, "fingerprint": fingerprint_text("ls")},
        fingerprint_text("apiKey"): {"redacted": True},
    }
    assert all(event_name != Events.TOOL_USE_GRANTED_IN_PROMPT for event_name, _metadata in telemetry_events)


@pytest.mark.asyncio
async def test_headless_auto_approval_denies_when_audit_log_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fail_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fail_append)
    monkeypatch.setattr("iac_code.cli.headless.graceful_shutdown", lambda: None)
    runner = _make_runner(OutputFormat.TEXT, io.StringIO())
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    events = [
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "mkdir out"},
            tool_use_id="tu-audit-fail",
            response_future=future,
            audit_context={
                "session_id": "session-headless-audit-fail",
                "metadata": PermissionAuditMetadata(
                    scope="settings_rule",
                    source="permission_pipeline",
                    rule_source="user_settings",
                    rule="bash(mkdir:*)",
                    reason_type="rule",
                    reason_detail="matched allow rule: bash(mkdir:*)",
                    is_read_only=False,
                    operation={"is_read_only": False},
                ),
            },
        ),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    assert future.result() is False


@pytest.mark.asyncio
async def test_headless_auto_approval_denies_untrusted_aliyun_write(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _make_runner(OutputFormat.TEXT, io.StringIO())
    audit_records = []

    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    monkeypatch.setattr("iac_code.cli.headless.graceful_shutdown", lambda: None)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    events = [
        PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="tu-aliyun-write",
            response_future=future,
            audit_context={
                "session_id": "session-headless",
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="permission_pipeline",
                    reason_type="untrusted_write",
                    reason_detail="untrusted Aliyun write",
                    is_read_only=False,
                    operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
                ),
            },
        ),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    assert future.result() is False
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "headless_auto_deny"
    assert record.scope == "auto_deny"
    assert record.decision == "deny"
    assert record.reason_type == "untrusted_write"
    assert record.operation == {"product": "ros", "action": "CreateStack", "is_read_only": False}


@pytest.mark.asyncio
async def test_headless_auto_approval_handles_wrapped_aliyun_write(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _make_runner(OutputFormat.TEXT, io.StringIO())
    audit_records = []

    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    monkeypatch.setattr("iac_code.cli.headless.graceful_shutdown", lambda: None)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    permission_event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack"},
        tool_use_id="tu-wrapped-aliyun-write",
        response_future=future,
        audit_context={
            "session_id": "session-headless",
            "metadata": PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                reason_type="untrusted_write",
                reason_detail="untrusted Aliyun write",
                is_read_only=False,
                operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
            ),
        },
    )
    events = [
        SubPipelineStreamEvent(
            sub_pipeline_id="sub-1",
            candidate_index=0,
            inner=permission_event,
        ),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    assert future.result() is False
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "headless_auto_deny"
    assert record.scope == "auto_deny"
    assert record.decision == "deny"
    assert record.reason_type == "untrusted_write"


@pytest.mark.asyncio
async def test_headless_completed_permission_future_is_not_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)
    audit_records = []
    telemetry_events = []

    def fake_log_event(event_name, metadata):
        telemetry_events.append((event_name, metadata))

    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
    )
    monkeypatch.setattr("iac_code.cli.headless.log_event", fake_log_event)
    monkeypatch.setattr("iac_code.cli.headless.graceful_shutdown", lambda: None)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    future.set_result(False)
    events = [
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "ls"},
            tool_use_id="tu_done",
            response_future=future,
        ),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    assert future.result() is False
    assert audit_records == []
    assert all(event_name != Events.TOOL_USE_GRANTED_IN_PROMPT for event_name, _metadata in telemetry_events)
