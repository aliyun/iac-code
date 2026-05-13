"""Divider component wrapping Rich Rule."""

from __future__ import annotations

from rich.rule import Rule


class Divider:
    """A horizontal divider line, optionally with centered text.

    Wraps :class:`rich.rule.Rule`.
    """

    def __init__(self, text: str = "", style: str = "dim") -> None:
        self.text = text
        self.style = style

    def render(self) -> Rule:
        """Return a Rich Rule renderable."""
        return Rule(title=self.text, style=self.style)
