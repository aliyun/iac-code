"""Tests for KeybindingManager."""

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.keybindings.manager import KeyBinding, KeybindingManager


def make_key(key, ctrl=False, alt=False):
    return KeyEvent(key=key, char=key, ctrl=ctrl, alt=alt)


class TestKeybindingManager:
    def test_register_and_resolve(self):
        mgr = KeybindingManager()
        called = []

        def handler():
            called.append(True)
            return True

        binding = KeyBinding(key="ctrl+r", action="reload", context="global", handler=handler)
        mgr.register(binding)
        mgr.push_context("global")

        event = make_key("r", ctrl=True)
        result = mgr.resolve(event)

        assert result is True
        assert called == [True]

    def test_unresolved_returns_false(self):
        mgr = KeybindingManager()
        mgr.push_context("global")
        event = make_key("x")
        result = mgr.resolve(event)
        assert result is False

    def test_context_priority(self):
        mgr = KeybindingManager()
        order = []

        def global_handler():
            order.append("global")
            return True

        def dialog_handler():
            order.append("dialog")
            return True

        mgr.register(KeyBinding(key="escape", action="cancel_global", context="global", handler=global_handler))
        mgr.register(KeyBinding(key="escape", action="cancel_dialog", context="dialog", handler=dialog_handler))
        mgr.push_context("global")
        mgr.push_context("dialog")

        event = make_key("escape")
        result = mgr.resolve(event)

        assert result is True
        assert order == ["dialog"]  # higher priority context wins

    def test_event_bubbling(self):
        mgr = KeybindingManager()
        order = []

        def dialog_handler():
            order.append("dialog")
            return False  # does not consume, bubbles

        def global_handler():
            order.append("global")
            return True

        mgr.register(KeyBinding(key="escape", action="cancel_dialog", context="dialog", handler=dialog_handler))
        mgr.register(KeyBinding(key="escape", action="cancel_global", context="global", handler=global_handler))
        mgr.push_context("global")
        mgr.push_context("dialog")

        event = make_key("escape")
        result = mgr.resolve(event)

        assert result is True
        assert order == ["dialog", "global"]

    def test_pop_context(self):
        mgr = KeybindingManager()
        called = []

        def handler():
            called.append(True)
            return True

        mgr.register(KeyBinding(key="escape", action="cancel", context="dialog", handler=handler))
        mgr.push_context("global")
        mgr.push_context("dialog")
        mgr.pop_context("dialog")

        event = make_key("escape")
        result = mgr.resolve(event)

        assert result is False
        assert called == []

    def test_unregister_context(self):
        mgr = KeybindingManager()
        called = []

        def handler():
            called.append(True)
            return True

        mgr.register(KeyBinding(key="ctrl+r", action="reload", context="global", handler=handler))
        mgr.push_context("global")
        mgr.unregister_context("global")

        event = make_key("r", ctrl=True)
        result = mgr.resolve(event)

        assert result is False
        assert called == []

    def test_register_returns_unregister_fn(self):
        mgr = KeybindingManager()
        called = []

        def handler():
            called.append(True)
            return True

        binding = KeyBinding(key="ctrl+r", action="reload", context="global", handler=handler)
        unregister = mgr.register(binding)
        mgr.push_context("global")

        # Should resolve initially
        assert mgr.resolve(make_key("r", ctrl=True)) is True
        called.clear()

        # After unregister, should not resolve
        unregister()
        assert mgr.resolve(make_key("r", ctrl=True)) is False

    def test_get_display_text(self):
        mgr = KeybindingManager()

        def handler():
            return True

        mgr.register(KeyBinding(key="ctrl+r", action="reload", context="global", handler=handler))

        display = mgr.get_display_text("reload", "global")
        assert display == "Ctrl+R"

    def test_get_display_text_not_found(self):
        mgr = KeybindingManager()
        result = mgr.get_display_text("nonexistent", "global")
        assert result is None

    def test_get_hints_for_context(self):
        mgr = KeybindingManager()

        def h1():
            return True

        def h2():
            return True

        mgr.register(KeyBinding(key="ctrl+r", action="reload", context="global", handler=h1))
        mgr.register(KeyBinding(key="escape", action="cancel", context="global", handler=h2))

        hints = mgr.get_hints_for_context("global")
        assert len(hints) == 2
        display_texts = [h[0] for h in hints]
        actions = [h[1] for h in hints]
        assert "Ctrl+R" in display_texts
        assert "Escape" in display_texts
        assert "reload" in actions
        assert "cancel" in actions

    def test_active_contexts_property(self):
        mgr = KeybindingManager()
        assert mgr.active_contexts == []

        mgr.push_context("global")
        assert mgr.active_contexts == ["global"]

        mgr.push_context("dialog")
        assert mgr.active_contexts == ["global", "dialog"]

        mgr.pop_context("dialog")
        assert mgr.active_contexts == ["global"]

    def test_multiple_bindings_same_key_different_contexts(self):
        mgr = KeybindingManager()
        results = []

        def global_handler():
            results.append("global")
            return True

        def select_handler():
            results.append("select")
            return True

        mgr.register(KeyBinding(key="up", action="prev_global", context="global", handler=global_handler))
        mgr.register(KeyBinding(key="up", action="prev_select", context="select", handler=select_handler))

        mgr.push_context("global")
        mgr.resolve(make_key("up"))
        assert results == ["global"]

        results.clear()
        mgr.push_context("select")
        mgr.resolve(make_key("up"))
        assert results == ["select"]

    def test_format_key_display_simple(self):
        mgr = KeybindingManager()

        def handler():
            return True

        mgr.register(KeyBinding(key="up", action="move_up", context="global", handler=handler))
        display = mgr.get_display_text("move_up", "global")
        assert display == "Up"

    def test_format_key_display_ctrl_shift(self):
        mgr = KeybindingManager()

        def handler():
            return True

        mgr.register(KeyBinding(key="ctrl+alt+x", action="special", context="global", handler=handler))
        display = mgr.get_display_text("special", "global")
        assert display == "Ctrl+Alt+X"
