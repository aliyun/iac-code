from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import acp
import pytest

from iac_code.acp.session import (
    _OPTION_ALLOW_ALWAYS,
    _OPTION_ALLOW_ONCE,
    _OPTION_REJECT_ALWAYS,
    _OPTION_REJECT_ONCE,
    ACPSession,
)
from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings
from iac_code.types.stream_events import PermissionRequestEvent, TextDeltaEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeConn:
    """Configurable fake ACP client connection for permission tests.

    Supported outcome values:
    - "allow_once": AllowedOutcome with option_id="allow_once"
    - "allow_always": AllowedOutcome with option_id="allow_always"
    - "reject_once": DeniedOutcome (no extra metadata)
    - "reject_always": DeniedOutcome with response _meta={"option_id": "reject_always"}
    """

    def __init__(self, *, outcome: str = "allow_once") -> None:
        self._outcome = outcome
        self.permission_requests: list[dict] = []
        self.updates: list = []

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        self.permission_requests.append(
            {
                "options": options,
                "session_id": session_id,
                "tool_call": tool_call,
            }
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
        # reject_once (default deny)
        return acp.schema.RequestPermissionResponse(outcome=acp.schema.DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)


def _make_session(session_id: str, agent_loop, conn: object) -> ACPSession:
    return ACPSession(session_id, agent_loop, cast(acp.Client, conn))


class FakeLoopApprove:
    """Loop that yields one permission request then text."""

    async def run_streaming(self, prompt):
        future = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="write_file",
            tool_input={"path": "main.tf", "content": "resource {}"},
            tool_use_id="tool1",
            response_future=future,
        )
        result = await future
        if result:
            yield TextDeltaEvent(text="executed")
        else:
            yield TextDeltaEvent(text="denied")


class FakeMCPTool:
    supports_blanket_allow = True

    def user_facing_name(self, tool_input):
        return "MCP yuque/search-docs"


class FakeMCPRegistry:
    def get(self, tool_name: str):
        return FakeMCPTool() if tool_name == "mcp__yuque__search_docs" else None

    def list_tools(self):
        return []


class FakeLoopMCPPermission:
    tool_registry = FakeMCPRegistry()

    async def run_streaming(self, prompt):
        future = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="mcp__yuque__search_docs",
            tool_input={"query": "ros", "api_token": "IAC_PRIVATE_PERMISSION_TOKEN_37"},
            tool_use_id="mcp_tool1",
            response_future=future,
            permission_result=SimpleNamespace(suggestions=[]),
            audit_context={
                "session_id": "internal-session-id-37",
                "settings": PermissionAuditSettings(max_file_bytes=123, max_files=2),
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="mcp",
                    rule_source="secret-rule-source",
                    rule="secret-rule-37",
                    reason_type="secret-reason-type",
                    reason_detail="secret-reason-detail",
                    is_read_only=False,
                    operation={
                        "publicName": "mcp__yuque__search_docs",
                        "originalServerName": "yuque",
                        "originalToolName": "search-docs",
                        "isReadOnly": False,
                        "isDestructive": True,
                    },
                ),
            },
        )
        await future
        yield TextDeltaEvent(text="done")


class FakeLoopMultiPermission:
    """Loop that yields multiple permission requests in one turn."""

    async def run_streaming(self, prompt):
        future1 = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="write_file",
            tool_input={"path": "a.tf"},
            tool_use_id="tool_a",
            response_future=future1,
        )
        await future1

        future2 = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "rm -rf /"},
            tool_use_id="tool_b",
            response_future=future2,
        )
        await future2

        yield TextDeltaEvent(text="done")


class FakeLoopSlow:
    """Loop that blocks long enough to be cancelled."""

    async def run_streaming(self, prompt):
        future = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="write_file",
            tool_input={"path": "main.tf"},
            tool_use_id="tool1",
            response_future=future,
        )
        await asyncio.sleep(10)  # will be cancelled
        yield TextDeltaEvent(text="never")


# ---------------------------------------------------------------------------
# Permission approve → tool execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_approve_leads_to_tool_execution() -> None:
    conn = FakeConn(outcome="allow_once")
    session = _make_session("s1", FakeLoopApprove(), conn)

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="create main.tf")])

    assert response.stop_reason == "end_turn"
    assert len(conn.permission_requests) == 1
    # Verify the text event after approval was forwarded
    texts = [u for u in conn.updates if getattr(u, "session_update", None) == "agent_message_chunk"]
    assert len(texts) >= 1


