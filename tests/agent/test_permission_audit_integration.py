import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.mcp.tools import MCPTool
from iac_code.mcp.types import MCPToolRecord
from iac_code.services.permissions.audit import emit_permission_boundary_audit, fingerprint_text
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.bash.bash_tool import BashTool
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.api_contract import CanonicalWireContract
from iac_code.tools.cloud.aliyun.contract_store import PROCESS_RESOLVED_CONTRACT_STORE
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionAuditSettings,
    PermissionDecisionReason,
    PermissionMode,
    PermissionResult,
    ToolPermissionContext,
)
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.utils.project_paths import sanitize_path


class FakeProvider:
    """Mock provider that yields predetermined tool calls across multiple turns."""

    def __init__(self, turns: list[list]):
        self._turns = turns
        self._call_count = 0

    def get_model_name(self) -> str:
        return "fake-model"

    async def stream(self, messages, system, tools=None, max_tokens=8192):
        idx = min(self._call_count, len(self._turns) - 1)
        self._call_count += 1
        for event in self._turns[idx]:
            yield event


class FakePermissionTool(Tool):
    def __init__(self, permission: PermissionResult) -> None:
        self.permission = permission

    @property
    def name(self) -> str:
        return "fake_permission"

    @property
    def description(self) -> str:
        return "Fake permission-controlled tool"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"payload": {"type": "string"}}}

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("executed")

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        return self.permission


class FakeAliyunApi(AliyunApi):
    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("{}")


class FakeMCPManager:
    def __init__(self) -> None:
        self.called_with: dict[str, Any] = {}

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any], **kwargs) -> dict[str, Any]:
        self.called_with = {
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "kwargs": kwargs,
        }
        return {"content": [{"type": "text", "text": "search result"}]}


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _session_audit_log_path(config_dir, cwd: str, session_id: str):
    return config_dir / "projects" / sanitize_path(cwd) / session_id / "permission-audit.jsonl"


def _tool_turn(
    tool_use_id: str = "t1",
    *,
    payload: str = "secret-value",
    tool_name: str = "fake_permission",
    tool_input: dict[str, Any] | None = None,
) -> list:
    final_input = tool_input if tool_input is not None else {"payload": payload}
    return [
        MessageStartEvent(message_id=f"msg-{tool_use_id}"),
        ToolUseStartEvent(tool_use_id=tool_use_id, name=tool_name),
        ToolUseEndEvent(tool_use_id=tool_use_id, name=tool_name, input=final_input),
        MessageEndEvent(stop_reason="tool_use", usage=Usage()),
    ]


def _text_turn(text: str) -> list:
    return [
        MessageStartEvent(message_id="msg-text"),
        TextDeltaEvent(text=text),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]


async def _collect_events(loop: AgentLoop, prompt: str, permission_handler=None):
    events = []
    async for event in loop.run_streaming(prompt):
        events.append(event)
        if isinstance(event, PermissionRequestEvent) and event.response_future:
            result = permission_handler(event) if permission_handler else False
            event.response_future.set_result(result)
    return events


def _audit_metadata(
    *,
    scope: str = "settings_rule",
    rule_source: str | None = "user_settings",
    rule: str | None = "fake_permission",
    reason_type: str | None = "rule",
    reason_detail: str | None = "matched fake rule",
    is_read_only: bool | None = False,
) -> PermissionAuditMetadata:
    return PermissionAuditMetadata(
        scope=scope,
        source="permission_pipeline",
        rule_source=rule_source,
        rule=rule,
        reason_type=reason_type,
        reason_detail=reason_detail,
        is_read_only=is_read_only,
        operation={"product": "ROS", "action": "CreateStack"},
    )


