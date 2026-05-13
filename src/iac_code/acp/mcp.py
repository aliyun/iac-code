"""MCP server configuration conversion module.

Converts MCP server configurations from the ACP client into the internal format.
"""

from __future__ import annotations

import logging
from typing import Any

from iac_code.acp.types import MCPServer

logger = logging.getLogger(__name__)


def convert_mcp_configs(mcp_servers: list[MCPServer]) -> list[dict[str, Any]]:
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


def _convert_single_server(server: MCPServer) -> dict[str, Any] | None:
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
    else:
        logger.warning("Unsupported MCP server type: %s", type(server).__name__)
        return None
