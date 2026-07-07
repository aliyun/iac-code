from rich.text import Text

from iac_code.ui.diagram_rendering import style_attachment_lines


def test_style_attachment_lines_dims_node_attachment_lines():
    diagram = Text(
        "┌────────────────────┐\n"
        "│        ECS         │\n"
        "│       + EIP        │\n"
        "│  + Security group  │\n"
        "└────────────────────┘"
    )

    styled = style_attachment_lines(diagram)

    assert styled is not diagram
    assert _styled_slice(styled, "+ EIP") == "dim cyan"
    assert _styled_slice(styled, "+ Security group") == "dim cyan"
    assert _styled_slice(styled, "ECS") is None


def test_style_attachment_lines_returns_non_text_renderables_unchanged():
    renderable = object()

    assert style_attachment_lines(renderable) is renderable


def test_style_attachment_lines_ignores_plus_text_outside_node_boxes():
    diagram = Text("+ edge label\n│       + EIP        │")

    styled = style_attachment_lines(diagram)

    assert _styled_slice(styled, "+ EIP") == "dim cyan"
    assert _styled_slice(styled, "+ edge label") is None


def test_style_attachment_lines_does_not_bleed_across_neighbor_boxes():
    diagram = Text("│        + EIP        │      │        DmzCen CEN        │")

    styled = style_attachment_lines(diagram)

    assert _styled_slice(styled, "+ EIP") == "dim cyan"
    assert _styled_slice(styled, "DmzCen CEN") is None


def test_style_attachment_lines_styles_wrapped_attachment_continuation():
    diagram = Text("│    DMZ NLB     │\n│ + Shared bandwidth │\n│     package    │\n│ + Listener     │")

    styled = style_attachment_lines(diagram)

    assert _styled_slice(styled, "+ Shared bandwidth") == "dim cyan"
    assert _styled_slice(styled, "package") == "dim cyan"
    assert _styled_slice(styled, "+ Listener") == "dim cyan"
    assert _styled_slice(styled, "DMZ NLB") is None


def _styled_slice(text: Text, value: str) -> str | None:
    start = text.plain.index(value)
    end = start + len(value)
    for span in text.spans:
        if span.start <= start and span.end >= end:
            return str(span.style)
    return None
