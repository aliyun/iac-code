"""Integration tests for bash permission pipeline — reproduces reported bug scenarios.

Bug 1: compound command with mixed readonly/non-readonly subcommands bypasses prompt
Bug 2: sub-agent without permission_context bypasses permission pipeline
Bug 3: session rules not propagated from renderer to agent loop
"""

import pytest

from iac_code.tools.bash.permissions import _BEHAVIOR_ORDER, _merge_results, bash_tool_has_permission
from iac_code.types.permissions import (
    PermissionMode,
    PermissionResult,
    PermissionRuleValue,
    ToolPermissionContext,
)


def _ctx(mode=PermissionMode.DEFAULT, allow=None, deny=None, ask=None, cwd="/project", extra_dirs=None):
    return ToolPermissionContext(
        mode=mode,
        cwd=cwd,
        allow_rules=allow or {},
        deny_rules=deny or {},
        ask_rules=ask or {},
        additional_directories=extra_dirs or [],
    )


class TestBehaviorOrderBug:
    """Verify passthrough is stricter than allow during merge.

    Previously _BEHAVIOR_ORDER had passthrough=3 > allow=2, which meant compound
    commands with mixed readonly (allow) and non-readonly (passthrough) subcommands
    would be auto-allowed instead of asking.
    """

    def test_passthrough_stricter_than_allow(self):
        assert _BEHAVIOR_ORDER["passthrough"] < _BEHAVIOR_ORDER["allow"]

    def test_deny_strictest(self):
        assert _BEHAVIOR_ORDER["deny"] < _BEHAVIOR_ORDER["ask"]
        assert _BEHAVIOR_ORDER["deny"] < _BEHAVIOR_ORDER["passthrough"]
        assert _BEHAVIOR_ORDER["deny"] < _BEHAVIOR_ORDER["allow"]

    def test_merge_passthrough_and_allow_yields_passthrough(self):
        results = [
            PermissionResult(behavior="passthrough"),
            PermissionResult(behavior="allow"),
        ]
        merged = _merge_results(results)
        assert merged.behavior == "passthrough"

    def test_merge_all_allow_yields_allow(self):
        results = [
            PermissionResult(behavior="allow"),
            PermissionResult(behavior="allow"),
        ]
        merged = _merge_results(results)
        assert merged.behavior == "allow"

    def test_merge_deny_wins_over_all(self):
        results = [
            PermissionResult(behavior="allow"),
            PermissionResult(behavior="deny"),
            PermissionResult(behavior="passthrough"),
        ]
        merged = _merge_results(results)
        assert merged.behavior == "deny"

    def test_merge_ask_wins_over_passthrough_and_allow(self):
        results = [
            PermissionResult(behavior="allow"),
            PermissionResult(behavior="ask"),
            PermissionResult(behavior="passthrough"),
        ]
        merged = _merge_results(results)
        assert merged.behavior == "ask"


class TestCompoundCommandBug:
    """Reproduce Bug 3: compound commands with readonly subcommands should not auto-allow."""

    @pytest.mark.asyncio
    async def test_cd_pwd_mkdir_compound_asks(self):
        """cd && pwd && mkdir should ASK (not allow), because mkdir is non-readonly."""
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("cd /project && pwd && mkdir test_dir", ctx)
        assert r.behavior in ("ask", "passthrough"), f"Expected ask/passthrough, got {r.behavior}"

    @pytest.mark.asyncio
    async def test_cd_pwd_mkdir_with_redirect_asks(self):
        """The exact bug scenario: cd && pwd && mkdir 2>&1 || echo should NOT auto-allow."""
        ctx = _ctx(cwd="/project")
        cmd = 'cd /project && pwd && mkdir test_dir 2>&1 || echo "Failed"'
        r = await bash_tool_has_permission(cmd, ctx)
        assert r.behavior in ("ask", "passthrough"), f"Expected ask/passthrough, got {r.behavior}"

    @pytest.mark.asyncio
    async def test_ls_and_rm_compound_not_allowed(self):
        """ls (readonly) + rm (non-readonly) compound should ask, not allow."""
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("ls -la && rm file.txt", ctx)
        assert r.behavior in ("ask", "passthrough", "deny")
        assert r.behavior != "allow"

    @pytest.mark.asyncio
    async def test_pwd_and_curl_asks(self):
        """pwd (readonly) + curl (non-readonly) should ask."""
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("pwd && curl https://example.com", ctx)
        assert r.behavior in ("ask", "passthrough")

    @pytest.mark.asyncio
    async def test_all_readonly_compound_allows(self):
        """ls && pwd && cat file should allow (all readonly)."""
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("ls && pwd && cat file.txt", ctx)
        assert r.behavior == "allow"


