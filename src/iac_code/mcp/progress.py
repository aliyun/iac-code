from __future__ import annotations

from typing import Any

from iac_code.i18n import _
from iac_code.mcp.redaction import sanitize_mcp_public_text
from iac_code.types.stream_events import MCPProgressEvent

_MCP_PROGRESS_TEXT_MAX_CHARS = 4000


def _safe_progress_text(value: object) -> str:
    return sanitize_mcp_public_text(value, fallback_summary="")[:_MCP_PROGRESS_TEXT_MAX_CHARS]


def mcp_progress_public_name(event: MCPProgressEvent) -> str:
    if event.public_name:
        return _safe_progress_text(event.public_name)
    return _safe_progress_text("mcp__{}__{}".format(event.server_name, event.tool_name))


def mcp_progress_metadata(event: MCPProgressEvent) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "status": "progress",
        "toolUseId": event.tool_use_id or "",
        "publicName": mcp_progress_public_name(event),
        "originalServerName": _safe_progress_text(event.server_name),
        "originalToolName": _safe_progress_text(event.tool_name),
    }
    if event.progress is not None:
        metadata["progress"] = event.progress
    if event.total is not None:
        metadata["total"] = event.total
    if event.message:
        metadata["message"] = _safe_progress_text(event.message)
    return metadata


def format_mcp_progress_title(event: MCPProgressEvent) -> str:
    return _("MCP {server}:{tool}").format(
        server=_safe_progress_text(event.server_name),
        tool=_safe_progress_text(event.tool_name),
    )


def format_mcp_progress_text(event: MCPProgressEvent) -> str:
    parts = [format_mcp_progress_title(event)]
    if event.progress is not None and event.total is not None:
        parts.append("{:g}/{:g}".format(event.progress, event.total))
    elif event.progress is not None:
        parts.append("{:g}".format(event.progress))
    if event.message:
        parts.append(_safe_progress_text(event.message))
    return ": ".join(parts)
