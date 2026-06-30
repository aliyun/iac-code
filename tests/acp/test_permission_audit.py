"""Tests for ACP permission prompt audit behavior."""

from __future__ import annotations

import asyncio
import json

import acp
import pytest

from iac_code.acp.session import ACPSession
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionAuditSettings,
    PermissionResult,
    PermissionRuleValue,
)
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent


class FakeConn:
    """Configurable fake ACP client connection for permission tests."""

    def __init__(self, *, outcome: str = "allow_once") -> None:
        self._outcome = outcome
        self.permission_requests: list[dict] = []

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        self.permission_requests.append(
            {
                "options": options,
                "session_id": session_id,
                "tool_call": tool_call,
            }
        )
        if self._outcome == "allow_rule":
            option_id = next(option.option_id for option in options if option.option_id.startswith("allow_rule:"))
            return acp.schema.RequestPermissionResponse(
                outcome=acp.schema.AllowedOutcome(outcome="selected", option_id=option_id)
            )
        if self._outcome == "deny_rule":
            option_id = next(option.option_id for option in options if option.option_id.startswith("deny_rule:"))
            return acp.schema.RequestPermissionResponse(
                outcome=acp.schema.DeniedOutcome(outcome="cancelled"),
                field_meta={"option_id": option_id},
            )
        if self._outcome in ("allow_once", "allow_always"):
            return acp.schema.RequestPermissionResponse(
                outcome=acp.schema.AllowedOutcome(outcome="selected", option_id=self._outcome)
            )
        if self._outcome == "reject_always":
            return acp.schema.RequestPermissionResponse(
                outcome=acp.schema.DeniedOutcome(outcome="cancelled"),
                field_meta={"option_id": "reject_always"},
            )
        return acp.schema.RequestPermissionResponse(outcome=acp.schema.DeniedOutcome(outcome="cancelled"))


class FakeLoopApprove:
    """Minimal agent loop stand-in for direct permission tests."""


class FakeLoopWithAliyun:
    """Minimal agent loop with aliyun_api registered for permission option tests."""

    def __init__(self) -> None:
        aliyun_api = AliyunApi()
        self.tool_registry = type(
            "Registry",
            (),
            {
                "get": lambda self, name: aliyun_api if name == "aliyun_api" else None,
                "list_tools": lambda self: [aliyun_api],
            },
        )()


class FakeLoopWrappedPermission:
    def __init__(self, permission_event: PermissionRequestEvent) -> None:
        self.permission_event = permission_event

    async def run_streaming(self, prompt):
        yield SubPipelineStreamEvent(
            sub_pipeline_id="sub-1",
            candidate_index=0,
            inner=self.permission_event,
        )
        if self.permission_event.response_future is not None:
            await self.permission_event.response_future


def _make_event(*, audit_context=None, permission_result: PermissionResult | None = None) -> PermissionRequestEvent:
    return PermissionRequestEvent(
        tool_name="write_file",
        tool_input={"path": "main.tf", "content": "resource {}"},
        tool_use_id="tool1",
        permission_result=permission_result,
        audit_context=audit_context,
    )


def _make_aliyun_write_event() -> PermissionRequestEvent:
    return PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack"},
        tool_use_id="tool-aliyun-write",
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
    )


@pytest.fixture
def audit_records(monkeypatch):
    records = []

    def fake_emit(record, settings=None):
        records.append((record, settings))

    monkeypatch.setattr("iac_code.services.permissions.audit.emit_permission_audit", fake_emit)
    return records


@pytest.mark.asyncio
async def test_acp_tool_cache_hit_uses_acp_tool_cache_source(audit_records) -> None:
    conn = FakeConn(outcome="allow_once")
    session = ACPSession("s1", FakeLoopApprove(), conn)
    session._permission_cache["write_file"] = "always_allow"

    result = await session._request_permission(_make_event())

    assert result is True
    assert conn.permission_requests == []
    [(record, _settings)] = audit_records
    assert record.source == "acp_tool_cache"
    assert record.scope == "tool_cache"
    assert record.rule_source == "tool_cache"
    assert record.decision == "allow"


@pytest.mark.asyncio
async def test_acp_prompt_does_not_expose_internal_audit_context(audit_records) -> None:
    conn = FakeConn(outcome="allow_once")
    session = ACPSession("s1", FakeLoopApprove(), conn)
    audit_context = {
        "session_id": "session-secret",
        "settings": PermissionAuditSettings(max_file_bytes=123, max_files=2),
        "metadata": PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="user_settings",
            rule="secret-rule:DoWrite",
            reason_type="rule",
            reason_detail="matched secret-rule:DoWrite",
            is_read_only=False,
        ),
    }

    result = await session._request_permission(_make_event(audit_context=audit_context))

    assert result is True
    payload = conn.permission_requests[0]["tool_call"].model_dump()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "audit_context" not in payload
    assert "audit_context" not in serialized
    assert "rule_source" not in serialized
    assert "secret-rule:DoWrite" not in serialized
    assert "max_file_bytes" not in serialized