class TestSingleCommandPermission:
    """Verify single non-readonly commands always ask in DEFAULT mode."""

    @pytest.mark.asyncio
    async def test_curl_asks(self):
        ctx = _ctx()
        r = await bash_tool_has_permission("curl https://example.com", ctx)
        assert r.behavior in ("ask", "passthrough")

    @pytest.mark.asyncio
    async def test_mkdir_asks(self):
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("mkdir test_dir", ctx)
        assert r.behavior in ("ask", "passthrough")

    @pytest.mark.asyncio
    async def test_curl_asks_consistently(self):
        """Same command should produce the same result on repeated calls (Bug 1 regression)."""
        ctx = _ctx()
        r1 = await bash_tool_has_permission("curl https://example.com", ctx)
        r2 = await bash_tool_has_permission("curl https://example.com", ctx)
        assert r1.behavior == r2.behavior

    @pytest.mark.asyncio
    async def test_mkdir_asks_consistently(self):
        """Same command should produce the same result on repeated calls (Bug 2 regression)."""
        ctx = _ctx(cwd="/project")
        r1 = await bash_tool_has_permission("mkdir test_dir", ctx)
        r2 = await bash_tool_has_permission("mkdir test_dir", ctx)
        assert r1.behavior == r2.behavior


class TestPipelineEndToEnd:
    """Test the full pipeline (tool → pipeline → result)."""

    @pytest.mark.asyncio
    async def test_pipeline_converts_passthrough_to_ask(self):
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx = _ctx()
        r = await check_tool_permission(tool, {"command": "curl https://example.com"}, ctx)
        assert r.behavior == "ask"

    @pytest.mark.asyncio
    async def test_pipeline_compound_asks(self):
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx = _ctx(cwd="/project")
        r = await check_tool_permission(tool, {"command": "cd /project && pwd && mkdir test_dir"}, ctx)
        assert r.behavior == "ask"

    @pytest.mark.asyncio
    async def test_pipeline_readonly_allows(self):
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx = _ctx()
        r = await check_tool_permission(tool, {"command": "ls -la"}, ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_pipeline_dont_ask_mode_denies(self):
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx = _ctx(mode=PermissionMode.DONT_ASK)
        r = await check_tool_permission(tool, {"command": "curl https://example.com"}, ctx)
        assert r.behavior == "deny"


class TestSessionRulePropagation:
    """Verify session rules apply correctly across permission checks."""

    @pytest.mark.asyncio
    async def test_session_allow_rule_takes_effect(self):
        from iac_code.services.permissions.storage import apply_session_rule

        ctx = _ctx()
        sug = PermissionRuleValue(tool_name="bash", rule_content="curl:*")
        new_ctx = apply_session_rule(ctx, "allow", sug)

        r = await bash_tool_has_permission("curl https://example.com", new_ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_original_context_unmodified_after_session_rule(self):
        from iac_code.services.permissions.storage import apply_session_rule

        ctx = _ctx()
        sug = PermissionRuleValue(tool_name="bash", rule_content="curl:*")
        new_ctx = apply_session_rule(ctx, "allow", sug)

        r_old = await bash_tool_has_permission("curl https://example.com", ctx)
        r_new = await bash_tool_has_permission("curl https://example.com", new_ctx)
        assert r_old.behavior in ("ask", "passthrough")
        assert r_new.behavior == "allow"

    @pytest.mark.asyncio
    async def test_session_rule_context_used_by_getter(self):
        """Simulate the getter pattern: AgentLoop reads latest context from store."""
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.services.permissions.storage import apply_session_rule
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx_holder = [_ctx()]

        def getter():
            return ctx_holder[0]

        r1 = await check_tool_permission(tool, {"command": "curl https://example.com"}, getter())
        assert r1.behavior == "ask"

        sug = PermissionRuleValue(tool_name="bash", rule_content="curl:*")
        ctx_holder[0] = apply_session_rule(ctx_holder[0], "allow", sug)

        r2 = await check_tool_permission(tool, {"command": "curl https://example.com"}, getter())
        assert r2.behavior == "allow"


class TestTooComplexWithSessionRules:
    """Bug regression: too_complex commands must respect allow/deny rules.

    Previously, `echo $(whoami)` was classified as too_complex and returned `ask`
    before any rule matching. Session allow rules for `echo:*` were never consulted,
    causing an infinite prompt loop when the user selected "always allow echo:*".
    """

    @pytest.mark.asyncio
    async def test_too_complex_without_rule_asks(self):
        """echo $(whoami) without rules should ask."""
        ctx = _ctx()
        r = await bash_tool_has_permission("echo $(whoami)", ctx)
        assert r.behavior == "ask"

    @pytest.mark.asyncio
    async def test_too_complex_with_allow_rule_allows(self):
        """echo $(whoami) with allow rule `echo:*` should allow."""
        ctx = _ctx(allow={"session": ["bash(echo:*)"]})
        r = await bash_tool_has_permission("echo $(whoami)", ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_too_complex_deny_rule_still_denies(self):
        """Deny rules take precedence even for too_complex commands."""
        ctx = _ctx(deny={"session": ["bash(echo:*)"]})
        r = await bash_tool_has_permission("echo $(whoami)", ctx)
        assert r.behavior == "deny"

    @pytest.mark.asyncio
    async def test_too_complex_deny_overrides_allow(self):
        """If both deny and allow match, deny wins."""
        ctx = _ctx(
            allow={"session": ["bash(echo:*)"]},
            deny={"user_settings": ["bash(echo:*)"]},
        )
        r = await bash_tool_has_permission("echo $(whoami)", ctx)
        assert r.behavior == "deny"

    @pytest.mark.asyncio
    async def test_eval_with_allow_rule_allows(self):
        """eval (dangerous builtin, too_complex) with matching allow rule should allow."""
        ctx = _ctx(allow={"session": ["bash(eval:*)"]})
        r = await bash_tool_has_permission("eval 'ls'", ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_session_rule_after_rejection_allows_next_call(self):
        """Simulate: reject once → add session rule → next call allowed."""
        from iac_code.services.permissions.storage import apply_session_rule

        ctx = _ctx()
        r1 = await bash_tool_has_permission("echo $(whoami)", ctx)
        assert r1.behavior == "ask"
        assert r1.suggestions

        sug = r1.suggestions[0]
        new_ctx = apply_session_rule(ctx, "allow", sug)

        r2 = await bash_tool_has_permission("echo $(whoami)", new_ctx)
        assert r2.behavior == "allow"


class TestPipelineSuggestionsPreserved:
    """Bug regression: pipeline must preserve suggestions from bash engine.

    Previously, the pipeline's passthrough→ask conversion created a new
    PermissionResult without carrying over suggestions. This caused the
    renderer to show "Yes, allow always for this tool" (tool-level) instead
    of "Yes, always allow 'curl:*' (this session)" (rule-level). Selecting
    the tool-level option permanently auto-allowed ALL bash commands.
    """

    @pytest.mark.asyncio
    async def test_pipeline_preserves_suggestions_for_passthrough(self):
        """Suggestions from bash engine must survive pipeline's passthrough→ask conversion."""
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx = _ctx()
        r = await check_tool_permission(tool, {"command": "curl https://example.com"}, ctx)
        assert r.behavior == "ask"
        assert r.suggestions, "suggestions must be preserved through the pipeline"
        assert r.suggestions[0].rule_content == "curl:*"

    @pytest.mark.asyncio
    async def test_pipeline_preserves_suggestions_for_docker(self):
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx = _ctx()
        r = await check_tool_permission(tool, {"command": "docker run hello-world"}, ctx)
        assert r.behavior == "ask"
        assert r.suggestions, "docker command should have suggestions"
        assert "docker" in r.suggestions[0].rule_content

    @pytest.mark.asyncio
    async def test_too_complex_suggestions_preserved(self):
        """too_complex commands should also have suggestions in the pipeline result."""
        from iac_code.services.permissions.pipeline import check_tool_permission
        from iac_code.tools.bash.bash_tool import BashTool

        tool = BashTool()
        ctx = _ctx()
        r = await check_tool_permission(tool, {"command": "echo $(whoami)"}, ctx)
        assert r.behavior == "ask"
        assert r.suggestions, "too_complex should have suggestions"
        assert "echo" in r.suggestions[0].rule_content


class TestSuggestionGeneration:
    """Verify suggestions are generated for ask/passthrough results."""

    @pytest.mark.asyncio
    async def test_curl_generates_suggestion(self):
        ctx = _ctx()
        r = await bash_tool_has_permission("curl https://example.com", ctx)
        assert r.suggestions
        assert r.suggestions[0].tool_name == "bash"
        assert "curl" in r.suggestions[0].rule_content

    @pytest.mark.asyncio
    async def test_compound_generates_suggestion(self):
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("cd /project && mkdir foo", ctx)
        assert r.suggestions


class TestCompoundComplexSuggestions:
    """Verify compound commands with complex sub-commands generate correct suggestions."""

    @pytest.mark.asyncio
    async def test_eval_and_mkdir_generates_mkdir_suggestion_only(self):
        """eval ls && mkdir -p xxx: suggestion should be mkdir:*, not eval:*."""
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("eval ls && mkdir -p xxx", ctx)
        assert r.behavior == "ask"
        assert r.suggestions
        rule_contents = [s.rule_content for s in r.suggestions]
        assert "mkdir:*" in rule_contents
        assert "eval:*" not in rule_contents

    @pytest.mark.asyncio
    async def test_compound_all_allowed_still_allows(self):
        """ls && cat foo: both readonly allowed."""
        ctx = _ctx()
        r = await bash_tool_has_permission("ls && cat foo", ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_compound_one_allowed_one_not(self):
        """ls && mkdir foo: ls allowed, mkdir not → ask."""
        ctx = _ctx(cwd="/project")
        r = await bash_tool_has_permission("ls && mkdir foo", ctx)
        assert r.behavior in ("ask", "passthrough")

    @pytest.mark.asyncio
    async def test_eval_with_allow_rule_and_mkdir_without(self):
        """eval:* allowed, mkdir not → ask for mkdir."""
        ctx = _ctx(allow={"session": ["bash(eval:*)"]}, cwd="/project")
        r = await bash_tool_has_permission("eval ls && mkdir -p xxx", ctx)
        assert r.behavior in ("ask", "passthrough")

    @pytest.mark.asyncio
    async def test_echo_with_subst_generates_echo_suggestion(self):
        """echo $(whoami): echo is not dangerous, should generate echo:* suggestion."""
        ctx = _ctx()
        r = await bash_tool_has_permission("echo $(whoami)", ctx)
        assert r.behavior == "ask"
        assert r.suggestions
        rule_contents = [s.rule_content for s in r.suggestions]
        assert "echo:*" in rule_contents
