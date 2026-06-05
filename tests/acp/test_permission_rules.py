"""Tests for ACP rule-level permission support."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import acp
import acp.schema
import pytest

from iac_code.acp.session import (
    _OPTION_ALLOW_ALWAYS,
    _OPTION_ALLOW_ONCE,
    _OPTION_REJECT_ALWAYS,
    _OPTION_REJECT_ONCE,
    _PREFIX_ALLOW_RULE,
    _PREFIX_DENY_RULE,
    ACPSession,
)
from iac_code.tools.read_file import ReadFileTool
from iac_code.types.permissions import PermissionRuleValue, ToolPermissionContext
from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, TextDeltaEvent, Usage


@dataclass
class FakePermissionResult:
    behavior: str = "ask"
    message: str = ""
    suggestions: list[PermissionRuleValue] | None = None


class _FakeLoop:
    def __init__(self, permission_context=None):
        self._permission_context = permission_context
        self.tool_registry = MagicMock()
        self.tool_registry.get.return_value = None

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class _FakeConn:
    def __init__(self, outcome):
        self._outcome = outcome
        self.last_options: list = []
        self.last_content: str = ""

    async def session_update(self, session_id, update, **kwargs):
        pass

    async def request_permission(self, options, session_id, tool_call_update):
        self.last_options = options
        for content_item in tool_call_update.content:
            if hasattr(content_item, "content") and hasattr(content_item.content, "text"):
                self.last_content = content_item.content.text
        return self._outcome


def _make_allowed_outcome(option_id: str):
    outcome = acp.schema.AllowedOutcome(outcome="selected", optionId=option_id)
    return MagicMock(outcome=outcome)


def _make_denied_outcome(option_id: str | None = None):
    """Build a fake RequestPermissionResponse with DeniedOutcome.

    For DeniedOutcome, the ACP protocol has no option_id field on the outcome itself.
    Clients encode the selected option in response.field_meta["option_id"].
    """
    outcome = acp.schema.DeniedOutcome(outcome="cancelled")
    response = MagicMock(outcome=outcome)
    response.field_meta = {"option_id": option_id} if option_id else {}
    return response


def _make_event(tool_name="bash", tool_input=None, suggestions=None):
    perm_result = FakePermissionResult(suggestions=suggestions) if suggestions else None
    return PermissionRequestEvent(
        tool_name=tool_name,
        tool_input=tool_input or {"command": "git status"},
        tool_use_id="tu-123",
        permission_result=perm_result,
    )


# ---------------------------------------------------------------------------
# Test: Dynamic option generation with suggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_include_rule_suggestions_when_present():
    """When suggestions exist, options include rule-level allow/deny."""
    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ONCE))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event(suggestions=suggestions)

    await session._request_permission(event)

    option_ids = [opt.option_id for opt in conn.last_options]
    assert _OPTION_ALLOW_ONCE in option_ids
    assert _PREFIX_ALLOW_RULE + "git:*" in option_ids
    assert _PREFIX_DENY_RULE + "git:*" in option_ids
    assert _OPTION_REJECT_ONCE in option_ids
    assert _OPTION_REJECT_ALWAYS in option_ids
    # allow_always (tool-level) should NOT be present when suggestions exist
    assert _OPTION_ALLOW_ALWAYS not in option_ids


@pytest.mark.asyncio
async def test_options_fallback_to_tool_level_without_suggestions():
    """Without suggestions, options include tool-level allow_always."""
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ONCE))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event(suggestions=None)

    await session._request_permission(event)

    option_ids = [opt.option_id for opt in conn.last_options]
    assert _OPTION_ALLOW_ALWAYS in option_ids
    # Rule-level options should NOT be present
    assert not any(oid.startswith(_PREFIX_ALLOW_RULE) for oid in option_ids)
    assert not any(oid.startswith(_PREFIX_DENY_RULE) for oid in option_ids)


@pytest.mark.asyncio
async def test_read_file_without_suggestions_omits_allow_always():
    """read_file path prompts should not offer blanket future allow."""
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ONCE))
    loop = _FakeLoop()
    loop.tool_registry.get.return_value = ReadFileTool()
    session = ACPSession("s1", loop, conn)
    event = _make_event(tool_name="read_file", tool_input={"path": "/tmp/outside.txt"}, suggestions=None)

    await session._request_permission(event)

    option_ids = [opt.option_id for opt in conn.last_options]
    assert _OPTION_ALLOW_ALWAYS not in option_ids


# ---------------------------------------------------------------------------
# Test: allow_rule response applies rule to permission_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_rule_applies_to_permission_context():
    """allow_rule:git:* → rule added to allow_rules['session'], returns True."""
    perm_ctx = ToolPermissionContext(cwd="/tmp")
    conn = _FakeConn(_make_allowed_outcome(_PREFIX_ALLOW_RULE + "git:*"))
    loop = _FakeLoop(permission_context=perm_ctx)
    session = ACPSession("s1", loop, conn)

    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    event = _make_event(suggestions=suggestions)

    result = await session._request_permission(event)

    assert result is True
    updated_ctx = loop._permission_context
    assert "session" in updated_ctx.allow_rules
    assert "bash(git:*)" in updated_ctx.allow_rules["session"]


@pytest.mark.asyncio
async def test_allow_rule_multiple_suggestions():
    """allow_rule:curl:*,wget:* → both rules added."""
    perm_ctx = ToolPermissionContext(cwd="/tmp")
    conn = _FakeConn(_make_allowed_outcome(_PREFIX_ALLOW_RULE + "curl:*,wget:*"))
    loop = _FakeLoop(permission_context=perm_ctx)
    session = ACPSession("s1", loop, conn)

    suggestions = [
        PermissionRuleValue(tool_name="bash", rule_content="curl:*"),
        PermissionRuleValue(tool_name="bash", rule_content="wget:*"),
    ]
    event = _make_event(suggestions=suggestions)

    result = await session._request_permission(event)

    assert result is True
    rules = loop._permission_context.allow_rules.get("session", [])
    assert "bash(curl:*)" in rules
    assert "bash(wget:*)" in rules


# ---------------------------------------------------------------------------
# Test: deny_rule response applies rule to permission_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_rule_applies_to_permission_context():
    """deny_rule:curl:* → rule added to deny_rules['session'], returns False."""
    perm_ctx = ToolPermissionContext(cwd="/tmp")
    conn = _FakeConn(_make_denied_outcome(_PREFIX_DENY_RULE + "curl:*"))
    loop = _FakeLoop(permission_context=perm_ctx)
    session = ACPSession("s1", loop, conn)

    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="curl:*")]
    event = _make_event(suggestions=suggestions)

    result = await session._request_permission(event)

    assert result is False
    updated_ctx = loop._permission_context
    assert "session" in updated_ctx.deny_rules
    assert "bash(curl:*)" in updated_ctx.deny_rules["session"]


# ---------------------------------------------------------------------------
# Test: Existing tool-level behaviors unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_once_returns_true_no_cache():
    """allow_once → returns True, no cache entry."""
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ONCE))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event()

    result = await session._request_permission(event)

    assert result is True
    assert "bash" not in session._permission_cache


@pytest.mark.asyncio
async def test_allow_always_caches_tool():
    """allow_always → returns True, caches tool-level decision."""
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ALWAYS))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event()

    result = await session._request_permission(event)

    assert result is True
    assert session._permission_cache.get("bash") == "always_allow"


@pytest.mark.asyncio
async def test_reject_once_returns_false():
    """reject_once → returns False, no cache entry."""
    conn = _FakeConn(_make_denied_outcome(_OPTION_REJECT_ONCE))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event()

    result = await session._request_permission(event)

    assert result is False
    assert "bash" not in session._permission_cache


@pytest.mark.asyncio
async def test_reject_always_caches_tool():
    """reject_always → returns False, caches tool-level decision."""
    conn = _FakeConn(_make_denied_outcome(_OPTION_REJECT_ALWAYS))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event()

    result = await session._request_permission(event)

    assert result is False
    assert session._permission_cache.get("bash") == "always_deny"


# ---------------------------------------------------------------------------
# Test: Cache short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_allow_skips_permission_request():
    """Cached always_allow short-circuits without calling request_permission."""
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ONCE))
    session = ACPSession("s1", _FakeLoop(), conn)
    session._permission_cache["bash"] = "always_allow"
    event = _make_event()

    result = await session._request_permission(event)

    assert result is True
    assert conn.last_options == []  # request_permission was never called


@pytest.mark.asyncio
async def test_cached_deny_skips_permission_request():
    """Cached always_deny short-circuits without calling request_permission."""
    conn = _FakeConn(_make_denied_outcome())
    session = ACPSession("s1", _FakeLoop(), conn)
    session._permission_cache["bash"] = "always_deny"
    event = _make_event()

    result = await session._request_permission(event)

    assert result is False
    assert conn.last_options == []


# ---------------------------------------------------------------------------
# Test: Content includes rule context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_includes_suggested_rule():
    """ToolCallUpdate content includes suggested rule when suggestions exist."""
    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ONCE))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event(suggestions=suggestions)

    await session._request_permission(event)

    assert "Suggested rule: git:*" in conn.last_content
    assert "bash" in conn.last_content


@pytest.mark.asyncio
async def test_content_no_suggested_rule_without_suggestions():
    """ToolCallUpdate content does not include 'Suggested rule' when no suggestions."""
    conn = _FakeConn(_make_allowed_outcome(_OPTION_ALLOW_ONCE))
    session = ACPSession("s1", _FakeLoop(), conn)
    event = _make_event(suggestions=None)

    await session._request_permission(event)

    assert "Suggested rule" not in conn.last_content


# ---------------------------------------------------------------------------
# Test: No permission_context graceful handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_rule_without_permission_context_still_returns_true():
    """allow_rule still returns True even when no permission_context is available."""
    conn = _FakeConn(_make_allowed_outcome(_PREFIX_ALLOW_RULE + "git:*"))
    loop = _FakeLoop(permission_context=None)
    session = ACPSession("s1", loop, conn)

    suggestions = [PermissionRuleValue(tool_name="bash", rule_content="git:*")]
    event = _make_event(suggestions=suggestions)

    result = await session._request_permission(event)

    assert result is True
    # No crash, permission_context remains None
    assert loop._permission_context is None
