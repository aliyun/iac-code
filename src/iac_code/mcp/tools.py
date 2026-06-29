from __future__ import annotations

import inspect
import json
import re
from typing import Any, Callable

from iac_code.i18n import _
from iac_code.mcp.output import convert_mcp_tool_result
from iac_code.mcp.types import MCPResourceRecord, MCPToolRecord
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import MCPProgressEvent


class MCPTool(Tool):
    def __init__(self, *, manager: Any, record: MCPToolRecord, session_id: str) -> None:
        self._manager = manager
        self._record = record
        self._session_id = session_id

    @property
    def name(self) -> str:
        return self._record.public_name

    @property
    def description(self) -> str:
        description = self._record.description or _("MCP tool {tool!r} from server {server!r}.").format(
            tool=self._record.tool_name,
            server=self._record.server_name,
        )
        return description[:4000]

    @property
    def input_schema(self) -> dict[str, Any]:
        if isinstance(self._record.input_schema, dict) and self._record.input_schema:
            return dict(self._record.input_schema)
        return {"type": "object", "properties": {}}

    @property
    def timeout(self) -> float | None:
        return 600.0

    def is_read_only(self, input: dict | None = None) -> bool:
        return self._record.annotations.get("readOnlyHint") is True

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return self.is_read_only(tool_input)

    def is_destructive(self, input: dict | None = None) -> bool:
        return self._record.annotations.get("destructiveHint") is True

    def needs_event_queue(self) -> bool:
        return True

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("MCP {server}:{tool}").format(server=self._record.server_name, tool=self._record.tool_name)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        meta = {"iac_code/toolUseId": context.tool_use_id} if context.tool_use_id else None
        progress_callback = self._build_progress_callback(context) if context.event_queue is not None else None
        result = await self._manager.call_tool(
            self._record.server_name,
            self._record.tool_name,
            tool_input,
            progress_callback=progress_callback,
            meta=meta,
        )
        return convert_mcp_tool_result(
            result,
            server_name=self._record.server_name,
            tool_name=self._record.tool_name,
            session_id=self._session_id,
        )

    def _build_progress_callback(self, context: ToolContext):
        queue = context.event_queue
        assert queue is not None

        async def progress_callback(progress: Any, total: Any = None, message: Any = None) -> None:
            progress_value = _progress_field(
                progress,
                "progress",
                progress if isinstance(progress, int | float) else None,
            )
            total_value = _progress_field(progress, "total", total)
            message_value = _progress_field(progress, "message", message)
            await queue.put(
                MCPProgressEvent(
                    server_name=self._record.server_name,
                    tool_name=self._record.tool_name,
                    progress=_to_float(progress_value),
                    total=_to_float(total_value),
                    message=str(message_value) if message_value is not None else None,
                    tool_use_id=context.tool_use_id,
                )
            )

        return progress_callback


class ListMCPResourcesTool(Tool):
    name = "list_mcp_resources"
    description = _("List resources exposed by connected MCP servers.")
    input_schema = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": _("Optional MCP server name filter.")},
        },
        "additionalProperties": False,
    }

    def __init__(self, *, manager: Any) -> None:
        self._manager = manager

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        server_filter = tool_input.get("server")
        resources = [
            resource
            for resource in self._manager.list_resources()
            if not server_filter or resource.server_name == server_filter
        ]
        if not resources:
            return ToolResult.success(_("No MCP resources are currently available."))
        lines = [_format_resource_line(resource) for resource in resources]
        return ToolResult.success("\n".join(lines))


class ReadMCPResourceTool(Tool):
    name = "read_mcp_resource"
    description = _("Read a resource exposed by a connected MCP server.")
    input_schema = {
        "type": "object",
        "properties": {
            "uri": {"type": "string"},
            "server": {"type": "string"},
        },
        "required": ["server", "uri"],
        "additionalProperties": False,
    }

    def __init__(self, *, manager: Any, session_id: str) -> None:
        self._manager = manager
        self._session_id = session_id

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        server_name, result = await self._manager.read_resource(tool_input["uri"], server_name=tool_input["server"])
        contents = _get_value(result, "contents", [])
        content_blocks = [{"type": "resource", "resource": content} for content in contents]
        return convert_mcp_tool_result(
            {"content": content_blocks},
            server_name=server_name,
            tool_name="read_resource",
            session_id=self._session_id,
        )


class MCPAuthenticateTool(Tool):
    description = _("Start authentication for an MCP server.")
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(
        self,
        *,
        server_name: str,
        auth_url_factory: Callable[[str], Any] | None = None,
        auth_flow: Callable[[str], Any] | None = None,
    ) -> None:
        self._server_name = server_name
        self._auth_url_factory = auth_url_factory
        self._auth_flow = auth_flow

    @property
    def name(self) -> str:
        return "mcp__{}__authenticate".format(_safe_identifier(self._server_name))

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        if self._auth_flow is not None:
            message = self._auth_flow(self._server_name)
            if inspect.isawaitable(message):
                message = await message
            return ToolResult.success(str(message))

        if self._auth_url_factory is None:
            return ToolResult.error(
                _("No MCP authentication flow is configured for {server!r}.").format(server=self._server_name)
            )

        auth_url = self._auth_url_factory(self._server_name)
        if inspect.isawaitable(auth_url):
            auth_url = await auth_url
        return ToolResult.success(
            _("Open this URL to authenticate MCP server {server!r}:\n{url}").format(
                server=self._server_name,
                url=auth_url,
            )
        )


def _format_resource_line(resource: MCPResourceRecord) -> str:
    parts = [resource.server_name, resource.uri]
    if resource.name:
        parts.append(resource.name)
    if resource.mime_type:
        parts.append(resource.mime_type)
    return " | ".join(parts)


def _progress_field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _to_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    if hasattr(value, "model_dump") and key == "contents":
        dumped = value.model_dump(by_alias=True, mode="json")
        return dumped.get(key, default)
    return getattr(value, key, default)


def _safe_identifier(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "mcp"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
