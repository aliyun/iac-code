"""Tests for ProgressBar component."""

from rich.text import Text

from iac_code.ui.components.progress_bar import ProgressBar


class TestProgressBar:
    def test_render_returns_text(self):
        pb = ProgressBar()
        result = pb.render()
        assert isinstance(result, Text)

    def test_initial_zero(self):
        pb = ProgressBar(total=100, completed=0, width=10)
        result = pb.render()
        plain = result.plain
        assert "0%" in plain

    def test_full_progress(self):
        pb = ProgressBar(total=100, completed=100, width=10)
        result = pb.render()
        plain = result.plain
        assert "100%" in plain

    def test_partial_progress(self):
        pb = ProgressBar(total=100, completed=50, width=10)
        result = pb.render()
        plain = result.plain
        assert "50%" in plain

    def test_filled_chars(self):
        pb = ProgressBar(total=100, completed=100, width=10, filled_char="X", empty_char=".")
        result = pb.render()
        plain = result.plain
        assert "X" in plain
        assert "." not in plain.split("%")[0]  # no empty chars when full

    def test_empty_chars(self):
        pb = ProgressBar(total=100, completed=0, width=10, filled_char="X", empty_char=".")
        result = pb.render()
        plain = result.plain
        assert "." in plain
        assert "X" not in plain.split("%")[0]  # no filled chars when empty

    def test_update(self):
        pb = ProgressBar(total=100, completed=0, width=10)
        pb.update(65)
        result = pb.render()
        plain = result.plain
        assert "65%" in plain

    def test_default_width(self):
        pb = ProgressBar()
        assert pb.width == 40

    def test_default_chars(self):
        pb = ProgressBar()
        assert pb.filled_char == "█"
        assert pb.empty_char == "░"

    def test_mixed_filled_empty(self):
        pb = ProgressBar(total=100, completed=50, width=10, filled_char="█", empty_char="░")
        result = pb.render()
        plain = result.plain
        assert "█" in plain
        assert "░" in plain
