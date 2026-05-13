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


def _make_renderer(app_state_store=None) -> Renderer:
    console = Console(record=True)
    tool_registry = MagicMock()
    tool_registry.get.return_value = None  # no tool; renderer uses event.tool_name as display
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