async def _run_fake_tool_with_audit(
    monkeypatch,
    tmp_path,
    permission: PermissionResult,
    *,
    context: ToolPermissionContext | None = None,
):
    records = []
    settings_seen = []

    def fake_emit(record, settings=None):
        records.append(record)
        settings_seen.append(settings)

    monkeypatch.setattr("iac_code.agent.agent_loop.emit_permission_audit", fake_emit)
    provider = FakeProvider([_tool_turn(), _text_turn("done")])
    registry = ToolRegistry()
    registry.register(FakePermissionTool(permission))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-audit",
        permission_context=context or ToolPermissionContext(cwd=str(tmp_path)),
    )

    events = await _collect_events(loop, "run fake tool", permission_handler=Mock(return_value=False))

    return events, records, settings_seen


def _permission_requests(events) -> list[PermissionRequestEvent]:
    return [event for event in events if isinstance(event, PermissionRequestEvent)]


@pytest.mark.asyncio
async def test_agent_loop_prompt_event_carries_internal_audit_context(tmp_path):
    settings = PermissionAuditSettings(max_file_bytes=123, max_files=2)
    metadata = _audit_metadata(scope="once", rule_source=None, rule=None, reason_type="needs_prompt")
    provider = FakeProvider([_tool_turn(), _text_turn("done")])
    registry = ToolRegistry()
    registry.register(FakePermissionTool(PermissionResult(behavior="ask", audit=metadata)))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-prompt",
        permission_context=ToolPermissionContext(cwd=str(tmp_path), audit_settings=settings),
    )

    events = await _collect_events(loop, "run fake tool", permission_handler=Mock(return_value=False))

    [prompt] = _permission_requests(events)
    assert prompt.audit_context == {
        "session_id": "session-prompt",
        "cwd": str(tmp_path),
        "settings": settings,
        "metadata": metadata,
    }


@pytest.mark.asyncio
async def test_agent_loop_durably_audits_every_merged_aliyun_prompt_subcheck_without_business_values(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    body_file = outside / "private-payload.bin"
    body_file.write_bytes(b"private-body-value")
    contract = CanonicalWireContract(
        metadata_source="fresh",
        product="Ecs",
        version="2014-05-26",
        action="CreateInstance",
        style="RPC",
        method="POST",
        pathname="/",
        operation_type="write",
        auth_type="AK",
        signature_scheme="acs3",
        transport="tea",
        executable=True,
        unsupported_reasons=(),
        parameters=(),
        consumes=("application/octet-stream",),
        produces=("application/json",),
        request_body_type="byte",
        policy_digest="fixture-policy",
    )

    class Resolver:
        async def resolve(self, call, *, allow_fallback):
            del call, allow_fallback
            return contract

    tool = AliyunApi(
        services=SimpleNamespace(
            contract_resolver=Resolver(),
            contract_store=PROCESS_RESOLVED_CONTRACT_STORE,
            permission_stage_observer=None,
        )
    )
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"Name": "business-parameter-value"},
        "body_file": str(body_file),
    }
    provider = FakeProvider(
        [
            _tool_turn(tool_name="aliyun_api", tool_input=tool_input),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(project),
        max_turns=2,
        session_id="session-aliyun-merged-audit",
        permission_context=ToolPermissionContext(
            cwd=str(project),
            ask_rules={"project_settings": ["aliyun_api(ecs:CreateInstance)"]},
        ),
    )

    def reject_with_primary_boundary_audit(event: PermissionRequestEvent) -> bool:
        assert emit_permission_boundary_audit(
            event,
            decision="deny",
            scope="once",
            source="repl_prompt",
        )
        return False

    events = await _collect_events(loop, "create an instance", permission_handler=reject_with_primary_boundary_audit)

    [prompt] = _permission_requests(events)
    assert prompt.permission_result is not None
    assert [reason.type for reason in prompt.permission_result.reasons] == [
        "path_constraint",
        "rule",
        "untrusted_write",
    ]
    assert prompt.permission_result.message.count("Allow") == 1
    rows = _read_jsonl(_session_audit_log_path(tmp_path, str(project), "session-aliyun-merged-audit"))
    assert [row["reason_type"] for row in rows] == ["untrusted_write", "path_constraint", "rule"]
    assert all(row["tool_use_id"] == "t1" for row in rows)
    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0

    serialized_rows = json.dumps(rows, sort_keys=True)
    assert str(body_file) not in serialized_rows
    assert "private-body-value" not in serialized_rows
    assert "business-parameter-value" not in serialized_rows


@pytest.mark.asyncio
async def test_agent_loop_approved_prompt_fails_closed_when_additional_subcheck_audit_fails(
    monkeypatch,
    tmp_path,
):
    primary = _audit_metadata(scope="once", rule_source=None, rule=None, reason_type="untrusted_write")
    file_subcheck = _audit_metadata(scope="once", rule_source=None, rule=None, reason_type="path_constraint")
    permission = PermissionResult(
        behavior="ask",
        message="Allow merged operation?",
        audit=primary,
        audit_items=(primary, file_subcheck),
    )
    monkeypatch.setattr("iac_code.agent.agent_loop.emit_permission_audit", lambda record, settings=None: False)
    provider = FakeProvider([_tool_turn(), _text_turn("done")])
    registry = ToolRegistry()
    registry.register(FakePermissionTool(permission))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-additional-audit-fail",
        permission_context=ToolPermissionContext(cwd=str(tmp_path)),
    )

    events = await _collect_events(loop, "run merged operation", permission_handler=Mock(return_value=True))

    assert len(_permission_requests(events)) == 1
    assert any(
        isinstance(event, ToolResultEvent) and event.is_error and event.result == "Permission denied."
        for event in events
    )
    assert not any(
        isinstance(event, ToolResultEvent) and not event.is_error and event.result == "executed" for event in events
    )


