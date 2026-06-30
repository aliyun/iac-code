"""Permission audit coverage for AgentTool child-boundary decisions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from iac_code.agent.agent_tool import run_sub_agent
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings
from iac_code.types.stream_events import PermissionRequestEvent


@pytest.mark.asyncio
async def test_agent_tool_auto_denial_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    audit_records = []

    async def fake_stream(_prompt):
        yield PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "rm -rf build", "accessKeyId": "secret-ak"},
            tool_use_id="toolu-child",
            response_future=future,
            audit_context={
                "session_id": "session-child",
                "settings": PermissionAuditSettings(include_tool_input=True),
                "metadata": PermissionAuditMetadata(
                    scope="settings_rule",
                    source="permission_pipeline",
                    rule_source="project_settings",
                    rule="bash(rm:*)",
                    reason_type="rule",
                    reason_detail="matched ask rule: bash(rm:*)",
                    is_read_only=False,
                    operation={"is_read_only": False},
                ),
            },
        )

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            self.context_manager = SimpleNamespace(get_total_tokens=lambda: 321)

        def run_streaming(self, prompt):
            return fake_stream(prompt)

    monkeypatch.setattr(
        "iac_code.agent.agent_tool.get_agent_definition",
        lambda agent_type: SimpleNamespace(max_turns=3),
    )
    monkeypatch.setattr("iac_code.agent.agent_tool.filter_tools", lambda registry, defn: "filtered-tools")
    monkeypatch.setattr("iac_code.agent.system_prompt.build_system_prompt", lambda cwd=None: "built prompt")
    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: audit_records.append((record, settings)),
    )

    await run_sub_agent(
        prompt="demo",
        parent_provider_manager="pm",
        parent_tool_registry="registry",
    )

    assert future.result() is False
    assert len(audit_records) == 1
    record, settings_seen = audit_records[0]
    assert settings_seen.include_tool_input is True
    assert record.session_id == "session-child"
    assert record.source == "agent_tool_auto_deny"
    assert record.scope == "auto_deny"
    assert record.decision == "deny"
    assert record.rule_source == "project_settings"
    assert record.rule == "bash(rm:*)"
    assert record.operation == {"is_read_only": False}
    assert record.tool_input_redacted == {
        "command": {"type": "str", "length": 12, "fingerprint": fingerprint_text("rm -rf build")},
        fingerprint_text("accessKeyId"): {"redacted": True},
    }
