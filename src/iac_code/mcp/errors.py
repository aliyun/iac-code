from __future__ import annotations


class MCPError(Exception):
    """Base exception for iac-code MCP integration failures."""


class MCPConnectionError(MCPError):
    """Raised when an MCP server connection fails."""


class MCPNeedsAuthError(MCPConnectionError):
    """Raised when a remote MCP server requires OAuth authentication."""


class MCPElicitationUnsupportedError(MCPError):
    """Raised when an MCP server asks for unsupported user elicitation."""
