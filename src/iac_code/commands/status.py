"""Status command - show current session state and recorded API usage."""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from iac_code.i18n import _

LABEL_COLUMN_WIDTH = 12


async def status_command(context=None, **kwargs) -> str | None:
    if context is None:
        return _("Status command requires a context.")
    repl = getattr(context, "repl", None)
    if repl is None:
        return _("Status command requires a REPL context.")
    if not hasattr(repl, "get_status_snapshot"):
        return _("Status is only available in interactive mode.")

    snapshot = repl.get_status_snapshot()
    context.console.print(_render_status_panel(snapshot))
    return None


def _render_status_panel(snapshot: dict[str, Any]) -> Panel:
    text = Text()
    _append_line(text, _("Session"), _session_display(snapshot))
    _append_line(text, _("Provider"), snapshot.get("provider") or _("not configured"))
    _append_line(text, _("Model"), snapshot.get("model") or _("not configured"))
    _append_line(text, _("Region"), snapshot.get("region") or _("not configured"))
    _append_line(text, _("CWD"), snapshot.get("cwd") or "")
    text.append("\n")

    usage = snapshot.get("api_usage")
    text.append(_("API Token Usage (recorded):"), style="bold")
    text.append("\n")
    if usage is not None and getattr(usage, "has_recorded_usage", False):
        _append_line(text, _("Input"), _format_int(getattr(usage, "input_tokens", 0)), indent=2)
        _append_line(text, _("Output"), _format_int(getattr(usage, "output_tokens", 0)), indent=2)
        _append_line(text, _("Cache read"), _format_int(getattr(usage, "cache_read_input_tokens", 0)), indent=2)
        _append_line(text, _("Total"), _format_int(getattr(usage, "total_tokens", 0)), indent=2)
    else:
        text.append("  ")
        text.append(_("No recorded API usage for this session yet."), style="dim")
        text.append("\n")
    text.append("\n")

    _append_line(text, _("Turns"), "{} / {}".format(snapshot.get("turn_count", 0), snapshot.get("max_turns", 0)))
    _append_line(text, _("Context"), _format_context(snapshot.get("context_usage") or {}))

    return Panel(Group(text), title=_("Session Status"), border_style="cyan", expand=False)


def _append_line(text: Text, label: str, value: str, *, indent: int = 0) -> None:
    label_text = label + ":"
    padding = max(0, LABEL_COLUMN_WIDTH - cell_len(label_text))
    text.append(" " * indent)
    text.append(label_text, style="bold")
    text.append(" " * (padding + 1))
    text.append(str(value))
    text.append("\n")


def _session_display(snapshot: dict[str, Any]) -> str:
    session_id = snapshot.get("session_id") or ""
    if snapshot.get("resumed"):
        return _("{session_id} (resumed)").format(session_id=session_id)
    return str(session_id)


def _format_context(context_usage: dict[str, Any]) -> str:
    percent = float(context_usage.get("usage_percent") or 0)
    total = int(context_usage.get("total_tokens") or 0)
    window = int(context_usage.get("context_window") or 0)
    return _("{percent} used ({total} / {window})").format(
        percent=f"{percent:.0f}%",
        total=_format_compact(total),
        window=_format_compact(window),
    )


def _format_int(value: int) -> str:
    return f"{int(value):,}"


def _format_compact(value: int) -> str:
    value = int(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value // 1000}k"
    return str(value)