# ---------------------------------------------------------------------------
# Permission deny → denied flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_deny_leads_to_denied_text() -> None:
    conn = FakeConn(outcome="reject_once")
    session = _make_session("s1", FakeLoopApprove(), conn)

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="create main.tf")])

    assert response.stop_reason == "end_turn"
    assert len(conn.permission_requests) == 1
    # The FakeLoop should have received False and yielded "denied"
    texts = [u for u in conn.updates if getattr(u, "session_update", None) == "agent_message_chunk"]
    assert any("denied" in getattr(getattr(t, "content", None), "text", "") for t in texts)


# ---------------------------------------------------------------------------
# Permission request cancelled during wait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_request_cancelled_during_wait() -> None:
    conn = FakeConn(outcome="allow_once")
    session = _make_session("s1", FakeLoopSlow(), conn)

    # Start prompt and cancel quickly
    task = asyncio.create_task(session.prompt([acp.schema.TextContentBlock(type="text", text="go")]))
    await asyncio.sleep(0.05)
    await session.cancel()

    response = await task
    assert response.stop_reason == "cancelled"


# ---------------------------------------------------------------------------
# Multiple tools requesting permission in same turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_tools_requesting_permission_same_turn() -> None:
    conn = FakeConn(outcome="allow_once")
    session = _make_session("s1", FakeLoopMultiPermission(), conn)

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="do both")])

    assert response.stop_reason == "end_turn"
    assert len(conn.permission_requests) == 2
    # Verify different tool_call_ids
    ids = [r["tool_call"].tool_call_id for r in conn.permission_requests]
    assert ids[0] != ids[1]
    assert "tool_a" in ids[0]
    assert "tool_b" in ids[1]


