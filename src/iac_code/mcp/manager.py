from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from iac_code.i18n import _
from iac_code.mcp.client import MCPClientAdapter, MCPClientProtocol
from iac_code.mcp.errors import MCPConnectionError, MCPElicitationUnsupportedError, MCPNeedsAuthError
from iac_code.mcp.oauth import MCPNeedsAuthCache, oauth_scope_identity
from iac_code.mcp.types import (
    MCPConnectionState,
    MCPPromptRecord,
    MCPResourceRecord,
    MCPToolRecord,
    MCPTransport,
    ScopedMCPServerConfig,
)
from iac_code.utils.public_errors import sanitize_public_text


@dataclass
class MCPConnectionRecord:
    scoped_config: ScopedMCPServerConfig
    state: MCPConnectionState = MCPConnectionState.PENDING
    client: MCPClientProtocol | None = None
    error: str | None = None
    retry_count: int = 0
    tools: list[MCPToolRecord] = field(default_factory=list)
    resources: list[MCPResourceRecord] = field(default_factory=list)
    prompts: list[MCPPromptRecord] = field(default_factory=list)
    capability_errors: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.scoped_config.name


ClientFactory = Callable[[ScopedMCPServerConfig], MCPClientProtocol]
ChangeListener = Callable[[str, str], Awaitable[None] | None]


