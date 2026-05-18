"""Tests for Renderer.prompt_permission arrow-key selector and cache short-circuit."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from iac_code.state.app_state import AppState, AppStateStore
from iac_code.types.stream_events import PermissionRequestEvent
from iac_code.ui.renderer import Renderer


def _make_renderer(app_state_store=None, tool=None) -> Renderer:
    console = Console(record=True)
    tool_registry = MagicMock()
    tool_registry.get.return_value = tool
    return Renderer(console, tool_registry, app_state_store=app_state_store)


def _make_event(tool_name: str = "bash") -> PermissionRequestEvent:
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    return PermissionRequestEvent(
        tool_name=tool_name,
        tool_input={"cmd": "ls"},
        tool_use_id="t1",
        response_future=fut,
    )


def _patch_select(return_value):
    """Patch Select.run to return a predetermined value."""
    return patch("iac_code.ui.components.select.Select.run", return_value=return_value)


class TestArrowKeySelector:
    @pytest.mark.asyncio
    async def test_allow_once(self):
        store = AppStateStore()
        renderer = _make_renderer(store)
        event = _make_event()
        with _patch_select("allow_once"):
            result = await renderer.prompt_permission(event)
        assert result is True
        assert len(store.get_state().always_allow_rules) == 0

    @pytest.mark.asyncio
    async def test_always_allow_records(self):
        store = AppStateStore()
        renderer = _make_renderer(store)
        event = _make_event("bash")
        with _patch_select("always_allow"):
            result = await renderer.prompt_permission(event)
        assert result is True
        assert store.get_state().always_allow_rules["bash"] == "always_allow"

    @pytest.mark.asyncio
    async def test_reject_once(self):
        store = AppStateStore()
        renderer = _make_renderer(store)
        event = _make_event()
        with _patch_select("reject_once"):
            result = await renderer.prompt_permission(event)
        assert result is False
        assert len(store.get_state().always_allow_rules) == 0

    @pytest.mark.asyncio
    async def test_always_deny_records(self):
        store = AppStateStore()
        renderer = _make_renderer(store)
        event = _make_event("bash")
        with _patch_select("always_deny"):
            result = await renderer.prompt_permission(event)
        assert result is False
        assert store.get_state().always_allow_rules["bash"] == "always_deny"

    @pytest.mark.asyncio
    async def test_cancel_returns_false(self):
        store = AppStateStore()
        renderer = _make_renderer(store)
        event = _make_event()
        with _patch_select(None):
            result = await renderer.prompt_permission(event)
        assert result is False
        assert len(store.get_state().always_allow_rules) == 0


class TestCacheShortCircuit:
    @pytest.mark.asyncio
    async def test_always_allow_hit_skips_selector(self):
        store = AppStateStore(AppState(always_allow_rules=OrderedDict([("bash", "always_allow")])))
        renderer = _make_renderer(store)
        event = _make_event("bash")
        with _patch_select(None) as mock_run:
            result = await renderer.prompt_permission(event)
        assert result is True
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_always_deny_hit_skips_selector(self):
        store = AppStateStore(AppState(always_allow_rules=OrderedDict([("bash", "always_deny")])))
        renderer = _make_renderer(store)
        event = _make_event("bash")
        with _patch_select(None) as mock_run:
            result = await renderer.prompt_permission(event)
        assert result is False
        mock_run.assert_not_called()


class TestNoneStoreFallback:
    @pytest.mark.asyncio
    async def test_prompt_with_store_none_still_works(self):
        renderer = _make_renderer(app_state_store=None)
        event = _make_event()
        with _patch_select("allow_once"):
            result = await renderer.prompt_permission(event)
        assert result is True


def _make_event_with_suggestion(tool_name: str = "bash") -> PermissionRequestEvent:
    from iac_code.types.permissions import PermissionResult, PermissionRuleValue

    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    return PermissionRequestEvent(
        tool_name=tool_name,
        tool_input={"command": "mkdir foo"},
        tool_use_id="t1",
        response_future=fut,
        permission_result=PermissionResult(
            behavior="ask",
            suggestions=[PermissionRuleValue(tool_name="bash", rule_content="mkdir:*")],
        ),
    )


class TestRuleLevelDeny:
    @pytest.mark.asyncio
    async def test_always_deny_rule_returns_false(self):
        """Selecting 'always_deny_rule' should return False."""
        import dataclasses

        from iac_code.types.permissions import ToolPermissionContext

        store = AppStateStore()
        store.set_state(lambda s: dataclasses.replace(s, permission_context=ToolPermissionContext()))

        renderer = _make_renderer(store)
        event = _make_event_with_suggestion()
        with _patch_select("always_deny_rule"):
            result = await renderer.prompt_permission(event)
        assert result is False

    @pytest.mark.asyncio
    async def test_always_deny_rule_adds_deny_session_rule(self):
        """'always_deny_rule' should apply a deny session rule to the permission context."""
        import dataclasses

        from iac_code.types.permissions import ToolPermissionContext

        store = AppStateStore()
        store.set_state(lambda s: dataclasses.replace(s, permission_context=ToolPermissionContext()))

        renderer = _make_renderer(store)
        event = _make_event_with_suggestion()
        with _patch_select("always_deny_rule"):
            await renderer.prompt_permission(event)

        ctx = store.get_state().permission_context
        deny_rules = ctx.deny_rules.get("session", [])
        assert any("mkdir:*" in r for r in deny_rules)

    @pytest.mark.asyncio
    async def test_always_allow_rule_adds_allow_session_rule(self):
        """'always_allow_rule' should apply an allow session rule."""
        import dataclasses

        from iac_code.types.permissions import ToolPermissionContext

        store = AppStateStore()
        store.set_state(lambda s: dataclasses.replace(s, permission_context=ToolPermissionContext()))

        renderer = _make_renderer(store)
        event = _make_event_with_suggestion()
        with _patch_select("always_allow_rule"):
            result = await renderer.prompt_permission(event)

        assert result is True
        ctx = store.get_state().permission_context
        allow_rules = ctx.allow_rules.get("session", [])
        assert any("mkdir:*" in r for r in allow_rules)


def _make_event_with_multiple_suggestions(tool_name: str = "bash") -> PermissionRequestEvent:
    from iac_code.types.permissions import PermissionResult, PermissionRuleValue

    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    return PermissionRequestEvent(
        tool_name=tool_name,
        tool_input={"command": "mkdir -p a && rm -rf b"},
        tool_use_id="t1",
        response_future=fut,
        permission_result=PermissionResult(
            behavior="ask",
            suggestions=[
                PermissionRuleValue(tool_name="bash", rule_content="mkdir:*"),
                PermissionRuleValue(tool_name="bash", rule_content="rm:*"),
            ],
        ),
    )


class TestMultipleSuggestions:
    @pytest.mark.asyncio
    async def test_allow_rule_applies_all_suggestions(self):
        """'always_allow_rule' with multiple suggestions should apply all rules."""
        import dataclasses

        from iac_code.types.permissions import ToolPermissionContext

        store = AppStateStore()
        store.set_state(lambda s: dataclasses.replace(s, permission_context=ToolPermissionContext()))

        renderer = _make_renderer(store)
        event = _make_event_with_multiple_suggestions()
        with _patch_select("always_allow_rule"):
            result = await renderer.prompt_permission(event)

        assert result is True
        ctx = store.get_state().permission_context
        allow_rules = ctx.allow_rules.get("session", [])
        assert any("mkdir:*" in r for r in allow_rules)
        assert any("rm:*" in r for r in allow_rules)

    @pytest.mark.asyncio
    async def test_deny_rule_applies_all_suggestions(self):
        """'always_deny_rule' with multiple suggestions should apply all rules."""
        import dataclasses

        from iac_code.types.permissions import ToolPermissionContext

        store = AppStateStore()
        store.set_state(lambda s: dataclasses.replace(s, permission_context=ToolPermissionContext()))

        renderer = _make_renderer(store)
        event = _make_event_with_multiple_suggestions()
        with _patch_select("always_deny_rule"):
            result = await renderer.prompt_permission(event)

        assert result is False
        ctx = store.get_state().permission_context
        deny_rules = ctx.deny_rules.get("session", [])
        assert any("mkdir:*" in r for r in deny_rules)
        assert any("rm:*" in r for r in deny_rules)

    @pytest.mark.asyncio
    async def test_label_shows_all_rules(self):
        """Option label should display all suggestion rules comma-separated."""
        from iac_code.tools.bash import BashTool

        store = AppStateStore()
        tool = BashTool()
        renderer = _make_renderer(store, tool=tool)
        event = _make_event_with_multiple_suggestions()

        captured_labels = {}

        def capture_select_init(self, *, options, **kwargs):
            captured_labels["all"] = [o.label for o in options]
            self._options = options
            self._default_value = kwargs.get("default_value")

        with patch("iac_code.ui.components.select.Select.__init__", capture_select_init):
            with _patch_select("reject_once"):
                await renderer.prompt_permission(event)

        labels_text = " ".join(captured_labels["all"])
        assert "mkdir:*" in labels_text
        assert "rm:*" in labels_text


class TestSupportsBlanketAllow:
    @pytest.mark.asyncio
    async def test_bash_no_suggestions_hides_always_allow(self):
        """Bash tool (supports_blanket_allow=False) without suggestions should NOT offer always_allow."""
        from iac_code.tools.bash import BashTool

        store = AppStateStore()
        tool = BashTool()
        renderer = _make_renderer(store, tool=tool)
        event = _make_event("bash")

        captured_options = {}

        def capture_select_init(self, *, options, **kwargs):
            captured_options["values"] = [o.value for o in options]
            self._options = options
            self._default_value = kwargs.get("default_value")

        with patch("iac_code.ui.components.select.Select.__init__", capture_select_init):
            with _patch_select("reject_once"):
                await renderer.prompt_permission(event)

        assert "always_allow" not in captured_options["values"]

    @pytest.mark.asyncio
    async def test_bash_no_suggestions_still_has_always_deny(self):
        """Bash tool without suggestions should still show always_deny option."""
        from iac_code.tools.bash import BashTool

        store = AppStateStore()
        tool = BashTool()
        renderer = _make_renderer(store, tool=tool)
        event = _make_event("bash")

        captured_options = {}

        def capture_select_init(self, *, options, **kwargs):
            captured_options["values"] = [o.value for o in options]
            self._options = options
            self._default_value = kwargs.get("default_value")

        with patch("iac_code.ui.components.select.Select.__init__", capture_select_init):
            with _patch_select("always_deny"):
                result = await renderer.prompt_permission(event)

        assert "always_deny" in captured_options["values"]
        assert result is False
        assert store.get_state().always_allow_rules["bash"] == "always_deny"

    @pytest.mark.asyncio
    async def test_normal_tool_no_suggestions_shows_always_allow(self):
        """Normal tool (supports_blanket_allow=True) without suggestions should show always_allow."""
        store = AppStateStore()
        tool = MagicMock()
        tool.supports_blanket_allow = True
        tool.user_facing_name.return_value = "WebFetch"
        tool.render_tool_use_message.return_value = None
        renderer = _make_renderer(store, tool=tool)
        event = _make_event("web_fetch")

        captured_options = {}

        def capture_select_init(self, *, options, **kwargs):
            captured_options["values"] = [o.value for o in options]
            self._options = options
            self._default_value = kwargs.get("default_value")

        with patch("iac_code.ui.components.select.Select.__init__", capture_select_init):
            with _patch_select("always_allow"):
                result = await renderer.prompt_permission(event)

        assert "always_allow" in captured_options["values"]
        assert result is True
        assert store.get_state().always_allow_rules["web_fetch"] == "always_allow"