@pytest.mark.asyncio
async def test_agent_loop_prompt_event_carries_transcript_audit_context(tmp_path):
    settings = PermissionAuditSettings(max_file_bytes=123, max_files=2)
    metadata = _audit_metadata(scope="once", rule_source=None, rule=None, reason_type="needs_prompt")
    transcript_dir = tmp_path / "root-session" / "pipeline" / "transcripts" / "transcript_att_0001"
    audit_log_path = transcript_dir / "permission-audit.jsonl"
    provider = FakeProvider([_tool_turn(), _text_turn("done")])
    registry = ToolRegistry()
    registry.register(FakePermissionTool(PermissionResult(behavior="ask", audit=metadata)))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="transcript_att_0001",
        root_session_id="root-session",
        transcript_id="transcript_att_0001",
        audit_log_path=audit_log_path,
        permission_context=ToolPermissionContext(cwd=str(tmp_path), audit_settings=settings),
    )

    events = await _collect_events(loop, "run fake tool", permission_handler=Mock(return_value=False))

    [prompt] = _permission_requests(events)
    assert prompt.audit_context == {
        "session_id": "transcript_att_0001",
        "root_session_id": "root-session",
        "transcript_id": "transcript_att_0001",
        "cwd": str(tmp_path),
        "settings": settings,
        "metadata": metadata,
        "audit_log_path": str(audit_log_path),
    }


@pytest.mark.asyncio
async def test_agent_loop_bash_ask_rule_prompt_carries_rule_audit_context(tmp_path):
    settings = PermissionAuditSettings(max_file_bytes=123, max_files=2)
    provider = FakeProvider([_tool_turn(tool_name="bash", tool_input={"command": "mkdir foo"}), _text_turn("done")])
    registry = ToolRegistry()
    registry.register(BashTool())
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-bash-ask",
        permission_context=ToolPermissionContext(
            cwd=str(tmp_path),
            ask_rules={"project_settings": ["bash(mkdir:*)"]},
            audit_settings=settings,
        ),
    )

    events = await _collect_events(loop, "run bash", permission_handler=Mock(return_value=False))

    [prompt] = _permission_requests(events)
    metadata = prompt.audit_context["metadata"]
    assert prompt.audit_context["session_id"] == "session-bash-ask"
    assert prompt.audit_context["cwd"] == str(tmp_path)
    assert prompt.audit_context["settings"] is settings
    assert metadata.scope == "settings_rule"
    assert metadata.rule_source == "project_settings"
    assert metadata.rule == "bash(mkdir:*)"
    assert metadata.reason_type == "rule"
    assert metadata.is_read_only is False