class MCPManager:
    def __init__(
        self,
        configs: list[ScopedMCPServerConfig],
        *,
        client_factory: ClientFactory | None = None,
        roots: list[str | Path] | None = None,
        max_reconnect_attempts: int = 2,
        connect_timeout_seconds: float = 20.0,
        operation_timeout_seconds: float | None = None,
        max_concurrent_connections: int = 8,
        needs_auth_cache: MCPNeedsAuthCache | None = None,
        session_id: str | None = None,
    ) -> None:
        self._roots = [Path(root) for root in roots or []]
        self._client_factory = client_factory or self._default_client_factory
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connect_timeout_seconds = connect_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds or connect_timeout_seconds
        self._max_concurrent_connections = max_concurrent_connections
        self._needs_auth_cache = needs_auth_cache or MCPNeedsAuthCache()
        self._session_id = session_id
        self._change_listeners: list[ChangeListener] = []
        self._connections = {
            config.name: MCPConnectionRecord(scoped_config=config) for config in configs if config.approved
        }

    async def connect_all(self) -> None:
        records = list(self._connections.values())
        if self._max_concurrent_connections <= 1:
            for record in records:
                await self._connect(record)
        else:
            semaphore = asyncio.Semaphore(self._max_concurrent_connections)

            async def connect_record(record: MCPConnectionRecord) -> None:
                async with semaphore:
                    await self._connect(record)

            await asyncio.gather(*(connect_record(record) for record in records))
        self._assign_unique_public_names()

    async def disconnect_all(self) -> None:
        for record in self._connections.values():
            try:
                if record.client is not None:
                    await record.client.close()
            except Exception as exc:
                logger.debug(
                    "MCP server {!r} close failed: {}",
                    record.name,
                    sanitize_public_text(str(exc) or exc.__class__.__name__),
                )
            finally:
                record.client = None
                record.state = MCPConnectionState.DISABLED
                record.tools = []
                record.resources = []
                record.prompts = []
                record.capability_errors = {}

    async def reconnect_failed(self, server_name: str) -> None:
        record = self.connection(server_name)
        if record.state is not MCPConnectionState.FAILED:
            return
        if record.scoped_config.transport not in {MCPTransport.HTTP, MCPTransport.SSE}:
            return
        if record.retry_count >= self._max_reconnect_attempts:
            return

        record.retry_count += 1
        await self.reconnect(server_name)

    async def reconnect(self, server_name: str) -> None:
        record = self.connection(server_name)
        self._needs_auth_cache.clear(server_name)
        await self._connect(record)
        self._assign_unique_public_names()
        await self._notify_changed(server_name, "tools")
        await self._notify_changed(server_name, "resources")
        await self._notify_changed(server_name, "prompts")

    def connection(self, server_name: str) -> MCPConnectionRecord:
        return self._connections[server_name]

    def connection_state(self, server_name: str) -> MCPConnectionState:
        return self.connection(server_name).state

    def list_connections(self) -> list[MCPConnectionRecord]:
        return list(self._connections.values())

    def list_tools(self) -> list[MCPToolRecord]:
        return [tool for record in self._connections.values() for tool in record.tools]

    def list_resources(self) -> list[MCPResourceRecord]:
        return [resource for record in self._connections.values() for resource in record.resources]

    def list_prompts(self) -> list[MCPPromptRecord]:
        return [prompt for record in self._connections.values() for prompt in record.prompts]

    def needs_auth_servers(self) -> list[str]:
        return [record.name for record in self._connections.values() if record.state is MCPConnectionState.NEEDS_AUTH]

    def add_change_listener(self, listener: ChangeListener) -> None:
        self._change_listeners.append(listener)

    @property
    def operation_timeout_seconds(self) -> float:
        return self._operation_timeout_seconds

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        progress_callback: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        record = self.connection(server_name)
        client = _require_client(record)
        return await client.call_tool(tool_name, arguments=arguments, progress_callback=progress_callback, meta=meta)

    async def read_resource(self, uri: str, server_name: str) -> tuple[str, Any]:
        record = self.connection(server_name)
        client = _require_client(record)
        return record.name, await client.read_resource(uri)

    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict[str, str]) -> Any:
        record = self.connection(server_name)
        client = _require_client(record)
        return await client.get_prompt(prompt_name, arguments=arguments)

    async def handle_list_changed(self, server_name: str, *, capability: str) -> None:
        record = self.connection(server_name)
        if record.state is not MCPConnectionState.CONNECTED or record.client is None:
            return
        if capability == "tools":
            await self._refresh_tools(record)
        elif capability == "resources":
            await self._refresh_resources(record)
        elif capability == "prompts":
            await self._refresh_prompts(record)
        self._assign_unique_public_names()
        await self._notify_changed(server_name, capability)

    async def _notify_changed(self, server_name: str, capability: str) -> None:
        for listener in list(self._change_listeners):
            result = listener(server_name, capability)
            if inspect.isawaitable(result):
                await result

    async def list_roots(self) -> list[str]:
        return [root.resolve().as_uri() for root in self._roots]

    async def request_elicitation(self, server_name: str, params: MappingOrDict) -> None:
        raise MCPElicitationUnsupportedError(
            _("MCP server {server!r} requested elicitation, but iac-code does not support elicitation yet.").format(
                server=server_name
            )
        )

    async def _connect(self, record: MCPConnectionRecord) -> None:
        if record.client is not None:
            await record.client.close()
            record.client = None

        cached_auth = self._needs_auth_cache.get(record.name)
        if cached_auth is not None:
            record.state = MCPConnectionState.NEEDS_AUTH
            record.error = cached_auth.reason
            record.tools = []
            record.resources = []
            record.prompts = []
            record.capability_errors = {}
            return

        client = self._client_factory(record.scoped_config)
        try:
            await asyncio.wait_for(client.connect(), timeout=self._connect_timeout_seconds)
            record.client = client
            record.state = MCPConnectionState.CONNECTED
            record.error = None
            record.capability_errors = {}
            self._needs_auth_cache.clear(record.name)
            await self._refresh_discovery(record)
        except MCPNeedsAuthError as exc:
            reason = str(exc) or "authentication required"
            self._needs_auth_cache.mark(record.name, reason)
            record.state = MCPConnectionState.NEEDS_AUTH
            record.error = reason
            record.tools = []
            record.resources = []
            record.prompts = []
            record.capability_errors = {}
            try:
                await client.close()
            except Exception:
                pass
        except Exception as exc:
            with_context = str(exc) or exc.__class__.__name__
            record.state = MCPConnectionState.FAILED
            record.error = with_context
            record.tools = []
            record.resources = []
            record.prompts = []
            record.capability_errors = {}
            logger.warning(
                "MCP server {!r} connection failed: {}",
                record.name,
                sanitize_public_text(with_context),
            )
            try:
                await client.close()
            except Exception:
                pass

    async def _refresh_discovery(self, record: MCPConnectionRecord) -> None:
        await self._refresh_tools(record)
        await self._refresh_resources(record)
        await self._refresh_prompts(record)

    async def _refresh_tools(self, record: MCPConnectionRecord) -> None:
        client = _require_client(record)
        try:
            raw_tools = _extract_items(
                await asyncio.wait_for(client.list_tools(), timeout=self._operation_timeout_seconds),
                "tools",
            )
        except Exception as exc:
            _record_capability_error(record, "tools", exc)
            record.tools = []
            return
        record.capability_errors.pop("tools", None)
        record.tools = [
            MCPToolRecord(
                server_name=record.name,
                tool_name=str(_get_value(tool, "name", "")),
                public_name=_public_tool_name(record.name, str(_get_value(tool, "name", ""))),
                description=_get_value(tool, "description"),
                input_schema=_get_value(tool, "inputSchema", _get_value(tool, "input_schema", {})) or {},
                annotations=_get_value(tool, "annotations", {}) or {},
                meta=_get_value(tool, "_meta", _get_value(tool, "meta", {})) or {},
            )
            for tool in raw_tools
            if _get_value(tool, "name")
        ]

    async def _refresh_resources(self, record: MCPConnectionRecord) -> None:
        client = _require_client(record)
        try:
            raw_resources = _extract_items(
                await asyncio.wait_for(client.list_resources(), timeout=self._operation_timeout_seconds),
                "resources",
            )
        except Exception as exc:
            _record_capability_error(record, "resources", exc)
            record.resources = []
            return
        record.capability_errors.pop("resources", None)
        record.resources = [
            MCPResourceRecord(
                server_name=record.name,
                uri=str(_get_value(resource, "uri", "")),
                name=_get_value(resource, "name"),
                title=_get_value(resource, "title"),
                description=_get_value(resource, "description"),
                mime_type=_get_value(resource, "mimeType", _get_value(resource, "mime_type")),
                annotations=_get_value(resource, "annotations", {}) or {},
                meta=_get_value(resource, "_meta", _get_value(resource, "meta", {})) or {},
            )
            for resource in raw_resources
            if _get_value(resource, "uri")
        ]

    async def _refresh_prompts(self, record: MCPConnectionRecord) -> None:
        client = _require_client(record)
        try:
            raw_prompts = _extract_items(
                await asyncio.wait_for(client.list_prompts(), timeout=self._operation_timeout_seconds),
                "prompts",
            )
        except Exception as exc:
            _record_capability_error(record, "prompts", exc)
            record.prompts = []
            return
        record.capability_errors.pop("prompts", None)
        record.prompts = [
            MCPPromptRecord(
                server_name=record.name,
                prompt_name=str(_get_value(prompt, "name", "")),
                public_name=_public_prompt_name(record.name, str(_get_value(prompt, "name", ""))),
                description=_get_value(prompt, "description"),
                arguments=_get_value(prompt, "arguments", {}) or {},
                meta=_get_value(prompt, "_meta", _get_value(prompt, "meta", {})) or {},
            )
            for prompt in raw_prompts
            if _get_value(prompt, "name")
        ]

    def _default_client_factory(self, scoped_config: ScopedMCPServerConfig) -> MCPClientProtocol:
        async def on_list_changed(capability: str) -> None:
            await self.handle_list_changed(scoped_config.name, capability=capability)

        return MCPClientAdapter(
            scoped_config.config,
            roots=self._roots,
            scope=oauth_scope_identity(
                scoped_config.scope,
                source_path=scoped_config.source_path,
                session_id=self._session_id,
            ),
            list_changed_callback=on_list_changed,
        )

    def _assign_unique_public_names(self) -> None:
        tool_groups: dict[str, list[MCPToolRecord]] = {}
        for record in self._connections.values():
            for tool in record.tools:
                tool_groups.setdefault(_public_tool_name(tool.server_name, tool.tool_name), []).append(tool)

        replacements: dict[tuple[str, str], str] = {}
        for public_name, tools in tool_groups.items():
            if len(tools) <= 1:
                tool = tools[0]
                replacements[(tool.server_name, tool.tool_name)] = public_name
                continue
            for tool in tools:
                replacements[(tool.server_name, tool.tool_name)] = "{}_{}".format(
                    public_name,
                    _short_digest(tool.server_name, tool.tool_name),
                )

        for record in self._connections.values():
            if not record.tools:
                continue
            record.tools = [
                replace(tool, public_name=replacements.get((tool.server_name, tool.tool_name), tool.public_name))
                for tool in record.tools
            ]

        command_groups: dict[str, list[tuple[str, str, Any]]] = {}
        for record in self._connections.values():
            for prompt in record.prompts:
                command_groups.setdefault(
                    _public_prompt_name(prompt.server_name, prompt.prompt_name),
                    [],
                ).append(("prompt", prompt.prompt_name, prompt))
            for resource in record.resources:
                if resource.is_skill_resource:
                    command_groups.setdefault(
                        _public_resource_name(resource.server_name, resource.name or "skill"),
                        [],
                    ).append(("resource", resource.uri, resource))

        command_replacements: dict[tuple[str, str, str], str] = {}
        for public_name, entries in command_groups.items():
            if len(entries) <= 1:
                kind, identifier, item = entries[0]
                command_replacements[(kind, item.server_name, identifier)] = public_name
                continue
            for kind, identifier, item in entries:
                command_replacements[(kind, item.server_name, identifier)] = "{}_{}".format(
                    public_name,
                    _short_digest(kind, item.server_name, identifier),
                )

        for record in self._connections.values():
            if record.prompts:
                record.prompts = [
                    replace(
                        prompt,
                        public_name=command_replacements.get(
                            ("prompt", prompt.server_name, prompt.prompt_name),
                            prompt.public_name,
                        ),
                    )
                    for prompt in record.prompts
                ]
            if record.resources:
                record.resources = [
                    replace(
                        resource,
                        public_name=(
                            command_replacements.get(("resource", resource.server_name, resource.uri))
                            if resource.is_skill_resource
                            else resource.public_name
                        ),
                    )
                    for resource in record.resources
                ]


