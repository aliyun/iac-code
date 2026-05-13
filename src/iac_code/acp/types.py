from __future__ import annotations

import acp

ACPContentBlock = (
    acp.schema.TextContentBlock
    | acp.schema.ImageContentBlock
    | acp.schema.AudioContentBlock
    | acp.schema.ResourceContentBlock
    | acp.schema.EmbeddedResourceContentBlock
)

MCPServer = acp.schema.HttpMcpServer | acp.schema.SseMcpServer | acp.schema.McpServerStdio