# ---------------------------------------------------------------------------
# Permission request format correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_request_format() -> None:
    conn = FakeConn(outcome="allow_once")
    session = _make_session("s1", FakeLoopApprove(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    assert len(conn.permission_requests) == 1
    req = conn.permission_requests[0]

    # Verify all 4 permission options are sent
    option_ids = {o.option_id for o in req["options"]}
    assert option_ids == {_OPTION_ALLOW_ONCE, _OPTION_ALLOW_ALWAYS, _OPTION_REJECT_ONCE, _OPTION_REJECT_ALWAYS}

    # Verify kinds match option_ids
    option_kinds = {o.option_id: o.kind for o in req["options"]}
    assert option_kinds[_OPTION_ALLOW_ONCE] == "allow_once"
    assert option_kinds[_OPTION_ALLOW_ALWAYS] == "allow_always"
    assert option_kinds[_OPTION_REJECT_ONCE] == "reject_once"
    assert option_kinds[_OPTION_REJECT_ALWAYS] == "reject_always"

    # Verify ToolCallUpdate format
    tool_call = req["tool_call"]
    assert "permission/" in tool_call.tool_call_id
    assert tool_call.title == "write_file"
    assert len(tool_call.content) >= 1
    wire = tool_call.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    assert "_meta" not in wire
    assert not {"publicName", "originalServerName", "originalToolName", "isReadOnly", "isDestructive"} & set(wire)


@pytest.mark.asyncio
async def test_mcp_permission_request_wire_includes_safe_metadata() -> None:
    conn = FakeConn(outcome="allow_once")
    session = _make_session("s1", FakeLoopMCPPermission(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    assert len(conn.permission_requests) == 1
    tool_call = conn.permission_requests[0]["tool_call"]
    assert tool_call.title == "MCP yuque/search-docs"
    wire = tool_call.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    wire_text = json.dumps(wire, sort_keys=True)
    assert "IAC_PRIVATE_PERMISSION_TOKEN_37" not in wire_text
    for internal_value in (
        "internal-session-id-37",
        "secret-rule-source",
        "secret-rule-37",
        "secret-reason-type",
        "secret-reason-detail",
        "max_file_bytes",
    ):
        assert internal_value not in wire_text
    permission = wire["_meta"]["iac_code"]["permission"]
    assert permission["permissionId"] == "perm-mcp_tool1"
    assert permission["toolName"] == "mcp__yuque__search_docs"
    assert permission["toolUseId"] == "mcp_tool1"
    assert permission["scope"] == "once"
    assert permission["publicName"] == "mcp__yuque__search_docs"
    assert permission["originalServerName"] == "yuque"
    assert permission["originalToolName"] == "search-docs"
    assert permission["isReadOnly"] is False
    assert permission["isDestructive"] is True
    assert permission["inputSummary"]["tool_name"] == "mcp__yuque__search_docs"


# ---------------------------------------------------------------------------
# Permission cache — allow_always skips future requests
# ---------------------------------------------------------------------------


class FakeLoopRepeatedTool:
    """Loop that yields the same tool permission request twice."""

    async def run_streaming(self, prompt):
        future1 = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="write_file",
            tool_input={"path": "a.tf"},
            tool_use_id="tool_1",
            response_future=future1,
        )
        await future1

        future2 = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="write_file",
            tool_input={"path": "b.tf"},
            tool_use_id="tool_2",
            response_future=future2,
        )
        await future2

        yield TextDeltaEvent(text="done")


@pytest.mark.asyncio
async def test_allow_always_caches_and_skips_second_request() -> None:
    conn = FakeConn(outcome="allow_always")
    session = _make_session("s1", FakeLoopRepeatedTool(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    # Only the first call should reach the client; second is served from cache
    assert len(conn.permission_requests) == 1
    assert session._permission_cache.get("write_file") == "always_allow"


# ---------------------------------------------------------------------------
# Permission cache — reject_always skips future requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_always_caches_and_skips_second_request() -> None:
    conn = FakeConn(outcome="reject_always")
    session = _make_session("s1", FakeLoopRepeatedTool(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    # Only the first call should reach the client; second is served from cache
    assert len(conn.permission_requests) == 1
    assert session._permission_cache.get("write_file") == "always_deny"


# ---------------------------------------------------------------------------
# allow_once does NOT cache — asks every time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_once_does_not_cache() -> None:
    conn = FakeConn(outcome="allow_once")
    session = _make_session("s1", FakeLoopRepeatedTool(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    # Both calls should reach the client since allow_once doesn't cache
    assert len(conn.permission_requests) == 2
    assert "write_file" not in session._permission_cache


# ---------------------------------------------------------------------------
# reject_once does NOT cache — asks every time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_once_does_not_cache() -> None:
    conn = FakeConn(outcome="reject_once")
    session = _make_session("s1", FakeLoopRepeatedTool(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    # Both calls should reach the client since reject_once doesn't cache
    assert len(conn.permission_requests) == 2
    assert "write_file" not in session._permission_cache


# ---------------------------------------------------------------------------
# Cache is per-tool — different tools not affected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_is_per_tool() -> None:
    conn = FakeConn(outcome="allow_always")
    session = _make_session("s1", FakeLoopMultiPermission(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    # Both different tools should still trigger a permission request
    assert len(conn.permission_requests) == 2
    assert session._permission_cache.get("write_file") == "always_allow"
    assert session._permission_cache.get("bash") == "always_allow"


# ---------------------------------------------------------------------------
# Permission memory is per-session — separate sessions don't share cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_memory_is_per_session() -> None:
    """Scenario 6: allow_always in session1 does NOT affect session2."""
    conn = FakeConn(outcome="allow_always")

    session1 = _make_session("s1", FakeLoopRepeatedTool(), conn)
    await session1.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    # session1 should have cached allow_always for write_file
    assert session1._permission_cache.get("write_file") == "always_allow"

    # session2 is a brand-new session — its cache should be empty
    session2 = _make_session("s2", FakeLoopRepeatedTool(), conn)
    assert session2._permission_cache == {}

    # Running session2 should still trigger permission requests (not auto-allowed)
    conn.permission_requests.clear()  # reset counter
    await session2.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    # First call triggers a real permission request, second is cached within session2
    assert len(conn.permission_requests) == 1  # only 1 because allow_always caches in session2 too
    assert session2._permission_cache.get("write_file") == "always_allow"


# ---------------------------------------------------------------------------
# Concurrent permission requests in separate sessions don't interfere
# ---------------------------------------------------------------------------


class FakeLoopSinglePermission:
    """Loop that yields one permission request with a delay to simulate concurrency."""

    def __init__(self, tool_name: str = "write_file", delay: float = 0.05) -> None:
        self._tool_name = tool_name
        self._delay = delay

    async def run_streaming(self, prompt):
        future = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name=self._tool_name,
            tool_input={"path": "test.tf"},
            tool_use_id=f"tool_{self._tool_name}",
            response_future=future,
        )
        result = await future
        if result:
            yield TextDeltaEvent(text=f"{self._tool_name}_executed")
        else:
            yield TextDeltaEvent(text=f"{self._tool_name}_denied")


class FakeConnPerSession:
    """Fake conn that returns different outcomes per session_id."""

    def __init__(self, session_outcomes: dict[str, str]) -> None:
        self._session_outcomes = session_outcomes
        self.permission_requests: list[dict] = []
        self.updates: list = []

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        outcome_key = self._session_outcomes.get(session_id, "allow_once")
        self.permission_requests.append(
            {
                "options": options,
                "session_id": session_id,
                "tool_call": tool_call,
            }
        )
        if outcome_key in ("allow_once", "allow_always"):
            return acp.schema.RequestPermissionResponse(
                outcome=acp.schema.AllowedOutcome(outcome="selected", option_id=outcome_key)
            )
        if outcome_key == "reject_always":
            return acp.schema.RequestPermissionResponse(
                outcome=acp.schema.DeniedOutcome(outcome="cancelled"),
                field_meta={"option_id": "reject_always"},
            )
        return acp.schema.RequestPermissionResponse(outcome=acp.schema.DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))


@pytest.mark.asyncio
async def test_concurrent_permission_requests_do_not_interfere() -> None:
    """Scenario 13: Two sessions trigger permission concurrently; decisions are independent."""
    conn = FakeConnPerSession(
        session_outcomes={
            "sess_a": "allow_once",
            "sess_b": "reject_once",
        }
    )

    session_a = _make_session("sess_a", FakeLoopSinglePermission("write_file"), conn)
    session_b = _make_session("sess_b", FakeLoopSinglePermission("write_file"), conn)

    # Run both prompts concurrently
    resp_a, resp_b = await asyncio.gather(
        session_a.prompt([acp.schema.TextContentBlock(type="text", text="go a")]),
        session_b.prompt([acp.schema.TextContentBlock(type="text", text="go b")]),
    )

    assert resp_a.stop_reason == "end_turn"
    assert resp_b.stop_reason == "end_turn"

    # Both sessions should have triggered exactly 1 permission request each
    sess_a_reqs = [r for r in conn.permission_requests if r["session_id"] == "sess_a"]
    sess_b_reqs = [r for r in conn.permission_requests if r["session_id"] == "sess_b"]
    assert len(sess_a_reqs) == 1
    assert len(sess_b_reqs) == 1

    # Verify session_a got allowed (text contains "executed") and session_b got denied
    a_updates = [u for sid, u in conn.updates if sid == "sess_a"]
    b_updates = [u for sid, u in conn.updates if sid == "sess_b"]
    a_texts = [getattr(getattr(u, "content", None), "text", "") for u in a_updates]
    b_texts = [getattr(getattr(u, "content", None), "text", "") for u in b_updates]
    assert any("executed" in t for t in a_texts)
    assert any("denied" in t for t in b_texts)


# ---------------------------------------------------------------------------
# Basic permission request test (from test_permissions.py)
# ---------------------------------------------------------------------------


class _BasicPermissionLoop:
    """Loop that yields one permission request then text (always approved)."""

    async def run_streaming(self, prompt):
        future = asyncio.get_running_loop().create_future()
        yield PermissionRequestEvent(
            tool_name="write_file",
            tool_input={"path": "main.tf"},
            tool_use_id="tool1",
            response_future=future,
        )
        assert await future is True
        yield TextDeltaEvent(text="approved")


@pytest.mark.asyncio
async def test_permission_request_uses_acp_client() -> None:
    conn = FakeConn(outcome="allow_once")
    session = _make_session("s1", _BasicPermissionLoop(), conn)

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="go")])

    assert response.stop_reason == "end_turn"
    assert len(conn.permission_requests) == 1
    assert conn.updates[0].session_update == "agent_message_chunk"


# ---------------------------------------------------------------------------
# Permission cache is a bounded LRU
# ---------------------------------------------------------------------------


def test_permission_cache_evicts_oldest_when_above_limit(monkeypatch) -> None:
    """``_cache_permission`` must drop the least-recently-used decision once the
    cap is exceeded so a long-lived session can't grow this map without bound."""
    from iac_code.state import app_state as app_state_module

    # Use a tiny cap to keep the test fast and obvious.
    monkeypatch.setattr(app_state_module, "_PERMISSION_CACHE_MAX_SIZE", 3)

    conn_stub = object()  # never actually used here
    session = _make_session("lru-session", object(), conn_stub)

    session._cache_permission("tool_a", "always_allow")
    session._cache_permission("tool_b", "always_allow")
    session._cache_permission("tool_c", "always_allow")
    # Touch tool_a so tool_b becomes least-recently-used.
    session._cache_permission("tool_a", "always_allow")
    # Inserting a 4th distinct entry must evict tool_b, not tool_a.
    session._cache_permission("tool_d", "always_deny")

    assert set(session._permission_cache) == {"tool_a", "tool_c", "tool_d"}
    assert "tool_b" not in session._permission_cache
    assert session._permission_cache["tool_d"] == "always_deny"


def test_permission_cache_repeated_writes_do_not_grow(monkeypatch) -> None:
    """Writing the same tool repeatedly must not bypass the cap."""
    from iac_code.state import app_state as app_state_module

    monkeypatch.setattr(app_state_module, "_PERMISSION_CACHE_MAX_SIZE", 2)

    conn_stub = object()
    session = _make_session("lru-session", object(), conn_stub)

    for i in range(50):
        session._cache_permission(f"tool_{i % 4}", "always_allow")

    # Cap is 2, so after the loop we must have at most 2 entries.
    assert len(session._permission_cache) == 2