MappingOrDict = dict[str, Any]


def _require_client(record: MCPConnectionRecord) -> MCPClientProtocol:
    if record.client is None:
        raise MCPConnectionError(_("MCP server {server!r} is not connected.").format(server=record.name))
    return record.client


def _extract_items(value: Any, field_name: str) -> list[Any]:
    if isinstance(value, list):
        return value
    items = _get_value(value, field_name)
    if isinstance(items, list):
        return items
    return []


def _record_capability_error(record: MCPConnectionRecord, capability: str, exc: Exception) -> None:
    message = sanitize_public_text(str(exc) or exc.__class__.__name__)
    record.error = message
    record.capability_errors[capability] = message
    logger.warning(
        "MCP server {!r} {} discovery failed: {}",
        record.name,
        capability,
        message,
    )


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    if key == "_meta":
        return getattr(value, "meta", default)
    return getattr(value, key, default)


def _public_tool_name(server_name: str, tool_name: str) -> str:
    return "mcp__{}__{}".format(_safe_identifier(server_name), _safe_identifier(tool_name))


def _public_prompt_name(server_name: str, prompt_name: str) -> str:
    return "mcp__{}__{}".format(_safe_identifier(server_name), _safe_identifier(prompt_name))


def _public_resource_name(server_name: str, resource_name: str) -> str:
    return "mcp__{}__{}".format(_safe_identifier(server_name), _safe_identifier(resource_name))


def _safe_identifier(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "mcp"


def _short_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:8]
