"""Tests for REPL permission prompt audit behavior."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from iac_code.state.app_state import AppState, AppStateStore
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionAuditSettings,
    PermissionResult,
    PermissionRuleValue,
    ToolPermissionContext,
)
from iac_code.types.stream_events import PermissionRequestEvent
from iac_code.ui.renderer import Renderer


def _make_renderer(app_state_store=None, tool=None) -> Renderer:
    console = Console(record=True)
    tool_registry = MagicMock()
    tool_registry.get.return_value = tool
    return Renderer(console, tool_registry, app_state_store=app_state_store)


def _make_event(
    tool_name: str = "write_file",
    *,
    tool_input: dict | None = None,
    permission_result: PermissionResult | None = None,
    audit_metadata: PermissionAuditMetadata | None = None,
    audit_settings: PermissionAuditSettings | None = None,
) -> PermissionRequestEvent:
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    audit_context = None
    if audit_metadata is not None or audit_settings is not None:
        audit_context = {
            "session_id": "session-audit",
            "settings": audit_settings,
            "metadata": audit_metadata,
        }
    return PermissionRequestEvent(
        tool_name=tool_name,
        tool_input=tool_input or {"path": "main.tf", "content": "resource {}"},
        tool_use_id="t1",
        response_future=fut,
        permission_result=permission_result,
        audit_context=audit_context,
    )


def _patch_select(return_value):
    """Patch Select.run to return a predetermined value."""
    return patch("iac_code.ui.components.select.Select.run", return_value=return_value)


@pytest.fixture
def audit_records(monkeypatch):
    records = []

    def fake_emit(record, settings=None):
        records.append((record, settings))

    monkeypatch.setattr("iac_code.services.permissions.audit.emit_permission_audit", fake_emit)
    return records


class _RawAliyunDisplayTool:
    supports_blanket_allow = False

    def user_facing_name(self, input=None):
        return "CloudAPI"

    def render_tool_use_message(self, input, *, verbose=False):
        return "{} {}".format(input.get("action", ""), input.get("region_id", ""))


@pytest.mark.asyncio
async def test_renderer_ignores_stale_aliyun_cached_allow_for_write(audit_records) -> None:
    store = AppStateStore(AppState(always_allow_rules=OrderedDict([("aliyun_api", "always_allow")])))
    renderer = _make_renderer(store)
    event = _make_event(
        "aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack"},
        audit_metadata=PermissionAuditMetadata(
            scope="once",
            source="permission_pipeline",
            is_read_only=False,
            operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
        ),
    )

    with _patch_select("reject_once") as mock_run:
        result = await renderer.prompt_permission(event)

    assert result is False
    mock_run.assert_called_once()
    assert "aliyun_api" not in store.get_state().always_allow_rules
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.decision == "deny"
    assert record.reason_detail == "reject_once"


@pytest.mark.asyncio
async def test_renderer_permission_prompt_summarizes_aliyun_detail(audit_records) -> None:
    store = AppStateStore()
    renderer = _make_renderer(store, tool=_RawAliyunDisplayTool())
    event = _make_event(
        "aliyun_api",
        tool_input={
            "product": "ROS",
            "action": "CreateStack Signature=signature-secret",
            "region_id": "cn-hangzhou Authorization=Bearer bearer-secret",
            "params": {"AccessKeySecret": "secret-value"},
        },
        audit_metadata=PermissionAuditMetadata(
            scope="once",
            source="permission_pipeline",
            is_read_only=False,
            operation={"product": "ROS", "action": "CreateStack", "is_read_only": False},
        ),
    )

    with _patch_select("allow_once"):
        result = await renderer.prompt_permission(event)

    assert result is True
    rendered = renderer.console.export_text()
    assert "Input summary:" in rendered
    assert "signature-secret" not in rendered
    assert "bearer-secret" not in rendered
    assert "secret-value" not in rendered
    assert "AccessKeySecret" not in rendered


@pytest.mark.asyncio
async def test_renderer_audits_tool_cache_deny_for_write(audit_records) -> None:
    store = AppStateStore(AppState(always_allow_rules=OrderedDict([("write_file", "always_deny")])))
    renderer = _make_renderer(store)
    event = _make_event("write_file")

    with _patch_select("allow_once") as mock_run:
        result = await renderer.prompt_permission(event)

    assert result is False
    mock_run.assert_not_called()
    [(record, _settings)] = audit_records
    assert record.source == "repl_tool_cache"
    assert record.scope == "tool_cache"
    assert record.rule_source == "tool_cache"
    assert record.decision == "deny"
    assert record.tool_use_id == "t1"


@pytest.mark.asyncio
async def test_renderer_prompt_allow_once_emits_one_prompt_event(audit_records) -> None:
    store = AppStateStore()
    renderer = _make_renderer(store)
    event = _make_event("write_file")

    with _patch_select("allow_once"):
        result = await renderer.prompt_permission(event)

    assert result is True
    assert len(store.get_state().always_allow_rules) == 0
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.scope == "once"
    assert record.decision == "allow"
    assert record.reason_detail == "allow_once"


@pytest.mark.asyncio
async def test_renderer_prompt_allow_denies_when_audit_log_write_fails(monkeypatch) -> None:
    def fail_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fail_append)
    store = AppStateStore()
    renderer = _make_renderer(store)
    event = _make_event(
        "write_file",
        audit_metadata=PermissionAuditMetadata(
            scope="once",
            source="permission_pipeline",
            is_read_only=False,
        ),
    )

    with _patch_select("allow_once"):
        result = await renderer.prompt_permission(event)

    assert result is False


@pytest.mark.asyncio
async def test_renderer_prompt_preserves_triggering_ask_rule_provenance(audit_records) -> None:
    store = AppStateStore()
    renderer = _make_renderer(store)
    event = _make_event(
        "read_file",
        audit_metadata=PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="read_file",
            reason_type="rule",
            reason_detail="matched ask rule(s): read_file",
            is_read_only=True,
        ),
    )

    with _patch_select("allow_once"):
        result = await renderer.prompt_permission(event)

    assert result is True
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.scope == "once"
    assert record.decision == "allow"
    assert record.reason_type == "prompt_selection"
    assert record.reason_detail == "allow_once"
    assert record.rule_source == "project_settings"
    assert record.rule == "read_file"


@pytest.mark.asyncio
async def test_renderer_prompt_preserves_non_rule_trigger_reason(audit_records) -> None:
    store = AppStateStore()
    renderer = _make_renderer(store)
    event = _make_event(
        "write_file",
        audit_metadata=PermissionAuditMetadata(
            scope="once",
            source="permission_pipeline",
            reason_type="path_constraint",
            reason_detail="path_constraint",
            is_read_only=False,
        ),
    )

    with _patch_select("allow_once"):
        result = await renderer.prompt_permission(event)

    assert result is True
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.reason_type == "prompt_selection"
    assert record.reason_detail == "allow_once"
    assert record.trigger_reason_type == "path_constraint"


@pytest.mark.asyncio
async def test_renderer_prompt_always_allow_tool_cache_records_tool_cache_source(audit_records) -> None:
    store = AppStateStore()
    renderer = _make_renderer(store)
    event = _make_event(
        "write_file",
        audit_metadata=PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="write_file",
            reason_type="rule",
            reason_detail="matched ask rule(s): write_file",
            is_read_only=False,
        ),
    )

    with _patch_select("always_allow"):
        result = await renderer.prompt_permission(event)

    assert result is True
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.scope == "tool_cache"
    assert record.decision == "allow"
    assert record.rule_source == "tool_cache"


@pytest.mark.asyncio
async def test_renderer_prompt_cancel_emits_reject_once_prompt_event(audit_records) -> None:
    store = AppStateStore()
    renderer = _make_renderer(store)
    event = _make_event("write_file")

    with _patch_select(None):
        result = await renderer.prompt_permission(event)

    assert result is False
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.scope == "once"
    assert record.decision == "deny"
    assert record.reason_detail == "reject_once"


@pytest.mark.asyncio
async def test_renderer_prompt_always_allow_rule_records_session_rule_audit(audit_records) -> None:
    store = AppStateStore(AppState(permission_context=ToolPermissionContext()))
    renderer = _make_renderer(store)
    event = _make_event(
        "bash",
        tool_input={"command": "mkdir foo"},
        audit_metadata=PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="bash(mkdir:*)",
            reason_type="rule",
            reason_detail="matched ask rule(s): bash(mkdir:*)",
            is_read_only=False,
        ),
        permission_result=PermissionResult(
            behavior="ask",
            suggestions=[PermissionRuleValue(tool_name="bash", rule_content="mkdir:*")],
        ),
    )

    with _patch_select("always_allow_rule"):
        result = await renderer.prompt_permission(event)

    assert result is True
    allow_rules = store.get_state().permission_context.allow_rules.get("session", [])
    assert any("mkdir:*" in rule for rule in allow_rules)
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.scope == "session_rule"
    assert record.decision == "allow"
    assert record.rule_source == "session"
    assert record.rule == "bash(mkdir:*)"


@pytest.mark.asyncio
async def test_renderer_prompt_always_deny_rule_records_session_rule_source(audit_records) -> None:
    store = AppStateStore(AppState(permission_context=ToolPermissionContext()))
    renderer = _make_renderer(store)
    event = _make_event(
        "bash",
        tool_input={"command": "rm foo"},
        audit_metadata=PermissionAuditMetadata(
            scope="settings_rule",
            source="permission_pipeline",
            rule_source="project_settings",
            rule="bash(rm:*)",
            reason_type="rule",
            reason_detail="matched ask rule(s): bash(rm:*)",
            is_read_only=False,
        ),
        permission_result=PermissionResult(
            behavior="ask",
            suggestions=[PermissionRuleValue(tool_name="bash", rule_content="rm:*")],
        ),
    )

    with _patch_select("always_deny_rule"):
        result = await renderer.prompt_permission(event)

    assert result is False
    deny_rules = store.get_state().permission_context.deny_rules.get("session", [])
    assert any("rm:*" in rule for rule in deny_rules)
    [(record, _settings)] = audit_records
    assert record.source == "repl_prompt"
    assert record.scope == "session_rule"
    assert record.decision == "deny"
    assert record.rule_source == "session"
    assert record.rule == "bash(rm:*)"
