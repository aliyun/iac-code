"""Tests for Tabs component."""

from iac_code.ui.components.tabs import Tab, Tabs
from iac_code.ui.core.key_event import KeyEvent


def key(k, ctrl=False):
    return KeyEvent(key=k, char=k, ctrl=ctrl)


def make_tabs():
    return [
        Tab(id="a", title="Alpha", content="Content A"),
        Tab(id="b", title="Beta", content="Content B"),
        Tab(id="c", title="Gamma", content="Content C"),
    ]


class TestTabs:
    def test_initial_tab_default(self):
        tabs = Tabs(make_tabs())
        assert tabs.selected_tab == "a"

    def test_initial_tab_explicit(self):
        tabs = Tabs(make_tabs(), default_tab="b")
        assert tabs.selected_tab == "b"

    def test_switch_right(self):
        tabs = Tabs(make_tabs())
        tabs.handle_key(key("right"))
        assert tabs.selected_tab == "b"

    def test_switch_left(self):
        tabs = Tabs(make_tabs(), default_tab="b")
        tabs.handle_key(key("left"))
        assert tabs.selected_tab == "a"

    def test_no_wrap_right(self):
        tabs = Tabs(make_tabs(), default_tab="c")
        tabs.handle_key(key("right"))
        assert tabs.selected_tab == "c"  # stays at last tab

    def test_no_wrap_left(self):
        tabs = Tabs(make_tabs(), default_tab="a")
        tabs.handle_key(key("left"))
        assert tabs.selected_tab == "a"  # stays at first tab

    def test_on_tab_change_callback(self):
        changes = []
        tabs = Tabs(make_tabs(), on_tab_change=lambda tab_id: changes.append(tab_id))
        tabs.handle_key(key("right"))
        assert changes == ["b"]

    def test_on_tab_change_not_called_when_no_wrap(self):
        changes = []
        tabs = Tabs(make_tabs(), default_tab="c", on_tab_change=lambda tab_id: changes.append(tab_id))
        tabs.handle_key(key("right"))
        assert changes == []

    def test_render_returns_renderable(self):
        tabs = Tabs(make_tabs())
        result = tabs.render()
        # Must be some renderable type
        assert result is not None

    def test_render_contains_tab_titles(self):
        tabs = Tabs(make_tabs())
        result = tabs.render()
        # Collect plain text from the result
        from io import StringIO

        from rich.console import Console

        console = Console(file=StringIO(), force_terminal=False, width=120)
        console.print(result)
        output = console.file.getvalue()
        assert "Alpha" in output
        assert "Beta" in output
        assert "Gamma" in output

    def test_render_active_tab_highlighted(self):
        tabs = Tabs(make_tabs())
        result = tabs.render()
        from io import StringIO

        from rich.console import Console

        console = Console(file=StringIO(), force_terminal=False, width=120)
        console.print(result)
        output = console.file.getvalue()
        assert "[Alpha]" in output  # active tab has brackets

    def test_render_inactive_tabs_no_brackets(self):
        tabs = Tabs(make_tabs())
        result = tabs.render()
        from io import StringIO

        from rich.console import Console

        console = Console(file=StringIO(), force_terminal=False, width=120)
        console.print(result)
        output = console.file.getvalue()
        assert "[Beta]" not in output  # inactive tabs have no brackets

    def test_render_has_separator(self):
        tabs = Tabs(make_tabs())
        result = tabs.render()
        from io import StringIO

        from rich.console import Console

        console = Console(file=StringIO(), force_terminal=False, width=120)
        console.print(result)
        output = console.file.getvalue()
        assert "|" in output  # separator between tabs

    def test_unhandled_key_returns_false(self):
        tabs = Tabs(make_tabs())
        result = tabs.handle_key(key("enter"))
        assert result is False

    def test_handled_key_returns_true(self):
        tabs = Tabs(make_tabs())
        result = tabs.handle_key(key("right"))
        assert result is True

    def test_content_callable(self):
        def content_fn():
            return "Dynamic Content"

        tab_list = [Tab(id="x", title="X", content=content_fn)]
        tabs = Tabs(tab_list)
        # Should not raise
        result = tabs.render()
        assert result is not None

    def test_multiple_right_switches(self):
        tabs = Tabs(make_tabs())
        tabs.handle_key(key("right"))
        tabs.handle_key(key("right"))
        assert tabs.selected_tab == "c"

    def test_right_then_left(self):
        tabs = Tabs(make_tabs())
        tabs.handle_key(key("right"))
        tabs.handle_key(key("left"))
        assert tabs.selected_tab == "a"