@pytest.mark.asyncio
async def test_agent_loop_bash_accept_edits_allow_is_audited(monkeypatch, tmp_path):
    records = []
    settings_seen = []
    settings = PermissionAuditSettings(max_file_bytes=123, max_files=2)

    def fake_emit(record, settings=None):
        records.append(record)
        settings_seen.append(settings)

    monkeypatch.setattr("iac_code.agent.agent_loop.emit_permission_audit", fake_emit)
    provider = FakeProvider([_tool_turn(tool_name="bash", tool_input={"command": "mkdir foo"}), _text_turn("done")])
    registry = ToolRegistry()
    registry.register(BashTool())
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-accept-edits",
        permission_context=ToolPermissionContext(
            cwd=str(tmp_path),
            mode=PermissionMode.ACCEPT_EDITS,
            audit_settings=settings,
        ),
    )

    events = await _collect_events(loop, "run bash", permission_handler=Mock(return_value=False))

    assert not _permission_requests(events)
    assert len(records) == 1
    assert records[0].decision == "allow"
    assert records[0].scope == "mode"
    assert records[0].source == "permission_pipeline"
    assert records[0].rule_source == "mode"
    assert records[0].reason_type == "accept_edits"
    assert records[0].operation == {"is_read_only": False}
    assert settings_seen == [settings]


