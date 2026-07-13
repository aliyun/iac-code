"""MCP server configuration conversion module.

Converts MCP server configurations from the ACP client into the internal format.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, cast

logger = logging.getLogger(__name__)


def convert_mcp_configs(mcp_servers: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert ACP MCP server configurations to the internal format.

    Args:
        mcp_servers: List of MCP server configurations from the ACP SDK.

    Returns:
        List of converted internal MCP configurations.
    """
    configs: list[dict[str, Any]] = []
    for server in mcp_servers:
        config = _convert_single_server(server)
        if config:
            configs.append(config)
    return configs


def _convert_single_server(server: Any) -> dict[str, Any] | None:
    """Convert a single MCP server configuration."""
    import acp

    if isinstance(server, acp.schema.McpServerStdio):
        return {
            "type": "stdio",
            "command": server.command,
            "args": list(server.args),
            "env": {v.name: v.value for v in server.env} if server.env else {},
            "name": server.name,
        }
    elif isinstance(server, (acp.schema.SseMcpServer, acp.schema.HttpMcpServer)):
        return {
            "type": getattr(server, "type", "sse"),
            "url": server.url,
            "headers": {h.name: h.value for h in server.headers} if server.headers else {},
            "name": server.name,
        }
    elif isinstance(server, Mapping):
        return _convert_mapping_server(server)
    else:
        logger.warning("Unsupported MCP server type: %s", type(server).__name__)
        return None


def _convert_mapping_server(server: Mapping[str, Any]) -> dict[str, Any] | None:
    name = server.get("name")
    if not isinstance(name, str) or not name:
        logger.warning("Invalid MCP server config: missing name")
        return None
    converted = dict(server)
    converted["name"] = name
    if "headers" in converted:
        converted["headers"] = _name_value_entries_to_dict(converted["headers"])
    if "env" in converted:
        converted["env"] = _name_value_entries_to_dict(converted["env"])
    return converted


def _name_value_entries_to_dict(value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, Any], value)
        return {key: item for key, item in mapping.items() if isinstance(key, str) and isinstance(item, str)}
    if not isinstance(value, list):
        return {}
    result: dict[str, str] = {}
    for item in value:
        if isinstance(item, Mapping):
            entry = cast(Mapping[str, Any], item)
            name = entry.get("name")
            entry_value = entry.get("value")
        else:
            name = getattr(item, "name", None)
            entry_value = getattr(item, "value", None)
        if isinstance(name, str) and isinstance(entry_value, str):
            result[name] = entry_value
    return result
