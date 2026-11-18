"""Display projections for runtime progress, separate from model messages."""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _


def wiki_summary(snapshot: dict[str, Any], phase: str | None = None) -> str:
    phase = phase or snapshot.get("status", "pending")
    if phase in {"ready", "fallback_ready"}:
        status = _("Matched {count} files").format(count=len(snapshot.get("hits", [])))
    else:
        status = {
            "extracting": _("Extracting keywords"),
            "searching": _("Searching"),
            "pending": _("Interrupted"),
            "skipped": _("No search needed"),
            "no_matches": _("No matches"),
            "failed": _("Failed"),
            "canceled": _("Cancelled"),
        }.get(phase, phase)
    label = _("Background Wiki search") if snapshot.get("dispatch_mode") == "async" else _("Wiki search")
    return "{} · {}".format(label, status)


def wiki_projection(snapshot: dict[str, Any], phase: str | None = None) -> dict[str, Any]:
    return {
        "queryId": snapshot.get("query_id", ""),
        "dispatchMode": snapshot.get("dispatch_mode", "serial"),
        "phase": phase or snapshot.get("status", "pending"),
        "summary": wiki_summary(snapshot, phase),
        "keywords": list(snapshot.get("keywords", [])),
        "files": [{"path": hit["path"], "title": hit["title"]} for hit in snapshot.get("hits", [])],
    }
