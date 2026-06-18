#!/usr/bin/env python3
"""Quick test harness for DiagramEvent rendering.

Usage:
    uv run python scripts/rendering/test_diagram_render.py [--html] [template.yml ...]

Options:
    --html    Generate an HTML file and open it in the browser (recommended
              for complex diagrams).  Without this flag, renders to the
              terminal via termaid / Rich fallback.

If no template files are given, uses templates/*.yml in the current directory.
"""

from __future__ import annotations

import sys
import tempfile
import webbrowser
from pathlib import Path

from rich.console import Console, Group
from rich.text import Text

from iac_code.pipeline.engine.show_diagram_tool import ros_template_to_mermaid
from iac_code.types.stream_events import DiagramEvent

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Architecture Diagrams</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafafa; }}
  .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
           padding: 1.5rem; margin-bottom: 2rem; }}
  .card h2 {{ margin-top: 0; color: #0077b6; }}
  .mermaid {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1>Architecture Diagrams</h1>
{cards}
<script>mermaid.initialize({{ startOnLoad: true, theme: 'default' }});</script>
</body>
</html>
"""

CARD_TEMPLATE = """\
<div class="card">
  <h2>{name}</h2>
  <div class="mermaid">
{source}
  </div>
</div>
"""


def render_terminal(console: Console, event: DiagramEvent) -> None:
    title = Text(f"▀ {event.candidate_name}", style="bold cyan")
    try:
        from termaid import render_rich  # ty: ignore[unresolved-import]

        diagram = render_rich(event.mermaid_source)
        console.print(Group(title, Text(""), diagram, Text("")))
    except (ImportError, Exception):
        from rich.markdown import Markdown

        code_block = Markdown(f"```mermaid\n{event.mermaid_source}\n```")
        console.print(Group(title, Text(""), code_block, Text("")))


def render_html(events: list[DiagramEvent]) -> None:
    cards = "\n".join(CARD_TEMPLATE.format(name=e.candidate_name, source=e.mermaid_source) for e in events)
    html = HTML_TEMPLATE.format(cards=cards)

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
        path = f.name

    webbrowser.open(f"file://{path}")
    print(f"Opened {path} in browser.")


def main() -> None:
    console = Console()
    use_html = "--html" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--html"]

    paths = [Path(p) for p in args] if args else sorted(Path(".").glob("templates/*.yml"))

    if not paths:
        console.print("[red]No template files found. Pass paths as arguments or run from a directory with templates/.[/]")
        sys.exit(1)

    events: list[DiagramEvent] = []

    for path in paths:
        if not path.exists():
            console.print(f"[red]File not found: {path}[/]")
            continue

        template_content = path.read_text(encoding="utf-8")
        mermaid_source = ros_template_to_mermaid(template_content)

        events.append(
            DiagramEvent(
                candidate_name=path.stem,
                template_content=template_content,
                mermaid_source=mermaid_source,
            )
        )

    if use_html:
        render_html(events)
    else:
        for event in events:
            console.print(f"[dim]--- {event.candidate_name} ---[/]")
            render_terminal(console, event)
            console.print("[dim]Mermaid source:[/]")
            console.print(event.mermaid_source)
            console.print()


if __name__ == "__main__":
    main()
