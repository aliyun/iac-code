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

    memory_recall = snapshot.get("memory_recall")
    if _should_show_memory_recall() and isinstance(memory_recall, dict) and memory_recall:
        _append_memory_recall(text, memory_recall)
        text.append("\n")

    _append_line(text, _("Turns"), "{} / {}".format(snapshot.get("turn_count", 0), snapshot.get("max_turns", 0)))
    _append_line(text, _("Context"), _format_context(snapshot.get("context_usage") or {}))

    return Panel(Group(text), title=_("Session Status"), border_style="cyan", expand=False)


def _should_show_memory_recall() -> bool:
    from iac_code.utils.log import is_debug_enabled

    return is_debug_enabled()


def _append_memory_recall(text: Text, memory_recall: dict[str, Any]) -> None:
    text.append(_("Memory Recall"), style="bold")
    text.append("\n")
    _append_line(
        text,
        _("Side queries"),
        _("{total} total, {success} success, {failed} failed, {cancelled} cancelled").format(
            total=int(memory_recall.get("total_side_queries") or 0),
            success=int(memory_recall.get("successful_side_queries") or 0),
            failed=int(memory_recall.get("failed_side_queries") or 0),
            cancelled=int(memory_recall.get("cancelled_side_queries") or 0),
        ),
        indent=2,
    )
    last_attempt_files = [str(item) for item in memory_recall.get("last_selected_files") or []]
    _append_line(
        text,
        _("Last attempt"),
        _("{status} in {duration} ms, {count} files selected").format(
            status=str(memory_recall.get("last_status") or "skipped"),
            duration=int(memory_recall.get("last_duration_ms") or 0),
            count=len(last_attempt_files),
        ),
        indent=2,
    )
    last_side_query_files = [str(item) for item in memory_recall.get("last_side_query_selected_files") or []]
    if int(memory_recall.get("total_side_queries") or 0) > 0:
        _append_line(
            text,
            _("Last side call"),
            _("{status} in {duration} ms, {count} files selected").format(
                status=str(memory_recall.get("last_side_query_status") or "skipped"),
                duration=int(memory_recall.get("last_side_query_duration_ms") or 0),
                count=len(last_side_query_files),
            ),
            indent=2,
        )
    if last_side_query_files:
        _append_line(text, _("Last files"), ", ".join(last_side_query_files[:3]), indent=2)
    _append_memory_usage(text, _("Side call usage"), memory_recall.get("total_usage"), indent=2, include_events=True)
    _append_memory_usage(text, _("Last usage"), memory_recall.get("last_usage"), indent=2)


def _append_line(text: Text, label: str, value: str, *, indent: int = 0) -> None:
    label_text = label + ":"
    padding = max(0, LABEL_COLUMN_WIDTH - cell_len(label_text))
    text.append(" " * indent)
    text.append(label_text, style="bold")
    text.append(" " * (padding + 1))
    text.append(str(value))
    text.append("\n")


def _append_block(text: Text, value: str, *, indent: int = 0) -> None:
    prefix = " " * indent
    for line in value.splitlines():
        text.append(prefix)
        text.append(line, style="dim")
        text.append("\n")


def _append_memory_usage(
    text: Text,
    label: str,
    usage: Any,
    *,
    indent: int = 0,
    include_events: bool = False,
) -> None:
    if not isinstance(usage, dict) or not usage.get("has_recorded_usage"):
        _append_line(text, label, _("No token usage reported"), indent=indent)
        return

    if include_events:
        value = _("{events} records, input {input}, output {output}, cache read {cache_read}, total {total}").format(
            events=_format_int(int(usage.get("recorded_events") or 0)),
            input=_format_int(int(usage.get("input_tokens") or 0)),
            output=_format_int(int(usage.get("output_tokens") or 0)),
            cache_read=_format_int(int(usage.get("cache_read_input_tokens") or 0)),
            total=_format_int(int(usage.get("total_tokens") or 0)),
        )
    else:
        value = _("input {input}, output {output}, cache read {cache_read}, total {total}").format(
            input=_format_int(int(usage.get("input_tokens") or 0)),
            output=_format_int(int(usage.get("output_tokens") or 0)),
            cache_read=_format_int(int(usage.get("cache_read_input_tokens") or 0)),
            total=_format_int(int(usage.get("total_tokens") or 0)),
        )
    _append_line(text, label, value, indent=indent)


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


def _format_decimal_suffix(value: int, divisor: int, suffix: str) -> str:
    amount = value / divisor
    formatted = f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


def _format_compact(value: int) -> str:
    value = int(value)
    if value >= 1_000_000:
        return _format_decimal_suffix(value, 1_000_000, "M")
    if value >= 1_000:
        return _format_decimal_suffix(value, 1_000, "k")
    return str(value)
