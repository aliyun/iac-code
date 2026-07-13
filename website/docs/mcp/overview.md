---
sidebar_position: 1
title: MCP Integration
description: Use Model Context Protocol servers to extend IaC Code with external tools, resources, prompts, and skills.
---

# MCP Integration

IaC Code can act as a Model Context Protocol (MCP) host. MCP servers extend the agent with external tools, resources, prompts, and reusable skills while still going through IaC Code's permission, session, logging, and output handling paths.

Use MCP when you want IaC Code to call a local or remote capability that is not built into the product, such as a private template catalog, an internal deployment reviewer, an inventory query service, or a specialized cloud operation tool.

## Supported Surfaces

| Surface | MCP support |
|---|---|
| Interactive REPL | Loads user, local, and approved project servers. Prompts before trusting new project `.mcp.json` servers. |
| Non-interactive mode | Loads user, local, and approved project servers. Never prompts; pending project servers are skipped with warnings. |
| ACP server | Accepts session MCP server configs from ACP clients and exposes discovered MCP capabilities inside that session. |
| A2A server | Loads MCP through the normal runtime and can publish MCP warnings and tool progress in A2A task metadata. |
| Pipeline mode | Uses the same runtime integrations as normal mode, including MCP tool progress and warning propagation. |

## Supported Capabilities

| Capability | Status |
|---|---|
| `stdio` transport | Supported for local MCP server processes. |
| Streamable HTTP transport | Supported for remote MCP servers. |
| SSE transport | Supported for remote MCP servers. |
| MCP tools | Exposed as agent tools named `mcp__<server>__<tool>`. |
| MCP resources | Exposed through `list_mcp_resources` and `read_mcp_resource`. |
| MCP prompts | Exposed as slash commands named `mcp__<server>__<prompt>`. |
| MCP `skill://` resources | Exposed as skill commands named `mcp__<server>__<skill>`. |
| OAuth loopback auth | Supported for remote servers with OAuth metadata. |
| `roots/list` | Supported. IaC Code returns the active workspace root as a file URI. |
| `list_changed` notifications | Supported for tools, resources, and prompts. Registrations refresh dynamically. |
| MCP elicitation | Supported in interactive sessions. Non-interactive runs cancel safely. URL elicitation can retry the original tool call after user confirmation. |
| WebSocket transport | Supported for URL-only `ws://` and `wss://` servers. Headers, `headersHelper`, and OAuth are rejected for WebSocket because the installed SDK transport accepts only a URL. |
| Dynamic `headersHelper` commands | Supported for trusted `http` and `sse` servers. Helpers run without a shell, with a bounded timeout, minimal environment, and redacted diagnostics. |
| SDK and IDE transports | Not supported. |
| IaC Code as an MCP server | Not supported. IaC Code currently acts as an MCP host only. |

## How It Works

At runtime IaC Code:

1. Loads MCP configuration from user, project, local, and session sources.
2. Expands `${VAR}` and `${VAR:-default}` references.
3. Skips unsafe or invalid servers with user-visible warnings.
4. Connects approved servers with bounded concurrency.
5. Discovers tools, resources, prompts, and `skill://` resources.
6. Registers those capabilities into the existing tool and command registries.
7. Injects connected server instructions into the agent prompt as server-scoped guidance.
8. Converts MCP tool results into normal IaC Code tool results, storing binary artifacts and large text artifacts under the runtime configuration directory.
9. Disconnects MCP clients when the REPL, headless run, ACP session, or A2A runtime closes.

One failed MCP server does not block other configured servers. Connection and discovery failures stay visible as MCP warnings.

## Naming

MCP tools and commands are normalized into public names:

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Characters outside letters, numbers, and underscores become underscores. If two discovered capabilities collide after normalization, IaC Code appends a short digest to keep names unique.

For MCP skills, IaC Code also registers a compatibility alias such as `<server>:<skill>` when that alias does not conflict with an existing command. Diagnostics preserve the original server, tool, prompt, or skill names even when public names are normalized.

## Related Pages

- [MCP Quick Start](./quick-start.md)
- [MCP Configuration](./configuration.md)
- [Tools, Resources, Prompts, and Skills](./capabilities.md)
- [OAuth and Security](./oauth-and-security.md)
- [Troubleshooting](./troubleshooting.md)
