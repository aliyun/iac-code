"""End-to-end permission scenario tests using a mock LLM provider.

These tests simulate real user workflows by mocking the LLM to return specific
bash commands, then verifying the permission pipeline produces correct events
(ask, allow, deny) and correct tool results.

This is the "automated testing mechanism" requested to replace manual REPL testing.
"""

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.tools.base import ToolRegistry
from iac_code.types.permissions import (
    PermissionMode,
    PermissionRuleValue,
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


def _ctx(mode=PermissionMode.DEFAULT, allow=None, deny=None, ask=None, cwd="/project"):
    return ToolPermissionContext(
        mode=mode,
        cwd=cwd,
        allow_rules=allow or {},
        deny_rules=deny or {},
        ask_rules=ask or {},
    )


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


def _bash_turn(tool_use_id: str, command: str, *, text: str = "") -> list:
    """Build a fake LLM turn that calls bash with the given command."""
    events = [MessageStartEvent(message_id=f"msg-{tool_use_id}")]
    if text:
        events.append(TextDeltaEvent(text=text))
    events.extend(
        [
            ToolUseStartEvent(tool_use_id=tool_use_id, name="bash"),
            ToolUseEndEvent(tool_use_id=tool_use_id, name="bash", input={"command": command}),
            MessageEndEvent(stop_reason="tool_use", usage=Usage()),
        ]
    )
    return events


def _read_file_turn(tool_use_id: str, path: str, *, text: str = "") -> list:
    """Build a fake LLM turn that calls read_file with the given path."""
    events = [MessageStartEvent(message_id=f"msg-{tool_use_id}")]
    if text:
        events.append(TextDeltaEvent(text=text))
    events.extend(
        [
            ToolUseStartEvent(tool_use_id=tool_use_id, name="read_file"),
            ToolUseEndEvent(tool_use_id=tool_use_id, name="read_file", input={"path": path}),
            MessageEndEvent(stop_reason="tool_use", usage=Usage()),
        ]
    )
    return events


def _text_turn(text: str) -> list:
    """Build a fake LLM turn that just responds with text (no tool calls)."""
    return [
        MessageStartEvent(message_id="msg-text"),
        TextDeltaEvent(text=text),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]


async def _collect_events(loop: AgentLoop, prompt: str, permission_handler=None):
    """Run the agent loop and collect all events, handling permissions automatically."""
    events = []
    async for event in loop.run_streaming(prompt):
        events.append(event)
        if isinstance(event, PermissionRequestEvent) and event.response_future:
            if permission_handler:
                result = permission_handler(event)
            else:
                result = False
            event.response_future.set_result(result)
    return events


def _has_permission_request(events) -> bool:
    return any(isinstance(e, PermissionRequestEvent) for e in events)


def _tool_results(events) -> list[ToolResultEvent]:
    return [e for e in events if isinstance(e, ToolResultEvent)]


def _permission_events(events) -> list[PermissionRequestEvent]:
    return [e for e in events if isinstance(e, PermissionRequestEvent)]


class TestSingleCommandPermissionScenarios:
    """Single-turn scenarios: verify ask/allow/deny for individual commands."""

    @pytest.mark.asyncio
    async def test_readonly_command_auto_allowed(self):
        """ls should auto-allow without prompting."""
        provider = FakeProvider([_bash_turn("t1", "ls -la"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(),
        )
        events = await _collect_events(loop, "list files")
        assert not _has_permission_request(events)
        results = _tool_results(events)
        assert any(not r.is_error for r in results)

    @pytest.mark.asyncio
    async def test_loop_trusted_read_roots_apply_to_read_file_permissions(self, tmp_path):
        """Agent loop skill read roots should be available to permission checks and tool execution."""
        project = tmp_path / "project"
        skill_root = tmp_path / "skill"
        project.mkdir()
        skill_root.mkdir()
        reference = skill_root / "template-parameters.md"
        reference.write_text("Skill reference content", encoding="utf-8")

        provider = FakeProvider([_read_file_turn("t1", str(reference)), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            cwd=str(project),
            max_turns=2,
            permission_context=ToolPermissionContext(cwd=str(project)),
            tool_context_trusted_read_directories=[str(skill_root)],
        )

        events = await _collect_events(loop, "read skill reference")

        assert not _has_permission_request(events)
        results = _tool_results(events)
        assert any("Skill reference content" in result.result for result in results)

    @pytest.mark.asyncio
    async def test_session_trusted_read_roots_do_not_change_relative_read_lookup(self, tmp_path):
        """Non-pipeline trusted read roots should not make read_file resolve relative paths from them."""
        project = tmp_path / "project"
        session_root = tmp_path / "session-artifacts"
        project.mkdir()
        reference = session_root / "references" / "template-parameters.md"
        reference.parent.mkdir(parents=True)
        reference.write_text("Session reference content", encoding="utf-8")

        provider = FakeProvider([_read_file_turn("t1", "references/template-parameters.md"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            cwd=str(project),
            max_turns=2,
            permission_context=ToolPermissionContext(
                cwd=str(project),
                trusted_read_directories=[str(session_root)],
            ),
        )

        events = await _collect_events(loop, "read session reference")

        assert not _has_permission_request(events)
        results = _tool_results(events)
        assert any(result.is_error for result in results)
        expected_error = f"File not found: {project / 'references' / 'template-parameters.md'}"
        assert any(expected_error in result.result for result in results)
        assert not any("Session reference content" in result.result for result in results)

    @pytest.mark.asyncio
    async def test_curl_requires_permission(self):
        """curl should prompt for permission."""
        provider = FakeProvider(
            [
                _bash_turn("t1", "curl https://example.com"),
                _text_turn("denied"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(),
        )
        events = await _collect_events(loop, "run curl")
        assert _has_permission_request(events)

    @pytest.mark.asyncio
    async def test_mkdir_requires_permission(self):
        """mkdir should prompt for permission."""
        provider = FakeProvider(
            [
                _bash_turn("t1", "mkdir test_dir"),
                _text_turn("denied"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(),
        )
        events = await _collect_events(loop, "create dir")
        assert _has_permission_request(events)


class TestRejectOnceScenarios:
    """Verify "reject once" behavior: same command on retry should still prompt."""

    @pytest.mark.asyncio
    async def test_curl_reject_once_prompts_again(self):
        """After rejecting curl once, the SAME command should prompt again on retry."""
        provider = FakeProvider(
            [
                _bash_turn("t1", "curl https://example.com"),
                _text_turn("ok I won't"),
                _bash_turn("t2", "curl https://example.com"),
                _text_turn("denied again"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        ctx = _ctx()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=ctx,
        )

        events1 = await _collect_events(loop, "run curl")
        perms1 = _permission_events(events1)
        assert len(perms1) == 1, "First call should prompt once"

        events2 = await _collect_events(loop, "run curl again")
        perms2 = _permission_events(events2)
        assert len(perms2) == 1, "Second call should ALSO prompt (reject-once is not persistent)"

    @pytest.mark.asyncio
    async def test_mkdir_reject_once_prompts_again(self):
        """After rejecting mkdir once, the same command should prompt again."""
        provider = FakeProvider(
            [
                _bash_turn("t1", "mkdir test_dir"),
                _text_turn("ok"),
                _bash_turn("t2", "mkdir test_dir"),
                _text_turn("denied again"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(),
        )

        events1 = await _collect_events(loop, "create dir")
        assert _has_permission_request(events1)

        events2 = await _collect_events(loop, "create dir again")
        assert _has_permission_request(events2), "Reject-once must not prevent future prompts"


class TestCompoundCommandScenarios:
    """Verify compound commands with mixed readonly/non-readonly subcommands."""

    @pytest.mark.asyncio
    async def test_compound_with_readonly_and_nonreadonly_asks(self):
        """cd && pwd && mkdir should ask (mkdir is non-readonly)."""
        provider = FakeProvider(
            [
                _bash_turn("t1", "cd /project && pwd && mkdir test_dir"),
                _text_turn("denied"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(),
        )
        events = await _collect_events(loop, "create dir")
        assert _has_permission_request(events), "Compound with non-readonly must prompt"

    @pytest.mark.asyncio
    async def test_compound_with_redirect_asks(self):
        """cd && pwd && mkdir 2>&1 || echo should ask (the exact bug scenario)."""
        provider = FakeProvider(
            [
                _bash_turn("t1", 'cd /project && pwd && mkdir test_dir 2>&1 || echo "Failed"'),
                _text_turn("denied"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(),
        )
        events = await _collect_events(loop, "create dir")
        assert _has_permission_request(events), "Compound with redirect + non-readonly must prompt"

    @pytest.mark.asyncio
    async def test_all_readonly_compound_no_prompt(self, tmp_path):
        """ls && pwd && cat should NOT prompt (all readonly)."""
        (tmp_path / "file.txt").write_text("ok", encoding="utf-8")
        provider = FakeProvider([_bash_turn("t1", "ls && pwd && cat file.txt"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(cwd=str(tmp_path)),
        )
        events = await _collect_events(loop, "show info")
        assert not _has_permission_request(events), "All-readonly compound should auto-allow"


class TestSessionRuleScenarios:
    """Verify session rules applied via the context getter propagate correctly."""

    @pytest.mark.asyncio
    async def test_session_allow_rule_skips_prompt(self):
        """After adding a session allow rule, matching commands should auto-allow."""
        from iac_code.services.permissions.storage import apply_session_rule

        ctx_holder = [_ctx()]
        sug = PermissionRuleValue(tool_name="bash", rule_content="curl:*")
        ctx_holder[0] = apply_session_rule(ctx_holder[0], "allow", sug)

        provider = FakeProvider([_bash_turn("t1", "curl https://example.com"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context_getter=lambda: ctx_holder[0],
        )
        events = await _collect_events(loop, "curl it")
        assert not _has_permission_request(events), "Session allow rule should skip prompt"

    @pytest.mark.asyncio
    async def test_context_getter_reflects_mid_session_changes(self):
        """Getter should pick up changes made after AgentLoop construction."""
        from iac_code.services.permissions.storage import apply_session_rule

        ctx_holder = [_ctx()]

        provider = FakeProvider(
            [
                _bash_turn("t1", "curl https://example.com"),
                _text_turn("denied"),
                _bash_turn("t2", "curl https://other.com"),
                _text_turn("done"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context_getter=lambda: ctx_holder[0],
        )

        events1 = await _collect_events(loop, "curl")
        assert _has_permission_request(events1), "Before session rule: should prompt"

        sug = PermissionRuleValue(tool_name="bash", rule_content="curl:*")
        ctx_holder[0] = apply_session_rule(ctx_holder[0], "allow", sug)

        events2 = await _collect_events(loop, "curl again")
        assert not _has_permission_request(events2), "After session rule: should auto-allow"


class TestPermissionModeScenarios:
    """Verify different permission modes produce correct behavior."""

    @pytest.mark.asyncio
    async def test_bypass_mode_auto_allows(self):
        """BYPASS_PERMISSIONS mode should auto-allow everything."""
        provider = FakeProvider([_bash_turn("t1", "rm -rf /tmp/test"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(mode=PermissionMode.BYPASS_PERMISSIONS),
        )
        events = await _collect_events(loop, "delete")
        assert not _has_permission_request(events)

    @pytest.mark.asyncio
    async def test_dont_ask_mode_auto_denies(self):
        """DONT_ASK mode should auto-deny commands that would normally ask."""
        provider = FakeProvider([_bash_turn("t1", "curl https://example.com"), _text_turn("denied")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(mode=PermissionMode.DONT_ASK),
        )
        events = await _collect_events(loop, "curl")
        assert not _has_permission_request(events), "DONT_ASK should not prompt"
        results = _tool_results(events)
        assert any(r.is_error for r in results), "DONT_ASK should deny"

    @pytest.mark.asyncio
    async def test_accept_edits_allows_filesystem(self):
        """ACCEPT_EDITS mode should auto-allow filesystem commands like mkdir."""
        provider = FakeProvider([_bash_turn("t1", "mkdir /project/foo"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(mode=PermissionMode.ACCEPT_EDITS),
        )
        events = await _collect_events(loop, "create dir")
        assert not _has_permission_request(events), "ACCEPT_EDITS should auto-allow mkdir"


class TestDenyRuleScenarios:
    """Verify deny rules block commands without prompting."""

    @pytest.mark.asyncio
    async def test_deny_rule_blocks_without_prompt(self):
        """Commands matching a deny rule should be denied without showing a prompt."""
        provider = FakeProvider(
            [
                _bash_turn("t1", "rm -rf /"),
                _text_turn("blocked"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        ctx = _ctx(deny={"user_settings": ["bash(rm:*)"]})
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=ctx,
        )
        events = await _collect_events(loop, "delete all")
        assert not _has_permission_request(events), "Deny rule should not prompt"
        results = _tool_results(events)
        assert any(r.is_error for r in results), "Deny rule should produce error result"


class TestTooComplexSessionRuleScenario:
    """Session allow rules must not bypass too_complex permission requests."""

    @pytest.mark.asyncio
    async def test_echo_whoami_with_session_rule_still_requests_permission(self):
        """echo $(whoami) is too_complex, so echo:* must not auto-allow it."""
        from iac_code.services.permissions.storage import apply_session_rule

        ctx_holder = [_ctx()]
        sug = PermissionRuleValue(tool_name="bash", rule_content="echo:*")
        ctx_holder[0] = apply_session_rule(ctx_holder[0], "allow", sug)

        provider = FakeProvider([_bash_turn("t1", "echo $(whoami)"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context_getter=lambda: ctx_holder[0],
        )
        events = await _collect_events(loop, "echo whoami")
        assert _has_permission_request(events), "Session allow rule must not skip prompt for too_complex"

    @pytest.mark.asyncio
    async def test_eval_with_session_rule_still_requests_permission(self):
        """eval 'ls' is too_complex, so eval:* must not auto-allow it."""
        from iac_code.services.permissions.storage import apply_session_rule

        ctx_holder = [_ctx()]
        sug = PermissionRuleValue(tool_name="bash", rule_content="eval:*")
        ctx_holder[0] = apply_session_rule(ctx_holder[0], "allow", sug)

        provider = FakeProvider([_bash_turn("t1", "eval 'ls'"), _text_turn("done")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context_getter=lambda: ctx_holder[0],
        )
        events = await _collect_events(loop, "eval ls")
        assert _has_permission_request(events), "Session allow rule must not skip prompt for eval"

    @pytest.mark.asyncio
    async def test_pipeline_result_has_suggestions(self):
        """Pipeline result for passthrough commands must carry suggestions for the renderer."""
        provider = FakeProvider([_bash_turn("t1", "curl https://example.com"), _text_turn("denied")])
        registry = ToolRegistry()
        registry.register_default_tools()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=_ctx(),
        )
        events = await _collect_events(loop, "curl")
        perms = _permission_events(events)
        assert len(perms) == 1
        pr = perms[0].permission_result
        assert pr is not None
        assert pr.suggestions, "Pipeline must preserve suggestions for the renderer"
        assert "curl" in pr.suggestions[0].rule_content


class TestAllowOnceVsAlwaysAllow:
    """Verify that allow-once does NOT create persistent rules."""

    @pytest.mark.asyncio
    async def test_allow_once_doesnt_persist(self):
        """Allowing once should not affect subsequent calls."""
        provider = FakeProvider(
            [
                _bash_turn("t1", "curl https://example.com"),
                _text_turn("got it"),
                _bash_turn("t2", "curl https://example.com"),
                _text_turn("got it again"),
            ]
        )
        registry = ToolRegistry()
        registry.register_default_tools()
        ctx = _ctx()
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=registry,
            max_turns=2,
            permission_context=ctx,
        )

        events1 = await _collect_events(loop, "curl", permission_handler=lambda e: True)
        perms1 = _permission_events(events1)
        assert len(perms1) == 1

        events2 = await _collect_events(loop, "curl again", permission_handler=lambda e: True)
        perms2 = _permission_events(events2)
        assert len(perms2) == 1, "Allow-once must not persist across turns"
