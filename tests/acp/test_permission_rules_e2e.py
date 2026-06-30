"""End-to-end test: ACP permission flow with rule-level options.

Verifies that the ACP session generates dynamic rule-level permission options
and correctly applies rules to the permission_context when selected.

Run with:
    uv run python -m pytest tests/acp/test_permission_rules_e2e.py -v -s
"""

from __future__ import annotations

from unittest.mock import MagicMock

import acp
import acp.schema
import pytest

from iac_code.acp.session import ACPSession
from iac_code.types.permissions import PermissionRuleValue, ToolPermissionContext
from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, TextDeltaEvent, Usage


class _PermissionTriggeringLoop:
    """Fake agent loop that emits a PermissionRequestEvent with suggestions."""

    def __init__(self, suggestions: list[PermissionRuleValue]):
        self._permission_context = ToolPermissionContext(cwd="/tmp")
        self._suggestions = suggestions

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="Running command...")
        yield PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "git push origin main"},
            tool_use_id="tu-perm-1",
            permission_result=MagicMock(
                behavior="ask",
                suggestions=self._suggestions,
            ),
        )
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class _CapturingConn:
    """Fake ACP connection that captures permission requests and returns a configurable response."""

    def __init__(self, response_option_id: str = "allow_once"):
        self.updates: list = []
        self.captured_options: list[acp.schema.PermissionOption] = []
        self.captured_content: str = ""
        self._response_option_id = response_option_id

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)

    async def request_permission(self, options, session_id, tool_call_update):
        self.captured_options = options
        for item in tool_call_update.content:
            if hasattr(item, "content") and hasattr(item.content, "text"):
                self.captured_content = item.content.text

        # Determine if response is allow or deny
        if self._response_option_id.startswith("allow") or self._response_option_id == "allow_once":
            outcome = acp.schema.AllowedOutcome(outcome="selected", optionId=self._response_option_id)
        else:
            outcome = acp.schema.DeniedOutcome(outcome="cancelled")
        resp = MagicMock(outcome=outcome, field_meta={"option_id": self._response_option_id})
        return resp


@pytest.mark.asyncio
async def test_permission_request_shows_rule_options():
    """ACP session generates rule-level options when suggestions are present."""
    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    conn = _CapturingConn(response_option_id="allow_once")
    loop = _PermissionTriggeringLoop(suggestions)
    session = ACPSession("test-e2e-1", loop, conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="push my code")])

    option_ids = [opt.option_id for opt in conn.captured_options]
    print("\n[Permission Options]:", option_ids)

    assert "allow_once" in option_ids
    assert "allow_rule:git:*" in option_ids
    assert "reject_once" in option_ids
    assert "deny_rule:git:*" in option_ids
    assert "reject_always" in option_ids
    # tool-level allow_always should NOT be present when rule suggestions exist
    assert "allow_always" not in option_ids


@pytest.mark.asyncio
async def test_permission_content_shows_command_and_rule():
    """ToolCallUpdate content includes redacted input and suggested rule."""
    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    conn = _CapturingConn(response_option_id="allow_once")
    loop = _PermissionTriggeringLoop(suggestions)
    session = ACPSession("test-e2e-2", loop, conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="push")])

    print("\n[Content]:", conn.captured_content)
    assert "Input:" in conn.captured_content
    assert "command" in conn.captured_content
    assert "git push origin main" in conn.captured_content
    assert "Suggested rule: git:*" in conn.captured_content


@pytest.mark.asyncio
async def test_selecting_allow_rule_persists_to_permission_context():
    """Selecting allow_rule:git:* writes the rule to permission_context.allow_rules['session']."""
    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    conn = _CapturingConn(response_option_id="allow_rule:git:*")
    loop = _PermissionTriggeringLoop(suggestions)
    session = ACPSession("test-e2e-3", loop, conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="push")])

    perm_ctx = loop._permission_context
    print("\n[Allow Rules]:", perm_ctx.allow_rules)
    assert "session" in perm_ctx.allow_rules
    assert "bash(git:*)" in perm_ctx.allow_rules["session"]


@pytest.mark.asyncio
async def test_selecting_deny_rule_persists_to_permission_context():
    """Selecting deny_rule:git:* writes the rule to permission_context.deny_rules['session']."""
    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    conn = _CapturingConn(response_option_id="deny_rule:git:*")
    loop = _PermissionTriggeringLoop(suggestions)
    session = ACPSession("test-e2e-4", loop, conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="push")])

    perm_ctx = loop._permission_context
    print("\n[Deny Rules]:", perm_ctx.deny_rules)
    assert "session" in perm_ctx.deny_rules
    assert "bash(git:*)" in perm_ctx.deny_rules["session"]
