"""U-I5: Live must be restarted after DiagramEvent so progress keeps animating.

Before the fix: the DiagramEvent handler in run_streaming_output stopped Live
to print the mermaid diagram, then never restarted it. Subsequent events that
re-call ``_ensure_live()`` (e.g. TextDeltaEvent) would lazily recreate Live,
but events that do NOT call ``_ensure_live()`` (e.g. MessageEndEvent) would
arrive with ``live is None`` and any progress would be silent.

The fix: after ``console.print(diagram_renderable)``, call ``_ensure_live()``
(and restart the refresh loop) so Live is alive again for the rest of the step.

To make the bug observable we send a stream that does NOT include any further
events that re-trigger ``_ensure_live()``: just MessageStart → DiagramEvent →
MessageEnd. Before the fix exactly one Live instance is constructed; after the
fix two are constructed (one for the message start, one for the diagram
restart).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import (
    DiagramEvent,
    MessageEndEvent,
    MessageStartEvent,
    Usage,
)
from iac_code.ui.renderer import Renderer


@pytest.mark.asyncio
async def test_live_recreated_after_diagram_event(monkeypatch):
    """DiagramEvent handler must restart Live after printing the diagram so the
    progress bar keeps animating for subsequent events in the same step.
    """
    live_constructed: list[MagicMock] = []

    def fake_live_factory(*args, **kwargs):
        m = MagicMock(name=f"Live#{len(live_constructed)}")
        # ``_started`` is the Rich-internal flag inspected by
        # ``Renderer._quiet_stop_live``; keep it False so that helper returns
        # immediately and we don't have to mock its full teardown path.
        m._started = False
        m.start = MagicMock()
        m.stop = MagicMock()
        m.update = MagicMock()
        live_constructed.append(m)
        return m

    # Patch where renderer.py imports Live (rich.live.Live re-exported into
    # iac_code.ui.renderer's module namespace).
    monkeypatch.setattr("iac_code.ui.renderer.Live", fake_live_factory)

    console = MagicMock()
    registry = ToolRegistry()
    renderer = Renderer(console, registry, status_callback=lambda: "test")

    async def events():
        yield MessageStartEvent(message_id="m1")
        yield DiagramEvent(
            candidate_name="方案1",
            template_content="ROSTemplate...",
            mermaid_source="graph TD\n  A-->B",
        )
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    await renderer.run_streaming_output(events(), permission_handler=None)

    assert len(live_constructed) >= 2, (
        f"only {len(live_constructed)} Live instance(s) constructed; "
        "expected >= 2 (one for MessageStart, one for DiagramEvent restart). "
        "The DiagramEvent handler stopped Live without restarting it."
    )