@pytest.mark.asyncio
async def test_agent_loop_aliyun_exact_allow_jsonl_includes_read_write_classification(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    provider = FakeProvider(
        [
            _tool_turn(
                tool_name="aliyun_api",
                tool_input={"product": "ros", "action": "CreateStack", "region_id": "cn-hangzhou"},
            ),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(FakeAliyunApi())
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-aliyun-allow",
        permission_context=ToolPermissionContext(
            cwd=str(tmp_path),
            allow_rules={"user_settings": ["aliyun_api(ros:CreateStack)"]},
        ),
    )

    events = await _collect_events(loop, "run aliyun", permission_handler=Mock(return_value=False))

    assert not _permission_requests(events)
    [row] = _read_jsonl(_session_audit_log_path(tmp_path, str(tmp_path), "session-aliyun-allow"))
    assert row["tool_name"] == "aliyun_api"
    assert row["decision"] == "allow"
    assert row["scope"] == "settings_rule"
    assert row["operation"] == {
        "product": "ros",
        "action": "CreateStack",
        "region": "cn-hangzhou",
        "is_read_only": False,
    }


@pytest.mark.asyncio
async def test_agent_loop_aliyun_bypass_mode_allows_write_with_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    provider = FakeProvider(
        [
            _tool_turn(
                tool_name="aliyun_api",
                tool_input={"product": "ros", "action": "CreateStack", "region_id": "cn-hangzhou"},
            ),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(FakeAliyunApi())
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-aliyun-bypass",
        permission_context=ToolPermissionContext(cwd=str(tmp_path), mode=PermissionMode.BYPASS_PERMISSIONS),
    )

    events = await _collect_events(loop, "run aliyun", permission_handler=Mock(return_value=False))

    assert not _permission_requests(events)
    assert any(isinstance(event, ToolResultEvent) and not event.is_error for event in events)
    [row] = _read_jsonl(_session_audit_log_path(tmp_path, str(tmp_path), "session-aliyun-bypass"))
    assert row["tool_name"] == "aliyun_api"
    assert row["decision"] == "allow"
    assert row["scope"] == "mode"
    assert row["source"] == "permission_pipeline"
    assert row["rule_source"] == "mode"
    assert row["reason_type"] == "bypass_permissions"
    assert row["operation"] == {
        "product": "ros",
        "action": "CreateStack",
        "region": "cn-hangzhou",
        "is_read_only": False,
    }


@pytest.mark.asyncio
async def test_agent_loop_aliyun_bypass_mode_denies_write_when_audit_fails(monkeypatch, tmp_path):
    provider = FakeProvider(
        [
            _tool_turn(
                tool_name="aliyun_api",
                tool_input={"product": "ros", "action": "CreateStack", "region_id": "cn-hangzhou"},
            ),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(FakeAliyunApi())
    monkeypatch.setattr("iac_code.agent.agent_loop.emit_permission_audit", lambda record, settings=None: False)
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-aliyun-bypass-audit-fail",
        permission_context=ToolPermissionContext(cwd=str(tmp_path), mode=PermissionMode.BYPASS_PERMISSIONS),
    )

    events = await _collect_events(loop, "run aliyun", permission_handler=Mock(return_value=True))

    assert not _permission_requests(events)
    assert any(
        isinstance(event, ToolResultEvent) and event.is_error and event.result == "Permission denied."
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_loop_sticky_prompt_carries_trigger_reason_metadata(tmp_path):
    outside_path = tmp_path.parent / "outside-workspace.txt"
    provider = FakeProvider(
        [
            _tool_turn(
                tool_name="write_file",
                tool_input={"path": str(outside_path), "content": "secret-value"},
            ),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-path-constraint",
        permission_context=ToolPermissionContext(cwd=str(tmp_path)),
    )

    events = await _collect_events(loop, "write outside", permission_handler=Mock(return_value=False))

    [prompt] = _permission_requests(events)
    metadata = prompt.audit_context["metadata"]
    assert metadata is not None
    assert metadata.scope == "once"
    assert metadata.source == "permission_pipeline"
    assert metadata.reason_type == "path_constraint"
    assert metadata.reason_detail == "path_constraint"
    assert metadata.is_read_only is False


@pytest.mark.asyncio
async def test_agent_loop_bash_compound_prompt_carries_trigger_reason_metadata(tmp_path):
    provider = FakeProvider(
        [
            _tool_turn(tool_name="bash", tool_input={"command": "cd repo && git status"}),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(BashTool())
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-compound-cd-git",
        permission_context=ToolPermissionContext(cwd=str(tmp_path)),
    )

    events = await _collect_events(loop, "run bash", permission_handler=Mock(return_value=False))

    [prompt] = _permission_requests(events)
    metadata = prompt.audit_context["metadata"]
    assert metadata is not None
    assert metadata.scope == "once"
    assert metadata.source == "permission_pipeline"
    assert metadata.reason_type == "compound_cd_git"
    assert metadata.reason_detail == "compound_cd_git"
    assert metadata.is_read_only is False


@pytest.mark.asyncio
async def test_agent_loop_audits_no_prompt_allow_with_audit_metadata(monkeypatch, tmp_path):
    events, records, settings_seen = await _run_fake_tool_with_audit(
        monkeypatch,
        tmp_path,
        PermissionResult(behavior="allow", audit=_audit_metadata(rule_source="user_settings")),
    )

    assert not _permission_requests(events)
    assert len(records) == 1
    assert records[0].decision == "allow"
    assert records[0].scope == "settings_rule"
    assert records[0].source == "permission_pipeline"
    assert records[0].rule_source == "user_settings"
    assert records[0].tool_name == "fake_permission"
    assert records[0].tool_use_id == "t1"
    assert records[0].operation == {"product": "ROS", "action": "CreateStack", "is_read_only": False}
    assert records[0].input_summary["fields"]["payload"] == {"type": "str"}
    assert "secret-value" not in str(records[0].input_summary)
    assert settings_seen[0] is not None


@pytest.mark.asyncio
async def test_agent_loop_no_prompt_allow_denies_when_audit_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("iac_code.agent.agent_loop.emit_permission_audit", lambda record, settings=None: False)
    provider = FakeProvider([_tool_turn(), _text_turn("done")])
    registry = ToolRegistry()
    registry.register(FakePermissionTool(PermissionResult(behavior="allow", audit=_audit_metadata())))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-audit-fail",
        permission_context=ToolPermissionContext(cwd=str(tmp_path)),
    )

    events = await _collect_events(loop, "run fake tool", permission_handler=Mock(return_value=True))

    assert not _permission_requests(events)
    assert any(
        isinstance(event, ToolResultEvent) and event.is_error and event.result == "Permission denied."
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_loop_no_prompt_audit_populates_redacted_input_when_enabled(monkeypatch, tmp_path):
    records = []
    settings_seen = []
    settings = PermissionAuditSettings(include_tool_input=True, max_file_bytes=123, max_files=2)

    def fake_emit(record, settings=None):
        records.append(record)
        settings_seen.append(settings)

    monkeypatch.setattr("iac_code.agent.agent_loop.emit_permission_audit", fake_emit)
    provider = FakeProvider(
        [
            _tool_turn(tool_input={"payload": "visible", "access_key_secret": "secret-value"}),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(FakePermissionTool(PermissionResult(behavior="allow", audit=_audit_metadata())))
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-audit",
        permission_context=ToolPermissionContext(cwd=str(tmp_path), audit_settings=settings),
    )

    events = await _collect_events(loop, "run fake tool", permission_handler=Mock(return_value=False))

    assert not _permission_requests(events)
    assert len(records) == 1
    assert records[0].tool_input_redacted == {
        "payload": {"type": "str", "length": 7, "fingerprint": fingerprint_text("visible")},
        fingerprint_text("access_key_secret"): {"redacted": True},
    }
    assert settings_seen == [settings]


@pytest.mark.asyncio
async def test_agent_loop_audits_no_prompt_deny_with_audit_metadata(monkeypatch, tmp_path):
    events, records, _settings_seen = await _run_fake_tool_with_audit(
        monkeypatch,
        tmp_path,
        PermissionResult(behavior="deny", audit=_audit_metadata(rule_source="project_settings")),
    )

    assert not _permission_requests(events)
    assert any(isinstance(event, ToolResultEvent) and event.is_error for event in events)
    assert len(records) == 1
    assert records[0].decision == "deny"
    assert records[0].scope == "settings_rule"
    assert records[0].rule_source == "project_settings"


@pytest.mark.asyncio
async def test_agent_loop_audits_dont_ask_mode_through_permission_pipeline(monkeypatch, tmp_path):
    events, records, _settings_seen = await _run_fake_tool_with_audit(
        monkeypatch,
        tmp_path,
        PermissionResult(behavior="ask", audit=_audit_metadata(scope="once", rule_source=None, rule=None)),
        context=ToolPermissionContext(cwd=str(tmp_path), mode=PermissionMode.DONT_ASK),
    )

    assert not _permission_requests(events)
    assert len(records) == 1
    assert records[0].decision == "deny"
    assert records[0].scope == "mode"
    assert records[0].rule_source == "mode"
    assert records[0].reason_type == "dont_ask"


@pytest.mark.asyncio
async def test_agent_loop_does_not_audit_read_only_no_prompt_allow(monkeypatch, tmp_path):
    events, records, _settings_seen = await _run_fake_tool_with_audit(
        monkeypatch,
        tmp_path,
        PermissionResult(behavior="allow", audit=_audit_metadata(scope="read_only", is_read_only=True)),
    )

    assert not _permission_requests(events)
    assert records == []


@pytest.mark.asyncio
async def test_agent_loop_audits_mcp_read_only_no_prompt_allow(monkeypatch, tmp_path):
    audit = PermissionAuditMetadata(
        scope="read_only",
        source="permission_pipeline",
        is_read_only=True,
        operation={
            "publicName": "mcp__yuque__search_docs",
            "originalServerName": "yuque",
            "originalToolName": "search-docs",
            "isReadOnly": True,
            "isDestructive": False,
        },
    )

    events, records, _settings_seen = await _run_fake_tool_with_audit(
        monkeypatch,
        tmp_path,
        PermissionResult(behavior="allow", audit=audit),
    )

    assert not _permission_requests(events)
    assert len(records) == 1
    assert records[0].decision == "allow"
    assert records[0].scope == "read_only"
    assert records[0].source == "permission_pipeline"
    assert records[0].operation == {
        "publicName": "mcp__yuque__search_docs",
        "originalServerName": "yuque",
        "originalToolName": "search-docs",
        "isReadOnly": True,
        "isDestructive": False,
    }


@pytest.mark.asyncio
async def test_agent_loop_writes_audit_for_real_mcp_read_only_auto_allow(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager()
    provider = FakeProvider(
        [
            _tool_turn(
                tool_name="mcp__yuque__search_docs",
                tool_input={"query": "ros", "access_key_secret": "secret-value"},
            ),
            _text_turn("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        MCPTool(
            manager=manager,
            record=MCPToolRecord(
                server_name="yuque.internal",
                tool_name="search-docs",
                public_name="mcp__yuque__search_docs",
                original_server_name="yuque",
                original_tool_name="search-docs",
                input_schema={"type": "object"},
                annotations={"readOnlyHint": True, "destructiveHint": False},
            ),
            session_id="session-mcp-readonly",
        )
    )
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="test",
        tool_registry=registry,
        cwd=str(tmp_path),
        max_turns=2,
        session_id="session-mcp-readonly",
        permission_context=ToolPermissionContext(cwd=str(tmp_path)),
    )

    events = await _collect_events(loop, "search docs", permission_handler=Mock(return_value=False))

    assert not _permission_requests(events)
    assert manager.called_with["server_name"] == "yuque"
    assert manager.called_with["tool_name"] == "search-docs"
    [row] = _read_jsonl(_session_audit_log_path(tmp_path / "config", str(tmp_path), "session-mcp-readonly"))
    assert row["decision"] == "allow"
    assert row["scope"] == "read_only"
    assert row["tool_name"] == "mcp__yuque__search_docs"
    assert row["operation"] == {
        "publicName": "mcp__yuque__search_docs",
        "originalServerName": "yuque",
        "originalToolName": "search-docs",
        "isReadOnly": True,
        "isDestructive": False,
    }
    assert "secret-value" not in str(row)


@pytest.mark.asyncio
async def test_agent_loop_audits_read_only_no_prompt_deny(monkeypatch, tmp_path):
    events, records, _settings_seen = await _run_fake_tool_with_audit(
        monkeypatch,
        tmp_path,
        PermissionResult(
            behavior="deny",
            message="blocked",
            audit=_audit_metadata(scope="settings_rule", rule_source="project_settings", is_read_only=True),
        ),
    )

    assert not _permission_requests(events)
    assert any(isinstance(event, ToolResultEvent) and event.is_error for event in events)
    assert len(records) == 1
    assert records[0].decision == "deny"
    assert records[0].scope == "settings_rule"
    assert records[0].rule_source == "project_settings"


@pytest.mark.asyncio
async def test_agent_loop_does_not_audit_no_prompt_decision_without_audit_metadata(monkeypatch, tmp_path):
    events, records, _settings_seen = await _run_fake_tool_with_audit(
        monkeypatch,
        tmp_path,
        PermissionResult(
            behavior="allow",
            reason=PermissionDecisionReason(type="rule", detail="matched allow rule: fake_permission"),
        ),
        context=ToolPermissionContext(cwd=str(tmp_path), allow_rules={"user_settings": ["fake_permission"]}),
    )

    assert not _permission_requests(events)
    assert records == []
