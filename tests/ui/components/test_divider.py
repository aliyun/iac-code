"""Tests for Divider component."""

from rich.rule import Rule

from iac_code.ui.components.divider import Divider


class TestDivider:
    def test_render_returns_rule(self):
        d = Divider()
        result = d.render()
        assert isinstance(result, Rule)

    def test_render_with_text(self):
        d = Divider(text="Section")
        result = d.render()
        assert isinstance(result, Rule)

    def test_default_text_empty(self):
        d = Divider()
        assert d.text == ""

    def test_default_style_dim(self):
        d = Divider()
        assert d.style == "dim"

    def test_custom_style(self):
        d = Divider(style="bold red")
        assert d.style == "bold red"

    def test_custom_text(self):
        d = Divider(text="Hello")
        assert d.text == "Hello"
