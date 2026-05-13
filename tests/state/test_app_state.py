"""Tests for the state/app_state module."""

import os
from collections import OrderedDict
from unittest.mock import MagicMock

from iac_code.state.app_state import AppState, AppStateStore
from iac_code.types.permissions import PermissionMode


class TestAppState:
    """Tests for AppState dataclass."""

    def test_app_state_defaults(self):
        """Test AppState default values."""
        state = AppState()
        assert state.model == ""
        assert state.cwd == os.getcwd()
        assert state.permission_mode == PermissionMode.DEFAULT
        assert state.messages == []
        assert state.is_busy is False
        assert state.always_allow_rules == {}
        assert isinstance(state.always_allow_rules, OrderedDict)
        assert state.spinner_text == ""

    def test_app_state_custom_values(self):
        """Test AppState with custom values."""
        state = AppState(
            model="gpt-4",
            cwd="/tmp",
            permission_mode=PermissionMode.AUTO,
            is_busy=True,
            spinner_text="Working...",
        )
        assert state.model == "gpt-4"
        assert state.cwd == "/tmp"
        assert state.permission_mode == PermissionMode.AUTO
        assert state.is_busy is True
        assert state.spinner_text == "Working..."


class TestAppStateStore:
    """Tests for AppStateStore."""

    def test_get_state_returns_initial_state(self):
        """Test get_state returns initial state."""
        store = AppStateStore()
        state = store.get_state()
        assert isinstance(state, AppState)
        assert state.model == ""

    def test_get_state_with_custom_initial(self):
        """Test get_state with custom initial state."""
        initial = AppState(model="gpt-4")
        store = AppStateStore(initial_state=initial)
        state = store.get_state()
        assert state.model == "gpt-4"

    def test_set_state_with_kwargs(self):
        """Test set_state with kwargs updates state."""
        store = AppStateStore()
        store.set_state(is_busy=True, model="claude")
        state = store.get_state()
        assert state.is_busy is True
        assert state.model == "claude"

    def test_set_state_with_updater_function(self):
        """Test set_state with updater function."""
        import dataclasses

        store = AppStateStore()

        def updater(s: AppState) -> AppState:
            return dataclasses.replace(s, model="updated-model", is_busy=True)

        store.set_state(updater)
        state = store.get_state()
        assert state.model == "updated-model"
        assert state.is_busy is True

    def test_subscribe_callback_triggered(self):
        """Test subscribe callback is triggered on state change."""
        store = AppStateStore()
        callback = MagicMock()
        store.subscribe(callback)

        store.set_state(model="new-model")

        callback.assert_called_once()
        called_state = callback.call_args[0][0]
        assert called_state.model == "new-model"

    def test_subscribe_returns_unsubscribe(self):
        """Test subscribe returns unsubscribe function."""
        store = AppStateStore()
        callback = MagicMock()
        unsubscribe = store.subscribe(callback)

        # First change should trigger
        store.set_state(model="m1")
        assert callback.call_count == 1

        # Unsubscribe
        unsubscribe()

        # Second change should NOT trigger
        store.set_state(model="m2")
        assert callback.call_count == 1  # Still 1

    def test_multiple_subscribers(self):
        """Test multiple subscribers are all notified."""
        store = AppStateStore()
        callback1 = MagicMock()
        callback2 = MagicMock()
        store.subscribe(callback1)
        store.subscribe(callback2)

        store.set_state(is_busy=True)

        callback1.assert_called_once()
        callback2.assert_called_once()

    def test_unsubscribe_only_removes_specific_listener(self):
        """Test unsubscribe only removes the specific listener."""
        store = AppStateStore()
        callback1 = MagicMock()
        callback2 = MagicMock()
        unsubscribe1 = store.subscribe(callback1)
        store.subscribe(callback2)

        unsubscribe1()
        store.set_state(model="test")

        callback1.assert_not_called()
        callback2.assert_called_once()
