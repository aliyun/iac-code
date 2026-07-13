"""iac-code ACP runner customizations."""

from __future__ import annotations

from typing import Any

import acp
from acp.agent import connection as agent_connection
from acp.agent.connection import AgentSideConnection
from acp.agent.router import build_agent_router as _build_sdk_agent_router
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.meta import AGENT_METHODS
from acp.router import MessageRouter
from acp.utils import normalize_result
from pydantic import BaseModel, Field


class _NewSessionRequest(BaseModel):
    field_meta: dict[str, Any] | None = Field(default=None, alias="_meta")
    cwd: str
    mcp_servers: list[Any] | None = Field(default=None, alias="mcpServers")


class _SessionRequestWithMcpServers(BaseModel):
    field_meta: dict[str, Any] | None = Field(default=None, alias="_meta")
    cwd: str
    mcp_servers: list[Any] | None = Field(default=None, alias="mcpServers")
    session_id: str = Field(alias="sessionId")


def build_iac_agent_router(agent: acp.Agent, use_unstable_protocol: bool = False) -> MessageRouter:
    """Build an ACP router that tolerates forward-compatible MCP server entries.

    The ACP SDK request models strictly validate ``mcpServers`` against the
    transports known to that SDK version before iac-code can apply its own
    warning-and-skip conversion. Replacing only the session request models keeps
    all other ACP protocol validation unchanged.
    """
    router = _build_sdk_agent_router(agent, use_unstable_protocol=use_unstable_protocol)
    router.route_request(AGENT_METHODS["session_new"], _NewSessionRequest, agent, "new_session")
    router.route_request(
        AGENT_METHODS["session_load"],
        _SessionRequestWithMcpServers,
        agent,
        "load_session",
        adapt_result=normalize_result,
    )
    router.route_request(
        AGENT_METHODS["session_fork"],
        _SessionRequestWithMcpServers,
        agent,
        "fork_session",
        unstable=True,
    )
    router.route_request(
        AGENT_METHODS["session_resume"],
        _SessionRequestWithMcpServers,
        agent,
        "resume_session",
        unstable=True,
    )
    return router


async def run_iac_code_acp_agent(
    agent: acp.Agent,
    input_stream: Any = None,
    output_stream: Any = None,
    *,
    use_unstable_protocol: bool = False,
    stdio_buffer_limit_bytes: int = DEFAULT_STDIO_BUFFER_LIMIT_BYTES,
    **connection_kwargs: Any,
) -> None:
    """Run an iac-code ACP agent with iac-code's MCP-tolerant router."""
    if input_stream is None and output_stream is None:
        from acp.stdio import stdio_streams

        output_stream, input_stream = await stdio_streams(limit=stdio_buffer_limit_bytes)

    original_builder = agent_connection.build_agent_router
    setattr(agent_connection, "build_agent_router", build_iac_agent_router)
    try:
        conn = AgentSideConnection(
            agent,
            input_stream,
            output_stream,
            listening=False,
            use_unstable_protocol=use_unstable_protocol,
            **connection_kwargs,
        )
    finally:
        setattr(agent_connection, "build_agent_router", original_builder)

    await conn.listen()