@pytest.mark.asyncio
async def test_acp_prompt_reject_always_emits_prompt_audit(audit_records) -> None:
    conn = FakeConn(outcome="reject_always")
    session = ACPSession("s1", FakeLoopApprove(), conn)
    audit_context = {
        "metadata": PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="write_file",
            reason_type="rule",
            reason_detail="matched ask rule(s): write_file",
            is_read_only=False,
        )
    }

    result = await session._request_permission(_make_event(audit_context=audit_context))

    assert result is False
    assert session._permission_cache["write_file"] == "always_deny"
    [(record, _settings)] = audit_records
    assert record.source == "acp_prompt"
    assert record.scope == "tool_cache"
    assert record.decision == "deny"
    assert record.rule_source == "tool_cache"
    assert record.reason_detail == "reject_always"


@pytest.mark.asyncio
async def test_acp_prompt_preserves_triggering_ask_rule_provenance(audit_records) -> None:
    conn = FakeConn(outcome="allow_once")
    session = ACPSession("s1", FakeLoopApprove(), conn)
    audit_context = {
        "metadata": PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="write_file",
            reason_type="rule",
            reason_detail="matched ask rule(s): write_file",
            is_read_only=False,
        )
    }

    result = await session._request_permission(_make_event(audit_context=audit_context))

    assert result is True
    [(record, _settings)] = audit_records
    assert record.source == "acp_prompt"
    assert record.scope == "once"
    assert record.decision == "allow"
    assert record.reason_type == "prompt_selection"
    assert record.reason_detail == "allow_once"
    assert record.rule_source == "project_settings"
    assert record.rule == "write_file"


@pytest.mark.asyncio
async def test_acp_aliyun_write_allow_denies_when_audit_log_write_fails(monkeypatch) -> None:
    def fail_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fail_append)
    conn = FakeConn(outcome="allow_once")
    session = ACPSession("s1", FakeLoopWithAliyun(), conn)

    result = await session._request_permission(_make_aliyun_write_event())

    assert result is False


@pytest.mark.asyncio
async def test_acp_allow_denies_when_audit_log_write_fails(monkeypatch) -> None:
    def fail_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fail_append)
    conn = FakeConn(outcome="allow_once")
    session = ACPSession("s1", FakeLoopApprove(), conn)
    event = _make_event(
        audit_context={
            "metadata": PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=False,
                operation={"is_read_only": False},
            )
        }
    )

    result = await session._request_permission(event)

    assert result is False


@pytest.mark.asyncio
async def test_acp_prompt_handles_wrapped_permission_request(audit_records) -> None:
    conn = FakeConn(outcome="allow_once")
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    permission_event = _make_aliyun_write_event()
    permission_event.response_future = future
    session = ACPSession("s1", FakeLoopWrappedPermission(permission_event), conn)

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="create stack")])

    assert response.stop_reason == "end_turn"
    assert future.result() is True
    assert len(conn.permission_requests) == 1
    [(record, _settings)] = audit_records
    assert record.source == "acp_prompt"
    assert record.scope == "once"
    assert record.decision == "allow"
    assert record.operation == {"product": "ros", "action": "CreateStack", "is_read_only": False}


@pytest.mark.asyncio
async def test_acp_prompt_allow_rule_emits_session_rule_audit(audit_records) -> None:
    conn = FakeConn(outcome="allow_rule")
    session = ACPSession("s1", FakeLoopApprove(), conn)
    audit_context = {
        "metadata": PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="write_file(path:main.tf)",
            reason_type="rule",
            reason_detail="matched ask rule(s): write_file(path:main.tf)",
            is_read_only=False,
        )
    }
    permission_result = PermissionResult(
        behavior="ask",
        suggestions=[PermissionRuleValue(tool_name="write_file", rule_content="path:main.tf")],
    )

    result = await session._request_permission(
        _make_event(audit_context=audit_context, permission_result=permission_result)
    )

    assert result is True
    [(record, _settings)] = audit_records
    assert record.source == "acp_prompt"
    assert record.scope == "session_rule"
    assert record.decision == "allow"
    assert record.rule_source == "session"
    assert record.rule == "write_file(path:main.tf)"
    assert record.reason_detail == "allow_rule"


@pytest.mark.asyncio
async def test_acp_prompt_deny_rule_emits_session_rule_source(audit_records) -> None:
    conn = FakeConn(outcome="deny_rule")
    session = ACPSession("s1", FakeLoopApprove(), conn)
    audit_context = {
        "metadata": PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="write_file(path:main.tf)",
            reason_type="rule",
            reason_detail="matched ask rule(s): write_file(path:main.tf)",
            is_read_only=False,
        )
    }
    permission_result = PermissionResult(
        behavior="ask",
        suggestions=[PermissionRuleValue(tool_name="write_file", rule_content="path:main.tf")],
    )

    result = await session._request_permission(
        _make_event(audit_context=audit_context, permission_result=permission_result)
    )

    assert result is False
    [(record, _settings)] = audit_records
    assert record.source == "acp_prompt"
    assert record.scope == "session_rule"
    assert record.decision == "deny"
    assert record.rule_source == "session"
    assert record.rule == "write_file(path:main.tf)"
    assert record.reason_detail == "deny_rule"
